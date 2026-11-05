"""Generate the official New Era 2.0 Institutional AI Trading Terminal icon (.png and multi-size .ico)."""

from __future__ import annotations

import struct
from pathlib import Path

from PyQt6.QtCore import QByteArray, QBuffer, QIODevice, QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QRadialGradient,
)


def render_icon_image(size: int = 256) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)

    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

    s = float(size)
    pad = s * 0.05
    rect = QRectF(pad, pad, s - 2 * pad, s - 2 * pad)
    radius = s * 0.22

    # 1. Deep institutional background with subtle radial glow
    bg_grad = QLinearGradient(0, 0, s, s)
    bg_grad.setColorAt(0.0, QColor("#06101e"))
    bg_grad.setColorAt(0.55, QColor("#0b192c"))
    bg_grad.setColorAt(1.0, QColor("#040812"))

    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    p.fillPath(path, bg_grad)

    # 2. Inner cyan/gold ambient glow
    glow = QRadialGradient(s * 0.5, s * 0.42, s * 0.45)
    glow.setColorAt(0.0, QColor(6, 182, 212, 65))
    glow.setColorAt(0.6, QColor(245, 158, 11, 28))
    glow.setColorAt(1.0, QColor(0, 0, 0, 0))
    p.fillPath(path, glow)

    # 3. Outer glowing border (Cyan -> Emerald -> Gold)
    border_grad = QLinearGradient(0, 0, s, s)
    border_grad.setColorAt(0.0, QColor("#38bdf8"))
    border_grad.setColorAt(0.5, QColor("#10b981"))
    border_grad.setColorAt(1.0, QColor("#f59e0b"))
    p.setPen(QPen(border_grad, max(2.0, s * 0.032)))
    p.drawRoundedRect(rect, radius, radius)

    # 4. Draw 3 ascending institutional candlesticks (Gold & Emerald)
    candles = [
        # (x_center_ratio, top_wick, body_top, body_bot, bot_wick, color_hex)
        (0.26, 0.52, 0.58, 0.76, 0.81, "#0ea5e9"),
        (0.43, 0.38, 0.44, 0.68, 0.74, "#10b981"),
        (0.60, 0.22, 0.28, 0.58, 0.65, "#f59e0b"),
    ]
    body_w = s * 0.095
    for cx_r, tw_r, bt_r, bb_r, bw_r, col_hex in candles:
        cx = s * cx_r
        col = QColor(col_hex)
        p.setPen(QPen(col, max(1.5, s * 0.022), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(cx, s * tw_r), QPointF(cx, s * bw_r))
        b_rect = QRectF(cx - body_w / 2.0, s * bt_r, body_w, s * (bb_r - bt_r))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(b_rect, s * 0.018, s * 0.018)

    # 5. Dynamic AI impulse arrow sweeping upward to top-right
    arrow_pen = QPen(QColor("#38bdf8"), max(2.2, s * 0.042), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    p.setPen(arrow_pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    curve = QPainterPath()
    curve.moveTo(s * 0.20, s * 0.74)
    curve.cubicTo(s * 0.38, s * 0.68, s * 0.55, s * 0.48, s * 0.78, s * 0.22)
    p.drawPath(curve)

    # Arrowhead at (0.78, 0.22)
    head = QPolygonF(
        [
            QPointF(s * 0.81, s * 0.19),
            QPointF(s * 0.67, s * 0.22),
            QPointF(s * 0.77, s * 0.33),
        ]
    )
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#38bdf8"))
    p.drawPolygon(head)

    # 6. "NE" Gold Badge at bottom-right
    if size >= 32:
        font = QFont("Segoe UI", max(6, int(s * 0.15)), QFont.Weight.Black)
        p.setFont(font)
        p.setPen(QColor("#f8fafc"))
        text_rect = QRectF(s * 0.44, s * 0.62, s * 0.46, s * 0.26)
        p.drawText(text_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "NE")

    p.end()
    return img


def qimage_to_png_bytes(img: QImage) -> bytes:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba.data())


def build_multi_size_ico(ico_path: Path, png_path: Path) -> None:
    sizes = [256, 128, 64, 48, 32, 16]
    png_blobs: list[tuple[int, bytes]] = []

    for sz in sizes:
        img = render_icon_image(sz)
        if sz == 256:
            png_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(str(png_path), "PNG")
        png_blobs.append((sz, qimage_to_png_bytes(img)))

    # Build standard Windows ICO header + directory + PNG payloads
    count = len(png_blobs)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + count * 16
    dir_entries = bytearray()
    payloads = bytearray()

    for sz, blob in png_blobs:
        w_byte = 0 if sz >= 256 else sz
        h_byte = 0 if sz >= 256 else sz
        entry = struct.pack(
            "<BBBBHHII",
            w_byte,
            h_byte,
            0,      # palette count
            0,      # reserved
            1,      # color planes
            32,     # bits per pixel
            len(blob),
            offset,
        )
        dir_entries.extend(entry)
        payloads.extend(blob)
        offset += len(blob)

    ico_path.parent.mkdir(parents=True, exist_ok=True)
    ico_path.write_bytes(bytes(header) + bytes(dir_entries) + bytes(payloads))


if __name__ == "__main__":
    app = QGuiApplication([])
    root = Path(__file__).resolve().parent
    ico_file = root / "assets" / "newera_icon.ico"
    png_file = root / "assets" / "newera_icon.png"
    build_multi_size_ico(ico_file, png_file)
    print(f"Generated icon: {ico_file} ({ico_file.stat().st_size} bytes) & {png_file} ({png_file.stat().st_size} bytes)")
