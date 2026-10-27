#!/usr/bin/env python3
"""
ui_kit.py — общая тема и виджеты для интерфейса NOVA AI (панель + настройки).

Важно: на macOS обычный tk.Button игнорирует bg/fg (aqua-стиль) — кнопки
получаются светло-серыми, и текст «сливается». Поэтому кнопки, чекбоксы и
радиокнопки сделаны через ttk + тема clam, где цвета применяются надёжно.

Палитра — контрастная тёмная: светлый текст на тёмном фоне, кнопки с рамкой
и hover-эффектом.
"""
import math
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# ---------------------------------------------------------------
# Палитра (высокий контраст)
# ---------------------------------------------------------------
BG = "#0d1117"          # фон окна
PANEL = "#151b23"       # подложка карточек
PANEL2 = "#1b2530"      # карточка / hover
BORDER = "#2c3947"      # рамки между блоками
FIELD = "#090d13"       # поля ввода
TEXT = "#f2f7fb"        # основной текст (почти белый)
DIM = "#b9c6d2"         # вторичный текст (светлый, читаемый)
FAINT = "#7e92a3"       # подписи-подсказки
ACCENT = "#37d0ff"      # акцент (циан)
ACCENT_HOVER = "#6fe1ff"
ACCENT_DK = "#0e6f9e"   # тёмный кант акцентных кнопок
ON_ACCENT = "#04222e"   # текст на циане (тёмный = максимум контраста)
GREEN = "#2fd07d"       # успех / вкл
GREEN_HOVER = "#5ce79b"
GREEN_DK = "#0e7a44"
ON_GREEN = "#042318"
RED = "#ff5148"         # опасность / выкл
RED_HOVER = "#ff7a6b"
RED_DK = "#a42924"
ON_RED = "#ffffff"
YELLOW = "#ffcc4d"      # предупреждение (используется панелью настроек)
NEUTRAL = "#3c4a58"     # нейтральные кнопки
NEUTRAL_HOVER = "#4f6173"
NEUTRAL_DK = "#222c36"
ON_NEUTRAL = "#eef4f9"

FONT = "Helvetica Neue"
MONO = "Menlo"

# kind → (bg, hover, fg, border)
KINDS = {
    "accent": (ACCENT, ACCENT_HOVER, ON_ACCENT, ACCENT_DK),
    "green": (GREEN, GREEN_HOVER, ON_GREEN, GREEN_DK),
    "danger": (RED, RED_HOVER, ON_RED, RED_DK),
    "neutral": (NEUTRAL, NEUTRAL_HOVER, ON_NEUTRAL, NEUTRAL_DK),
}

_initialized = set()


def init_styles(root):
    """Настраивает ttk-стили (clam) для кнопок/чекбоксов/радио/вкладок."""
    root_id = id(root.winfo_toplevel())
    if root_id in _initialized:
        return
    _initialized.add(root_id)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    # Вкладки
    style.configure("J.TNotebook", background=BG, borderwidth=0)
    style.configure(
        "J.TNotebook.Tab", background=PANEL, foreground=DIM,
        padding=(18, 9), font=(FONT, 11, "bold"),
    )
    style.map(
        "J.TNotebook.Tab",
        background=[("selected", PANEL2)],
        foreground=[("selected", ACCENT)],
    )

    # Чекбоксы и радио
    style.configure(
        "J.TCheckbutton", background=PANEL2, foreground=DIM,
        font=(FONT, 10, "bold"), padding=(0, 2), focuscolor=PANEL2,
    )
    style.map(
        "J.TCheckbutton",
        background=[("active", PANEL2)],
        foreground=[("active", TEXT)],
    )
    style.configure(
        "J.TRadiobutton", background=PANEL2, foreground=DIM,
        font=(FONT, 10), padding=(0, 2), focuscolor=PANEL2,
    )
    style.map(
        "J.TRadiobutton",
        background=[("active", PANEL2)],
        foreground=[("active", TEXT)],
    )

    # Кнопки всех сортов (bg, hover, fg, border)
    for kind, (bg, hover, fg, border) in KINDS.items():
        for size in (8, 9, 10, 11, 12, 13, 14):
            for bold_suffix, weight in (("B", "bold"), ("N", "normal")):
                name = _btn_style_name(kind, size, bold_suffix)
                style.configure(
                    name,
                    background=bg, foreground=fg,
                    bordercolor=border, lightcolor=hover, darkcolor=border,
                    focuscolor=ACCENT, relief="flat",
                    font=(FONT, size, weight), padding=(12, 7),
                )
                style.map(
                    name,
                    background=[("pressed", border), ("active", hover)],
                    foreground=[("pressed", fg), ("active", fg)],
                    bordercolor=[("focus", ACCENT)],
                )


def _btn_style_name(kind, size, bold_suffix="B"):
    return f"J.{kind}.{size}{bold_suffix}.TButton"


def _ensure_styles(widget):
    try:
        init_styles(widget.winfo_toplevel())
    except tk.TclError:
        pass


def label(parent, text, size=11, color=TEXT, bold=False, bg=PANEL,
          anchor="w", justify="left", wraplength=0):
    """Тёмный контрастный лейбл."""
    return tk.Label(
        parent, text=text, font=(FONT, size, "bold" if bold else "normal"),
        fg=color, bg=bg, anchor=anchor, justify=justify,
        wraplength=wraplength,
    )


def btn(parent, text, command, kind="accent", size=10, width=None,
        padx=14, pady=8, bold=True):
    """Кнопка на ttk+clam: цвета применяются на macOS, есть hover/рамка."""
    _ensure_styles(parent)
    name = _btn_style_name(kind, size, "B" if bold else "N")
    opts = {
        "text": text,
        "command": command,
        "style": name,
        "cursor": "hand2",
        "padding": (padx, pady),
    }
    if width:
        opts["width"] = width
    b = ttk.Button(parent, **opts)
    b._jarvis_kind = kind
    b._jarvis_size = size
    b._jarvis_bold = bold
    return b


def set_kind(b, kind):
    """Меняет цвет кнопки на лету (например, Включить/Выключить)."""
    _ensure_styles(b)
    b._jarvis_kind = kind
    b.configure(style=_btn_style_name(
        kind, b._jarvis_size, "B" if b._jarvis_bold else "N"))
    return b


def card(parent, bg=PANEL2, hover=True):
    """Карточка с рамкой для группировки элементов."""
    c = tk.Frame(parent, bg=bg, highlightbackground=BORDER,
                 highlightthickness=1, padx=14, pady=10)
    if hover:
        c._is_jarvis_card = True
    return c


def sep(parent, color=BORDER, height=1):
    s = tk.Frame(parent, bg=color, height=height)
    return s


def check(parent, text, variable, on_change=None, bold=False):
    """Контрастный чекбокс (ttk, clam)."""
    _ensure_styles(parent)
    c = ttk.Checkbutton(
        parent, text=text, variable=variable, command=on_change,
        style="J.TCheckbutton", cursor="hand2",
    )
    return c


def radio(parent, text, variable, value, bold=False):
    r = ttk.Radiobutton(
        parent, text=text, variable=variable, value=value,
        style="J.TRadiobutton", cursor="hand2",
    )
    return r


class ScrollArea(tk.Frame):
    """Область с прокруткой (колёсико + полоса), тёмная тема."""

    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        _ensure_styles(self)
        style = ttk.Style()
        style.configure(
            "J.Vertical.TScrollbar", background=PANEL, troughcolor=BG,
            bordercolor=BG, arrowcolor=DIM, darkcolor=PANEL2, lightcolor=PANEL2,
        )
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                style="J.Vertical.TScrollbar")
        self.inner = tk.Frame(self.canvas, bg=BG)
        self._win_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfigure(self._win_id, width=e.width),
        )
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.sb.pack(side="right", fill="y")
        self.canvas.bind("<MouseWheel>", self._on_wheel, add="+")
        self.inner.bind("<MouseWheel>", self._on_wheel, add="+")

    def _on_wheel(self, event):
        step = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(step, "units")


# ================================================================
# АНИМАЦИИ — плавные переходы для современного интерфейса
# ================================================================

def hex_to_rgb(color):
    color = str(color).strip()
    if color.startswith("#") and len(color) == 7:
        try:
            return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
        except ValueError:
            pass
    return (255, 255, 255)


def rgb_to_hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(c))) for c in rgb)


def interpolate(color1, color2, t):
    """Плавное смешение двух HEX-цветов (t от 0 до 1)."""
    t = max(0.0, min(1.0, float(t)))
    a, b = hex_to_rgb(color1), hex_to_rgb(color2)
    return rgb_to_hex(tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)))


def tween_frame(widget, attr, target, steps=9, interval=14, on_done=None):
    """Плавно анимирует цветовой атрибут tk-виджета (bg, highlightbackground)."""
    cancels = getattr(widget, "_jarvis_tweens", {})
    old_id = cancels.get(attr)
    if old_id is not None:
        try:
            widget.after_cancel(old_id)
        except tk.TclError:
            pass
    try:
        start = widget.cget(attr)
    except tk.TclError:
        return
    steps = max(1, steps or 1)
    counter = {"i": 0}

    def step():
        counter["i"] += 1
        try:
            color = interpolate(start, target, counter["i"] / steps)
            widget.configure(**{attr: color})
        except tk.TclError:
            return
        if counter["i"] < steps:
            cancels[attr] = widget.after(interval, step)
        else:
            cancels.pop(attr, None)
            if on_done:
                on_done()
    cancels[attr] = widget.after(1, step)
    widget._jarvis_tweens = cancels


def fade_in_window(win, duration_ms=240):
    """Плавное появление окна (alpha 0 → 1). Безопасно на macOS/Win."""
    try:
        steps = max(8, duration_ms // 18)
        alpha = [0.0]
        win.attributes("-alpha", 0.0)
    except tk.TclError:
        return

    def step():
        alpha[0] = min(1.0, alpha[0] + 1.0 / steps)
        try:
            win.attributes("-alpha", max(0.02, alpha[0]))
        except tk.TclError:
            return
        if alpha[0] < 1.0:
            win.after(duration_ms // steps, step)

    if win.winfo_viewable():
        step()
    else:
        win.after(60, step)


def fade_out_window(win, duration_ms=200, on_done=None):
    """Плавное скрытие окна (alpha 1 → 0), затем on_done()."""
    try:
        steps = max(8, duration_ms // 18)
        alpha = [1.0]
    except tk.TclError:
        if on_done:
            on_done()
        return

    def step():
        alpha[0] = max(0.0, alpha[0] - 1.0 / steps)
        try:
            win.attributes("-alpha", max(0.0, alpha[0]))
        except tk.TclError:
            pass
        if alpha[0] <= 0.0:
            if on_done:
                on_done()
        else:
            win.after(duration_ms // steps, step)

    win.after(10, step)


def start_pulse(label_widget, light, dark, period_ms=1300):
    """Мягкая пульсация текста-индикатора (например, статусной точки)."""
    stop_pulse(label_widget)
    label_widget._jarvis_phase = 0.0

    def tick():
        label_widget._jarvis_phase = getattr(label_widget, "_jarvis_phase", 0.0) + 0.12
        wave = (math.sin(label_widget._jarvis_phase) + 1) / 2
        try:
            label_widget.configure(fg=interpolate(dark, light, wave))
        except tk.TclError:
            return
        label_widget._jarvis_pulse_after = label_widget.after(
            max(30, period_ms // 30), tick)

    tick()


def stop_pulse(widget):
    after_id = getattr(widget, "_jarvis_pulse_after", None)
    if after_id is not None:
        try:
            widget.after_cancel(after_id)
        except tk.TclError:
            pass
        widget._jarvis_pulse_after = None


# ---------------------------------------------------------------
# Скруглённая canvas-кнопка с hover/press анимациями
# ---------------------------------------------------------------
class ModernButton(tk.Canvas):
    """Современная кнопка: скруглённые углы, плавный hover и нажатие."""

    def __init__(self, parent, text, command=None, kind="accent", size=12,
                 width=None, height=42, radius=13, padx=24):
        base, hover, fg, border = KINDS.get(kind, KINDS["neutral"])
        self._base = base
        self._hover_color = hover
        self._fg = fg
        self._border = border
        self._command = command
        self._enabled = True
        self._pressed_inside = False
        font = tkfont.Font(family=FONT, size=size, weight="bold")
        if width is None:
            width = font.measure(text) + padx * 2
        pbg = BG
        try:
            pbg = parent.cget("bg")
        except tk.TclError:
            pass
        super().__init__(parent, width=int(width), height=int(height), bg=pbg,
                         highlightthickness=0, cursor="hand2")
        w, h = int(width), int(height)
        r = min(radius, h // 2)
        points = self._rounded_rect(2, 2, w - 2, h - 2, r)
        self._rect = self.create_polygon(*points, smooth=True)
        self.itemconfigure(self._rect, fill=base, outline=border, width=1)
        self._txt = self.create_text(
            w / 2, h / 2, text=text, fill=fg, font=font)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    @staticmethod
    def _rounded_rect(x1, y1, x2, y2, r):
        points = [
            x1 + r, y1, x2 - r, y1, x2, y1,
            x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2,
            x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return points

    def _animate_rect(self, target, on_done=None):
        cancels = getattr(self, "_jarvis_tweens", {})
        old = cancels.get("fill")
        if old is not None:
            try:
                self.after_cancel(old)
            except tk.TclError:
                pass
        try:
            start = self.itemcget(self._rect, "fill")
        except tk.TclError:
            return
        steps, interval = 10, 14
        counter = {"i": 0}

        def step():
            counter["i"] += 1
            try:
                self.itemconfigure(
                    self._rect,
                    fill=interpolate(start, target, counter["i"] / steps))
            except tk.TclError:
                return
            if counter["i"] < steps:
                cancels["fill"] = self.after(interval, step)
            else:
                cancels.pop("fill", None)
                if on_done:
                    on_done()
        cancels["fill"] = self.after(1, step)
        self._jarvis_tweens = cancels

    def _on_enter(self, _event=None):
        if self._enabled:
            self._animate_rect(self._hover_color)

    def _on_leave(self, _event=None):
        self._pressed_inside = False
        if self._enabled:
            self._animate_rect(self._base)

    def _on_press(self, _event=None):
        if self._enabled:
            self._pressed_inside = True
            self._animate_rect(self._border)

    def _on_release(self, event=None):
        if not self._enabled:
            return
        was_inside = self._pressed_inside
        self._pressed_inside = False
        width = int(self.cget("width"))
        height = int(self.cget("height"))
        if was_inside and event and 0 <= event.x <= width and 0 <= event.y <= height:
            self._animate_rect(self._hover_color)
            if self._command:
                self._command()
        else:
            self._animate_rect(self._base)

    def set_text(self, text):
        try:
            self.itemconfigure(self._txt, text=text)
        except tk.TclError:
            pass

    def set_kind(self, kind):
        base, hover, fg, border = KINDS.get(kind, KINDS["neutral"])
        self._base, self._hover_color, self._fg, self._border = base, hover, fg, border
        try:
            self.itemconfigure(self._txt, fill=fg)
            self.itemconfigure(self._rect, outline=border)
        except tk.TclError:
            pass
        self._animate_rect(base)

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        grey = "#55606d" if not self._enabled else self._base
        self.itemconfigure(self._rect, outline="#2c3947" if not self._enabled
                           else self._border)
        self._animate_rect(grey)


# ---------------------------------------------------------------
# Дышашая сфера-индикатор (главная панель)
# ---------------------------------------------------------------
class Orb(tk.Canvas):
    """Сфера с мягким «дыханием», плавным переключением состояний
    и вращающейся дугой при активной работе."""

    PALETTES = {
        "on": {
            "core": "#6ff2c0",
            "ring": "#2fd07d",
            "glow2": "#1e7a52",
            "glow1": "#14324a",
            "arc": ACCENT,
            "letter": "#042318",
        },
        "off": {
            "core": "#44546a",
            "ring": "#2c3947",
            "glow2": "#1b2530",
            "glow1": "#141a22",
            "arc": FAINT,
            "letter": "#141a22",
        },
    }

    def __init__(self, parent, size=190):
        pbg = BG
        try:
            pbg = parent.cget("bg")
        except tk.TclError:
            pass
        super().__init__(parent, width=size, height=size, bg=pbg,
                         highlightthickness=0)
        self.size = size
        self._running = False
        self._phase = 0.0
        self._arc_start = 90.0
        self._pal = dict(self.PALETTES["off"])
        r = size / 2
        cx = cy = r
        # слои свечения от внешнего к ядру
        self._glow1 = self.create_oval(0, 0, 0, 0, outline="", fill=self._pal["glow1"])
        self._glow2 = self.create_oval(0, 0, 0, 0, outline="", fill=self._pal["glow2"])
        self._ring = self.create_oval(0, 0, 0, 0,
                                      outline=self._pal["ring"], width=2)
        self._arc = self.create_arc(0, 0, 0, 0,
                                    start=self._arc_start, extent=34,
                                    style="arc", outline=self._pal["arc"], width=3)
        self._core = self.create_oval(0, 0, 0, 0, fill=self._pal["core"], outline="")
        letter_font = tkfont.Font(family=FONT, size=34, weight="bold")
        self._letter = self.create_text(cx, cy, text="J", font=letter_font,
                                        fill=self._pal["letter"])
        self._layout()
        self.after(33, self._tick)

    def set_running(self, running):
        self._running = bool(running)

    def _layout(self):
        r = self.size / 2
        s = self._swing if hasattr(self, "_swing") else 0.0
        def circle(k, grow=0):
            m = r * k + grow
            return (r - m, r - m, r + m, r + m)
        self.coords(self._glow1, *circle(0.46, int(s * 8)))
        self.coords(self._glow2, *circle(0.38, int(s * 5)))
        self.coords(self._ring, *circle(0.30, int(s * 2)))
        self.coords(self._core, *circle(0.20, int(s * 1)))
        self.coords(self._arc, *circle(0.24, int(s * 2)))
        self.itemconfigure(
            self._arc,
            start=self._arc_start,
            state="normal" if self._running else "hidden",
        )

    def _drift(self):
        target = self.PALETTES["on" if self._running else "off"]
        for name, value in target.items():
            if name == "letter":
                continue
            self._pal[name] = interpolate(self._pal[name], value, 0.09)

    def _tick(self):
        self._phase += 0.10
        self._swing = (math.sin(self._phase) + 1) / 2
        if self._running:
            self._arc_start = (self._arc_start + 6) % 360
        else:
            self._arc_start = 90.0
        self._drift()
        # _layout() дёргает coords() по элементам холста. При закрытии окна
        # холст уже уничтожен, и coords() бросает TclError до блока try —
        # исключение прерывает тик и печатается в stderr как
        # «Exception in Tkinter callback». Поэтому _layout() тоже внутри try.
        try:
            self._layout()
            for item, name in ((
                (self._glow1, "glow1"), (self._glow2, "glow2"),
                (self._ring, "ring"), (self._core, "core"), (self._arc, "arc"),
            )):
                self.itemconfigure(item, fill=self._pal[name],
                                   outline=self._pal[name])
            self.itemconfigure(self._letter, fill=self._pal["letter"])
        except tk.TclError:
            return
        self.after(33, self._tick)


# ---------------------------------------------------------------
# Подсветка карточек под курсором (глобально, плавно)
# ---------------------------------------------------------------
_motion_hooks = {}


def _ancestor_card(widget):
    node = widget
    while node is not None:
        if getattr(node, "_is_jarvis_card", False):
            return node
        try:
            node = node.master
        except (tk.TclError, AttributeError):
            return None
    return None


def install_motion_hover(root, hover_color=ACCENT_DK, base_color=BORDER):
    """Плавная подсветка рамки карточки при наведении (одна на приложение)."""
    key = id(root)
    if key in _motion_hooks:
        return
    state = {"card": None}

    def handler(event):
        try:
            widget = root.winfo_containing(event.x_root, event.y_root)
        except tk.TclError:
            return
        card = _ancestor_card(widget)
        if card is state["card"]:
            return
        previous = state["card"]
        state["card"] = card
        if previous is not None:
            try:
                if previous.winfo_exists():
                    tween_frame(previous, "highlightbackground", base_color, steps=8)
            except tk.TclError:
                pass
        if card is not None:
            tween_frame(card, "highlightbackground", hover_color, steps=8)

    root.bind_all("<Motion>", handler, add="+")
    _motion_hooks[key] = handler