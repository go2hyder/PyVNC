"""Draw the PyVNC app icon and export assets/PyVNC.png, .icns (macOS) and .ico (Windows).

    python scripts/make_icons.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parent.parent / "assets"
SIZE = 1024


def draw(size: int = SIZE) -> Image.Image:
    s = size / 1024
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # Background: rounded square with a vertical blue gradient.
    gradient = Image.new("RGBA", (size, size))
    gd = ImageDraw.Draw(gradient)
    top, bottom = (64, 156, 255), (18, 72, 196)
    for y in range(size):
        t = y / (size - 1)
        gd.line([(0, y), (size, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([int(80 * s), int(80 * s), int(944 * s), int(944 * s)],
                                           radius=int(190 * s), fill=255)
    img.paste(gradient, (0, 0), mask)

    d = ImageDraw.Draw(img)
    white = (255, 255, 255, 255)
    # Monitor bezel, screen, and stand.
    d.rounded_rectangle([int(215 * s), int(260 * s), int(809 * s), int(668 * s)], radius=int(44 * s), fill=white)
    d.rounded_rectangle([int(255 * s), int(300 * s), int(769 * s), int(628 * s)], radius=int(16 * s),
                        fill=(30, 40, 70, 255))
    d.polygon([(int(452 * s), int(668 * s)), (int(572 * s), int(668 * s)),
               (int(598 * s), int(760 * s)), (int(426 * s), int(760 * s))], fill=white)
    d.rounded_rectangle([int(360 * s), int(752 * s), int(664 * s), int(792 * s)], radius=int(20 * s), fill=white)

    # A mouse pointer on the screen, the "remote control" hint.
    ox, oy = 470 * s, 360 * s
    arrow = [(0, 0), (0, 200), (48, 154), (84, 236), (118, 220), (82, 140), (146, 140)]
    d.polygon([(int(ox + x * s), int(oy + y * s)) for x, y in arrow], fill=white)
    return img


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    icon = draw()
    icon.save(ASSETS / "PyVNC.png")
    icon.save(ASSETS / "PyVNC.icns")
    icon.save(ASSETS / "PyVNC.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("Wrote", ", ".join(p.name for p in sorted(ASSETS.glob("PyVNC.*"))))


if __name__ == "__main__":
    main()
