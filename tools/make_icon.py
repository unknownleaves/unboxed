"""
Generates Unboxed's app icon with no third-party libraries.
Outputs:  assets/icon.png (256px), assets/icon.ico (16-256px), assets/icon_{N}.png
Run:      python tools/make_icon.py
"""
import math
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "assets")


def hexc(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


# ---------------- signed distance functions (unit square coords) ---------- #
def sd_round_rect(x, y, x0, y0, x1, y1, r):
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hx, hy = (x1 - x0) / 2 - r, (y1 - y0) / 2 - r
    qx, qy = abs(x - cx) - hx, abs(y - cy) - hy
    return math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - r


def sd_poly(x, y, pts):
    d = float("inf")
    s = 1
    n = len(pts)
    for i in range(n):
        ax, ay = pts[i]
        bx, by = pts[i - 1]
        ex, ey = bx - ax, by - ay
        wx, wy = x - ax, y - ay
        t = max(0, min(1, (wx * ex + wy * ey) / (ex * ex + ey * ey)))
        dx, dy = wx - ex * t, wy - ey * t
        d = min(d, dx * dx + dy * dy)
        c1 = y >= ay
        c2 = y < by
        c3 = ex * wy > ey * wx
        if (c1 and c2 and c3) or (not c1 and not c2 and not c3):
            s = -s
    return s * math.sqrt(d)


def coverage(dist, px):
    # anti-aliased edge, ~1 pixel wide
    v = 0.5 - dist / px
    return 0.0 if v <= 0 else 1.0 if v >= 1 else v


def mix(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


BG_TOP, BG_BOT = hexc("#7B6CFF"), hexc("#3D27D1")
FLAP = hexc("#C9C1FF")
BODY_TOP, BODY_BOT = hexc("#FFFFFF"), hexc("#E6E1FF")
LIP = hexc("#B3A8FF")
ARROW_TOP, ARROW_BOT = hexc("#FFD45C"), hexc("#FF9F1C")
SHADOW = hexc("#1E1170")


def render(size):
    px = 1.0 / size
    rows = []
    left_flap = [(0.26, 0.52), (0.45, 0.52), (0.36, 0.415), (0.14, 0.44)]
    right_flap = [(1 - x, y) for x, y in left_flap]
    arrow_head = [(0.345, 0.355), (0.50, 0.165), (0.655, 0.355)]
    for j in range(size):
        row = bytearray()
        for i in range(size):
            x, y = (i + 0.5) * px, (j + 0.5) * px
            col, a = (0, 0, 0), 0.0

            def over(c, cov):
                nonlocal col, a
                if cov <= 0:
                    return
                na = cov + a * (1 - cov)
                col = tuple((c[k] * cov + col[k] * a * (1 - cov)) / na for k in range(3))
                a = na

            m = 0.04 if size >= 32 else 0.0
            over(mix(BG_TOP, BG_BOT, y), coverage(sd_round_rect(x, y, m, m, 1 - m, 1 - m, 0.225), px))
            if a > 0:
                # soft drop shadow under box
                sh = sd_round_rect(x, y, 0.25, 0.56, 0.75, 0.86, 0.06)
                over(SHADOW, 0.35 * max(0.0, min(1.0, 0.5 - sh / 0.05)) * a)
                over(FLAP, coverage(sd_poly(x, y, left_flap), px) * a)
                over(FLAP, coverage(sd_poly(x, y, right_flap), px) * a)
                ac = mix(ARROW_TOP, ARROW_BOT, (y - 0.17) / 0.45)
                over(ac, coverage(sd_round_rect(x, y, 0.445, 0.30, 0.555, 0.64, 0.02), px) * a)
                over(ac, coverage(sd_poly(x, y, arrow_head) - 0.012, px) * a)
                over(mix(BODY_TOP, BODY_BOT, (y - 0.52) / 0.3),
                     coverage(sd_round_rect(x, y, 0.26, 0.52, 0.74, 0.82, 0.045), px) * a)
                over(LIP, coverage(sd_round_rect(x, y, 0.26, 0.52, 0.74, 0.575, 0.02), px) * a)
                if size >= 48:  # little tape label
                    over(hexc("#D9D3FF"), coverage(sd_round_rect(x, y, 0.42, 0.65, 0.58, 0.72, 0.012), px) * a)
            row += bytes([int(round(c * 255)) for c in col]) + bytes([int(round(a * 255))])
        rows.append(bytes(row))
    return rows


def png_bytes(size, rows):
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main():
    os.makedirs(OUT, exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    pngs = {}
    for s in sizes:
        pngs[s] = png_bytes(s, render(s))
        with open(os.path.join(OUT, f"icon_{s}.png"), "wb") as f:
            f.write(pngs[s])
    with open(os.path.join(OUT, "icon.png"), "wb") as f:
        f.write(pngs[256])
    # ICO with PNG-compressed entries (supported since Windows Vista)
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    dirs, data = b"", b""
    for s in sizes:
        d = pngs[s]
        dirs += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(d), offset + len(data))
        data += d
    with open(os.path.join(OUT, "icon.ico"), "wb") as f:
        f.write(header + dirs + data)
    print("Icons written to", OUT)


if __name__ == "__main__":
    main()
