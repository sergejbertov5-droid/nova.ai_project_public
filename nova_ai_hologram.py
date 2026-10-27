#!/usr/bin/env python3
"""
jarvis_hologram.py — «живая» проекция NOVA AI за всеми окнами (v4).

Полностью новая композиция. Вместо частиц и грубой mesh — чёткий
голографический «кор»:

  1. стеклянная заливка головы (вертикальный градиент) с «вдохом»;
  2. лёгкие восходящие частицы внутри головы;
  3. свип-скан, при прохождении которого корпус «подсвечивается»;
  4. вращающиеся тонкие овалы-кольца со спутниками вокруг головы + ореол;
  5. проектор-база: луч, линза, расходящееся кольцо-рипл, платформа;
  6. минималистичные HUD-скобки и боковые штрих-линии данных.

Окно — строго ПОЗАДИ всех окон и приложений: уровень -2147483618 (между
обоями Dock и иконками Finder), прозрачное, безрамочное, клики насквозь,
нет в Cmd-Tab / Dock / Mission Control.

Запуск:  .venv/bin/python3 jarvis_hologram.py
Тест:    JARVIS_HOLO_TEST=1 .venv/bin/python3 jarvis_hologram.py
"""
import math
import os
import sys
import time

import AppKit
import Quartz
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSColor,
    NSMakeRect,
    NSView,
    NSWindow,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorIgnoresCycle,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSTimer,
)
from Quartz import (
    kCGImageAlphaPremultipliedLast,
    CATransform3DMakeScale,
    CGBitmapContextCreate,
    CGBitmapContextCreateImage,
    CGColorSpaceCreateDeviceRGB,
    CGRectMake,
    CGPointMake,
    CGSizeMake,
    CGPathAddLineToPoint,
    CGPathCloseSubpath,
    CGPathCreateMutable,
    CGPathMoveToPoint,
)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
# Яркость голограммы (0.2..1.0): пишет core/interactions.hologram_brightness.
BRIGHTNESS_FILE = os.path.join(PROJECT_DIR, "logs", "holo_brightness.ctl")

WIN_W = 560
WIN_H = 640
HEAD = 330
HX = (WIN_W - HEAD) / 2.0          # левый край головы (root coords)
HY = 160.0                         # нижний край головы
CY = HY + HEAD / 2.0               # центр головы
BASE_Y = 96.0
DEFAULT_HOLO_LEVEL = -2147483618   # между обоями и иконками → за всеми окнами
FPS = 30.0
DT = 1.0 / FPS

# Палитра «янтарь» — классический тёпло-оранжевый NOVA AI (Age of Ultron)
C_WHITE = (1.00, 0.93, 0.72)   # тёплый белый (яркие линии)
C_MAIN = (1.00, 0.55, 0.12)    # основной оранжевый
C_DIM = (0.95, 0.45, 0.10)     # приглушённый янтарь


def _color(r, g, b, a=1.0):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a)


def _cg(r, g, b, a=1.0):
    return _color(r, g, b, a).CGColor()


def _dot_cgimage(size=44, rgb=(1.0, 1.0, 1.0)):
    """Мягкое радиальное пятно (аура, линза, частицы, спутники)."""
    w = h = size
    buf = bytearray(w * h * 4)
    c = (size - 1) / 2.0
    r = size / 2.0
    rr, gg, bb = rgb
    for y in range(h):
        base = y * w
        for x in range(w):
            dx = (x - c) / r
            dy = (y - c) / r
            d = math.sqrt(dx * dx + dy * dy)
            a = 0.0 if d >= 1.0 else (1.0 - d) ** 2.2
            v = int(a * 255)
            i = (base + x) * 4
            buf[i] = int(rr * v)
            buf[i + 1] = int(gg * v)
            buf[i + 2] = int(bb * v)
            buf[i + 3] = v
    cs = CGColorSpaceCreateDeviceRGB()
    ctx = CGBitmapContextCreate(buf, w, h, 8, w * 4, cs, kCGImageAlphaPremultipliedLast)
    return CGBitmapContextCreateImage(ctx)


class HoloView(NSView):
    def acceptsFirstResponder(self):
        return False


class JarvisHologram:
    def __init__(self):
        self.app = NSApplication.sharedApplication()
        self.app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        screen = AppKit.NSScreen.mainScreen()
        if screen is None:
            raise RuntimeError("Нет подключённого экрана")
        self.screen_frame = screen.frame()

        self.w, self.h = WIN_W, WIN_H
        x = int(float(Quartz.CGRectGetMidX(screen.frame())) - self.w / 2)
        y = int(os.environ.get("JARVIS_HOLO_Y", "30"))
        if os.environ.get("JARVIS_HOLO_X"):
            x = int(os.environ["JARVIS_HOLO_X"])

        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, self.w, self.h),
            NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered,
            False,
        )
        self.window.setTitle_("NOVA AI Hologram")
        self.window.setOpaque_(False)
        self.window.setBackgroundColor_(NSColor.clearColor())
        self.window.setHasShadow_(False)
        self.window.setIgnoresMouseEvents_(True)
        self.window.setLevel_(int(os.environ.get("JARVIS_HOLO_LEVEL", DEFAULT_HOLO_LEVEL)))
        self.window.setReleasedWhenClosed_(False)
        self.window.setAlphaValue_(0.0)   # появляться будем плавно, без вспышки
        self.window.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorIgnoresCycle
        )

        self.view = HoloView.alloc().initWithFrame_(NSMakeRect(0, 0, self.w, self.h))
        self.view.setWantsLayer_(True)
        self.window.setContentView_(self.view)

        self._build_scene()

        self._t0 = time.monotonic()
        self._next_scan = self._t0 + 2.2
        self._scan_active = False
        self._scan_start = 0.0
        self._next_ripple = self._t0 + 1.4
        self._ripple_age = 1.0       # «выключено», стартует по таймеру
        self._ring_phase = 0.0
        self._ring_speed = math.pi * 2 / 30.0
        for i, r in enumerate(self._orbit_rings):
            r["phase0"] = i * math.pi / 2.7

        # Яркость из logs/holo_brightness.ctl (0.2..1.0), читаем в _tick не чаще
        # раза в полсекунды и умножаем на общий множитель прозрачности окна.
        self._brightness = 1.0
        self._brightness_ts = 0.0

        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(DT, True, self._tick)

    # ==================================================================
    def _build_scene(self):
        root = Quartz.CALayer.layer()
        root.setFrame_(NSMakeRect(0, 0, self.w, self.h))
        root.setMasksToBounds_(True)
        self.view.setLayer_(root)
        self.root = root

        # ---- Аура (мягкое свечение позади) ----
        aura = Quartz.CALayer.layer()
        aura.setFrame_(CGRectMake(self.w / 2 - 300, CY - 300, 600, 600))
        aura.setContents_(_dot_cgimage(430, rgb=(1.0, 0.50, 0.10)))
        aura.setOpacity_(0.0)
        root.addSublayer_(aura)
        self.aura = aura

        # ---- Ореол-кольцо позади головы ----
        halo = Quartz.CAShapeLayer.layer()
        halo.setFillColor_(None)
        halo.setStrokeColor_(_cg(*C_DIM, 0.0))
        halo.setLineWidth_(1.0)
        halo.setOpacity_(0.0)
        root.addSublayer_(halo)
        self.halo = halo

        # ---- Луч проектора ----
        beam = Quartz.CAGradientLayer.layer()
        beam.setFrame_(CGRectMake(self.w / 2 - 10, BASE_Y + 10, 20, HY - BASE_Y - 10))
        beam.setColors_([
            _cg(*C_MAIN, 0.0),
            _cg(*C_MAIN, 0.07),
            _cg(*C_WHITE, 0.05),
        ])
        beam.setLocations_([0.0, 0.6, 1.0])
        beam.setStartPoint_(CGPointMake(0.5, 0.0))
        beam.setEndPoint_(CGPointMake(0.5, 1.0))
        beam.setOpacity_(0.0)
        root.addSublayer_(beam)
        self.beam = beam

        # ---- Кор голограммы ----
        head_rect = CGRectMake(0, 0, HEAD, HEAD)
        core = Quartz.CALayer.layer()
        core.setFrame_(CGRectMake(HX, HY, HEAD, HEAD))
        root.addSublayer_(core)
        self.core = core

        # 4.1 стеклянная заливка (вертикальный градиент)
        glass = Quartz.CAGradientLayer.layer()
        glass.setFrame_(head_rect)
        glass.setColors_([
            _cg(*C_WHITE, 0.26),
            _cg(*C_MAIN, 0.10),
            _cg(*C_MAIN, 0.0),
        ])
        glass.setLocations_([0.0, 0.55, 1.0])
        glass.setStartPoint_(CGPointMake(0.5, 1.0))
        glass.setEndPoint_(CGPointMake(0.5, 0.0))
        glass.setOpacity_(0.0)
        core.addSublayer_(glass)
        self.glass = glass

        # 4.2 внешний светящийся контур
        outline = Quartz.CAShapeLayer.layer()
        outline.setFrame_(head_rect)
        outline.setFillColor_(None)
        outline.setStrokeColor_(_cg(*C_WHITE, 1.0))
        outline.setLineWidth_(2.2)
        outline.setShadowColor_(_cg(*C_MAIN, 1.0))
        outline.setShadowOpacity_(0.95)
        outline.setShadowRadius_(11)
        outline.setOpacity_(0.0)
        core.addSublayer_(outline)
        self.outline = outline

        # 4.3 внутренние оболочки (пунктир) + спицы-связи
        self._inner_rings = []
        sch = Quartz.CAShapeLayer.layer()
        sch.setFrame_(head_rect)
        sch.setFillColor_(None)
        sch.setStrokeColor_(_cg(*C_MAIN, 0.0))
        sch.setLineWidth_(1.0)
        sch.setOpacity_(0.0)
        core.addSublayer_(sch)
        self.shell = sch

        # 4.4 восходящие частицы-данные
        part = Quartz.CAEmitterLayer.layer()
        part.setEmitterPosition_(CGPointMake(HEAD / 2, HEAD / 2))
        part.setEmitterShape_(Quartz.kCAEmitterLayerRectangle)
        part.setEmitterSize_(CGSizeMake(HEAD - 30, HEAD))
        part.setEmitterMode_(Quartz.kCAEmitterLayerSurface)
        part.setLifetime_(1.0)
        dot_img = _dot_cgimage(26, rgb=(1.0, 0.84, 0.55))
        cells = []
        for br, vel, sc in ((4.0, 14.0, 0.55), (3.0, 22.0, 0.35), (2.0, 9.0, 0.80)):
            cell = Quartz.CAEmitterCell.emitterCell()
            cell.setContents_(dot_img)
            cell.setColor_(_cg(*C_WHITE, 0.85))
            cell.setBirthRate_(br)
            cell.setLifetime_(5.0)
            cell.setLifetimeRange_(2.5)
            cell.setVelocity_(vel)
            cell.setVelocityRange_(6)
            cell.setEmissionLongitude_(math.pi / 2)   # вверх
            cell.setEmissionRange_(0.18)
            cell.setYAcceleration_(4.0)
            cell.setScale_(sc)
            cell.setScaleRange_(sc * 0.4)
            cell.setAlphaSpeed_(-0.10)
            cells.append((cell, br))
        part.setEmitterCells_([c for c, _ in cells])
        part.setOpacity_(0.0)
        core.addSublayer_(part)
        self.particles = part
        self._part_cells = cells

        # 4.5 свип-скан
        scan = Quartz.CALayer.layer()
        scan.setContents_(_dot_cgimage(70, rgb=(1.0, 0.92, 0.72)))
        scan.setContentsGravity_(Quartz.kCAGravityResize)
        scan.setFrame_(CGRectMake(8, HY - 20, HEAD - 16, 34))
        scan.setOpacity_(0.0)
        scan.setShadowColor_(_cg(*C_MAIN, 1.0))
        scan.setShadowOpacity_(0.9)
        scan.setShadowRadius_(14)
        core.addSublayer_(scan)
        self.scan = scan

        # ---- Кольца-орбиты вокруг головы ----
        self._orbit_rings = []
        dot_img = _dot_cgimage(28, rgb=(1.0, 0.90, 0.70))
        for rx_f, ry_f, speed, alpha, width in (
            (0.80, 0.30, 1.00, 0.60, 1.7),
            (0.98, 0.21, -0.60, 0.38, 1.1),
        ):
            ring = Quartz.CAShapeLayer.layer()
            ring.setFillColor_(None)
            ring.setStrokeColor_(_cg(*C_WHITE, alpha))
            ring.setLineWidth_(width)
            ring.setShadowColor_(_cg(*C_MAIN, 1.0))
            ring.setShadowOpacity_(0.8)
            ring.setShadowRadius_(7)
            root.addSublayer_(ring)
            dots_n = 3 if speed > 0 else 4
            self._orbit_rings.append({
                "layer": ring,
                "rx": HEAD * rx_f,
                "ry": HEAD * ry_f,
                "speed": speed,
                "alpha": alpha,
                "dots": [],
                "phase0": 0.0,
            })
            for k in range(dots_n):
                d = Quartz.CALayer.layer()
                d.setContents_(dot_img)
                d.setFrame_(CGRectMake(0, 0, 9, 9))
                d.setOpacity_(0.0)
                root.addSublayer_(d)
                self._orbit_rings[-1]["dots"].append(
                    {"layer": d, "off": k * math.pi / dots_n})

        # ---- HUD-скобки и штрих-линии ----
        self._brackets = []
        bw = 52
        for cx0, cy0, dx, dy in (
            (HX - 30, HY + HEAD + 24, 1, 1),
            (HX + HEAD + 30, HY + HEAD + 24, -1, 1),
            (HX - 30, HY - 24, 1, -1),
            (HX + HEAD + 30, HY - 24, -1, -1),
        ):
            br = Quartz.CAShapeLayer.layer()
            bp = CGPathCreateMutable()
            CGPathMoveToPoint(bp, None, cx0 + dx * bw, cy0)
            CGPathAddLineToPoint(bp, None, cx0, cy0)
            CGPathAddLineToPoint(bp, None, cx0, cy0 + dy * bw)
            br.setPath_(bp)
            br.setStrokeColor_(_cg(*C_WHITE, 0.9))
            br.setLineWidth_(1.8)
            br.setFillColor_(None)
            br.setShadowColor_(_cg(*C_MAIN, 1.0))
            br.setShadowOpacity_(0.9)
            br.setShadowRadius_(6)
            br.setOpacity_(0.0)
            root.addSublayer_(br)
            self._brackets.append(br)

        ticks = Quartz.CAShapeLayer.layer()
        tp = CGPathCreateMutable()
        for sx in (HX - 66, HX + HEAD + 66):
            for k in range(6):
                yy = HY + 30 + k * (HEAD - 60) / 5
                CGPathMoveToPoint(tp, None, sx, yy - 6)
                CGPathAddLineToPoint(tp, None, sx, yy + 6)
        ticks.setPath_(tp)
        ticks.setStrokeColor_(_cg(*C_DIM, 1.0))
        ticks.setLineWidth_(1.0)
        ticks.setLineDashPattern_([2, 5])
        ticks.setOpacity_(0.0)
        root.addSublayer_(ticks)
        self.ticks = ticks

        # ---- Проектор-база ----
        base = Quartz.CAShapeLayer.layer()
        bp = CGPathCreateMutable()
        CGPathMoveToPoint(bp, None, self.w / 2 - 150, BASE_Y)
        CGPathAddLineToPoint(bp, None, self.w / 2 + 150, BASE_Y)
        CGPathMoveToPoint(bp, None, self.w / 2 - 96, BASE_Y - 14)
        CGPathAddLineToPoint(bp, None, self.w / 2 + 96, BASE_Y - 14)
        base.setPath_(bp)
        base.setStrokeColor_(_cg(*C_DIM, 0.9))
        base.setLineWidth_(1.2)
        base.setFillColor_(None)
        base.setOpacity_(0.0)
        root.addSublayer_(base)
        self.base = base

        lens = Quartz.CALayer.layer()
        lens.setFrame_(CGRectMake(self.w / 2 - 12, BASE_Y - 12, 24, 24))
        lens.setContents_(_dot_cgimage(46, rgb=(1.0, 0.65, 0.25)))
        lens.setOpacity_(0.0)
        lens.setShadowColor_(_cg(*C_MAIN, 1.0))
        lens.setShadowOpacity_(0.95)
        lens.setShadowRadius_(12)
        root.addSublayer_(lens)
        self.lens = lens

        ripple = Quartz.CAShapeLayer.layer()
        ripple.setFillColor_(None)
        ripple.setStrokeColor_(_cg(*C_WHITE, 1.0))
        ripple.setLineWidth_(1.2)
        ripple.setOpacity_(0.0)
        ripple.setShadowColor_(_cg(*C_MAIN, 1.0))
        ripple.setShadowOpacity_(0.8)
        ripple.setShadowRadius_(8)
        root.addSublayer_(ripple)
        self.ripple = ripple

        self.head_center = CGPointMake(self.w / 2, CY)

    # ==================================================================
    def _tick(self, timer):
        t = time.monotonic()
        dt = t - self._t0

        # --- Яркость из ctl-файла (не чаще раза в 0.5с) ---
        if t - self._brightness_ts >= 0.5:
            self._brightness_ts = t
            try:
                with open(BRIGHTNESS_FILE, "r") as f:
                    value = float(f.read().strip())
                if 0.2 <= value <= 1.0:
                    self._brightness = value
            except (OSError, ValueError):
                pass

        # --- Плавное появление окна (длиннее + сглаженное, без вспышки) ---
        wboot = min(1.0, dt / 1.8)
        self.window.setAlphaValue_(
            wboot * wboot * (3 - 2 * wboot) * self._brightness
        )

        # --- Послойная материализация: аура → кор → кольца ---
        boot = min(1.0, dt / 3.6)
        ease = boot * boot * (3 - 2 * boot)
        core_boot = min(1.0, max(0.0, (dt - 0.35) / 2.9))
        core_ease = core_boot * core_boot * (3 - 2 * core_boot)
        ring_boot = min(1.0, max(0.0, (dt - 0.7) / 2.7))
        ring_ease = ring_boot * ring_boot * (3 - 2 * ring_boot)

        breath = 0.5 + 0.5 * math.sin(dt * (2 * math.pi / 10.0))
        bob = 3.5 * math.sin(dt * (2 * math.pi / 8.5))
        cy = CY + bob

        # «Материализация» кора: лёгкое увеличение 0.90 → 1.0
        s = 0.90 + 0.10 * core_ease
        self.core.setTransform_(CATransform3DMakeScale(s, s, 1.0))

        # --- Аура / луч / база ---
        self.aura.setOpacity_((0.30 + 0.10 * breath + 0.12 * ease) * ease)
        self.beam.setOpacity_((0.16 + 0.06 * breath) * ease)
        self.base.setOpacity_((0.55 + 0.25 * breath) * ease)
        self.lens.setOpacity_((0.92 + 0.08 * math.sin(dt * 2.7)) * ease)

        # --- Кор ---
        scan_boost = 0.0
        if self._scan_active:
            scan_boost = 0.55 * math.sin(math.pi * self._scan_k())
        self.glass.setOpacity_((0.18 + 0.10 * breath + scan_boost) * core_ease)
        self.outline.setOpacity_((0.95 + 0.05 * breath) * core_ease)
        self.outline.setShadowRadius_(10 + 3 * breath)
        self.shell.setOpacity_((0.55 + 0.15 * breath) * core_ease)
        for i, ring in enumerate(self._inner_rings):
            ring.setOpacity_((0.42 + 0.12 * breath) * core_ease)
        self.particles.setOpacity_((0.85 + 0.10 * breath) * core_ease)

        # частицы: вдох + усиление при скане + пробуждение
        mult = (0.55 + 0.45 * core_ease) * (0.7 + 0.6 * breath)
        if self._scan_active:
            mult += 0.8
        for cell, br in self._part_cells:
            cell.setBirthRate_(br * mult)

        # --- Свип-скан ---
        if not self._scan_active and t >= self._next_scan:
            self._scan_active = True
            self._scan_start = t
            self._scan_dur = 1.35
        if self._scan_active:
            k = self._scan_k()
            if k >= 1.0:
                self._scan_active = False
                self.scan.setOpacity_(0.0)
                self._next_scan = t + 5.0 + (time.monotonic() % 3.5)
            else:
                yy = HY + HEAD - 36 - k * (HEAD - 58)
                self.scan.setFrameOrigin_(CGPointMake(HX + 8, yy))
                self.scan.setOpacity_((0.95 * math.sin(math.pi * k)) * ease)

        # --- Ореол и кольца ---
        # Раскрутка при пробуждении: быстрое вращение замедляется до номинального
        spin = 1.0 + 3.0 * (1.0 - ring_ease)
        self._ring_phase += self._ring_speed * spin * DT
        for r in self._orbit_rings:
            ph = r["phase0"] + self._ring_phase * r["speed"]
            apprx = abs(math.cos(ph)) * r["rx"] + 3.0
            ring = r["layer"]
            ring.setPath_(_ellipse_path(self.w / 2, cy, apprx, r["ry"], 0, 56))
            ring.setOpacity_((r["alpha"] + 0.15 * breath) * ring_ease)
            for dot in r["dots"]:
                a = dot["off"] + ph
                dot["layer"].setPosition_(
                    CGPointMake(
                        self.w / 2 + math.cos(a) * apprx,
                        cy + math.sin(a) * r["ry"],
                    )
                )
                dot["layer"].setOpacity_(
                    (0.5 + 0.5 * math.sin(a * 2.0)) * ring_ease)

        halo_rx = HEAD * 0.82 * (0.72 + 0.28 * math.sin(dt * 0.35))
        self.halo.setPath_(_ellipse_path(self.w / 2, cy - 10, halo_rx,
                                         halo_rx * 0.24, 0, 56))
        self.halo.setOpacity_((0.16 + 0.10 * breath) * ring_ease)

        # --- Рипл у базы ---
        if t >= self._next_ripple:
            self._next_ripple = t + 3.6 + (time.monotonic() % 2.0)
            self._ripple_age = 0.0
        if self._ripple_age < 1.0:
            self._ripple_age += DT
            age = self._ripple_age
            dur = 1.5
            if age <= dur:
                g = 0.35 + 2.2 * (age / dur)
                self.ripple.setPath_(_ellipse_path(
                    self.w / 2, BASE_Y + 6, g * 46, g * 18, 0, 40))
                self.ripple.setOpacity_(
                    (0.55 * (1.0 - age / dur)) * ease)
            else:
                self.ripple.setOpacity_(0.0)

        # --- HUD-скобки, лёгкое мерцание ---
        for i, br in enumerate(self._brackets):
            br.setOpacity_(
                (0.70 + 0.30 * math.sin(dt * (2 * math.pi / 3.4 + i))) * ease)
        self.ticks.setOpacity_((0.45 + 0.20 * breath) * ease)

    def _scan_k(self):
        return (time.monotonic() - self._scan_start) / self._scan_dur

    # ==================================================================
    def run(self):
        self.window.orderFrontRegardless()
        self.app.run()


def _ellipse_path(cx, cy, rx, ry, tilt_deg=0.0, steps=52):
    tilt = math.radians(tilt_deg)
    ct, st = math.cos(tilt), math.sin(tilt)
    p = CGPathCreateMutable()
    first = True
    for i in range(steps + 1):
        a = 2 * math.pi * i / steps
        x = rx * math.cos(a)
        y = ry * math.sin(a)
        wx = x * ct - y * st
        wy = x * st + y * ct
        px, py = cx + wx, cy + wy
        if first:
            CGPathMoveToPoint(p, None, px, py)
            first = False
        else:
            CGPathAddLineToPoint(p, None, px, py)
    CGPathCloseSubpath(p)
    return p


def main():
    test = os.environ.get("JARVIS_HOLO_TEST") or "--test" in sys.argv
    seconds = float(os.environ.get("JARVIS_HOLO_TEST_SECONDS", "3"))
    try:
        holo = JarvisHologram()
    except Exception as exc:
        print(f"NOVA AI HOLOGRAM | FAIL {type(exc).__name__}: {exc}", flush=True)
        sys.exit(1)
    w = holo.window
    print(
        "NOVA AI HOLOGRAM v4 | window="
        f"{int(w.frame().origin.x)},{int(w.frame().origin.y)} "
        f"{int(w.frame().size.width)}x{int(w.frame().size.height)} | "
        f"level={w.level()} ignoresMouse={w.ignoresMouseEvents()} "
        f"contour={len(holo._inner_rings)} rings={len(holo._orbit_rings)}",
        flush=True,
    )
    if test:
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            seconds, False, lambda _t: holo.app.terminate_(None)
        )
    holo.run()


if __name__ == "__main__":
    main()