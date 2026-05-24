# Organize

`pix organize <template>` physically rearranges files in the library to match a folder-shape template. It's the only operation that moves files between folders — [migrate](migrate.md) is in-place per-file; this is structural.

Like migrate, organize is a single blocking, sequential command: plan → edit → confirm → apply. The same console/log split applies (silent during plan-gen except a single rewriting progress line; full per-file detail in `plan.log`).

## Scope

Organize is **library-wide**. `pix organize <template>` resolves the library root (walking up from CWD looking for `.pix/`) and operates on every file under that root, regardless of which subfolder the user invoked the command from. CWD only helps locate the root; it doesn't scope the operation.

There is no subfolder-scoped organize, and no per-path templates — the library has exactly one canonical shape. If the user wants a staging area that doesn't get re-shaped (an `imports/`, an `inbox/`, an `unsorted/`), the correct pattern is to **keep that folder outside the library root** and run migrate against it without ever organizing. Once a file is inside the library, the active template owns its location.

This is a deliberate simplicity trade. Multiple-shapes-per-library is a real use case but the design surface (cross-scope moves, prefix-precedence rules, commit's auto-trigger picking the right template) is enough complexity that we'd rather force the user to maintain separate roots if they really need different shapes.

### CWD constraint

The user must invoke `pix organize` either **from the library root itself** or **from a location outside the library**. Running from a strict subfolder of the library is rejected up front, because the empty-folder cleanup at the end of apply (see [Empty-folder cleanup](#empty-folder-cleanup)) can't remove a folder that's currently a process's working directory — Windows holds a handle on it. We refuse before doing any work rather than fail partway through cleanup.

Error message: `Refusing to organize while CWD is a subfolder of the library. cd to <library-root> (or any directory outside the library) and re-run.` Exit non-zero, no plan written, no state changes.

The library root itself is fine as CWD — organize never removes the root, and `.pix/` keeps it non-empty regardless.

## Workflow

1. **Validate prerequisites.** Walk the library; abort before generating any plan if either condition holds:
   - **Any file lacks `pix:OriginalPath`** — un-migrated. Surface the offending paths; tell the user to run `pix migrate <library-root>` first. (Organize templates read effective tag values; un-migrated files have no values to read.)
   - **Any file lacks a cached content hash** — i.e. `<library>/.pix/cache/.../<filename>.hash` is missing or stale. Surface paths; tell the user to run `pix hash <library-root>` first. The cached hash is the deterministic collision tiebreaker (see [library.md → Collision handling](library.md#collision-handling)); without it, suffix assignment for files sharing a target path isn't deterministic. See [hash.md](hash.md) — same prereq dedupe enforces.
2. **Allocate run folder.** Create `<library-root>\.pix\runs\<run-id>\` (same shape as migrate's run folder).
3. **Parse template.** See [template grammar](#template-grammar) below. Bad templates abort with a clear error before any work.
4. **Build metadata cache.** Bulk-extract pix:* fields for every library file (same one-shot ExifTool pattern as migrate). The cache only needs `pix:OriginalPath`, `pix:DateAuto`, `pix:DateOverride`, `pix:EventAuto`, `pix:EventOverride` — much smaller than migrate's read.
5. **Generate plan.** For each file: compute effective tag values, render the template to get the target folder, compute the **bare** canonical filename from effective date (ignoring any `_NNN` suffix on the current name — see [Filename recomputation](#filename-recomputation) below), assemble the tentative target path. Group all tentative targets by target folder, resolve collisions per group, and assign final names. Compare each final target path to the current path; emit a `MOVE` line if different. Write per-file decisions to `plan.log`. Detect empty-folder candidates.
6. **Confirm, optionally edit, apply** — same `Apply? [Y/e/n]` flow as migrate (defaults to Y; `e` opens the plan for review/edit and re-prompts).
7. **Apply.** Process plan lines sequentially. Each plan line is a single same-volume rename — no markers needed (organize doesn't destroy bytes, just relocates them). After all moves, bottom-up sweep of emptied folders.
8. **Update active template.** On successful apply, write the template string to `.pix/config.yaml` under an `organize.template` key (created if absent). Commit reads it to decide whether to auto-trigger re-organize.

No folder lock — single-user, single-active-run assumption (same as migrate).

## Template grammar

Templates are slash-separated levels; each level is rendered into one folder name. Tokens (`{tag}`) and literal text can mix freely within a level. Examples:

| Template | Rendered for year=2023, month=08, event=Hawaii |
|---|---|
| `{year}` | `2023/2023-08-15_143205.jpg` |
| `{year}/{month}` | `2023/08/2023-08-15_143205.jpg` |
| `{year}/{month}/{event}` | `2023/08/Hawaii/2023-08-15_143205.jpg` |
| `{year}-archive/{event}` | `2023-archive/Hawaii/2023-08-15_143205.jpg` |
| `Photos/{year}/{month}` | `Photos/2023/08/2023-08-15_143205.jpg` |

The full grammar (filter syntax, `null`, negation) is defined in [tags.md → Template grammar](tags.md#template-grammar).

### Tokens valid in organize

`organize` is single-valued only (a file has one physical location), so allowed tokens are:

- `{year}`, `{month}`, `{day}` — derived from effective `date`
- `{date}` — full effective datetime as a string (e.g., `2023-08-15-14:32:05`)
- `{event}` — effective event value

`{time}` is rejected at template parse time — per-second folders are a foot-gun. Multi-valued tokens (`{person}`, `{face}`) are checkout/export only and are rejected here.

### Null and filtered placement

A token resolving to `null` (no value) maps to a literal folder named `null/` **at that level**. The file's path-build stops at the first null on its traversal:

| Template | Tag values | Target path |
|---|---|---|
| `{year}/{event}` | year=2023, event=Hawaii | `2023/Hawaii/` |
| `{year}/{event}` | year=2023, event=null | `2023/null/` |
| `{year}/{event}` | year=null, event=Hawaii | `null/Hawaii/` |
| `{year}/{event}` | year=null, event=null | `null/` |

The same per-level rule applies to `filtered/` (files excluded by an explicit filter like `{event:Hawaii,Party}`).

### Folder-name sanitization

Tag values may contain characters that Windows forbids in folder names (`<`, `>`, `:`, `"`, `/`, `\`, `|`, `?`, `*`). Organize sanitizes the **rendered folder name** only; the underlying tag value is untouched:

- Each illegal character is replaced with `_`.
- Trailing whitespace and trailing `.` are stripped (NTFS quirk).
- Reserved DOS names (`CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, `LPT1`–`LPT9`, case-insensitive) are prefixed with `_` (e.g., `CON` → `_CON`).

Rare collision case: tag values `Birthday|Party` and `Birthday_Party` both render to `Birthday_Party`. They'll collide on canonical filename + `_NNN` suffix resolution like any other collision (see below). If the user cares about that ambiguity, they can edit the tag.

## Filename recomputation

A canonical filename has two parts: the **bare** name derived from the file's effective date (e.g., `2023-08-15_143205.jpg`) and an optional **collision suffix** `_NNN` inserted before the extension when peers in the same folder collide. The bare name is a fact about the file's metadata; the suffix is a fact about the folder's peer set.

**Organize discards any existing `_NNN` suffix on the current filename and recomputes it from scratch based on the new peer set at the target folder.** Concretely:

1. For each file, read effective `date` and compute the bare canonical filename. The current filename is *not* parsed — metadata is the source of truth.
2. Render the template to get the target folder.
3. Tentative target path = target folder + bare canonical filename.
4. Group all tentative targets by target folder.
5. Within each group, apply the [library.md collision rule](library.md#collision-handling): first file (sorted by content hash ascending) keeps the bare name; the rest get `_001`, `_002`, … suffixes.
6. Final target path = target folder + resolved filename.

This means MOVE plan lines can do four things:

| Current → Target | What changed | Example |
|---|---|---|
| Folder changes, filename unchanged | Pure relocation | `imports/2023-08-15_143205.jpg` → `2023/Hawaii/2023-08-15_143205.jpg` |
| Folder changes, filename suffix changes | Relocation + collision-driven rename | `imports/2023-08-15_143205_001.jpg` → `2023/Hawaii/2023-08-15_143205.jpg` (no peer at destination) |
| Folder same, filename suffix changes | In-place rename driven by peer-set change | `2023/Hawaii/2023-08-15_143205_001.jpg` → `2023/Hawaii/2023-08-15_143205.jpg` (peer got deleted between runs) |
| Folder same, filename same | No plan line (idempotent) | — |

All four cases use the single `MOVE` action label. The plan line shows the full target path so the user can see what's happening:

```
L001 | MOVE | imports/2023-08-15_143205_001.jpg | →2023/Hawaii/2023-08-15_143205.jpg
```

Migrate's collision resolution within source folders is **not** a problem for organize — those suffixes simply get dropped (or kept, or reassigned) according to the target-folder peer set. Migrate and organize use the same collision algorithm from library.md; the only thing that changes is the grouping key (source folder vs. target folder).

## Collision handling

Per [library.md → Collision handling](library.md#collision-handling): `_NNN` suffix inserted before the extension, content-hash-ascending tiebreaker, first member keeps the bare name. See [Filename recomputation](#filename-recomputation) above for how this composes with organize's target-folder grouping.

## Empty-folder cleanup

After all moves apply successfully, walk the library bottom-up and remove every empty folder. Recurse upward — if removing `2022/old-trip/` makes `2022/` empty, `2022/` is also removed. `.pix/` is **never** touched, and folders containing only `.pix/` at the root level are preserved.

If a fold-up would remove a folder containing hidden or system files, leave it alone (defensive — we only delete what we know is empty).

The cleanup runs after the plan applies, not as part of any individual plan line. A crash mid-apply leaves some empty folders that the next organize sweeps.

## Atomicity and crash recovery

Each plan line is a single same-volume rename — atomic on its own. No markers needed (organize doesn't decompose into multi-step sequences). Mid-apply crashes leave some files moved and others not; the next `pix organize` re-plans from the current state.

No `data/` captures in the run folder. `plan.txt` records every source → target mapping; `apply.log` records which lines completed. Together they're the rollback record (deferred `pix rollback <run-id>` reads them and reverses each move). Rename is non-destructive; there's nothing else to back up.

## Plan format

Same shape as migrate's plan.txt, with `MOVE` as the only action:

```
# Organize plan: F:\photos
# Generated 2026-05-21 15:00
# Run ID: 2026-05-21_15-00-00
# Template: {year}/{month}/{event}
#
# Delete a line to skip that file this run. Commented "#" lines are info only.
# Format: L<line-id> | ACTION | path | details

L001 | MOVE | imports/2023-08-15_143205.jpg      | →2023/08/Hawaii/2023-08-15_143205.jpg
L002 | MOVE | imports/2023-08-15_143205_001.jpg  | →2023/08/Hawaii/2023-08-15_143205_001.jpg
L003 | MOVE | imports/2023-08-15_143612.jpg      | →2023/08/Hawaii/2023-08-15_143612.jpg
L004 | MOVE | 2023/08/null/2023-09-01_120000.jpg | →2023/09/null/2023-09-01_120000.jpg
L005 | MOVE | 2023/08/Hawaii/2023-08-15_143205_001.jpg | →2023/08/Hawaii/2023-08-15_143205.jpg

# Summary: 5 MOVE
```

Notes on the example:

- L001 + L002 collided in `imports/` (both 14:32:05). L002 kept its `_001` because L001 also moves into the same target folder. Their relative order is preserved.
- L004 is a pure relocation triggered by drift — the file's `pix:DateAuto` was re-derived from `2023-08-...` to `2023-09-...` on the last migrate, and organize now reflects the new month.
- L005 is an in-place suffix change: its prior collision-peer at the destination got deleted between organize runs, so `_001` is no longer needed.

One line per file. No collapsed "folder-level" rows — keeps the editor experience uniform with migrate and lets the user delete individual lines to skip specific files.

Files already at their target path produce no plan line (idempotence). The current filename's `_NNN` suffix is *not* a reason to keep the file in place — see [Filename recomputation](#filename-recomputation).

## Active template persistence

On successful apply, the template string is written to `.pix/config.yaml` under an optional `organize.template` key:

```yaml
extensions:
  jpg: keep
  # ...
organize:
  template: "{year}/{month}/{event}"
```

This is the "active" template. Commit (see [tag-editing.md](tag-editing.md)) reads it to decide whether a tag change should auto-trigger re-organize. The key is **optional** — its addition to config doesn't bump `SCHEMA_VERSION` (see [library.md → Schema versioning](library.md#schema-versioning)). Libraries without it have no active template; commit's auto-organize is a no-op.

A subsequent `pix organize <different-template>` overwrites the key. There's no separate "set template" command.

## Run folder layout

Matches migrate's:

```
runs/<run-id>/
  plan.txt          # editable, then immutable
  plan.log          # per-file plan-gen decisions + phase headers
  apply.log         # per-MOVE Started/Completed transitions
```

No `data/` (nothing destroyed; no captures). No `debug.log` unless we extend the streaming debug log to organize (deferred — organize's per-file decision is trivially "render template, compare path", so the reasoning trace is far less interesting than migrate's).

## Rollback (deferred)

`pix rollback <run-id>` for an organize run reads `plan.txt`, reverses each MOVE (target → source), and reverts the active template in `config.yaml` if it was changed by this run. Sketched; full design deferred.

## Idempotence

A library already in the target template shape produces an empty plan. Re-running with no input changes produces no work.
