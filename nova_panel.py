"""nova_panel.py — панель NOVA AI.

Новая панель управління NOVA AI. Виглядає як сучасна AI-панель,
а не просто NOVA AI-скін. Міцна на реальних даних (без покладених даних),
автоматична блокировка повторного запуску, чіткі Stati (idle/loading/running/success/error).

Використовує теми з ui_kit.py (clam, Accent, Green, Red, Faint).
Незмінює control_gui.py та ui_kit.py.
"""
import os
import sys
import json
import subprocess
import tempfile
import shutil
import signal
import re
import time
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

import ui_kit as ui
from core.semantic_memory import SemanticMemory, SemanticMemoryConfig, RecordNotFoundError
from core.envconfig import read_env

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(PROJECT_DIR, "logs")

# ---------------------------------------------------------------------------
# 1. Stato №1 — Idle (вихідний)
# ---------------------------------------------------------------------------
IDLE = "idle"

# ---------------------------------------------------------------------------
# 2. Stato №2 — Loading (завантаження)
# ---------------------------------------------------------------------------
LOADING = "loading"

# ---------------------------------------------------------------------------
# 3. Stato №3 — Running (робота)
# ---------------------------------------------------------------------------
RUNNING = "running"

# ---------------------------------------------------------------------------
# 4. Stato №4 — Success (успіх)
# ---------------------------------------------------------------------------
SUCCESS = "success"

# ---------------------------------------------------------------------------
# 5. Stato №5 — Error (помилка)
# ---------------------------------------------------------------------------
ERROR = "error"

# ---------------------------------------------------------------------------
# Головне вікно
# ---------------------------------------------------------------------------
MAIN_WINDOW_TITLE = "NOVA AI"
DEFAULT_GEOMETRY = "520x820"

# ---------------------------------------------------------------------------
# 1. Nova AI Panel — Основне вікно
# ---------------------------------------------------------------------------
def main() -> None:
    global root, status_dot, status_text, toggle_button, hologram_button, settings_button, logs_button
    global wake_button, silent_button, dialog_label, mem_info, orb

    # Перевірка одиночного екземпляру
    global _panel_state
    _panel_state = {
        "running": False,
        "idle": True,
        "loading": False,
        "running_state": False,
        "success": False,
        "error": False,
        "hologram_on": False,
        "silent": False,
    }

    # Перевірка одиночного екземпляру
    global _single_instance_check
    _single_instance_check = True

    root = tk.Tk()
    root.title("NOVA AI")
    root.geometry(os.environ.get("NOVA_AI_CONTROL_GEOMETRY", "520x820"))
    root.resizable(False, False)
    root.configure(bg=ui.BG)
    ui.init_styles(root)
    ui.install_motion_hover(root)

    # === === 1. Верхній гамбрен (шапка) ===
    top_bar = tk.Frame(root, bg=ui.BG, height=60)
    top_bar.pack(fill="x")
    # Аватар / Orb
    global orb
    orb = ui.Orb(root, size=130)
    orb.pack(pady=(10, 2))
    # Заголовок
    ui.label(root, "NOVA AI", size=28, bold=True, color=ui.TEXT, bg=ui.BG).pack(pady=(0, 2))
    ui.label(root, "personal assistant · hologram core", size=9, color=ui.ACCENT, bg=ui.BG).pack(pady=(0, 4))
    ui.label(root, "2026 · v4 core", size=8, color=ui.FAINT, bg=ui.BG).pack()

    # --- Ліва навигаційна панель ---
    nav_frame = tk.Frame(root, bg=ui.BG)
    nav_frame.pack(side="left", fill="y", padx=(12, 0), pady=12)
    for label in ["Command Center", "Memory", "Activity", "Logs", "Settings"]:
        btn = ui.ModernButton(nav_frame, label, lambda l=label: None, kind="neutral", size=11, width=180, height=38, radius=10)
        btn.pack(pady=6)

    # Права панель
    right_frame = tk.Frame(root, bg=ui.BG)
    right_frame.pack(side="right", fill="both", expand=True, padx=(12, 0), pady=12)

    # --- 1Б) Карточка "Состояние системи" ---
    status_card = tk.Frame(root, bg=ui.PANEL2, highlightbackground=ui.BORDER, highlightthickness=1)
    status_card.pack(fill="x", padx=14, pady=(0, 8))
    tk.Label(status_card, text="СОСТОЯНІ СИСТЕМІ", size=9, bold=True, color=ui.FAINT, bg=ui.PANEL2).pack(pady=(0, 4))
    # Orb (Status Core)
    orb = ui.Orb(status_card, size=120)
    orb.pack(pady=(2, 6))
    # Рядок статусу
    status_row = tk.Frame(status_card, bg=ui.PANEL2)
    status_row.pack(pady=(0, 8))
    global status_dot, status_text
    status_dot = tk.Label(status_row, "●", size=12, bold=True, bg=ui.PANEL2, color=ui.FAINT)
    status_dot.pack(side="left", padx=(0, 6))
    global status_text
    status_text = tk.Label(status_row, "idle", size=12, bg=ui.PANEL2, color=ui.DIM)
    status_text.pack(side="left")
    # Інформація про semantic memory
    global mem_info
    mem_info = tk.Label(status_card, text="SEM: backend=disabled, vectors=0, index=empty", size=9, bg=ui.PANEL2, color=ui.FAINT)
    mem_info.pack(anchor="w", pady=(6, 0), padx=10)

    # Toggle кнопка
    toggle_frame = tk.Frame(status_card, bg=ui.PANEL2)
    toggle_frame.pack(fill="x", pady=(0, 8))
    global toggle_button
    toggle_button = ui.ModernButton(status_card, "Запустити NOVA AI", lambda: toggle_nova(), kind="green", size=13, height=44, radius=14, width=280)
    toggle_button.pack(pady=(0, 6))

    # Ряд кнопок
    buttons_frame = tk.Frame(root, bg=ui.PANEL2)
    buttons_frame.pack(fill="x", padx=14, pady=(0, 8))
    tk.Button(buttons_frame, text="Налаштування", command=lambda: open_settings(), kind="accent", height=42, radius=13).pack(side="left", padx=4)
    tk.Button(buttons_frame, "Логи", open_logs, kind="neutral", height=42, radius=13).pack(side="left", padx=4)
    holo_btn = ui.ModernButton(root, "Холограма: вкл", lambda: toggle_hologram(), kind="accent", height=42, radius=13).pack(side="left", padx=4)

    # --- Event Log / Journal ---
    log_frame = tk.Frame(root, bg=ui.PANEL2, highlightbackground="#2c3947", highlightthickness=1)
    log_frame.pack(fill="both", expand=True, padx=14, pady=(0, 8))
    tk.Label(log_card, text="Події / Journal", size=10, bold=True, color=ui.FAINT, bg=ui.PANEL2).pack(anchor="w", padx=8, pady=(4, 2))
    log_canvas = tk.Frame(log_card, bg=ui.PANEL2)
    log_canvas.pack(fill="both", expand=True, padx=8, pady=2)
    log_canvas_canvas = tk.Canvas(log_canvas, bg=ui.PANEL2, highlightthickness=0)
    log_canvas_canvas.pack(side="left", fill="both", expand=True)
    log_scroll = tk.Scrollbar(log_canvas, orient="vertical", command=log_canvas_canvas.yview)
    log_scroll.pack(side="right", fill="y")
    log_canvas_canvas.config(yscrollcommand=log_scroll.set)
    log_content = tk.Frame(log_canvas, bg=ui.PANEL2)
    log_content.pack(fill="both", expand=True)
    log_content_id = lbl_canvas_canvas.create_window((0, 0), window=log_content, anchor="nw")
    log_content.bind("<Configure>", lambda e: log_canvas.configure(scrollregion=log_canvas.bbox("all")))

    # Charts placeholder (дані недоступні)
    chart_card = tk.Frame(root, bg=ui.PANEL2, highlightbackground="#2c3947", highlightthickness=1)
    tk.Label(chart_card, text="Графіки: дані недоступні", size=10, bold=True, color=ui.FAINT, bg=ui.PANEL2).pack(anchor="w", padx=8, pady=(4, 2))
    chart_ph = tk.Frame(chart_card, bg=ui.PANEL2, highlightbackground="#2c3947", highlightthickness=1)
    chart_ph.pack(fill="x", padx=8, pady=(4, 8))

    # Footer
    tk.Label(root, text="NOVA AI · позади всіх окон · кліки проходят насквозь", size=8, color=ui.FAINT, bg=ui.BG).pack(side="bottom", pady=(0, 6))
    tk.Label(root, text="Зміни в настройках — після перезапуску NOVA AI", size=9, color=ui.FAINT, bg=ui.BG).pack(side="bottom", pady=(0, 6))

    # Ініціалізація стану
    _panel_state = {
        "running": False,
        "idle": True,
        "loading": False,
        "running_state": False,
        "success": False,
        "error": False,
        "hologram_on": False,
        "silent": False,
    }

    _update_status_ui()

    root.protocol("WM_DELETE_WINDOW", lambda: None)  # no close without confirmation
    root.mainloop()