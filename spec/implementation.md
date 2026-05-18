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
| Format-aware content hash (tier 1) | hand-rolled framing: JPEG → strip APP-marker metadata (APP1/EXIF, APP1/XMP, APP13/IPTC, …) and hash the rest; MP4 / ISO BMFF → parse boxes and hash only the concatenated `mdat` payload(s). Hashed with `blake3` (256-bit, hex-encoded). Stored on each file as `pix:ContentHash` by migrate (see [migrate.md](migrate.md) and [tags.md → System fields](tags.md#system-fields)). |
| Perceptual hash (tier 2) | `imagehash` (photos), sampled-frame imagehash (videos) |
| Face detection + embedding | `insightface` (ONNX-backed) |
| Identity clustering | `hdbscan` or cosine-similarity threshold |
| Parallelism | `concurrent.futures.ProcessPoolExecutor` (CPU work happens in native extensions so the GIL is not a constraint) |
| Env + lockfile | `uv` |
| Type checking | `pyright` (strict) |
| Tests | `pytest` |

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
