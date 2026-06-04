# Dedupe

`pix dedupe <path>` removes duplicate files from the library, keeping one canonical copy per duplicate group. It runs against an already-normalized library — files have canonical formats and filenames, and the [content-hash cache](hash.md) is populated — and consumes the cached hashes rather than re-computing.

Splitting dedupe out of migrate keeps migrate a pure per-file in-place transform and lets dedupe focus on cross-file relational logic. `pix merge` will reuse the same primitive when it lands.

Like migrate and organize, dedupe is a single blocking, sequential command: plan → edit → confirm → apply. The same console/log split applies (silent during plan-gen except a single rewriting progress line; full per-file detail in `plan.log`).

## Scope

Dedupe is **library-wide**. `pix dedupe <path>` resolves the library root (walks up from the path, then PIX_ROOT, then CWD) and operates on every file under that root. There's no subfolder-scoped dedupe and no per-path policy — the library has one set of duplicate-resolution decisions.

### CWD constraint

Same rule as organize: the user must invoke `pix dedupe` from the library root or from a location outside the library. Dedupe removes files (moves them to `data/`) and sweeps the resulting empty folders; Windows can't remove a folder that's a process's working directory.

## What counts as a duplicate

Matching is **by media type**:

- **Images (and any non-video):** equal cached content hash. Exact, provable, byte-identical. The hash is format-aware (see [hash.md](hash.md)) — JPEGs strip APP-marker metadata before hashing — so TAG writes don't invalidate it, but format conversions do.
- **Videos (`mp4`/`mov`/`m4v`):** matched by **perceptual fingerprint**, not exact hash. The byte hash can't see that two *re-encodes of the same source* are the same — x265 vs `hevc_nvenc` (the GPU/CPU hybrid), or the same encoder across versions, produce different `mdat` bytes. The fingerprint (see [`video_fingerprint`](../src/pix/video_fingerprint.py)) is `K` dHashes of frames sampled at fixed *fractional timestamps* of the decoded picture — encoder-, GOP-, and quality-robust. Cached per file as `.vfp` (validated by size+mtime), computed once on the first dedupe/sync after a video is added.

Two videos are duplicates when they share resolution, durations within `_DUR_TOL` (0.75s), and a fingerprint Hamming distance within the band `[--min, --max]`. Default band **0–30**: on the real ~14k-video library, visual review confirmed 0–30 is all true duplicates, 30–40 is ambiguous, and 40+ diverges. `select_video_keeper` ranks the keeper by **resolution → bitrate (size÷duration) → duration → path** (different copies genuinely differ in quality — unlike byte-identical exact dups, where the keeper is arbitrary). Same-resolution bucketing means cross-resolution duplicates are a deliberate v1 exclusion.

Bare `pix dedupe` (and `pix sync`) apply images-exact + videos-perceptual `[0,30]` and conserve removed files to the run folder (recoverable). `--min/--max` rescope the video band; **`--checkout`/`--commit`** add a human-review gate (below) for curating higher/ambiguous bands.

## Review: checkout / commit

For bands you don't want applied unattended, `--checkout <dir>` stages instead of deleting:

1. **`pix dedupe <path> --checkout <dir> [--min N --max M]`** — group, then write into `<dir>` one stacked **montage** per video group (keeper strip on top, duplicates below) plus a machine-readable `manifest.json`. Deletes nothing. Holds the library lock only while scanning/grouping/writing the manifest, then **releases it** — montages render lock-free, and the (possibly long) human-review window holds no lock, so organize/migrate can run meanwhile.
2. **Curate** — the reviewer deletes the montage of any group they *don't* want deduped (same gesture as deleting a line from a migrate plan).
3. **`pix dedupe --commit <dir>`** (no path — the manifest names the library) — re-groups the library fresh (so everything is re-validated against current bytes), keeps only the groups whose montage still exists, and applies those. A group whose membership changed since checkout no longer matches and is skipped (reported), so staleness is safe by construction.

There's no global lock spanning the review window; the manifest is the state, and commit re-validates. This mirrors the editable-plan philosophy of migrate.

## Prerequisites

Plan-gen refuses if any file in the library has:

- **No `pix:OriginalPath`** (un-migrated file). Migrate hasn't seen it; dedupe shouldn't either. Surface paths, tell user: `Run "pix migrate <library-root>" first.`
- **Missing or stale cached hash** (file under the library has no `.pix/cache/.../<filename>.hash` entry, or the cached `(size, mtime_ns)` no longer matches). Surface paths, tell user: `Run "pix hash <library-root>" first.` See [hash.md](hash.md) — hash is a separate command precisely so migrate's hot path doesn't pay full-file BLAKE3 cost on every file; dedupe stays a pure consumer.

Both refusals exit non-zero before any plan is written.

## Keeper selection

Within a duplicate group (≥ 2 files sharing a hash), one file is the **keeper** and the rest are **losers** (marked for removal).

For **exact (hash) groups** the rule is simply the **lex-smallest library-relative path** (forward-slash-normalized, case-insensitive, ascending). There is no investment tier: the [tag merge](#tag-merge) below consolidates every file's user investment (and best auto values) onto whichever file survives, so keeper selection no longer has to protect against losing an override. It just needs to pick a deterministic survivor — the members are byte-identical anyway.

For **perceptual (video) groups** the members differ in quality, so the keeper is the best copy: **resolution → bitrate → duration → lex path** (`select_video_keeper`). The tag merge still rides along onto whichever copy wins.

Year-prefixed canonical date folders (`2023/...`) sort before letters in lex order, so the "in the date tree" file usually wins — and `organize` re-derives location from the effective date afterward regardless, so the keeper's starting location isn't a durable property worth optimizing for.

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
L003 | MERGE | 2023/08/Hawaii/2023-08-15_143205.jpg | event_auto →'Hawaii' (merge ←archive/…)

# Group 2 — hash def456…, 2 files
# Keeper: 2023/12/2023-12-25_090015.jpg
# WARNING: date_override: losers diverge ['2019-*-*-*:*:*', '2020-*-*-*:*:*'] — took '2019-*-*-*:*:*'
L004 | DEDUP | imports/2023-12-25_090015.jpg     | hash def456…
L005 | MERGE | 2023/12/2023-12-25_090015.jpg     | date_override →2019-*-*-*:*:*

# Summary: 3 DEDUP, 2 MERGE across 2 groups
```

- Header per group: hash prefix (first 8–12 chars for readability), file count, keeper path.
- One DEDUP line per loser. The user can delete a specific line to skip that one delete; other losers in the same group still go.
- At most one MERGE line per group, on the keeper, listing the field consolidations (see [Tag merge](#tag-merge)). Deleting it skips the metadata merge but still removes the duplicates.
- `# WARNING` lines (info-only comments) flag fill-empty divergence — which value was taken and which were dropped. The dropped values survive on the losers under `data/`.
- Re-keeper is not directly editable via the plan in v1. To pick a different keeper, the user deletes the DEDUP line(s) for the would-be-new-keeper (so it survives) and lets the rest of the group's lines apply — the survivor becomes the keeper by virtue of being the only one left. Documented in the plan header as a usage tip.

## Tag merge

A duplicate group is the *same image* recorded in several copies, and the copies often disagree on metadata — one copy kept its EXIF while another was stripped on a download, one sits in an event-named folder while another doesn't, one carries a user override. Rather than pick a single "best file" and discard the rest's metadata, dedupe **assembles the best value of each tag across the group onto the keeper**. Which file is the keeper is therefore mostly irrelevant — it's the surviving path, not the surviving metadata.

The merge is **per tag**, independent for each:

| Tag | Merge rule |
|---|---|
| **date** (`pix:MergeDate`) | The **earliest** resolved `_auto` date across the group, written to `pix:MergeDate` on the keeper **if it's earlier than (or the keeper has no) resolved date**. `pix:DateAuto` is rewritten from it for immediate effect. Rationale: corruption almost always stamps *later* (copy mtimes, re-saves, downloads; future dates are already rejected), so the earliest observed date across identical copies is the best proxy for the true capture time — no source-tier ranking needed. `pix:MergeDate` sits at the top of the `DateAuto` cascade (see [tags.md](tags.md#dateauto-derivation)), so the keeper re-derives it durably and deleting it reverts cleanly. |
| **event** (`pix:MergeEvent`) | **Fill-empty**: if the keeper's resolved `EventAuto` is empty and a loser has one, adopt it (written to `pix:MergeEvent`, with `pix:EventAuto` rewritten). Two different non-empty event texts can't be ranked, so we never overwrite the keeper's own. |
| **`pix:DateOverride`** | **Fill-empty**: keep the keeper's own override if it has one; otherwise adopt a loser's. Overrides are user intent, so a merged override goes straight into the real override slot (no `Merge*` field) and never clobbers the keeper's own. |
| **`pix:EventOverride`** | Same fill-empty rule as `DateOverride`. |
| **`pix:OriginalPath`** | Keeper's own. Write-once provenance — not mergeable. |
| **`*AutoPrevious`** | Not merged (per-file dirty flags). Dedupe sets `pix:DateAutoPrevious` only if its `MergeDate` change moves `DateAuto` while a pinning `DateOverride` is present — mirroring migrate's dirty-flag rule. |
| **face regions** | Out of scope until face detection ships. |

**Divergence.** For the fill-empty fields, when the keeper's slot is empty and **two or more losers contribute different values**, dedupe takes the value from the **lex-smallest contributor** (fully deterministic) and emits a `# WARNING` line in the plan naming the dropped value(s). Nothing is truly lost — every loser's full XMP is conserved under `data/` (below). `MergeDate` needs no divergence handling: `min()` is unambiguous.

The merge produces at most one `MERGE` plan line per keeper (the bundle of field writes); a group whose keeper already holds the best of every tag produces none.

## Conservation

Every DEDUP removal moves the loser into `runs/<run-id>/data/L<NNN>_<original-filename>`. The full file (with its XMP) lives there for rollback. No copy, no extra storage — a single atomic same-volume rename. A loser's metadata that didn't win the merge survives here.

A `MERGE` write is also conservation-captured: before mutating the keeper, its prior XMP is exported to `runs/<run-id>/data/L<NNN>_<keeper-filename>.xmp` (same discipline as migrate's TAG writes). The keeper file itself is not moved — only its metadata changes — so the sidecar is the rollback record for the merge.

Writing `pix:*` metadata changes the keeper's mtime, invalidating its cache entries' `(size, mtime_ns)` key. The `.meta`/`.video` entries are dropped (re-derived later). The **content hash is unchanged** — XMP lives in stripped-before-hashing regions (APP markers for JPEG, non-`mdat` for MP4; see [hash.md](hash.md)) — so the keeper stays in its duplicate class; rather than drop the `.hash` entry, dedupe **re-stamps it in place** with the keeper's new `(size, mtime_ns)`, preserving the (still-correct) hash value. This matters because `pix sync` runs `organize` immediately after dedupe, and organize requires a valid cached hash for every file and won't compute one — dropping the keeper's hash would abort the sync with `N file(s) ... lack a cached content hash`. (A keeper with no prior cached hash is left dropped — the degenerate case can't happen via sync, where `hash` runs first.)

**Keeper that can't be tagged → quarantine.** If the MERGE write doesn't persist — a structurally damaged keeper (truncated `mdat`, etc.) where ExifTool reports `0 image files updated` and `write_tags` raises `TagWriteFailed` — dedupe moves that keeper into `.pix/errors/` (same mechanism as migrate's failure quarantine; bytes intact, ExifTool didn't touch it) and continues. The group's losers are still removed (all copies conserved under `data/` / `errors/`). This surfaces the damaged file rather than leaving it un-taggable in the library, where it would otherwise keep tripping a later organize. Dedupe exits non-zero when any keeper was quarantined.

## Empty-folder cleanup

Same rule as organize: after all DEDUP moves apply, walk the library bottom-up and remove empty folders. Never touches `.pix/` and never removes the library root itself. A folder containing only `.pix/` at the top level is preserved.

## Atomicity and crash recovery

Each DEDUP line is a single same-volume rename — atomic on its own. A MERGE line is a sidecar export followed by an in-place ExifTool write on the keeper. No markers.

**Apply order: MERGE before DEDUP.** All MERGE writes run before any loser removal. This is what makes a mid-apply crash recoverable: if the merge committed but the removals didn't, the next `pix dedupe` re-plans and finds the keeper already holds the merged values (so the merge is a no-op — see Idempotence) while the losers are still present. The reverse order would be lossy — removing the losers first, then crashing, would strand the to-be-merged values on files that are gone from the live library (still in `data/`, but no longer auto-consolidated).

Mid-apply crashes otherwise leave some losers moved into `data/` and others still in place; the next run re-plans from current state (the keeper survives, fewer losers remain, the plan shrinks). No special cleanup pass needed.

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
    L003_<keeper-filename>.xmp   — keeper's pre-merge XMP (one per MERGE line)
    ...
```

## Idempotence

A library with no duplicates produces an empty plan. Re-running dedupe with no input changes produces no work. The plan summary line reads `Summary: 0 DEDUP, 0 MERGE across 0 groups.`

Merges are idempotent too: once a keeper holds the consolidated values (`pix:MergeDate`/`pix:MergeEvent` written, overrides filled), a re-plan sees the keeper's own resolved values already equal the group's best, so no MERGE line is emitted. Every field write is compared against the keeper's current value and dropped when equal.

## Console output

Same policy as migrate and organize:

- During plan-gen: silent except the single rewriting `NNN% Xphase - hashing <path>` progress line (front-of-line phase elapsed per [migrate.md → Console output](migrate.md#console-output)). All phase headers, file counts, per-file group assignments → `plan.log`.
- During apply: single rewriting `NNN% Xphase - L042 DEDUP <path>` line.
- After plan-gen: `Plan written: ...`, `Summary: N DEDUP, K MERGE across M group(s).`, `Apply? [Y/e/n]`. `--no-prompt` skips the confirmation and applies the full plan (still written); used by [`pix sync`](sync.md).
- After apply: `Removed N duplicate(s) across M group(s).` plus `Merged tags onto K keeper(s).` when any merge applied.

Errors and aborts still print directly to stderr.

## Rollback (deferred)

`pix rollback <run-id>` reads `plan.txt`, moves each `data/L###_<filename>` back to its original library-relative path, and restores each keeper's pre-merge XMP from its `data/L###_<keeper>.xmp` sidecar (undoing the MERGE writes). Sketched; full design deferred until rollback ships.

## Known v1 limitations

- **Cross-format duplicates are not detected.** A HEIC and its JPG conversion are content-equivalent but have different format-aware hashes. Future tier-2 perceptual hashing or `pix:OriginalPath`-chain detection will address this.
- **Override divergence is resolved by lex order, not surfaced for choice.** When two losers contribute conflicting overrides to an empty keeper slot, dedupe takes the lex-smallest deterministically and warns; it doesn't pause for the user to pick. The dropped values remain on the losers under `data/`.
- **No interactive keeper override.** The user can skip a delete (line deletion) but not directly say "make this file the keeper instead." Workaround: delete the lines for the file you want to keep, which lets it survive while the rest of the group is removed.
- **No near-duplicate detection.** Only exact hash matches. Burst photos, slightly-edited versions, re-saved JPEGs are separate files to dedupe.
