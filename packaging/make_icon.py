"""Generate the app icon (assets/bambu.ico) with Pillow.

Run once at build time; the resulting .ico is committed and reused by the tray
and the Windows installer/shortcuts.
"""

import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "assets", "bambu.ico")

GREEN = (0, 174, 66, 255)
DARK = (10, 14, 12, 255)
WHITE = (245, 247, 246, 255)


def draw_icon(size: int) -> Image.Image:
    scale = size / 256.0
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def s(v):
        return int(round(v * scale))

    # Rounded green tile.
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=s(58), fill=GREEN)

    # Printer body.
    d.rounded_rectangle([s(54), s(96), s(202), s(178)], radius=s(16), fill=WHITE)
    # Top paper / spool.
    d.rounded_rectangle([s(78), s(56), s(178), s(104)], radius=s(10), fill=WHITE)
    # Output slot.
    d.rounded_rectangle([s(74), s(120), s(182), s(140)], radius=s(8), fill=DARK)
    # Bottom tray.
    d.rounded_rectangle([s(78), s(186), s(178), s(210)], radius=s(8), fill=WHITE)
    return img


def main():
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [draw_icon(256)]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    images[0].save(OUT, format="ICO", sizes=[(n, n) for n in sizes])
    print("wrote", OUT)


if __name__ == "__main__":
    main()
