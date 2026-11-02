"""Generate the app icon (.ico) and the browser-extension PNG icons.

Pure Python (no Pillow needed):  python tools/make_icons.py
"""

import os
import struct
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLUE = (26, 115, 232)


def pixel(x, y, size):
    """RGBA of the icon at (x, y): blue disc with a white download arrow."""
    ss = 4  # supersampling for smooth edges
    hits_disc = hits_arrow = 0
    for sy in range(ss):
        for sx in range(ss):
            nx = (x + (sx + 0.5) / ss) / size
            ny = (y + (sy + 0.5) / ss) / size
            dx, dy = nx - 0.5, ny - 0.5
            if dx * dx + dy * dy <= 0.49 * 0.49:
                hits_disc += 1
                shaft = 0.42 <= nx <= 0.58 and 0.18 <= ny <= 0.55
                head = 0.48 <= ny <= 0.76 and abs(nx - 0.5) <= (0.76 - ny) * 1.1
                bar = 0.79 <= ny <= 0.85 and 0.28 <= nx <= 0.72
                if shaft or head or bar:
                    hits_arrow += 1
    total = ss * ss
    if not hits_disc:
        return (0, 0, 0, 0)
    a = hits_disc / total
    w = hits_arrow / hits_disc
    r, g, b = (round(c * (1 - w) + 255 * w) for c in BLUE)
    return (r, g, b, round(255 * a))


def png_bytes(size):
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            raw.extend(pixel(x, y, size))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def write_ico(path, sizes):
    images = [png_bytes(s) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for s, data in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    with open(path, "wb") as f:
        f.write(header + entries + b"".join(images))


if __name__ == "__main__":
    write_ico(os.path.join(ROOT, "assets", "swiftget.ico"), [16, 24, 32, 48, 64, 256])
    for s in (16, 32, 48, 128):
        with open(os.path.join(ROOT, "browser-extension", "icons", f"icon{s}.png"), "wb") as f:
            f.write(png_bytes(s))
    print("icons written")
