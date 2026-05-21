# Performance backlog

Suggestions for future migrate-perf passes, ordered roughly by ROI. Not designed in detail — each is an idea + rough impact + rough effort. Land them when the v1 surface is stable and we have real wall-clock numbers from TB-scale runs to attack.

Each item links back to the file it'd primarily touch.

## High impact, low effort

### 1. Stream content hashing instead of `f.read()`-into-memory
`src/pix/content_hash.py` — `hash_jpeg` and `hash_mp4` both slurp the whole file into RAM. For multi-GB videos this can OOM and is unconditionally wasteful.

- `hash_mp4`: seek through box headers, then stream `mdat` payloads in 1 MiB chunks into BLAKE3. Header walk only needs the first dozen-ish bytes of each top-level box.
- `hash_jpeg`: stream (or mmap) and feed BLAKE3 in chunks while scanning marker-by-marker.

### 2. Native-speed JPEG marker scan
`src/pix/content_hash.py` — `hash_jpeg` is a Python byte-at-a-time loop. Replace the inner loop with `data.find(b"\xff", i)` to jump between marker candidates; only the marker-boundary handling stays in Python. Combined with #1 this should be ~10× on a 5 MB JPEG.

### 3. Combine TAG sidecar export + write in one ExifTool round-trip
`src/pix/apply.py:_apply_tag`, `src/pix/exiftool_session.py` — currently two `-execute` calls per TAG line (`export_xmp_sidecar` then `write_tags`). ExifTool can do both in one `-execute`. At TB-scale, TAG is the bulk of work; halving the per-line round-trip count is a meaningful chunk.

### 4. Restrict the bulk metadata read to the tags we actually consume
`src/pix/metadata.py:build_cache` — passes no `-TagName` filter, so ExifTool returns every readable tag. The cache only consumes a finite set (pix:\*, the DateAuto candidate fields, face regions, file basics — listed in `spec/migrate.md` → Metadata cache). Explicit `-EXIF:DateTimeOriginal -XMP:DateCreated ...` flags shrink the JSON payload and parse time, often 10×+ on libraries with rich XMP/MakerNotes. Also reduces `FileMetadata.raw` memory footprint at the same time.

## Medium impact / structural

### 5. Apply-phase parallelism
`src/pix/apply.py` (and `spec/README.md` open decision) — apply is sequential. Plan lines are independent. `ProcessPoolExecutor` for CONVERT + hash (CPU-bound), funnel TAG writes through one persistent ExifTool session (I/O-bound; can be N parallel sessions if needed). Most of the wall-clock win for first-migrate of a large folder lives here.

### 6. Persistent metadata cache
Already flagged in `spec/migrate.md` → "Future". Under `.pix/cache/`, keyed on `(path, size, mtime_ns)`. Skips the bulk read for files known-clean. Highest ROI on the 2nd through Nth run of the same folder, not the first.

### 7. Combine `ffprobe` + `ffmpeg` for video convert
`src/pix/convert.py:convert_to_mp4` spawns ffprobe (~100 ms) before ffmpeg (~100 ms) for every video. Two paths:
- (a) Drop ffprobe entirely; let ffmpeg try `-c copy` and fall back to re-encode on failure.
- (b) Probe codec during plan-gen via batched ffprobe, store on the PlanLine. Codec is metadata that fits naturally in the cache.

### 8. Skip the ExifTool metadata-copy pass for JPEG CONVERT
`src/pix/apply.py:_apply_convert` — Pillow can write EXIF directly via `img.save(..., exif=...)`. The post-convert ExifTool `-tagsFromFile` rewrite becomes XMP/IPTC-only and cheaper (and avoids re-reading the just-written JPEG). Adds JPEG-specific complexity; only worth it if HEIC/PNG → JPEG dominates wall-clock.

## Smaller / opportunistic

### 9. Skip `Path.resolve()` per cache entry
`src/pix/metadata.py:parse_exiftool_json` — `resolve()` stats each path on Windows. For 500k files this adds up. Strings from ExifTool's `SourceFile` are already absolute and normalized; a string-normalize is enough.

### 10. Combine the cleanup-pass walks with the source walk
`src/pix/cleanup.py` + `src/pix/scan.py` — currently three separate recursive walks (`**/*.__migrate__.*`, `**/*_exiftool_tmp`, `walk_source_files`) before the ExifTool bulk read does a fourth. One walk that classifies each entry into all four buckets at once cuts directory-traversal cost ~4×.

### 11. Trim `FileMetadata.raw` to consumed fields
`src/pix/metadata.py:FileMetadata` — keeps the full parsed dict alive per file. Disappears for free if #4 lands. Otherwise: project to a slim struct after parsing.

## Non-perf items parked here

### 12. Config evolution: existing libraries don't pick up new default extensions
`src/pix/config.py` + `src/pix/commands/init.py` — `DEFAULT_CONFIG_YAML` is only written on first `pix init`. When we add a new extension to the default (e.g. `mts` in v0.1.12), existing libraries' `.pix/config.yaml` is stale and the new extension fails the unknown-extension check. Surfaced when migrating `G:\pix` after the mts addition. Possible directions: (a) merge-on-read — when loading config, log any default extensions missing from the user's file and suggest the additions; (b) `pix config sync` command that diff/merges; (c) leave it manual and just document the change in release notes. No design pick yet.
