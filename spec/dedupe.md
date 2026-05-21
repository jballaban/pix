# Dedupe (sketched)

`pix dedupe` is a separate operation from [migrate](migrate.md). It runs against an already-normalized library — i.e., files have canonical formats and filenames, and content-hashing is meaningful. Splitting it out (rather than bundling into migrate) means migrate stays a pure per-file in-place transform, and dedupe gets to focus on cross-file relational logic. `pix merge` will reuse the same primitive.

Design TBD. Sketch of what it needs:

- **Read pre-computed hashes; don't recompute.** Migrate ensures every migrated file has `pix:ContentHash` (format-aware BLAKE3 — for JPEG, hash everything except APP-marker metadata; for MP4 and other ISO BMFF containers, hash only the concatenated `mdat` box payloads. See [tags.md → System fields](tags.md#system-fields)). Dedupe pulls the hashes from the metadata cache (the same bulk-read pattern as migrate) and groups by hash. No need for `pix dedupe` to scan file content — that work is already done. Files lacking a hash (e.g. content modified externally since last migrate) need to be re-hashed; an unmigrated file shouldn't reach dedupe.
- **Plan / apply pattern.** Same git-commit-style workflow as migrate — summary then `Apply? [Y/e/n]` (defaults to Y; `e` opens the plan in `$EDITOR` for review/edit and re-prompts; `n` aborts). Run folder under `.pix/runs/` with captures of every deleted duplicate (see [library.md](library.md#file-layout)).
- **Keeper selection.** When N files share a content hash, deterministic and explainable. Likely: lex-smallest path wins; user can edit the plan to change keepers per-line.
- **Operates library-wide, not per-folder.** Migrate is folder-scoped; dedupe is library-scoped because content dups span folders.
- **Deferred:** perceptual hashing (tier-2), near-dup detection, burst clustering, quality-winner heuristics. Additive; today's confident-only design doesn't preclude them.
