# pix spec — overview

This folder holds the design spec for `pix`. Each file covers one scope; cross-references between files are explicit.

## What we're building

CLI tooling to manage a personal media library (photos + videos) at terabyte scale. The library is currently scattered; the tools aggregate, dedupe, normalize, tag, and reorganize it — without losing data.

The tool is named `pix`. The CLI is invoked as `pix <op> ...`.

## Operations

Eight top-level operations. `init` and `migrate` are implemented; the rest are sketches or deferred.

| Op | What it does | Spec | Status |
|---|---|---|---|
| `init [<path>]` | Establish a library root by creating `<path>\.pix\` with default config. | [library.md](library.md#establishing-a-root) | **v1 implemented** |
| `migrate <folder>` | Per-file **in-place** normalization: convert formats, rename, re-derive `_auto` tags, write tags into files. | [migrate.md](migrate.md) | **v1 implemented** (face detection deferred — see [Open decisions](#open-decisions)) |
| `dedupe` | Find duplicates by content hash across the library and remove redundant copies. | [dedupe.md](dedupe.md) | Sketched |
| `merge <src> <dst>` | Combine two already-migrated trees; reuses `dedupe`. | — | Deferred (after dedupe) |
| `organize <template>` | Physically rearrange files per a template. Single-valued tags only. | [organize.md](organize.md) | Sketched |
| `checkout <template>` / `commit` | Tag editing via folder-shuffle. | [tag-editing.md](tag-editing.md) | Sketched |
| `export <template>` | Produce a copy/link-based derived view at a separate path. Read-only. | [export.md](export.md) | Sketched |

## Cross-cutting invariants

These hold across all operations. Each spec reinforces the invariants relevant to it.

- **Atomicity / no data loss.** Every action is transactional. Multi-step operations stage to temp paths and validate before committing. Partial failure rolls back cleanly. Same-volume operations exploit atomic rename.
- **Soft delete only (conservation).** Every destructive operation captures the data it replaces into the current run's folder under `.pix/runs/<run-id>/`. User performs the final hard-delete manually by removing old run folders.
- **CONVERT preserves source metadata.** Any CONVERT action carries forward all non-format-specific metadata (EXIF, XMP including face regions, IPTC, container-level metadata) from the source into the output file. CONVERT changes only the encoding/container, never the metadata payload.
- **TAG writes preserve untouched fields.** A TAG action modifies only the pix:* fields named in the plan line. All other metadata on the file — other pix:* fields, EXIF, XMP, IPTC, face regions — is preserved bit-for-bit. This is what makes incremental migrate runs and re-derivation passes safe.
- **Same-volume constraint.** Library root, `.pix/`, and source folders being migrated are assumed on the same volume so atomic rename and hard links are available.
- **Idempotence.** Re-running an op with no input changes produces no work. Every metadata write compares new value to current; equal → no write, no plan line.
- **CLI + folder-as-UI only.** No GUI. Bulk tag editing via folder-shuffle in checkouts; migrate plans via text editor.
- **Performance at scale.** Library is terabytes. Use parallelism where it helps; bulk operations preferred when atomicity still holds.

## Open decisions

- **Dedupe design** — workflow, plan format, keeper selection, hash cache. Sketched in [dedupe.md](dedupe.md); full design deferred. (Migrate has now stabilized, so this is unblocked.)
- **Merge design** — workflow, plan format, reuse of `dedupe`. Deferred until dedupe stabilizes.
- **Apply-phase parallelism for migrate** — currently sequential. Lines are independent; a worker pool is a future perf-pass, not a v1 concern.
- **Face detection — deferred to last.** Migrate v1 *does not* detect faces or write `XMP-mwg-rs:RegionList`. The spec covers face workflow in [tag-editing.md](tag-editing.md) and the writing protocol in [tags.md](tags.md#structured-metadata-face-regions), but the migrate-time detection step (insightface + embedding match against confirmed identity centroids) is intentionally postponed until everything else in the spec is built. When it lands it integrates as another bundled step inside the existing TAG / CONVERT+RENAME+TAG action; no new top-level operation.
