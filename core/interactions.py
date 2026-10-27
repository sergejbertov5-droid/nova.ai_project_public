"""
core/interactions.py — голосовое управление системой через AppleScript,
голограммой и панелью (безопасно, локально).

Всё, что требует прав (System Events), сделано аккуратными try/except
с понятным ответом, а не молчаливым падением: если macOS не даёт доступ,
NOVA AI честно говорит, что нужно разрешение.
"""
import logging
import os
import re
import signal
import subprocess

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(PROJECT_DIR, "logs")
HOLO_APP = os.path.join(PROJECT_DIR, "NOVA AI Hologram.app")
BRIGHTNESS_FILE = os.path.join(LOGS_DIR, "holo_brightness.ctl")


def _run(cmd) -> tuple:
    """Запуск команды с пограничным временем. (ok, output)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
        ok = result.returncode == 0
        return ok, (result.stdout or result.stderr or "").strip()
    except Exception as exc:
        logging.debug("interactions cmd failed: %s", exc)
        return False, str(exc)


# ----------------------------------------------------------------------
# Громкость и звук
# ----------------------------------------------------------------------
def set_volume_level(level: int) -> str:
    """Установить громкость 0..100. Возвращает ответ для озвучки."""
    if not isinstance(level, int):
        level = int(level)
    level = max(0, min(100, level))
    ok, _ = _osascript(f"set volume output volume {level}")
    if ok:
        return _plural_volume(level)
    return "Сэр, не удалось изменить громкость — macOS не дал доступ к управлению звуком."


def volume_delta(delta: int) -> str:
    """Изменить громкость на delta (±)."""
    ok, _ = _osascript(
        "set v to output volume of (get volume settings)\n"
        f"set volume output volume (v + {delta})"
    )
    if ok:
        v = current_volume()
        return _plural_volume(v)
    return "Сэр, не удалось изменить громкость — macOS не дал доступ к управлению звуком."


def current_volume() -> int:
    ok, out = _osascript("return output volume of (get volume settings)")
    if ok:
        try:
            return int(out)
        except (TypeError, ValueError):
            return -1
    return -1


def _plural_volume(level: int) -> str:
    if level <= 0:
        return "Громкость на нуле, сэр."
    if level < 35:
        return f"Громкость низкая — {level} процентов, сэр."
    if level < 80:
        return f"Громкость {level} процентов, сэр."
    return f"Громкость {level} процентов. Осторожнее с ушами, сэр."


def set_mute(on: bool) -> str:
    ok, _ = _osascript(f"set volume output muted {str(on).lower()}")
    if ok:
        return "Звук отключён, сэр." if on else "Звук включён, сэр."
    return "Сэр, не удалось управлять звуком — macOS не дал доступ."


# ----------------------------------------------------------------------
# Музыка (Apple Music)
# ----------------------------------------------------------------------
def music(action: str) -> str:
    """action: play | pause | next | previous | volume_up | volume_down."""
    scripts = {
        "play": 'tell application "Music" to play',
        "pause": 'tell application "Music" to pause',
        "next": 'tell application "Music" to next track',
        "previous": 'tell application "Music" to previous track',
    }
    script = scripts.get(action)
    if not script:
        return "Сэр, не знаю такого действия с музыкой."
    ok, out = _osascript(script)
    if ok:
        return _music_reply(action)
    return (
        "Сэр, не удалось управлять музыкой. Проверьте, что приложение Music "
        "доступно и разрешена автоматизация в Системных настройках."
    )


def _music_reply(action: str) -> str:
    replies = {
        "play": "Включаю музыку, сэр.",
        "pause": "Ставлю на паузу, сэр.",
        "next": "Следующий трек, сэр.",
        "previous": "Предыдущий трек, сэр.",
    }
    return replies.get(action, "Готово, сэр.")


# ----------------------------------------------------------------------
# Экран, блокировка, фокус, сон
# ----------------------------------------------------------------------
def lock_screen() -> str:
    """Блокирует экран (Cmd+Ctrl+Q через System Events)."""
    ok, _ = _osascript(
        'tell application "System Events" to keystroke "q" '
        'using {command down, control down}'
    )
    if ok:
        return "Блокирую экран, сэр."
    return (
        "Сэр, не могу заблокировать экран — разрешите NOVA AI управлять "
        "системой (Системные настройки → Конфиденциальность → Управление)."
    )


def display_sleep() -> str:
    ok, _ = _run(["pmset", "displaysleepnow"])
    if ok:
        return "Гашу экран, сэр."
    return "Сэр, не удалось погасить экран."


def system_sleep() -> str:
    ok, _ = _run(["pmset", "sleepnow"])
    if ok:
        return "Усыпляю компьютер, сэр."
    return "Сэр, не удалось усыпить компьютер."


def focus_mode(enable: bool = True) -> str:
    """Режим фокуса/Не беспокоить — пустой через osascript чипт в Control Center
    лучше не трогать без прав; просим явно и честно сообщаем результат."""
    mode_name = "включаю режим фокуса" if enable else "выключаю режим фокуса"
    # Пробуем через System Events кликом по меню-бару Не беспокоить.
    ok, _ = _run(
        [
            "osascript",
            "-e",
            'tell application "System Events" to tell process "ControlCenter" '
            'to click menu bar item 1 of menu bar 2',
        ]
    )
    if ok:
        return f"Сэр, {mode_name}."
    return (
        "Сэр, режим фокуса переключается вручную: macOS не даёт приложению "
        "управлять режимом фокуса без полного доступа к системе."
    )


def take_screenshot() -> str:
    """Снимок экрана на Desktop с временем в имени."""
    try:
        from datetime import datetime
    except ImportError:
        return "Сэр, не удалось сделать скриншот."
    path = os.path.expanduser(f"~/Desktop/jarvis_{datetime.now():%Y%m%d_%H%M%S}.png")
    ok, _ = _run(["screencapture", "-x", path])
    if ok and os.path.exists(path):
        return f"Скриншот сохранён на рабочий стол: {os.path.basename(path)}, сэр."
    return "Сэр, не удалось сделать скриншот — возможно, нет разрешения на запись экрана."


# ----------------------------------------------------------------------
# Голограмма
# ----------------------------------------------------------------------
def hologram_running() -> bool:
    ok, out = _run(["pgrep", "-f", "jarvis_hologram.py"])
    return ok and bool(out.strip())


def start_hologram() -> str:
    if hologram_running():
        return "Голограмма уже на экране, сэр."
    subprocess.Popen(
        ["open", "-g", HOLO_APP],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return "Включаю голограмму, сэр."


def stop_hologram() -> str:
    ok, out = _run(["pgrep", "-f", "jarvis_hologram.py"])
    if not ok or not out.strip():
        return "Голограмма и так выключена, сэр."
    for pid in out.split():
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            pass
    return "Выключаю голограмму, сэр."


def hologram_brightness(delta: float = 0.0, absolute: float = None) -> str:
    """Яркость голограммы: multiply 0.2..1.0 через ctl-файл."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    current = 1.0
    try:
        with open(BRIGHTNESS_FILE, "r") as f:
            value = float(f.read().strip())
        if 0.2 <= value <= 1.0:
            current = value
    except (OSError, ValueError):
        pass
    if absolute is not None:
        current = absolute
    else:
        current = max(0.2, min(1.0, current + delta))
    try:
        with open(BRIGHTNESS_FILE, "w") as f:
            f.write(f"{current:.2f}")
    except OSError:
        return "Сэр, не удалось изменить яркость голограммы."
    if delta > 0:
        return "Ярче, сэр."
    if delta < 0:
        return "Темнее, сэр."
    return f"Яркость голограммы: {int(current * 100)} процентов, сэр."


# ----------------------------------------------------------------------
# Панель управления
# ----------------------------------------------------------------------
def panel_running() -> bool:
    ok, out = _run(["pgrep", "-f", "control_gui.py"])
    return ok and bool(out.strip())


def start_panel() -> str:
    if panel_running():
        return "Панель уже открыта, сэр."
    subprocess.Popen(
        ["python3", os.path.join(PROJECT_DIR, "control_gui.py")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return "Открываю панель управления, сэр."


def stop_panel() -> str:
    if not panel_running():
        return "Панель и так закрыта, сэр."
    ok, out = _run(["pkill", "-f", "control_gui.py"])
    if ok:
        return "Закрываю панель, сэр."
    return "Не удалось закрыть панель, сэр."


# ----------------------------------------------------------------------
# Хелпер osascript
# ----------------------------------------------------------------------
def _osascript(script: str) -> tuple:
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=15,
        )
        ok = result.returncode == 0
        return ok, (result.stdout or result.stderr or "").strip()
    except Exception as exc:
        logging.debug("osascript failed: %s", exc)
        return False, str(exc)