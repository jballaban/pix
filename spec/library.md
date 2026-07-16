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

`pix init [<path>]` creates `<path>` (if it doesn't exist) and `<path>\.pix\`, defaulting to CWD, and seeds it with a `pix.yaml` settings file. After that, the folder is a library root and pix commands invoked from anywhere under it resolve here. Nesting is blocked: running `pix init` inside an existing library root fails.

### Multiple libraries

Each library is independent. Two libraries on the same machine (e.g., `F:\personal\` and `F:\work\`) each have their own `.pix\` and share no state. Cross-library moves are an explicit, deferred operation.

## No versioning — structural recovery instead

The library is **version-less.** There is no `schema_version`, no `state.yaml`, no `pix upgrade` command, and no schema gate on commands. Any pix build operates on any library directly. This is deliberate: in pix's entire history every "schema bump" was an extension-default change, and the format policy no longer lives in the library at all (it's a build constant — see [migrate.md → Extension policy](migrate.md#extension-policy)). The only structural `.pix/` change to date — folding machine-local state into `.pix/local/` (see File layout) — is handled structurally too: an idempotent, self-healing relocation with no version gate. So the versioning machinery only ever served a job that no longer exists.

Format drift in persisted `.pix/` data is handled **structurally**, by what each kind of data *is*, not by a version number:

| Data | Durability | If a build can't read it |
|---|---|---|
| `local/` (`cache.db`, `lock`, `staging/`, `checkout/`, `faces/`) | regenerable / machine-local | **regenerate / recreate** — an unreadable or stale cache entry is just a miss (recompute from the file); the lock and workspaces are transient |
| `errors/`, `stash/` | only-copy | **restore** the file to its origin and reprocess (see below) |
| `runs/` | historical / rollback | **leave** — old run folders are frozen records; new code doesn't reinterpret them |

### Persisted-data recovery invariant

This is the standing contract that makes version-less safe, and the rule to follow whenever persisted formats change:

- **The only irreplaceable field is the origin path.** For an only-copy folder (`errors/`, `stash/`), the path back to the library is the one thing that can't be reconstructed. Keep it a plain string under a stable key, **forever**. (`errors/` additionally encodes it in the file's mirrored *location*, so its sidecar is purely advisory.) Everything else in a sidecar — error message, timestamps, the pix version stamp — is diagnostic or an optimization, and losing it costs nothing.
- **Every other persisted field is additive or reconstructible.** New fields are added with a default (old data missing them still reads) or are derivable, so a reader of any age copes. Never rename or repurpose the origin field; never make recovery depend on the folder's *layout*.
- **Never delete an unreadable only-copy.** If a build genuinely can't make sense of an `errors/`/`stash/` entry, it preserves it and surfaces it to the user — the universal fallback is always "restore the file to the library and reprocess," which needs only the origin path.

Follow that and a breaking migration is never needed: regenerable data rebuilds, only-copy data restores from its frozen path, and additive readers absorb the rest. (If a genuinely breaking change ever did loom, a single "refuse if newer" integer could be reintroduced as a tripwire — but it earns its place only that day.)

Per-file `pix:*` XMP fields are likewise absorbed lazily by migrate's re-derivation (`DateAuto`, `EventAuto`, etc. re-derive every run); `OriginalPath` is the write-once exception and its XMP key is stable.

### Source vs library root

`pix migrate <source>` normalizes the files inside `<source>` **in place** — converting formats, applying the canonical filename, writing `_auto` tag values into metadata. Files never leave `<source>` during migrate; only `organize` rearranges folders. The library identified by the resolved root provides the `.pix\` directory where the run record and captured originals live. The source folder doesn't need to live inside the library root — anywhere on the same volume works (same-volume rename / hard-link constraint still applies).

## File layout

- **Library** — real, human-readable, **media only**. Whatever shape the active organize template produces (e.g. `<library-root>/2023/2023-08/2023-08-15_143205.jpg`). No tool scaffolding lives inside the library — `organize` rewrites the tree freely.
- **`<library-root>/.pix/`** — all scaffolding:
  - `pix.yaml` — per-library settings (optional `runs_dir`, `organize.template`). Hand-editable; pix preserves the keys it knows and drops unknown keys/comments on rewrite. The **format policy is not here** — it's a build constant (see [migrate.md → Extension policy](migrate.md#extension-policy)). (Was `config.yaml`.)
  - `runs/<run-id>/` — per-run state. Contains `plan.txt` plus `L<NNN>_<original-filename>` captures for files destroyed during the run (CONVERT/DELETE originals) and `L<NNN>_<original-filename>.xmp` sidecars for TAG-only mutations. Each run folder is independently rollback-able. Run folders accumulate across runs until the user manually deletes them. Run-id is just a folder name on disk; no code reads it back, no marker carries it.
    - **Relocatable (`runs_dir`).** The optional `runs_dir` config key repoints migrate's run folders to another path — typically another **volume** — so a full library drive can offload the conserved-original captures (the bulk of run-folder size, especially during a re-encode pass). When `runs_dir` is on a different volume, captures move cross-volume via copy+delete (`timeout.safe_move`) instead of an atomic same-volume rename: slower, and the capture step is no longer instant, but still crash-safe (the source survives until the copy completes). Only migrate's run folders relocate; **staging, markers, and the media tree stay on the library volume** (their renames must be same-volume/atomic), and `checkout` workspaces stay local too (they hard-link media, which can't cross volumes). The key is optional and doesn't bump `SCHEMA_VERSION`.
  - `stash/` — holding area for files the user wants set aside but not lost (RAW formats, proprietary 360 source files). Flat folder with `<filename>` + `<filename>.stashinfo` (YAML sidecar) per unique-content entry. Created lazily on first stash. See [migrate.md → Stash action](migrate.md#stash-action).
  - `errors/` — quarantine for files that failed processing; each keeps its name and its *location* mirrors the source path, with a `.errorinfo` sidecar. Only-copy (see recovery invariant above).
  - `local/` — **machine-local, never-synced state**, grouped under one folder so a file-sync client can exclude it in a single rule (see [implementation.md → Sync client interaction](implementation.md#sync-client-interaction)). Everything here is regenerable or transient:
    - `cache.db` (+ `-wal`/`-shm`) — the single-file cache store: hash index, filtered EXIF metadata, and video fingerprints, one row per file. Regenerable — recompute on a miss.
    - `lock` — the single-active-op library lock (machine-local PID payload; see [README → Concurrency](README.md)).
    - `staging/` — shared scratch for off-library conversions during the current migrate apply. Wiped at the start of every migrate.
    - `checkout/` — the active tag-editing workspace (hard links to library files).
    - `faces/` — face crop cache + embeddings DB.

    `pix init` creates `local/` up front (so a sync client can exclude it before the first run); libraries predating it fold their top-level `cache.db`/`lock`/`staging`/`checkout`/`faces` into `local/` automatically on the next command.
- **Same-volume constraint** — `.pix/` and source folders being migrated are assumed on the same volume so atomic rename and hard links are available.

## Canonical filename

Every file in the library has a canonical name derived purely from its effective `date` tag:

```
{year}-{month}-{day}_{time}.{ext}
```

e.g. `2023-08-15_143205.jpg`. The components (`{year}`, `{month}`, `{day}`, `{time}`) are computed from the single effective `date` value — `pix:DateAuto` patched by `pix:DateOverride` if present (see [tags.md → Effective value computation](tags.md#effective-value-computation)). `pix:DateAuto` is set at `migrate` time; the override is set/cleared via [tag-editing](tag-editing.md) and flows through automatically on the next `migrate`/`organize`. A `DateOverride` of `2022-*-*-*:*:*` patches year=2022 over the auto-derived date, renaming `2023-08-15_143205.jpg` to `2022-08-15_143205.jpg` — the filename always reflects the *effective* date.

The original source path is preserved in metadata (see *Original source path* below) so provenance — including the device-assigned filename — isn't lost.

**Exception — name-preserving keep.** Insta360 `.insv`/`.insp` are kept under their **original camera filename** and never renamed to the canonical form. A recording's two lens files share a capture timestamp, so the canonical name would collide them and the collision tiebreaker would scramble lens identity, breaking the `VID_<date>_<time>_<lens>_<seq>` pairing Insta360 Studio depends on. These files are still dated/tagged and organized into folders by effective date — only the filename is left alone. See [migrate.md → Name-preserving keep](migrate.md#name-preserving-keep-insta360-insv--insp).

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
