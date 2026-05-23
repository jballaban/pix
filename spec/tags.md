# Tags, metadata, and templates

This spec covers what pix knows about each file (the tag model), how that knowledge is persisted (metadata mapping), and how the user expresses queries over it (template grammar).

Related:
- [tag-editing.md](tag-editing.md) for the workflow that writes user overrides.
- [migrate.md](migrate.md) for the workflow that writes `_auto` values.
- [library.md](library.md#canonical-filename) for how effective values drive the filename.

## Tag model

### Flat tag schema

| Tag | Type | Multi-valued? | Notes |
|---|---|---|---|
| `date` | datetime | no | single fact: when the photo/video was taken. Components (year, month, day, time) are derived from this single field by templates and the [canonical filename](library.md#canonical-filename). |
| `event` | string | no | freeform |
| `person` | name | yes (list) | derived from face regions (see below) |
| `face` | name | yes (list) | same value space as `person`; renders as face crops in templates |

There is **one** date tag, not four — having separate `year`/`month`/`day`/`time` tags would multiply the override surface unnecessarily. Templates reference `{year}`, `{month}`, `{day}`, `{time}` as a convenience: they are computed from `date` at render time.

Every tag has a paired `_auto` value (`date_auto`, `event_auto`, …):

- `_auto` is the tool's best guess — set by migrate, never overwritten by user edits, always re-derivable.
- The bare name (`date`, `event`, …) is the user override. When set, it (partially or fully) wins over `_auto`. When unset, the tag value is exactly `_auto`.
- "Unset" means the override property is **absent** from the file's XMP — not stored as null. Clearing an override (folder-shuffle to `null/`) translates to deleting that property entirely. Files in pristine state therefore carry no override properties at all.

The split lets us upgrade `_auto` derivation logic and surface mismatches for review.

### Date override is a wildcard string

`pix:DateOverride` is a single string in the format `YYYY-MM-DD-HH:MM:SS`, with `*` permitted in any field. The effective `date` is computed by taking `pix:DateAuto` and replacing each non-`*` field of the override.

| `pix:DateAuto` | `pix:DateOverride` | Effective `date` |
|---|---|---|
| `2023-08-15-14:32:05` | (absent) | `2023-08-15-14:32:05` |
| `2023-08-15-14:32:05` | `*-03-*-*:*:*` | `2023-03-15-14:32:05` (override pins month) |
| `2023-08-15-14:32:05` | `2020-*-01-*:*:*` | `2020-08-01-14:32:05` (override pins year + day) |
| `2023-08-15-14:32:05` | `*-*-*-12:00:00` | `2023-08-15-12:00:00` (override pins time) |

A `DateOverride` that's all `*` is equivalent to absent and should not be stored; tag-editing clears the field when it would reach that state.

### Auto-previous fields (dirty flagging)

Each `_auto` field has an optional `*AutoPrevious` companion: `pix:DateAutoPrevious`, `pix:EventAutoPrevious`. The field stores the prior `_auto` value as a record of drift while an override was active.

It is written by [migrate](migrate.md) when **both**:

1. The `_auto` value is changing (not first-time null → value, but value-A → value-B), and
2. The corresponding override field is set (for `DateAuto`, `DateOverride` is present with at least one non-`*` field).

It is cleared when:

- The override is removed via [tag-editing](tag-editing.md). Commit reconciles by deleting the Previous alongside the override.
- A future user action explicitly resolves the dirty flag (mechanism deferred; likely a `pix` subcommand or a special checkout template).

The field's presence is the dirty flag. Migrate itself doesn't surface dirty files to the user — that's a future workflow's job. The data sufficient to do so (current `_auto`, prior `_auto`, current override) is preserved on the file.

`*AutoPrevious` fields are absent unless needed; the conservation-capture (sidecar to the run folder) of the prior XMP applies the same way as any TAG write.

### `DateAuto` derivation

`pix:DateAuto` is set by migrate from the highest-priority date source available on the file. The candidate list is consulted in order; first match wins. If none match, `DateAuto` stays null and the file lands in `null/` for date-based templates (the user can still set `DateOverride` later via [tag-editing](tag-editing.md) to give it a date).

**Photos:**

1. `EXIF:DateTimeOriginal`
2. `EXIF:CreateDate` / `EXIF:DateTimeDigitized`
3. `XMP:DateCreated`
4. `XMP:CreateDate`
5. `IPTC:DateCreated`
6. **Filename pattern on `pix:OriginalPath`** (if set) — e.g. `IMG_YYYYMMDD_HHMMSS`, `PXL_YYYYMMDD_HHMMSSsss`. Once a file has been migrated its current filename is just our canonical output (`YYYY-MM-DD_HHMMSS.ext`) and re-deriving from it is circular; the original name is the surviving filesystem-side signal.
7. **Parent-folder pattern on `pix:OriginalPath` parent** (if set) — e.g. `2023-08-15-trip/`.
8. Filename pattern on the current name (matches first-migrate files where `pix:OriginalPath` isn't set yet, and any case where the user hand-renamed a migrated file).
9. Parent-folder pattern on the current parent.
10. `File:ModifyDate` / NTFS mtime (least trustworthy — copies, archive extractions, and sync clients all clobber this).

**Videos:**

1. `QuickTime:CreateDate` (timezone-normalized)
2. `QuickTime:MediaCreateDate`
3. `XMP:CreateDate`
4. Filename pattern on `pix:OriginalPath` (if set)
5. Parent-folder pattern on `pix:OriginalPath` parent (if set)
6. Filename pattern on current name
7. Parent-folder pattern on current parent
8. NTFS mtime (least trustworthy)

The same candidate list is re-consulted on every migrate, so improving the heuristics (recognizing more filename patterns, smarter folder-name parsing, etc.) produces drift in stored `DateAuto` values on the next run — which is exactly what `*AutoPrevious` is designed to flag when an override is in play (see [Auto-previous fields](#auto-previous-fields-dirty-flagging)).

### `EventAuto` derivation

`pix:EventAuto` is set by migrate from the **immediate parent folder name** of `pix:OriginalPath` (or, when OriginalPath isn't set yet — first migrate — the file's current parent folder).

The folder name is processed as:

1. Strip a leading run of digits and common separators (`-`, `_`, `.`, space). Regex: `^[\d\-_. ]+`.
2. Trim trailing whitespace.
3. If the result is empty OR contains no alphabetic character, `EventAuto` stays absent.
4. Otherwise `EventAuto` = the cleaned string. Case and internal separators are preserved.

| Parent folder | EventAuto |
|---|---|
| `2023-01-Party` | `Party` |
| `2023_01_Party` | `Party` |
| `2023 01 Party` | `Party` |
| `20230101_Party` | `Party` |
| `2023.08.15-Birthday` | `Birthday` |
| `2023-08-Hawaii Trip` | `Hawaii Trip` |
| `Hawaii Trip` (no date prefix) | `Hawaii Trip` |
| `misc` | `misc` |
| `Birthday-Party` (no leading date) | `Birthday-Party` |
| `Party-2023` (date at end is preserved) | `Party-2023` |
| `2023-03` (date-only) | (none) |
| `2023` | (none) |
| `2023-08-15` | (none) |

Like `DateAuto`, `EventAuto` is re-consulted on every migrate. Improving the heuristics, or moving a file to a new folder, updates the stored value. When `EventOverride` is set and the re-derived `EventAuto` differs from stored, `pix:EventAutoPrevious` is written as the dirty flag (see [Auto-previous fields](#auto-previous-fields-dirty-flagging)).

If re-derivation now returns nothing (e.g., the file was moved into a date-only folder), the stored `EventAuto` is left alone — same conservative behavior as `DateAuto`. Don't lose a previously-stored value just because the heuristics regressed.

### Structured metadata: face regions

Face data isn't a flat tag — it's structured per-region metadata stored on the file via an industry-standard XMP face-region schema (Microsoft Photo Region / MWG-Regions). Each region:

- bounding box (optional — a region with no coords means "this person is in this photo but no face was detected")
- identity (name)
- `confirmed?` flag — replaces the `_auto`/override pattern for faces. Each region is either auto-detected (unconfirmed) or user-confirmed/corrected.

`person` is **derived**: `person = set of distinct identities across the file's face regions`. There is no independently stored `person` value — both `{person}` and `{face}` template tokens read from face regions, differing only in rendering (full photos vs cached face crops).

The flat-tag-surface rule still holds: templates only ever see flat tokens; face regions are storage details.

## Metadata mapping

Tag state is persisted in XMP, primarily in a custom `pix` namespace. Standard fields (`EXIF:DateTimeOriginal`, etc.) are read for heuristics on first migrate but **never written** by pix — they remain camera-recorded provenance.

### Custom namespace

| Property | Value |
|---|---|
| URI | `http://pix.local/` (placeholder, not resolvable) |
| Prefix | `pix` |
| Registration | Ship an ExifTool config alongside `pix.exe` defining the namespace and field types |

### Field map

| Concept | Field | Notes |
|---|---|---|
| Camera-recorded original datetime (photo) | `EXIF:DateTimeOriginal` | Read-only for pix. Immutable provenance. |
| Camera-recorded original datetime (video) | `QuickTime:CreateDate` | Read-only for pix. Immutable provenance. |
| `date_auto` (heuristic-derived datetime) | `xmp:pix:DateAuto` (datetime, `YYYY-MM-DD-HH:MM:SS`) | Written by migrate. |
| `date` override (wildcard) | `xmp:pix:DateOverride` (string, same format with `*` allowed) | Absent if not set. |
| `date_auto` prior value (dirty flag) | `xmp:pix:DateAutoPrevious` (datetime) | Absent unless `DateAuto` has changed while an override was active; see [Auto-previous fields](#auto-previous-fields-dirty-flagging). |
| `event_auto` | `xmp:pix:EventAuto` (string) | Written by migrate. |
| `event` override | `xmp:pix:EventOverride` (string) | Absent if not set. |
| `event_auto` prior value (dirty flag) | `xmp:pix:EventAutoPrevious` (string) | Absent unless `EventAuto` has changed while `EventOverride` was active. |
| Face regions | `XMP-mwg-rs:RegionList` (primary) + `XMP-MP:RegionInfo` (mirror for Windows interop) | ExifTool writes both from one structure. |
| Per-region confirmed flag | `xmp:pix:FaceConfirmed` (per region) | MWG doesn't define this; pix extends. |
| Original source path (write-once) | `xmp:pix:OriginalPath` (string) | Set on first migrate, never overwritten. See [library.md](library.md#original-source-path-write-once-provenance). |

### Effective value computation

Read at any time the tag value is needed (filename derivation, organize template, checkout view):

1. **Date.** Start with `pix:DateAuto`. If `pix:DateOverride` is present, replace each non-`*` field of the override over the corresponding field of `DateAuto`. The result is the effective `date`. Components (`{year}`, `{month}`, `{day}`, `{time}`) are then derived from the effective `date` as needed.
2. **Event.** If `pix:EventOverride` is present, its value is the effective `event`. Otherwise, fall back to `pix:EventAuto`.
3. **Person, Face.** Derived: set of distinct identities across the file's face regions. Not stored.

`*AutoPrevious` fields do not participate in effective value computation; they are informational only (see [Auto-previous fields](#auto-previous-fields-dirty-flagging)).

### System fields

`pix:OriginalPath` is a **system field** — pix-managed metadata that doesn't follow the `_auto`/override/Previous pattern. It has no user-editable surface, no folder representation in checkouts, and no template tokens. It's stored in the custom pix namespace so it travels with the file like any other pix:* field, but conceptually it's provenance, not a tag.

| Field | Lifecycle | Re-derived when | Writer |
|---|---|---|---|
| `pix:OriginalPath` | Written once on first migrate. Never overwritten. | Never. Pure historical fact (see [library.md](library.md#original-source-path-write-once-provenance)). | `pix migrate` |

**Content hash is not a tag.** It used to live here as `pix:ContentHash`, but it's now stored in the per-file cache under `.pix/cache/` and computed by [`pix hash`](hash.md). The hash is a derived fact about the file's bytes, not user-curated metadata; the cache layer keys it on `(size, mtime_ns)` so it auto-invalidates, and avoids one ExifTool round-trip per file at TB scale. See [hash.md](hash.md) for the format-aware hashing algorithm and cache schema.

### Side effect

Files the user has never overridden carry only `DateAuto`, `EventAuto`, `OriginalPath`, and any face regions. Override properties (`DateOverride`, `EventOverride`) and Previous fields (`DateAutoPrevious`, `EventAutoPrevious`) appear only on files where they're meaningful.

### Consequence: filename / EXIF dissonance under overrides

When a date override is set, the canonical filename will reflect the effective (override-patched) date (e.g., `2022-08-15_143205.jpg` when `DateOverride` pins year=2022) while `EXIF:DateTimeOriginal` continues to show the camera-recorded value (e.g., 2023). This is the explicit trade — pix preserves camera-recorded truth as immutable provenance and broadcasts the user's chosen truth only through the filename and `pix:*` fields. External tools that read `EXIF:DateTimeOriginal` will see the camera value; tools that read the filename or `pix:*` will see the effective value.

### Idempotence

Every metadata write compares new value to current stored value; equal → no write, no plan line. Deterministic heuristics produce no churn on re-runs.

## Template grammar

Used by `organize`, `checkout`, and `export` (see [organize.md](organize.md), [tag-editing.md](tag-editing.md), [export.md](export.md)).

- `{tag}` — enumerate; produce one folder per distinct value.
- `{tag:val1,val2}` — filter to listed values; produce one folder per listed value.
- `null` — special value meaning "no value set."
- `!` — negation prefix. `{year:!null}` = everything tagged; `{year:null,2020}` = untagged + 2020.

No range filters and no operators beyond list/`null`/`!`.

### Date components as tokens

Templates may reference `{date}` directly (the full datetime as a string), or any of the derived components: `{year}`, `{month}`, `{day}`, `{time}`. These are computed from the effective `date` value at render time; they aren't independently stored tags.

### Folder categories per operation

| Folder | `organize` | `checkout` | `export` |
|---|---|---|---|
| Valued (`2023/`, `james/`) | yes | yes | yes |
| `null/` (untagged) | yes by default | yes by default | only if filter explicitly includes `null` |
| `filtered/` (excluded by explicit filter) | yes — must account for every file | yes | n/a — excluded files just don't appear |

### Single-valued vs multi-valued

A file has exactly one physical location in the library, so `organize` templates can only reference single-valued tags (`date` and its derived components, `event`). Multi-valued tags (`person`, `face`) are usable in `checkout` and `export`, which materialize hard links — one per (file, tag-value) pair.

`{time}` is technically permitted in folder templates but useless as a folder level (per-second folders).
