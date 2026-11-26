"""Draw packaging/windows/devmem.ico: the app's logo (ui/app.css `.logo`, a rounded square filled with a conic gradient
violet -> cyan -> violet starting at 210 degrees). Standard library only; the output is committed, so this only runs
when the icon should change:  python packaging/windows/make_icon.py

Sizes 16-48 are stored as 32-bit DIBs (read by every Windows version and by Inno Setup), 256 as PNG (the usual form).
"""
from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path

VIOLET = (0x8B, 0x7C, 0xF6)
CYAN = (0x38, 0xD6, 0xE8)
SIZES = (16, 24, 32, 48, 256)
SAMPLES = 4          # supersampling per axis, for smooth edges
OUT = Path(__file__).resolve().parent / "devmem.ico"


def _color(x: float, y: float) -> tuple[float, float, float]:
    """Conic gradient around the centre (0.5, 0.5): angle clockwise from the top, 210 degrees = violet."""
    ang = (math.degrees(math.atan2(x - 0.5, 0.5 - y)) - 210) % 360 / 360
    t = 1 - abs(2 * ang - 1)          # 0 violet, 1 cyan, back to 0
    return tuple(a + (b - a) * t for a, b in zip(VIOLET, CYAN))  # type: ignore[return-value]


def _inside(x: float, y: float, r: float) -> bool:
    """Inside the rounded square [0,1]^2 with corner radius r."""
    cx = min(max(x, r), 1 - r)
    cy = min(max(y, r), 1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def render(size: int) -> list[tuple[int, int, int, int]]:
    """RGBA pixels, top row first. A 1-pixel transparent margin at small sizes keeps the shape crisp."""
    margin = 1 if size <= 32 else size / 32
    inner = size - 2 * margin
    r = 0.3                                   # 16px radius on a 54px logo in the app
    px = []
    for j in range(size):
        for i in range(size):
            acc = [0.0, 0.0, 0.0]
            hit = 0
            for sj in range(SAMPLES):
                for si in range(SAMPLES):
                    x = (i + (si + 0.5) / SAMPLES - margin) / inner
                    y = (j + (sj + 0.5) / SAMPLES - margin) / inner
                    if 0 <= x <= 1 and 0 <= y <= 1 and _inside(x, y, r):
                        c = _color(x, y)
                        acc = [a + b for a, b in zip(acc, c)]
                        hit += 1
            if hit:
                px.append((round(acc[0] / hit), round(acc[1] / hit), round(acc[2] / hit),
                           round(255 * hit / SAMPLES ** 2)))
            else:
                px.append((0, 0, 0, 0))
    return px


def png(size: int, px) -> bytes:
    raw = b"".join(b"\x00" + bytes(c for p in px[row * size:(row + 1) * size] for c in p) for row in range(size))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def dib(size: int, px) -> bytes:
    """BITMAPINFOHEADER + BGRA rows bottom-up + a 1-bit AND mask (all 0: the alpha channel decides)."""
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    rows = b"".join(bytes(c for r, g, b, a in px[row * size:(row + 1) * size] for c in (b, g, r, a))
                    for row in reversed(range(size)))
    mask_row = ((size + 31) // 32) * 4
    return header + rows + b"\x00" * (mask_row * size)


def build() -> bytes:
    images = []
    for s in SIZES:
        px = render(s)
        images.append((s, png(s, px) if s >= 256 else dib(s, px)))
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for s, data in images:
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return out + b"".join(d for _, d in images)


if __name__ == "__main__":
    OUT.write_bytes(build())
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    sys.exit(0)
