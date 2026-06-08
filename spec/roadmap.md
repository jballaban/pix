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

## Near-duplicate (perceptual) dedupe — **images** (video done)

**Video** near-dup dedupe is built: `pix dedupe` computes a perceptual
fingerprint (sampled-frame dHash, cached in `cache.db`'s `vfp` column) and
groups re-encoded clips within a Hamming band, gated behind a `--checkout`/
`--commit` review. **Images** still group only by exact, format-aware content
hash, so a photo **re-encoded** at a different quality (visually identical,
byte-different) isn't caught. The remaining work is the image tier-2 pass:
compute a perceptual hash (pHash/dHash), group by Hamming distance, and
**surface** candidates for confirmation (never auto-remove — burst frames are
genuinely different photos). A cheaper interim heuristic: `pix:OriginalPath`-
lineage matching (same source-device basename + capture second). See
[dedupe.md → Known limitations](dedupe.md#known-v1-limitations).

## Cross-format dedupe

A file and its conversion (e.g. a HEIC and the JPG it became) have different
content hashes, so they aren't grouped. Detecting this needs perceptual hashing
or `OriginalPath`-lineage detection — related to the near-duplicate work above.

## Export — `pix export`

Materialize a filtered/derived view of the library (multi-valued tokens like
`{person}`/`{face}`, format/size variants) without disturbing the canonical
tree. Sketched only — see [export.md](export.md).

## Stable collision suffixes (kill ordinal `_NNN` churn)

When two files share a canonical name (same effective second), `plan.py`'s
`_resolve_collisions` gives the primary the bare name and every other a
**positional** `_{idx:03d}` suffix (`_001`, `_002`, …) assigned by sort order
and which slots are already `occupied`. The suffix therefore encodes *rank
within the current colliding set*, not file identity — so adding, removing, or
reordering a sibling reshuffles the assignments, and each shift is a rename.
`organize.py` already carries dedicated cycle/temp-park handling for the
`_001`↔`_002` swap this produces; every shuffle also relocates a cache row and
churns inodes (which perturbs sync clients).

**Idea:** replace the ordinal with a **stable per-file token** — a short
(~5-char) alphanumeric appended once on collision and kept forever, so a file's
canonical name never changes just because a neighbor appeared or left.

Design options / open questions to resolve before building:

- **Where the token comes from.** Strong candidate: **derive it deterministically
  from `pix:OriginalPath`** (already written at first migrate, stable for the
  file's life), e.g. `base32(blake3(OriginalPath))[:5]`. Two files colliding on a
  canonical name almost always have different OriginalPaths → distinct tokens,
  and it's *recomputable* — no new persisted state, no migration of a token
  registry. Alternatives: a random token persisted in a `pix:NameToken` tag
  (authoritative but adds a write), or first-N of the content hash (rejected:
  unknown at rename time, changes on CONVERT).
- **Does the primary keep the bare name?** Keeping it bare is prettiest for the
  common no-collision case, but a tokened sibling could still get *promoted* to
  bare if the primary leaves (one residual rename). Fully churn-free means every
  member of a once-collided set keeps a token forever — at the cost of a lone
  survivor reading `…_a3k9p.jpg` with no visible collision.
- **"Already canonical" recognition.** The canonical-name detector must treat
  `<datetime>_<token>.<ext>` as already-correct-for-this-file (no rename), and
  the token format must be unambiguous vs. a user's `_holiday` or a legacy
  `_001`. Token collisions within one second-bucket must be detected and the
  token extended/salted.
- **Migration of existing `_NNN` files.** Grandfather them (recognize-and-keep,
  assign tokens only to new collisions) vs. a one-time rewrite to tokens
  (one churn now, stable forever).

Touch points: `plan.py:_resolve_collisions` + the canonical-name/`_drop_noop_renames`
recognizer; `organize.py`'s collision/cycle handling can shed its `_NNN`-swap
special-casing once suffixes are stable.

## Smaller items

- **Codec-rank keeper tiebreak** — make dedupe's "original beats transcode"
  explicit (lossless/intra > H.264 > HEVC, below resolution/duration) instead of
  relying on the bitrate proxy. Low priority; see
  [video-redesign.md §0](video-redesign.md). (The former "skip-lean transcode"
  item is obsolete — pix no longer re-encodes video.)
- **Subfolder-scoped hash/organize** — both are library-wide today.

Performance-oriented ideas (against already-built code paths) live in
[perf-backlog.md](perf-backlog.md).
