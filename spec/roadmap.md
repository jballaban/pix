# Roadmap

Capabilities that are designed (and in some cases sketched in the specs) but
**not yet built**. Code is the source of truth for what exists today; this is
what's intended to come.

The old CLI pipeline's roadmap — rollback, tag checkout removal, stable
collision suffixes, `pix export` — went with it on 2026-10-07 (commit
`e1f3853` has it). What follows is for the NAS architecture
([nas-app.md](nas-app.md)).

## Delivery trees — the NAS app's distributions

Designed in [nas-app.md §7](nas-app.md#7-distributions), not built: standing
trees on the NAS (`/tv`, `/book`; no `/photo` — the app is the only viewer) kept in step
with curation — copies with metadata baked in, canonical names assigned once,
drift reported rather than stopped. Needs a design pass first: §7 was written
when curation was a `tier`, and it is now an `audience`.

The old CLI's export engine (desired-set diff, per-tree manifest, drift stops
rather than guesses), its tag-filter grammar, and the requirement that video
ship as H.264 were all meant to carry over into this; they are at commit
`e1f3853` in `src/pix/export.py`, `src/pix/tag_filter.py` and `spec/export.md`.

Clips join it with no new concepts ([clips.md](clips.md) §7): a clip is
delivered like any video, from its cut or its playback render, with its own
effective date written into the copy; and because its footage can change when
its range does, its delivered copy is replaced in place, under the same name,
so anything that refers to it by path keeps the entry.

## Clean downloads — what is left

Photo and video downloads carry nothing identifying
([metadata-cleanup.md](metadata-cleanup.md)). Still open there (§8): video
playback (`/media`), keeping HDR gain maps, other formats (PNG, `.insv`,
HEIC), *Original path* in the details panel, and distributions.

## Near-duplicate grouping — image perceptual hashing

Most of a burst is one photo shot eight times; grouping them is the biggest
lever in curation ([nas-app.md §8](nas-app.md#8-the-app)). Images match today
only by content hash, so a photo re-encoded at a different quality is not
caught. The work is a perceptual hash (pHash/dHash) per image, grouped by
Hamming distance and **surfaced** for confirmation — never acted on alone,
since burst frames are genuinely different photos. The old CLI's video
fingerprinting (`src/pix/video_fingerprint.py` at `e1f3853`) is the starting
point for video.

Performance-oriented ideas (against already-built code paths) live in
[perf-backlog.md](perf-backlog.md).
