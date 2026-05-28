# Tag editing — `pix checkout`

The workflow for applying user **overrides** on `_auto` tags after [migrate](migrate.md) has run. Migrate writes heuristics (`_auto` baselines); this workflow captures human judgment (`DateOverride`, `EventOverride`, face identities). See [tags.md](tags.md) for the tag model this edits.

Unlike migrate (per-file, in-place) and organize (structural moves), tag-editing is a **folder-shuffle UI**: pix materializes a temporary workspace of links shaped by a template, the user rearranges links in their file explorer, and `pix checkout --commit` infers the implied tag changes from where things ended up.

## CLI surface

All tag-editing is expressed as actions on the single `pix checkout` command:

| Invocation | Effect |
|---|---|
| `pix checkout <path> <template>` | Start a checkout scoped to `<path>` and below: materialize the workspace for `<template>`. Both args required — the path resolves the library root and bounds the file set, exactly like `pix migrate <folder>`. Refuses if a checkout is already open. |
| `pix checkout --commit` | Diff the open checkout against its snapshot, write the inferred tag changes, tear the workspace down. No positional args. Errors if no checkout is open. |
| `pix checkout --reset` | Discard the open checkout (delete the workspace + snapshot). No tags written. No positional args. Errors if no checkout is open. |
| `pix checkout` (no args) | Status: if a checkout is open, print its template, scope, creation time, link count, and workspace path, plus a hint to `--commit`/`--reset`. If none is open, print a usage hint. |

`--commit` and `--reset` are mutually exclusive and take no positional arguments.

## The cycle

1. **`pix checkout F:\photos\2023 {year}/{event}`** builds `<library-root>/.pix/checkout/` — a folder tree shaped by the template, whose leaves are **hard links** to the library files under `<path>` (here `F:\photos\2023`; see [Scope](#scope) and [Materializing the workspace](#materializing-the-workspace)). A snapshot of the starting state is written to `.pix/checkout/snapshot.json`.
2. The user shuffles links between folders in their file explorer (over minutes, hours, or days).
3. **`pix checkout --commit`** diffs the workspace against the snapshot, infers the implied tag changes, shows a plan, and on accept writes the override tags into the files and tears the workspace down. Or **`pix checkout --reset`** throws the session away.

Commit **only writes tags.** It does not rename files or move them between folders. The library therefore becomes *eventually consistent*: a committed date change is reflected in the file's canonical name on the next [`pix migrate`](library.md#canonical-filename) (RENAME) and in its folder location on the next [`pix organize`](organize.md) (MOVE). Commit captures intent; migrate/organize materialize it.

## The freeze — an open checkout locks the library

While `.pix/checkout/` exists, **every pix command except `pix checkout --commit` and `pix checkout --reset` refuses up front**, with:

```
A checkout is open (template: {year}/{event}, started 2026-05-28 15:00).
Run `pix checkout --commit` or `pix checkout --reset` before any other operation.
```

This includes `migrate`, `organize`, `dedupe`, `hash`, and `upgrade`. Exit non-zero, no work done.

### Why freeze

A checkout link and its library file are two directory entries pointing at the **same inode**; commit identifies files by that inode (see [Identity model](#identity-model)). Several ops would silently break that identity if they ran during a session:

- **migrate TAG re-derivation** rewrites `_auto` via ExifTool `-overwrite_original`, which writes a temp file and renames it over the original — swapping the library path onto a **new inode** and orphaning the checkout link (it now points at stale, detached bytes). Pure RENAME is safe, but a full migrate run also re-derives tags every pass, so migrate as a whole is not safe.
- **dedupe** moves a redundant file's directory entry into a run folder — the inode survives but is no longer in the library tree, so the checkout link points at something that is no longer a library file.
- **organize** moves files; **upgrade** archives everything under `.pix/` (it would sweep the live checkout into `archive/`).

Rather than build drift-tolerance (re-identifying orphaned links by content hash, mapping deduped files to surviving keepers), the freeze makes the whole model provably correct: nothing mutates library inodes mid-session, so the snapshot stays valid and commit's identity is exact. The cost — no imports/normalization while a checkout is parked open — is acceptable for a single-user, transient editing session, and `--reset` is the escape hatch.

The freeze is enforced by **folder presence**, not the library lock (the lock lives for one invocation; a checkout session spans many). Each command, after resolving the root, checks for `.pix/checkout/` and refuses if present.

## Scope

The required `<path>` argument does double duty, exactly like `pix migrate <folder>`:

- **Resolves the library root** — pix walks up from `<path>` looking for `.pix/`.
- **Bounds the file set** — only files at or under `<path>` are linked into the workspace. A whole-library checkout is `pix checkout <library-root> <template>` (or `pix checkout . <template>` run from the root).

`<path>` must resolve to a location **at or under the library root**, and **not inside `.pix/`** (tool scaffolding — nothing to edit). Either violation aborts before any work.

Scope is by **physical location**, not tag value: `pix checkout F:\photos\2023 {event}` checks out whatever currently lives under `2023\`. Because the library's shape is organize-driven this usually lines up with the obvious dimension, but the rule is simply "files under this path, now."

Scoping keeps the workspace navigable on a large library — materializing hard links for an entire TB-scale tree (and then shuffling it in Explorer) is unwieldy; a scoped checkout is something a human can actually work through.

Unlike [organize](organize.md#cwd-constraint), checkout has **no CWD constraint** — it never moves library files or removes empty folders, so running it from inside the library (`pix checkout . <template>`) is a first-class use.

The [freeze](#the-freeze--an-open-checkout-locks-the-library) is always **library-wide**, regardless of scope: an open checkout blocks every other op everywhere, because they could invalidate the checked-out inodes no matter which subtree was scoped.

## Prerequisites

Checkout aborts before doing any work if **any file under `<path>` lacks `pix:OriginalPath`** (un-migrated — no effective tag values to render the workspace from). Only the scoped subset is checked, since only those files are linked. Surface the offending paths and tell the user to run `pix migrate` on them first.

Unlike organize, checkout does **not** require `pix hash` — identity is by inode (see [Identity model](#identity-model)), not content hash, so no hash cache is needed.

## Materializing the workspace

`pix checkout <template>`:

1. **Resolves the library root** by walking up from `<path>` for `.pix/` (like `pix migrate <folder>`), **validates the template** against the [template grammar](tags.md#template-grammar), and **validates the scope** (see [Scope](#scope)).
2. **Allocates** `<library-root>/.pix/checkout/` (fails if it already exists — one checkout at a time).
3. **Builds the link tree.** For each library file **under `<path>`**, compute the effective value of each template token, render + sanitize each level to a folder name (identical rules to [organize](organize.md#folder-name-sanitization)), and create a **hard link** at `checkout/<level1>/<level2>/.../<canonical-filename>`. Per-leaf name collisions get the same `_NNN` suffix as organize (cosmetic — identity is not name-based).
4. **Writes the snapshot** (see below).
5. Acquires the library lock only **briefly** during materialization, then releases it. The editing session itself is not under the lock; the [freeze](#the-freeze--an-open-checkout-locks-the-library) guards concurrency instead.

Hard links (not moves) mean the library is untouched while the user shuffles, and they're free on the same NTFS volume (the [same-volume invariant](README.md#cross-cutting-invariants) already holds). Hard links also look like ordinary files in Explorer, so thumbnails and double-click-to-open work exactly as for the real photo.

### Template scope: single-valued tokens, compound allowed

`checkout` accepts a **compound** template — any arrangement of single-valued tokens (`{year}`, `{month}`, `{day}`, `{event}`, and `{date}`) across slash-separated levels (`{year}/{event}`, `{year}/{month}/{event}`, …). **Every level is editable.** Because each file has one value per single-valued token, each file appears exactly **once** in the tree, which is what makes inode identity unambiguous.

Multi-valued tokens (`{person}`, `{face}`) must be the **sole** token in a checkout — a file appears once per value, so the tree carries duplicates of the same inode. The canonical multi-valued flow is the [face-specific checkout](#face-specific-checkout-face) below, which is **deferred** along with migrate-time face detection.

`{time}` is rejected as a folder level (per-second folders are useless), same as organize.

## Identity model

Commit must answer, for every link it finds: *which library file is this, and what were its tag-values at checkout?* The join key is the **NTFS file ID** — `(st_dev, st_ino)`, which Python 3.12 populates from the file index on Windows. All hard links to one file share it.

Under the [freeze](#the-freeze--an-open-checkout-locks-the-library) the library is immutable for the duration of the session, so each snapshot record's `library_path` also stays valid — commit reads it directly to know where to write tags, and uses the inode only to match a shuffled link back to its snapshot record.

A content-hash fallback (recognizing user-created byte-copies, mapping deduped files to keepers) is **reserved for the deferred multi-valued face flow**; the single-valued flow under the freeze does not need it.

## The snapshot

Written at checkout time to `.pix/checkout/snapshot.json` — the baseline commit diffs against:

```jsonc
{
  "template": "{year}/{event}",
  "scope": "F:/photos/2023",
  "created": "2026-05-28T15:00:00",
  "links": [
    {
      "ino": "0x00050000_0000000000012345",   // st_dev + st_ino, the join key
      "library_path": "F:/photos/2023/Hawaii/2023-08-15_143205.jpg",
      "values": { "year": "2023", "event": "Hawaii" }   // effective values at checkout
    }
  ]
}
```

- `template` — defines which level maps to which token (level *i* → token *i*), so commit doesn't need the user to re-supply it.
- `scope` — the path the checkout was bounded to (informational; recorded for the status line and audit).
- `ino` — the identity join key.
- `library_path` — where to write tags (valid for the whole session thanks to the freeze).
- `values` — the "before" side of the diff.

For single-valued (compound) checkouts each `ino` appears once. Face checkouts (deferred) record one entry per (file, region) and add region detail; that schema extends this when the face flow lands.

## Folder-shuffle conventions

Commit reconstructs intent purely from **where each link ended up**.

| Action in the workspace | Inferred change |
|---|---|
| Move a link to a different **valued** folder at some level | Set that level's token to the destination value. |
| Move a link into a level's **`null/`** folder | Clear that token (set the override field to `*` / remove the value). |
| Move a link across **multiple** levels at once | One change per affected token, **bundled into a single TAG line** for that file. |
| **Hard-delete** a link (Del / Recycle Bin / move out of the workspace) | **Clear all editable tokens** for that file (revert every override the template covers back to `_auto`). Generalizes the single-token "delete = null" rule; reversible (conservation captures prior XMP) and surfaced in the commit plan before anything is written. |
| Move a link into the workspace's **`pending-delete/`** folder | Mark the underlying **file** for deletion: captured into the commit run folder and removed from the library. |

`null/` and `filtered/` levels follow the same per-level semantics organize uses (see [organize → Null and filtered placement](organize.md#null-and-filtered-placement)).

### Date overrides are wildcard patches

`pix:DateOverride` is a single wildcard string (see [tags.md → Date override is a wildcard string](tags.md#date-override-is-a-wildcard-string)); a checkout on a derived date component only patches that field:

| Folder action | Effect on `pix:DateOverride` |
|---|---|
| `{year}` level `2023/` → `2022/` | Set the **year** field to `2022`; other fields unchanged. |
| `{month}` level `08/` → `03/` | Set the **month** field to `03`. |
| Any date-component level → `null/` | Clear that field (set to `*`). |

If patching leaves the override all-`*`, the `pix:DateOverride` field is **removed** entirely (equivalent to absent — see the all-wildcards rule in tags.md). If no override existed and the patch introduces a non-`*` field, `DateOverride` is created.

### Cleaning up `*AutoPrevious` on override changes

When a commit changes an override field, it reconciles the corresponding `*AutoPrevious` (see [tags.md → Auto-previous fields](tags.md#auto-previous-fields-dirty-flagging)):

- **Override removed entirely** (`DateOverride` field deleted, or `EventOverride` cleared) → delete `DateAutoPrevious` / `EventAutoPrevious` if present. Without an override there's nothing being masked, so the dirty flag is no longer meaningful.
- **Override modified but still present** → leave `*AutoPrevious` in place; the user adjusted their override but hasn't resolved the drift.

Commit handles this implicitly as part of writing the override change; no separate user action.

## Commit: inferring and applying changes

`pix checkout --commit`:

1. **Acquire the library lock**; load `.pix/checkout/snapshot.json`.
2. **Allocate a run folder** `<library-root>/.pix/runs/<run-id>/` (same shape and id format as migrate's).
3. **Walk the workspace.** For each leaf link: split its workspace-relative path into level components (mapping each to its token via `template`; a `null/` component ⇒ that token is null), and `stat` it to get its `ino`. Collect any links under `pending-delete/` separately.
4. **Diff against the snapshot**, per snapshot record:
   - In `pending-delete/` → emit a **`DELETE`** line for `library_path`.
   - Present in the workspace → compare current token tuple vs. snapshot `values`; each differing token is one field edit. Bundle all of a file's edits into one **`TAG`** line.
   - Absent from the workspace (hard-deleted) → clear all editable tokens (emit a `TAG` line if it actually changes anything).
   - Defensive: if `library_path` no longer exists, the freeze was violated externally — abort with a clear error rather than guessing.
5. **Write the plan** to `runs/<run-id>/plan.txt`, print the summary, and prompt `Apply? [Y/e/n]` — identical edit/confirm loop to migrate and organize (`e` opens `$EDITOR`, re-reads, re-summarizes; deleting a line skips that file this commit).
6. **Apply** sequentially. This is migrate's TAG/DELETE machinery verbatim:
   - **TAG** — export the file's current XMP to `runs/<run-id>/data/L<NNN>_<name>.xmp` (conservation capture), then one ExifTool `-overwrite_original` call writing the changed `pix:*` override fields (plus any `*AutoPrevious` reconciliation). Atomicity is ExifTool's own (see [migrate → Example 3](migrate.md#example-3--tag-only)).
   - **DELETE** — move the file → `runs/<run-id>/data/L<NNN>_<name>` (one rename; capture + removal).
7. **Tear down** `.pix/checkout/` (unlink the hard links, delete the snapshot) on a successful apply. Removing hard links never touches the library files. The freeze is now lifted.

`plan.txt` is immutable once written; progress streams to `apply.log`. No markers are needed — TAG and DELETE are each a single atomic step (no CONVERT decomposition).

### Worked example

Checkout `{year}/{event}`. File `…143205.jpg` starts at `2023/Hawaii/`. The user drags its link to `2022/null/`.

- Snapshot record: `ino=X, library_path=…/2023/Hawaii/2023-08-15_143205.jpg, values={year:2023, event:Hawaii}`.
- Commit finds the link at `2022/null/…`, `stat`s it → `ino=X`, joins to the record.
- Current tuple `{year:2022, event:null}` vs. snapshot `{year:2023, event:Hawaii}` ⇒ year `2023→2022`, event `Hawaii→cleared`.

```
L001 | TAG | F:\photos\2023\Hawaii\2023-08-15_143205.jpg | date_override year *→2022; event_override "Hawaii"→cleared
```

After apply the file carries `DateOverride = 2022-*-*-*:*:*` and no `EventOverride`. Its effective date is now 2022, but it still sits in `2023/Hawaii/` under the `2023-…` name until the next `migrate` (renames it) and `organize` (relocates it).

### Plan format

```
# Commit plan: F:\photos
# Generated 2026-05-28 16:10
# Run ID: 2026-05-28_16-10-00
# Template: {year}/{event}
# Source: checkout started 2026-05-28 15:00
#
# Delete a line to skip that file this commit. Commented "#" lines are info only.
# Format: L<line-id> | ACTION | path | details

L001 | TAG    | F:\photos\2023\Hawaii\2023-08-15_143205.jpg | date_override year *→2022; event_override "Hawaii"→cleared
L002 | TAG    | F:\photos\2023\null\2023-09-01_120000.jpg   | event_override null→"Birthday"
L003 | DELETE | F:\photos\2023\Hawaii\2023-08-15_143612.jpg | pending-delete

# Summary: 2 TAG, 1 DELETE
```

Plan-line details show the **override-field operation** (what gets written), since that's the durable change. One line per file; multiple token edits on a file bundle onto its single line.

### Run folder layout

Matches migrate's:

```
runs/<run-id>/
  plan.txt          # editable, then immutable
  plan.log          # phase headers + per-file decisions
  apply.log         # per-line Started/Completed/Failed transitions
  data/
    L<NNN>_<name>.xmp   # prior XMP for each TAG line (rollback record)
    L<NNN>_<name>       # captured file for each DELETE line
```

Conservation holds exactly as in migrate: nothing is destroyed without a capture. `pix rollback <run-id>` (deferred) reverses TAG writes from the `.xmp` sidecars and restores DELETE captures.

## Reset

`pix checkout --reset` removes `.pix/checkout/` (links + snapshot) and exits. No tags are written, no run folder is allocated, the library is untouched, and the freeze lifts. It's the throw-away for a checkout the user no longer wants to commit.

## Face-specific checkout: `{face}` (deferred)

> **Status: deferred.** Depends on migrate-time face detection, which is [postponed until last](README.md#open-decisions). The semantics below are designed; they build on the multi-valued single-token rule above and will extend the snapshot schema with per-region identity when implemented.

`pix checkout {face}` is the canonical way to label faces, including cold-start (zero confirmed identities). No separate seeding command.

Each face region (see [tags.md](tags.md#structured-metadata-face-regions)) has three states based on identity + `confirmed` flag:

| State | Identity | Confirmed | Source |
|---|---|---|---|
| Confirmed | set | true | User named it |
| Suggested | set | false | Migrate matched its embedding to a known identity's centroid |
| Unidentified | null | false | Auto-detected, no centroid match |

### Materialized workspace

The checkout renders face crops (hard-linked from the `.pix/faces/` cache) into folders:

```
checkout/
  James/                  # confirmed: identity=James, confirmed=true
  Mom/
  ?James/                 # suggested: identity=James, confirmed=false — needs review
  ?001/                   # unidentified cluster of similar embeddings
  ?002/
```

- **No `?` prefix** → confirmed identity folder.
- **`?<name>/`** → migrate-suggested for that name; user reviews.
- **`?NNN/`** → unidentified cluster. Cluster IDs assigned on-demand at checkout time (not stored on regions), ordered by descending cluster size (so `?001` is typically the largest pile).

Clustering: HDBSCAN with `min_cluster_size=3`. Confirmed identity centroids participate as seeds, so a new face near an established identity routes to `?<name>/` rather than a fresh cluster.

### Suggestion threshold

During migrate, a newly detected face's embedding is compared to confirmed identity centroids. Closest match above cosine similarity **0.5** → pre-assigned (`confirmed=false`); below → identity stays null. Configurable via `--face-suggest-threshold`.

### Folder-shuffle semantics (face checkout)

| Action | Commit effect |
|---|---|
| Rename `?001/` → `Dad/` (new name) | All faces become identity=Dad, confirmed=true |
| Rename `James/` → `Jim/` | Re-identify all faces in folder to Jim |
| Move face from `?James/` to `James/` | Confirm the suggestion |
| Move face from `James/` to `Mom/` | Re-identify (correct a confirmed mistake) |
| Move face from `James/` to `?001/` | Reject identity; clear it back to unidentified |
| Move face to `pending-delete/` | Remove that face region from the source file (false positive) |

Bulk relabeling is just folder rename or many moves — no special command.

### Deferred (face)

- `pix relabel <old> <new>` CLI shortcut — same effect as renaming a folder in checkout, without materializing a workspace.
- Active learning (surfacing borderline matches below the suggestion threshold).
- Quality-filtering of face crops (blurry, partial) before clustering.
