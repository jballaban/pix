# Roadmap

Capabilities that are designed (and in some cases sketched in the specs) but
**not yet built**. Code is the source of truth for what exists today; this is
what's intended to come.

## Rollback — `pix rollback <run-id>`

Every destructive run conserves what it replaces into `runs/<run-id>/` (CONVERT/
DELETE originals, TAG sidecars, dedupe captures, organize move records). A
rollback command would read a run's `plan.txt` + captures and reverse it:
restore originals, undo renames/moves, revert tag writes. The data needed is
already guaranteed present per the conservation law; only the command is
missing. Sketched in [migrate.md](migrate.md), [organize.md](organize.md), and
[dedupe.md](dedupe.md).

## Tag editing — removal, blanking, and override review

`pix checkout` currently supports **assigning** a tag (drag a file into a value
folder → set that override). Still to build (see [tag-editing.md](tag-editing.md)):

- **Removing / blanking** a tag (move-up-to-parent; the "set to nothing"
  representation) — commit reports these as skipped today.
- **`pix checkout --overrides`** review mode.
- **Face checkout (`{face}`)** — depends on migrate-time face detection, also
  not yet built.

## Near-duplicate (perceptual) dedupe

`pix dedupe` today groups only by exact, format-aware content hash, so a photo
**re-encoded** at a different quality (visually identical, byte-different) isn't
caught. A planned tier-2 pass would compute a perceptual hash (pHash/dHash),
group by Hamming distance, and **surface** the candidates for confirmation
(never auto-remove — burst frames are genuinely different photos). A cheaper
interim heuristic: `pix:OriginalPath`-lineage matching (same source-device
basename + capture second). See
[dedupe.md → Known limitations](dedupe.md#known-v1-limitations).

## Cross-format dedupe

A file and its conversion (e.g. a HEIC and the JPG it became) have different
content hashes, so they aren't grouped. Detecting this needs perceptual hashing
or `OriginalPath`-lineage detection — related to the near-duplicate work above.

## Export — `pix export`

Materialize a filtered/derived view of the library (multi-valued tokens like
`{person}`/`{face}`, format/size variants) without disturbing the canonical
tree. Sketched only — see [export.md](export.md).

## Smaller items

- **Skip-lean transcode** — don't re-encode already-lean videos where HEVC saves
  little; needs bitrate + dimensions in the video cache. See
  [migrate.md → Canonical video codec](migrate.md#canonical-video-codec).
- **Subfolder-scoped hash/organize** — both are library-wide today.

Performance-oriented ideas (against already-built code paths) live in
[perf-backlog.md](perf-backlog.md).
