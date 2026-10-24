"""Renders docs/preview.png, the README's preview image, with the real overlay code drawn
over a made-up game scene. Run from the repo root:

    QT_QPA_PLATFORM=offscreen .venv/bin/python packaging/render_preview.py
"""

import math
import os
import random
import sys
import time

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen,
                           QRadialGradient)
from PySide6.QtWidgets import QApplication, QWidget

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from overlay import CaptionOverlay  # noqa: E402

W, H, SCALE = 1600, 800, 2
OLDER = "Hey, are you coming to the match tonight?"
NEWEST = "We need one more player, otherwise we'll get randoms again."
SPOKEN = "“Wir brauchen noch einen Spieler, sonst kriegen wir wieder Randoms.”"


class Screen:  # the overlay only asks its screen for a geometry
    def geometry(self):
        return QRect(0, 0, W, H)


def ridge(rng, base, amplitude, detail):
    """A mountain silhouette: a few summed sine waves."""
    waves = [(amplitude / (i + 1) ** 1.1, detail * (i + 1) ** 1.6, rng.uniform(0, 2 * math.pi)) for i in range(5)]
    path = QPainterPath(QPointF(0, H))
    for x in range(0, W + 9, 8):
        path.lineTo(x, base - sum(a * math.sin(x * f + p) for a, f, p in waves))
    path.lineTo(W, H)
    path.closeSubpath()
    return path


def draw_scene(p):
    sky = QLinearGradient(0, 0, 0, H * 0.72)
    for at, color in ((0, "#070a1f"), (0.45, "#24245c"), (0.78, "#a2477a"), (1, "#f0915a")):
        sky.setColorAt(at, QColor(color))
    p.fillRect(QRectF(0, 0, W, H), sky)

    rng = random.Random(7)
    p.setPen(Qt.NoPen)
    for _ in range(170):  # stars, thinning out towards the horizon
        x, y = rng.uniform(0, W), H * 0.5 * rng.random() ** 1.6
        p.setBrush(QColor(255, 255, 255, int(rng.uniform(40, 200) * (1 - y / (H * 0.5)))))
        p.drawEllipse(QPointF(x, y), rng.uniform(0.5, 1.4), rng.uniform(0.5, 1.4))

    sun = QRadialGradient(QPointF(W * 0.68, H * 0.6), W * 0.32)
    sun.setColorAt(0, QColor(255, 214, 160, 235))
    sun.setColorAt(0.08, QColor(255, 180, 120, 170))
    sun.setColorAt(1, QColor(255, 140, 90, 0))
    p.fillRect(QRectF(0, 0, W, H), sun)

    rng = random.Random(3)
    # Mountain ranges, hazier the further away; each fades darker towards its foot.
    layers = ((H * 0.60, 70, 0.004, "#7a4478", "#4f3060"), (H * 0.68, 80, 0.0035, "#4a2e60", "#2f1f48"),
              (H * 0.78, 90, 0.003, "#2a1d44", "#1a1230"), (H * 0.90, 70, 0.0028, "#150e27", "#0c0817"))
    for base, amplitude, detail, top, foot in layers:
        fill = QLinearGradient(0, base - amplitude, 0, H)
        fill.setColorAt(0, QColor(top))
        fill.setColorAt(1, QColor(foot))
        p.fillPath(ridge(rng, base, amplitude, detail), fill)

    vignette = QRadialGradient(QPointF(W / 2, H / 2), W * 0.7)
    vignette.setColorAt(0.55, QColor(0, 0, 0, 0))
    vignette.setColorAt(1, QColor(0, 0, 0, 150))
    p.fillRect(QRectF(0, 0, W, H), vignette)


def draw_speaker(p, font_family):
    """A generic voice-chat 'is speaking' card showing what was actually said."""
    box = QRectF(36, 36, 500, 112)
    path = QPainterPath()
    path.addRoundedRect(box, 16, 16)
    p.fillPath(path, QColor(12, 12, 18, 170))
    p.setPen(QPen(QColor(255, 255, 255, 22), 1))
    p.drawPath(path)

    center = QPointF(box.left() + 44, box.top() + 44)
    p.setPen(QPen(QColor(87, 214, 132), 3))
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(center, 25, 25)
    avatar = QLinearGradient(center.x() - 22, center.y() - 22, center.x() + 22, center.y() + 22)
    avatar.setColorAt(0, QColor("#f5a25d"))
    avatar.setColorAt(1, QColor("#e3557a"))
    p.setPen(Qt.NoPen)
    p.setBrush(avatar)
    p.drawEllipse(center, 21, 21)
    p.setPen(QColor(255, 255, 255))
    p.setFont(font(font_family, 20, QFont.Weight.DemiBold))
    p.drawText(QRectF(center.x() - 21, center.y() - 21, 42, 42), Qt.AlignCenter, "L")

    p.setFont(font(font_family, 19, QFont.Weight.DemiBold))
    p.drawText(QPointF(box.left() + 84, box.top() + 38), "Lukas")
    p.setPen(QColor(255, 255, 255, 140))
    p.setFont(font(font_family, 14))
    p.drawText(QPointF(box.left() + 84, box.top() + 60), "speaking German")
    # Little "voice" bars next to the name.
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(87, 214, 132))
    for i, h in enumerate((8, 15, 11, 17, 7)):
        p.drawRoundedRect(QRectF(box.left() + 150 + i * 6, box.top() + 30 - h / 2 - 6, 3.5, h), 1.75, 1.75)

    p.setPen(QColor(255, 255, 255, 190))
    quote = font(font_family, 14)
    quote.setItalic(True)
    p.setFont(quote)
    p.drawText(QRectF(box.left() + 20, box.top() + 76, box.width() - 40, 30), Qt.AlignVCenter, SPOKEN)


def font(family, px, weight=QFont.Weight.Normal):
    f = QFont(family)
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


def render_overlay():
    overlay = CaptionOverlay(Screen())
    overlay.configure(font_size=32)
    overlay.show_final(1, OLDER)
    overlay.show_final(2, NEWEST)
    for line in overlay.lines:  # skip the fade-in and keep them on screen
        line.born -= 10
        line.expires = time.monotonic() + 1e6
    for _ in range(300):  # let the layout animations settle
        overlay._last_tick = time.monotonic() - 0.05
        overlay._tick()
    return overlay


def main():
    app = QApplication.instance() or QApplication([])
    overlay = render_overlay()
    image = QImage(W * SCALE, H * SCALE, QImage.Format_ARGB32_Premultiplied)
    p = QPainter(image)
    p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing | QPainter.SmoothPixmapTransform)
    p.scale(SCALE, SCALE)
    for draw in (draw_scene, lambda p: draw_speaker(p, overlay.caption_font.family())):
        p.save()  # don't leak pens and brushes into the next drawing
        draw(p)
        p.restore()
    overlay.render(p, QPoint(overlay.x(), overlay.y()), renderFlags=QWidget.RenderFlag.DrawChildren)
    p.end()
    out = os.path.join(ROOT, "docs", "preview.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    image.convertToFormat(QImage.Format_RGB32).save(out, "PNG")
    print(f"wrote {out} ({os.path.getsize(out) // 1024} KB)")
    del app


if __name__ == "__main__":
    main()
