"""Build assets/newera.ico (multi-size PNG-in-ICO) from a source PNG using PyQt6 only.

Usage:  python tools/build_icon.py [source.png]
If no source is given (or missing), a vector "N + rising chart" icon is drawn with QPainter.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def draw_fallback(size: int = 1024) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = float(size)
    bg = QLinearGradient(0, 0, s, s)
    bg.setColorAt(0.0, QColor("#1e293b"))
    bg.setColorAt(1.0, QColor("#020617"))
    path = QPainterPath()
    path.addRoundedRect(QRectF(s * 0.04, s * 0.04, s * 0.92, s * 0.92), s * 0.2, s * 0.2)
    p.fillPath(path, QBrush(bg))
    line = QLinearGradient(0, s, s, 0)
    line.setColorAt(0.0, QColor("#06b6d4"))
    line.setColorAt(1.0, QColor("#10b981"))
    pen = QPen(QBrush(line), s * 0.1, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    pts = [QPointF(s * 0.27, s * 0.75), QPointF(s * 0.27, s * 0.3), QPointF(s * 0.72, s * 0.72), QPointF(s * 0.72, s * 0.27)]
    n = QPainterPath(pts[0])
    for pt in pts[1:]:
        n.lineTo(pt)
    p.drawPath(n)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#fbbf24"))
    p.drawEllipse(QPointF(s * 0.72, s * 0.22), s * 0.075, s * 0.075)
    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba.data())


def write_ico(src: QImage, out: Path) -> None:
    blobs = []
    for sz in SIZES:
        scaled = src.scaled(sz, sz, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        blobs.append((sz, png_bytes(scaled)))
    header = struct.pack("<HHH", 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    entries = b""
    data = b""
    for sz, blob in blobs:
        dim = 0 if sz >= 256 else sz
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        data += blob
    out.write_bytes(header + entries + data)


def cut_out_tile(img: QImage) -> QImage:
    """Crop to the dark rounded tile (drop white JPEG background) and apply a transparent rounded mask."""
    img = img.convertToFormat(QImage.Format.Format_ARGB32)
    w, h = img.width(), img.height()

    def is_bg(x: int, y: int) -> bool:
        c = img.pixelColor(x, y)
        return c.red() > 200 and c.green() > 200 and c.blue() > 200

    cy, cx = h // 2, w // 2
    left = next((x for x in range(w) if not is_bg(x, cy)), 0)
    right = next((x for x in range(w - 1, -1, -1) if not is_bg(x, cy)), w - 1)
    top = next((y for y in range(h) if not is_bg(cx, y)), 0)
    bottom = next((y for y in range(h - 1, -1, -1) if not is_bg(cx, y)), h - 1)
    side = min(right - left, bottom - top)
    if side < min(w, h) * 0.4:  # detection failed -> keep full image
        return img
    inset = max(2, side // 120)  # trim the light rim glow edge
    tile = img.copy(left + inset, top + inset, side - 2 * inset, side - 2 * inset)
    out = QImage(tile.size(), QImage.Format.Format_ARGB32)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    clip = QPainterPath()
    s = float(tile.width())
    clip.addRoundedRect(QRectF(0, 0, s, s), s * 0.2, s * 0.2)
    p.setClipPath(clip)
    p.drawImage(0, 0, tile)
    p.end()
    return out


def main() -> int:
    _app = QGuiApplication(sys.argv[:1])
    ASSETS.mkdir(parents=True, exist_ok=True)
    src_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    img = QImage(str(src_path)) if src_path and src_path.is_file() else QImage()
    if img.isNull():
        img = draw_fallback()
        print("Using vector fallback icon")
    else:
        img = cut_out_tile(img)
    img = img.convertToFormat(QImage.Format.Format_ARGB32)
    side = min(img.width(), img.height())
    img = img.copy((img.width() - side) // 2, (img.height() - side) // 2, side, side)
    img.scaled(256, 256, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation).save(
        str(ASSETS / "newera_icon_256.png")
    )
    img.save(str(ASSETS / "newera_icon_source.png"))
    write_ico(img, ASSETS / "newera.ico")
    print("Wrote", ASSETS / "newera.ico")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
