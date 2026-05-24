# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repo.

## Status

Pre-code. The design lives in `spec/`. Treat each `spec/*.md` as authoritative until source files arrive.

## How to work here

- **Design-first.** When the design changes, edit `spec/*.md` first. The spec is the source of truth. Don't generate code unless explicitly asked.
- **One scope per file.** Keep this file short. Spec content goes in `spec/`; don't grow CLAUDE.md.
- **Propose the tradeoff, then update.** For non-trivial design changes, surface the tradeoff to the user, get sign-off, then edit the relevant spec file.
- **Read the relevant spec before working in an area.** Cross-references between spec files are explicit; follow them.

## Spec map

- [`spec/README.md`](spec/README.md) — overview, cross-cutting invariants, ops table, open decisions
- [`spec/library.md`](spec/library.md) — library root, file layout, canonical filenames, original source path
- [`spec/tags.md`](spec/tags.md) — tag model, metadata mapping, template grammar
- [`spec/migrate.md`](spec/migrate.md) — migrate (designed in full)
- [`spec/hash.md`](spec/hash.md) — `pix hash` command (designed; not yet implemented)
- [`spec/dedupe.md`](spec/dedupe.md) — dedupe (sketched)
- [`spec/tag-editing.md`](spec/tag-editing.md) — checkout/commit, face workflow
- [`spec/organize.md`](spec/organize.md) — organize (sketched)
- [`spec/export.md`](spec/export.md) — export (sketched)
- [`spec/implementation.md`](spec/implementation.md) — language, libs, env, perf notes, sync-client interaction
- [`spec/backlog.md`](spec/backlog.md) — spec-vs-code gap (features designed but not yet built)
- [`spec/perf-backlog.md`](spec/perf-backlog.md) — performance ideas against already-implemented code

## Environment

- Platform: Windows 11, PowerShell. Bash available via the Bash tool for POSIX scripts.
- Working directory: `F:\code\pix`.
- Not a git repository yet.
