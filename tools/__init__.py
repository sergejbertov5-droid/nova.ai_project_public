"""NOVA AI tools package.

Пакет не тянет за собой PyObjC: импорт tools.maps поднимал
Foundation/CoreLocation, и без них не импортировался весь `tools`,
включая инструменты, которые PyObjC не используют вовсе. Теперь
инструменты доступны как модули (`from tools.system import ...`), а
через пакетный импорт доступны только гарантированно безопасные.
"""

__all__ = [
    "maps_location",
    "maps_weather",
    "maps_search",
    "maps_directions",
    "get_current_location",
]


def __getattr__(name):
    """Ленивый реэкспорт: подтягиваем tools.maps только по требованию.

    `from tools import maps_weather` работает как раньше, но сам пакет
    импортируется без PyObjC. Если PyObjC нет, ошибка возникает в
    момент реального использования геолокации — с понятным текстом,
    а не падением пакета на import.
    """
    if name in __all__:
        from tools import maps

        return getattr(maps, name)
    raise AttributeError(f"module 'tools' has no attribute {name!r}")
