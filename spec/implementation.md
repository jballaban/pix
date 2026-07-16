# Implementation

Language, libraries, runtime constraints, and deployment notes.

## Platform

`pix` runs **native on Windows** (not WSL). The library lives on NTFS and the design depends on NTFS-native atomic rename and `CreateHardLinkW` semantics; running through WSL's DrvFs bridge would be 10-100× slower at TB scale and adds risk to the FS-primitive guarantees.

Language: **Python 3.12+**.

## Tech stack

| Concern | Choice |
|---|---|
| CLI framework | `typer` |
| Image decode/encode | `Pillow` + `pillow-heif` |
| Video remux | `ffmpeg` (subprocess; bundled `.exe` or on PATH) — lossless `-c copy` container normalization, never re-encode |
| Metadata read/write | `ExifTool` (subprocess via `pyexiftool`) — only tool that reliably handles EXIF/XMP/IPTC across photo + video formats including MWG face regions. **Reads:** bulk-extract with `exiftool -j -r -G:1 <folder>` once per migrate, populating an in-memory cache (see [migrate.md → Metadata cache](migrate.md#metadata-cache)). **Writes:** per-file via `-overwrite_original`, using `-stay_open` mode (one long-running ExifTool process per migrate, communicating via stdin/stdout) to avoid the ~200ms-per-spawn overhead. |
| Format-aware content hash (tier 1) | hand-rolled framing: JPEG → strip APP-marker metadata (APP1/EXIF, APP1/XMP, APP13/IPTC, …) and hash the rest; MP4 / ISO BMFF → parse boxes and hash only the concatenated `mdat` payload(s). Hashed with `blake3` (256-bit, hex-encoded). Stored in the SQLite cache store `.pix/local/cache.db` by `pix hash` (see [hash.md](hash.md)). |
| Perceptual hash (tier 2) | `imagehash` (photos), sampled-frame imagehash (videos) |
| Face detection + embedding | `insightface` (ONNX-backed) |
| Identity clustering | `hdbscan` or cosine-similarity threshold |
| Parallelism | `concurrent.futures.ProcessPoolExecutor` (CPU work happens in native extensions so the GIL is not a constraint) |
| Env + lockfile | `uv` |
| Type checking | `pyright` (strict) |
| Tests | `pytest` |

## Subprocess hardening

pix orchestrates several subprocesses (ExifTool, ffmpeg/ffprobe, Pillow via its native libs). Any of them can wedge on a malformed input or a slow disk — a single file can otherwise eat hours of wall time before being killed. Two requirements:

1. **Every subprocess call has a timeout.** If it exceeds the timeout, the wrapper kills the subprocess and raises a typed exception that the calling action handles like any other failure.
2. **CTRL+C works.** A user pressing Ctrl-C while a subprocess is mid-call must interrupt within ~500 ms, kill the subprocess, write a clean log line, and exit non-zero.

### Timeout matrix

Timeouts are per primitive call, not per plan action. A `CONVERT+RENAME+TAG` plan line is a sequence of (Pillow OR ffmpeg) → ExifTool → fs rename, each with its own timeout.

| Primitive | Default | Rationale |
|---|---|---|
| ExifTool `-execute` (read, sidecar export, tag write) | **30s** | Metadata ops should be sub-second; 30s = wedged. Same timeout regardless of whether it's reading or writing — same protocol underneath. |
| Pillow JPG encode (`convert_to_jpg`) | **60s** | A 50 MP JPG re-encodes in seconds. 60s = pathological. |
| ffmpeg remux (`-c copy`, the only video CONVERT) | **5 min** | Should be fast (bytes copy + container rewrite); 5 min covers big files on slow disks. The audio-only AAC fallback shares this timeout (audio re-encode is cheap). pix never re-encodes video, so there's no hour-scale ffmpeg path. |
| Filesystem rename / move | **10s** | Should be instant; 10s catches AV locks and network FS hangs. |
| Format-aware content hash compute | **60s** | Even a 10 GB MP4 hashes in seconds (BLAKE3 ~3 GB/s; post the JPEG marker-scan optimization the JPEG path is native-speed). 60s = pathological. |

Defaults are hard-coded in v1. No env-var overrides, no config knobs. If real workflows hit a ceiling, we add an override then. Until then, hitting a timeout is the signal that we need to learn something about the workload.

### On timeout: halt for investigation

All timeouts halt the run on first occurrence. Apply writes `Failed   <action>  <file>: <tool> timed out after <Xs>` to apply.log and exits non-zero. The intent is diagnostic — timeouts shouldn't fire in steady state; when one does, the user wants to see it immediately and decide whether the limit needs raising for their workload. Once timeout values are tuned against real data, individual operations can opt into skip-and-continue if their failure mode is per-file data quality (this is what CONVERT-with-truncated-source does for non-timeout failures via `ConvertFailed`).

Non-timeout `ConvertFailed` (truncated source, unreadable format) still skips and continues — see [migrate.md → Failure handling](migrate.md#failure-handling). That carve-out is data-quality-specific; timeouts are not.

`pix hash` is per-file skip-and-log for *non-timeout* failures (a file with permission issues doesn't block the rest of the library). Hash compute timeouts still halt — same rationale as above: a hash that takes >60s is pathological and worth investigating before we let the run continue.

### CTRL+C — reader-thread pattern

The ExifTool wrapper uses `-stay_open True` and reads framed responses by blocking on `stdout.readline()` until a `{ready}` sentinel arrives. On Windows, a Python-level signal handler cannot interrupt a C-level blocking read on a pipe — SIGINT sits in the queue until the read returns, which is "never" when the subprocess wedges.

Fix: move the blocking read to a daemon worker thread that publishes lines onto a `queue.Queue`. The main thread polls the queue with a short timeout (~250 ms):

```
ExifToolSession.__init__:
    daemon thread: for line in iter(stdout.readline, ''): queue.put(line); queue.put(None)

ExifToolSession.execute(*args, timeout):
    write + flush stdin
    deadline = monotonic() + timeout
    while True:
        try: line = queue.get(timeout=0.25)
        except Empty:
            if shutdown_flag: kill subprocess; raise KeyboardInterrupt
            if monotonic() > deadline: kill subprocess; raise ExifToolTimeout
            continue
        if line is None: raise RuntimeError("subprocess exited")
        if line == "{ready}\n": return ''.join(out)
        out.append(line)
```

Top-level CLI installs a `signal.signal(SIGINT, ...)` handler that flips a process-wide `shutdown_flag`. Every wrapper's poll loop checks it; on shutdown the wrapper kills the subprocess, writes an `Interrupted` line to apply.log, and exits non-zero.

ffmpeg / Pillow are invoked synchronously and don't have the framed-response problem — `Popen.wait(timeout=N)` is interrupt-safe under CPython 3.12, so they need only `Popen.wait(timeout=...)` plus a finally-clause that calls `terminate()` on interrupt.

The 250 ms polling tick is a soft real-time guarantee: Ctrl-C is observable within at most one tick.

## Long-path handling

Use `\\?\` prefixes on all FS paths.

## Sync client interaction

A pix library is designed to be safely syncable by a file-sync client (Synology Drive, OneDrive, Dropbox, …): the media tree and the durable `.pix/{runs,errors,stash}` data sync normally. The one thing to exclude is **`.pix/local/`**, which groups everything that must not be synced under a single folder (so it takes a single exclude rule):

- `cache.db` (+ its `-wal`/`-shm`) — the recreatable cache store (below). A live WAL SQLite copied by a sync client is a corruption/conflict trap, and it's regenerable regardless.
- `lock` — the library lock; its payload is a machine-local PID, so syncing it invites false locks and conflict-copies across machines.
- `staging/`, `checkout/`, `faces/` — transient working state / recreatable cache. `checkout/` additionally hard-links library files, which some clients re-upload as independent copies.

**Everything in `.pix/local/` is regenerable or machine-local by design** — losing it costs at most a cache rebuild (re-hash / re-fingerprint), never library data. That's what makes it safe to exclude even if a client's exclude behavior is aggressive. (An *open* checkout is the one thing to close first — commit or reset it — before changing sync rules.)

The durable state stays top-level and may be synced or backed up deliberately: `.pix/runs/` holds full file captures from every migrate run (syncing roughly doubles cloud storage per run — relocate via `runs_dir` if that's too heavy), and `.pix/errors/`, `.pix/stash/` hold only-copy files.

**In Synology Drive Client:** Sync Tasks → select the task → Sync Rules → Selective Sync → deselect `.pix/local` (one entry; no name/extension filters needed). `pix init` creates `.pix/local/` up front so it exists to be deselected *before* the first run.

**Upgrade path.** Libraries created before `.pix/local/` existed fold into the new layout automatically on the next command — `root.ensure_local_layout` moves the workspaces, `cache_db` relocates the DB via a WAL checkpoint + single-file rename (self-healing: the old path keeps working until the move succeeds, so no data loss), and `library_lock` stale-cleans any pre-upgrade lock. A read-only `pix info events` is enough to trigger all of it.

## Cache store

All derived caches live in one SQLite DB, `<library>/.pix/local/cache.db` (WAL mode),
one row per library file keyed by absolute path: `files(path, size, mtime_ns,
meta, hash, vfp)`. It replaces the former per-file sidecar tree
(`.pix/cache/<mirror>.meta`/`.hash`/`.vfp`) — up to three tiny files per media
file — which made every command stat+read+parse ~200k files just to answer
"what changed?". The store loads in one `SELECT`; prune/relocate/remove and the
`pix info events` query are one statement each.

**Validation (unified key).** A row's `(size, mtime_ns)` is the file's identity;
a column (`meta`/`hash`/`vfp`) is valid iff it is non-NULL *and* the row stamp
matches the live file. Every in-place pix write that bumps mtime goes through
`cache_db.note_inplace_metadata_change`, which re-stamps, merges the new tags
into `meta`, and carries the content `hash` + `vfp` forward (a metadata-only
write leaves content — hence hash/fingerprint — unchanged). So set/clear,
rotate-retag, dedupe MERGE, and migrate's in-place TAG no longer force a
needless re-hash/re-fingerprint.

**What's stored in `meta`.** Only the tags pix consumes (`pix.metadata_filter`):
`SourceFile`, the `pix:*` fields, the DateAuto candidates, and face/region tags
— not the full ExifTool dump. Live reads (`pix info meta`) bypass the cache and still
see every tag.

**Migration.** On first open of a library that still has the legacy
`.pix/cache/` tree, the store folds each still-valid sidecar into a row and
reaps the tree (one-time, idempotent, gated on an `import_done` flag). The
single-writer library lock serializes writers; WAL allows concurrent readers.

`pix init` prints a one-time reminder to exclude `.pix/local/` from the sync client (see "Sync client interaction" above for the full rationale and the Synology steps). The media tree and the durable `.pix/{runs,errors,stash}` data sync normally.

The one remaining sync unknown is not about `.pix/` at all: whether a client recognizes `organize`'s library-wide file **moves** as renames (cheap, server-side move) or re-uploads them (delete + create). This is a per-client, deployment-time question — Synology Drive Client logs a `Locally renamed` action when it recognizes a move, but large batches have been reported to degrade to re-upload, so it's worth measuring at scale before trusting a full-vault reorganize. Hard links exist only under `.pix/local/checkout` (excluded), so once `.pix/local` is excluded the client never has to reason about links.

## Environment notes

- All media local to the machine. Synology Drive Client syncs the library files (outside `.pix/`) to NAS/cloud in the background.
