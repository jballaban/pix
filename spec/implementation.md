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
| Video transcode | `ffmpeg` (subprocess; bundled `.exe` or on PATH) |
| Metadata read/write | `ExifTool` (subprocess via `pyexiftool`) — only tool that reliably handles EXIF/XMP/IPTC across photo + video formats including MWG face regions. **Reads:** bulk-extract with `exiftool -j -r -G:1 <folder>` once per migrate, populating an in-memory cache (see [migrate.md → Metadata cache](migrate.md#metadata-cache)). **Writes:** per-file via `-overwrite_original`, using `-stay_open` mode (one long-running ExifTool process per migrate, communicating via stdin/stdout) to avoid the ~200ms-per-spawn overhead. |
| Format-aware content hash (tier 1) | hand-rolled framing: JPEG → strip APP-marker metadata (APP1/EXIF, APP1/XMP, APP13/IPTC, …) and hash the rest; MP4 / ISO BMFF → parse boxes and hash only the concatenated `mdat` payload(s). Hashed with `blake3` (256-bit, hex-encoded). Stored in the per-file cache under `.pix/cache/` by `pix hash` (see [hash.md](hash.md)). |
| Perceptual hash (tier 2) | `imagehash` (photos), sampled-frame imagehash (videos) |
| Face detection + embedding | `insightface` (ONNX-backed) |
| Identity clustering | `hdbscan` or cosine-similarity threshold |
| Parallelism | `concurrent.futures.ProcessPoolExecutor` (CPU work happens in native extensions so the GIL is not a constraint) |
| Env + lockfile | `uv` |
| Type checking | `pyright` (strict) |
| Tests | `pytest` |

## Subprocess hardening

pix orchestrates several subprocesses (ExifTool, ffmpeg/ffprobe, Pillow via its native libs). Any of them can wedge on a malformed input or a slow disk, and we've seen real-world hangs where a single file ate hours of wall time before being killed. Two requirements:

1. **Every subprocess call has a timeout.** If it exceeds the timeout, the wrapper kills the subprocess and raises a typed exception that the calling action handles like any other failure.
2. **CTRL+C works.** A user pressing Ctrl-C while a subprocess is mid-call must interrupt within ~500 ms, kill the subprocess, write a clean log line, and exit non-zero.

### Timeout matrix

Timeouts are per primitive call, not per plan action. A `CONVERT+RENAME+TAG` plan line is a sequence of (Pillow OR ffmpeg) → ExifTool → fs rename, each with its own timeout.

| Primitive | Default | Rationale |
|---|---|---|
| ExifTool `-execute` (read, sidecar export, tag write) | **30s** | Metadata ops should be sub-second; 30s = wedged. Same timeout regardless of whether it's reading or writing — same protocol underneath. |
| Pillow JPG encode (`convert_to_jpg`) | **60s** | A 50 MP JPG re-encodes in seconds. 60s = pathological. |
| ffmpeg re-mux (`-c copy`) | **5 min** | Should be fast (bytes copy + container rewrite); 5 min covers big files on slow disks. |
| ffmpeg re-encode (libx265 / aac) | **1 hour** | Realistic for 30–60 min H.264-source clips at 1–2× real-time on libx265. Covers most realistic library content. |
| Filesystem rename / move | **10s** | Should be instant; 10s catches AV locks and network FS hangs. |
| Format-aware content hash compute | **60s** | Even a 10 GB MP4 hashes in seconds (BLAKE3 ~3 GB/s; post the JPEG marker-scan optimization the JPEG path is native-speed). 60s = pathological. |

Defaults are hard-coded in v1. No env-var overrides, no config knobs. If real workflows hit a ceiling — most likely the 1-hour ffmpeg re-encode for 4K family videos — we add an override then. Until then, hitting a timeout is the signal that we need to learn something about the workload.

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

`.pix/` must be excluded from any file-sync client (Synology Drive, OneDrive, Dropbox, …). Reasons:

- `.pix/runs/` holds full file captures from every migrate run — syncing roughly doubles cloud storage per run, and run folders accumulate until the user deletes them.
- `.pix/checkouts/` contains hard links to library files; some sync clients treat each link as an independent file and re-upload.
- `.pix/staging/` and `.pix/faces/` are local working state / recreatable cache.

`pix init` prints a one-time reminder to add `.pix` to the sync client's exclude rules. For Synology Drive Client on Windows: **Settings → Sync Rules → Excluded folders → add `.pix`**. The actual library files (outside `.pix/`) sync normally.

Empirically verifying the exclude is honored is a deployment-time check, not a design unknown — there are no hard links outside `.pix/` in the design, so the sync client never has to reason about them once the exclude is in place.

## Environment notes

- All media local to the machine. Synology Drive Client syncs the library files (outside `.pix/`) to NAS/cloud in the background.
