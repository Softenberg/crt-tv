"""Draw the loading-screen test card: a PM5544-style pattern as SVT used to broadcast.

Run on any machine with Pillow:  python tools/make_testcard.py
Writes assets/testcard.png (768x576, PAL). The black box under the centre is left empty:
tv.py draws a live clock into it with ffmpeg's drawtext (see CLOCK_BOX).
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 768, 576
CX, CY = W // 2, H // 2
CELL = 41                      # grid square
R = 6 * CELL                   # circle radius
GREY, WHITE, BLACK = (128, 128, 128), (235, 235, 235), (16, 16, 16)
TOP = CY - R
CLOCK_BOX = (CX - 96, TOP + 8 * CELL + 4, CX + 96, TOP + 9 * CELL - 4)  # keep in sync with tv.py


def font(size):
    for name in ("arialbd.ttf", "DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def background():
    img = Image.new("RGB", (W, H), GREY)
    d = ImageDraw.Draw(img)
    for k in range(-10, 11):  # white crosshatch centred on the picture
        d.rectangle([CX + k * CELL - 1, 0, CX + k * CELL + 1, H], fill=WHITE)
        d.rectangle([0, CY + k * CELL - 1, W, CY + k * CELL + 1], fill=WHITE)
    for x in range(0, W, CELL // 2):  # castellations along the top and bottom edges
        colour = BLACK if (x // (CELL // 2)) % 2 else WHITE
        d.rectangle([x, 0, x + CELL // 2, 14], fill=colour)
        d.rectangle([x, H - 15, x + CELL // 2, H], fill=colour)
    return img


def circle_content():
    """Everything inside the big circle, drawn as horizontal bands."""
    img = background()
    d = ImageDraw.Draw(img)
    left, right = CX - R, CX + R
    top = CY - R

    def band(row, rows=1):
        return top + row * CELL, top + (row + rows) * CELL

    # Reflection check: alternating black/white blocks.
    y0, y1 = band(1)
    for i, x in enumerate(range(left, right, CELL)):
        d.rectangle([x, y0, x + CELL, y1], fill=BLACK if i % 2 else WHITE)

    # Station ident box.
    y0, y1 = band(2)
    d.rectangle([left, y0, right, y1], fill=WHITE)
    d.rectangle([CX - 110, y0 + 3, CX + 110, y1 - 3], fill=BLACK)
    d.text((CX, (y0 + y1) // 2), "SVT 1", font=font(30), fill=WHITE, anchor="mm")

    # Colour bars at 75%.
    y0, y1 = band(3, 2)
    bars = [(191, 191, 191), (191, 191, 0), (0, 191, 191), (0, 191, 0),
            (191, 0, 191), (191, 0, 0), (0, 0, 191), BLACK]
    width = (right - left) / len(bars)
    for i, colour in enumerate(bars):
        d.rectangle([left + i * width, y0, left + (i + 1) * width, y1], fill=colour)

    # Grey scale staircase.
    y0, y1 = band(5)[0], CY - 14
    steps = 6
    width = (right - left) / steps
    for i in range(steps):
        v = 16 + i * (235 - 16) // (steps - 1)
        d.rectangle([left + i * width, y0, left + (i + 1) * width, y1], fill=(v, v, v))

    # Centre line with cross, on the exact centre of the circle.
    y0, y1 = CY - 14, CY + 14
    d.rectangle([left, y0, right, y1], fill=BLACK)
    d.rectangle([left, CY - 2, right, CY + 2], fill=WHITE)
    d.rectangle([CX - 2, y0, CX + 2, y1], fill=WHITE)

    # Frequency gratings (multiburst), coarse to fine.
    y0, y1 = CY + 14, band(7)[1]
    periods = [12, 8, 6, 4, 3]
    width = (right - left - 2 * CELL) / len(periods)
    d.rectangle([left, y0, left + CELL, y1], fill=WHITE)
    d.rectangle([right - CELL, y0, right, y1], fill=BLACK)
    for i, period in enumerate(periods):
        x0 = left + CELL + i * width
        for x in range(int(x0), int(x0 + width)):
            if (x // (period // 2 or 1)) % 2:
                d.line([x, y0, x, y1], fill=WHITE)
            else:
                d.line([x, y0, x, y1], fill=BLACK)

    # Clock band (the clock itself is drawn live by tv.py).
    y0, y1 = band(8)
    d.rectangle([left, y0, right, y1], fill=WHITE)
    d.rectangle(CLOCK_BOX, fill=BLACK)

    # Reflection check again, inverted.
    y0, y1 = band(9)
    for i, x in enumerate(range(left, right, CELL)):
        d.rectangle([x, y0, x + CELL, y1], fill=WHITE if i % 2 else BLACK)
    return img


def main():
    card = background()
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).ellipse([CX - R, CY - R, CX + R, CY + R], fill=255)
    card.paste(circle_content(), (0, 0), mask)
    ImageDraw.Draw(card).ellipse([CX - R, CY - R, CX + R, CY + R], outline=WHITE, width=3)
    out = Path(__file__).resolve().parent.parent / "assets" / "testcard.png"
    out.parent.mkdir(exist_ok=True)
    card.save(out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
