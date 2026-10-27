#!/usr/bin/env python3
"""
control_gui.py — панель управления NOVA AI (интерфейс 2026, v2).

Красивое тёмное оформление: градиентная лента сверху, метка NOVA AI в
двойном светящемся кольце, карточки «Состояние» и «Управление» (с мягкой
подсветкой при наведении), статус-чипы, дышащий индикатор.

Кнопки:
  • Включить / Выключить NOVA AI — скрытый запуск (LaunchAgent, без окна
    Terminal) + «живая» голограмма позади всех окон.
  • Образ — включить/выключить голограмму NOVA AI отдельно.
  • Настройки / Логи.
"""
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import tkinter as tk
from tkinter import messagebox

import ui_kit as ui

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PLIST = os.path.expanduser("~/Library/LaunchAgents/com.nova_ai.assistant.plist")
LABEL = "com.nova_ai.assistant"
LOGS_DIR = os.path.join(PROJECT_DIR, "logs")
HOLO_APP = os.path.join(PROJECT_DIR, "NOVA AI Hologram.app")
BUILD = "2026 · v4 core"

# IPC-файлы между панелью и голосовым циклом (см. voice/ipc.py).
WAKE_FILE = os.path.join(LOGS_DIR, "jarvis_wake.ctl")
SILENT_FILE = os.path.join(LOGS_DIR, "jarvis_silent.ctl")
DIALOG_FILE = os.path.join(LOGS_DIR, "dialog.jsonl")

ACCENT, ACCENT_DK, ACCENT_HOVER = ui.ACCENT, ui.ACCENT_DK, ui.ACCENT_HOVER


# ----------------------------------------------------------------------
# IPC панель → голосовой цикл (разбудить / тихий режим / история диалога)
# ----------------------------------------------------------------------
def _request_wake():
    """Кнопка «Разбудить» / Cmd+J: пишем метку времени в ctl-файл."""
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        with open(WAKE_FILE, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def _read_silent_file() -> bool:
    try:
        with open(SILENT_FILE, "r") as f:
            return f.read().strip() == "1"
    except OSError:
        return False


def _write_silent_file(flag: bool) -> None:
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        with open(SILENT_FILE, "w") as f:
            f.write("1" if flag else "0")
    except OSError:
        pass


def _read_dialog_lines(n=8) -> list:
    try:
        with open(DIALOG_FILE, "r") as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for line in lines[-n:]:
        try:
            data = json.loads(line)
            if data.get("text"):
                out.append(data)
        except (ValueError, TypeError):
            continue
    return out


# ----------------------------------------------------------------------
# Процессы NOVA AI и голограммы
# ----------------------------------------------------------------------
def jarvis_pids():
    patterns = [
        "launch_jarvis.sh",
        "python3 -m main",
        "python -m main",
        "main.py",
        "run_nova_ai_terminal.command",
    ]
    results = set()
    for pattern in patterns:
        result = subprocess.run(
            ["pgrep", "-f", pattern], capture_output=True, text=True
        )
        for value in result.stdout.split():
            if value.isdigit():
                results.add(int(value))
    return sorted(results)


def holo_pids():
    result = subprocess.run(
        ["pgrep", "-f", "jarvis_hologram.py"], capture_output=True, text=True
    )
    return [int(v) for v in result.stdout.split() if v.isdigit()]


def holo_running():
    return bool(holo_pids())


def start_hologram():
    if holo_running():
        return
    subprocess.Popen(
        ["open", "-g", HOLO_APP], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def stop_hologram():
    for pid in holo_pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def agent_loaded():
    result = subprocess.run(
        ["launchctl", "list", LABEL], capture_output=True, text=True
    )
    return result.returncode == 0


def agent_running():
    result = subprocess.run(
        ["launchctl", "list", LABEL], capture_output=True, text=True
    )
    if result.returncode != 0:
        return False
    out = (result.stdout or "").strip()
    if not out:
        return False
    m = re.search(r'"PID"\s*=\s*(-?\d+)', out)
    if m:
        return m.group(1).isdigit() and int(m.group(1)) > 0
    parts = out.split()
    return len(parts) >= 3 and parts[0].isdigit()


def is_running():
    return agent_running() or bool(jarvis_pids())


def stop_nova_ai():
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
        capture_output=True,
    )
    for _ in range(10):
        if not agent_loaded():
            break
        time.sleep(0.5)
    for pid in jarvis_pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    stop_hologram()


def start_jarvis() -> bool:
    if not os.path.exists(PLIST):
        messagebox.showerror("NOVA AI", "Файл автозапуска не найден.")
        return False
    stop_nova_ai()
    time.sleep(1.5)
    last_err = ""
    for attempt in range(4):
        result = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", PLIST],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            start_hologram()
            return True
        last_err = (result.stderr or result.stdout).strip()
        if attempt < 3:
            time.sleep(2.5)
    messagebox.showerror(
        "NOVA AI", f"Не удалось запустить NOVA AI.\n\n{last_err or 'Ошибка launchctl'}"
    )
    return False


def _control_panel_pids():
    result = subprocess.run(
        ["pgrep", "-f", "control_gui.py"], capture_output=True, text=True
    )
    pids = []
    for value in result.stdout.split():
        if not value.isdigit():
            continue
        comm = subprocess.run(
            ["ps", "-p", value, "-o", "comm="], capture_output=True, text=True
        ).stdout.strip()
        if comm.endswith("/Python") or comm in ("python3", "python"):
            pids.append(int(value))
    return pids


def _single_instance():
    me = os.getpid()
    return all(pid == me for pid in _control_panel_pids())


# ----------------------------------------------------------------------
# Действия
# ----------------------------------------------------------------------
def toggle():
    toggle_button.set_enabled(False)
    try:
        if is_running():
            stop_nova_ai()
            root.after(900, refresh)
        else:
            ok = start_jarvis()
            root.after(1200 if ok else 300, refresh)
    finally:
        toggle_button.set_enabled(True)


def toggle_hologram():
    holo_button.set_enabled(False)
    try:
        if holo_running():
            stop_hologram()
        else:
            start_hologram()
        root.after(600, refresh)
    finally:
        holo_button.set_enabled(True)


def on_wake():
    """Кнопка «Разбудить» / Cmd+J — голосовой цикл примет фразу без wake word."""
    _request_wake()
    wake_button.set_text("Разбужен ✓")
    root.after(1600, lambda: wake_button.set_text("Разбудить"))


def toggle_silent():
    """Тумблер тихого режима: только панель, без озвучки."""
    target = not _read_silent_file()
    _write_silent_file(target)
    _update_silent_button(target)


def _update_silent_button(is_silent: bool):
    if is_silent:
        silent_button.set_text("Тихо: вкл")
        silent_button.set_kind("danger")
    else:
        silent_button.set_text("Тихо: выкл")
        silent_button.set_kind("neutral")


def open_settings():
    import control_settings
    # Открываем сразу на разделе AI / API KEYS — главный сценарий задачи:
    # вставить Groq-ключ, не открывая .env вручную.
    control_settings.open_settings(parent=root, tab="ai")


def open_logs():
    os.makedirs(LOGS_DIR, exist_ok=True)
    subprocess.run(["open", LOGS_DIR])


# ----------------------------------------------------------------------
# Оформление
# ----------------------------------------------------------------------
def _gradient_strip(parent, width=520, height=6):
    """Циановая градиентная лента сверху окна."""
    canvas = tk.Canvas(parent, width=width, height=height, bg=ui.BG,
                       highlightthickness=0)
    stops = [
        (0.00, ui.ACCENT_DK),
        (0.35, ui.ACCENT),
        (0.70, ui.ACCENT_HOVER),
        (1.00, ui.ACCENT),
    ]
    tiles = 80
    for i in range(tiles):
        t = i / (tiles - 1)
        for a, b in zip(stops, stops[1:]):
            if a[0] <= t <= b[0]:
                tt = (t - a[0]) / max(1e-6, b[0] - a[0])
                color = ui.interpolate(a[1], b[1], tt)
                break
        x0 = int(width * i / tiles)
        x1 = int(width * (i + 1) / tiles)
        canvas.create_rectangle(x0, 0, x1, height, fill=color, outline="")
    return canvas


def _avatar(parent, size=132):
    """Метка NOVA AI в двойном светящемся кольце."""
    canvas = tk.Canvas(parent, width=size, height=size, bg=ui.BG,
                       highlightthickness=0)
    c = size / 2
    # внешнее мягкое кольцо-свечение
    canvas.create_oval(6, 6, size - 6, size - 6,
                       outline="#0e6f9e", width=6)
    # тонкое циановое кольцо
    canvas.create_oval(16, 16, size - 16, size - 16,
                       outline=ACCENT, width=2)
    canvas.create_text(c, c, text="J", fill=ACCENT,
                       font=(ui.FONT, 40, "bold"))
    return canvas


def _card(parent, title):
    card = ui.card(parent, bg=ui.PANEL2)
    header = tk.Frame(card, bg=ui.PANEL2)
    header.pack(fill="x", padx=4, pady=(0, 6))
    ui.label(header, title, size=8, bold=True, color=ui.FAINT,
             bg=ui.PANEL2).pack(side="left")
    ui.label(header, "•••", size=8, color=ui.FAINT, bg=ui.PANEL2).pack(side="right")
    return card


def _chip(parent, text, color, fill=ui.PANEL2):
    frame = tk.Frame(parent, bg=fill)
    ui.label(frame, text, size=9, bold=True, bg=fill, color=color).pack(
        padx=10, pady=4
    )
    return frame


def _build_chips(parent):
    from core.envconfig import read_env
    env = read_env()
    ctx = {}
    ai_key = bool(env.get("AI_API_KEY") or env.get("GROQ_API_KEY"))
    gem_key = bool(env.get("GEMINI_API_KEY"))
    if ai_key:
        ctx["AI"] = ("AI · Groq ✓", ui.GREEN)
    elif gem_key:
        ctx["AI"] = ("AI · Gemini (вкл. VPN)", ACCENT)
    else:
        ctx["AI"] = ("AI · нет ключа", ui.FAINT)
    voice = "голос · облако"
    if not (env.get("FISH_API_KEY") or env.get("ELEVENLABS_API_KEY")):
        voice = "голос · macOS"
    ctx["Голос"] = (voice, ui.GREEN)
    bot = env.get("TG_BOT_ENABLED", "").lower() in ("1", "true", "yes")
    ctx["Бот"] = (
        ("бот · вкл", ui.GREEN) if bot else ("бот · выкл", ui.FAINT)
    )
    for text, color in ctx.values():
        _chip(parent, text, color).pack(side="left", padx=4)


# ----------------------------------------------------------------------
# Окно
# ----------------------------------------------------------------------
def main() -> None:
    if not _single_instance():
        sys.exit(0)

    global root, status_label, toggle_button, status_dot, orb, holo_button
    global wake_button, silent_button, dialog_label
    root = tk.Tk()
    root.title("NOVA AI")
    root.geometry(os.environ.get("JARVIS_CONTROL_GEOMETRY", "520x820"))
    root.resizable(False, False)
    root.configure(bg=ui.BG)
    ui.init_styles(root)
    ui.install_motion_hover(root)

    # Градиентная лента
    _gradient_strip(root, width=520).pack(fill="x")

    # Метка NOVA AI
    _avatar(root).pack(pady=(22, 4))

    # Заголовок
    ui.label(root, "NOVA AI", size=26, bold=True, bg=ui.BG,
             color=ui.TEXT).pack()
    ui.label(root, "personal assistant · hologram core", size=9,
             color=ACCENT, bg=ui.BG).pack(pady=(0, 4))
    ui.label(root, BUILD, size=8, color=ui.FAINT, bg=ui.BG).pack()

    # --- Карточка «Состояние» ---
    status_card = _card(root, "СОСТОЯНИЕ")
    status_card.pack(fill="x", padx=28, pady=(14, 8))

    orb = ui.Orb(status_card, size=112)
    orb.pack(pady=(2, 6))

    status_row = tk.Frame(status_card, bg=ui.PANEL2)
    status_row.pack(pady=(0, 8))
    status_dot = ui.label(status_row, "●", size=13, bold=True,
                          bg=ui.PANEL2, color=ui.FAINT)
    status_dot.pack(side="left", padx=(0, 8))
    status_label = ui.label(status_row, "проверка…", size=13,
                            bg=ui.PANEL2, color=ui.DIM)
    status_label.pack(side="left")

    chips = tk.Frame(status_card, bg=ui.PANEL2)
    chips.pack(pady=(0, 8))
    _build_chips(chips)

    # --- Карточка «Управление» ---
    control_card = _card(root, "УПРАВЛЕНИЕ")
    control_card.pack(fill="x", padx=28, pady=(4, 10))

    toggle_button = ui.ModernButton(control_card, "Включить NOVA AI", toggle,
                                    kind="green", size=15, height=52,
                                    radius=16, width=330)
    toggle_button.pack(pady=(4, 10))

    buttons_row = tk.Frame(control_card, bg=ui.PANEL2)
    buttons_row.pack(pady=(0, 6))
    ui.ModernButton(buttons_row, "⚙  Настройки", open_settings,
                    kind="accent", size=12, width=148, height=42,
                    radius=13).pack(side="left", padx=4)
    ui.ModernButton(buttons_row, "Логи", open_logs,
                    kind="neutral", size=12, width=84, height=42,
                    radius=13).pack(side="left", padx=4)
    holo_button = ui.ModernButton(buttons_row, "Образ: …", toggle_hologram,
                                  kind="accent", size=12, width=128, height=42,
                                  radius=13)
    holo_button.pack(side="left", padx=4)

    # Пункт 16: пробуждение панелью («Разбудить», Cmd+J) и пункт 15:
    # тумблер тихого режима (только визуально).
    wake_row = tk.Frame(control_card, bg=ui.PANEL2)
    wake_row.pack(pady=(0, 10))
    wake_button = ui.ModernButton(wake_row, "Разбудить", on_wake,
                                  kind="accent", size=12, width=148, height=40,
                                  radius=13)
    wake_button.pack(side="left", padx=4)
    silent_button = ui.ModernButton(wake_row, "Тихо: выкл", toggle_silent,
                                    kind="neutral", size=12, width=148, height=40,
                                    radius=13)
    silent_button.pack(side="left", padx=4)
    root.bind_all("<Command-j>", lambda _e: on_wake())
    root.bind_all("<Command-J>", lambda _e: on_wake())

    # --- Карточка «Диалог» (пункт 14: история на панели) ---
    dialog_card = _card(root, "ДИАЛОГ")
    dialog_card.pack(fill="x", padx=28, pady=(4, 10))
    dialog_label = ui.label(
        dialog_card, "Пусто — скажите что-нибудь NOVA AI",
        size=10, color=ui.FAINT, bg=ui.PANEL2,
    )
    dialog_label.config(justify="left", anchor="w")
    dialog_label.pack(fill="x", padx=10, pady=(2, 8))

    # --- Футер ---
    ui.label(root, "Образ NOVA AI — позади всех окон · клики проходят насквозь",
             size=8, color=ui.FAINT, bg=ui.BG).pack(side="bottom", pady=(0, 10))
    ui.label(root, "Изменения в настройках — после перезапуска NOVA AI",
             size=9, color=ui.FAINT, bg=ui.BG).pack(side="bottom", pady=(0, 1))

    refresh()
    root.after(350, lambda: (root.lift(), root.attributes("-topmost", True),
                             root.focus_force()))
    root.after(700, lambda: root.attributes("-topmost", False))
    if os.environ.get("JARVIS_CONTROL_DEBUG"):
        root.after(900, _report_position)
    ui.fade_in_window(root, 260)
    root.protocol("WM_DELETE_WINDOW",
                  lambda: ui.fade_out_window(root, 190, on_done=root.destroy))
    root.mainloop()


def _report_position():
    root.update_idletasks()
    with open("/tmp/jarvis_panel_pos.txt", "w") as f:
        f.write(f"{root.winfo_rootx()} {root.winfo_rooty()} "
                f"{root.winfo_width()} {root.winfo_height()} "
                f"{root.winfo_id()}\n")


# ----------------------------------------------------------------------
# Циклическое обновление
# ----------------------------------------------------------------------
def refresh():
    global orb
    running = is_running()
    orb.set_running(running)
    if running:
        status_label.config(text="NOVA AI работает в фоне", fg=ui.TEXT)
        status_dot.config(fg=ui.GREEN)
        ui.start_pulse(status_dot, "#8dffd6", ui.GREEN, period_ms=1500)
        toggle_button.set_kind("danger")
        toggle_button.set_text("Выключить NOVA AI")
    else:
        status_label.config(text="NOVA AI выключен", fg=ui.DIM)
        ui.stop_pulse(status_dot)
        status_dot.config(fg=ui.RED)
        toggle_button.set_kind("green")
        toggle_button.set_text("Включить NOVA AI")
    holo_button.set_text("Образ: вкл" if holo_running() else "Образ: выкл")
    _update_silent_button(_read_silent_file())

    # Пункт 14: последние реплики диалога на панели.
    try:
        turns = _read_dialog_lines(8)
        if turns:
            rows = []
            for turn in turns:
                role = "Вы" if turn.get("role") == "user" else "NOVA AI"
                text = (turn.get("text") or "").strip().replace("\n", " ")
                if len(text) > 64:
                    text = text[:61] + "…"
                rows.append(f"{role}: {text}")
            dialog_label.config(text="\n".join(rows), fg=ui.DIM)
        else:
            dialog_label.config(
                text="Пусто — скажите что-нибудь NOVA AI", fg=ui.FAINT
            )
    except Exception:
        logging.exception("Dialog card refresh failed")  # noqa: F821
    root.after(2000, refresh)


if __name__ == "__main__":
    main()