# Spec-implementation backlog

Spec is ahead of code. This file tracks the gap. Each item is a chunk of spec that's been written but not yet built; landing them brings the implementation back in sync with the spec it claims to follow.

Distinct from [`perf-backlog.md`](perf-backlog.md), which tracks optimizations to *already-implemented* code paths.

Most of these stem from commit `ac3ef4f` — *spec: hash-as-cache, concurrency lock, subprocess hardening*.

## 1. `pix hash` command — new top-level op — **done in v0.1.51**

Implemented the full command per spec/hash.md. `src/pix/commands/hash.py` orchestrates the run; `src/pix/hash_cache.py` owns the read/write API with `(size, mtime_ns)` validity checks and atomic `.hash.tmp` + fsync + rename writes. `prompt_proceed()` was added to `src/pix/editor.py` for the Y/n-only prompt. Library-wide lock acquisition is **not** wired up — that's item #3; for v0.1.51 the command runs without lock protection. Hash compute timeout is **not** wired up either — that's item #4.

Once `pix hash` has been run, `pix dedupe` and `pix organize` (broken by item #2) start working again on libraries with current cache entries.

## 2. Remove `pix:ContentHash` from the metadata payload — **done in v0.1.50**

Stripped all XMP coupling: `PIX_CONTENT_HASH` constant removed, `needs_content_hash` field removed, apply.py no longer computes/writes the hash, `exiftool_config.cfg` namespace trimmed. Dedupe and organize now read from `src/pix/hash_cache.py` (a stub that always returns None until item #1 lands) and refuse upfront with `MissingHashesError` pointing to `pix hash`. Both ops are non-functional in v0.1.50 by design — they become functional when item #1 populates the cache.

## 3. Library-wide lock at `<library>\.pix\lock` — **done in v0.1.52**

`src/pix/library_lock.py` provides a context-manager `acquire(library_root, op)` that writes `<pid>\n<op>\n<iso-timestamp>` to `<library>/.pix/lock` via O_EXCL + fsync, raises `LockHeld` if a live `pix` process holds it, and stale-cleans (with a stderr notice) if the PID is dead or recycled-to-non-pix. Process liveness uses `psutil.Process(pid).name()`; case-insensitive comparison strips `.exe` so `pix.exe`/`pix` both match. Wired into `migrate`, `organize`, `dedupe`, and `hash` — each command's body was factored into a `_run_<op>(...)` helper invoked under the lock. `init` and read-only ops do not acquire.

## 4. Subprocess hardening — missing timeouts — **done in v0.1.54**

New `src/pix/timeout.py` exposes `OperationTimeout`, `run_with_timeout(name, timeout, func, *args, **kwargs)`, and `safe_rename(src, dst, timeout=10)`. Wired in:

- **Pillow JPG encode (60s)** — `convert.convert_to_jpg` runs `Image.save()` under `run_with_timeout`.
- **Content-hash compute (60s)** — `commands/hash.py` wraps each `compute_content_hash(fp)` call.
- **Filesystem rename (10s)** — every user-data `Path.rename(...)` in `apply.py`, `dedupe.py`, `organize.py`, and `cleanup.py` now goes through `safe_rename`. Internal cache-layer renames (`metadata_cache.py`, `hash_cache.py`) stay best-effort without a timeout.

Policy change (documented in `spec/implementation.md`): **all timeouts halt the run** so the user can investigate what hit the limit. Previously CONVERT timeouts skip-and-logged alongside other CONVERT failures; that's gone — `ConvertFailed` still skips, `OperationTimeout` halts. ffmpeg/ffprobe timeouts now also halt (previously they were `ConvertTimeout(ConvertFailed)` which routed to skip-and-log). Once timeout values are tuned to real workloads we can revisit per-op.

The `Interrupted` line in apply.log on SIGINT was already in place — `apply.py` and `commands/hash.py` both catch `KeyboardInterrupt` per-line, log `Interrupted`, and re-raise.

## 6. Telemetry in apply.log — **done in v0.1.55**

apply.log now records sub-second timestamps, per-line `dur=…` durations, `size=…` for CONVERT/HASH lines, and an end-of-run summary block (per-action p50/p95/max counts plus the top-10 slowest entries). Always-on, no flag. Lets a 10-minute migrate run be analyzed from the summary block alone without parsing 60k transition lines.

Added `src/pix/telemetry.py` (`LineRecord`, `write_summary`) and `format_duration_compact` + `format_size` helpers in `src/pix/duration.py`. Wired into migrate's `apply.apply_plan`, `commands/hash`, `dedupe.apply_plan`, and `organize.apply_plan`.

## 7. `pix checkout` — tag editing via folder-shuffle — **designed, not yet built**

Full design landed in [spec/tag-editing.md](tag-editing.md). Three actions on one command: `pix checkout <path> <template>` (scope to `<path>` and below like `pix migrate <folder>`; materialize a hard-link workspace under `.pix/checkout/` shaped by a compound single-valued template + write `snapshot.json`), `pix checkout --commit` (diff workspace vs. snapshot by NTFS file-ID, write inferred `DateOverride`/`EventOverride` edits as migrate-style TAG lines with XMP conservation capture into a run folder, then tear the workspace down), and `pix checkout --reset` (throw the workspace away). Commit writes **tags only** — no rename/relocate; the library is eventually consistent via the next migrate/organize. Scope bounds the file set but the freeze stays library-wide.

Cross-cutting pieces this needs:
- **The freeze.** Every command except `checkout --commit`/`--reset` must refuse when `.pix/checkout/` exists. New guard, separate from the library lock; wire into `migrate`, `organize`, `dedupe`, `hash`, `upgrade`.
- **Reuse migrate's TAG/DELETE apply path** (per-file ExifTool `-overwrite_original`, `.xmp` sidecar capture, run-folder layout, `apply.log`) and the shared `Apply? [Y/e/n]` editor/confirm loop.
- Prereq check: all files migrated (`pix:OriginalPath`); no hash requirement.
- Face checkout (`{face}`) stays **deferred** with migrate-time face detection.

## 8. Bare `pix organize` re-applies the stored template — **done in v0.1.92**

`organize.template` was previously read only by commit's auto-trigger; commit no longer organizes (see [tag-editing.md](tag-editing.md)), so the key was repurposed: `pix organize <path>` with the template omitted re-applies the stored default shape (errors with guidance if none is stored). `commands/organize.py` makes the template arg optional and falls back to `config.organize_template` before parsing. See [organize.md → Active template persistence](organize.md#active-template-persistence).

## 5. Tiered duration format — **done in v0.1.53**

New `src/pix/duration.py` exposes `format_duration` (integer-tiered for progress-line suffixes) and `format_duration_precise` (one decimal under 60s, for post-phase summaries). `progress.py` consumes `format_duration` for the `(Xs)` suffix; `commands/{migrate,dedupe,organize,hash}.py` consume `format_duration_precise` for the `Found N files in …` / `Read N files in …` / `Plan generated in …` summary lines. The placeholder `_format_duration` that lived in `commands/hash.py` is gone.
