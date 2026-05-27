# Performance backlog

Suggestions for future migrate-perf passes, ordered roughly by ROI. Not designed in detail — each is an idea + rough impact + rough effort. Land them when the v1 surface is stable and we have real wall-clock numbers from TB-scale runs to attack.

Each item links back to the file it'd primarily touch.

## High impact, low effort

### 1. Stream content hashing instead of `f.read()`-into-memory
`src/pix/content_hash.py` — `hash_jpeg` and `hash_mp4` both slurp the whole file into RAM. For multi-GB videos this can OOM and is unconditionally wasteful.

- `hash_mp4`: seek through box headers, then stream `mdat` payloads in 1 MiB chunks into BLAKE3. Header walk only needs the first dozen-ish bytes of each top-level box.
- `hash_jpeg`: stream (or mmap) and feed BLAKE3 in chunks while scanning marker-by-marker.

### 2. Native-speed JPEG marker scan — **done in v0.1.77**

`hash_jpeg` was hashing JPEGs byte-at-a-time in Python — at 100 ms/MB it was the dominant cost of `pix hash`. Telemetry on a 472-file sample showed throughput stuck at 3.5 files/sec with CPU at 3% and I/O at 1%, confirming we were spending all the time in interpreter overhead, not on any resource. Replaced the per-byte loop with `data.find(b"\xff", i)` to skip across entropy-coded scan-data runs in C and feed them to BLAKE3 in one `update()` call. Byte-for-byte identical hash output (same updates, just batched) so existing cache entries stay valid. Marker-boundary handling is unchanged.

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

## Items added since the original list

### 13. Cache prune for orphaned `.cache` files
The per-file metadata cache (`pix.metadata_cache.PerFileCache`) writes a sidecar at `<library>/.pix/cache/<absolute-path-mirror>.cache` for each file ExifTool reads. Cache mutations during apply (rename/remove/etc.) are best-effort; on failure the cache file may be left behind even after its media file goes away. Over time these accumulate.

Doable cheaply: organize and dedupe already walk the full library; an additional pass would compare the cache tree to the file tree and `unlink()` any `.cache` file whose corresponding media doesn't exist. Could be implicit (each organize/dedupe sweep) or explicit (`pix cache prune` command).

For migrate, can't auto-prune because migrate's walk only covers a source folder; library files outside that source might be falsely flagged.

Storage impact today is tiny (a few KB per orphan, sub-MB even at TB scale), so this isn't urgent.

### 14. Trim cached metadata to consumed fields (also #4 above, deferred again)
The per-file cache currently stores ExifTool's full JSON output per file (5–10 KB of tags, MakerNotes, etc., most of which pix never reads). Filtering at the cache layer to just the consumed fields (pix:\*, DateAuto candidates, face regions, file basics) would cut cache size 10–100× and speed up parse on cache hits.

Independent of upstream item #4 (which is about restricting what ExifTool *returns*); this is about what we *store* even if ExifTool gave us everything.

### 21. Hardware-accelerated video encode (NVENC / Quick Sync / AMF)
**Phase:** CONVERT — specifically the libx265 re-encode path for non-H.264/HEVC sources (MPG, VOB, exotic AVI, etc.). Re-mux on H.264/HEVC sources is already I/O-bound; this is only about the genuine re-encode cases.

After the codec-detection bug fix (v0.1.56), the vast majority of MOVs re-mux via `-c copy` (sub-second, I/O-bound). The remaining re-encode workload is small for a personal library — mostly older camcorder formats (MPG/MPEG/VOB) plus the occasional non-H.264 AVI. Whether GPU helps depends on how much of that you actually have.

ffmpeg options on Windows:
- **`hevc_nvenc`** (NVIDIA): ~10× faster than libx265, ~10–20% larger files for equivalent quality.
- **`hevc_qsv`** (Intel iGPU): ~5–10× faster, similar size penalty.
- **`hevc_amf`** (AMD): ~5–10× faster, similar.

Implementation cost (~200 lines + spec work):
- Detect available encoders via `ffmpeg -hide_banner -encoders | grep hevc_`.
- Config knob in `.pix/config.yaml` to pick `auto` / `cpu` / `nvenc` / `qsv` / `amf`.
- Per-encoder CRF tuning (libx265 `-crf 23` is not equivalent to `nvenc -cq 23`).
- Fallback when GPU encode fails (driver issues, unsupported pixel format).

Quality trade is real: libx265 is the compression gold standard. For an archive pass, the ~15% extra storage for GPU-encoded output might or might not be acceptable. User-configurable, default cpu.

**Verdict:** evaluate after a real run with the codec bug fixed. If post-fix telemetry shows re-encoding is <2% of total CONVERT time, this isn't worth doing. If you have hundreds of MPG/VOB files, it's a big win.

Cheaper alternatives to try first if re-encode time is a problem:
- libx265 `-preset fast` or `-preset faster`: 3-5× speedup, ~5% file size cost, zero engineering.
- Apply-phase parallelism (item #5): N×-speedup on parallel ffmpeg processes, no quality cost.

## Items added 2026-05-25 (read-through review)

Quick code-review wins identified before real telemetry was available. Each is a 1–20 line change with no new abstractions. Phase context is what matters here — a 100× speedup on a phase that's <1% of total runtime is irrelevant. Numbers will sharpen once we have apply.log summary blocks from a real run; re-prioritize then.

### 15. Switch `scan.walk_source_files` from `rglob` → `os.walk` — **done in v0.1.59**

`Path("…").rglob("*")` yielded Path objects requiring `is_file()` + `resolve()` syscalls per entry, and we descended into `.pix/` then filtered. Switched to `os.walk` which yields filenames separately (no per-entry is_file stat) and lets us prune `dirnames` in-place to never descend into `.pix/`. Dropped `resolve()` per entry — caller is expected to have resolved `folder`, and Windows `os.walk` yields canonical NTFS case.

Real-world: walk phase was ~12s on the user's library; expected to drop to a few seconds.

### 16. Drop redundant `path.resolve()` + `path.is_file()` in `metadata.parse_exiftool_json` — **done in v0.1.64**

Dropped both calls. ExifTool emits a `SourceFile` entry only for files it successfully read via the `-@ <listfile>` we provided, so re-validating each result with two stats per file was pure waste. Subsumes the `resolve()` half of item #9.

### 17. `PerFileCache.cache_path_for` re-resolves an already-resolved path — **done in v0.1.60**

Dropped the `media_path.resolve()` call inside `cache_path_for` in both `pix.metadata_cache` and `pix.hash_cache`. Walker (post-v0.1.59) returns absolute canonical paths; the defensive resolve was costing one stat per cache lookup. Bundled with `read_text` → `read_bytes` switch (saves the UTF-8 decode step on the cache file content). User reported the "checking cache" phase ran ~12s before; expected to drop noticeably.

### 23. Pass `-fast2` to the bulk ExifTool read — **done in v0.1.64**

Added `-fast2` to the `_exiftool_bulk_read` subprocess call. ExifTool's `-fast2` skips the JPEG trailer scan (trailing IPTC/Photoshop blocks) and skips MakerNote extraction. pix consumes none of those — date fields live in EXIF IFD0/ExifIFD, our own tags are XMP, video timestamps are QuickTime, plus File:* basics. Trade is invisible at the consumer level; ExifTool docs cite roughly 2× faster JPEG reads. User reported the "reading metadata" phase at ~44s; bundled with #16 expected to drop noticeably.

### 22. Skip the per-file `is_file()` precheck + carry sizes from the walk — **done in v0.1.62**

Previously each `PerFileCache.get()` did three syscalls per file: an `is_file()` stat on the cache path, a `read_bytes()` on the cache JSON, and a `media_path.stat()` to validate size. Two of those three are now gone:

- `is_file()` precheck dropped — `read_bytes()` is attempted directly, `FileNotFoundError` is caught as a miss. Saves one stat per cache hit (the steady-state majority).
- Media file size threaded in from the walk — `walk_source_files` now uses `os.scandir` directly and returns `list[tuple[Path, int]]`. On Windows the DirEntry already carries size from the dirent, so we get it for free during the walk. `cache.get(path, expected_size)` validates against the caller-supplied size instead of stat'ing again.

Net: cache-hit path goes from 3 syscalls to 1 (just the JSON read). User reported the "checking cache" phase at ~17s; expected to drop ~2–3×.

### 18. Throttle `LiveProgress.begin()` per-file render — **done in v0.1.76**

100ms throttle added inside `_render()` itself, so every progress path (begin / advance / background tick) is rate-limited identically. `__exit__` does a `force=True` render so the 100% transition still lands. Per-instance throttle state lives on `LiveProgress`, so no API change at call sites — every command benefits transparently.

Measured: plan-gen on a 63k-file library dropped from 14s consistently to 8s — ~40%, the upper end of the 5–15s estimate. The remaining 8s is real CPU work (date derivation, candidate-set evaluation, collision resolution).

### 19. Drop per-file `plan_log.flush()` in plan-gen
**Phase:** plan-gen.

`plan.py:231` calls `plan_log.flush()` every iteration. Python text-mode flush is userspace→OS (not fsync), but still serializes through I/O on every call. plan.log is throwaway-on-crash anyway — if plan-gen aborts the user just re-runs.

Fix: drop the flush, let block buffering coalesce. Or flush every N lines (~1000) so user can `tail -f plan.log` during long runs.

Expected savings: a few seconds on 200k iterations. Same order as #18.

**Verdict:** worth doing alongside #18 since both are 1-line plan-gen tweaks.

### 20. Double `fp.stat()` in `commands/hash.py` apply loop
**Phase:** hash apply (one full library pass).

Regression introduced in v0.1.55 (telemetry). The Started-line telemetry calls `fp.stat().st_size` for the `size=…` field; the next line then does `st = fp.stat()` again to capture size+mtime for the cache write. Both stats produce the same data — should reuse the first result.

Expected savings: one stat per file in `pix hash`. On a 200k-file library that's 200k stats — maybe 2-5s on a fast disk. Tiny but it's literally a 2-line fix.

**Verdict:** worth fixing as a quick cleanup since I introduced it. Treat as bugfix rather than backlog.

## Items added 2026-05-26 (hash-scan perf pass)

### 26. Apply the dedupe pattern to organize — **done in v0.1.83**

Organize had the same shape as dedupe pre-v0.1.82: two sequential `read_cached_hash` calls per file (prereq refusal + collision-resolution tiebreaker), plus a flashing "Walking library..." LiveProgress on the sub-second walk. Mechanical port:

- commands/organize.py now does a single parallel pass via `read_all_cached_hashes` and threads the `hashes` dict into `generate_plan`.
- `generate_plan` takes `hashes` as a keyword arg and reads it via `hashes.get(p)` for both the no-hash refusal and the collision-tiebreaker lookup. No more `read_cached_hash` import in `pix.organize`.
- `MissingHashesError` is now caught in commands/organize.py with the same shape as the dedupe handler. (Previously it would have surfaced as an uncaught traceback — pre-existing gap.)
- Silent walk replaces the flashing walking line.

Conftest fixture simplified: with both dedupe and organize consuming a precomputed hash dict, the `patched_hash_cache` monkeypatching is gone — the fixture is now just an empty dict tests populate and pass through as `hashes=`.

### 25. Parallelize dedupe's hash reads + dedupe duplicate per-file calls — **done in v0.1.82**

Dedupe was calling `read_cached_hash` sequentially twice per file — once in `require_migrated_with_hashes` (the prereq check) and once in `group_by_hash` (grouping). On a 63k-file library that's 252k sequential syscalls (4 per file). Refactored to:

- Compute the hash map once in commands/dedupe.py via the new `hash_cache.read_all_cached_hashes` primitive (parallel, 32 workers, mirrors `find_missing_hashes`).
- Thread the resulting `dict[Path, str | None]` through to `generate_plan`, which passes it to both `require_migrated_with_hashes` and `group_by_hash`. Neither function does I/O any more — they're pure dict consumers.
- Drop dedupe's flashing "Walking library..." progress line (migrate-pattern silent walk).

`find_missing_hashes` is now a thin wrapper over `read_all_cached_hashes` — single source of truth for parallel hash-cache scans across the hash and dedupe commands.

### 24. Parallelize the hash-cache scan + skip per-file stat via scandir mtime — **done in v0.1.81**

The hash-scan phase (the "are any files missing a cached hash?" pass over the whole library) was sequential and stat-heavy: 63k files × 3 syscalls each (`cache_path.is_file()` + `read_bytes` + `file_path.stat()`) ran ~3 minutes for a no-op pass where every file was already cached. Three changes together:

- **Parallelized via `ThreadPoolExecutor`** (32 workers, matching `metadata.filter_cache_misses`). Each per-file check is one `read_bytes` of a small JSON file — I/O-bound and independent, so concurrent execution on SSD pushes throughput ~10× higher. New `hash_cache.find_missing_hashes` mirrors the `filter_cache_misses` pattern.
- **Dropped `is_file()` precheck.** Try `read_bytes`, catch `FileNotFoundError`. Same pattern `PerFileCache.get` adopted in v0.1.62. Saves one stat per cache hit.
- **Carry `mtime_ns` from the walk.** `walk_source_files` now returns `(Path, size, mtime_ns)` triples — both attributes come free from the scandir DirEntry on Windows. `find_missing_hashes` validates against the walk-provided values instead of stat'ing each media file. Saves the third syscall per file. `filter_cache_misses` / `build_cache` accept the same 3-tuple shape; metadata cache continues to validate on size only.

Combined: 3 syscalls/file → 1 syscall/file, 32× parallel. Expected ~30× total speedup on the scan phase (~3 min → ~6 s on a 63k-file library).

## Non-perf items parked here

### 12. Config evolution: existing libraries don't pick up new default extensions — **superseded in v0.1.20**
Subsumed by the schema-versioning system (see [spec/library.md → Schema versioning](library.md#schema-versioning)). When a future release bumps `SCHEMA_VERSION`, existing libraries get archive-and-reset; users can recover any customizations from `.pix/archive/v<old>/`. The original `mts`-on-existing-libraries problem (v0.1.12) wasn't a schema break — it was just an addition to defaults — so libraries created before v0.1.12 still need the manual edit. From v0.1.20 onward, any change that *requires* updated config will be paired with a `SCHEMA_VERSION` bump.
