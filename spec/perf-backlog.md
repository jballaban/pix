# Performance ideas

Optimization ideas against already-built code paths — each an idea + rough
impact + rough effort, not designed in detail. Land them when there are real
wall-clock numbers from large runs to attack. Each notes the file it'd touch.

These are *performance* refinements; designed-but-unbuilt *features* live in
[roadmap.md](roadmap.md).

## Higher impact

### Apply-phase parallelism
`src/pix/apply.py` — apply is sequential, but plan lines are independent. A
process pool for CONVERT + hash (CPU-bound) and N persistent ExifTool sessions
for TAG writes (I/O-bound) is where most of the first-migrate wall-clock win
lives. (Tracked as an open decision in [README.md](README.md).)

### One ExifTool round-trip per TAG line
`apply.py:_apply_tag` + `exiftool_session.py` — a TAG currently makes two
`-execute` calls (sidecar export, then write). ExifTool can do both in one. TAG
is the bulk of work at scale, so halving the round-trips is meaningful.

## Smaller / opportunistic

### Skip the ExifTool metadata-copy pass for JPEG CONVERT
`apply.py:_apply_convert` — Pillow can write EXIF directly via `img.save(...,
exif=...)`, reducing the post-convert ExifTool rewrite to XMP/IPTC only. Worth
it only if HEIC/PNG/DNG → JPEG dominates wall-clock.

### One walk instead of several
`src/pix/cleanup.py` + `src/pix/scan.py` — the cleanup marker globs and the
source walk are separate recursive traversals before the ExifTool bulk read
walks again. One classifying walk would cut traversal cost.

### Drop the per-file `plan_log.flush()` in plan-gen
`plan.py` flushes plan.log every iteration; plan.log is throwaway-on-crash, so
block buffering (or flush every ~1000 lines) is fine and saves a few seconds on
200k iterations.

## Considered and dropped

- **Streaming content hashing** (`content_hash.py` `hash_jpeg`/`hash_mp4` load the
  whole file into RAM). Memory is not a constraint on the dev box (128 GB), and
  the I/O saving is marginal — `mdat` is the bulk of a video, so streaming would
  still read nearly all the bytes; only `moov` (tens of MB) is skipped. The
  rewrite's real cost is a digest-parity property test to guard against
  cache-invalidating drift, with no offsetting win. If many simultaneous
  multi-GB hashes ever pressure RAM, cap hashing concurrency rather than
  rewriting the hasher.
