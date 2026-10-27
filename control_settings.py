#!/usr/bin/env python3
"""
control_settings.py — панель настроек NOVA AI (открывается из NOVA AI Control).

Вкладки:
  1) Безопасность — мастер-пароль на личные данные: создание, смена, удаление.
     Пока пароль не введён, графы с личными данными во вкладке
     «Доступы и данные» заблокированы. Здесь же — «удалить все личные ключи»
     (для безопасной передачи проекта другому человеку).
  2) Доступы и данные — подпункты доступа к Telegram (бот и аккаунт),
     графы подключения Telegram API и других личных данных (AI, озвучка),
     а также их удаление. Все поля защищены мастер-паролем.
  3) Разрешения — права NOVA AI на действия в компьютере
     (сохраняются в data/permissions.json).
  4) Общие — режим ввода (голос/текст), персона.

Изменения ключей/настроек применяются после перезапуска NOVA AI.
"""
import glob
import json
import os
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

import requests

import ui_kit as ui
from core.ai_providers import (
    PROVIDER_ORDER,
    PROVIDERS,
    clear_cooldown,
    cooldown_report,
    ollama_up,
    provider_summary,
    providers_for,
    redact_secrets,
)
# Штатный AI-рантайм: «Test Connection» ходит в API тем же путём,
# что и сам NOVA AI (registry → _post_provider с ретраями), а не
# самописным HTTP-клиентом.
from core.brain import AIBrain, RetryableHTTPError
from core.envconfig import (
    ENV_FILE,
    delete_all_sensitive,
    delete_keys,
    read_env,
    write_env,
)
from core.commands import tools_by_permission
from core.permissions import (
    DEFAULT_PERMISSIONS_FILE,
    PermissionPolicy,
)
from core.vault import (
    MIN_PASSWORD_LENGTH,
    change_password,
    delete_password,
    has_password,
    has_password_broken,
    reset_password,
    set_password,
    verify_password,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_SESSION_PREFIX = os.path.join("data", "telegram_bot_session")
BOT_STATE_FILE = os.path.join("data", "telegram_bot_state.json")

RISK_COLORS = {"низкий": "#43e08a", "средний": "#ffcc4d",
               "высокий": "#ff9640", "критичный": "#ff5a52"}

# Ключи по подразделам вкладки «Доступы и данные»
ACCOUNT_KEYS = [
    ("TELEGRAM_API_ID", "Telegram — API ID",
     "my.telegram.org → API development tools. Это секрет: не передавайте третьим лицам.", True),
    ("TELEGRAM_API_HASH", "Telegram — API Hash",
     "Пароль приложения Telegram (my.telegram.org). Секрет.", True),
]
AI_KEYS = [
    ("AI_API_KEY", "AI ключ (Groq / Gemini / OpenAI)",
     "Для AI-выводов и решений. Создать бесплатно: console.groq.com/keys. Секрет.", True),
    ("AI_BASE_URL", "AI — эндпоинт (URL)",
     "Например: https://api.groq.com/openai/v1", False),
    ("AI_MODEL", "AI — модель",
     "Например: llama-3.3-70b-versatile", False),
]
TTS_KEYS = [
    ("TTS_PROVIDER", "Источник голоса (auto/fish/elevenlabs)",
     "auto = Fish.audio, если ключ задан, иначе ElevenLabs.", False),
    ("FISH_API_KEY", "Fish.audio — API ключ",
     "Голос NOVA AI из фильма. fish.audio → API Keys → создать ключ (бесплатный тир). Секрет.", True),
    ("FISH_VOICE_ID", "Fish.audio — ID голоса (NOVA AI)",
     "По умолчанию стоит голос NOVA AI из фильма.", False),
    ("FISH_MODEL", "Fish.audio — модель",
     "s2.1-pro-free (беспл.) или s2.1-pro / s2-pro.", False),
    ("ELEVENLABS_API_KEY", "ElevenLabs — API ключ (фолбэк)",
     "Резерв, если Fish недоступен: elevenlabs.io → Profile → API Keys. Секрет.", True),
    ("ELEVENLABS_VOICE_ID", "ElevenLabs — голос (voice ID)", "", False),
    ("ELEVENLABS_MODEL_ID", "ElevenLabs — модель озвучки", "", False),
]


def _entry(card, var, secret=False, font_size=12):
    e = tk.Entry(
        card, textvariable=var, font=(ui.MONO, font_size),
        bg=ui.FIELD, fg="#dff4ff", insertbackground=ui.ACCENT,
        relief="flat", bd=0,
        highlightthickness=1, highlightbackground=ui.BORDER, highlightcolor=ui.ACCENT,
    )
    if secret:
        e.configure(show="\u2022")
    return e


def _env_bool(env, key, default=False):
    return env.get(key, "true" if default else "false").strip().lower() == "true"


def _mmss(seconds: int) -> str:
    """Секунды по-человечески: «45 с», «2 мин», «1 ч 5 мин»."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} с"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    return f"{seconds // 3600} ч {(seconds % 3600) // 60} мин"


def _clabel(parent, text, size=11, color=ui.TEXT, bold=False, **kw):
    """Лейбл внутри карточки (bg совпадает с фоном карточки — ровно)."""
    kw.setdefault("bg", ui.PANEL2)
    return ui.label(parent, text, size=size, color=color, bold=bold, **kw)


def _set_enabled(widget, enabled):
    """Рекурсивно включает/выключает виджеты (ttk и tk)."""
    state = "normal" if enabled else "disabled"
    queue = [widget]
    while queue:
        w = queue.pop()
        try:
            if isinstance(w, ttk.Widget):
                w.state(["!disabled"] if enabled else ["disabled"])
            elif isinstance(w, (tk.Entry, tk.Spinbox, tk.Text)):
                w.configure(state=state)
            elif isinstance(w, tk.Button):
                w.configure(state=state)
        except tk.TclError:
            pass
        try:
            queue.extend(w.winfo_children())
        except tk.TclError:
            pass


# -----------------------------------------------------------
# Test Connection: человеческое объяснение ошибки API
# -----------------------------------------------------------
def _error_detail(body: str) -> str:
    """Первая строка сообщения провайдера об ошибке (без раскрытия секретов)."""
    if not body:
        return ""
    try:
        payload = json.loads(body)
    except ValueError:
        for line in str(body).splitlines():
            line = line.strip()
            if line and not line.startswith("{") and not line.startswith(" "):
                return line[:140]
        return ""
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        msg = err.get("message") or err.get("code") or ""
    elif isinstance(err, str):
        msg = err
    else:
        msg = ""
    return str(msg).strip()[:140]


def _geo_body(body: str) -> bool:
    """Тело намекает на географические ограничения провайдера."""
    low = (body or "").lower()
    return any(w in low for w in (
        "location", "region", "country", "geo",
        "not available in your", "unsupported region",
    ))


def _classify_status(status, body: str = "") -> str:
    """Причина ошибки API по HTTP-коду — понятно и без секретов.

    Тело уже прошло через redact_secrets. HTTP-код допускается
    показывать как техническую информацию (часть архитектуры проекта).
    """
    detail = _error_detail(body)
    if status == 401:
        reason = "Invalid API key"
    elif status == 403:
        reason = ("Provider unavailable for this region"
                  if _geo_body(body) else "Invalid API key")
    elif status == 404:
        reason = "Model unavailable"
    elif status == 429:
        reason = "Rate limit"
    elif status in (400, 422):
        reason = "Invalid request"
    elif 500 <= (status or 0) < 600:
        reason = "Provider unavailable"
    else:
        reason = "HTTP error"
    text = f"{reason} (HTTP {status})" if status else reason
    if detail:
        text = f"{text} — {detail}"
    return text[:260]


class SettingsWindow:
    def __init__(self, parent=None):
        self.parent = parent
        self.win = tk.Toplevel(parent) if parent else tk.Tk()
        self.win.title("NOVA AI — Настройки")
        self.win.configure(bg=ui.BG)
        self.win.geometry(os.environ.get("JARVIS_SETTINGS_GEOMETRY", "1040x800"))
        self.win.minsize(860, 660)
        ui.init_styles(self.win)

        self.env = read_env()
        # выбор вкладки: из control_gui (parent._requested_tab) или env
        self._requested_tab = (getattr(parent, "_requested_tab", None)
                               or os.environ.get("JARVIS_SETTINGS_TAB"))
        # Мастер-пароль: без него личные графы заблокированы.
        #
        # ВАЖНО: при повреждённом vault.json панель обязана остаться
        # закрытой. Раньше стояло `not has_password()`, а has_password()
        # на битом файле возвращал False — то есть сломанный файл
        # разблокировал все ключи. Теперь has_password() на повреждении
        # возвращает True (пароль был), а has_password_broken()
        # сообщает UI, что показать нужно не «нет пароля», а «файл
        # повреждён, восстановите».
        self._vault_broken = has_password_broken()
        self._unlocked = (not has_password()) and not self._vault_broken
        self._sensitive_frames = []
        self._access_tab_frame = None
        self._access_sub = None          # под-ноутбук «Доступы и данные»
        self._ai_tab_frame = None        # под-вкладка AI / API KEYS
        # pid → состояние карточки провайдера (нужно тестам и рефрешу)
        self.ai_provider_rows = {}
        self._provider_combo = None

        self._build_header()
        self._build_tabs()
        self._build_statusbar()

        self.win.columnconfigure(0, weight=1)
        self.win.rowconfigure(2, weight=1)

        # Современные штрихи: появление окна, подсветка карточек, закрытие
        ui.install_motion_hover(self.win.winfo_toplevel())
        ui.fade_in_window(self.win, 220)
        self.win.protocol("WM_DELETE_WINDOW", self._graceful_close)

    def _graceful_close(self):
        ui.fade_out_window(self.win, 170, on_done=self.win.destroy)

    # -----------------------------------------------------------
    # Каркас
    # -----------------------------------------------------------
    def _build_header(self):
        head = ui.card(self.win, bg=ui.BG)
        head.configure(highlightthickness=0)
        head.grid(row=0, column=0, sticky="ew", padx=18, pady=(12, 4))
        head.columnconfigure(0, weight=1)

        left = tk.Frame(head, bg=ui.BG)
        left.grid(row=0, column=0, sticky="w")
        ui.label(left, "NOVA AI — Настройки", size=18, bold=True, bg=ui.BG,
                 color=ui.ACCENT).pack(anchor="w")
        ui.label(left, "Безопасность · Доступы · Разрешения · Параметры",
                 size=10, color=ui.DIM, bg=ui.BG).pack(anchor="w")

        self._lock_pill = tk.Label(
            head, text="", font=(ui.FONT, 10, "bold"), bg=ui.PANEL2,
            padx=12, pady=5,
        )
        self._lock_pill.grid(row=0, column=1, sticky="e", padx=(8, 8))

        ui.btn(head, "Открыть .env", self._open_env, kind="neutral",
               size=10, padx=12, pady=6).grid(row=0, column=2, sticky="e",
                                              rowspan=2)

    def _open_env(self):
        target = str(ENV_FILE)
        if not os.path.exists(target):
            messagebox.showwarning("NOVA AI", ".env пока не существует.")
            return
        subprocess.run(["open", "-e", target])

    def _build_statusbar(self):
        self.status = tk.Label(
            self.win, text="", font=(ui.FONT, 10), fg=ui.DIM, bg=ui.BG, anchor="w",
        )
        self.status.grid(row=3, column=0, sticky="ew", padx=18, pady=(4, 10))

    def _build_tabs(self):
        style = ttk.Style(self.win)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("J.TNotebook", background=ui.BG, borderwidth=0)
        style.configure(
            "J.TNotebook.Tab", background=ui.PANEL, foreground=ui.DIM,
            padding=(20, 10), font=(ui.FONT, 11, "bold"),
        )
        style.map(
            "J.TNotebook.Tab",
            background=[("selected", ui.PANEL2)],
            foreground=[("selected", ui.ACCENT)],
        )
        style.configure("J.Sub.TNotebook", background=ui.BG, borderwidth=0)
        style.configure(
            "J.Sub.TNotebook.Tab", background=ui.PANEL, foreground=ui.FAINT,
            padding=(16, 9), font=(ui.FONT, 11, "bold"),
        )
        style.map(
            "J.Sub.TNotebook.Tab",
            background=[("selected", ui.PANEL2)],
            foreground=[("selected", ui.ACCENT)],
        )
        style.configure(
            "J.TCombobox",
            fieldbackground=ui.FIELD,
            background=ui.FIELD,
            foreground="#dff4ff",
            arrowcolor=ui.ACCENT,
            bordercolor=ui.BORDER,
            lightcolor=ui.BORDER,
            darkcolor=ui.BORDER,
        )
        style.map(
            "J.TCombobox",
            fieldbackground=[("readonly", ui.FIELD)],
            foreground=[("readonly", "#dff4ff")],
        )

        self.tabs = ttk.Notebook(self.win, style="J.TNotebook")
        self.tabs.grid(row=2, column=0, sticky="nsew", padx=12, pady=(4, 4))

        f_sec = tk.Frame(self.tabs, bg=ui.BG)
        f_acc = tk.Frame(self.tabs, bg=ui.BG)
        f_perm = tk.Frame(self.tabs, bg=ui.BG)
        f_gen = tk.Frame(self.tabs, bg=ui.BG)

        self.tabs.add(f_sec, text="  Безопасность  ")
        self.tabs.add(f_acc, text="  Доступы и данные  ")
        self.tabs.add(f_perm, text="  Разрешения  ")
        self.tabs.add(f_gen, text="  Общие  ")

        self._build_security_tab(f_sec)
        self._build_access_tab(f_acc)
        self._build_permissions_tab(f_perm)
        self._build_general_tab(f_gen)

        if self._requested_tab == "permissions":
            self.tabs.select(self.tabs.tabs()[2])
        elif self._requested_tab == "security":
            self.tabs.select(self.tabs.tabs()[0])
        elif self._requested_tab == "ai":
            self.tabs.select(self.tabs.tabs()[1])
            if self._access_sub is not None and self._ai_tab_frame is not None:
                self._access_sub.select(self._ai_tab_frame)
        self._refresh_lock_status()

    # -----------------------------------------------------------
    # Вкладка «Безопасность» — мастер-пароль + передача проекта
    # -----------------------------------------------------------
    def _build_security_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        self._row_counter = 0
        self._security_tab_frame = parent

        c = ui.card(parent)
        c.grid(row=self._next_row(), column=0, sticky="ew", padx=16, pady=(16, 8))
        c.columnconfigure(0, weight=1)

        head = tk.Frame(c, bg=ui.PANEL2)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        _clabel(head, "Мастер-пароль на личные данные", size=15, bold=True,
                color=ui.ACCENT).grid(row=0, column=0, sticky="w")
        self._sec_status = tk.Label(
            head, text="", font=(ui.FONT, 10, "bold"), bg=ui.PANEL2, anchor="e")
        self._sec_status.grid(row=0, column=1, sticky="e", padx=(12, 0))

        _clabel(
            c,
            "Пароль закрывает графы с личными данными: Telegram-бот и аккаунт, "
            "ключи AI, озвучка. Пока пароль не введён — эти графы недоступны "
            "даже владельцу. Сам пароль хранится только PBKDF2-хэшем "
            "(data/vault.json), в открытом виде нигде не записывается.",
            size=10, color=ui.DIM, wraplength=940, justify="left",
        ).grid(row=1, column=0, sticky="ew", pady=(12, 4))

        ui.sep(c).grid(row=2, column=0, sticky="ew", pady=(6, 14))

        # Поле пароля + кнопка разблокировки (в одну строку, на всю ширину)
        pw_row = tk.Frame(c, bg=ui.PANEL2)
        pw_row.grid(row=3, column=0, sticky="ew")
        pw_row.columnconfigure(0, weight=1)
        self.pw_var = tk.StringVar()
        self.pw_entry = _entry(pw_row, self.pw_var, secret=True, font_size=14)
        self.pw_entry.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        self._unlock_btn = ui.btn(pw_row, "Разблокировать", self._try_unlock,
                                  kind="accent", size=12, width=16, padx=18, pady=10)
        self._unlock_btn.grid(row=0, column=1, sticky="e")

        # Кнопки управления паролем — ровно в ряд, одинаковой ширины
        btns = tk.Frame(c, bg=ui.PANEL2)
        btns.grid(row=4, column=0, sticky="w", pady=(16, 2))
        self._create_btn = ui.btn(btns, "Создать пароль", self._create_password,
                                  kind="green", size=12, width=17, padx=18, pady=10)
        self._change_btn = ui.btn(btns, "Сменить пароль", self._change_password,
                                  kind="neutral", size=12, width=17, padx=18, pady=10)
        self._delete_btn = ui.btn(btns, "Удалить пароль", self._delete_password,
                                  kind="danger", size=12, width=17, padx=18, pady=10)
        for i, b in enumerate((self._create_btn, self._change_btn, self._delete_btn)):
            b.grid(row=0, column=i, sticky="w", padx=(0 if i == 0 else 10, 0))

        self._spacer(parent, 10)
        self._build_delete_panel(parent)
        self._spacer(parent, 8)
        parent.rowconfigure(2, weight=1)
        self._refresh_lock_status()

    def _refresh_lock_status(self):
        locked = not self._unlocked
        if hasattr(self, "_sec_status"):
            ui.stop_pulse(self._sec_status)
        if self._vault_broken:
            # Повреждённый vault.json — третье состояние, не «нет
            # пароля»: проверить старый пароль нечем, но и открывать
            # ключи нельзя. Показываем ровно это и предлагаем путь.
            text = ("⚠️ Файл мастер-пароля повреждён — данные заблокированы. "
                    "Восстановите data/vault.json или задайте пароль заново.")
            color, pill = "#ffb3ae", "⚠️ пароль: файл повреждён"
        elif has_password():
            if locked:
                text, color = "🔒 Пароль установлен — данные заблокированы", "#ffb3ae"
                pill = "🔒 пароль: установлен"
            else:
                text, color = "🔓 Пароль установлен — данные разблокированы", ui.GREEN
                pill = "🔒 пароль: установлен"
        else:
            text, color = "Пароль не установлен — личные данные открыты", ui.FAINT
            pill = "пароль: не установлен"
        if hasattr(self, "_sec_status"):
            self._sec_status.configure(text=text, fg=color)
            if locked and has_password():
                # мягкое «дыхание» тревоги, пока графы заперты
                ui.start_pulse(self._sec_status, "#ffd9d5", "#b3453f",
                               period_ms=1700)
        if hasattr(self, "_lock_pill"):
            self._lock_pill.configure(text=pill)
        # Кнопки и поле разблокировки
        has_pw = has_password()
        # При повреждении создание пароля заново — единственный путь
        # разблокировать: старый проверить уже нечем.
        can_create = (not has_pw) or self._vault_broken
        self._create_btn.configure(state="normal" if can_create else "disabled")
        self._change_btn.configure(
            state="normal" if (has_pw and not self._vault_broken) else "disabled"
        )
        self._delete_btn.configure(
            state="normal" if (has_pw and not self._vault_broken) else "disabled"
        )
        _set_enabled(self.pw_entry, locked and not self._vault_broken)
        # При повреждении поле пароля бессмысленно: разблокировать
        # нечем. Кнопка тоже — но подсказка объясняет, что делать.
        self._unlock_btn.configure(
            state="normal" if (locked and not self._vault_broken) else "disabled"
        )

    def _try_unlock(self):
        value = self.pw_var.get()
        if not verify_password(value):
            self.pw_var.set("")
            messagebox.showerror("Неверный пароль", "Мастер-пароль не подходит.")
            return
        self.pw_var.set("")
        self._unlocked = True
        self._refresh_lock_status()
        if self._access_tab_frame is not None:
            self._reload_access_tab(self._access_tab_frame)
        self._set_status("Пароль принят — личные данные разблокированы", ui.GREEN)

    def _create_password(self):
        recovering = self._vault_broken
        title = "Восстановить пароль" if recovering else "Создать пароль"
        intro = (
            "Файл мастер-пароля повреждён, старый пароль проверить нельзя. "
            "Задайте новый — доступ к данным вернётся."
            if recovering else
            "Придумайте мастер-пароль — им будут закрыты графы с личными данными:"
        )
        values = self._ask_fields(
            title,
            intro,
            [("Пароль (минимум 8 символов)", True), ("Повторите пароль", True)],
        )
        if values is None:
            return
        first, second = values
        if not first or len(first) < MIN_PASSWORD_LENGTH:
            messagebox.showwarning(
                title, f"Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов."
            )
            return
        if first != second:
            messagebox.showwarning(title, "Пароли не совпадают.")
            return
        if recovering:
            # Явный сброс старого хэша: пользователь подтвердил, что
            # пароль утрачен. Старый проверить физически нечем.
            ok = reset_password(first)
        else:
            ok = set_password(first)
        if ok:
            self._vault_broken = False
            # Пароль только что создал владелец — сеанс остаётся разблокированным
            self._unlocked = True
            self._refresh_lock_status()
            if self._access_tab_frame is not None:
                self._reload_access_tab(self._access_tab_frame)
            self._set_status("Мастер-пароль установлен. Графы личных данных закрыты для всех, у кого нет пароля.", ui.GREEN)

    def _change_password(self):
        values = self._ask_fields(
            "Сменить пароль",
            "Смена мастер-пароля (потребуется старый):",
            [("Старый пароль", True),
             (f"Новый пароль (мин. {MIN_PASSWORD_LENGTH})", True),
             ("Повторите новый", True)],
        )
        if values is None:
            return
        old, new, again = values
        if not new or len(new) < MIN_PASSWORD_LENGTH:
            messagebox.showwarning(
                "Сменить пароль",
                f"Новый пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов.",
            )
            return
        if new != again:
            messagebox.showwarning("Сменить пароль", "Новые пароли не совпадают.")
            return
        if change_password(old, new):
            self._set_status("Мастер-пароль изменён.", ui.GREEN)
        else:
            messagebox.showerror("Сменить пароль", "Старый пароль не подходит.")

    def _delete_password(self):
        values = self._ask_fields(
            "Удалить пароль",
            "Удалить мастер-пароль? Графы личных данных снова станут открытыми.\n"
            "Для удаления введите текущий пароль:",
            [("Текущий пароль", True)],
        )
        if values is None:
            return
        if delete_password(values[0]):
            self._unlocked = True
            self._refresh_lock_status()
            if self._access_tab_frame is not None:
                self._reload_access_tab(self._access_tab_frame)
            self._set_status("Мастер-пароль удалён — личные данные открыты.", ui.ACCENT)
        else:
            messagebox.showerror("Удалить пароль", "Пароль не подходит — удалить не удалось.")

    def _ask_fields(self, title, message, fields):
        """Диалог с полями (секретные/обычные). Возвращает список значений или None."""
        dlg = tk.Toplevel(self.win)
        dlg.title(title)
        dlg.configure(bg=ui.BG)
        dlg.resizable(False, False)
        dlg.columnconfigure(0, weight=1)
        if message:
            ui.label(dlg, message, size=10, color=ui.DIM, bg=ui.BG,
                     wraplength=400, justify="left").grid(
                row=0, column=0, sticky="w", padx=16, pady=(12, 6))
        variables = []
        for i, (label, secret) in enumerate(fields):
            ui.label(dlg, label, size=10, bold=True, bg=ui.BG).grid(
                row=1 + i * 2, column=0, sticky="w", padx=16, pady=(6, 0))
            var = tk.StringVar()
            ent = _entry(dlg, var, secret, font_size=13)
            ent.grid(row=2 + i * 2, column=0, sticky="ew", padx=16, pady=(2, 6))
            variables.append(var)
        row_btns = tk.Frame(dlg, bg=ui.BG)
        row_btns.grid(row=3 + len(fields) * 2, column=0, sticky="e", padx=16, pady=(8, 16))
        result = {}
        def ok():
            result["values"] = [v.get() for v in variables]
            dlg.destroy()
        def cancel():
            result["values"] = None
            dlg.destroy()
        ui.btn(row_btns, "Отмена", cancel, kind="neutral", size=10, width=10).pack(side="left", padx=2)
        ui.btn(row_btns, "OK", ok, kind="accent", size=10, width=10).pack(side="left", padx=2)
        dlg.update_idletasks()
        dlg.geometry(f"+{self.win.winfo_rootx() + 60}+{self.win.winfo_rooty() + 60}")
        dlg.grab_set()
        children = [w for w in dlg.winfo_children() if isinstance(w, tk.Entry)]
        if children:
            children[0].focus_set()
            children[0].bind("<Return>", lambda e: ok())
        self.win.wait_window(dlg)
        return result.get("values")

    def _build_delete_panel(self, body):
        c = ui.card(body)
        c.configure(bg=ui.PANEL, highlightbackground=ui.RED_DK)
        c.grid(row=self._next_row(), column=0, sticky="ew", padx=16, pady=8)
        c.columnconfigure(0, weight=1)

        _clabel(c, "Для передачи проекта другому человеку", size=13, bold=True).grid(
            row=0, column=0, sticky="w")
        _clabel(
            c,
            "Нажмите кнопку — из файла настроек будут удалены ВСЕ личные ключи "
            "(AI API, ElevenLabs, Telegram API и бот). NOVA AI останется рабочим, "
            "но вернётся в режим эвристики, пока вы не вставите ключи заново.",
            size=10, color=ui.DIM, wraplength=940, justify="left",
        ).grid(row=1, column=0, sticky="ew", pady=(6, 12))

        def wipe():
            if not messagebox.askyesno(
                "Удалить все личные ключи",
                "Удалить ВСЕ личные ключи (AI API, ElevenLabs, Telegram API и бот) из .env?\n\n"
                "Это нужно делать ПЕРЕД передачей проекта другому человеку.\n"
                "Продолжить?",
            ):
                return
            deleted = delete_all_sensitive()
            self.env = read_env()
            if deleted:
                messagebox.showinfo(
                    "Готово",
                    "Удалены ключи: " + ", ".join(deleted) + "\n"
                    "Проект теперь можно безопасно передавать.",
                )
                self.win.destroy()
            else:
                messagebox.showwarning("Готово", "Личные ключи не найдены в .env.")

        ui.btn(c, "\U0001f6e1  Удалить все личные ключи", wipe, kind="danger",
               size=13, padx=20, pady=12).grid(row=2, column=0, sticky="w")

    # -----------------------------------------------------------
    # Вкладка «Доступы и данные» — подпункты доступа + удаление
    # -----------------------------------------------------------
    def _build_access_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)
        self._access_tab_frame = parent
        self._sensitive_frames = []

        head = tk.Frame(parent, bg=ui.BG)
        head.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 4))
        head.columnconfigure(0, weight=1)
        ui.label(head, "Доступы к Telegram, AI и озвучке", size=15, bold=True,
                 color=ui.ACCENT, bg=ui.BG).grid(row=0, column=0, sticky="w")
        ui.label(
            head,
            "Подпункты доступа к Telegram, графы подключения личных данных и их "
            "удаление. Все поля блокируются мастер-паролем.",
            size=10, color=ui.DIM, bg=ui.BG,
        ).grid(row=1, column=0, sticky="w", pady=(3, 0))

        if not self._unlocked:
            banner = tk.Frame(parent, bg=ui.PANEL,
                              highlightbackground=ui.RED_DK, highlightthickness=1)
            banner.grid(row=1, column=0, sticky="ew", padx=18, pady=(8, 2))
            banner.columnconfigure(0, weight=1)
            tk.Label(
                banner,
                text="🔒 Доступы к личным данным заблокированы мастер-паролем. "
                     "Графы с ключами скрыты, пока вы не введёте пароль.",
                font=(ui.FONT, 10, "bold"), fg="#ffb3ae", bg=ui.PANEL, anchor="w",
            ).grid(row=0, column=0, sticky="w", padx=12, pady=(8, 2))

            def go_security():
                self.tabs.select(self.tabs.tabs()[0])
            ui.btn(banner, "Разблокировать", go_security, kind="danger", size=10,
                   width=17, padx=16, pady=8).grid(row=0, column=1, sticky="e", padx=12, pady=6)

        sub = ttk.Notebook(parent, style="J.Sub.TNotebook")
        sub.grid(row=2, column=0, sticky="nsew", padx=14, pady=(8, 6))
        self._access_sub = sub

        f_bot = tk.Frame(sub, bg=ui.BG)
        f_acc = tk.Frame(sub, bg=ui.BG)
        f_ai = tk.Frame(sub, bg=ui.BG)
        f_tts = tk.Frame(sub, bg=ui.BG)
        self._ai_tab_frame = f_ai

        sub.add(f_bot, text="  Telegram-бот  ")
        sub.add(f_acc, text="  Telegram-аккаунт  ")
        sub.add(f_ai, text="  AI / API KEYS  ")
        sub.add(f_tts, text="  Озвучка (Fish / ElevenLabs)  ")

        self._build_bot_tab(f_bot)
        self._build_account_tab(f_acc)
        self._build_ai_tab(f_ai)
        self._build_tts_tab(f_tts)

        self._apply_lock()

    def _reload_access_tab(self, parent):
        for widget in parent.winfo_children():
            widget.destroy()
        self._build_access_tab(parent)

    def _apply_lock(self):
        enabled = self._unlocked
        for sf in self._sensitive_frames:
            _set_enabled(sf, enabled)

    @staticmethod
    def _tab_fill(frame):
        """Под-вкладка занимает всю доступную область (веса grid)."""
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

    @staticmethod
    def _sub_header(body, text):
        return _clabel(body, text, size=13, bold=True, color=ui.ACCENT)

    # --- Подраздел: Telegram-бот ---------------------------------
    def _build_bot_tab(self, parent):
        self._tab_fill(parent)
        scroll, body = self._scroll_area(parent)
        scroll.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        self._sensitive_frames.append(scroll)

        # Подпункт 1: доступ
        c = ui.card(body)
        c.grid(row=self._next_row(), column=0, sticky="ew", padx=8, pady=4)
        c.columnconfigure(0, weight=1)
        _clabel(c, "Доступ к боту", size=12, bold=True).grid(row=0, column=0, sticky="w")
        self.bot_enabled_var = tk.BooleanVar(
            value=_env_bool(self.env, "TG_BOT_ENABLED"))
        ui.check(
            c, "Разрешить NOVA AI подключаться к Telegram-боту и слушать входящие",
            self.bot_enabled_var, bold=True,
        ).grid(row=1, column=0, sticky="w", pady=(6, 2))
        _clabel(
            c,
            "Бот создаётся в @BotFather и отвечает от вашего имени, когда вы заняты. "
            "Выключите — бот перестанет запускаться.",
            size=9, color=ui.DIM,
        ).grid(row=2, column=0, sticky="w")

        def save_access():
            write_env({"TG_BOT_ENABLED": "true" if self.bot_enabled_var.get() else "false"})
            self.env = read_env()
            self._set_status("Доступ к боту сохранён (применится после перезапуска NOVA AI)", ui.GREEN)
        ui.btn(c, "Сохранить доступ", save_access, kind="accent", size=10, width=18).grid(
            row=3, column=0, sticky="w", pady=(8, 2))

        # Подпункт 2: подключение
        self._sub_header(body, "Подключение бота").grid(
            row=self._next_row(), column=0, sticky="ew", padx=10, pady=(16, 6))
        self._add_key_row(body, "TG_BOT_TOKEN", "Токен бота (@BotFather)",
                          "Команда /newbot в @BotFather → скопируйте токен. "
                          "Это секрет: третьим лицам не передавать.", True)
        configured = bool(
            self.env.get("TG_BOT_TOKEN")
            and self.env.get("TELEGRAM_API_ID", "0").strip() not in ("", "0")
            and self.env.get("TELEGRAM_API_HASH")
        )
        ui.label(
            body,
            ("\u2705 Бот настроен: NOVA AI может запустить его при включённом доступе." if configured
             else "\u23f8 Бот пока не настроен: вставьте токен (и проверьте Telegram API "
                  "в подразделе ниже)."),
            size=10, color=ui.GREEN if configured else ui.DIM, bg=ui.BG,
        ).grid(row=self._next_row(), column=0, sticky="w", padx=10, pady=(6, 2))

        # Подпункт 3: права на действия и диалоги
        c = ui.card(body)
        c.grid(row=self._next_row(), column=0, sticky="ew", padx=8, pady=6)
        c.columnconfigure(0, weight=1)
        _clabel(c, "Права бота — на какие действия и диалоги", size=12, bold=True).grid(
            row=0, column=0, sticky="w")
        self.bot_read_var = tk.BooleanVar(value=_env_bool(self.env, "TG_BOT_READ", True))
        self.bot_write_var = tk.BooleanVar(value=_env_bool(self.env, "TG_BOT_WRITE", True))
        self.bot_autoreply_var = tk.BooleanVar(value=_env_bool(self.env, "TG_BOT_AUTOREPLY", True))
        ui.check(c, "Читать входящие сообщения", self.bot_read_var, bold=True).grid(
            row=1, column=0, sticky="w", pady=(6, 0))
        ui.check(c, "Писать от моего имени в разрешённых чатах", self.bot_write_var).grid(
            row=2, column=0, sticky="w")
        ui.check(c, "Отвечать автоматически, когда я занят (автоответы)", self.bot_autoreply_var).grid(
            row=3, column=0, sticky="w")

        def save_rights():
            write_env({
                "TG_BOT_READ": "true" if self.bot_read_var.get() else "false",
                "TG_BOT_WRITE": "true" if self.bot_write_var.get() else "false",
                "TG_BOT_AUTOREPLY": "true" if self.bot_autoreply_var.get() else "false",
            })
            self.env = read_env()
            self._set_status("Права бота сохранены (применятся после перезапуска NOVA AI)", ui.GREEN)
        ui.btn(c, "Сохранить права", save_rights, kind="accent", size=10, width=18).grid(
            row=4, column=0, sticky="w", pady=(8, 2))

        # Подпункт 4: параметры ответов
        self._sub_header(body, "Параметры ответов").grid(
            row=self._next_row(), column=0, sticky="ew", padx=10, pady=(16, 6))
        self._add_key_row(body, "TG_BOT_OWNER_ID", "Ваш Telegram user id",
                          "Бот игнорирует ваши собственные сообщения. Узнать id: @userinfobot", False)
        self._add_key_row(body, "TG_BOT_DIALOGS", "Разрешённые чаты (id через запятую)",
                          "Пусто = отвечать во всех личных чатах. Можно ограничить, "
                          "например: 123456789, 987654321", False)
        self._add_key_row(body, "TG_BOT_GREETING", "Приветствие бота",
                          "{owner} подставляется автоматически. По умолчанию: "
                          "«Привет! Я Jar'vis, AI-ассистент {owner}…»", False)

        # Подпункт 5: отвязать бота
        c = ui.card(body)
        c.configure(bg=ui.PANEL, highlightbackground=ui.RED_DK)
        c.grid(row=self._next_row(), column=0, sticky="ew", padx=8, pady=8)
        c.columnconfigure(0, weight=1)
        _clabel(c, "Отвязать Telegram-бот", size=12, bold=True).grid(row=0, column=0, sticky="w")
        _clabel(
            c,
            "Удалит токен из .env, сотрёт сессию и историю ответов бота. "
            "Бот перестанет запускаться.",
            size=9, color=ui.DIM,
        ).grid(row=1, column=0, sticky="w", pady=(6, 10))

        def disconnect_bot():
            if not messagebox.askyesno(
                "Отвязать Telegram-бот",
                "Удалить токен бота, сессию и историю ответов?\n"
                "Для повторного подключения создайте нового бота в @BotFather.",
            ):
                return
            delete_keys(["TG_BOT_TOKEN"])
            removed = []
            for pattern in (BOT_SESSION_PREFIX + "*", BOT_STATE_FILE):
                for f in glob.glob(pattern):
                    try:
                        os.remove(f)
                        removed.append(os.path.basename(f))
                    except OSError:
                        pass
            self.env = read_env()
            suffix = (": " + ", ".join(removed)) if removed else ""
            self._set_status("Telegram-бот отвязан" + suffix, ui.ACCENT)
            self._reload_access_tab(self._access_tab_frame)

        ui.btn(c, "Отвязать бота", disconnect_bot, kind="danger", size=10, padx=16, pady=8).grid(
            row=2, column=0, sticky="w")

    # --- Подраздел: Telegram-аккаунт ------------------------------
    def _build_account_tab(self, parent):
        self._tab_fill(parent)
        scroll, body = self._scroll_area(parent)
        scroll.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        self._sensitive_frames.append(scroll)

        self._sub_header(body, "Подключение аккаунта (Telegram API)").grid(
            row=self._next_row(), column=0, sticky="ew", padx=10, pady=(14, 6))
        for key, title, hint, secret in ACCOUNT_KEYS:
            self._add_key_row(body, key, title, hint, secret)
        self._spacer(body, 10)
        _clabel(
            body,
            "Аккаунт-доступ нужен для проверки входящих сообщений монитора NOVA AI "
            "и является основой для работы бота. Безопасно получить: my.telegram.org "
            "→ API development tools.",
            size=10, color=ui.DIM, wraplength=920, justify="left",
        ).grid(row=self._next_row(), column=0, sticky="ew", padx=10, pady=(4, 2))
        self._spacer(body, 10)

    # --- Подраздел: AI (Groq) -------------------------------------
    def _build_ai_tab(self, parent):
        self._tab_fill(parent)
        scroll, body = self._scroll_area(parent)
        scroll.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        self._sensitive_frames.append(scroll)

        # --- Провайдер AI: «неограниченно и онлайн» ---
        c = ui.card(body)
        c.grid(row=self._next_row(), column=0, sticky="ew", padx=8, pady=4)
        c.columnconfigure(0, weight=1)
        _clabel(c, "Провайдер AI — «неограниченно и онлайн»", size=13, bold=True).grid(
            row=0, column=0, sticky="w")
        _clabel(
            c,
            "NOVA AI перебирает провайдеров цепочкой: у кого кончилась квота или "
            "ошибка — запрос уходит следующему. При AI_PROVIDER=auto активны все, "
            "у кого вставлен ключ; локальная Ollama подключается автоматически "
            "и работает безлимитно, даже без интернета.",
            size=10, color=ui.DIM, wraplength=920, justify="left",
        ).grid(row=1, column=0, sticky="ew", pady=(6, 10))

        row = tk.Frame(c, bg=ui.PANEL2)
        row.grid(row=2, column=0, sticky="ew")
        row.columnconfigure(0, weight=1)
        _clabel(row, "AI_PROVIDER", size=10, bold=True).grid(row=0, column=0, sticky="w")
        self.provider_var = tk.StringVar(value=self.env.get("AI_PROVIDER", "auto"))
        combo = ttk.Combobox(
            row, textvariable=self.provider_var, state="readonly",
            style="J.TCombobox",
            values=["auto", *list(PROVIDER_ORDER), "custom"],
            font=(ui.FONT, 11), width=50,
        )
        combo.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self._provider_combo = combo

        def save_provider():
            write_env({"AI_PROVIDER": self.provider_var.get()})
            self.env = read_env()
            self._set_status("Провайдер сохранён: " + provider_summary(self.env), ui.GREEN)
            self._refresh_ai_markup()
            refresh_status()
        ui.btn(row, "Сохранить", save_provider, kind="accent", size=10, width=12).grid(
            row=1, column=1, sticky="e", padx=(14, 0), pady=(4, 0))

        self.provider_status = tk.Label(
            c, text="", font=(ui.FONT, 10), fg=ui.GREEN, bg=ui.PANEL2, anchor="w")
        self.provider_status.grid(row=3, column=0, sticky="ew", pady=(10, 0))

        self.cooldown_status = tk.Label(
            c, text="", font=(ui.FONT, 10), fg=ui.YELLOW, bg=ui.PANEL2,
            anchor="w", justify="left")
        self.cooldown_status.grid(row=4, column=0, sticky="ew", pady=(4, 0))

        def refresh_status():
            text = provider_summary(self.env)
            color = ui.GREEN if not text.startswith("Нет") else ui.DIM
            self.provider_status.configure(text="Активно сейчас: " + text, fg=color)

            # Провайдеры на кулдауне невидимы: они просто выпадают из
            # цепочки, и снаружи выглядит так, будто NOVA AI перестал
            # думать. Показываем, кто лежит и на сколько.
            cooling = cooldown_report()
            if cooling:
                names = ", ".join(
                    f"{item['name']} ({_mmss(item['left'])})"
                    for item in cooling[:4]
                )
                if len(cooling) > 4:
                    names += f", ещё {len(cooling) - 4}"
                self.cooldown_status.configure(
                    text="На паузе после ошибки: " + names)
            else:
                self.cooldown_status.configure(text="")

        def clear_cooling():
            cleared = clear_cooldown()
            if cleared:
                self._set_status(
                    "Кулдаун снят: " + ", ".join(
                        PROVIDERS.get(p, {}).get("name", p) for p in cleared
                    ) + ". Следующий запрос пойдёт без ожидания.",
                    ui.GREEN,
                )
            else:
                self._set_status("Никто не на паузе — сбрасывать нечего.", ui.DIM)
            refresh_status()

        refresh_status()
        buttons = tk.Frame(c, bg=ui.PANEL2)
        buttons.grid(row=5, column=0, sticky="w", pady=(8, 0))
        ui.btn(buttons, "Обновить статус", refresh_status, kind="neutral", size=9,
               width=16).grid(row=0, column=0, sticky="w")
        # Кулдаун на auth-ошибку живёт час. Если ключ в этот час поменяли,
        # провайдер молчал до конца часа и «чинить» было нечем — кнопка
        # закрывает эту дыру.
        ui.btn(buttons, "Сбросить паузу", clear_cooling, kind="accent", size=9,
               width=16).grid(row=0, column=1, sticky="w", padx=(10, 0))

        # --- AI PROVIDERS: карточки провайдеров со статусом и Test Connection ---
        self._sub_header(body, "AI PROVIDERS — ключи и проверка связи").grid(
            row=self._next_row(), column=0, sticky="ew", padx=10, pady=(16, 6))
        for pid in PROVIDER_ORDER:
            p = PROVIDERS[pid]
            if p.get("key_env"):
                self._add_provider_card(body, pid)
            else:
                self._add_ollama_row(body, p)

        # --- Свой / legacy эндпоинт ---
        self._sub_header(body, "Свой OpenAI-совместимый и legacy").grid(
            row=self._next_row(), column=0, sticky="ew", padx=10, pady=(16, 6))
        for key, title, hint, secret in AI_KEYS:
            # AI_API_KEY (legacy-ключ Groq) и AI_MODEL влияют на карточки
            # провайдеров — обновляем статус и модель сразу после сохранения.
            hook = self._refresh_ai_markup if key in ("AI_API_KEY", "AI_MODEL") else None
            self._add_key_row(body, key, title, hint, secret, on_saved=hook)

        # --- JSON-режим ---
        c = ui.card(body)
        c.grid(row=self._next_row(), column=0, sticky="ew", padx=8, pady=6)
        c.columnconfigure(0, weight=1)
        self.ai_json_var = tk.BooleanVar(value=_env_bool(self.env, "AI_JSON_MODE", True))
        _clabel(c, "AI_JSON_MODE — строгий JSON-режим", size=11, bold=True).grid(
            row=0, column=0, sticky="w")
        ui.check(
            c, "Использовать response_format=json_object (Groq — да, Gemini/Ollama — нет)",
            self.ai_json_var,
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))

        def save_json():
            write_env({"AI_JSON_MODE": "true" if self.ai_json_var.get() else "false"})
            self.env = read_env()
            self._set_status("AI_JSON_MODE сохранён", ui.GREEN)
        ui.btn(c, "Сохранить", save_json, kind="accent", size=10, width=14).grid(
            row=2, column=0, sticky="w", pady=(8, 2))
        self._spacer(body, 10)

    def _add_ollama_row(self, body, provider):
        """Карточка локальной Ollama: без ключа, безлимитно, работает офлайн."""
        c = ui.card(body)
        c.grid(row=self._next_row(), column=0, sticky="ew", padx=8, pady=4)
        c.columnconfigure(0, weight=1)

        head = tk.Frame(c, bg=ui.PANEL2)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        _clabel(head, provider["name"], size=11, bold=True).grid(row=0, column=0, sticky="w")
        ui.btn(head, "Скачать →",
               lambda: webbrowser.open("https://ollama.com/download"),
               kind="neutral", size=9, width=10).grid(row=0, column=1, sticky="e")

        self.ollama_status = tk.Label(
            c, text="", font=(ui.FONT, 10), bg=ui.PANEL2, anchor="w")
        self.ollama_status.grid(row=1, column=0, sticky="w", pady=(6, 2))

        def check_ollama():
            up = ollama_up()
            text = (
                "● работает — NOVA AI использует его безлимитно"
                if up
                else "○ не обнаружен. Установите Ollama и выполните:\n"
                     "    ollama pull qwen2.5:7b  (или другой язык-модель)"
            )
            self.ollama_status.configure(
                text=text, fg=ui.GREEN if up else ui.DIM)
            return up
        check_ollama()

        _clabel(c, provider["free"], size=9, color=ui.DIM).grid(
            row=2, column=0, sticky="w", pady=(2, 0))
        ui.btn(c, "Проверить статус", check_ollama, kind="neutral", size=9,
               width=16).grid(row=3, column=0, sticky="w", pady=(8, 0))

    # --- Подраздел: Озвучка (ElevenLabs) ---------------------------
    def _build_tts_tab(self, parent):
        self._tab_fill(parent)
        scroll, body = self._scroll_area(parent)
        scroll.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        self._sensitive_frames.append(scroll)

        self._sub_header(body, "Озвучка голосом (Fish.audio NOVA AI → ElevenLabs)").grid(
            row=self._next_row(), column=0, sticky="ew", padx=10, pady=(14, 6))
        for key, title, hint, secret in TTS_KEYS:
            self._add_key_row(body, key, title, hint, secret)
        self._spacer(body, 10)

    # -----------------------------------------------------------
    # Ключи: строка с полем, сохранением и удалением
    # -----------------------------------------------------------
    def _add_key_row(self, body, key, title, hint, secret, link=None, on_saved=None):
        c = ui.card(body)
        c.grid(row=self._next_row(), column=0, sticky="nsew", padx=8, pady=4)
        c.columnconfigure(1, weight=1)
        c.columnconfigure(0, weight=1)

        _clabel(c, title, size=12, bold=True).grid(
            row=0, column=0, columnspan=2, sticky="w")

        var = tk.StringVar(value=self.env.get(key, ""))
        entry = _entry(c, var, secret, font_size=13)
        entry.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 3))

        if hint:
            _clabel(c, hint, size=9, color=ui.DIM, wraplength=900, justify="left").grid(
                row=2, column=0, columnspan=2, sticky="w")

        status_lbl = tk.Label(
            c, text="", font=(ui.FONT, 9), fg=ui.GREEN, bg=ui.PANEL2, anchor="w")
        status_lbl.grid(row=3, column=0, sticky="w", pady=(4, 0), columnspan=2)

        btns = tk.Frame(c, bg=ui.PANEL2)
        btns.grid(row=4, column=0, columnspan=2, sticky="e", pady=(2, 0))

        link_btn = None
        if link:
            def _open(url=link):
                target = url if url.startswith("http") else "https://" + url
                webbrowser.open(target)
            link_btn = ui.btn(btns, "Получить ключ →", _open,
                              kind="neutral", size=9, width=16, padx=14, pady=7)
        show_btn = None
        if secret:
            show_btn = ui.btn(btns, "Показать", None, kind="neutral", size=10, width=9)
        save_btn = ui.btn(btns, "Сохранить", None, kind="accent", size=10, width=10)
        del_btn = ui.btn(btns, "Удалить", None, kind="danger", size=10, width=9)
        for w in (link_btn, show_btn, save_btn, del_btn):
            if w is not None:
                w.pack(side="left", padx=2)

        state = {"shown": False}

        def toggle_show():
            state["shown"] = not state["shown"]
            entry.configure(show="" if state["shown"] else "\u2022")
            show_btn.configure(text="Скрыть" if state["shown"] else "Показать")

        def save():
            value = var.get().strip()
            write_env({key: value})
            self.env = read_env()
            status_lbl.configure(
                text="\u2713 сохранено" if value else "очищено",
                fg=ui.GREEN if value else ui.DIM)
            if on_saved:
                on_saved()

        def delete():
            if secret and not messagebox.askyesno(
                "Удалить ключ",
                f"Удалить «{title}» из файла настроек?\n\n"
                "Эта функция полезна при передаче проекта другому человеку.",
            ):
                return
            deleted = delete_keys([key])
            var.set("")
            status_lbl.configure(
                text="\u2713 удалено" if deleted else "не найден в .env",
                fg=ui.GREEN if deleted else ui.DIM)

        if show_btn:
            show_btn.configure(command=toggle_show)
        save_btn.configure(command=save)
        del_btn.configure(command=delete)

    # -----------------------------------------------------------
    # AI PROVIDERS: карточки провайдеров
    # -----------------------------------------------------------
    def _add_provider_card(self, body, pid):
        """Карточка провайдера: статус, ключ (маска), модель и кнопка
        «Test Connection», которая ходит в API штатным runtime
        (AIBrain._post_provider → существующий provider registry)."""
        p = PROVIDERS[pid]
        key_env = p["key_env"]
        c = ui.card(body)
        c.grid(row=self._next_row(), column=0, sticky="nsew", padx=8, pady=4)
        c.columnconfigure(0, weight=1)

        head = tk.Frame(c, bg=ui.PANEL2)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(3, weight=1)

        status_dot = tk.Label(head, text="○", font=(ui.FONT, 10, "bold"),
                              fg=ui.DIM, bg=ui.PANEL2, anchor="w")
        status_dot.grid(row=0, column=0, sticky="w")
        status_lbl = tk.Label(head, text="Not configured",
                              font=(ui.FONT, 11, "bold"), fg=ui.DIM,
                              bg=ui.PANEL2, anchor="w")
        status_lbl.grid(row=0, column=1, sticky="w", padx=(6, 0))
        _clabel(head, p["name"], size=12, bold=True).grid(
            row=0, column=2, sticky="w", padx=(18, 0))
        model_lbl = _clabel(head, "Model: —", size=9, color=ui.DIM)
        model_lbl.grid(row=0, column=3, sticky="e")

        var = tk.StringVar(value=self.env.get(key_env, ""))
        entry = _entry(c, var, secret=True, font_size=13)
        entry.grid(row=1, column=0, sticky="ew", pady=(6, 3))

        hint = p["free"]
        if p.get("signup"):
            hint += f"  Получить: {p['signup']}"
        _clabel(c, hint, size=9, color=ui.DIM, wraplength=900,
                justify="left").grid(row=2, column=0, sticky="w")

        test_lbl = tk.Label(c, text="", font=(ui.FONT, 9), fg=ui.DIM,
                            bg=ui.PANEL2, anchor="w", justify="left",
                            wraplength=860)
        test_lbl.grid(row=3, column=0, sticky="w", pady=(4, 0))

        btns = tk.Frame(c, bg=ui.PANEL2)
        btns.grid(row=4, column=0, sticky="e", pady=(2, 0))

        signup_btn = None
        if p.get("signup"):
            def _open(url=p["signup"]):
                target = url if url.startswith("http") else "https://" + url
                webbrowser.open(target)
            signup_btn = ui.btn(btns, "Получить ключ →", _open,
                                kind="neutral", size=9, width=16, padx=14, pady=7)
        show_btn = ui.btn(btns, "Показать", None, kind="neutral", size=10, width=9)
        save_btn = ui.btn(btns, "Сохранить", None, kind="accent", size=10, width=10)
        del_btn = ui.btn(btns, "Удалить", None, kind="danger", size=10, width=9)
        test_btn = ui.btn(btns, "Test Connection", None, kind="accent",
                          size=10, width=16, padx=16, pady=8)
        for w in (signup_btn, show_btn, save_btn, del_btn, test_btn):
            if w is not None:
                w.pack(side="left", padx=2)

        row = {
            "pid": pid,
            "key": key_env,
            "var": var,
            "entry": entry,
            "status_lbl": status_lbl,
            "status_dot": status_dot,
            "model_lbl": model_lbl,
            "test_lbl": test_lbl,
            "save_btn": save_btn,
            "del_btn": del_btn,
            "test_btn": test_btn,
            "show_btn": show_btn,
            "shown": False,
            "test_result": None,
            "test_running": False,
            "connected": False,
            "error": False,
        }

        def toggle_show():
            row["shown"] = not row["shown"]
            entry.configure(show="" if row["shown"] else "\u2022")
            show_btn.configure(text="Скрыть" if row["shown"] else "Показать")

        def _after_change():
            # Сохранение/удаление требуют нового теста связи.
            row["connected"] = False
            row["error"] = False
            row["test_result"] = None
            test_lbl.configure(text="", fg=ui.DIM)
            self._refresh_provider_card(row)

        def save():
            value = var.get().strip()
            write_env({key_env: value})
            self.env = read_env()
            if value:
                self.env[key_env] = value
            else:
                self.env.pop(key_env, None)  # пустое значение = удаление строки
            _after_change()
            self._set_status(
                f"{p['name']}: ключ сохранён" if value else f"{p['name']}: ключ очищен",
                ui.GREEN if value else ui.DIM)

        def delete():
            if not messagebox.askyesno(
                "Удалить ключ",
                f"Удалить API-ключ {key_env} ({p['name']}) из файла настроек?",
            ):
                return
            deleted = delete_keys([key_env])
            var.set("")
            self.env.pop(key_env, None)
            _after_change()
            self._set_status(
                f"{p['name']}: ключ удалён" if deleted else "ключ не найден в .env",
                ui.GREEN if deleted else ui.DIM)

        show_btn.configure(command=toggle_show)
        save_btn.configure(command=save)
        del_btn.configure(command=delete)
        test_btn.configure(command=lambda: self._test_connection(pid, row))

        self.ai_provider_rows[pid] = row
        self._refresh_provider_card(row)

    def _resolve_cfg(self, pid):
        """Актуальный конфиг провайдера через штатный registry.

        Используется pinned-режим providers_for (AI_PROVIDER=<pid>), а ключи
        для резолва берутся из self.env — он зеркалит .env и уже учитывает
        только что сохранённые значения. None — ключа нет (или Ollama лежит).
        """
        p = PROVIDERS.get(pid)
        if not p:
            return None
        resolve_env = {"AI_PROVIDER": pid}
        for name in (p.get("key_env"), p.get("fallback_key_env")):
            if name:
                resolve_env[name] = self.env.get(name, "")
        resolve_env["AI_MODEL"] = self.env.get("AI_MODEL", "")
        resolve_env["AI_BASE_URL"] = self.env.get("AI_BASE_URL", "")
        resolve_env["AI_JSON_MODE"] = self.env.get("AI_JSON_MODE", "true")
        cfgs = providers_for(resolve_env, check_ollama=False)
        return cfgs[0] if cfgs else None

    def _refresh_provider_card(self, row):
        """Пересчитывает статус (dot) и модель на карточке провайдера."""
        cfg = self._resolve_cfg(row["pid"])
        model = (
            (cfg or {}).get("model")
            or PROVIDERS.get(row["pid"], {}).get("model")
            or "—"
        )
        if row.get("connected"):
            status, dot, color = "Connected", "\u2713", ui.GREEN
        elif row.get("error"):
            status, dot, color = "Error", "\u26a0", ui.RED
        elif cfg is not None:
            status, dot, color = "Configured", "\u25cf", ui.GREEN
        else:
            status, dot, color = "Not configured", "\u25cb", ui.DIM
        try:
            row["model_lbl"].configure(text="Model: " + model)
            row["status_lbl"].configure(text=status, fg=color)
            row["status_dot"].configure(text=dot, fg=color)
        except tk.TclError:
            pass  # окно закрыли

    def _refresh_ai_markup(self):
        """Пересчитывает статус/модель всех карточек после изменений в .env."""
        for row in list(self.ai_provider_rows.values()):
            self._refresh_provider_card(row)

    def _test_connection(self, pid, row):
        """«Test Connection»: штатный runtime → минимальный API-запрос.

        Сеть уходит в поток, чтобы не морозить окно; UI обновляется через
        поллинг (self.win.after) — безопасно для Tk. Ошибки никогда не
        роняют окно и не показывают секреты.
        """
        if row.get("test_running"):
            return
        typed = row["var"].get().strip()
        if typed and typed != self.env.get(row["key"], ""):
            row["test_lbl"].configure(
                text="Сначала сохраните ключ, затем проверяйте связь.",
                fg=ui.YELLOW)
            return
        cfg = self._resolve_cfg(pid)
        if cfg is None:
            row["test_running"] = False
            row["test_result"] = None
            row["connected"] = False
            row["error"] = False
            row["test_lbl"].configure(
                text="\u25cb Not configured — вставьте API Key и сохраните.",
                fg=ui.DIM)
            self._refresh_provider_card(row)
            return
        row["test_running"] = True
        row["test_result"] = None
        row["test_btn"].configure(state="disabled")
        row["test_lbl"].configure(
            text="\u23f3 Проверяю связь… (может занять до 30 секунд)",
            fg=ui.YELLOW)
        threading.Thread(
            target=self._probe_worker, args=(row, cfg), daemon=True).start()
        self._poll_test(row)

    def _probe_worker(self, row, cfg):
        """Фоновый поток: только кладёт результат в словарь, Tk не трогает."""
        try:
            row["test_result"] = self._probe_provider(cfg)
        except Exception as error:
            # В исключении секрета нет — только класс ошибки.
            row["test_result"] = (
                False,
                f"\u2715 Connection failed: внутренняя ошибка ({type(error).__name__})",
            )

    def _probe_provider(self, cfg):
        """Минимальный запрос тем же путём, что и сам NOVA AI.

        Существующий AIBrain._post_provider (registry → URL/ключ/модель,
        ретраи на 429/5xx) — отдельного HTTP-клиента нет.
        """
        brain = AIBrain()
        messages = [{"role": "user", "content": "ping"}]
        try:
            resp = brain._post_provider(
                cfg, messages, max_tokens=8, temperature=0.0, json_mode=False)
        except RetryableHTTPError as error:
            body = redact_secrets(error.body or "", (cfg.get("key") or "",))
            return (False, "\u2715 Connection failed: " + _classify_status(
                error.status, body))
        except requests.exceptions.Timeout:
            return (False, "\u2715 Connection failed: Network error (timeout)")
        except requests.exceptions.ConnectionError:
            return (False, "\u2715 Connection failed: Network error (no connection)")
        except requests.exceptions.RequestException:
            return (False, "\u2715 Connection failed: Network error")
        if resp.ok:
            return (True, "\u2713 Connection successful")
        body = redact_secrets(resp.text or "", (cfg.get("key") or "",))
        return (False, "\u2715 Connection failed: " + _classify_status(
            resp.status_code, body))

    def _poll_test(self, row):
        """Поллинг результата фонового теста (вызывается в главном потоке)."""
        if row.get("test_result") is None:
            try:
                self.win.after(120, self._poll_test, row)
            except tk.TclError:
                return  # окно закрыли во время проверки
            return
        self._finish_test(row, row["test_result"])

    def _finish_test(self, row, result):
        ok, text = result
        row["test_result"] = result
        row["test_running"] = False
        row["connected"] = bool(ok)
        row["error"] = not ok
        try:
            row["test_btn"].configure(state="normal")
            row["test_lbl"].configure(text=text, fg=ui.GREEN if ok else ui.RED)
            self._refresh_provider_card(row)
            name = PROVIDERS.get(row["pid"], {}).get("name", row["pid"])
            self._set_status(f"{name}: {text}", ui.GREEN if ok else ui.RED)
        except tk.TclError:
            pass  # окно закрыли во время проверки

    # -----------------------------------------------------------
    # Вкладка «Разрешения»
    # -----------------------------------------------------------
    def _build_permissions_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        ui.label(parent, "Разрешения NOVA AI на действия в компьютере",
                 size=14, bold=True, bg=ui.BG).grid(row=0, column=0, sticky="ew",
                                                    padx=16, pady=(14, 2))
        ui.label(parent, "Если разрешение снято — NOVA AI не сможет совершать такие действия.",
                 size=10, color=ui.DIM, bg=ui.BG).grid(row=1, column=0, sticky="ew",
                                                       padx=16)

        self.policy = PermissionPolicy()
        self.perm_rows = []

        scroll, body = self._scroll_area(parent)
        scroll.grid(row=2, column=0, sticky="nsew", padx=4)
        body.columnconfigure(0, weight=1)

        for item in self.policy.describe_all():
            self._add_permission_card(body, item)
        self._spacer(body, 10)

        actions = tk.Frame(body, bg=ui.BG)
        actions.grid(row=self._next_row(), column=0, sticky="w", padx=8, pady=(8, 4))
        save_btn = ui.btn(actions, "Сохранить разрешения", None, kind="accent", size=11, width=20)
        reset_btn = ui.btn(actions, "Сбросить к стандартным", None, kind="neutral", size=11, width=20)
        save_btn.pack(side="left", padx=2)
        reset_btn.pack(side="left", padx=2)

        self._spacer(body, 6)
        ui.label(
            body,
            "Изменения сохраняются в data/permissions.json и применяются "
            "после перезапуска NOVA AI.",
            size=10, color=ui.DIM, bg=ui.BG,
        ).grid(row=self._next_row(), column=0, sticky="ew", pady=(4, 8))
        self._spacer(body, 4)

        def save():
            for row in self.perm_rows:
                self.policy.set_permission(
                    row["name"],
                    allowed=row["allowed_var"].get(),
                    confirmation_required=row["conf_var"].get(),
                )
            path = self.policy.save()
            self._set_status(f"Разрешения сохранены: permissions.json ({path.name})", ui.GREEN)

        def reset():
            if not messagebox.askyesno(
                "Сбросить разрешения",
                "Вернуть стандартные разрешения (файл permissions.json будет удалён)?",
            ):
                return
            if DEFAULT_PERMISSIONS_FILE.exists():
                DEFAULT_PERMISSIONS_FILE.unlink()
            self.policy = PermissionPolicy()
            self._reload_permissions_tab(parent)
            self._set_status("Разрешения сброшены к стандартным", ui.ACCENT)

        save_btn.configure(command=save)
        reset_btn.configure(command=reset)

    def _add_permission_card(self, body, item):
        c = ui.card(body)
        c.grid(row=self._next_row(), column=0, sticky="ew", padx=8, pady=3)
        c.columnconfigure(1, weight=1)
        c.columnconfigure(0, weight=1)

        _clabel(c, item["title"], size=12, bold=True).grid(
            row=0, column=0, sticky="w")
        tk.Label(
            c, text=item["risk_label"], font=(ui.FONT, 9, "bold"),
            fg=RISK_COLORS.get(item["risk_label"], ui.DIM), bg=ui.PANEL2,
        ).grid(row=0, column=1, sticky="e", padx=(6, 0))

        _clabel(c, item["description"], size=10, color=ui.DIM, wraplength=900,
                justify="left").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 6))

        # Чем именно управляет переключатель — видно прямо в настройках.
        tools = tools_by_permission().get(item["name"], [])
        if tools:
            shown = ", ".join(tools) if len(tools) <= 4 else (
                ", ".join(tools[:4]) + f" и ещё {len(tools) - 4}"
            )
            note = f"Включает: {shown}"
            note_color = ui.DIM
        else:
            # Раньше такой переключатель выглядел как рабочий: его можно
            # было снять, и сэр считал, что действие запрещено. На деле
            # под ним нет ни одного инструмента, а проверить связь было
            # нечем — список инструментов жил внутри __init__.
            note = "Пока ни к чему не подключён: ни один инструмент не использует это разрешение."
            note_color = ui.YELLOW
        _clabel(c, note, size=9, color=note_color, wraplength=900,
                justify="left").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(0, 6))

        controls = tk.Frame(c, bg=ui.PANEL2)
        controls.grid(row=3, column=0, columnspan=2, sticky="w")
        allowed_var = tk.BooleanVar(value=item["allowed"])
        conf_var = tk.BooleanVar(value=item["confirmation_required"])

        ui.check(controls, "Разрешено", allowed_var, bold=True).pack(side="left", padx=(0, 18))
        ui.check(controls, "Спрашивать подтверждение", conf_var).pack(side="left")

        self.perm_rows.append({
            "name": item["name"],
            "allowed_var": allowed_var,
            "conf_var": conf_var,
        })

    def _reload_permissions_tab(self, parent):
        for widget in parent.winfo_children():
            widget.destroy()
        self._build_permissions_tab(parent)

    # -----------------------------------------------------------
    # Вкладка «Общие»
    # -----------------------------------------------------------
    def _build_general_tab(self, parent):
        parent.columnconfigure(0, weight=1)

        c = ui.card(parent)
        c.grid(row=0, column=0, sticky="ew", padx=16, pady=16)
        c.columnconfigure(1, weight=1)
        c.columnconfigure(0, weight=1)

        _clabel(c, "Режим ввода", size=13, bold=True).grid(
            row=0, column=0, sticky="w", pady=(0, 8))
        self.mode_var = tk.StringVar(value=self.env.get("JARVIS_INPUT_MODE", "voice"))
        mode_frame = tk.Frame(c, bg=ui.PANEL2)
        mode_frame.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 16))
        ui.radio(mode_frame, "Голосовой (wake-word «Джарвис»)", self.mode_var, "voice"
                 ).pack(side="left", padx=(0, 26))
        ui.radio(mode_frame, "Текстовый (в терминале)", self.mode_var, "text"
                 ).pack(side="left")

        _clabel(c, "Персона", size=13, bold=True).grid(
            row=2, column=0, sticky="w", pady=(0, 8))
        self.persona_var = tk.StringVar(value=self.env.get("JARVIS_PERSONA", "sol"))
        persona_frame = tk.Frame(c, bg=ui.PANEL2)
        persona_frame.grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 16))
        for name, desc in (("sol", "Sol — быстрый и уверенный"),
                           ("terra", "Terra — системный инженер"),
                           ("luna", "Luna — внимательный собеседник")):
            ui.radio(persona_frame, desc, self.persona_var, name).pack(anchor="w")

        def save():
            write_env({
                "JARVIS_INPUT_MODE": self.mode_var.get(),
                "JARVIS_PERSONA": self.persona_var.get(),
            })
            self.env = read_env()
            self._set_status("Общие настройки сохранены (применятся после перезапуска NOVA AI)", ui.GREEN)

        ui.btn(c, "Сохранить общие настройки", save, kind="accent", size=12, width=24,
               padx=18, pady=10).grid(row=4, column=0, columnspan=2, sticky="w")

        # -------------------------------------------------------
        # NOVA AI онлайн 24/7 — автозапуск при входе в macOS
        # -------------------------------------------------------
        c2 = ui.card(parent)
        c2.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 16))
        c2.columnconfigure(0, weight=1)

        _clabel(c2, "NOVA AI онлайн 24/7 — автозапуск при входе", size=13, bold=True).grid(
            row=0, column=0, sticky="w")
        _clabel(
            c2,
            "NOVA AI запускается автоматически при входе в macOS и перезапускается "
            "при сбоях (launchd-сервис com.nova_ai.assistant) — ассистент всегда "
            "на связи, а Telegram-бот отвечает сообщениям, пока вы заняты.",
            size=10, color=ui.DIM, wraplength=940, justify="left",
        ).grid(row=1, column=0, sticky="ew", pady=(6, 10))

        self.online_status = tk.Label(
            c2, text="", font=(ui.FONT, 10, "bold"), bg=ui.PANEL2, anchor="w")
        self.online_status.grid(row=2, column=0, sticky="w", pady=(0, 10))

        def refresh_online():
            loaded = self._launchd_loaded()
            if loaded is None:
                text, color = "Не удалось проверить статус сервиса", ui.DIM
            elif loaded:
                text, color = "✅ NOVA AI работает как сервис — онлайн 24/7", ui.GREEN
            else:
                text, color = "⚪ Автозапуск выключен — NOVA AI стартует только вручную", ui.FAINT
            self.online_status.configure(text=text, fg=color)
        refresh_online()

        online_btns = tk.Frame(c2, bg=ui.PANEL2)
        online_btns.grid(row=3, column=0, sticky="w")
        start_btn = ui.btn(online_btns, "Включить онлайн 24/7", None,
                           kind="green", size=10, width=22, padx=16, pady=8)
        stop_btn = ui.btn(online_btns, "Выключить автозапуск", None,
                          kind="neutral", size=10, width=20, padx=16, pady=8)
        check_btn = ui.btn(online_btns, "Проверить", None,
                           kind="neutral", size=10, width=12, padx=16, pady=8)

        def enable_online():
            plist = os.path.expanduser("~/Library/LaunchAgents/com.nova_ai.assistant.plist")
            if not os.path.exists(plist):
                messagebox.showwarning(
                    "Автозапуск",
                    "Не найден com.nova_ai.assistant.plist — включите NOVA AI один раз через "
                    "«NOVA AI Control», и сервис появится.",
                )
                return
            uid = os.getuid()
            res = subprocess.run(
                ["launchctl", "bootstrap", f"gui/{uid}", plist],
                capture_output=True, text=True,
            )
            if res.returncode != 0:
                # уже загружен — просто перезапускаем сервис
                subprocess.run(
                    ["launchctl", "kickstart", f"gui/{uid}/com.nova_ai.assistant"],
                    capture_output=True, text=True,
                )
            refresh_online()
            self._set_status("NOVA AI переведён в режим онлайн 24/7", ui.GREEN)

        def disable_online():
            uid = os.getuid()
            subprocess.run(
                ["launchctl", "bootout", f"gui/{uid}/com.nova_ai.assistant"],
                capture_output=True, text=True,
            )
            refresh_online()
            self._set_status("Автозапуск NOVA AI выключен", ui.ACCENT)

        start_btn.configure(command=enable_online)
        stop_btn.configure(command=disable_online)
        check_btn.configure(command=refresh_online)
        for b in (start_btn, stop_btn, check_btn):
            b.pack(side="left", padx=2)

    # -----------------------------------------------------------
    # Вспомогательное
    # -----------------------------------------------------------
    def _next_row(self):
        row = self._row_counter
        self._row_counter += 1
        return row

    def _scroll_area(self, parent):
        self._row_counter = 0
        sf = ui.ScrollArea(parent)
        return sf, sf.inner

    def _spacer(self, body, height=6):
        tk.Frame(body, bg=ui.BG, height=height).grid(
            row=self._next_row(), column=0, sticky="ew")

    def _launchd_loaded(self):
        """Загружен ли launchd-сервис com.nova_ai.assistant (True/False/None)."""
        try:
            proc = subprocess.run(
                ["launchctl", "list"], capture_output=True, text=True, timeout=5,
            )
            return "com.nova_ai.assistant" in proc.stdout
        except Exception:
            return None

    def _set_status(self, text, color=ui.DIM):
        self.status.configure(text=text, fg=color)


def open_settings(parent=None, tab=None):
    """Открывает окно настроек. Если parent нет — свой mainloop."""
    if parent is not None:
        parent._requested_tab = tab
    win = SettingsWindow(parent)
    if parent is None:
        win.win.mainloop()
    return win


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        win = SettingsWindow()
        win.win.after(800, win.win.destroy)
        win.win.mainloop()
        print("SELFCHECK OK")
        sys.exit(0)
    open_settings()