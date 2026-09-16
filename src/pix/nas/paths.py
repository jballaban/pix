"""Where a derived file lives, and how big it is (spec/nas-app.md §9).

Split out of `derive` so the **app** can answer those two questions without
importing the machinery that *makes* the files. `derive` decodes, so it pulls in
Pillow, pillow-heif, blake3 and the CLI stack; the app decodes nothing, and its
container image ships none of them (see `deploy/Dockerfile`). Importing `derive`
there merely to ask where a render lives failed at `from PIL import Image`
before the first request — a crash loop whose cause sat three modules away from
the line that needed it.

Each function takes its tier root rather than reading one, the way
`derived_path` always has: the root is then whatever the *calling* module holds,
which is the thing tests point at a sandbox.
"""

from __future__ import annotations

from pathlib import Path

#: Long-edge pixels. Both are regenerable, but regenerating 62k files is an
#: afternoon, so they are worth getting roughly right rather than discovering
#: mid-curation.
THUMB_PX: int = 400
#: Sized for the grid's largest setting rather than for looking at one
#: photograph: ~460px cells on a wide screen, doubled for a 2x display, with a
#: little left over. Measured at 106KB median against the real library, where
#: the 1600px preview it replaces there is 221KB.
LARGE_PX: int = 1000
PREVIEW_PX: int = 1600


def derived_path(media: Path, root: Path) -> Path:
    """Where `media`'s derived image lives, mirroring master's folder layout.

    Always `.jpg`: the derived tiers are for looking at, so a HEIC's thumbnail
    and an MP4's poster frame are both just JPEGs.
    """
    return root / media.parent.name / (media.name + ".jpg")


def meta_path(media: Path, root: Path) -> Path:
    """Where `media`'s probed-facts JSON lives, mirroring master's layout."""
    return root / media.parent.name / (media.name + ".json")


def render_path(media: Path, root: Path) -> Path:
    """Where `media`'s playable rendition lives, mirroring master's layout."""
    return root / media.parent.name / (media.name + ".mp4")
