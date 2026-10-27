"""Ручная сквозная проверка голосового конвейера NOVA AI.

Подаёт в распознаватель заранее записанные фразы (через подменённый источник
кадров вместо живого микрофона) и проверяет: сегментацию по энергии (VAD),
локальное распознавание Vosk, слово-будильник и окно «без будильника».

Использует НАСТОЯЩИЙ код voice.speech_to_text: подменяется только источник
кадров (`_next_frame`), поэтому баги сегментации ловятся по-настоящему.

Запуск:  .venv/bin/python tests/manual_stt_pipeline_check.py
"""
import os
import subprocess
import sys
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice.speech_to_text as stt_mod  # noqa: E402

TMP = "/tmp/jarvis_stt_check"
SAMPLE_RATE = 16000


def synth(name: str, text: str) -> str:
    """Готовит wav 16 кГц моно с русской речью (macOS say)."""
    os.makedirs(TMP, exist_ok=True)
    aiff = f"{TMP}/{name}.aiff"
    wav = f"{TMP}/{name}.wav"
    subprocess.run(["say", "-v", "Milena", "-o", aiff, text], check=True)
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", f"LEI16@{SAMPLE_RATE}", "-c", "1", aiff, wav],
        check=True,
        capture_output=True,
    )
    return wav


def load_wav(path: str) -> bytes:
    with wave.open(path) as handle:
        return handle.readframes(handle.getnframes())


class FileReader(stt_mod._MicReader):
    """Поток микрофона, который отдаёт заранее записанные кадры.

    Наследует настоящую логику capture()/VAD/порогов — подменён только
    источник кадров, поэтому проверяется ровно тот код, что работает в бою.
    """

    def __init__(self, audio: bytes, rate: int = SAMPLE_RATE, mic_index: int = 0):
        self.mic_index = mic_index
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

    def _next_frame(self, timeout: float) -> bytes:
        chunk = stt_mod._CHUNK * 2
        if self._pos >= len(self._audio):
            return b""            # запись закончилась -> «тишина»
        data = self._audio[self._pos:self._pos + chunk]
        self._pos += chunk
        time.sleep(0.064)        # играем аудио в реальном времени
        return data

    def flush(self):
        self._pos = 0

    def close(self, timeout: float = 2.0):
        self._closed = True


class FakeSTT(stt_mod.SpeechRecognitionSTT):
    """Распознаватель без живого микрофона (Vosk — подлинный)."""

    def __init__(self, audio: bytes, **kwargs):
        import speech_recognition as sr
        from vosk import Model

        self.language = kwargs.get("language") or "ru-RU"
        self.use_wake_word = kwargs.get("use_wake_word", True)
        self.wake_word_timeout = kwargs.get("wake_word_timeout", 8.0)
        self.phrase_timeout = 0.8
        self.max_phrase_duration = 10.0
        self.recognizer = sr.Recognizer()
        self.recognizer.operation_timeout = 12.0
        self._vosk = Model(stt_mod.find_vosk_model())
        self.mic_index = 0
        self.reader = FileReader(audio)
        self.mic_rms = 0.0
        self._wake_word_active = False
        self._last_wake_time = 0.0


def main() -> int:
    phrase_wake = load_wav(synth("wake", "Джарвис, какой сегодня день"))
    phrase_plain = load_wav(synth("plain", "открой почту и напомни про встречу"))
    failures = []

    # 1. Слово-будильник + команда
    stt = FakeSTT(phrase_wake)
    got = stt.listen()
    print(f"1) «Джарвис, какой сегодня день» -> «{got}»")
    if got != "какой сегодня день":
        failures.append(f"ожидалось «какой сегодня день», получено «{got}»")

    # 2. Фраза без слова-будильника — игнорируется
    stt = FakeSTT(phrase_plain)
    got = stt.listen()
    print(f"2) фраза без будильника -> «{got}» (ожидаем пусто)")
    if got:
        failures.append(f"фраза без будильника не отфильтрована: «{got}»")

    # 3. После будильника окно 12 секунд — фраза без будильника принимается
    stt = FakeSTT(phrase_plain)
    stt.arm_without_wake()
    got = stt.listen()
    print(f"3) фраза без будильника в окне -> «{got}»")
    if not got:
        failures.append("окно «без будильника» не работает")

    # 4. Режим без будильника: любая фраза принимается
    stt = FakeSTT(phrase_plain, use_wake_word=False)
    got = stt.listen()
    print(f"4) use_wake_word=False -> «{got}»")
    if not got:
        failures.append("use_wake_word=False не принимает фразу")

    # 5. Перебивание: короткое окно, пониженный порог
    stt = FakeSTT(load_wav(synth("stop", "стоп")), use_wake_word=False)
    got = stt.listen_for_interrupt(timeout=3.0)
    print(f"5) перебивание «стоп» -> «{got}»")
    if "стоп" not in got.lower():
        failures.append(f"перебивание не распознано: «{got}»")

    # 6. Только тишина — команда не появляется (микрофон не «выдумывает» речь)
    silence = b"\x00\x00" * stt_mod._CHUNK * 200
    stt = FakeSTT(silence)
    started = time.monotonic()
    got = stt.listen()
    waited = time.monotonic() - started
    print(f"6) тишина 3с -> «{got}» за {waited:.1f}s (ожидаем пусто)")
    if got:
        failures.append(f"на тишине распознано «{got}»")
    if waited > 12:
        failures.append(f"слишком долгое ожидание в тишине: {waited:.1f}s")

    print()
    if failures:
        print("ОШИБКИ:")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("Все проверки голосового конвейера пройдены.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
