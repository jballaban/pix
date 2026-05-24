# Spec-implementation backlog

Spec is ahead of code. This file tracks the gap. Each item is a chunk of spec that's been written but not yet built; landing them brings the implementation back in sync with the spec it claims to follow.

Distinct from [`perf-backlog.md`](perf-backlog.md), which tracks optimizations to *already-implemented* code paths.

Most of these stem from commit `ac3ef4f` — *spec: hash-as-cache, concurrency lock, subprocess hardening*.

## 1. `pix hash` command — new top-level op — **done in v0.1.51**

Implemented the full command per spec/hash.md. `src/pix/commands/hash.py` orchestrates the run; `src/pix/hash_cache.py` owns the read/write API with `(size, mtime_ns)` validity checks and atomic `.hash.tmp` + fsync + rename writes. `prompt_proceed()` was added to `src/pix/editor.py` for the Y/n-only prompt. Library-wide lock acquisition is **not** wired up — that's item #3; for v0.1.51 the command runs without lock protection. Hash compute timeout is **not** wired up either — that's item #4.

Once `pix hash` has been run, `pix dedupe` and `pix organize` (broken by item #2) start working again on libraries with current cache entries.

## 2. Remove `pix:ContentHash` from the metadata payload — **done in v0.1.50**

Stripped all XMP coupling: `PIX_CONTENT_HASH` constant removed, `needs_content_hash` field removed, apply.py no longer computes/writes the hash, `exiftool_config.cfg` namespace trimmed. Dedupe and organize now read from `src/pix/hash_cache.py` (a stub that always returns None until item #1 lands) and refuse upfront with `MissingHashesError` pointing to `pix hash`. Both ops are non-functional in v0.1.50 by design — they become functional when item #1 populates the cache.

## 3. Library-wide lock at `<library>\.pix\lock`

Spec: [`README.md` → Concurrency](README.md#concurrency). Entirely unbuilt; no `library_lock` / `acquire_lock` anywhere.

- New `src/pix/library_lock.py` (or similar) that writes `<pid>\n<op>\n<iso-timestamp>` and exposes a context manager.
- Acquired at the start of `migrate`, `organize`, `dedupe`, future `hash`. Released on clean exit and on KeyboardInterrupt.
- On existing lock: probe PID liveness *and* identify as a `pix` process. Live → refuse with the spec'd message and exit non-zero before any work. Dead → log `cleaning stale lock from PID <N>`, take the lock, proceed.
- `init` and read-only ops do **not** acquire the lock.
- Wire into `commands/migrate.py`, `commands/organize.py`, `commands/dedupe.py`, and the new `commands/hash.py`.

## 4. Subprocess hardening — missing timeouts

Spec: [`implementation.md` → Subprocess hardening](implementation.md#subprocess-hardening). Partially built.

In place:
- ExifTool 30s + reader-thread + `shutdown_flag` (`exiftool_session.py:42-148`).
- ffmpeg re-mux 5min, re-encode 1h, ffprobe 30s (`convert.py:44-47`).

Missing:
- **Pillow JPG encode timeout (60s)** — `convert_to_jpg` in `convert.py:50-65` runs `Image.save()` unbounded. Either thread-wrap with a join-timeout or accept the GIL caveat; spec lists 60s explicitly.
- **Filesystem rename timeout (10s)** — every `ln.abs_path.rename(...)` in `apply.py` (e.g. `apply.py:243`, `apply.py:282`) is unbounded. Catches AV locks and network-FS hangs per spec.
- **Content-hash compute timeout (60s)** — `compute_content_hash` in `content_hash.py:36-51` reads whole-file synchronously with no upper bound. Needed once `pix hash` is the caller.
- **`Interrupted` line in apply.log on SIGINT** — `cli.py:33-37` catches `KeyboardInterrupt` cleanly but doesn't write the interrupt to the active run's `apply.log` as the spec describes.

## 5. Tiered duration format

Spec: [`migrate.md` → Duration format](migrate.md#duration-format). Currently single-tier (integer seconds only).

- `src/pix/progress.py:168` emits `({elapsed}s)` and stops there. Add a `format_duration(seconds)` helper rendering `Xs` / `XmYs` / `XhYmZs` per the spec table.
- Apply across migrate / dedupe / organize / hash progress lines and end-of-phase summaries (summaries may use one decimal under 60s).
