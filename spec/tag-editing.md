# Tag editing — `checkout` / `commit`

Separate workflow from [migrate](migrate.md). Used to apply user overrides on `_auto` tags after migrate runs. See [tags.md](tags.md) for the tag model this workflow edits.

## Workflow

1. `checkout <template>` builds a workspace under `<library-root>\.pix\checkouts\<id>\` whose folders match the template (see [template grammar](tags.md#template-grammar)). Files appear as hard links into the library. Multi-valued tags produce multiple links per file (one per value).
2. User shuffles files between folders in their file explorer.
3. `commit` diffs the workspace against the snapshot taken at checkout time, infers the implied tag changes, shows a diff, and applies on accept (writes tags into files; re-organizes the library if any changed tag is part of the active organize template).

Multi-valued tags work naturally — each (file, tag-value) pair is one link.

## Folder-shuffle conventions

Inside a checkout:

- **Move link to a different valued folder** → change the tag value to the destination.
- **Move link to `null/`** → clear the tag value (single-valued) or remove that value (multi-valued).
- **Hard-delete a link** (Recycle Bin / Del) → equivalent to moving to `null/`.
- **Move link to checkout's `pending-delete/` folder** → mark the underlying *file* for deletion. Commit warns about other links; on accept, the file is captured into the commit's run folder under `runs/<commit-run-id>/` (see [library.md](library.md#file-layout)).
- **Last link of a multi-valued tag's checkout deleted** → file appears in `null/`.

### Date overrides are wildcard patches

The date override is a single wildcard string (see [tags.md → Date override is a wildcard string](tags.md#date-override-is-a-wildcard-string)), but checkouts on derived date components (`{year}`, `{month}`, `{day}`, `{time}`) only affect the corresponding field of that string. Commit patches the relevant slot:

| Folder action | Effect on `pix:DateOverride` |
|---|---|
| Move file in `{year}` checkout from `2023/` to `2022/` | Set the year field of the override to `2022`. Other fields unchanged. |
| Move file in `{year}` checkout to `null/` | Clear the year field (set to `*`). |
| Move file in `{month}` checkout to `03/` | Set the month field to `03`. |

If after patching, the override is all `*` fields, the `pix:DateOverride` field is removed from the file entirely (it's equivalent to absent — see the all-wildcards rule in tags.md). Likewise, if no override existed before and the patch introduces a non-`*` field, `DateOverride` is created.

### Cleaning up `*AutoPrevious` on override changes

When a commit changes an override field, it reconciles the corresponding `*AutoPrevious` (see [tags.md → Auto-previous fields](tags.md#auto-previous-fields-dirty-flagging)):

- **Override removed entirely** (`DateOverride` field deleted, or `EventOverride` cleared) → delete `DateAutoPrevious` / `EventAutoPrevious` if present. The dirty flag is no longer meaningful: without an override, `_auto` is the effective value, so there's nothing being masked.
- **Override modified but still present** → leave `*AutoPrevious` in place. The user adjusted their override but hasn't explicitly resolved the drift; the dirty flag still applies until a future "resolve" action clears it.

Commit handles this implicitly as part of writing the override change; no separate user action is needed.

## Face-specific checkout: `{face}` workflow

`pix checkout {face}` is the canonical way to label faces, including cold-start (the library has zero confirmed identities). No separate seeding command.

Each face region (see [tags.md](tags.md#structured-metadata-face-regions)) has three states based on identity + `confirmed` flag:

| State | Identity | Confirmed | Source |
|---|---|---|---|
| Confirmed | set | true | User named it |
| Suggested | set | false | Migrate matched its embedding to a known identity's centroid |
| Unidentified | null | false | Auto-detected, no centroid match |

### Materialized workspace

The checkout renders face crops (hard-linked from `.pix/faces/` cache) into folders:

```
checkouts/<id>/
  James/                  # confirmed: identity=James, confirmed=true
  Mom/
  ?James/                 # suggested: identity=James, confirmed=false — needs review
  ?001/                   # unidentified cluster of similar embeddings
  ?002/
```

- **No `?` prefix** → confirmed identity folder.
- **`?<name>/`** → migrate-suggested for that name; user reviews.
- **`?NNN/`** → unidentified cluster. Cluster IDs assigned on-demand at checkout time (not stored on regions), ordered by descending cluster size (so `?001` is typically the largest pile).

Clustering: HDBSCAN with `min_cluster_size=3`. Confirmed identity centroids participate as seeds, so a new face near an established identity gets routed to `?<name>/` rather than a fresh cluster.

### Suggestion threshold

During migrate, a newly detected face's embedding is compared to confirmed identity centroids. Closest match above cosine similarity **0.5** → pre-assigned (`confirmed=false`); below threshold → identity stays null. Configurable via `--face-suggest-threshold`.

### Folder-shuffle semantics (face checkout)

| Action | Commit effect |
|---|---|
| Rename `?001/` → `Dad/` (new name) | All faces become identity=Dad, confirmed=true |
| Rename `James/` → `Jim/` | Re-identify all faces in folder to Jim |
| Move face from `?James/` to `James/` | Confirm the suggestion |
| Move face from `James/` to `Mom/` | Re-identify (correct a confirmed mistake) |
| Move face from `James/` to `?001/` | Reject identity; clear it back to unidentified |
| Move face to checkout's `pending-delete/` | Remove that face region from the source file (false positive) |

Bulk relabeling is just folder rename or many moves — no special command needed.

### Deferred

- `pix relabel <old> <new>` CLI shortcut — same effect as renaming a folder in checkout, without materializing a workspace.
- Active learning (surfacing borderline matches even below the suggestion threshold).
- Quality-filtering of face crops (blurry, partial) before clustering.
