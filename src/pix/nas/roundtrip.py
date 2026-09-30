"""Files that come back — what pix made, handed out, and is now being given
again (spec/nas-app.md §15, *Round trips*).

A render downloaded from the app, a clip saved to a phone, a still sent to
somebody and sent back: each is a file pix itself made, and importing it would
put a second copy of something already in the archive into master. Two checks,
at the two places a file passes on its way in:

- **At import, by stamp.** Everything pix makes carries `pix:SourceFile` — the
  master it came from — and a clip `pix:ClipId` besides. A file carrying either
  is not landed; its record says it returned, so a device never offers it
  again. This is the cheap check, and it is the one a messaging app defeats by
  stripping metadata.
- **By content hash**, against everything the index holds — every master,
  every render, every clip's files. What the stamp misses, the hash does not,
  for as long as the coded image is untouched; and it catches a copy of an
  original that pix never touched as well, which is §15's duplicate.

**As early as each import allows.** A device import has the file on local
disk the moment it is downloaded, so both checks run there, and a match never
takes a place in staging. A folder import only links its files into staging,
and hashing each one there would read the whole source — all of the old
library, when seeding — so it checks the stamp, which reads a file's head,
and leaves the hash to `upload`, which reads every file anyway to copy it.

**Only as current as the index.** A file uploaded since the last `pix2 index`
is not in it, so a copy of that is an import like any other; the stamp is
what does not wait.

**Only these two stamps.** The seeded library carries older `pix:` tags of its
own (`EventAuto`, `OriginalPath`, …) because the old pipeline wrote them into
every file, and those are originals — seeding them is the whole point. Neither
of these names was ever written by it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

#: How much of a file to look in. Everything pix makes puts its stamp near the
#: front — an MP4's `moov` is moved there (`+faststart`), and a JPEG's XMP is
#: its first segments — so the head is enough, and reading only it keeps the
#: check free beside the copy it sits next to.
HEAD: int = 1 << 20

#: `pix:SourceFile` as XMP writes it — attribute or element — for saying which
#: master a returned file came from. An MP4 keeps its keys apart from their
#: values, so there it is simply *pix*.
_VALUE = re.compile(
    rb'pix:SourceFile(?:="|>)([^"<\x00]{1,400})')


def returned(path: Path) -> str | None:
    """What `path` says pix made it from, if pix made it — `folder/name`
    where that can be read, `pix` where only the stamp can — else None."""
    try:
        with path.open("rb") as fh:
            head = fh.read(HEAD)
    except OSError:
        return None
    if b"pix:SourceFile" not in head and b"pix:ClipId" not in head:
        return None
    found = _VALUE.search(head)
    if found:
        try:
            return found.group(1).decode("utf-8").strip() or "pix"
        except UnicodeDecodeError:
            return "pix"
    return "pix"


def stamp_args(source_file: str, *, clip_id: str | None = None,
               clip_range: str | None = None) -> list[str]:
    """The ffmpeg arguments that stamp an MP4 as pix's. `use_metadata_tags`
    is what lets MP4 carry keys of its own rather than only the handful it
    has names for."""
    args = ["-metadata", f"pix:SourceFile={source_file}"]
    if clip_id:
        args += ["-metadata", f"pix:ClipId={clip_id}"]
    if clip_range:
        args += ["-metadata", f"pix:ClipRange={clip_range}"]
    return args


def known_hashes(index_db: Path) -> dict[str, str] | None:
    """Content hash -> `folder/name` for everything the index holds: masters,
    renders, and clips' own files. None if the index cannot be read — which
    is not a reason to refuse an import, only to catch less in it."""
    import sqlite3

    if not index_db.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{index_db.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        rows = conn.execute(
            "SELECT folder, name, content_hash, render_hash FROM files "
            "WHERE content_hash IS NOT NULL OR render_hash IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    known: dict[str, str] = {}
    for folder, name, content, render in rows:
        for digest in (content, render):
            if digest:
                known.setdefault(str(digest), f"{folder}/{name}")
    return known


def held(path: Path, known: dict[str, str] | None) -> str | None:
    """What in the library `path` is a copy of, by its coded image — or None.

    The metadata-blind hash (`identity.content_hash`), so a copy re-tagged or
    renamed on the way out and back still matches. One re-encoded on the way
    does not, and is an import like any other.
    """
    if not known:
        return None
    from pix.nas import identity

    digest = identity.content_hash(path)
    return known.get(digest) if digest else None


def checker(index_db: Path) -> "Callable[[Path], str | None]":
    """Both checks, for a file just landed on local disk: the stamp first,
    since it is a read of the head, then the hash. The index is read once, when
    this is made, rather than per file."""
    known = known_hashes(index_db)

    def check(path: Path) -> str | None:
        return returned(path) or held(path, known)

    return check
