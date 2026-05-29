# pix spec — overview

This folder holds the design spec for `pix`. Each file covers one scope; cross-references between files are explicit.

## What we're building

CLI tooling to manage a personal media library (photos + videos) at terabyte scale. The library is currently scattered; the tools aggregate, dedupe, normalize, tag, and reorganize it — without losing data.

The tool is named `pix`. The CLI is invoked as `pix <op> ...`.

## Operations

Eight top-level operations. `init`, `migrate`, `hash`, `dedupe`, and `organize` are implemented; the rest are designed-pending-build, sketched, or deferred.

| Op | What it does | Spec | Status |
|---|---|---|---|
| `init [<path>]` | Establish a library root by creating `<path>\.pix\` with default config. | [library.md](library.md#establishing-a-root) | **v1 implemented** |
| `migrate <folder>` | Per-file **in-place** normalization: convert formats, rename, re-derive `_auto` tags, write tags into files. | [migrate.md](migrate.md) | **v1 implemented** (face detection deferred — see [Open decisions](#open-decisions)) |
| `hash <library-root>` | Populate the per-file content-hash cache at `.pix/cache/` for every file missing or stale. Decoupled from migrate so migrate's hot path stays fast. | [hash.md](hash.md) | **v1 implemented** |
| `dedupe` | Find duplicates by content hash across the library and remove redundant copies. | [dedupe.md](dedupe.md) | **v1 implemented** |
| `merge <src> <dst>` | Combine two already-migrated trees; reuses `dedupe`. | — | Deferred (after dedupe) |
| `organize [<template>]` | Physically rearrange files per a template (bare = re-apply the stored default). Single-valued tags only. | [organize.md](organize.md) | **v1 implemented** |
| `checkout <path> <template>` / `checkout --commit` / `checkout --reset` | Tag editing via folder-shuffle, scoped to `<path>` (like migrate). Compound single-valued templates; commit writes tags only. | [tag-editing.md](tag-editing.md) | **Designed (pending build)** |
| `export <template>` | Produce a copy/link-based derived view at a separate path. Read-only. | [export.md](export.md) | Sketched |

## Cross-cutting invariants

These hold across all operations. Each spec reinforces the invariants relevant to it.

- **Atomicity / no data loss.** Every action is transactional. Multi-step operations stage to temp paths and validate before committing. Partial failure rolls back cleanly. Same-volume operations exploit atomic rename.
- **Soft delete only (conservation).** Every destructive operation captures the data it replaces into the current run's folder under `.pix/runs/<run-id>/`. User performs the final hard-delete manually by removing old run folders. The one exception is `pix hash`, whose writes are purely additive into the recomputable `.pix/cache/` layer (no source data is replaced) — see [hash.md → Conservation invariant](hash.md#conservation-invariant).
- **CONVERT preserves source metadata.** Any CONVERT action carries forward all non-format-specific metadata (EXIF, XMP including face regions, IPTC, container-level metadata) from the source into the output file. CONVERT changes only the encoding/container, never the metadata payload.
- **TAG writes preserve untouched fields.** A TAG action modifies only the pix:* fields named in the plan line. All other metadata on the file — other pix:* fields, EXIF, XMP, IPTC, face regions — is preserved bit-for-bit. This is what makes incremental migrate runs and re-derivation passes safe.
- **Same-volume constraint.** Library root, `.pix/`, and source folders being migrated are assumed on the same volume so atomic rename and hard links are available.
- **Idempotence.** Re-running an op with no input changes produces no work. Every metadata write compares new value to current; equal → no write, no plan line.
- **CLI + folder-as-UI only.** No GUI. Bulk tag editing via folder-shuffle in checkouts; migrate plans via text editor.
- **Performance at scale.** Library is terabytes. Use parallelism where it helps; bulk operations preferred when atomicity still holds.
- **Single active operation per library.** Enforced by a library-wide lock — see [Concurrency](#concurrency) below.

## Concurrency

pix is single-user, but the operations are long-running enough that the user might (intentionally or by accident) start a second `pix` command while the first is still going. Multiple writers can corrupt the library: both might mutate the same file, both might update the persistent metadata cache for the same path, both might allocate overlapping run-ids.

**Solution: a library-wide lock at `<library>\.pix\lock`.** A single sentinel file contains the PID, the op name, and the start timestamp:

```
12345
migrate
2026-05-23T15:32:01
```

Acquired at the start of any write-mode op (migrate, organize, dedupe, hash). Released on clean exit. If the file already exists when a new op starts:

- **PID is live** (the process exists and is a `pix` invocation) → refuse with `another pix process is running: PID 12345, op 'migrate', started 2026-05-23T15:32:01. Wait or kill it before retrying.` Exit non-zero before doing any work.
- **PID is dead** (process gone — crashed or killed) → assume stale, log `cleaning stale lock from PID 12345`, take the lock, proceed.

Lock files live in `.pix/lock` so they're excluded from sync clients alongside the rest of `.pix/` state.

`pix init` does not acquire the lock — it creates `.pix/` from scratch and has nothing to conflict with. Read-only future operations (e.g. `pix list`, if it lands) won't acquire the lock either. Anything that writes does.

This is intentionally a coarse lock: one operation at a time, library-wide. Finer-grained locking (e.g., letting `pix hash` run concurrently with `pix migrate` on disjoint subtrees) is a future-work concession, not a v1 design goal.

**Checkout freeze.** Separately from the per-invocation lock, an **open tag-editing checkout freezes the whole library**: while `<library>\.pix\checkout\` exists, every command except `pix checkout --commit` and `pix checkout --reset` refuses up front. A checkout materializes hard links whose identity commit relies on (NTFS file-ID); migrate/dedupe/organize/upgrade would all invalidate that identity if they ran mid-session. The freeze is enforced by folder presence (a checkout session spans many invocations, so the lock can't cover it). See [tag-editing.md → The freeze](tag-editing.md#the-freeze--an-open-checkout-locks-the-library).

## Open decisions

- **Dedupe design** — workflow, plan format, keeper selection, hash cache. Sketched in [dedupe.md](dedupe.md); full design deferred. (Migrate has now stabilized, so this is unblocked.)
- **Merge design** — workflow, plan format, reuse of `dedupe`. Deferred until dedupe stabilizes.
- **Apply-phase parallelism for migrate** — currently sequential. Lines are independent; a worker pool is a future perf-pass, not a v1 concern.
- **Face detection — deferred to last.** Migrate v1 *does not* detect faces or write `XMP-mwg-rs:RegionList`. The spec covers face workflow in [tag-editing.md](tag-editing.md) and the writing protocol in [tags.md](tags.md#structured-metadata-face-regions), but the migrate-time detection step (insightface + embedding match against confirmed identity centroids) is intentionally postponed until everything else in the spec is built. When it lands it integrates as another bundled step inside the existing TAG / CONVERT+RENAME+TAG action; no new top-level operation.
