"""
Health checks для NOVA AI.
Проверяет доступность всех внешних зависимостей при старте.
"""
import logging
import requests
from config import (
    ELEVENLABS_API_KEY,
    TELEGRAM_API_ID,
    TELEGRAM_API_HASH,
)

logger = logging.getLogger(__name__)


def check_ai() -> tuple[bool, str]:
    """Проверка доступности AI: пингуем онлайн-провайдеров из цепочки
    (Groq/Gemini/Cerebras/OpenRouter/NIM) и возвращаем OK, если отвечает
    хотя бы один (остальные падают в фейловер и кулдаун без потери сервиса)."""
    from core import ai_providers as ap

    active = ap.providers_for()
    online = [p for p in active if p["id"] != "ollama"]
    if not active:
        return False, "Нет ключей провайдеров и Ollama недоступна"
    if not online:
        return False, "Доступна только локальная Ollama (нет онлайн-ключа)"
    last_err = "неизвестная ошибка"
    for provider in online:
        try:
            response = requests.post(
                f"{provider['url'].rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {provider['key']}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": provider["model"],
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 5,
                },
                timeout=10,
            )
            if response.ok:
                return True, f"OK ({provider['name']})"
            last_err = f"{provider['name']} HTTP {response.status_code}"
        except Exception as e:
            last_err = f"{provider['name']}: {e}"
    return False, f"Все онлайн-провайдеры недоступны ({last_err})"


def check_elevenlabs() -> tuple[bool, str]:
    """Проверка ElevenLabs API (если настроен)."""
    if not ELEVENLABS_API_KEY:
        return True, "Не настроен (используется macOS say)"
    try:
        response = requests.get(
            "https://api.elevenlabs.io/v1/user",
            headers={"xi-api-key": ELEVENLABS_API_KEY},
            timeout=10,
        )
        if response.ok:
            return True, "OK"
        return False, f"HTTP {response.status_code}"
    except Exception as e:
        return False, str(e)


def check_fish() -> tuple[bool, str]:
    """Проверка Fish.audio (голос NOVA AI): ключ и ID голоса заданы?"""
    from config import FISH_API_KEY, FISH_VOICE_ID, FISH_MODEL
    if not FISH_API_KEY:
        return False, "FISH_API_KEY не задан (голос NOVA AI выключен)"
    if not FISH_VOICE_ID:
        return False, "FISH_VOICE_ID не задан"
    return True, f"OK (voice={FISH_VOICE_ID[:8]}…, model={FISH_MODEL})"


def check_telegram() -> tuple[bool, str]:
    """Проверка Telegram API credentials."""
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        return True, "Не настроен"
    # Только проверка что credentials заданы, не коннектимся
    return True, "Credentials заданы"


def check_chrome() -> tuple[bool, str]:
    """Проверка что Chrome доступен."""
    import subprocess
    try:
        result = subprocess.run(
            ["osascript", "-e", 'tell application "Google Chrome" to get name'],
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0:
            return True, "OK"
        return False, "Chrome не отвечает на AppleScript"
    except Exception as e:
        return False, str(e)


def check_mic() -> tuple[bool, str]:
    """Проверка микрофона.

    Различает две разные поломки, которые раньше выглядели одинаково:
    - поток не открылся (нет доступа TCC, устройство занято) — это поломка;
    - поток открылся, но слышно только тишину — микрофон жив, возможно он
      в mute или выключен в системе. Помечать это как «нет доступа» неверно.
    """
    try:
        from voice.speech_to_text import (
            MIC_DEAD_LEVEL,
            find_vosk_model,
            mic_stream_status,
            pick_best_mic_index,
        )

        engine = (
            "Vosk, локально (без сети)"
            if find_vosk_model()
            else "Google STT (нужен интернет)"
        )

        index = pick_best_mic_index()
        if index is None:
            return False, "Микрофон не найден (нет входных устройств)"

        status = mic_stream_status(index)
        if not status["ok"]:
            return (
                False,
                f"Микрофон недоступен (вход [{index}], {engine}): {status['error']}. "
                "Проверьте доступ: Системные настройки → Конфиденциальность "
                "и безопасность → Микрофон (включите Python/Terminal)",
            )

        rms = status["rms"]
        if rms >= MIC_DEAD_LEVEL:
            return True, f"OK (вход [{index}], RMS={rms:.0f}, {engine})"
        return (
            True,
            f"OK, в комнате тихо (вход [{index}], RMS={rms:.0f}, {engine}) — "
            "микрофон открыт; если не слышно, проверьте его отключение "
            "(клавиша mute / Control Center)",
        )
    except Exception as e:
        return False, f"Ошибка: {e}"


def run_all_checks() -> dict:
    """Запуск всех проверок. Возвращает dict с результатами.

    Всегда критичные: AI, Fish-озвучка, Telegram, Chrome.
    Не критичные (резервные провайдеры): ElevenLabs — его недоступность
    не роняет общий статус, а лишь переключает озвучку на say/фолбэк.
    """
    checks = {
        "ai": check_ai,
        "elevenlabs": check_elevenlabs,
        "fish": check_fish,
        "mic": check_mic,
        "telegram": check_telegram,
        "chrome": check_chrome,
    }
    # Резервные подсистемы: падение НЕ влияет на общий статус
    non_critical = {"elevenlabs"}

    from config import INPUT_MODE
    if INPUT_MODE != "voice":
        # В текстовом режиме микрофон не критичен
        non_critical.add("mic")

    results = {}
    all_ok = True

    for name, check_fn in checks.items():
        ok, msg = check_fn()
        warn = (not ok) and name in non_critical
        results[name] = {"ok": ok, "message": msg, "warn": warn}
        if ok:
            logger.info("Health check passed: %s", name)
        elif warn:
            logger.warning("Health check warning (резерв): %s - %s", name, msg)
        else:
            all_ok = False
            logger.warning("Health check failed: %s - %s", name, msg)

    results["_overall"] = all_ok
    return results


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    results = run_all_checks()
    print(json.dumps(results, ensure_ascii=False, indent=2))