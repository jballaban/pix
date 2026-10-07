"""Where the web app reads and writes — every root, in one place.

**Read as `webroots.X` at the moment of use, never imported by name.** The web
app is being split from one module into several, and a name imported into
each (`from ... import MASTER_DIR`) is a separate binding in each: a test that
redirects one of them leaves the others pointing at the real share. One module
holding the roots, looked up when needed, is one thing to redirect — and the
test guard (`tests/conftest.py`) sandboxes it like everything else.

The values are the share's (`pix.nas.const`); this module is only where the
web app looks them up.
"""

from __future__ import annotations

from pathlib import Path

from pix.nas import const

#: The index the app reads, and writes single rows of.
DB_PATH: Path = const.INDEX_DB
MASTER_DIR: Path = const.MASTER_DIR
META_DIR: Path = const.META_DIR
THUMB_DIR: Path = const.THUMB_DIR
PREVIEW_DIR: Path = const.PREVIEW_DIR
LARGE_DIR: Path = const.LARGE_DIR
RENDER_DIR: Path = const.RENDER_DIR
STRIP_DIR: Path = const.STRIP_DIR
LOG_DIR: Path = const.LOG_DIR
