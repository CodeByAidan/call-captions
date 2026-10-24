"""The app's icon, drawn in code so it's crisp at every size and needs no image files."""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPixmap


def icon_pixmap(size):
    s = size / 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.Antialiasing)

    bubble = QPainterPath()
    bubble.addRoundedRect(QRectF(4 * s, 6 * s, 56 * s, 40 * s), 12 * s, 12 * s)
    tail = QPainterPath()
    tail.moveTo(14 * s, 40 * s)
    tail.lineTo(11 * s, 58 * s)
    tail.lineTo(30 * s, 44 * s)
    tail.closeSubpath()
    gradient = QLinearGradient(0, 0, size, size)
    gradient.setColorAt(0, QColor(92, 140, 255))
    gradient.setColorAt(1, QColor(124, 92, 255))
    p.fillPath(bubble.united(tail), gradient)

    # Two caption lines.
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(255, 255, 255, 240))
    p.drawRoundedRect(QRectF(14 * s, 17 * s, 36 * s, 6 * s), 3 * s, 3 * s)
    p.drawRoundedRect(QRectF(14 * s, 29 * s, 24 * s, 6 * s), 3 * s, 3 * s)
    p.end()
    return pixmap


def app_icon():
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(icon_pixmap(size))
    return icon
