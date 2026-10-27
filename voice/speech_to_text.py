"""
Распознавание речи для NOVA AI: постоянный поток микрофона + локальный Vosk.

Что исправлено (по реальным ошибкам из logs/nova_ai-errors.jsonl):
1. «STT error: 'NoneType' object has no attribute 'close'» — 1465 раз.
   Причина в speech_recognition: Microphone.__enter__ ПРОГЛАТЫВАЕТ ошибку
   открытия потока и оставляет self.stream = None. Следующий вызов
   Recognizer.listen() падал с AssertionError, а __exit__ затем обращался
   к None.close(). Здесь поток микрофона открывается и проверяется ЯВНО,
   без магического контекстного менеджера sr.Microphone.
2. CoreAudio «Audio Unit: cannot do in current context» (-10863) — поток
   микрофона открывался/закрывался на каждом цикле (раз в ~8 секунд).
   Теперь открыт ОДИН постоянный поток, чтение идёт непрерывно в фоне.
3. «STT error: recognition connection failed / timed out» — Google STT
   (legacy-endpoint) гео-блокируется и отваливается по сети. Теперь
   основной движок — локальный Vosk (работает без интернета), Google
   остался только запасным вариантом.
4. Второй параллельный поток микрофона больше не открывается: замеры
   уровня идут из уже открытого потока (см. _ACTIVE_READERS).
"""
import collections
import json
import logging
import os
import threading
import time

# Константы, доступные без pyaudio
MIC_DEAD_LEVEL = 5.0
_MIN_VOICE = 350.0
_NOISE_CEILING = 1500.0
_MAX_BUFFER_SECONDS = 10.0
_WAKE_WINDOW = 12.0

# Пытаемся импортировать pyaudio; если нет — оставляем заглушки.
# Реальные классы загружаются лениво при первом обращении.
_pyaudio = None
_sr = None
_audioop = None

try:
    import pyaudio as _pyaudio
    import speech_recognition as _sr
except ImportError:
    _pyaudio = None
    _sr = None

try:
    import audioop as _audioop
except ImportError:
    _audioop = None

import voice.state as state

from config import STT_MIC_INDEX, STT_LANGUAGE, STT_ENGINE, STT_VOSK_MODEL

# 16 кГц — стандарт Vosk; Google STT тоже принимает такой поток.
_SAMPLE_RATE = 16000
_CHUNK = 1024                      # ~64 мс
_RATE_FALLBACKS = (16000, 44100, 48000, 22050)
_SOUND_THRESHOLD = 40.0            # «по микрофону уверенно идёт звук» (RMS)
# Микрофон с выданным доступом почти всегда даёт хоть минимальный фоновый
# уровень (в тихой комнате это 20-100). Ровно ноль — почти наверняка запрет
# доступа (TCC) или микрофон переключён в mute.
_MIN_VOICE = 350.0                 # минимальный порог «идёт речь»
_NOISE_CEILING = 1500.0            # потолок порога для очень шумной комнаты
_MAX_BUFFER_SECONDS = 10.0         # буфер фонового чтения
_WAKE_WINDOW = 12.0                # окно «без слова-будильника», сек

# 16 кГц — стандарт Vosk; Google STT тоже принимает такой поток.
_SAMPLE_RATE = 16000
_CHUNK = 1024                      # ~64 мс
_RATE_FALLBACKS = (16000, 44100, 48000, 22050)
_SOUND_THRESHOLD = 40.0            # «по микрофону уверенно идёт звук» (RMS)
# Микрофон с выданным доступом почти всегда даёт хоть минимальный фоновый
# уровень (в тихой комнате это 20-100). Ровно ноль — почти наверняка запрет
# доступа (TCC) или микрофон переключён в mute.
MIC_DEAD_LEVEL = 5.0
_MIN_VOICE = 350.0                 # минимальный порог «идёт речь»
_NOISE_CEILING = 1500.0            # потолок порога для очень шумной комнаты
_MAX_BUFFER_SECONDS = 10.0         # буфер фонового чтения
_WAKE_WINDOW = 12.0                # окно «без слова-будильника», сек

try:                                # audioop есть в 3.11, но исчезнет в 3.13
    import audioop
except ImportError:                 # pragma: no cover
    audioop = None


def _rms16(data: bytes) -> float:
    """RMS 16-битного моно-сигнала (запасной путь без audioop)."""
    if audioop is not None:
        return float(audioop.rms(data, 2))
    import array

    samples = array.array("h")
    samples.frombytes(bytes(data[: len(data) - (len(data) % 2)]))
    if not samples:
        return 0.0
    total = 0
    for value in samples:
        total += value * value
    return (total / len(samples)) ** 0.5


def _max16(data: bytes) -> int:
    if audioop is not None:
        return int(audioop.max(data, 2))
    import array

    samples = array.array("h")
    samples.frombytes(bytes(data[: len(data) - (len(data) % 2)]))
    return max((abs(v) for v in samples), default=0)


def _resample16(data: bytes, rate: int, target: int = _SAMPLE_RATE) -> bytes:
    """Пересчёт 16-битного моно из rate в target (для Vosk)."""
    if rate == target or audioop is None:
        return data
    converted, _ = audioop.ratecv(data, 2, 1, rate, target, None)
    return converted


class MicUnavailable(RuntimeError):
    """Микрофон не открылся или поток отвалился — нужно пересоздать."""


# Активные (уже открытые) потоки: mic_index -> _MicReader. Нужен, чтобы
# замеры уровня не открывали второй поток на том же устройстве.
_ACTIVE_READERS = {}
_ACTIVE_LOCK = threading.Lock()

# Загруженные модели Vosk: path -> Model (чтобы не перечитывать модель
# при каждом пересоздании микрофона).
_VOSK_MODELS = {}


class _MicReader:
    """
    Постоянно открытый входной поток микрофона с фоновым чтением.

    Накапливает кадры в ограниченный буфер и отдаёт их потребителю.
    Никаких вложенных `with microphone` — поток живёт, пока жив объект.
    """

    def __init__(self, mic_index=None, rate=_SAMPLE_RATE):
        self.mic_index = mic_index
        self.rate = rate
        self._pa = None
        self._stream = None
        self._thread = None
        self._stop = threading.Event()
        self._cond = threading.Condition()
        self._frames = collections.deque()
        self._level = 0.0
        # Сглаженный уровень: мгновенный скачет между паузой и звуком не
        # должен выглядеть как «микрофон ожил», иначе зря пересоздаём поток.
        self._level_smooth = 0.0
        self._error = None
        self._closed = False
        # Оценка «тишины» (адаптивный шумовой пол). От неё считается порог
        # речи. Важно: порог НЕ строится на мгновенном уровне — иначе
        # при Continuous speech условие «уровень >= уровень*1.6» не
        # выполняется никогда и фраза не начинается.
        self._noise = None
        # Лимит буфера задаём ДО запуска фонового потока, иначе поток
        # упадёт с AttributeError на первом же кадре.
        self._max_frames = max(8, int(_MAX_BUFFER_SECONDS * self.rate / _CHUNK))
        # На одном устройстве не должно быть двух потоков: macOS на такой
        # дубликат отвечает «Audio Unit: cannot do in current context» (-10863).
        # Если предыдущий поток ещё жив — освобождаем его ДО открытия нового.
        with _ACTIVE_LOCK:
            previous = _ACTIVE_READERS.get(self.mic_index)
        if previous is not None and previous is not self:
            logging.info(
                "Микрофон: освобождаю предыдущий поток входа [%s]", self.mic_index
            )
            try:
                previous.close()
            except Exception:
                logging.debug("Предыдущий поток микрофона не закрылся", exc_info=True)
        self._open()
        with _ACTIVE_LOCK:
            _ACTIVE_READERS[self.mic_index] = self

    # ---------- открытие / чтение ----------
    def _open(self) -> None:
        last_error = None
        for rate in _RATE_FALLBACKS:
            pa = None
            try:
                pa = _pyaudio.PyAudio()
                stream = pa.open(
                    format=_pyaudio.paInt16,
                    channels=1,
                    rate=rate,
                    input=True,
                    input_device_index=self.mic_index,
                    frames_per_buffer=_CHUNK,
                )
                self._pa, self._stream, self.rate = pa, stream, rate
                # Частота могла отличаться от запрошенной — пересчитываем
                # лимит буфера до старта фонового потока.
                self._max_frames = max(
                    8, int(_MAX_BUFFER_SECONDS * rate / _CHUNK)
                )
                self._stop.clear()
                # Прогрев делаем ДО запуска фонового потока: два потока,
                # читающие один pyaudio-поток, — верный путь к SIGSEGV.
                self._warmup()
                self._thread = threading.Thread(
                    target=self._pump, name="nova_ai-mic", daemon=True
                )
                self._thread.start()
                return
            except Exception as exc:
                last_error = exc
                if pa is not None:
                    try:
                        pa.terminate()
                    except Exception:
                        pass
                time.sleep(0.2)
        raise MicUnavailable(f"микрофон недоступен: {last_error}")

    def _warmup(self) -> None:
        """Первые кадры после open() часто мусорные/тихие — выбрасываем."""
        deadline = time.monotonic() + 0.35
        while time.monotonic() < deadline:
            try:
                self._stream.read(_CHUNK, exception_on_overflow=False)
            except Exception:
                break
            time.sleep(0.01)

    def _pump(self) -> None:
        """Фоновое чтение кадров в буфер (daemon-поток).

        ВАЖНО: этот поток — единственный, кто трогает pyaudio-поток. Закрытие
        микрофона из другого потока, пока здесь идёт read(), рвёт память
        PortAudio и убивает процесс с SIGSEGV (см. crash-репорты:
        PaUtil_ReadRingBuffer → _platform_memmove). Поэтому закрывает поток
        тоже он сам, в finally.
        """
        try:
            while not self._stop.is_set():
                data = self._stream.read(_CHUNK, exception_on_overflow=False)
                if not data:
                    continue
                self._update_level(_rms16(data))
                self._update_noise(self._level)
                with self._cond:
                    self._frames.append(data)
                    while len(self._frames) > self._max_frames:
                        self._frames.popleft()
                    self._cond.notify_all()
        except Exception as exc:          # устройство отвалилось/усыплено
            self._error = exc
            logging.warning("Микрофон: чтение прервано (%s)", exc)
        finally:
            with self._cond:
                self._cond.notify_all()
            # Освобождаем CoreAudio здесь же — в том же потоке, который читал.
            self._release_stream()

    def _update_level(self, energy: float) -> None:
        """Мгновенный уровень + сглаженный (быстрый подъём, медленное затухание).

        Сглаженный уровень нужен для решения «микрофон вообще молчит?»:
        одиночный тихий кадр между словами не должен выглядеть как поломка
        и вызывать пересоздание потока микрофона.

        getattr — обязателен: подклассы (тестовые, файловые потоки) могут
        переопределять __init__ и не задавать сглаженный уровень. Без этого
        capture() падал бы с AttributeError и молча возвращал пустую фразу.
        """
        smooth = getattr(self, "_level_smooth", 0.0)
        self._level = energy
        if energy >= smooth:
            self._level_smooth = energy
        else:
            self._level_smooth = smooth + (energy - smooth) * 0.05

    def _next_frame(self, timeout: float) -> bytes:
        with self._cond:
            if not self._frames:
                self._cond.wait(timeout)
            if self._frames:
                return self._frames.popleft()
        return b""

    def _check_error(self) -> None:
        if self._error is not None:
            error, self._error = self._error, None
            raise MicUnavailable(f"поток микрофона прерван: {error}")

    def flush(self) -> None:
        """Выбросить накопившиеся кадры (начало новой фразы)."""
        with self._cond:
            self._frames.clear()

    def level(self) -> float:
        """Сглаженный уровень звука (RMS) — «есть ли вообще звук».

        Именно сглаженный: одиночный тихий кадр между словами — это не
        поломка микрофона, и по нему нельзя делать выводы.
        """
        self._check_error()
        return self._level_smooth

    def instant_level(self) -> float:
        """Уровень последнего кадра (для диагностики)."""
        return self._level

    def measure(self, duration: float = 1.0) -> tuple:
        """Замер уровня по текущему потоку. Возвращает (rms, max)."""
        self._check_error()
        self.flush()
        chunks = []
        deadline = time.monotonic() + max(0.05, duration)
        while time.monotonic() < deadline:
            frame = self._next_frame(timeout=0.1)
            if frame:
                chunks.append(frame)
        audio = b"".join(chunks)
        if not audio:
            return 0.0, 0
        rms = _rms16(audio)
        # Замер на старте — хорошая точка для начальной оценки «тишины».
        if rms > 0:
            self._noise = rms
        self._level = self._level_smooth = rms
        return rms, _max16(audio)

    def _update_noise(self, energy: float) -> None:
        """
        Адаптивная оценка шумового пола.

        Тишина — пол подтягивается к ней быстро, речь — почти не двигает
        пол вверх. Так порог остаётся «шумовым», а не «речевым», и начало
        фразы находится корректно в любой громкости.
        """
        if self._noise is None:
            self._noise = energy
        elif energy < self._noise:
            self._noise += (energy - self._noise) * 0.5
        else:
            # Очень медленный рост: пока сэр говорит, пол почти стоит на
            # месте, иначе долгий разговор поднял бы порог выше самой речи
            # и следующая фраза не была бы услышана.
            self._noise += (energy - self._noise) * 0.002

    def _voice_threshold(self) -> float:
        """Порог «идёт речь»: от шумового пола, с разумным потолком."""
        floor = self._noise if self._noise is not None else 0.0
        return max(_MIN_VOICE, min(floor * 3.0, _NOISE_CEILING))

    def capture(
        self,
        wait_timeout: float,
        max_duration: float,
        phrase_timeout: float,
        voice_threshold: float = 0.0,
    ) -> bytes:
        """
        Дождаться начала речи и записать фразу целиком.

        Возвращает PCM16 моно (self.rate) или b"", если за wait_timeout
        никто не заговорил.
        """
        self._check_error()
        self.flush()

        chunks = []
        speech_started = None
        last_voice = None
        deadline = time.monotonic() + max(0.0, wait_timeout)

        while True:
            self._check_error()
            frame = self._next_frame(timeout=0.15)
            now = time.monotonic()

            if not frame:
                if speech_started is None:
                    if now >= deadline:
                        return b""
                    continue
                if now - last_voice >= phrase_timeout:
                    break
                if now - speech_started >= max_duration:
                    break
                continue

            energy = _rms16(frame)
            self._update_level(energy)
            # Порог считаем ДО подтягивания шумового пола текущим кадром.
            threshold = voice_threshold or self._voice_threshold()
            self._update_noise(energy)
            if speech_started is None:
                # Во время озвучки порог ниже: «стоп» слышно сквозь голос NOVA AI.
                if energy >= threshold:
                    speech_started = now
                    last_voice = now
                    chunks.append(frame)
                elif now >= deadline:
                    return b""
                continue

            chunks.append(frame)
            if energy >= (voice_threshold or self._voice_threshold()):
                last_voice = now
            if now - speech_started >= max_duration:
                break
            if now - last_voice >= phrase_timeout:
                break

        return b"".join(chunks)

    def _release_stream(self) -> None:
        """Закрывает pyaudio-поток. Вызывается ТОЛЬКО из потока чтения.

        Повторный вызов безопасен: закрытие идёт один раз.
        """
        if self._closed:
            return
        self._closed = True
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        pa, self._pa = self._pa, None
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                pass

    def close(self, timeout: float = 2.0) -> None:
        """Останавливает фоновое чтение и освобождает микрофон.

        Здесь мы НЕ трогаем pyaudio напрямую: если закрыть поток, пока
        фоновый поток внутри read(), PortAudio читает освобождённую память
        и процесс падает с SIGSEGV. Вместо этого сигналим остановку и ждём
        завершения потока — закрывает он сам (в своём finally).
        """
        with _ACTIVE_LOCK:
            if _ACTIVE_READERS.get(self.mic_index) is self:
                _ACTIVE_READERS.pop(self.mic_index, None)

        self._stop.set()
        with self._cond:
            self._cond.notify_all()

        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.1, timeout))
            if thread.is_alive():
                # Устройство не отдаёт кадры: ждать дальше бессмысленно.
                # Освобождать CoreAudio из чужого потока нельзя — это ровно
                # тот краш, ради которого написан этот метод. Поток-демон
                # завершится вместе с процессом.
                logging.warning(
                    "Микрофон: поток чтения не завершился за %.1fс — "
                    "закрытие отложено (лучше утечка, чем падение)",
                    timeout,
                )
                return
        # Потока уже нет (или он завершился и закрыл поток сам) — на всякий
        # случай закрываем идемпотентным методом.
        self._release_stream()


# ================================================================
# Выбор микрофона и служебные замеры
# ================================================================
def active_mic_level() -> tuple:
    """Уровень (rms, mic_index) с уже открытого потока или (None, None).

    Нужен диагностике, чтобы не открывать второй поток на том же
    устройстве, пока NOVA AI слушает (macOS такие дубли не любит).
    """
    with _ACTIVE_LOCK:
        readers = list(_ACTIVE_READERS.values())
    for reader in readers:
        try:
            return reader.level(), reader.mic_index
        except MicUnavailable:
            continue
    return None, None


def probe_mic_level(mic_index=None, duration: float = 1.0) -> tuple:
    """Уровень звука (RMS, max) с микрофона. Возвращает (0.0, 0) при неудаче.

    Если поток этого микрофона уже открыт (NOVA AI слушает) — замер берётся
    из него, без открытия второго потока на том же устройстве.
    """
    with _ACTIVE_LOCK:
        reader = _ACTIVE_READERS.get(mic_index)
    if reader is not None:
        try:
            return reader.measure(duration)
        except MicUnavailable:
            logging.debug("probe_mic_level: активный поток упал (%s)", mic_index)

    last_error = None
    for _ in range(2):
        pa = None
        stream = None
        try:
            pa = _pyaudio.PyAudio()
            if mic_index is None:
                mic_index = pa.get_default_input_device_info()["index"]
            stream = pa.open(
                format=_pyaudio.paInt16,
                channels=1,
                rate=_SAMPLE_RATE,
                input=True,
                input_device_index=mic_index,
                frames_per_buffer=_CHUNK,
            )
            chunks = []
            n_frames = max(1, int(duration * _SAMPLE_RATE / _CHUNK))
            for _ in range(n_frames):
                chunks.append(stream.read(_CHUNK, exception_on_overflow=False))
            audio = b"".join(chunks)
            return _rms16(audio), _max16(audio)
        except Exception as exc:
            last_error = exc
            time.sleep(0.2)
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            if pa is not None:
                try:
                    pa.terminate()
                except Exception:
                    pass
    logging.debug("probe_mic_level(%s): %s", mic_index, last_error)
    return 0.0, 0


def mic_stream_status(mic_index=None, duration: float = 1.0) -> dict:
    """
    Диагностика потока микрофона без ложных выводов.

    Возвращает {"ok", "rms", "index", "error"}:
    - ok=True  — поток открылся: устройство живое, доступ разрешён
                 (уровень при этом может быть нулевым — это «mute», а не поломка);
    - ok=False — открыть не удалось, в error — реальная причина.
    """
    rms, index = active_mic_level()
    if rms is not None:
        return {"ok": True, "rms": rms, "index": index, "error": ""}

    pa = None
    stream = None
    try:
        pa = _pyaudio.PyAudio()
        if mic_index is None:
            mic_index = pa.get_default_input_device_info()["index"]
        stream = pa.open(
            format=_pyaudio.paInt16,
            channels=1,
            rate=_SAMPLE_RATE,
            input=True,
            input_device_index=mic_index,
            frames_per_buffer=_CHUNK,
        )
        chunks = []
        n_frames = max(1, int(duration * _SAMPLE_RATE / _CHUNK))
        for _ in range(n_frames):
            chunks.append(stream.read(_CHUNK, exception_on_overflow=False))
        audio = b"".join(chunks)
        return {
            "ok": True,
            "rms": _rms16(audio),
            "index": mic_index,
            "error": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "rms": 0.0,
            "index": mic_index,
            "error": str(exc),
        }
    finally:
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                pass


def list_input_devices() -> list:
    """Список (index, name) всех входных аудио-устройств."""
    try:
        pa = _pyaudio.PyAudio()
        try:
            devices = []
            for i in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(i)
                if info.get("maxInputChannels", 0) > 0:
                    devices.append((i, info["name"]))
            return devices
        finally:
            pa.terminate()
    except Exception:
        return []


def pick_best_mic_index() -> int:
    """
    Автовыбор микрофона с реально идущим звуком.

    1. Пробуем системный дефолт — если он «живой», берём его.
    2. Иначе перебираем остальные входы (iPhone-микрофон может молчать).
    3. Ничего живого нет — возвращаем дефолт и предупреждаем (скорее всего
       нет доступа к микрофону в Системных настройках).
    """
    pa = None
    try:
        pa = _pyaudio.PyAudio()
        default_index = pa.get_default_input_device_info()["index"]
    except Exception:
        return None
    finally:
        if pa is not None:
            pa.terminate()

    candidates = [(default_index, "default (системный)")]
    candidates += [(i, name) for i, name in list_input_devices() if i != default_index]

    for index, label in candidates:
        rms, _ = probe_mic_level(index, duration=0.6)
        if rms >= _SOUND_THRESHOLD:
            logging.info(
                "Микрофон: выбран вход [%s] %s (RMS=%.0f)", index, label, rms,
            )
            return index

    # Ни одно устройство сейчас не звучит. Это НЕ поломка: скорее всего тихо
    # в комнате или микрофон отключён. Берём системный вход по умолчанию —
    # он в любом случае пригодится, когда звук появится.
    logging.info(
        "Микрофон: сейчас тихо на всех входах (беру системный [%s]). Если "
        "NOVA AI не слышит — проверьте, не отключён ли микрофон: клавиша mute "
        "(F8) или Control Center → «Микрофон», либо Системные настройки → "
        "Конфиденциальность и безопасность → Микрофон.",
        default_index,
    )
    return default_index


def find_vosk_model() -> str:
    """Ищет папку модели Vosk: явный STT_VOSK_MODEL → models/vosk*.

    Возвращает путь к папке модели или "" (модель не найдена).
    """
    candidates = []
    if STT_VOSK_MODEL.strip():
        candidates.append(STT_VOSK_MODEL.strip())
    try:
        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        models_dir = os.path.join(project_dir, "models")
        if os.path.isdir(models_dir):
            for entry in sorted(os.listdir(models_dir)):
                if entry.lower().startswith("vosk"):
                    candidates.append(os.path.join(models_dir, entry))
    except OSError:
        pass
    for path in candidates:
        if os.path.isdir(path):
            for marker in ("am", "conf", "graph"):
                if os.path.exists(os.path.join(path, marker)) or os.path.exists(
                    os.path.join(path, marker + ".vb")
                ):
                    return path
    logging.info(
        "Vosk: локальная модель не найдена (%s) — STT через Google",
        candidates or "нет кандидатов",
    )
    return ""


# ================================================================
# Основной класс распознавания
# ================================================================
def _load_vosk_model(path: str):
    """Загружает модель Vosk с кэшем: пересоздание микрофона не должно
    каждый раз заново читать 50 МБ модели с диска."""
    cached = _VOSK_MODELS.get(path)
    if cached is not None:
        return cached
    from vosk import Model as VoskModel
    from vosk import SetLogLevel as vosk_set_log_level

    vosk_set_log_level(-1)
    model = VoskModel(path)
    _VOSK_MODELS[path] = model
    return model


class SpeechRecognitionSTT:
    """
    Распознавание речи с постоянным потоком микрофона.

    - Локальный Vosk (модель models/vosk*) — основной движок, без сети.
    - Google STT — запасной вариант, если модели нет или она молчит.
    - Слово-будильник («Джарвис», «NOVA AI», ...).
    - Перебивание голосом (barge-in), пока NOVA AI говорит.
    - Никаких крашей на CoreAudio: ошибки потока поднимают MicUnavailable,
      вызывающий код пересоздаёт распознаватель.
    """

    WAKE_WORDS = {
        "джарвис", "nova_ai", "жарвис", "джервис", "чарвис",
        "эй джарвис", "hey nova_ai", "окей джарвис", "ok nova_ai",
        "привет джарвис", "hello nova_ai", "даровас",
    }

    def __init__(
        self,
        language: str = None,
        use_wake_word: bool = True,
        wake_word_timeout: float = 8.0,
        phrase_timeout: float = 0.8,
        max_phrase_duration: float = 10.0,
        mic_index: int = None,
    ) -> None:
        self.language = (language or STT_LANGUAGE or "ru-RU")
        self.use_wake_word = use_wake_word
        self.wake_word_timeout = wake_word_timeout
        self.phrase_timeout = phrase_timeout
        self.max_phrase_duration = max_phrase_duration
        self.recognizer = _sr.Recognizer()
        # Не даём STT-запросу висеть вечно: если канал до Google «мёртвый»
        # (сеть/гео), без лимита NOVA AI «глохнет» навсегда.
        self.recognizer.operation_timeout = 12.0

        # ---- локальный Vosk ----
        self.vosk_model_path = ""
        self._vosk = None
        if STT_ENGINE in ("auto", "vosk"):
            self.vosk_model_path = find_vosk_model()
            if self.vosk_model_path:
                try:
                    self._vosk = _load_vosk_model(self.vosk_model_path)
                    logging.info(
                        "Vosk: локальное распознавание готово (%s)",
                        os.path.basename(self.vosk_model_path),
                    )
                except Exception as exc:
                    logging.warning("Vosk: не удалось загрузить модель: %s", exc)
                    self._vosk = None
            elif STT_ENGINE == "vosk":
                logging.warning(
                    "STT_ENGINE=vosk, но модели нет — использую Google STT"
                )

        # ---- выбор микрофона ----
        self.mic_index = mic_index
        if self.mic_index is None and STT_MIC_INDEX:
            try:
                self.mic_index = int(STT_MIC_INDEX)
            except ValueError:
                self.mic_index = None
        if self.mic_index is None:
            self.mic_index = pick_best_mic_index()

        # Один постоянный поток: дальше он живёт до close()/пересоздания.
        self.reader = _MicReader(self.mic_index)

        # Диагностика: действительно ли звук идёт (права macOS на микрофон)
        self.mic_rms = 0.0
        try:
            self.mic_rms, mic_max = self.reader.measure(duration=1.0)
        except MicUnavailable:
            self.reader.close()
            raise
        if self.mic_rms >= _SOUND_THRESHOLD:
            logging.info(
                "Микрофон OK: вход [%s], %d Гц, уровень RMS=%.0f (max=%d)",
                self.mic_index, self.reader.rate, self.mic_rms, mic_max,
            )
        elif self.mic_rms >= MIC_DEAD_LEVEL:
            # Доступ есть, просто сейчас тихо — это нормально, ругаться не на что.
            logging.info(
                "Микрофон доступен, в комнате тихо: вход [%s], %d Гц, RMS=%.0f",
                self.mic_index, self.reader.rate, self.mic_rms,
            )
        else:
            # Поток открылся — значит доступ к микрофону есть и устройство
            # живо. Ровно нули в комнате — обычное дело, когда никто не
            # говорит, поэтому тревожим по-тихому, а не голосим в Terminal.
            logging.info(
                "Микрофон открыт, в комнате пока тихо (вход [%s], RMS=%.0f). "
                "Если сэр говорит, а я молчу — проверьте, не отключён ли "
                "микрофон: клавиша mute (F8) или Control Center → «Микрофон».",
                self.mic_index, self.mic_rms,
            )

        self._wake_word_active = False
        self._last_wake_time = 0.0
        logging.info("SpeechRecognitionSTT готов (Vosk=%s, Google=запасной).",
                     "да" if self._vosk else "нет")

    # ---------- состояние ----------
    def mic_level(self) -> float:
        """Текущий уровень микрофона (RMS) — для самодиагностики."""
        try:
            return self.reader.level()
        except MicUnavailable:
            return -1.0

    def arm_without_wake(self) -> None:
        """Следующая фраза принимается без слова-будильника (~12 секунд)."""
        self._wake_word_active = True
        self._last_wake_time = time.time()
        logging.info("Wake-word временно отключён (окно %dс)", int(_WAKE_WINDOW))

    # ---------- распознавание ----------
    def _recognize_vosk(self, data: bytes, rate: int) -> str:
        """Локальное распознавание фрагмента (без сети)."""
        if self._vosk is None:
            return ""
        from vosk import KaldiRecognizer

        rec = KaldiRecognizer(self._vosk, _SAMPLE_RATE)
        rec.SetWords(False)
        payload = _resample16(data, rate)
        if payload:
            rec.AcceptWaveform(payload)
        try:
            text = (json.loads(rec.FinalResult()).get("text") or "").strip()
        except (ValueError, AttributeError):
            return ""
        return text

    def _recognize_google(self, data: bytes, rate: int) -> str:
        """Запасной путь: Google STT (нужен интернет вне гео-блока)."""
        audio = _sr.AudioData(data, rate, 2)
        try:
            return (self.recognizer.recognize_google(
                audio, language=self.language
            ) or "").strip()
        except (_sr.WaitTimeoutError, _sr.UnknownValueError):
            return ""
        except Exception as exc:
            logging.warning("Google STT не ответил (%s) — использую локальный Vosk", exc)
            return ""

    def _recognize(self, data: bytes, rate: int) -> str:
        """Основной путь: сначала локальный Vosk, при отсутствии — Google."""
        if self._vosk is not None:
            text = self._recognize_vosk(data, rate)
            if text:
                return text
            # пусто бывает и на шуме/тишине — второй раз Google звать не надо
            return ""
        return self._recognize_google(data, rate)

    def _recognize_interrupt(self, data: bytes, rate: int) -> str:
        """Короткое распознавание для перебивания (сначала Vosk)."""
        if self._vosk is not None:
            return self._recognize_vosk(data, rate)
        return self._recognize_google(data, rate)

    def _is_wake_word(self, text: str) -> bool:
        """Есть ли в фразе слово-будильник."""
        lowered = text.lower()
        compact = "".join(ch for ch in lowered if ch.isalnum() or ch.isspace())
        for wake in self.WAKE_WORDS:
            if wake in lowered or wake in compact:
                return True
        return False

    def _strip_wake_word(self, text: str) -> str:
        """Убирает слово-будильник из команды."""
        result = text.strip()
        for wake in sorted(self.WAKE_WORDS, key=len, reverse=True):
            result = result.lower().replace(wake, "")
        return result.strip(" ,.!?-:;")

    # ---------- основные циклы ----------
    def listen_for_interrupt(self, timeout: float = 2.0) -> str:
        """
        Короткое прослушивание, пока NOVA AI говорит (barge-in).

        Порог речи понижен, чтобы «стоп» был слышен сквозь голос NOVA AI.
        Возвращает распознанный текст или "" (тишина/ошибка).
        """
        try:
            data = self.reader.capture(
                wait_timeout=timeout,
                max_duration=2.5,
                phrase_timeout=0.6,
                voice_threshold=_MIN_VOICE * 0.6,
            )
        except MicUnavailable:
            raise
        except Exception as exc:
            logging.debug("Interrupt listen error: %s", exc)
            return ""
        if not data:
            return ""
        try:
            return (self._recognize_interrupt(data, self.reader.rate) or "").strip()
        except Exception as exc:
            logging.debug("Interrupt recognize error: %s", exc)
            return ""

    def listen(self) -> str:
        """
        Основной цикл: дождаться речи, распознать фразу, учесть wake word.

        Возвращает команду (без слова-будильника) или "" — если фразы не было.
        """
        # Пока NOVA AI говорит — не слушаем (перебивание обрабатывает вызывающий).
        waited = 0.0
        while state.is_speaking and waited < 30.0:
            time.sleep(0.2)
            waited += 0.2

        try:
            data = self.reader.capture(
                wait_timeout=self.wake_word_timeout,
                max_duration=self.max_phrase_duration,
                phrase_timeout=self.phrase_timeout,
            )
        except MicUnavailable:
            raise
        except Exception as exc:
            logging.exception("STT capture error: %s", exc)
            return ""

        if not data:
            return ""

        text = self._recognize(data, self.reader.rate)
        if not text:
            return ""

        logging.info("USER COMMAND: %s", text)

        if not self.use_wake_word:
            return text

        if self._is_wake_word(text):
            self._wake_word_active = True
            self._last_wake_time = time.time()
            command = self._strip_wake_word(text)
            if command:
                return command
            logging.info("Wake word detected, listening for command...")
            return self.listen()          # ждём саму команду

        # Окно «без будильника» после «Джарвис»/перебивания/«разбудить».
        if self._wake_word_active:
            if (time.time() - self._last_wake_time) < _WAKE_WINDOW:
                return text
            self._wake_word_active = False
        return ""

    def close(self) -> None:
        """Аккуратно закрыть поток микрофона."""
        reader = getattr(self, "reader", None)
        if reader is not None:
            try:
                reader.close()
            except Exception:
                logging.debug("Mic close error", exc_info=True)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


# Быстрая проверка микрофона и распознавания (одна фраза).
def quick_listen(language="ru-RU") -> str:
    stt = SpeechRecognitionSTT(language=language, use_wake_word=False)
    try:
        return stt.listen()
    finally:
        stt.close()
