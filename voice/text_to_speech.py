"""
ElevenLabs TTS — High-quality voice synthesis via ElevenLabs API.
Fallback to macOS 'say' if API unavailable.
"""
import logging
import threading
import subprocess
import time
import requests
import io
import os
from pathlib import Path

import voice.state as state

from config import (
    ELEVENLABS_API_KEY,
    ELEVENLABS_VOICE_ID,
    ELEVENLABS_MODEL_ID,
    ELEVENLABS_STABILITY,
    ELEVENLABS_SIMILARITY,
    ELEVENLABS_STYLE,
    ELEVENLABS_SPEAKER_BOOST,
    TTS_AUDIO_PATH,
    TTS_PROVIDER,
    FISH_API_KEY,
    FISH_VOICE_ID,
    FISH_MODEL,
)


# ================================================================
# Предохранитель озвучки
# ================================================================
# Облачные TTS могут быть недоступны долго: кончился кредит (Fish 402),
# ключ отозван (ElevenLabs 403), сеть не тянет. Без «предохранителя» каждая
# фраза сначала ждала по 2-7 секунд об отказе, и только потом звучала —
# ассистент казался задумчивым на ровном месте. Провайдер, который только что
# отказал, пропускаем на 15 минут: время ответа возвращается к норме, а через
# 15 минут проверка всё же случится (пополнится кредит или вернётся сеть).
TTS_COOLDOWN_SEC = 15 * 60.0

_tts_down_until = {}
_tts_down_reason = {}
_tts_lock = threading.Lock()


def tts_provider_is_down(name: str) -> bool:
    """True, если провайдер только что отказал и его пока не трогаем."""
    with _tts_lock:
        until = _tts_down_until.get(name, 0.0)
        if until <= time.monotonic():
            _tts_down_until.pop(name, None)
            return False
        return True


def tts_provider_down_reason(name: str) -> str:
    with _tts_lock:
        return _tts_down_reason.get(name, "")


def mark_tts_down(name: str, reason: str = "") -> None:
    with _tts_lock:
        _tts_down_until[name] = time.monotonic() + TTS_COOLDOWN_SEC
        _tts_down_reason[name] = reason
    logging.warning(
        "Озвучка %s недоступна (%s) — пропускаю её %d минут, отвечаю запасным "
        "голосом",
        name, reason or "ошибка", int(TTS_COOLDOWN_SEC // 60),
    )


def mark_tts_up(name: str) -> None:
    with _tts_lock:
        if _tts_down_until.pop(name, None) is not None:
            logging.info("Озвучка %s снова отвечает — возвращаю её в работу", name)
        _tts_down_reason.pop(name, None)


def reset_tts_breakers() -> None:
    """Сбросить предохранители (используется в тестах)."""
    with _tts_lock:
        _tts_down_until.clear()
        _tts_down_reason.clear()


class ElevenLabsTTS:
    """
    ElevenLabs TTS with macOS 'say' fallback.
    
    Features:
    - Streaming playback for low latency
    - Interruptible speech
    - Voice settings from config
    - Automatic fallback
    """

    BASE_URL = "https://api.elevenlabs.io/v1"

    def __init__(self):
        self.lock = threading.RLock()
        self.current_process = None
        self.speaking_text = None
        self._api_available = False
        
        # Voice settings from config
        self.voice_id = ELEVENLABS_VOICE_ID or "pNInz6obpgDQGcFmaJgB"
        self.model_id = ELEVENLABS_MODEL_ID or "eleven_multilingual_v2"
        self.stability = float(ELEVENLABS_STABILITY or 0.35)
        self.similarity = float(ELEVENLABS_SIMILARITY or 0.80)
        self.style = float(ELEVENLABS_STYLE or 0.35)
        self.speaker_boost = ELEVENLABS_SPEAKER_BOOST if isinstance(ELEVENLABS_SPEAKER_BOOST, bool) else True
        
        # macOS fallback settings
        self.fallback_voice = "Milena"
        self.fallback_rate = "175"
        
        # Check API availability
        self._check_api()
        
        logging.info(
            "ElevenLabs TTS initialized | api=%s | voice=%s | model=%s | fallback=%s",
            self._api_available, self.voice_id, self.model_id, self.fallback_voice
        )

    def _check_api(self):
        """Verify ElevenLabs API key works"""
        if not ELEVENLABS_API_KEY:
            logging.warning("ElevenLabs API key not set, using macOS fallback")
            self._api_available = False
            return
        
        try:
            response = requests.get(
                f"{self.BASE_URL}/voices",
                headers={"xi-api-key": ELEVENLABS_API_KEY},
                timeout=8
            )
            if response.ok:
                # Доступность определяет HTTP-статус, а не разбор тела.
                # Раньше не-JSON в ответе (заглушка прокси, сжатый ответ)
                # ронял проверку в except, и сервис, который на самом деле
                # отвечает, объявлялся недоступным на 15 минут.
                self._api_available = True
                mark_tts_up("elevenlabs")
                try:
                    voices = response.json().get("voices", [])
                    names = [v.get("name", v.get("voice_id")) for v in voices[:5]]
                    logging.info("ElevenLabs подключён, голоса: %s", names)
                except ValueError:
                    logging.info(
                        "ElevenLabs отвечает (HTTP %s), список голосов не "
                        "разобрался — на синтез это не влияет",
                        response.status_code,
                    )
            else:
                logging.warning("ElevenLabs API error: %s", response.status_code)
                self._api_available = False
                mark_tts_down("elevenlabs", f"HTTP {response.status_code}")
        except Exception as e:
            logging.warning("ElevenLabs API unreachable: %s, using fallback", e)
            self._api_available = False
            mark_tts_down("elevenlabs", type(e).__name__)

    def stop(self):
        """Immediately stop current speech"""
        with self.lock:
            process = self.current_process
            self.current_process = None
            self.speaking_text = None

        if process:
            try:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=1)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

        state.stop_speaking()

    def speak(self, text: str):
        """Speak text using ElevenLabs API or fallback"""
        if not text:
            return

        text = str(text).strip()
        if not text:
            return

        # Тихий режим (пункт 15): только визуальный ответ — на панели,
        # без озвучки. Печатаем, но не играем звук.
        if state.is_silent():
            print(f"[silent] NOVA AI: {text}")
            logging.info("Silent mode: skipped TTS for: %s", text[:200])
            return

        print(f"NOVA AI: {text}")

        # Stop any current speech first
        self.stop()

        # Try ElevenLabs first, fallback to macOS say.
        # Предохранитель: если сервис только что отказал — сразу macOS-голос,
        # без 5-секундного ожидания об отказе перед каждой фразой.
        if self._api_available and not tts_provider_is_down("elevenlabs"):
            self._speak_elevenlabs(text)
        else:
            self._speak_macos(text)

    def _speak_elevenlabs(self, text: str):
        """Синтезирует mp3 из ElevenLabs и играет через afplay <file>."""
        def _synthesize_and_play():
            process = None
            tmp_path = None
            try:
                state.start_speaking(text)
                
                url = f"{self.BASE_URL}/text-to-speech/{self.voice_id}/stream"
                headers = {
                    "xi-api-key": ELEVENLABS_API_KEY,
                    "Content-Type": "application/json",
                    "Accept": "audio/mpeg",
                }
                payload = {
                    "text": text,
                    "model_id": self.model_id,
                    "voice_settings": {
                        "stability": self.stability,
                        "similarity_boost": self.similarity,
                        "style": self.style,
                        "use_speaker_boost": self.speaker_boost,
                    },
                }

                response = requests.post(
                    url, headers=headers, json=payload, stream=True, timeout=15
                )

                if not response.ok:
                    logging.error("ElevenLabs TTS error: %s - %s", response.status_code, response.text[:200])
                    mark_tts_down("elevenlabs", f"HTTP {response.status_code}")
                    self._speak_macos(text)
                    return

                # HTTP 200 НЕ означает «аудио». При региональной блокировке
                # ElevenLabs отвечает 302 → 301 на help-страницу и отдаёт HTML
                # с кодом 200. Проверено 28.09: content-type text/html, тело
                # начинается с '<!DOCTYPE html>'. Раньше такой ответ писался в
                # .mp3 и уходил в afplay, который падал с 'AudioFileOpen failed'
                # — тихо, без следа в логе, и голос не звучал. Тип проверяем
                # ДО записи в файл, иначе резервный macOS say недостижим.
                ctype = (response.headers.get("Content-Type") or "").lower()
                if not ctype.startswith("audio/"):
                    logging.warning(
                        "[TTS] ElevenLabs вернул не аудио (HTTP %s, content-type=%r) "
                        "— вероятна региональная блокировка; переключаюсь на macOS say",
                        response.status_code,
                        ctype or "отсутствует",
                    )
                    mark_tts_down("elevenlabs", f"не аудио: {ctype or 'нет content-type'}")
                    self._speak_macos(text)
                    return

                mark_tts_up("elevenlabs")

                # afplay на macOS 13 не умеет читать mp3 из stdin ("afplay -"),
                # поэтому сохраняем поток во временный файл и проигрываем файл.
                import tempfile
                fd, tmp_path = tempfile.mkstemp(suffix=".mp3", prefix="jarvis_tts_")
                with os.fdopen(fd, "wb") as audio_file:
                    for chunk in response.iter_content(chunk_size=16384):
                        if chunk:
                            audio_file.write(chunk)

                process = subprocess.Popen(
                    ["afplay", tmp_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )

                with self.lock:
                    self.current_process = process
                    self.speaking_text = text

                # Код возврата afplay ОБЯЗАТЕЛЬНО проверяется. Раньше он
                # игнорировался, и отказ воспроизведения был неотличим от
                # успеха: ни строки в логе, ни звука у пользователя.
                # communicate() вместо wait() — чтобы забрать stderr.
                _stdout, err = process.communicate()
                if process.returncode != 0:
                    detail = (err or b"").decode("utf-8", "replace").strip()[:160]
                    logging.warning(
                        "[TTS] afplay не смог проиграть файл (код %s): %s — "
                        "переключаюсь на macOS say",
                        process.returncode,
                        detail or "stderr пуст",
                    )
                    self._speak_macos(text)

            except Exception as e:
                logging.exception("ElevenLabs TTS failed: %s", e)
                mark_tts_down("elevenlabs", type(e).__name__)
                self._speak_macos(text)
            finally:
                if tmp_path is not None:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                if process is not None:
                    if process.poll() is None:
                        try:
                            process.terminate()
                        except Exception:
                            pass
                    with self.lock:
                        # Чистим и останавливаем речь только если это всё ещё
                        # наша озвучка (а не более новая, пришедшая на смену).
                        if self.current_process is process:
                            self.current_process = None
                            self.speaking_text = None
                            state.stop_speaking()

        threading.Thread(target=_synthesize_and_play, daemon=True).start()

    def _speak_macos(self, text: str):
        """Fallback: macOS built-in 'say' command"""
        try:
            state.start_speaking(text)

            process = subprocess.Popen(
                ["say", "-v", self.fallback_voice, "-r", self.fallback_rate, text],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            with self.lock:
                self.current_process = process
                self.speaking_text = text

            def monitor():
                try:
                    process.wait()
                except Exception:
                    pass
                finally:
                    state.stop_speaking()
                    with self.lock:
                        if self.current_process == process:
                            self.current_process = None
                            self.speaking_text = None

            threading.Thread(target=monitor, daemon=True).start()

        except Exception as e:
            logging.exception("macOS Say TTS error: %s", e)
            state.stop_speaking()


class FishTTS:
    """
    Fish.audio TTS — голос NOVA AI из фильма (по умолчанию).
    Цепочка фолбэка: Fish.audio → ElevenLabs → macOS 'say'.
    """

    TTS_URL = "https://api.fish.audio/v1/tts"

    def __init__(self, fallback=None, provider=None):
        self.lock = threading.RLock()
        self.current_process = None
        self.speaking_text = None
        self.fallback = fallback or ElevenLabsTTS()
        self.provider = (provider or TTS_PROVIDER or "auto").strip().lower()
        self.api_key = (FISH_API_KEY or "").strip()
        self.voice_id = (FISH_VOICE_ID or "").strip()
        self.model = (FISH_MODEL or "s2.1-pro-free").strip()
        self._enabled = bool(self.api_key and self.voice_id)
        if self._enabled:
            logging.info(
                "Fish.audio TTS initialized | voice=%s | model=%s | provider=%s",
                self.voice_id, self.model, self.provider,
            )
        elif self.provider == "fish":
            logging.warning("Fish.audio key missing (TTS_PROVIDER=fish) — ElevenLabs/say")
        else:
            logging.warning("Fish.audio key missing — используем ElevenLabs/macOS say")

    def _use_fish(self) -> bool:
        """Выбор источника по TTS_PROVIDER: fish/elevenlabs/auto."""
        if self.provider == "fish":
            return self._enabled
        if self.provider == "elevenlabs":
            return False
        return self._enabled  # auto: fish если настроен, иначе фолбэк

    def stop(self):
        """Останавливает текущую озвучку (свою и фолбэка)."""
        with self.lock:
            process = self.current_process
            self.current_process = None
            self.speaking_text = None
        if process:
            try:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=1)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        state.stop_speaking()
        try:
            self.fallback.stop()
        except Exception:
            pass

    def speak(self, text: str):
        """Говорит текст голосом NOVA AI (Fish) с фолбэком на ElevenLabs/say."""
        if not text:
            return
        text = str(text).strip()
        if not text:
            return

        print(f"NOVA AI: {text}")

        # Остановить текущую речь
        self.stop()

        # Раньше каждая фраза начиналась с ожидания отказа Fish (402/таймаут) —
        # 2-7 секунд тишины перед ответом. Не тратим их, если сервис только что
        # отказал: сразу отдаём текст следующему по цепочке.
        if self._use_fish() and not tts_provider_is_down("fish"):
            self._speak_fish(text)
        else:
            self.fallback.speak(text)

    def _fallback_speak(self, text: str):
        """Останавливает fish-озвучку и отдаёт текст следующему провайдеру."""
        with self.lock:
            proc = self.current_process
            self.current_process = None
            self.speaking_text = None
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass
        state.stop_speaking()
        try:
            self.fallback.speak(text)
        except Exception as e:
            logging.exception("Fish.audio fallback to ElevenLabs failed: %s", e)

    def _speak_fish(self, text: str):
        """Синтезирует mp3 из Fish.audio и играет через afplay <file>."""
        def _synthesize_and_play():
            process = None
            tmp_path = None
            try:
                state.start_speaking(text)

                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "audio/mpeg",
                    "model": self.model,
                }
                payload = {
                    "text": text,
                    "reference_id": self.voice_id,
                    "format": "mp3",
                    "normalize": True,
                    "mp3_bitrate": 128,
                    "sample_rate": 44100,
                }

                response = requests.post(
                    self.TTS_URL, headers=headers, json=payload,
                    stream=True, timeout=30,
                )

                if not response.ok:
                    logging.error(
                        "Fish.audio TTS error: %s - %s",
                        response.status_code, response.text[:200],
                    )
                    # 402/403 — надолго (нет кредита/ключа), 5xx — ненадолго,
                    # но и то и другое не стоит повторять на каждой фразе.
                    mark_tts_down("fish", f"HTTP {response.status_code}")
                    self._fallback_speak(text)
                    return

                mark_tts_up("fish")

                # afplay на macOS 13 не умеет читать mp3 из stdin ("afplay -"),
                # поэтому сохраняем поток во временный файл и проигрываем файл.
                import tempfile
                fd, tmp_path = tempfile.mkstemp(suffix=".mp3", prefix="jarvis_tts_")
                with os.fdopen(fd, "wb") as audio_file:
                    for chunk in response.iter_content(chunk_size=16384):
                        if chunk:
                            audio_file.write(chunk)

                process = subprocess.Popen(
                    ["afplay", tmp_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

                with self.lock:
                    self.current_process = process
                    self.speaking_text = text

                process.wait()

            except Exception as e:
                logging.exception("Fish.audio TTS failed: %s", e)
                mark_tts_down("fish", type(e).__name__)
                self._fallback_speak(text)
            finally:
                if tmp_path is not None:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                if process is not None:
                    if process.poll() is None:
                        try:
                            process.terminate()
                        except Exception:
                            pass
                    with self.lock:
                        if self.current_process is process:
                            self.current_process = None
                            self.speaking_text = None
                            state.stop_speaking()

        threading.Thread(target=_synthesize_and_play, daemon=True).start()


class MacOSTTS:
    """Системный голос macOS `say` — последний рубеж озвучки.

    Зачем он нужен отдельным классом
    --------------------------------
    Fish.audio и ElevenLabs требуют ключ, кредит и сеть. Когда не работает
    ни то, ни другое (кончился кредит, ключ отозван, региональная блокировка),
    NOVA AI всё равно должен говорить: без голоса он выглядит сломанным, хотя
    текстовый режим, панель и навыки работают. `say` есть в любой macOS-системе,
    не требует ни ключа, ни сети, ни Python-пакетов — значит, это единственный
    голос, который не может отказать по внешним причинам.

    Отличия от `_speak_macos` внутри облачных классов
    -------------------------------------------------
    Там `say` — запасной путь *внутри* провайдера, который всё равно перед этим
    тратит сеть. Здесь это самостоятельный провайдер: собственная блокировка,
    собственный `stop()`, собственный учёт процесса. Благодаря этому
    `main.py` может поставить его последним в цепочке и быть уверен, что
    выключение озвучки его тоже дойдёт.

    Особенности, которые здесь исправлены по сравнению с `say` в облачных классах:
      * голос проверяется заранее (`_voice_exists`), чтобы «Доброе утро» не
        ушло в никуда с ошибкой `voice not found`;
      * `stop()` убивает процесс и ДОЖИДАЕТСЯ его — иначе следующая фраза
        стартует поверх ещё играющей, и две фразы звучат одновременно;
      * `say` никогда не блокирует вызывающий поток: проигрывание уходит в
        отдельный поток, как у облачных провайдеров;
      * `speak()` печатает текст даже в тихом режиме — иначе панель теряет
        сообщение целиком.
    """

    #: Голос по умолчанию. Русские голоса в macOS называются по-русски, но
    #: английские идентификаторы тоже принимаются — поэтому проверяем наличие.
    DEFAULT_VOICES = ("Milena", "Yuri", "Pavel", "Alex", "Samantha")
    FALLBACK_RATE = "175"

    def __init__(self, voice: str | None = None, rate: str | None = None):
        self.lock = threading.RLock()
        self.current_process = None
        self.speaking_text = None
        self.voice = voice or self._pick_voice()
        self.rate = rate or self.FALLBACK_RATE

    # ------------------------------------------------------------------
    @staticmethod
    def _voice_exists(name: str) -> bool:
        """Есть ли такой голос в системе (без запуска самого `say`)."""
        try:
            probe = subprocess.run(
                ["say", "-v", "?"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if probe.returncode != 0:
            return False
        listed = probe.stdout.decode("utf-8", "replace")
        # У macOS нет опции «сказать всё одним словом» — сверяем по границам
        # строк, иначе голос "Alex" совпал бы с "Alexa".
        voices = {line.split()[0] for line in listed.splitlines() if line.split()}
        return name in voices

    def _pick_voice(self) -> str:
        """Первый доступный голос из списка предпочтений."""
        for candidate in self.DEFAULT_VOICES:
            if self._voice_exists(candidate):
                return candidate
        # Ничего не нашли — не падаем: пустой голос = системный по умолчанию,
        # `say` сам выберет то, что есть.
        logging.warning(
            "Системный голос не найден ни в одном из %s — отдаю выбору macOS",
            ", ".join(self.DEFAULT_VOICES),
        )
        return ""

    # ------------------------------------------------------------------
    def stop(self) -> None:
        """Немедленно замолчать.

        Обязательно дожидаемся процесса: `say` умеет говорить дальше сам по
        себе, если ему не дать умереть, и следующая фраза наложилась бы на эту.
        """
        with self.lock:
            process = self.current_process
            self.current_process = None
            self.speaking_text = None

        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        try:
                            process.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            logging.warning("[TTS] say не завершился даже после kill")
            except Exception:
                logging.debug("Не удалось остановить say", exc_info=True)

        state.stop_speaking()

    def speak(self, text: str) -> None:
        """Озвучить текст системным голосом. Ошибки не поднимаются наружу."""
        text = str(text or "").strip()
        if not text:
            return

        # Тихий режим: панель должна получить текст, но звука быть не должно.
        if state.is_silent():
            print(f"[silent] NOVA AI: {text}")
            logging.info("Silent mode: skipped system TTS for: %s", text[:200])
            return

        print(f"NOVA AI: {text}")

        # Новая фраза всегда глушит предыдущую — иначе наложение голосов.
        self.stop()

        command = ["say"]
        if self.voice:
            command += ["-v", self.voice]
        command += ["-r", self.rate, "--", text]

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except OSError as error:
            # Никаких `say` (сломанный PATH, не-macOS среда) — это не повод
            # ронять NOVA AI: текст уже напечатан, панель его покажет.
            logging.error("Системный голос недоступен (say не запустился): %s", error)
            state.stop_speaking()
            return

        with self.lock:
            self.current_process = process
            self.speaking_text = text

        state.start_speaking(text)
        threading.Thread(target=self._await_playback, args=(process,), daemon=True).start()

    # ------------------------------------------------------------------
    def _await_playback(self, process) -> None:
        """Ждёт конца проигрывания и снимает флаг озвучки.

        Именно этот поток снимает `state.is_speaking`. Если он не доживёт до
        конца (процесс убит извне), флаг останется поднятым и голосовой цикл
        залипнет в режиме «слушаю только перебивание» — поэтому `finally`
        здесь обязателен, а `run_voice_loop` сверху ставит сторож.
        """
        try:
            _stdout, err = process.communicate()
            if process.returncode not in (0, -15):
                detail = (err or b"").decode("utf-8", "replace").strip()[:160]
                logging.warning(
                    "[TTS] say вернул код %s: %s", process.returncode, detail or "stderr пуст",
                )
        except Exception:
            logging.debug("say playback wait failed", exc_info=True)
        finally:
            with self.lock:
                # Снимаем состояние, только если это всё ещё наша фраза:
                # иначе мы бы стёрли процесс уже начатой новой озвучки.
                if self.current_process is process:
                    self.current_process = None
                    self.speaking_text = None
                    state.stop_speaking()


# Legacy class name for backward compatibility
class PiperTTS:
    """Deprecated: kept for any old references"""
    def __init__(self):
        self.tts = ElevenLabsTTS()
    
    def speak(self, text):
        self.tts.speak(text)
    
    def stop(self):
        self.tts.stop()