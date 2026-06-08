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

## Smaller / opportunistic

### Skip the ExifTool metadata-copy pass for JPEG CONVERT
`apply.py:_apply_convert` — Pillow can write EXIF directly via `img.save(...,
exif=...)`, reducing the post-convert ExifTool rewrite to XMP/IPTC only. Worth
it only if HEIC/PNG/DNG → JPEG dominates wall-clock.

### Drop the per-file `plan_log.flush()` in plan-gen
`plan.py` flushes plan.log every iteration; plan.log is throwaway-on-crash, so
block buffering (or flush every ~1000 lines) is fine and saves a few seconds on
200k iterations.

## Considered and dropped

- **One ExifTool round-trip per TAG line.** The claim "ExifTool can do both
  (sidecar export + in-place write) in one command" is false: when `-o <sidecar>`
  is present ExifTool creates the sidecar and leaves the source unmodified, so a
  single command can't capture a pre-edit sidecar *and* write the file
  (verified). The only true single-pass alternative — dropping
  `-overwrite_original` so ExifTool keeps a full `file_original` backup —
  replaces the tiny XMP sidecar with a full-file copy, ~doubling run-folder disk
  on a first migrate (where TAG hits every file); rejected. The genuinely
  achievable path (skip the ExifTool sidecar export and conserve the prior
  `pix:*` values pix already read into the cache → one round-trip, zero extra
  reads) changes the conservation artifact and so belongs with the **`pix
  rollback`** design, not as a standalone perf tweak. See [roadmap.md](roadmap.md).
- **Streaming content hashing** (`content_hash.py` `hash_jpeg`/`hash_mp4` load the
  whole file into RAM). Memory is not a constraint on the dev box (128 GB), and
  the I/O saving is marginal — `mdat` is the bulk of a video, so streaming would
  still read nearly all the bytes; only `moov` (tens of MB) is skipped. The
  rewrite's real cost is a digest-parity property test to guard against
  cache-invalidating drift, with no offsetting win. If many simultaneous
  multi-GB hashes ever pressure RAM, cap hashing concurrency rather than
  rewriting the hasher.
