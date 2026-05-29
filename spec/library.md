# Library

This spec covers everything about *where things live* in a pix library: how the library root is resolved, what lives inside `.pix/`, how files are named, and the provenance pix records for each file.

Related:
- [tags.md](tags.md) for the metadata that drives effective filename derivation.
- [migrate.md](migrate.md) for how files acquire their canonical name and metadata.

## Library root

Every `pix` command operates against a **library root** — a folder containing a `.pix\` directory where all tool state lives (runs, captures, staging, face cache, config, checkouts).

### Resolution

When any `pix <op>` is invoked, the library root is resolved by:

1. Walk up from the command's path argument (migrate's `<folder>`, organize's `<path>`) looking for a `.pix\` directory. First match wins.
2. `PIX_ROOT` env var, if set — must point at a directory containing `.pix\`.
3. Walk up from CWD as a last-resort interactive fallback.
4. If nothing found → fail with: `No pix library root found. Pass a path inside a library, set PIX_ROOT, or run 'pix init <path>' to establish one.`

There is no `--root` flag. The path argument every command takes is the explicit resolution input; the env var covers scripted contexts; CWD walk-up covers the common "I'm sitting inside my library" case.

### Establishing a root

`pix init [<path>]` creates `<path>` (if it doesn't exist) and `<path>\.pix\`, defaulting to CWD, and seeds it with a default `config.yaml`. After that, the folder is a library root and pix commands invoked from anywhere under it resolve here. Nesting is blocked: running `pix init` inside an existing library root fails.

### Multiple libraries

Each library is independent. Two libraries on the same machine (e.g., `F:\personal\` and `F:\work\`) each have their own `.pix\` and share no state. Cross-library moves are an explicit, deferred operation.

## Schema versioning

`.pix/state.yaml` carries a single integer:

```yaml
schema_version: 1
```

The constant `SCHEMA_VERSION` in `src/pix/schema.py` tracks the version this build of pix understands. Every pix command (except `init` and `upgrade`) compares the two after resolving the root:

| On-disk vs. running | Action |
|---|---|
| `state.yaml` missing | Bootstrap — write a fresh `state.yaml` at `SCHEMA_VERSION`. Nothing else touched. (Lets pre-versioning libraries adopt the system without losing their custom config; no destructive action.) |
| Equal | No-op. |
| On-disk lower | **Refuse** with a clear message pointing the user at `pix upgrade <root>`. The command exits non-zero without doing anything. |
| On-disk higher | Refuse. A newer pix touched this library; we don't know what we'd break. |

> **Terminology note:** in this spec, "schema upgrade" refers to internal `.pix/` state transitions. It's distinct from `pix migrate`, which is the file-level normalization command. The English word "migration" colloquially means either; the codebase deliberately uses **upgrade** for one and **migrate** for the other.

**`pix upgrade <path>` is the only command that mutates the library state to match a new schema.** It's run explicitly by the user when they see the upgrade-required message and they're ready. The reasoning: the user may have customizations in `config.yaml` (extension policies, organize templates) that they don't want silently changed on what they thought was an unrelated command. Forcing an explicit gesture means the change is deliberate.

### What `pix upgrade` actually does

1. **Resolves the library root** with the schema check disabled (otherwise it'd refuse to start on the very mismatch it's there to fix).
2. **Loads the user's existing config** into memory.
3. **Archives everything** in `.pix/` (except `archive/` itself) into `.pix/archive/v<old>/`. Nothing is ever lost — the prior config, runs, stash entries, etc. all live in the archive folder. **Crucially, this sweeps `stash/` and `errors/` too**, which hold the *only* copies of user files (intentionally set-aside originals; files that failed conversion). While they sit in the archive, pix doesn't see them: stashed files won't be listed and errored files won't be auto-retried by migrate (the restore scan reads `.pix/errors/`, never `.pix/archive/`). So when the archived snapshot contains a non-empty `stash/` or `errors/`, the command prints a prominent warning — with the per-folder file counts and archive paths — telling the user these are irreplaceable and to move anything they want back out of the archive before deleting it. (`runs/`, `cache/`, `staging/`, and `faces/` are regenerable, so they archive silently.)
4. **Walks `UPGRADES[old+1..current]`** — a per-version table of additive / removal changes — and applies them to the loaded config:
   - **Addition** (a new entry in defaults): if the user doesn't have the key, add it with the new default value. If the user has the key with the *same* value, no-op. If the user has the key with a *different* value, record a conflict.
   - **Removal** (a key dropped from defaults): if the user has the key, remove it. The archive preserves it.
5. **Writes the merged config** back to `<root>/.pix/config.yaml`:
   - If there are no conflicts: clean YAML, ready to use.
   - If there are conflicts: each conflicted entry is replaced with a git-style marker block (`<<<<<<< current` / `=======` / `>>>>>>> vN default`). The resulting file is **invalid YAML**, which is intentional — pix refuses to operate on a config with markers until the user picks a side and removes them. Config-load detects the markers explicitly and points the user at the right action.
6. **Updates `state.yaml`** to the new `SCHEMA_VERSION`.
7. **Reports** what was added, what was removed, and which entries conflicted.

The archive is always the safety net: the user can always look at `.pix/archive/v<old>/` to see exactly what they had before, including comments and original ordering, and copy customizations back manually.

Bump `SCHEMA_VERSION` only when something **material** in `.pix/` changes — a new mandatory config field, a renamed subfolder, a removed file format. Most pix releases don't touch persisted state and should not bump it. Past bumps are recorded in `src/pix/schema.py`'s module docstring so we have a written history of what each version meant.

Per-file `pix:*` XMP fields are **out of scope** for this system. Schema changes there are absorbed lazily by migrate's existing re-derivation (`DateAuto`, `EventAuto`, etc. re-derive every run). Run folders are also out of scope — they're frozen historical records, and rollback (when built) will be responsible for handling whatever layout each one was written in.

### Source vs library root

`pix migrate <source>` normalizes the files inside `<source>` **in place** — converting formats, applying the canonical filename, writing `_auto` tag values into metadata. Files never leave `<source>` during migrate; only `organize` rearranges folders. The library identified by the resolved root provides the `.pix\` directory where the run record and captured originals live. The source folder doesn't need to live inside the library root — anywhere on the same volume works (same-volume rename / hard-link constraint still applies).

## File layout

- **Library** — real, human-readable, **media only**. Whatever shape the active organize template produces (e.g. `<library-root>/2023/2023-08/2023-08-15_143205.jpg`). No tool scaffolding lives inside the library — `organize` rewrites the tree freely.
- **`<library-root>/.pix/`** — all scaffolding:
  - `config.yaml` — extension policy + other settings.
  - `state.yaml` — library-state schema version (see [Schema versioning](#schema-versioning)).
  - `runs/<run-id>/` — per-run state. Contains `plan.txt` plus `L<NNN>_<original-filename>` captures for files destroyed during the run (CONVERT/DELETE originals) and `L<NNN>_<original-filename>.xmp` sidecars for TAG-only mutations. Each run folder is independently rollback-able. Run folders accumulate across runs until the user manually deletes them. Run-id is just a folder name on disk; no code reads it back, no marker carries it.
  - `staging/` — shared scratch space for off-library conversions during the current migrate apply. Wiped at the start of every migrate.
  - `stash/` — holding area for files the user wants set aside but not lost (RAW formats, proprietary 360 source files). Flat folder with `<filename>` + `<filename>.stashinfo` (YAML sidecar) per unique-content entry. Created lazily on first stash. See [migrate.md → Stash action](migrate.md#stash-action).
  - `archive/v<N>/` — created automatically when a schema-version reset happens; holds the prior `.pix/` contents (everything except `archive/` itself). See [Schema versioning](#schema-versioning).
  - `faces/` — face crop cache + embeddings DB.
  - `checkouts/<id>/` — active tag-editing workspaces.
  - Other caches (hash index, EXIF cache, organize-template state).
- **Same-volume constraint** — `.pix/` and source folders being migrated are assumed on the same volume so atomic rename and hard links are available.

## Canonical filename

Every file in the library has a canonical name derived purely from its effective `date` tag:

```
{year}-{month}-{day}_{time}.{ext}
```

e.g. `2023-08-15_143205.jpg`. The components (`{year}`, `{month}`, `{day}`, `{time}`) are computed from the single effective `date` value — `pix:DateAuto` patched by `pix:DateOverride` if present (see [tags.md → Effective value computation](tags.md#effective-value-computation)). `pix:DateAuto` is set at `migrate` time; the override is set/cleared via [tag-editing](tag-editing.md) and flows through automatically on the next `migrate`/`organize`. A `DateOverride` of `2022-*-*-*:*:*` patches year=2022 over the auto-derived date, renaming `2023-08-15_143205.jpg` to `2022-08-15_143205.jpg` — the filename always reflects the *effective* date.

The original source path is preserved in metadata (see *Original source path* below) so provenance — including the device-assigned filename — isn't lost.

The extension portion is canonicalized: `.jpeg`/`.JPG`/`.JPEG` all become `.jpg`; other extensions are lowercased. A file whose on-disk extension doesn't match its canonical form triggers a RENAME on the next migrate even if no other change is needed. Full extension-canonicalization rules live in [migrate.md](migrate.md#extension-canonicalization).

### Collision handling

Multiple files mapping to the same canonical name within their destination folder are disambiguated with a `_NNN` suffix inserted before the extension: `2023-08-15_143205_001.jpg`, `2023-08-15_143205_002.jpg`, ….

- Computed at every `migrate` / `organize` (anywhere names are materialized). **Not persistent** — recomputed from scratch each run.
- Ordering tiebreaker: content hash, ascending. Stable across runs, so re-running is idempotent given stable inputs.
- Only files landing in the *same* destination folder count as collisions; identical canonical names in different folders don't conflict.
- The first file in a colliding group keeps the bare name (no `_000` suffix).

### Idempotence

If a file's current on-disk name already matches the canonical name computed from its effective tags (and any required `_NNN` collision suffix), RENAME is a no-op. Re-running `migrate`/`organize` with no input changes produces no work.

## Original source path (write-once provenance)

The very first time `migrate` processes a file, it stores the file's full source path in metadata as `original_path`. This field is **never overwritten** on subsequent migrate runs — it captures pure historical fact.

### Why

The source path carries inference signal the migrated file otherwise loses: parent folder names (`Trip 2023`, `Birthday — James`), device-assigned filenames (`IMG_0042`), camera-specific directory structures. Once a file is in the library, its location is organize-template-driven and reveals nothing about origin. Future improvements to `_auto` derivation can mine `original_path` for clues.

### Not a tag

`original_path` has no `_auto`/override duality, no folder representation, and no user-edit pathway. It is pure provenance — a fact about where the file came from, recorded once.

### Behavior

- Before applying ops to a file, migrate checks whether `original_path` is populated.
  - **Absent** → write it as part of the file's TAG action (TAG-only, RENAME+TAG, or CONVERT+RENAME+TAG depending on what else is changing). Conservation captures the prior XMP to the sidecar before the write, same as any other TAG. The plan line's details show `original_path null→<source-path>` alongside the other writes.
  - **Present** → leave alone. Even if the file is being re-migrated from a different source.
- A header comment in the plan notes the count for orientation: `# 12 files migrating for the first time will have their source path stored in metadata.`
- Because OriginalPath is a TAG, **the first migrate of any file always includes a TAG component.** Pure RENAME / pure CONVERT lines never appear on first-time files. See [migrate.md → What's in the plan](migrate.md#whats-in-the-plan).
- `_auto` derivation logic may read `original_path` freely.

### Duplicate originals across source folders

Migrate does not deduplicate, so this concern only surfaces during `pix dedupe`. When duplicates are eventually collapsed, source paths are walked in lex order so the choice of which duplicate wins (and thus owns `original_path`) is deterministic across runs. We don't store alternate source paths — if heuristics later need cross-references, the schema can extend.

### Field

Stored as `xmp:pix:OriginalPath` in the custom pix namespace. Full field map: [tags.md](tags.md#field-map).
