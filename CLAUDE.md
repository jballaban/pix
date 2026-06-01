# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repo.

## Status

Active codebase. The implementation lives in `src/pix/`; tests in `tests/`. **Code is the source of truth.** The `spec/*.md` files document design intent and may lag behind the code — when they disagree, the code wins. See [`spec/roadmap.md`](spec/roadmap.md) for designed-but-unbuilt work.

## How to work here

- **Read the relevant spec before working in an area.** Specs capture the design rationale; cross-references between spec files are explicit, so follow them. Treat them as reference, not as a gate — they may be out of date.
- **One scope per file.** Keep this file short.

## Dev workflow

- **Run tests:** `uv run pytest`. Type-check with `uv run pyright` (strict mode; see `pyproject.toml`).
- **Version bump per commit:** bump the `__version__` patch in `src/pix/__init__.py` on every commit that changes runtime behavior. The CLI prints this as the first line of every run, so dev and tester stay aligned.
- **Reinstall after commit:** run `uv tool install --reinstall --editable F:\code\pix` so `F:\bin\pix.exe` reflects the latest code.

## Spec map

- [`spec/README.md`](spec/README.md) — overview, cross-cutting invariants, ops table, open decisions
- [`spec/library.md`](spec/library.md) — library root, file layout, canonical filenames, original source path
- [`spec/tags.md`](spec/tags.md) — tag model, metadata mapping, template grammar
- [`spec/migrate.md`](spec/migrate.md) — migrate (implemented)
- [`spec/hash.md`](spec/hash.md) — `pix hash` command (implemented)
- [`spec/dedupe.md`](spec/dedupe.md) — dedupe (implemented)
- [`spec/organize.md`](spec/organize.md) — organize (implemented)
- [`spec/sync.md`](spec/sync.md) — sync: migrate→hash→dedupe→organize wrapper + shared `--no-prompt` (implemented)
- [`spec/tag-editing.md`](spec/tag-editing.md) — checkout/commit (assign implemented; removal/blank/face planned)
- [`spec/export.md`](spec/export.md) — export (sketched, not implemented)
- [`spec/implementation.md`](spec/implementation.md) — language, libs, env, perf notes, sync-client interaction
- [`spec/roadmap.md`](spec/roadmap.md) — designed-but-unbuilt features
- [`spec/perf-backlog.md`](spec/perf-backlog.md) — performance ideas against already-implemented code

## Environment

- Platform: Windows 11, PowerShell. Bash available via the Bash tool for POSIX scripts.
- Working directory: `F:\code\pix`.
- Git repository on branch `main`.
