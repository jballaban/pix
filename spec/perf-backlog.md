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

### 16. Drop redundant `path.resolve()` + `path.is_file()` in `metadata.parse_exiftool_json`
**Phase:** bulk metadata read. On a fresh first-migrate this is potentially the biggest single phase (every file needs ExifTool). On warm runs the persistent cache makes it tiny.

Lines 277–278: `Path(source_file).resolve()` then `path.is_file()`. Both are stats per result. ExifTool *just* successfully read the file — re-validating is pointless. On a 1000-file batch that's 2000 redundant stats; over a 200k-file first-migrate, ~400k stats saved.

`resolve()` half of this is also part of perf-backlog item #9.

Expected savings: a few seconds on a fresh first-migrate. Negligible on warm runs.

**Verdict:** defer. Lump with #9 if/when we touch this code.

### 17. `PerFileCache.cache_path_for` re-resolves an already-resolved path
**Phase:** cache lookup (the "checking cache" progress phase, plus inline lookups from organize/dedupe).

`walk_source_files` resolves every path. `PerFileCache.get()` calls `cache_path_for()` which calls `media_path.resolve()` again. Double resolution per cache lookup.

Trivial fix: assume the caller's path is absolute; only `resolve()` if not. Or expose a `cache_path_for_resolved(path)` variant for hot paths.

Expected savings: one stat per cache lookup. With 200k files and 32 parallel threads, the cache-lookup phase is currently ~10–15s. Maybe 5s saved. Modest.

**Verdict:** defer. Trivial change but lives in a phase that's already fast.

### 18. Throttle `LiveProgress.begin()` per-file render
**Phase:** plan-gen (hot loop: 200k iterations, sub-ms each).

`progress.begin()` calls `_render()` synchronously every call. On a 200k-file plan-gen that's 200k `\r`-style writes to stderr. On Windows console subsystem, each write is ~50µs — total ~10s of overhead in a phase that should be CPU-bound.

The 1s background ticker already refreshes the display; the per-begin render is redundant for fast phases where individual paths flicker too fast to read. For apply (slow per-line actions) the per-begin render is what makes the path visible immediately on a new action, so keep it there.

Cleanest fix: throttle inside `_render()` itself — skip if last render was <50ms ago. Doesn't change API; helps every caller transparently.

Expected savings: ~5–15s on plan-gen for large libraries.

**Verdict:** worth doing — small change, real-world win on the plan-gen phase the user sees as a single long bar.

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

## Non-perf items parked here

### 12. Config evolution: existing libraries don't pick up new default extensions — **superseded in v0.1.20**
Subsumed by the schema-versioning system (see [spec/library.md → Schema versioning](library.md#schema-versioning)). When a future release bumps `SCHEMA_VERSION`, existing libraries get archive-and-reset; users can recover any customizations from `.pix/archive/v<old>/`. The original `mts`-on-existing-libraries problem (v0.1.12) wasn't a schema break — it was just an addition to defaults — so libraries created before v0.1.12 still need the manual edit. From v0.1.20 onward, any change that *requires* updated config will be paired with a `SCHEMA_VERSION` bump.
