"""Render the app's home-screen icons (spec/nas-app.md §8).

    uv run python tools/make_icons.py

Writes PNGs into `src/pix/nas/icons/`, which are **committed**. They have to be
pre-rendered: the app's container ships without Pillow on purpose — it never
decodes anything, because `process` already made everything it displays — so
there is nothing on the NAS that could draw them on demand.

Drawn from the same geometry as `web._logo_mark` rather than by rasterising its
SVG, which would mean a renderer this project does not otherwise need. Keeping
them in one script means the logo can change in one place and be re-emitted,
instead of being traced by hand at four sizes.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

#: The logo's own coordinate space, matching the `viewBox` in `web.py`.
BOX: float = 28.0

TILE = "#14161a"
PANEL = "#1b1e24"
FG = "#e7e9ee"
SUN = "#e3b341"
HORIZON = "#6aa3ff"

#: Drawn this many times too big and then reduced, which is the whole of the
#: antialiasing — Pillow's shapes have hard edges, and at 192px a hard-edged
#: diagonal horizon looks like a staircase.
OVERSAMPLE: int = 8


def _mark(size: int, *, inset: float, rounded: bool) -> Image.Image:
    """The logo at `size` px: a photograph on a tile.

    `inset` is how much of the canvas the mark occupies — a maskable icon has
    to survive the launcher cropping it to a circle, so it keeps well clear of
    the edges, while an ordinary one fills more of its square.
    """
    px = size * OVERSAMPLE
    im = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    radius = px * 0.22 if rounded else 0
    if radius:
        d.rounded_rectangle((0, 0, px - 1, px - 1), radius=radius, fill=TILE)
    else:
        d.rectangle((0, 0, px - 1, px - 1), fill=TILE)

    # The mark's own 28-unit space, centred and scaled into the inset area.
    span = px * inset
    unit = span / BOX
    ox = oy = (px - span) / 2

    def at(x: float, y: float) -> tuple[float, float]:
        return (ox + x * unit, oy + y * unit)

    stroke = max(1, round(1.6 * unit))

    x0, y0 = at(4, 6)
    x1, y1 = at(24, 22)
    d.rounded_rectangle((x0, y0, x1, y1), radius=3 * unit,
                        fill=PANEL, outline=FG, width=stroke)

    cx, cy = at(9.2, 11)
    r = 1.8 * unit
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=SUN)

    d.line([at(5.6, 20.4), at(11, 14.6), at(14.4, 18.2),
            at(17.2, 15.4), at(22.4, 20.8)],
           fill=HORIZON, width=stroke, joint="curve")
    # Round caps, which `line` does not draw: a dot at each end of the run.
    for point in (at(5.6, 20.4), at(22.4, 20.8)):
        half = stroke / 2
        d.ellipse((point[0] - half, point[1] - half,
                   point[0] + half, point[1] + half), fill=HORIZON)

    return im.resize((size, size), Image.Resampling.LANCZOS)


#: What each icon is for, and why it is shaped the way it is.
ICONS: tuple[tuple[str, int, float, bool], ...] = (
    # The two sizes every installable app is expected to offer.
    ("icon-192.png", 192, 0.74, True),
    ("icon-512.png", 512, 0.74, True),
    # Maskable: Android crops this to whatever shape the launcher uses, so the
    # mark sits well inside the circle that is guaranteed to survive, and the
    # background runs to the edges rather than ending in a rounded corner that
    # would be cropped into a notch.
    ("icon-maskable-512.png", 512, 0.56, False),
    # iOS applies its own rounded mask and does not like transparency, so this
    # one is square and full-bleed too.
    ("apple-touch-icon.png", 180, 0.72, False),
)


def main() -> None:
    out = Path(__file__).resolve().parent.parent / "src" / "pix" / "nas" / "icons"
    out.mkdir(parents=True, exist_ok=True)
    for name, size, inset, rounded in ICONS:
        _mark(size, inset=inset, rounded=rounded).save(out / name, "PNG")
        print(f"{name}  {size}x{size}")


if __name__ == "__main__":
    main()
