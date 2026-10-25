"""Translucent, click-through caption overlay.

Captions rise in at the bottom of the screen, stay for a few seconds depending on how
long they are, then fade away. Nothing on it can be clicked, so it never gets in the way.
"""

import math
import sys
import time

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetricsF, QGuiApplication,
                           QPainter, QPainterPath, QPen)
from PySide6.QtWidgets import QWidget

FONT_FAMILIES = ("Inter", "SF Pro Display", "Segoe UI Variable Text", "Segoe UI", "Noto Sans", "Cantarell")
TEXT_FLAGS = Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap

FADE_IN = 0.22
FADE_OUT = 0.9
PARTIAL_TIMEOUT = 8.0   # a live caption that never got a final result
STATUS_HOLD = 3.5
STICKY_HOLD = 1e9       # status that stays until replaced (download progress)
PARTIAL_TONE = 0.62     # live (still changing) captions are drawn dimmer
OLDER_TONE = 0.8        # so are captions that aren't the newest one
SMOOTHING = 14.0        # higher = layout animations settle faster
STATUS_DOT = QColor(126, 226, 168)


def _hold_time(text):
    return min(10.0, max(4.0, 2.5 + 0.06 * len(text)))


def _approach(current, target, dt, rate=SMOOTHING):
    return target + (current - target) * math.exp(-rate * dt)


def _smoothstep(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


class _Line:
    def __init__(self, key, text, kind, now):
        self.key = key
        self.text = text
        self.kind = kind  # "partial" | "final" | "status"
        self.born = now
        self.expires = now + (STATUS_HOLD if kind == "status" else PARTIAL_TIMEOUT)
        self.dying_since = None
        self.slot = 0.0   # animated height this line currently takes up in the box
        self.tone = PARTIAL_TONE if kind == "partial" else 1.0
        self.w = self.h = 0.0
        self._measured = None

    def alpha(self, now):
        a = _smoothstep((now - self.born) / FADE_IN)
        if self.dying_since is not None:
            a = min(a, _smoothstep(1.0 - (now - self.dying_since) / FADE_OUT))
        return a


class CaptionOverlay(QWidget):
    def __init__(self, screen=None, max_lines=3):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.X11BypassWindowManagerHint | Qt.WindowTransparentForInput
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        if sys.platform == "darwin":
            self.setAttribute(Qt.WA_MacAlwaysShowToolWindow)  # don't hide when another app is active
        self.screen_ = screen or QGuiApplication.primaryScreen()
        self.max_lines = max_lines
        self.lines = []
        self.box_w = 0.0
        self._painted = None
        self._last_tick = time.monotonic()
        self._timer = QTimer(self, interval=16, timeout=self._tick)
        self.configure()
        if sys.platform == "win32":
            # Windows drops "always on top" when a game goes fullscreen; keep reclaiming it.
            self._topmost = QTimer(self, interval=2000, timeout=self._keep_on_top)
            self._topmost.start()

    def configure(self, position="bottom", scale=1.0, opacity=0.62, font_size=None):
        geo = self.screen_.geometry()
        size = font_size or round(max(18, min(40, geo.height() * 0.024)) * scale)
        family = next((f for f in FONT_FAMILIES if f in QFontDatabase.families()), None)
        self.caption_font = QFont(family) if family else QFontDatabase.systemFont(QFontDatabase.GeneralFont)
        self.caption_font.setPixelSize(size)
        self.caption_font.setWeight(QFont.Weight.Medium)
        self.status_font = QFont(self.caption_font)
        self.status_font.setPixelSize(round(size * 0.62))
        self.status_font.setWeight(QFont.Weight.Normal)
        self.metrics = {"caption": QFontMetricsF(self.caption_font), "status": QFontMetricsF(self.status_font)}

        self.position = position
        self.opacity = opacity
        self.edge_margin = geo.height() * 0.07
        self.pad_x, self.pad_y = size * 0.8, size * 0.45
        self.line_gap = size * 0.3
        self.radius = size * 0.55
        self.max_text_width = min(geo.width() * 0.62, size * 36)

        width = int(self.max_text_width + 2 * self.pad_x + 8)
        height = int(geo.height() * 0.45)
        top = geo.bottom() - height + 1 if position == "bottom" else geo.top()
        self.setGeometry(geo.x() + (geo.width() - width) // 2, top, width, height)
        for line in self.lines:
            line._measured = None
        self._painted = None
        self.update()

    # -- public API (call from the GUI thread) --------------------------------

    def show_partial(self, key, text):
        now = time.monotonic()
        line = self._find(key)
        if line is None:
            self._add(_Line(key, text, "partial", now))
        elif line.kind == "partial":
            line.text = text
            line.expires = now + PARTIAL_TIMEOUT

    def show_final(self, key, text):
        now = time.monotonic()
        line = self._find(key)
        if not text:
            if line:
                self._kill(line, now)
            return
        if line is None:
            line = _Line(key, text, "final", now)
            self._add(line)
        line.text, line.kind = text, "final"
        line.expires = now + _hold_time(text)

    def show_status(self, text, sticky=False):
        now = time.monotonic()
        line = next((l for l in self.lines if l.kind == "status" and l.dying_since is None), None)
        if line is None:
            line = _Line(None, text, "status", now)
            self._add(line)
        line.text = text
        line.expires = now + (STICKY_HOLD if sticky else STATUS_HOLD)

    def clear(self):
        now = time.monotonic()
        for line in self.lines:
            self._kill(line, now)

    # -- internals -------------------------------------------------------------

    def _find(self, key):
        return next((l for l in self.lines if l.key == key and l.kind != "status"
                     and l.dying_since is None), None)

    def _kill(self, line, now):
        if line.dying_since is None:
            line.dying_since = now

    def _add(self, line):
        now = time.monotonic()
        self.lines.append(line)
        alive = [l for l in self.lines if l.dying_since is None]
        for old in alive[:max(0, len(alive) - self.max_lines)]:
            self._kill(old, now)
        if not self._timer.isActive():
            self._last_tick = now
            self._timer.start()

    def _measure(self, line):
        if line._measured != (line.text, line.kind):
            fm = self.metrics["status" if line.kind == "status" else "caption"]
            r = fm.boundingRect(QRectF(0, 0, self.max_text_width, 1e5), TEXT_FLAGS, line.text)
            line.w, line.h = r.width(), r.height()
            if line.kind == "status":
                line.w += fm.height() * 0.75  # room for the dot
            line._measured = (line.text, line.kind)

    def showEvent(self, event):
        super().showEvent(event)
        # Only a real on-screen window has an NSView behind its winId (not "offscreen").
        if sys.platform == "darwin" and QGuiApplication.platformName() == "cocoa":
            self._float_over_fullscreen_apps()

    def _float_over_fullscreen_apps(self):
        # macOS gives fullscreen apps a Space of their own; join every Space, above them.
        try:
            from ctypes import c_void_p

            import AppKit
            import objc

            window = objc.objc_object(c_void_p=c_void_p(int(self.winId()))).window()
            window.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                                          | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
                                          | AppKit.NSWindowCollectionBehaviorStationary
                                          | AppKit.NSWindowCollectionBehaviorIgnoresCycle)
            window.setLevel_(AppKit.NSStatusWindowLevel)
            window.setHasShadow_(False)
            window.setIgnoresMouseEvents_(True)
        except Exception as e:
            print(f"Couldn't raise the overlay above fullscreen apps: {e}", file=sys.stderr)

    def _keep_on_top(self):
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, wintypes.UINT]
        SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
        user32.SetWindowPos(int(self.winId()), wintypes.HWND(-1), 0, 0, 0, 0,
                            SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)

    def _tick(self):
        now = time.monotonic()
        dt = min(0.05, now - self._last_tick)
        self._last_tick = now

        for line in self.lines:
            if line.dying_since is None and now >= line.expires:
                line.dying_since = now
        self.lines = [l for l in self.lines if l.dying_since is None or now - l.dying_since < FADE_OUT]
        if not self.lines:
            self._timer.stop()
            self.box_w = 0.0
            self._repaint_box(None)
            return

        # The last caption fades out in place; older ones also collapse so the box shrinks.
        any_alive = any(l.dying_since is None for l in self.lines)
        newest = next((l for l in reversed(self.lines) if l.kind != "status"), None)
        widths = []
        for line in self.lines:
            self._measure(line)
            collapsing = line.dying_since is not None and any_alive
            line.slot = _approach(line.slot, 0.0 if collapsing else line.h, dt)
            tone = PARTIAL_TONE if line.kind == "partial" else 1.0 if line is newest else OLDER_TONE
            line.tone = _approach(line.tone, tone, dt, 8.0)
            if not collapsing or line.slot > line.h * 0.5:
                widths.append(line.w)
        target_w = max(widths, default=self.box_w)
        self.box_w = target_w if self.box_w == 0 else _approach(self.box_w, target_w, dt)
        self._repaint_box(self._layout()[0])

    def _repaint_box(self, box):
        # Only repaint around the box: the window is mostly empty, and on Windows every
        # repainted pixel of a translucent window costs CPU.
        area = box.toAlignedRect().adjusted(-3, -3, 3, 3) if box else None
        dirty = area.united(self._painted) if area and self._painted else area or self._painted
        self._painted = area
        if dirty:
            self.update(dirty)

    def _layout(self):
        gaps = [self.line_gap * min(1.0, l.slot / l.h) if l.h else 0 for l in self.lines]
        content_h = sum(l.slot for l in self.lines) + sum(gaps[1:])
        w, h = self.box_w + 2 * self.pad_x, content_h + 2 * self.pad_y
        x = (self.width() - w) / 2
        y = self.height() - self.edge_margin - h if self.position == "bottom" else self.edge_margin
        return QRectF(x, y, w, h), gaps

    def paintEvent(self, event):
        if not self.lines or self.box_w == 0:
            return
        now = time.monotonic()
        box_alpha = max(l.alpha(now) for l in self.lines)
        if box_alpha <= 0.003:
            return
        box, gaps = self._layout()

        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        path = QPainterPath()
        path.addRoundedRect(box, self.radius, self.radius)
        p.fillPath(path, QColor(14, 14, 18, round(255 * self.opacity * box_alpha)))
        p.setPen(QPen(QColor(255, 255, 255, round(20 * box_alpha)), 1))
        p.drawPath(path)
        p.setClipPath(path)

        # Stack from the bottom up, newest at the bottom. Arriving lines hang from the top
        # of their slot (so they rise in from below); leaving ones sit on its bottom (so
        # they slide out over the top edge).
        bottom = box.bottom() - self.pad_y
        for line, gap in zip(reversed(self.lines), reversed(gaps)):
            a = line.alpha(now)
            if a > 0.003 and line.slot > 0.5:
                text_top = bottom - line.slot if line.dying_since is None else bottom - line.h
                self._draw_line(p, line, text_top, a)
            bottom -= line.slot + gap
        p.end()

    def _draw_line(self, p, line, top, alpha):
        cx = self.width() / 2
        if line.kind == "status":
            fm = self.metrics["status"]
            dot = fm.height() * 0.32
            text_w = line.w - fm.height() * 0.75
            left = cx - line.w / 2
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(STATUS_DOT.red(), STATUS_DOT.green(), STATUS_DOT.blue(), round(230 * alpha)))
            p.drawEllipse(QRectF(left, top + (fm.height() - dot) / 2, dot, dot))
            p.setFont(self.status_font)
            p.setPen(QColor(255, 255, 255, round(165 * alpha)))
            p.drawText(QRectF(cx - text_w / 2 + fm.height() * 0.375, top, text_w, line.h),
                       TEXT_FLAGS, line.text)
            return
        rect = QRectF(cx - self.max_text_width / 2, top, self.max_text_width, line.h)
        p.setFont(self.caption_font)
        p.setPen(QColor(0, 0, 0, round(150 * alpha)))
        p.drawText(rect.translated(0, 1.5), TEXT_FLAGS, line.text)
        p.setPen(QColor(255, 255, 255, round(250 * alpha * line.tone)))
        p.drawText(rect, TEXT_FLAGS, line.text)
