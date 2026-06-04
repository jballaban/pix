"""Checkout/commit review folder for perceptual `pix dedupe`.

`pix dedupe <path> --checkout <dir>` writes, for each proposed duplicate
group, a stacked montage (keeper strip on top, each duplicate below) plus a
machine-readable `manifest.json` and a `_README.txt`. The human curates by
**deleting the montage of any group they don't want touched** — exactly like
deleting a line from a migrate plan.

`pix dedupe --commit <dir>` re-groups the library fresh (so everything is
re-validated against current bytes), keeps only the groups whose montage
still exists, and applies those. Matching fresh groups to surviving montages
is by *member set* (library-relative paths), so a group whose membership
changed since checkout simply won't match and is skipped — safe by default.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from pix.dedupe import DedupeGroup
from pix.video_fingerprint import FRAC


MANIFEST_NAME = "manifest.json"
README_NAME = "_README.txt"

_README = """\
pix dedupe review folder
========================

Each image here is one proposed duplicate group:
  - TOP strip  = the file that would be KEPT (the keeper).
  - strip(s) below = the duplicate(s) that would be REMOVED (conserved to
    the run folder, recoverable).
Each strip shows {n} frames sampled across that clip.

To curate:
  - Keep a montage  -> that group WILL be deduped on commit.
  - DELETE a montage -> that whole group is SKIPPED (nothing removed).

Then run:  pix dedupe --commit "{dir}"

Filenames: gNNNN_dDDD.jpg  (group number _ max perceptual distance).
Lower distance = more certain duplicate. manifest.json is authoritative.
"""


@dataclass(frozen=True)
class ManifestGroup:
    group_id: str
    kind: str
    distance: int
    keeper_rel: str
    member_rels: tuple[str, ...]


def montage_name(group_id: str, distance: int) -> str:
    return f"{group_id}_d{distance:03d}.jpg"


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def write_manifest(
    review_dir: Path,
    library_root: Path,
    id_groups: list[tuple[str, DedupeGroup]],
    min_distance: int,
    max_distance: int,
) -> None:
    """Write manifest.json + _README.txt. `id_groups` pairs each assigned
    group id with its DedupeGroup."""
    groups_json: list[dict[str, object]] = []
    for gid, group in id_groups:
        members = [group.keeper, *group.losers]
        groups_json.append(
            {
                "id": gid,
                "kind": group.kind,
                "distance": group.distance,
                "keeper": _rel(group.keeper, library_root),
                "members": [_rel(m, library_root) for m in members],
            }
        )
    payload = {
        "library_root": str(library_root),
        "created": datetime.now().isoformat(timespec="seconds"),
        "min_distance": min_distance,
        "max_distance": max_distance,
        "groups": groups_json,
    }
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / MANIFEST_NAME).write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    (review_dir / README_NAME).write_text(
        _README.format(n=len(FRAC), dir=review_dir), encoding="utf-8"
    )


def read_manifest(review_dir: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (review_dir / MANIFEST_NAME).read_text(encoding="utf-8")
    )
    return data


def surviving_member_sets(review_dir: Path) -> list[frozenset[str]]:
    """Member sets (library-relative paths) of the groups whose montage is
    still present in `review_dir`. A group whose montage the user deleted is
    omitted, so commit will skip it."""
    manifest = read_manifest(review_dir)
    raw_groups = manifest.get("groups", [])
    survivors: list[frozenset[str]] = []
    if not isinstance(raw_groups, list):
        return survivors
    for g in cast("list[dict[str, Any]]", raw_groups):
        gid = g.get("id")
        dist = g.get("distance")
        members = g.get("members")
        if not (isinstance(gid, str) and isinstance(dist, int)
                and isinstance(members, list)):
            continue
        member_rels = cast("list[Any]", members)
        if (review_dir / montage_name(gid, dist)).exists():
            survivors.append(frozenset(str(m) for m in member_rels))
    return survivors


# --- montage rendering ------------------------------------------------------


def _strip(path: Path, duration: float, out: Path) -> bool:
    """Render one horizontal strip: `len(FRAC)` frames across the clip."""
    pngs: list[Path] = []
    for i, f in enumerate(FRAC):
        fp = out.parent / f"{out.stem}_{i}.png"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-ss", f"{max(0.0, f * duration):.3f}", "-i", str(path),
             "-frames:v", "1", "-vf", "scale=-2:200", "-y", str(fp)],
            check=False, capture_output=True,
        )
        if fp.exists():
            pngs.append(fp)
    if not pngs:
        return False
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    for p in pngs:
        cmd += ["-i", str(p)]
    cmd += ["-filter_complex", f"hstack=inputs={len(pngs)}", "-y", str(out)]
    subprocess.run(cmd, check=False, capture_output=True)
    for p in pngs:
        p.unlink(missing_ok=True)
    return out.exists()


def render_montage(
    review_dir: Path,
    group_id: str,
    distance: int,
    members: list[Path],
    durations: dict[Path, float],
) -> bool:
    """Stack each member's strip (keeper first) into one montage image.

    Best-effort: returns False if no strip could be produced (e.g. files
    moved). The manifest, not the montage, is authoritative — a missing
    montage just means that group won't be offered for commit."""
    strips: list[Path] = []
    for idx, m in enumerate(members):
        s = review_dir / f"{group_id}_strip{idx}.png"
        if _strip(m, durations.get(m, 0.0), s):
            strips.append(s)
    if not strips:
        return False
    out = review_dir / montage_name(group_id, distance)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    for s in strips:
        cmd += ["-i", str(s)]
    # scale every strip to a common width, then stack vertically.
    parts = "".join(f"[{i}:v]scale=1440:-2[s{i}];" for i in range(len(strips)))
    inputs = "".join(f"[s{i}]" for i in range(len(strips)))
    cmd += ["-filter_complex",
            f"{parts}{inputs}vstack=inputs={len(strips)}", "-y", str(out)]
    subprocess.run(cmd, check=False, capture_output=True)
    for s in strips:
        s.unlink(missing_ok=True)
    return out.exists()
