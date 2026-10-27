"""Тесты голосового ввода NOVA AI: пороги/VAD, слово-будильник, ресемплинг.

Тесты НЕ трогают микрофон и сеть: поток подменяется источником кадров,
распознавание — локальным Vosk (если модель установлена).
"""
import collections
import os
import struct
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice.speech_to_text as stt_mod  # noqa: E402


def tone(rms_value: float, frames: int) -> bytes:
    """PCM16 моно постоянного уровня (значения — шкала 16-битного сигнала)."""
    sample = max(-32768, min(32767, int(round(rms_value))))
    return struct.pack("<h", sample) * frames


class _FakeReader(stt_mod._MicReader):
    """Поток микрофона из заранее заданных кадров (без CoreAudio)."""

    def __init__(self, audio: bytes, rate: int = stt_mod._SAMPLE_RATE,
                 realtime: bool = False):
        self.mic_index = 0
        self.rate = rate
        self._pa = None
        self._stream = None
        self._thread = None
        self._stop = None
        self._cond = None
        self._frames = None
        self._level = 0.0
        self._level_smooth = 0.0
        self._error = None
        self._closed = False
        self._noise = None
        self._max_frames = 8
        self._audio = audio
        self._pos = 0
        # realtime=True — отдаём кадры с реальной скоростью (64 мс),
        # иначе проверки по времени бессмысленны.
        self._realtime = realtime

    def _next_frame(self, timeout: float) -> bytes:
        chunk = stt_mod._CHUNK * 2
        if self._pos >= len(self._audio):
            return b""
        data = self._audio[self._pos:self._pos + chunk]
        self._pos += chunk
        if self._realtime:
            time.sleep(chunk / 2 / self.rate)
        return data

    def flush(self):
        self._pos = 0


def _reader() -> "_FakeReader":
    return _FakeReader(b"")


def _stt() -> "stt_mod.SpeechRecognitionSTT":
    """Распознаватель без микрофона и без загрузки модели."""
    stt = stt_mod.SpeechRecognitionSTT.__new__(stt_mod.SpeechRecognitionSTT)
    stt._vosk = None
    stt._wake_word_active = False
    stt._last_wake_time = 0.0
    return stt


class AudioHelpersTest(unittest.TestCase):
    """Уровни, максимумы и пересчёт частоты."""

    def test_rms_of_silence_is_zero(self):
        self.assertEqual(stt_mod._rms16(b"\x00\x00" * 100), 0.0)
        self.assertEqual(stt_mod._max16(b"\x00\x00" * 100), 0)

    def test_rms_of_constant_tone(self):
        data = tone(1000, 100)
        self.assertAlmostEqual(stt_mod._rms16(data), 1000, delta=5)
        self.assertEqual(stt_mod._max16(data), 1000)

    def test_resample_changes_length(self):
        data = tone(1000, 4410)          # 0.1 с при 44.1 кГц
        converted = stt_mod._resample16(data, 44100)
        self.assertLess(len(converted), len(data))
        self.assertGreater(len(converted), 0)

    def test_resample_skips_same_rate(self):
        data = tone(1000, 100)
        self.assertIs(stt_mod._resample16(data, stt_mod._SAMPLE_RATE), data)


class NoiseFloorTest(unittest.TestCase):
    """Адаптивный порог речи: главная причина «NOVA AI не слышит»."""

    def test_threshold_starts_at_minimum(self):
        reader = _reader()
        self.assertEqual(reader._voice_threshold(), stt_mod._MIN_VOICE)

    def test_silence_lowers_floor_fast(self):
        reader = _reader()
        reader._noise = 5000.0
        for _ in range(5):
            reader._update_noise(200.0)
        self.assertLess(reader._noise, 400.0)

    def test_speech_raises_floor_slowly(self):
        reader = _reader()
        reader._noise = 200.0
        for _ in range(60):             # ~4 с непрерывной громкой речи
            reader._update_noise(4000.0)
        # Главное свойство: порог не поднялся выше уровня самой речи,
        # иначе следующая фраза после долгого разговора не была бы услышана.
        self.assertLess(reader._voice_threshold(), 4000.0)
        # шумовой пол остался вчетверо тише самой речи
        self.assertLess(reader._noise, 1000.0)

    def test_threshold_scales_with_noise(self):
        reader = _reader()
        reader._noise = 300.0
        self.assertAlmostEqual(reader._voice_threshold(), 900.0)
        reader._noise = 3000.0
        self.assertEqual(reader._voice_threshold(), stt_mod._NOISE_CEILING)

    def test_threshold_never_below_minimum(self):
        reader = _reader()
        reader._noise = 0.0
        self.assertEqual(reader._voice_threshold(), stt_mod._MIN_VOICE)

    def test_measured_silence_seeds_floor(self):
        reader = _reader()
        reader._audio = tone(150.0, stt_mod._CHUNK * 30)
        rms, _ = reader.measure(duration=0.1)
        self.assertAlmostEqual(reader._noise, rms, places=3)


class VadCaptureTest(unittest.TestCase):
    """Сегментация фразы: начало по речи, конец по тишине."""

    @staticmethod
    def _audio(silent_frames: int = 20, voice_frames: int = 40,
               tail_silent_frames: int = 20) -> bytes:
        return (
            tone(100.0, stt_mod._CHUNK * silent_frames)
            + tone(3000.0, stt_mod._CHUNK * voice_frames)
            + tone(100.0, stt_mod._CHUNK * tail_silent_frames)
        )

    def test_silence_returns_nothing(self):
        reader = _FakeReader(tone(100.0, stt_mod._CHUNK * 50))
        self.assertEqual(
            reader.capture(wait_timeout=0.2, max_duration=2.0, phrase_timeout=0.2),
            b"",
        )

    def test_phrase_is_captured_from_speech_onset(self):
        reader = _FakeReader(self._audio())
        audio = reader.capture(
            wait_timeout=1.0, max_duration=5.0, phrase_timeout=0.2
        )
        self.assertGreater(len(audio), 0)
        # записано должно быть в основном «говорение», а не тишина
        self.assertGreater(stt_mod._rms16(audio), 1000.0)

    def test_phrase_ends_on_silence(self):
        # 2 кадра тишины + 10 кадров речи (~0.64 с) + 20 кадров тишины (~1.28 с).
        # Фраза обязана оборваться на тишине, а не дослушать запись до конца.
        reader = _FakeReader(
            tone(100.0, stt_mod._CHUNK * 2)
            + tone(3000.0, stt_mod._CHUNK * 10)
            + tone(100.0, stt_mod._CHUNK * 20),
            realtime=True,
        )
        started = time.monotonic()
        audio = reader.capture(
            wait_timeout=1.0, max_duration=5.0, phrase_timeout=0.2
        )
        elapsed = time.monotonic() - started
        self.assertGreater(len(audio), 0)
        # ~0.77 с речи + 0.2 с ожидания тишины, а не все 2.05 с записи
        self.assertLess(elapsed, 1.4)

    def test_phrase_limited_by_max_duration(self):
        reader = _FakeReader(
            tone(3000.0, stt_mod._CHUNK * 60), realtime=True
        )
        started = time.monotonic()
        reader.capture(wait_timeout=0.2, max_duration=0.3, phrase_timeout=5.0)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0)

    def test_lowered_threshold_detects_quiet_speech(self):
        """Во время озвучки порог понижается — «стоп» слышно сквозь голос."""
        quiet_voice = tone(240.0, stt_mod._CHUNK * 40)
        reader = _FakeReader(quiet_voice)
        self.assertEqual(
            reader.capture(wait_timeout=0.3, max_duration=2.0, phrase_timeout=0.2),
            b"",
        )
        reader = _FakeReader(quiet_voice)
        self.assertGreater(
            len(reader.capture(
                wait_timeout=0.5,
                max_duration=2.0,
                phrase_timeout=0.2,
                voice_threshold=stt_mod._MIN_VOICE * 0.6,
            )),
            0,
        )


class WakeWordTest(unittest.TestCase):
    """Слово-будильник и окно «без будильника» после него."""

    def test_detects_variants(self):
        stt = _stt()
        for phrase in (
            "джарвис который час",
            "Джарвис, открой почту",
            "NOVA AI открой почту",
            "привет джарвис",
        ):
            self.assertTrue(stt._is_wake_word(phrase), phrase)

    def test_ignores_commands_without_wake_word(self):
        stt = _stt()
        for phrase in ("открой почту", "который час", "напомни про встречу"):
            self.assertFalse(stt._is_wake_word(phrase), phrase)

    def test_strips_wake_word_from_command(self):
        stt = _stt()
        self.assertEqual(
            stt._strip_wake_word("Джарвис, какой сегодня день"),
            "какой сегодня день",
        )
        self.assertEqual(
            stt._strip_wake_word("джарвис открой почту"), "открой почту"
        )

    def test_strip_keeps_command_without_wake_word(self):
        stt = _stt()
        self.assertEqual(
            stt._strip_wake_word("открой почту"), "открой почту"
        )

    def test_arm_without_wake_opens_window(self):
        import time

        stt = _stt()
        stt.arm_without_wake()
        self.assertTrue(stt._wake_word_active)
        self.assertLess(time.time() - stt._last_wake_time, 1.0)


class MicUnavailableTest(unittest.TestCase):
    """Ошибка потока микрофона поднимается явно, а не падает на None.close()."""

    def test_check_error_raises_clear_exception(self):
        reader = _reader()
        reader._error = RuntimeError("Audio Unit: cannot do in current context")
        with self.assertRaises(stt_mod.MicUnavailable) as ctx:
            reader._check_error()
        self.assertIn("cannot do in current context", str(ctx.exception))

    def test_error_is_consumed_once(self):
        reader = _reader()
        reader._error = RuntimeError("device lost")
        with self.assertRaises(stt_mod.MicUnavailable):
            reader._check_error()
        reader._check_error()      # повторно не падает — цикл сможет пересоздать поток

    def test_capture_propagates_mic_error(self):
        reader = _FakeReader(b"")
        reader._error = RuntimeError("устройство отвалилось")
        with self.assertRaises(stt_mod.MicUnavailable):
            reader.capture(wait_timeout=0.2, max_duration=1.0, phrase_timeout=0.2)

    def test_capture_survives_reader_without_smooth_level(self):
        """Подкласс, задающий __init__ сам, не должен ронять capture().

        Реальный случай: файловый/тестовый поток кадров не создаёт
        _level_smooth, и раньше capture() падал с AttributeError, молча
        возвращая "" — фраза «терялась» без всякой ошибки в логе.
        """
        reader = _FakeReader(tone(1500.0, stt_mod._CHUNK * 30), realtime=True)
        del reader._level_smooth          # как будто забыли про сглаживание
        audio = reader.capture(
            wait_timeout=1.0, max_duration=2.0, phrase_timeout=0.3
        )
        self.assertGreater(len(audio), 0, "фраза потеряна")
        self.assertGreaterEqual(reader.level(), stt_mod.MIC_DEAD_LEVEL)


class ActiveReaderRegistryTest(unittest.TestCase):
    """Замер уровня не открывает второй поток на том же устройстве."""

    def test_probe_uses_active_reader(self):
        reader = _FakeReader(tone(120.0, stt_mod._CHUNK * 30))
        with stt_mod._ACTIVE_LOCK:
            stt_mod._ACTIVE_READERS[reader.mic_index] = reader
        try:
            rms, mx = stt_mod.probe_mic_level(reader.mic_index, duration=0.1)
            self.assertAlmostEqual(rms, 120.0, delta=5)
            self.assertGreater(mx, 0)
        finally:
            with stt_mod._ACTIVE_LOCK:
                stt_mod._ACTIVE_READERS.pop(reader.mic_index, None)


@unittest.skipUnless(stt_mod.find_vosk_model(), "модель Vosk не установлена")
class VoskOfflineTest(unittest.TestCase):
    """Локальное распознавание не требует сети."""

    def test_silence_recognizes_to_empty_text(self):
        from vosk import Model

        stt = _stt()
        stt._vosk = Model(stt_mod.find_vosk_model())
        self.assertEqual(stt._recognize_vosk(b"\x00\x00" * 16000, 16000), "")

    def test_google_path_used_when_no_local_model(self):
        stt = _stt()
        self.assertIsNone(stt._vosk)
        # с выключенным Vosk путь идёт в Google; без сети он обязан
        # вернуть пустую строку, а не уронить голосовой цикл
        self.assertEqual(
            stt._recognize_google(b"\x00\x00" * 16000, 16000), ""
        )


class LevelSmoothingTest(unittest.TestCase):
    """Решение «микрофон жив?» не должно зависеть от одного кадра."""

    def test_sound_raises_level_immediately(self):
        reader = _reader()
        for _ in range(5):
            reader._update_level(400.0)
        self.assertEqual(reader.level(), 400.0)

    def test_single_silent_frame_keeps_level(self):
        """Пауза между словами не должна выглядеть как «микрофон мёртв»."""
        reader = _reader()
        for _ in range(10):
            reader._update_level(400.0)
        reader._update_level(0.0)
        self.assertGreaterEqual(reader.level(), stt_mod.MIC_DEAD_LEVEL)
        self.assertEqual(reader.instant_level(), 0.0)

    def test_level_falls_after_long_silence(self):
        reader = _reader()
        for _ in range(10):
            reader._update_level(400.0)
        for _ in range(200):          # ~13 с тишины
            reader._update_level(0.0)
        self.assertLess(reader.level(), stt_mod.MIC_DEAD_LEVEL)


class StreamOwnershipTest(unittest.TestCase):
    """Главная причина падений (SIGSEGV в PaUtil_ReadRingBuffer): закрывать
    поток микрофона должен только тот поток, который его читает.

    Здесь работают настоящие _pump(), _release_stream() и close(); вместо
    pyaudio — заглушки, которые запоминают, из какого потока их трогали.
    """

    class _StubStream:
        def __init__(self):
            self.read_by = []
            self.stopped_by = []
            self.closed_by = []

        def read(self, frames, exception_on_overflow=False):
            self.read_by.append(threading.current_thread().name)
            time.sleep(0.01)          # ~как чтение кадра
            return b"\x00\x00" * frames

        def stop_stream(self):
            self.stopped_by.append(threading.current_thread().name)

        def close(self):
            self.closed_by.append(threading.current_thread().name)

    class _StubPa:
        def __init__(self):
            self.terminated_by = []

        def terminate(self):
            self.terminated_by.append(threading.current_thread().name)

    def _live_reader(self):
        reader = stt_mod._MicReader.__new__(stt_mod._MicReader)
        reader.mic_index = 99
        reader.rate = stt_mod._SAMPLE_RATE
        reader._pa = self._StubPa()
        reader._stream = self._StubStream()
        reader._thread = None
        reader._stop = threading.Event()
        reader._cond = threading.Condition()
        reader._frames = collections.deque()
        reader._max_frames = 8
        reader._level = 0.0
        reader._level_smooth = 0.0
        reader._noise = None
        reader._error = None
        reader._closed = False
        return reader

    def test_stream_closed_by_reader_thread_only(self):
        reader = self._live_reader()
        stream, pa = reader._stream, reader._pa
        reader._thread = threading.Thread(
            target=reader._pump, name="nova_ai-mic", daemon=True
        )
        reader._thread.start()
        time.sleep(0.05)
        self.assertTrue(stream.read_by)
        # Пока поток жив, чужих обращений к pyaudio быть не должно.
        self.assertEqual(set(stream.read_by), {"nova_ai-mic"})

        reader.close(timeout=2.0)

        self.assertFalse(reader._thread.is_alive(), "поток чтения не завершился")
        # Критично: закрывает поток чтения, а не вызывающий поток —
        # иначе PortAudio читает освобождённую память (SIGSEGV).
        self.assertEqual(stream.closed_by, ["nova_ai-mic"])
        self.assertEqual(stream.stopped_by, ["nova_ai-mic"])
        self.assertEqual(pa.terminated_by, ["nova_ai-mic"])

    def test_close_releases_even_without_reader_thread(self):
        """Поток не успел стартовать — освобождаем сразу, но один раз."""
        reader = self._live_reader()
        stream = reader._stream
        reader.close(timeout=1.0)
        self.assertEqual(stream.closed_by, ["MainThread"])
        reader.close(timeout=1.0)
        reader.close(timeout=1.0)
        self.assertEqual(stream.closed_by, ["MainThread"])

    def test_close_does_not_touch_stream_while_reader_is_stuck(self):
        """Поток завис в read(): ждём, но НЕ освобождаем CoreAudio извне."""
        reader = self._live_reader()
        stream = reader._stream
        release = threading.Event()
        original = reader._pump

        def stuck_pump():
            release.wait(5.0)
            original()

        reader._thread = threading.Thread(
            target=stuck_pump, name="nova_ai-mic", daemon=True
        )
        reader._thread.start()
        try:
            reader.close(timeout=0.2)
            # Пока фоновый поток жив, поток микрофона не трогаем.
            self.assertEqual(stream.closed_by, [])
        finally:
            release.set()
            reader._thread.join(timeout=3.0)
        self.assertEqual(stream.closed_by, ["nova_ai-mic"])


if __name__ == "__main__":
    unittest.main()
