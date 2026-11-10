"""Draw the iPhone Mirror Plus icon -> iphone_mirror/coredevice/app.ico (+ app.png).

A Windows 11 style tile: rounded square with a blue-violet gradient, a white iPhone in the
middle (Dynamic Island, a lit screen) and a mouse pointer on its lower right corner for
"control it from the PC". Every size is drawn at 8x and scaled down, so small sizes stay
clean.

    python tools/make_plus_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parents[1] / "iphone_mirror" / "coredevice"
SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def lerp(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def gradient(size, c0, c1):
    """Diagonal gradient from top-left c0 to bottom-right c1."""
    g = Image.new("RGBA", (size, size))
    px = g.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * (size - 1))
            px[x, y] = lerp(c0, c1, t) + (255,)
    return g


def draw(size):
    S = size * 8
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # tile
    pad = round(S * 0.04)
    tile = gradient(S, (37, 99, 235), (147, 51, 234))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((pad, pad, S - pad, S - pad), radius=round(S * 0.23), fill=255)
    img.paste(tile, (0, 0), mask)
    # soft highlight on the upper half of the tile
    hl = Image.new("L", (S, S), 0)
    ImageDraw.Draw(hl).ellipse((-S * 0.2, -S * 0.75, S * 1.2, S * 0.45), fill=40)
    hl = hl.filter(ImageFilter.GaussianBlur(S * 0.06))
    hl = Image.composite(hl, Image.new("L", (S, S), 0), mask)
    img.paste(Image.new("RGBA", (S, S), (255, 255, 255, 255)), (0, 0), hl)

    d = ImageDraw.Draw(img)
    # phone body (white) with a shadow
    pw, ph = S * 0.40, S * 0.66
    x0, y0 = (S - pw) / 2 - S * 0.03, (S - ph) / 2
    shadow = Image.new("L", (S, S), 0)
    ImageDraw.Draw(shadow).rounded_rectangle((x0 + S * 0.015, y0 + S * 0.03, x0 + pw + S * 0.015, y0 + ph + S * 0.03),
                                             radius=pw * 0.22, fill=90)
    shadow = shadow.filter(ImageFilter.GaussianBlur(S * 0.02))
    img.paste(Image.new("RGBA", (S, S), (20, 10, 60, 255)), (0, 0), shadow)
    d.rounded_rectangle((x0, y0, x0 + pw, y0 + ph), radius=pw * 0.22, fill=(255, 255, 255, 255))
    # screen
    b = pw * 0.075
    sx0, sy0, sx1, sy1 = x0 + b, y0 + b, x0 + pw - b, y0 + ph - b
    screen = gradient(S, (56, 189, 248), (99, 102, 241))
    smask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(smask).rounded_rectangle((sx0, sy0, sx1, sy1), radius=(pw - 2 * b) * 0.2, fill=255)
    img.paste(screen, (0, 0), smask)
    # dynamic island
    iw, ih = pw * 0.32, pw * 0.085
    cx = (sx0 + sx1) / 2
    d.rounded_rectangle((cx - iw / 2, sy0 + pw * 0.06, cx + iw / 2, sy0 + pw * 0.06 + ih), radius=ih / 2,
                        fill=(15, 15, 30, 255))
    # app tiles on the screen (only where they stay readable)
    if size >= 40:
        cols, rows = 3, 3
        gx0, gy0 = sx0 + (sx1 - sx0) * 0.14, sy0 + (sy1 - sy0) * 0.22
        cell = (sx1 - sx0) * 0.72 / cols
        for r in range(rows):
            for c in range(cols):
                tx, ty = gx0 + c * cell, gy0 + r * cell
                d.rounded_rectangle((tx + cell * 0.12, ty + cell * 0.12, tx + cell * 0.88, ty + cell * 0.88),
                                    radius=cell * 0.2, fill=(255, 255, 255, 150))
    # mouse pointer on the lower right, crossing the phone edge
    k = S * 0.0105
    ax, ay = x0 + pw * 0.80, y0 + ph * 0.60
    arrow = [(0, 0), (0, 22), (5.5, 17), (9.5, 26), (13, 24.5), (9, 15.5), (16, 15.5)]
    pts = [(ax + px * k, ay + py * k) for px, py in arrow]
    d.polygon(pts, fill=(17, 24, 39, 255))  # dark outline
    inner = [(ax + (px * 0.78 + 1.6) * k, ay + (py * 0.78 + 3.4) * k) for px, py in arrow]
    d.polygon(inner, fill=(255, 255, 255, 255))
    return img.resize((size, size), Image.LANCZOS)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    big = draw(256)
    big.save(OUT / "app.png")
    imgs = [draw(s) for s in SIZES]
    imgs[-1].save(OUT / "app.ico", format="ICO", sizes=[(s, s) for s in SIZES], append_images=imgs[:-1])
    print("wrote", OUT / "app.ico")


if __name__ == "__main__":
    main()
