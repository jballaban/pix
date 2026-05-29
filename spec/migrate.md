# Migrate

`pix migrate <folder>` is the per-file, in-place normalization pass. It walks `<folder>` and its subfolders, converts each file's format (per [extension policy](#extension-policy)), renames to the canonical name (see [library.md](library.md#canonical-filename)), and writes `_auto` tag values into metadata (see [tags.md](tags.md#tag-model)).

Files never move between folders during migrate. Only [organize](organize.md) does that. Cross-file deduplication is a separate operation: see [dedupe.md](dedupe.md).

Migrate honors the spec-wide metadata-preservation invariants: **CONVERT carries forward all non-format-specific metadata from source to output**, and **TAG writes modify only the named pix:\* fields, leaving every other byte of metadata intact**. See [README.md → Cross-cutting invariants](README.md#cross-cutting-invariants).

## Workflow — git-commit-style

`migrate <folder>` is a single blocking, **sequential** command modelled on `git commit`:

1. **Cleanup pass.** Wipe `.pix\staging\`. Scan `<folder>` for orphan `*.__migrate__.*` markers from previously interrupted runs and resolve them (see [marker cleanup](#marker-cleanup)). There is no resume — interrupted runs are walked away from; cleanup just removes or finalizes their leftovers so the new plan reflects current state.
2. **Allocate run folder.** Create `<library-root>\.pix\runs\<run-id>\` where `<run-id>` is a timestamp like `2026-05-16_14-32-01`. The run-id is just a folder name on disk — no code reads it back, no marker carries it.
3. **Build metadata cache.** Bulk-extract metadata for every file in `<folder>` into an in-memory cache (see [Metadata cache](#metadata-cache)). One ExifTool invocation reads thousands of files in a single subprocess; per-file `pyexiftool` calls would be ~100× slower at TB scale.
4. **Generate plan.** Walk the cache and write the migration plan to `runs\<run-id>\plan.txt`. Plan generation never reads file metadata directly — it consults the cache.
5. **Confirm.** CLI shows a summary of the generated plan (counts per action type) and prompts: `Apply? [Y/e/n]`. The prompt **defaults to Y** — pressing Enter applies the plan directly. Editing is opt-in:
    - `y` (or Enter) → proceed to apply.
    - `e` → open the plan file in `$EDITOR` / `%EDITOR%` (fallback: notepad on Windows, vi on POSIX), wait for save+close, re-read the (possibly edited) plan, show the new summary, and re-prompt. The user can loop through edit cycles as many times as they want.
    - `n` → abort. The plan file stays on disk as-is and nothing else changes.

   An empty plan after edit is still a valid `Apply? [Y/e/n]` state — `y` becomes a no-op apply; `e` lets the user undo their over-zealous deletions by re-editing (if they had a backup or the prior content in memory).
6. **Apply.** Process plan lines sequentially. Each destructive operation captures the data it replaces into the run folder (see [conservation captures](#conservation-captures)) before destroying anything. TAG writes are per-file ExifTool calls; the cache is updated in-memory after each write but isn't strictly needed (apply executes the fixed plan and doesn't re-read).

`plan.txt` is **immutable once written.** Generation populates it; the editor pass may shrink it (line deletions); apply reads it but never writes back to it. Progress is streamed to a separate `runs\<run-id>\apply.log` opened in append mode — one line per state transition (`Started` / `Completed` / `Failed`). A crash leaves an `apply.log` truncated to whatever was flushed; the missing tail is the work that didn't finish. Both files are **for reference only** — the next `migrate` run replans from current filesystem state.

Status output during a migrate run keeps the console quiet — phase headers, file counts, and per-file detail all go to log files in the run folder; the console gets only the single rewriting progress line and the user-facing prompts (plan summary, `Apply? [Y/e/n]`, "Applied N actions").

1. **Console during plan-gen and apply** — exactly one line, rewritten in place via `\r` once per second:
   - `NNN% Xphase - L042 ACTION path (Yiter)` during apply
   - `NNN% Xphase - planning path` during plan-gen

   The phase-total elapsed sits at the **front of the line** as a fixed-width temporal anchor — present on every render so the user sees the run progressing even when each iteration is sub-second. The **per-iteration elapsed** is appended in trailing parens only when it's worth surfacing (≥1s) and the phase has multiple `begin()` calls; fast iterations and single-begin phases collapse to just the front block.

   Front-block layout: 3-char right-aligned percent, `%`, space, 8-char right-aligned phase-duration, ` - `. The duration field reserves enough width for `9h59m59s` and renders shorter tiers right-aligned with leading spaces (`      3s`, `   1m23s`); >9h overflows the field naturally. Examples:

   - Slow iteration: ` 45%    2m14s - L042 CONVERT+RENAME+TAG IMG_4821.HEIC (3s)`
   - Fast iteration: ` 45%    2m14s - L042 RENAME IMG_4821.HEIC`

   **Indeterminate-mode phases** (no total known — `Walking library...`, `Reading metadata...`) replace the percent slot with spaces so the duration column stays aligned across phases, and have no trailing per-iter parens (the only timer is already at the front):

   - Indeterminate: `        1m23s - Walking library...`

   On clean exit the percent wraps to `100%`. Auto-disabled when stdout isn't a TTY (tests, redirects).

   <a id="duration-format"></a>**Duration format** (applies to the front-of-line `Xphase` and to the trailing `(Yiter)` parens in progress lines, and to the end-of-phase summaries in `plan.log` / `apply.log`):

   | Elapsed | Format | Examples |
   |---|---|---|
   | `< 60s` | `Xs` (integer seconds) | `3s`, `42s` |
   | `60s – 3599s` | `XmYs` | `1m3s`, `27m08s` |
   | `≥ 3600s` | `XhYmZs` | `1h3m2s`, `12h45m07s` |

   Post-phase summary lines (e.g. `Found 64000 files in <duration>.`, `Plan generated in <duration>.`) follow the same tiered format, except they may show one decimal place when under 60s — sub-second precision is useful when measuring fast phases. From 60s upward the format is integer-only since sub-second precision is noise at minute/hour scale.

   The tiered format applies to every spec that mentions `(Xs)` (see [hash.md](hash.md), [dedupe.md](dedupe.md), [organize.md](organize.md)). Those specs use the `Xs` shorthand for brevity; the actual rendering follows this table.

2. **Console after plan-gen** — the user-relevant transition lines: `Plan written: <path>`, `Summary: ...`, `Apply? [Y/e/n]`. If the user picks `e`, the editor opens, then on close: `After edit: ...` and re-prompt. These are post-planning, action-relevant; they stay on the console where the user is actively making decisions.

3. **Log files** in the run folder capture everything the console doesn't show:
   - `plan.log` — every phase header (`Library root: ...`, `Walking source folder...`, `Found N files in Xs`, `Reading metadata from N files...`, `Read N files in Xs`, `Generating plan...`, `Plan generated in Xs`) plus one line per file considered (`<ISO timestamp> <abs-path> -> <L###> <ACTION>` or `... -> (skip)`). Captures the full enumeration, including skips that don't appear in `plan.txt`.
   - `debug.log` — verbose per-file reasoning for every file plan-gen considered: extension policy lookup, date-candidate trace, effective-date computation, first-migrate detection, collision resolution, final decision. Streamed during plan-gen (constant memory regardless of library size). Sections separated by `=== <path> ===` headers and labeled with `--- Section name ---` sub-headings. Always written, no flag.
   - `apply.log` — one line per Started/Completed/Failed transition during apply.

Errors and aborts still print directly to stderr — those interrupt the user and need to be visible.

No folder lock — single-user, single-active-run assumption.

If apply crashes or is interrupted, the partial run folder stays on disk as a historical record. The next `migrate` invocation does not try to resume it; its cleanup pass simply removes/finalizes any in-flight markers in the source folder, then plans fresh from current state. Old run folders accumulate until the user manually deletes them.

## Metadata cache

Plan generation needs the existing metadata of every file in `<folder>` (current `pix:*` values for change detection, EXIF/XMP date fields for `DateAuto` derivation — see [tags.md → DateAuto derivation](tags.md#dateauto-derivation), face regions). Reading each file individually with `pyexiftool` costs ~tens of milliseconds even with `-stay_open`; for a TB-scale folder with hundreds of thousands of files that's minutes-to-hours.

**Bulk read.** At step 3 of the workflow, migrate invokes ExifTool once with `-j -r -G:1 <folder>` (or a small number of invocations if size dictates), parses the resulting JSON, and indexes the result by file path. One subprocess, recursive walk inside ExifTool, native-speed I/O.

**Lifetime.** The cache is in-memory only; it lives for the duration of the migrate process and is rebuilt fresh every run. A crash discards it; the next run rebuilds. No cache file is persisted in v1.

**Contents.** For each source file, the cache holds:
- All `pix:*` fields currently on the file (used to detect what's changing).
- EXIF/XMP/IPTC fields that contribute to [`DateAuto` derivation](tags.md#dateauto-derivation).
- Face region structures (read once here; not re-fetched during face detection — that's a separate pipeline that consumes its own cache under `.pix/faces/`).
- File extension and on-disk filename.

Migrate does not read or write any content hash. Content-hash population lives in [`pix hash`](hash.md), which stores hashes in the `.pix/cache/` layer rather than in file metadata. Migrate and hash are orthogonal.

**Reads.** Plan generation never reads file metadata directly — it consults the cache. The cleanup pass (which may need to read tags from orphan markers to compute their canonical names) is the one exception, and it runs before the cache is built; per-marker reads are acceptable because orphan markers are rare.

**Writes.** TAG writes during apply remain **per-file** via ExifTool's `-overwrite_original` (see [Atomicity and crash recovery](#atomicity-and-crash-recovery)). This is the slow path, accepted as a v1 cost for simplicity. The in-memory cache is updated after each successful write so that any downstream consumer in the same run sees current state, but apply doesn't actually re-read.

**Persistent cache (implemented).** Files already seen by ExifTool have their metadata cached at `<library>/.pix/cache/<absolute-path-mirror>/<filename>.cache` (one tiny JSON per file). Cache entries are validated by file size; under pix's single-writer trust model, that's enough. Lookups are O(1) per file: one small read. Misses fall through to ExifTool, which uses file-list mode (`-@ <listfile>`) so it reads only the unfamiliar files instead of recursively walking everything again. Apply maintains the cache in sync — renames move the `.cache` file alongside the media, deletes drop it, tag writes update the cached metadata, and CONVERT writes a fresh entry for the new file via a live-session ExifTool read of the just-finalized output (so subsequent migrates don't have to re-read every converted file). Organize re-keys via per-line rename. Best-effort everywhere; a failed cache mutation just means one cache miss on the next run.

## Plan file format

One line per source file (atomic unit: all operations on a file bundle on one line). Commented lines at the bottom are informational only.

```
# Migration plan: F:\source\trip-2023
# Generated 2026-05-16 14:32
# Run ID: 2026-05-16_14-32-01
# 12 files migrating for the first time will have their source path stored in metadata.
#
# Delete a line to skip that file this run. Commented "#" lines are info only.
# Format: L<line-id> | ACTION | path | details

L001 | CONVERT+RENAME+TAG | IMG_001.HEIC      | →2023-08-15_143205.jpg; original_path init; date_auto null→2023-08-15-14:32:05
L002 | RENAME             | DSC_0042.JPG      | →2023-08-15_143612.jpg
L003 | DELETE              | Thumbs.db         | extension policy: delete
L004 | TAG                | 2023-08-15_143612.jpg | event_auto null→birthday
L005 | TAG                | 2022-08-15_143205.jpg | date_auto 2023-08-15-14:32:05→2024-08-15-14:32:05

# Summary: 4 CONVERT, 12 RENAME, 40 TAG, 5 DELETE
```

`original_path init` is shorthand for "this file is migrating for the first time; `pix:OriginalPath` is being set to the current source path." Migrate plans never contain hash actions — content-hash population is a separate operation; see [hash.md](hash.md).

L005 is a `date_auto` re-derivation on a file whose `pix:DateOverride` pins year=2022. The plan line shows the user-visible `_auto` change; the filename doesn't change (the override masks year, which is the only changed component). As a side effect of this TAG write, migrate also writes `pix:DateAutoPrevious = 2023-08-15-14:32:05` — the dirty flag for future review (see [tags.md](tags.md#auto-previous-fields-dirty-flagging)).

During apply, progress streams to a sibling `apply.log` in the same run folder, append-only, one line per state transition:

```
2026-05-16T14:32:01 L001 Started   CONVERT+RENAME+TAG  IMG_001.HEIC
2026-05-16T14:32:07 L001 Completed CONVERT+RENAME+TAG  IMG_001.HEIC
2026-05-16T14:32:07 L002 Started   RENAME              DSC_0042.JPG
2026-05-16T14:32:07 L002 Completed RENAME              DSC_0042.JPG
2026-05-16T14:32:07 L003 Started   DELETE              Thumbs.db
2026-05-16T14:32:07 L003 Completed DELETE              Thumbs.db
2026-05-16T14:32:08 L004 Started   TAG                 2023-08-15_143612.jpg
2026-05-16T14:32:08 L004 Failed    TAG                 2023-08-15_143612.jpg: exiftool exited 1
```

`plan.txt` itself is never rewritten. A `Started` line with no matching `Completed`/`Failed` is the line that was active when the process died.

### Failure handling

Two policies, by action type:

- **CONVERT failures move the source into `.pix/errors/` and continue.** A CONVERT action that fails on the conversion step itself (Pillow can't decode an HEIC, ffmpeg can't read a corrupted MOV, …) is logged to `apply.log` as `Failed`, then the source file is moved to `<library>/.pix/errors/<run-id>_<line-id>.<source-ext>` with a sidecar at `<...>.errorinfo` recording the `original_path`, the error message, the failure timestamp, the run-id, and the **pix version that produced the failure**. Same opaque-name + sidecar shape as [stash](#stash-action) — both folders preserve a file we couldn't fully process, the semantic distinction lives in *which folder* (stash = intentional set-aside, errors = runtime failure). The run continues to the next plan line. At end of run, the console prints the list of moved paths and migrate exits non-zero.

  **Auto-retry on version bump.** Migrate's cleanup phase restores any `.pix/errors/` entry whose recorded `pix_version` differs from the running `pix.__version__` (or is absent — legacy sidecars from pre-v0.1.86 are treated as stale). The file goes back to its `original_path`; the upcoming plan-gen pass sees it and re-proposes the CONVERT under the current code. Files quarantined by the *current* version are left in place — retrying the same code would hit the same failure, so the user must intervene (upgrade pix, restore from backup, or hard-delete the entry). The restore log lands in `plan.log` so the user can see what was retried this run.

  **Truncation tolerance.** Partially-recoverable images (camera crashed mid-write, network copy interrupted, drive failure mid-import) used to land in errors/ because Pillow refused the decode. `pix.convert` now sets `PIL.ImageFile.LOAD_TRUNCATED_IMAGES = True` globally so these inputs decode to whatever pixel data is available — typically a complete image at the top with black/garbage bands where bytes are missing. The user gets a valid (if visibly imperfect) JPG instead of nothing. Healthy files are unaffected.
- **All other actions halt.** A failure in TAG (ExifTool error), RENAME (filesystem error), or DELETE (filesystem error) halts the run on the first occurrence. These signal infrastructure problems, not per-file data quality — they're the same root cause for every file, so continuing wastes time. The user reads the error, fixes the root cause, and re-runs.

The distinction: CONVERT failures are almost always a property of the *source file* (truncated, corrupted, unreadable for that specific input); other failures are almost always a property of the *environment* (ExifTool wedged, disk full, AV holding a lock). Timeouts always halt regardless of the action — see [implementation.md → On timeout: halt for investigation](implementation.md#on-timeout-halt-for-investigation).

Line IDs (`L001`, `L002`, …) are assigned at plan generation. They're stable for the duration of the run and tie each plan line to its `apply.log` entries and its capture file in the run folder. Users who delete lines from the plan during edit leave gaps in the numbering — that's fine, the survivors keep their IDs.

Behaviors:

- **Skip an action** — delete the line. The file isn't touched this run. Next run re-proposes (the logic is the source of truth; users can't make per-file exceptions persist).

Each `migrate` invocation generates a fresh plan from current state. Prior runs' plan files live at `runs/<run-id>/plan.txt` and are kept indefinitely for reference (and as the index for any future rollback) — they are never consulted when generating or applying a new plan.

## What's in the plan

- **Conversions** (`CONVERT`) — format changes per [extension policy](#extension-policy).
- **Renames** (`RENAME`) — apply the canonical filename convention (effective `date` drives the filename; see [library.md](library.md#canonical-filename)), including extension canonicalization (`.jpeg` → `.jpg`, lowercase). RENAME fires only when the **effective** filename changes — an override pinning a component keeps that part of the filename stable even if `_auto` shifts.
- **Tag updates** (`TAG`) — any pix:* metadata write. Covers `_auto` value changes (whether or not an override masks the effective tag) and the first-time write of `pix:OriginalPath`. Content-hash population is **not** a TAG action; it lives in [`pix hash`](hash.md) and writes to the cache layer rather than file metadata.
  - Plan-line details show the user-visible `_auto` change (e.g. `date_auto 2023-08-15-14:32:05→2024-08-15-14:32:05`). They do not call out override-masking separately — that's bookkeeping the spec handles via the side-effect described below.
  - **Side effect on TAG writes:** when `_auto` is changing (not first-time null → value) AND an override is set for that tag, migrate also writes `*AutoPrevious` recording the prior `_auto` value. This is the dirty flag a future workflow uses to surface auto/override conflicts to the user. See [tags.md → Auto-previous fields](tags.md#auto-previous-fields-dirty-flagging). The Previous write is part of the same TAG action — it shares the sidecar capture and atomicity.
- **Deletes** (`DELETE`) — files whose extension is marked `delete` in config; captured into the run folder.

### First-time files always include a TAG component

Because `pix:OriginalPath` is written on first migrate and is itself a pix:* metadata field, the first migrate of any file always includes a TAG. Typical action labels on first migrate:

- **`CONVERT+RENAME+TAG`** — first migrate of a non-canonical format (e.g. HEIC, MOV).
- **`RENAME+TAG`** — first migrate of a file already in canonical format but with a camera-assigned name. Writes `OriginalPath` + `_auto` baselines + canonicalizes filename.
- **`TAG`** — first migrate of a file already canonically named (rare; usually only happens when the user has hand-named a file ahead of time).

Pure `RENAME` (no TAG) only appears for files that are *already fully tagged* (OriginalPath set, `_auto` baselines populated) but whose on-disk name doesn't match the canonical form — i.e. the user renamed the file after migrating it, or the filename convention itself changed.

### Other

Migrate does **not** deduplicate. Cross-file dedupe (same content under different names or different formats) is the job of a separate `pix dedupe` operation that runs against an already-normalized library. See [dedupe.md](dedupe.md).

User overrides on `_auto` values are **not** edited from the migrate plan. They live in a separate tag-checkout workflow ([tag-editing.md](tag-editing.md)). Migrate applies policy/heuristics; tag-checkouts capture user judgment.

## Extension policy

Every source extension must have an explicit action in `<library-root>\.pix\config.yaml`. Unknown extensions abort migrate before plan generation. Lookup is case-insensitive.

### Actions

| Action | Effect |
|---|---|
| `keep` | Already canonical. Extension is normalized (case + alias, see below); content untouched. |
| `convert_to_jpg` | Decode + re-encode as JPG (quality 95). Pillow + pillow-heif. EXIF/XMP preserved. |
| `convert_to_mp4` | Container → MP4, Windows-playable codec. Re-mux (`ffmpeg -c copy`) only when the source already meets the [Windows-playable](#windows-playability-check) criteria; otherwise re-encode `-c:v libx264 -profile:v main -pix_fmt yuv420p -crf 18 -c:a aac -b:a 192k`. Container metadata copied (`-map_metadata 0`). |
| `delete` | Capture the file into the run folder during migrate, then remove from source. Conservation applies. |
| `stash` | Move the file into `<library-root>/.pix/stash/` for future processing (RAW formats, proprietary 360 sources, anything we can't canonically process in v1). Whole-file BLAKE3 dedups across stash entries: same content from multiple sources lands once with a multi-origin sidecar. See [Stash action](#stash-action) below. |

Adding a **new target format** (e.g. `convert_to_webp`) requires code changes — new conversion implementation and tag-write support. Adding a new extension to an existing action is config-only.

### Extension canonicalization

The canonical extension is always lowercase, and certain aliases collapse to a single form:

| Source | Canonical |
|---|---|
| `.jpeg`, `.JPG`, `.JPEG` | `.jpg` |
| `.m4v`, `.M4V` | `.mp4` (Apple-branded MP4; byte-identical container) |
| `.mp4`, `.MP4` | `.mp4` |
| (any other) | lowercase of source |

A file whose name on disk doesn't match its canonical extension triggers a RENAME even if no other change is needed.

### Default config

Created on first run if absent:

```yaml
extensions:
  jpg:     keep
  jpeg:    keep
  mp4:     keep
  m4v:     keep            # Apple-branded MP4; same bytes, different extension
  heic:    convert_to_jpg
  heif:    convert_to_jpg
  png:     convert_to_jpg
  mov:     convert_to_mp4
  avi:     convert_to_mp4
  mts:     convert_to_mp4   # AVCHD camcorder MPEG-TS; usually H.264, remuxes cheaply
  mpg:     convert_to_mp4   # MPEG-1/MPEG-2 Program Stream; mandatory re-encode
  mpeg:    convert_to_mp4   # same format as .mpg, long extension
  vob:     convert_to_mp4   # DVD-Video object; MPEG-2 PS + DVD-specific extras
  dng:     stash            # Adobe Digital Negative — raw sensor data
  insp:    stash            # Insta360 proprietary photo
  insv:    stash            # Insta360 proprietary video
  ds_store: delete    # macOS system junk
  thumbs.db: delete   # Windows system junk
  ini:     delete    # desktop.ini and other Windows config sidecars
  txt:     delete    # plain text files (notes, release-notes, manifests)
  json:    delete    # metadata exports, sidecars
  gif:     delete    # web-format animated images (memes, downloads)
  webp:    delete    # web image format (downloads, screenshots)
  jwt:     delete    # Microsoft auth-broker trust manifests synced by OneDrive
```

Notable omissions — user must opt in by adding the extension:
- Other RAW formats (`.cr2`, `.nef`, `.arw`, `.raf`, `.rw2`, `.orf`, `.pef`) — likely `stash` (same reasoning as `.dng`). Added on demand.
- `.webp`, `.bmp`, `.tiff`/`.tif` — likely `convert_to_jpg`.
- `.mkv`, `.wmv`, `.3gp`, `.webm`, `.m2ts` — likely `convert_to_mp4`.
- Sidecar/metadata files (`.aae`, `.lrcat`, `.xmp`) — likely `delete`.

### Stash action

The `stash` action moves files into `<library-root>/.pix/stash/` for future processing. The library itself only contains files in canonical formats; stash is the holding area for things we can't (or shouldn't) canonicalize in v1: RAW sensor files (`.dng`, etc.), proprietary 360 source files (`.insp`/`.insv`), anything else the user wants set aside.

**Purist design.** Stash does the minimum: preserve the file and record where it came from. No hashing, no dedup, no collision logic at stash time. When the user later decides what to do with their stashed files (collapse duplicates, process the RAWs, stitch the 360 sources), that's a separate operation — a future command will hash and dedup on demand.

**Layout**: flat folder. Each stashed file gets an **opaque on-disk name** plus a tiny YAML sidecar:

```
.pix/stash/
  2026-05-22_15-30-00_L001.dng              ← raw bytes from the source
  2026-05-22_15-30-00_L001.dng.stashinfo    ← YAML sidecar
  2026-05-22_15-30-00_L002.insv
  2026-05-22_15-30-00_L002.insv.stashinfo
```

The on-disk filename is `<run-id>_<line-id><source-extension>`. Run-id is the timestamp the run started (e.g., `2026-05-22_15-30-00`); line-id is the plan-line label (e.g., `L042`). The combination is globally unique by construction — no collision logic is ever needed.

**Sidecar format** (the file's full provenance):

```yaml
origin: F:\source\trip-2023\IMG_001.dng
stashed_at: 2026-05-22T15:30:00
```

That's the entire sidecar. The original filename, full source path, and timestamp are all there. No hash, no original_filename field, no origins list. Anything we'd want to compute later (content hash for dedup, source-folder structure, multiple-imports detection) can be derived from these two facts on demand.

**Cross-volume**: source on a different volume from the library means the initial move is a copy+delete (via `shutil.move`). One-time cost per file; no clever dedup avoidance.

**Source folder fate**: same as DELETE — migrate doesn't own the source folder, so empty source folders after a stash are left in place.

**Rollback**: deferred. The apply.log records source → stash mapping per line; the sidecar carries `origin`. Either is enough to reverse a stash (move file back to its source path).

**Dedup of stashed files**: explicitly **out of scope for stash itself**. The same content imported from two different sources lands twice in `.pix/stash/`. A future operation (likely re-using migrate's plumbing) will scan stashed files, hash them, and propose dedup actions when the user decides to deal with them.

<a id="windows-playability-check"></a>
### Windows playability check

Windows's built-in H.264 decoder (Windows Media Player, Movies & TV, the system Media Foundation decoder) supports only 4:2:0 chroma sampling. Camcorder-era files encoded as H.264 High 4:2:2 / High 4:4:4 / High 10, or with pixel formats like `yuvj422p`, `yuv422p`, `yuv444p`, will not play in Windows even though ffmpeg, VLC, and MPC-HC handle them fine. Stock Windows just shows "can't play this format" — same surface error as a corrupt file, which is misleading.

Migrate enforces "playable on stock Windows" as a CONVERT trigger so the canonical library is universally playable.

**Criteria.** A video file is Windows-playable iff:

- **H.264** with profile in `{Constrained Baseline, Baseline, Main, High}` AND `pix_fmt ∈ {yuv420p, yuvj420p}` (8-bit 4:2:0; `yuvj420p` is the full-range/JPEG-range flavor of `yuv420p` — same chroma + bit depth, so it plays), OR
- **HEVC** (any profile / pix_fmt — users opt in to HEVC playback via the Windows HEVC Video Extension; modern phone/camera HEVC is already yuv420p 8-bit).

All other video streams (h264 with extended profiles or non-4:2:0 chroma; mpeg2video; mpeg4 ASP; etc.) are not Windows-playable and must be re-encoded.

**Probe.** During plan-gen, migrate runs `ffprobe -show_entries stream=codec_name,profile,pix_fmt` on every keep-policy mp4/m4v candidate. The probe runs in a thread pool (32 workers; ffprobe is process-startup-bound, not CPU-bound) so the phase scales with hundreds of videos. Results land in `<library>/.pix/cache/<absolute-path-mirror>/<filename>.video` keyed on `(size, mtime_ns)` — identical layout to the content-hash cache. First migrate pays the ffprobe cost; subsequent migrates over an unchanged library skip the ffprobe pass entirely (cache hit on every candidate). CONVERT rewrites a video → mtime shifts → next migrate re-probes (and finds the now-playable libx264 yuv420p output).

Source files matching the `convert_to_mp4` policy already go through CONVERT regardless and probe internally at apply time, so they aren't part of the plan-gen probe set.

Result feeds the action decision in plan-gen:

- `keep` policy + Windows-playable → no plan line (or RENAME/TAG only, per the usual idempotence rules).
- `keep` policy + **not** Windows-playable → **CONVERT+RENAME+TAG**, re-encoding under the criteria below.
- `convert_to_mp4` policy + source Windows-playable → re-mux (`-c copy`) as before.
- `convert_to_mp4` policy + source **not** Windows-playable → re-encode under the criteria below.

**Re-encode target.**

- Video: `-c:v libx264 -profile:v main -pix_fmt yuv420p -crf 18`. CRF 18 is visually lossless for typical handheld content; this is an archive pass, not a streaming pass.
- Audio: `-c:a aac -b:a 192k` (unchanged from prior policy).
- Container metadata: `-map_metadata 0` (unchanged).

This replaces the prior libx265-CRF-23 re-encode target: x264 + 4:2:0 is the universally playable lowest-common-denominator. Larger files vs HEVC, but the user can run a future `pix transcode` op if they later want HEVC for storage savings.

**Re-processing already-migrated files.** Files with `pix:OriginalPath` already set are *not* exempt from the playability check — migrate re-CONVERTs them under the new policy. This is deliberate: when the user runs `pix migrate <library-root>` after this spec change, every previously-migrated yuvj422p/High-4:2:2 file gets re-encoded once, and the library becomes uniformly playable. Originals are captured to `runs/<run-id>/data/` so the operation is reversible.

The XMP layer carries over (per the cross-cutting CONVERT invariant) so `pix:OriginalPath`, `pix:DateAuto`, user `DateOverride` / `EventOverride` etc. all survive the re-encode. The `.pix/cache/<…>.hash` cache for each re-encoded file becomes stale (size + mtime change); `pix hash` regenerates on next run.

### Fail-fast on unknown extensions

Before generating a plan, migrate walks the source and collects all distinct extensions. Any not in config aborts:

```
$ pix migrate F:\source\trip-2023

Unknown file extensions found in source:
  .webp   (e.g. F:\source\trip-2023\downloaded.webp)
  .bmp    (e.g. F:\source\trip-2023\old_scan.bmp)
  .lrcat  (e.g. F:\source\trip-2023\catalog.lrcat)

Edit <library-root>/.pix/config.yaml and set an action for each, then re-run.
Available actions: keep, convert_to_jpg, convert_to_mp4, delete
(Adding a new target format requires code changes.)

Aborted; no changes made.
```

Exit non-zero. No plan file written.

### Idempotence

A folder of files already in canonical form produces a plan with no CONVERT, no RENAME, no DELETE actions for those files.

## Atomicity and crash recovery

The atomic unit is **a single filesystem op** (same-volume rename), not a plan line. The plan is *intent*, not a transaction — mid-apply crashes leave a partial state on disk, and the cleanup pass at the start of the next `migrate` resolves the leftovers without trying to resume the interrupted work.

Each plan line decomposes into a sequence of small fs ops, each independently safe. State between ops is encoded in the filesystem itself via marker filenames — no transaction log or checkpoint state, no run-id embedded in markers.

For a CONVERT+TAG+RENAME line (the hardest case), the sequence is:

1. **Off-library work** — convert + write tags + validate in `<library-root>\.pix\staging\`. Crash here: temp orphan, deleted by the next run's cleanup. No source-folder impact.
2. **Bring into source as marker** — single rename of the temp file into the source folder as e.g. `IMG_001.HEIC.__migrate__.jpg` next to `IMG_001.HEIC`. The marker filename encodes "this replaces `IMG_001.HEIC`."
3. **Capture original** — rename `IMG_001.HEIC` → `<library-root>\.pix\runs\<run-id>\data\L<NNN>_IMG_001.HEIC` (see [conservation captures](#conservation-captures)).
4. **Finalize name** — rename `IMG_001.HEIC.__migrate__.jpg` to its canonical name (e.g. `2023-08-15_143205.jpg`).

Each step is one same-volume rename. The marker's existence is the only thing the next run needs to know about.

### Marker conventions

Markers use a synthetic `.__migrate__.` infix that's collision-proof against real filenames. The scan globs `**/*.__migrate__.*` and resolves each match purely from filesystem state — no sidecar metadata, no run-id encoded in the filename.

| Op | Marker? | Pattern |
|---|---|---|
| `CONVERT` (± TAG, ± RENAME) | yes | `{stem}.{old-ext}.__migrate__.{new-ext}` next to `{stem}.{old-ext}` |
| `TAG` only (in-place metadata write) | no — ExifTool's `-overwrite_original` provides its own atomicity |
| `RENAME` only | no — single atomic rename |
| `DELETE` (extension-policy) | no — single rename to the run folder (capture, not marker) |

### Marker cleanup

Cleanup runs at the start of every `migrate`, before plan generation. It does **not** resume the interrupted work — it just brings the source folder back to a consistent state so the new plan reflects current reality. The new run gets its own `runs/<run-id>/` folder; the interrupted run's folder is left untouched as a historical record.

1. Wipe `.pix\staging\` (any temp orphans from a previous run's step 1).
2. Glob `**/*.__migrate__.*` under `<source>` and resolve each CONVERT marker `X.{old-ext}.__migrate__.{new-ext}`:
   - **Both marker and `X.{old-ext}` present** (crash between step 2 and 3) → original is fine; delete the marker. The new plan will re-propose the CONVERT.
   - **Marker only, no `X.{old-ext}`** (crash between step 3 and 4) → the original is preserved in the prior run's folder. Read the marker's tags via ExifTool, compute the canonical name, and rename the marker to it. Work is finalized; the new plan won't re-propose it.
3. Glob `**/*_exiftool_tmp` under `<source>` and delete any matches. These are leftovers from ExifTool's atomic-write machinery if a TAG write was interrupted mid-flight; ExifTool's protocol guarantees the original file is untouched when the tmp is present, so deletion is safe. The new plan will re-propose the TAG.

Cleanup is silent — these are completing or discarding the prior run's work using only filesystem state.

### Conservation captures

Every destructive operation in a plan line writes the data it replaces into the run folder at `<library-root>\.pix\runs\<run-id>\`. This is **not optional** — conservation is the default. Nothing is permanently lost without being captured first.

| Action | Capture |
|---|---|
| `CONVERT` (± TAG, ± RENAME) | Move original file → `runs\<run-id>\data\L<NNN>_<original-filename>`. The file's XMP travels inside the file. |
| `DELETE` | Move file → `runs\<run-id>\data\L<NNN>_<original-filename>`. |
| `TAG` only (or `TAG` + `RENAME`) | Export current XMP via ExifTool → `runs\<run-id>\data\L<NNN>_<original-filename>.xmp`. The file itself stays in place; only its metadata changes. |
| `RENAME` only | No capture — reversible from the plan line alone. |

`L<NNN>` is the plan line ID; `<original-filename>` is the file's name as it was on disk. Captures live in a `data\` subfolder of the run dir, separating preserved file data from the run's logs (`plan.txt`, `plan.log`, `apply.log`, `debug.log`).

**Conservation law**: a `migrate` run never destroys data without preserving what it destroyed. State is reconstructible (modulo rollback code being written) from current state + run folders walked in reverse order. The data sufficient for `pix rollback <run-id>` is guaranteed present even though the rollback command itself is deferred.

**Cleanup**: run folders are user-managed. They accumulate across runs and stay until the user manually deletes them; doing so forfeits the ability to roll back those runs.

### Other consequences

- **`apply.log` is best-effort.** Each transition appends a line and flushes, but a crash mid-write may leave the log truncated. Source-folder state is the source of truth, not the log. The next `migrate` run replans from filesystem state and never consults the prior run's log.
- **No resume.** Re-run migrate after a crash; the cleanup pass tidies leftover markers and the new plan covers what's left as a fresh plan against current state.

## Worked examples

Each example walks through one plan line for a fictitious file: pre-state on disk, plan line, fs ops during apply, post-state, and what happens if a crash hits mid-line.

Throughout, `<source>` is `F:\source\trip-2023`, the library root is `F:\photos`, and the run-id is `2026-05-16_14-32-01`. Marker filenames are shown verbatim.

### Example 1 — `CONVERT+RENAME+TAG`

A HEIC straight off an iPhone. Has EXIF datetime 2023-08-15 14:32:05, no pix metadata yet.

**Pre-state:**
```
F:\source\trip-2023\
  IMG_4821.HEIC
```

**Plan line:**
```
L042 | CONVERT+RENAME+TAG | IMG_4821.HEIC | →2023-08-15_143205.jpg; original_path init; date_auto null→2023-08-15-14:32:05
```

**Apply ops (each is one same-volume rename, except step 1 which writes a file):**

1. Decode HEIC, re-encode as JPG, copy non-format-specific metadata from the source (EXIF/XMP/IPTC), then write `pix:DateAuto` + `pix:OriginalPath` into the JPG, validate by re-reading metadata. Result lands at `F:\photos\.pix\staging\IMG_4821.HEIC.tmp.jpg`.
2. Rename `F:\photos\.pix\staging\IMG_4821.HEIC.tmp.jpg` → `F:\source\trip-2023\IMG_4821.HEIC.__migrate__.jpg`. (Marker is now next to original.)
3. Rename `F:\source\trip-2023\IMG_4821.HEIC` → `F:\photos\.pix\runs\2026-05-16_14-32-01\data\L042_IMG_4821.HEIC`. (Original captured.)
4. Rename `F:\source\trip-2023\IMG_4821.HEIC.__migrate__.jpg` → `F:\source\trip-2023\2023-08-15_143205.jpg`. (Canonical name.)

**Post-state:**
```
F:\source\trip-2023\
  2023-08-15_143205.jpg
F:\photos\.pix\runs\2026-05-16_14-32-01\
  plan.txt
  plan.log
  apply.log
  data\
    L042_IMG_4821.HEIC
```

**Crash recovery:**

- *Crashed during step 1 (staging write):* `staging\` has a partial file. Source folder untouched. Next migrate's cleanup wipes `staging\`; the new plan re-proposes the CONVERT.
- *Crashed between step 2 and 3:* `IMG_4821.HEIC` and `IMG_4821.HEIC.__migrate__.jpg` both in source. Next migrate's cleanup sees both → deletes the marker. New plan re-proposes the CONVERT (the converted bytes are forfeit, but the original is intact).
- *Crashed between step 3 and 4:* Only `IMG_4821.HEIC.__migrate__.jpg` in source; original is in the prior run's folder. Next migrate's cleanup sees marker-without-sibling → reads tags from the marker, computes `2023-08-15_143205.jpg`, renames marker to it. New plan does **not** propose anything for this file (it's now canonical).

### Example 2 — `RENAME` only

A JPG that has already been fully migrated (so `pix:DateAuto`, `pix:OriginalPath`, and any `_auto` baselines are populated), but whose on-disk name doesn't match the canonical form. This applies only to **non-first-time** files; the user renamed it back to a camera-assigned name, or the filename convention changed in a later release. First-time files in canonical format become RENAME+TAG (see [What's in the plan](#first-time-files-always-include-a-tag-component)).

**Pre-state:**
```
F:\source\trip-2023\
  DSC_0042.JPG       # pix:DateAuto = 2023-08-15-14:36:12, pix:OriginalPath set
```

**Plan line:**
```
L013 | RENAME | DSC_0042.JPG | →2023-08-15_143612.jpg
```

**Apply ops:**

1. Rename `F:\source\trip-2023\DSC_0042.JPG` → `F:\source\trip-2023\2023-08-15_143612.jpg`. (Also covers `.JPG` → `.jpg` extension canonicalization in the same step.)

**Post-state:**
```
F:\source\trip-2023\
  2023-08-15_143612.jpg
F:\photos\.pix\runs\2026-05-16_14-32-01\
  plan.txt
  apply.log
```

No capture file — RENAME is reversible from the plan-line text alone.

**Crash recovery:** Rename is atomic; either the file has the old name or the new name. Nothing to clean up.

### Example 3 — `TAG` only

A JPG already canonically named, but a new `_auto` derivation classifies it under an event.

**Pre-state:**
```
F:\source\trip-2023\
  2023-08-15_143612.jpg     # pix:EventAuto absent
```

**Plan line:**
```
L055 | TAG | 2023-08-15_143612.jpg | event_auto null→birthday
```

**Apply ops:**

1. Export current XMP via ExifTool → `F:\photos\.pix\runs\2026-05-16_14-32-01\data\L055_2023-08-15_143612.jpg.xmp`. (Sidecar capture; file untouched.)
2. Call ExifTool with `-overwrite_original` to write the new tags directly into `F:\source\trip-2023\2023-08-15_143612.jpg`. ExifTool internally writes to `2023-08-15_143612.jpg_exiftool_tmp`, then atomically renames it over the original. From pix's perspective the call either succeeds (new metadata in place) or fails (original untouched).

No pix-managed marker is needed; ExifTool's own protocol provides atomicity.

**Post-state:**
```
F:\source\trip-2023\
  2023-08-15_143612.jpg     # pix:EventAuto = "birthday"
F:\photos\.pix\runs\2026-05-16_14-32-01\
  plan.txt
  plan.log
  apply.log
  data\
    L055_2023-08-15_143612.jpg.xmp
```

The image bytes are unchanged; only XMP differs. The sidecar in the run folder holds the prior XMP, which is enough to roll back the TAG change.

**Crash recovery:**

- *Crashed during step 1:* No file changes; sidecar may exist but is harmless. New plan re-proposes the TAG.
- *Crashed during step 2 before ExifTool's internal rename:* `2023-08-15_143612.jpg_exiftool_tmp` exists; original untouched. Cleanup deletes the tmp file (per the `*_exiftool_tmp` sweep). New plan re-proposes the TAG.
- *Crashed during step 2 after ExifTool's internal rename:* Write completed successfully; nothing to recover. New plan sees current tags and doesn't propose anything for this file.

### Example 4 — `DELETE` (extension policy)

A `Thumbs.db` left behind by Windows Explorer; config marks `thumbs.db: delete`.

**Pre-state:**
```
F:\source\trip-2023\
  Thumbs.db
```

**Plan line:**
```
L007 | DELETE | Thumbs.db | extension policy: delete
```

**Apply ops:**

1. Rename `F:\source\trip-2023\Thumbs.db` → `F:\photos\.pix\runs\2026-05-16_14-32-01\data\L007_Thumbs.db`. (One atomic rename; this is both the capture and the removal.)

**Post-state:**
```
F:\source\trip-2023\
  (Thumbs.db gone)
F:\photos\.pix\runs\2026-05-16_14-32-01\
  plan.txt
  plan.log
  apply.log
  data\
    L007_Thumbs.db
```

**Crash recovery:** Rename is atomic; the file is either in source or in the run folder. Nothing to clean up.

### Example 5 — `TAG` with `*AutoPrevious` side effect

A previously-migrated JPG where the user set a `DateOverride` pinning year=2022, and a new `_auto` re-derivation now says 2024. The `_auto` field still needs updating (otherwise the same drift would be re-proposed on every migrate); the override-mask means the filename doesn't change, but a `DateAutoPrevious` is recorded as the dirty flag.

**Pre-state:**
```
F:\source\trip-2023\
  2022-08-15_143205.jpg     # pix:DateAuto = 2023-08-15-14:32:05
                            # pix:DateOverride = 2022-*-*-*:*:*
```

**Plan line:**
```
L088 | TAG | 2022-08-15_143205.jpg | date_auto 2023-08-15-14:32:05→2024-08-15-14:32:05
```

**Apply ops:** (same shape as Example 3)

1. Export current XMP via ExifTool → `F:\photos\.pix\runs\2026-05-16_14-32-01\data\L088_2022-08-15_143205.jpg.xmp`. (Sidecar capture.)
2. Call ExifTool with `-overwrite_original` to write the new `pix:DateAuto` AND the new `pix:DateAutoPrevious` (set to the prior `DateAuto` value `2023-08-15-14:32:05`) in a single ExifTool invocation.

**Post-state:**
```
F:\source\trip-2023\
  2022-08-15_143205.jpg     # pix:DateAuto = 2024-08-15-14:32:05
                            # pix:DateAutoPrevious = 2023-08-15-14:32:05
                            # pix:DateOverride = 2022-*-*-*:*:* (unchanged)
F:\photos\.pix\runs\2026-05-16_14-32-01\
  plan.txt
  plan.log
  apply.log
  data\
    L088_2022-08-15_143205.jpg.xmp
```

The filename is unchanged because the effective `date`'s year is still 2022 (override pins it). `DateAutoPrevious` now flags this file for future review — a `pix` workflow can surface "files whose `_auto` drifted while masked by an override" by looking for the presence of this field.

**Crash recovery:** same as Example 3 (no marker; ExifTool atomicity handles the write).
