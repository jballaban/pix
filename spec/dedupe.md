# Dedupe

`pix dedupe <path>` removes duplicate files from the library, keeping one canonical copy per duplicate group. It runs against an already-normalized library — files have canonical formats and filenames, and the [content-hash cache](hash.md) is populated — and consumes the cached hashes rather than re-computing.

Splitting dedupe out of migrate keeps migrate a pure per-file in-place transform and lets dedupe focus on cross-file relational logic. `pix merge` will reuse the same primitive when it lands.

Like migrate and organize, dedupe is a single blocking, sequential command: plan → edit → confirm → apply. The same console/log split applies (silent during plan-gen except a single rewriting progress line; full per-file detail in `plan.log`).

## Scope

Dedupe is **library-wide**. `pix dedupe <path>` resolves the library root (walks up from the path, then PIX_ROOT, then CWD) and operates on every file under that root. There's no subfolder-scoped dedupe and no per-path policy — the library has one set of duplicate-resolution decisions.

### CWD constraint

Same rule as organize: the user must invoke `pix dedupe` from the library root or from a location outside the library. Dedupe removes files (moves them to `data/`) and sweeps the resulting empty folders; Windows can't remove a folder that's a process's working directory.

## What counts as a duplicate

Two files are duplicates if their cached content-hash values are equal. Nothing else.

The hash is format-aware (see [hash.md](hash.md)): JPEGs strip APP-marker metadata before hashing, MP4s hash only `mdat` payloads. This means metadata changes (TAG writes) don't invalidate the hash, but **format conversions do** — a HEIC and its JPG conversion have different hashes despite sharing a source. Cross-format dedupe is **out of scope for v1**; it would need perceptual hashing or `pix:OriginalPath`-lineage detection, both deferred.

## Prerequisites

Plan-gen refuses if any file in the library has:

- **No `pix:OriginalPath`** (un-migrated file). Migrate hasn't seen it; dedupe shouldn't either. Surface paths, tell user: `Run "pix migrate <library-root>" first.`
- **Missing or stale cached hash** (file under the library has no `.pix/cache/.../<filename>.hash` entry, or the cached `(size, mtime_ns)` no longer matches). Surface paths, tell user: `Run "pix hash <library-root>" first.` See [hash.md](hash.md) — hash is a separate command precisely so migrate's hot path doesn't pay full-file BLAKE3 cost on every file; dedupe stays a pure consumer.

Both refusals exit non-zero before any plan is written.

## Keeper selection

Within a duplicate group (≥ 2 files sharing a hash), one file is the **keeper** and the rest are **losers** (marked for removal).

The rule is **lex-smallest library-relative path, with a tier-break for user investment**:

1. **Invested vs pristine**: a file is "invested" if it has `pix:DateOverride` set OR `pix:EventOverride` set (any non-`*` slot in DateOverride counts; any non-null EventOverride counts). Files with face regions become a third investment signal once face detection ships — out of scope while migrate doesn't write regions.
2. Within each tier (invested vs pristine), sort by **library-relative path** forward-slash-normalized, case-insensitive, ascending. First wins.
3. Across tiers: invested beats pristine. If any file in the group is invested, the keeper is the lex-smallest invested file. Otherwise it's the lex-smallest pristine file.

This means a duplicate group of two files where one has the user's `pix:DateOverride` set always keeps that one — the user's investment isn't silently thrown away by sort order alone.

Year-prefixed canonical date folders (`2023/...`) happen to sort before letters in lex order, so the "in the date tree" file usually wins the pristine-tier tie-break — a happy accident rather than a designed property.

## Plan format

Grouped: comment header per duplicate set, one DEDUP line per loser.

```
# Dedupe plan: F:\photos
# Generated 2026-05-21 15:00
# Run ID: 2026-05-21_15-00-00
#
# Delete a line to skip that file this run. Commented "#" lines are info only.
# Format: L<line-id> | ACTION | path | details

# Group 1 — hash abc123…, 3 files
# Keeper: 2023/08/Hawaii/2023-08-15_143205.jpg
L001 | DEDUP | imports/old/2023-08-15_143205.jpg | hash abc123…
L002 | DEDUP | archive/2023-08-15_143205.jpg     | hash abc123…

# Group 2 — hash def456…, 2 files
# Keeper: 2023/12/2023-12-25_090015.jpg
L003 | DEDUP | imports/2023-12-25_090015.jpg     | hash def456…

# Summary: 3 DEDUP across 2 groups
```

- Header per group: hash prefix (first 8–12 chars for readability), file count, keeper path.
- One DEDUP line per loser. The user can delete a specific line to skip that one delete; other losers in the same group still go.
- Re-keeper is not directly editable via the plan in v1. To pick a different keeper, the user deletes the DEDUP line(s) for the would-be-new-keeper (so it survives) and lets the rest of the group's lines apply — the survivor becomes the keeper by virtue of being the only one left. Documented in the plan header as a usage tip.

## Conservation

Every DEDUP removal moves the loser into `runs/<run-id>/data/L<NNN>_<original-filename>`. The full file (with its XMP) lives there for rollback. No copy, no extra storage — a single atomic same-volume rename.

**Loser metadata is lost from the live library.** If a loser had user-set `pix:DateOverride` or `pix:EventOverride`, those values do not propagate to the keeper. The keeper-selection rule above prefers invested files specifically to minimize this case; when it happens despite that (e.g., two invested files with different overrides), the loser's overrides are preserved on the captured file under `data/` but not merged into the keeper. Future work: a tag-merge mode that surfaces conflicts and merges non-conflicting overrides.

## Empty-folder cleanup

Same rule as organize: after all DEDUP moves apply, walk the library bottom-up and remove empty folders. Never touches `.pix/` and never removes the library root itself. A folder containing only `.pix/` at the top level is preserved.

## Atomicity and crash recovery

Each DEDUP line is a single same-volume rename — atomic on its own. No markers. Mid-apply crashes leave some losers moved into `data/` and others still in place; the next `pix dedupe` re-plans from the current state (the keeper survives, fewer losers remain, the plan shrinks).

No special cleanup pass needed.

## Run folder layout

Identical to migrate/organize:

```
runs/<run-id>/
  plan.txt       — editable, then immutable
  plan.log       — per-file plan-gen decisions + phase headers
  debug.log      — verbose per-file reasoning (group membership, keeper choice)
  apply.log      — per-DEDUP Started/Completed/Failed transitions
  data/
    L001_<original-filename>     — captured losers
    L002_<original-filename>
    ...
```

## Idempotence

A library with no duplicates produces an empty plan. Re-running dedupe with no input changes produces no work. The plan summary lines reads `Summary: 0 DEDUP across 0 groups.`

## Console output

Same policy as migrate and organize:

- During plan-gen: silent except the single rewriting `NNN% Xphase - hashing <path>` progress line (front-of-line phase elapsed per [migrate.md → Console output](migrate.md#console-output)). All phase headers, file counts, per-file group assignments → `plan.log`.
- During apply: single rewriting `NNN% Xphase - L042 DEDUP <path>` line.
- After plan-gen: `Plan written: ...`, `Summary: ...`, `Apply? [Y/e/n]`.
- After apply: `Removed N duplicate(s) across M group(s).`

Errors and aborts still print directly to stderr.

## Rollback (deferred)

`pix rollback <run-id>` reads `plan.txt`, moves each `data/L###_<filename>` back to its original library-relative path. Sketched; full design deferred until rollback ships.

## Known v1 limitations

- **Cross-format duplicates are not detected.** A HEIC and its JPG conversion are content-equivalent but have different format-aware hashes. Future tier-2 perceptual hashing or `pix:OriginalPath`-chain detection will address this.
- **Loser overrides are not merged into the keeper.** Captured for rollback but not propagated.
- **No interactive keeper override.** The user can skip a delete (line deletion) but not directly say "make this file the keeper instead." Workaround: delete the lines for the file you want to keep, which lets it survive while the rest of the group is removed.
- **No near-duplicate detection.** Only exact hash matches. Burst photos, slightly-edited versions, re-saved JPEGs are separate files to dedupe.
