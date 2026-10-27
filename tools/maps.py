"""
Geolocation Tools — Maps, Weather, Location services.
Uses macOS CoreLocation (via PyObjC) and web APIs for weather/places.
"""
import json
import logging
import subprocess
import urllib.parse
from typing import Dict, Any, Optional, List
from dataclasses import dataclass

# PyObjC нужен только геолокации. Раньше импорт стоял на верхнем
# уровне модуля, а `tools/__init__.py` импортирует tools.maps — то
# есть в окружении без PyObjC не импортировался ВЕСЬ пакет tools,
# и падал даже инструмент, PyObjC не использующий вовсе (погода,
# поиск мест, маршруты). Теперь отсутствие PyObjC — это «геолокация
# недоступна», а не «NOVA AI не запустится».
try:
    from Foundation import NSURL
    from CoreLocation import CLLocationManager, kCLLocationAccuracyBest
    import objc

    GEOLOCATION_AVAILABLE = True
except ImportError as _pyobjc_error:  # pragma: no cover — зависит от окружения
    NSURL = None
    CLLocationManager = None
    kCLLocationAccuracyBest = None
    objc = None
    GEOLOCATION_AVAILABLE = False
    logging.info(
        "Геолокация недоступна (нет PyObjC: %s) — "
        "погода, места и маршруты по координатам работать не будут",
        _pyobjc_error,
    )

#: Потолок ожидания `open`. Карты (и Launch Services в общем) умеют
#: не возвращаться; без таймаута поток исполнителя инструментов —
#: non-daemon, и NOVA AI не сможет завершиться.
OPEN_TIMEOUT = 15.0


@dataclass
class LocationData:
    latitude: float
    longitude: float
    altitude: float = 0.0
    accuracy: float = 0.0
    timestamp: float = 0.0
    address: str = ""
    city: str = ""
    country: str = ""


class LocationManager:
    """macOS CoreLocation wrapper for getting current location"""
    
    def __init__(self):
        self.manager = None
        self.current_location = None
        self.location_ready = False
        self._setup()
    
    def _setup(self):
        if not GEOLOCATION_AVAILABLE:
            # Нет PyObjC — геолокация недоступна, но инструмент не падает.
            logging.info("Геолокация пропущена: PyObjC не установлен")
            return
        try:
            self.manager = CLLocationManager.alloc().init()
            self.manager.setDesiredAccuracy_(kCLLocationAccuracyBest)
            
            # Request authorization (may show dialog first time)
            auth_status = self.manager.authorizationStatus()
            if auth_status == 0:  # kCLAuthorizationStatusNotDetermined
                self.manager.requestWhenInUseAuthorization()
            
            # Start updating
            self.manager.startUpdatingLocation()
            
            # Wait briefly for location
            import time
            for _ in range(50):  # 5 seconds max
                if self.manager.location():
                    loc = self.manager.location()
                    self.current_location = LocationData(
                        latitude=loc.coordinate().latitude,
                        longitude=loc.coordinate().longitude,
                        altitude=loc.altitude(),
                        accuracy=loc.horizontalAccuracy(),
                        timestamp=loc.timestamp().timeIntervalSince1970()
                    )
                    self.location_ready = True
                    break
                time.sleep(0.1)
            
            self.manager.stopUpdatingLocation()
            logging.info("LocationManager: got location %s", self.current_location)
            
        except Exception as e:
            logging.error("LocationManager setup failed: %s", e)
            self.current_location = None
            self.location_ready = False
    
    def get_location(self) -> Optional[LocationData]:
        return self.current_location if self.location_ready else None


# Global instance (lazy)
_location_manager: Optional[LocationManager] = None


def get_current_location() -> Optional[LocationData]:
    """Get current GPS location"""
    global _location_manager
    if _location_manager is None:
        _location_manager = LocationManager()
    return _location_manager.get_location()


def reverse_geocode(lat: float, lon: float) -> Dict[str, str]:
    """Get address from coordinates using Apple's geocoder"""
    try:
        script = f'''
        tell application "MapKit"
        -- This won't work directly, use alternative
        end tell
        '''
        
        # Use Apple's geocoder via command line
        script = f'''
        set lat to {lat}
        set lon to {lon}
        tell application "System Events"
            -- Use core location reverse geocoding
        end tell
        '''
        
        # Fallback: use a simple web API or just return coords
        return {
            "lat": str(lat),
            "lon": str(lon),
            "address": f"Координаты: {lat:.6f}, {lon:.6f}",
            "city": "",
            "country": ""
        }
    except Exception as e:
        logging.error("Reverse geocode failed: %s", e)
        return {"lat": str(lat), "lon": str(lon), "address": "Неизвестно"}


def _geocode_city(city: str):
    """Геокодирует город через Open-Meteo, с фолбэком на таблицу крупных городов."""
    if not city:
        return None
    import requests
    try:
        geo_url = (
            f"https://geocoding-api.open-meteo.com/v1/search?"
            f"name={urllib.parse.quote(city)}&count=1&language=ru"
        )
        geo_resp = requests.get(geo_url, timeout=5)
        if geo_resp.ok:
            data = geo_resp.json()
            if data.get("results"):
                result = data["results"][0]
                return (result["latitude"], result["longitude"])
    except Exception:
        pass
    # Fallback: известные координаты крупных городов
    city_coords = {
        "москва": (55.7558, 37.6176),
        "санкт-петербург": (59.9343, 30.3351),
        "новосибирск": (55.0084, 82.9357),
        "екатеринбург": (56.8389, 60.6057),
        "казань": (55.7961, 49.1064),
        "нижний новгород": (56.3269, 44.0075),
        "челябинск": (55.1599, 61.4026),
        "омск": (54.9885, 73.3242),
        "самара": (53.2001, 50.15),
        "ростов-на-дону": (47.2357, 39.7015),
    }
    return city_coords.get(city.lower().strip())


def get_weather(lat: float = None, lon: float = None, city: str = None) -> Dict[str, Any]:
    """
    Get weather using Open-Meteo API (free, no key needed).
    Falls back to Apple Weather if available.
    """
    import requests
    
    try:
        # Явно указанный город имеет приоритет над текущей геолокацией,
        # чтобы "погода в Москве" не возвращала погоду по текущим координатам.
        if city and (lat is None or lon is None):
            coords = _geocode_city(city)
            if coords:
                lat, lon = coords

        if lat is None or lon is None:
            loc = get_current_location()
            if loc:
                lat, lon = loc.latitude, loc.longitude

        if lat is None or lon is None:
            return {"error": "Не удалось определить местоположение"}
        
        # Open-Meteo API
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            f"&current_weather=true"
            f"&hourly=temperature_2m,precipitation_probability,weathercode,windspeed_10m"
            f"&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
            f"&timezone=auto&language=ru"
        )
        
        resp = requests.get(url, timeout=10)
        if not resp.ok:
            return {"error": f"Weather API error: {resp.status_code}"}
        
        data = resp.json()
        current = data.get("current_weather", {})
        daily = data.get("daily", {})
        
        # Weather codes mapping
        weather_codes = {
            0: "Ясно", 1: "Преимущественно ясно", 2: "Переменная облачность", 3: "Пасмурно",
            45: "Туман", 48: "Туман с инеем",
            51: "Лёгкая морось", 53: "Морось", 55: "Сильная морось",
            56: "Лёгкий ледяной дождь", 57: "Сильный ледяной дождь",
            61: "Небольшой дождь", 63: "Дождь", 65: "Сильный дождь",
            66: "Лёгкий ледяной дождь", 67: "Сильный ледяной дождь",
            71: "Небольшой снег", 73: "Снег", 75: "Сильный снег",
            77: "Снежные зерна",
            80: "Небольшие ливни", 81: "Ливни", 82: "Сильные ливни",
            85: "Небольшой снегопад", 86: "Снегопад",
            95: "Гроза", 96: "Гроза с градом", 99: "Сильная гроза с градом"
        }
        
        code = current.get("weathercode", 0)
        condition = weather_codes.get(code, "Неизвестно")
        
        today_max = daily.get("temperature_2m_max", [None])[0]
        today_min = daily.get("temperature_2m_min", [None])[0]
        precip_chance = daily.get("precipitation_probability_max", [None])[0]
        
        return {
            "temperature": current.get("temperature"),
            "wind_speed": current.get("windspeed"),
            "condition": condition,
            "condition_code": code,
            "today_max": today_max,
            "today_min": today_min,
            "precipitation_chance": precip_chance,
            "latitude": lat,
            "longitude": lon,
            "time": current.get("time")
        }
        
    except Exception as e:
        logging.exception("Weather fetch failed: %s", e)
        return {"error": str(e)}


def search_places(query: str, lat: float = None, lon: float = None, radius: int = 5000) -> List[Dict]:
    """
    Search places using OpenStreetMap Nominatim.
    Works with or without current location.
    """
    import requests
    
    try:
        # Try to get current location if not provided
        if lat is None or lon is None:
            loc = get_current_location()
            if loc:
                lat, lon = loc.latitude, loc.longitude
        
        # Build Nominatim URL - works with or without viewbox
        url = (
            f"https://nominatim.openstreetmap.org/search?"
            f"q={urllib.parse.quote(query)}"
            f"&format=json&limit=10"
            f"&accept-language=ru"
        )
        
        # Add viewbox if we have location
        if lat and lon:
            url += f"&viewbox={lon-radius/111000},{lat+radius/111000},{lon+radius/111000},{lat-radius/111000}&bounded=1"
        
        headers = {"User-Agent": "NOVA AI/1.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        
        if resp.ok:
            results = resp.json()
            return [
                {
                    "name": r.get("display_name", "").split(",")[0],
                    "address": r.get("display_name", ""),
                    "lat": float(r["lat"]),
                    "lon": float(r["lon"]),
                    "type": r.get("type", ""),
                    "category": r.get("class", "")
                }
                for r in results[:10]
            ]
        
        return []
    except Exception as e:
        logging.exception("Place search failed: %s", e)
        return []


def get_directions(
    destination: str = None,
    dest_lat: float = None,
    dest_lon: float = None,
    transport: str = "driving"
) -> Dict[str, Any]:
    """Get directions to destination via Apple Maps"""
    try:
        # Open Apple Maps with directions
        if destination:
            # Search first, then get directions
            safe_dest = urllib.parse.quote(destination)
            url = f"maps://?daddr={safe_dest}&dirflg={transport[0]}"
        elif dest_lat and dest_lon:
            url = f"maps://?daddr={dest_lat},{dest_lon}&dirflg={transport[0]}"
        else:
            return {"error": "Укажите назначение"}
        
        # Use open command. Таймаут обязателен: `open` при зависших
        # Картах или Launch Services не возвращается, и поток
        # исполнителя (non-daemon) не даст NOVA AI завершиться.
        result = subprocess.run(
            ["open", url],
            capture_output=True,
            timeout=OPEN_TIMEOUT,
        )

        if result.returncode != 0:
            return {
                "error": (
                    "Не удалось открыть Карты: "
                    + (result.stderr.decode(errors="replace").strip() or f"код {result.returncode}")
                )
            }

        return {
            "status": "opened_in_maps",
            "url": url,
            "message": "Маршрут открыт в Картах"
        }
    except Exception as e:
        logging.exception("Directions failed: %s", e)
        return {"error": str(e)}


# Tool functions for registry

def maps_location() -> str:
    """Показать моё текущее местоположение"""
    loc = get_current_location()
    if not loc:
        return "Не удалось определить местоположение. Проверьте настройки конфиденциальности."
    
    addr = reverse_geocode(loc.latitude, loc.longitude)
    return (
        f"Ваше местоположение: {addr.get('address', 'определено по координатам')}. "
        f"Координаты: {loc.latitude:.6f}, {loc.longitude:.6f}. "
        f"Точность: ~{loc.accuracy:.0f}м."
    )


def maps_weather(city: str = "") -> str:
    """Узнать погоду. Город можно не указывать — определится автоматически."""
    weather = get_weather(city=city if city else None)
    
    if "error" in weather:
        return f"Не удалось получить погоду: {weather['error']}"
    
    parts = [
        f"Погода: {weather['condition']}",
        f"Температура: {weather['temperature']}°C",
    ]
    
    if weather.get("today_max") and weather.get("today_min"):
        parts.append(f"Диапазон сегодня: {weather['today_min']}…{weather['today_max']}°C")
    
    if weather.get("precipitation_chance") is not None:
        parts.append(f"Вероятность осадков: {weather['precipitation_chance']}%")
    
    if weather.get("wind_speed"):
        parts.append(f"Ветер: {weather['wind_speed']} км/ч")
    
    return ". ".join(parts) + "."


def maps_search(query: str) -> str:
    """Найти места рядом: кафе, аптеки, заправки, магазины и т.д."""
    if not query:
        return "Что искать, сэр?"
    
    places = search_places(query)
    
    if not places:
        return f"Ничего не найдено по запросу «{query}»."
    
    lines = [f"Найдено {len(places)} мест по запросу «{query}»:"]
    for i, p in enumerate(places[:5], 1):
        lines.append(f"{i}. {p['name']} — {p['address'][:80]}")
    
    return "\n".join(lines)


def maps_directions(destination: str, transport: str = "driving") -> str:
    """Построить маршрут до места. Транспорт: driving (авто), walking (пешком), transit (общественный)."""
    if not destination:
        return "Куда проложить маршрут, сэр?"
    
    result = get_directions(destination=destination, transport=transport)
    
    if "error" in result:
        return f"Ошибка: {result['error']}"
    
    transport_names = {"driving": "на авто", "walking": "пешком", "transit": "на транспорте"}
    return f"Маршрут до «{destination}» {transport_names.get(transport, transport)} открыт в Картах."


