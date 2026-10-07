# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repo.

## Status

Active codebase. The implementation lives in `src/pix/`; tests in `tests/`. **Code is the source of truth.** The `spec/*.md` files document design intent and may lag behind the code — when they disagree, the code wins. See [`spec/roadmap.md`](spec/roadmap.md) for designed-but-unbuilt work.

## How to work here

- **Read the relevant spec before working in an area.** Specs capture the design rationale; cross-references between spec files are explicit, so follow them. Treat them as reference, not as a gate — they may be out of date.
- **One scope per file.** Keep this file short.

## Dev workflow

- **Web app layout:** `pix.nas.web` only assembles the app; the code lives in `src/pix/nas/webapp/`, one module per concern (`app`, `shell`, `grid`, `pages`, `api`, `writes`, `clipping`, …). CSS, JS and HTML are files in `src/pix/nas/static/` (the grid script is `static/js/browse/NN-*.js`, joined in order), inlined into each page; edit them there, not as Python strings. A change needs a server restart. Roots the web app touches are read as `webroots.X` at call time.
- **Run tests:** `uv run pytest`. Type-check with `uv run pyright` (strict mode; see `pyproject.toml`).
- **Version bump per commit:** bump the `__version__` patch in `src/pix/__init__.py` on every commit that changes runtime behavior. The CLI prints this as the first line of every run, so dev and tester stay aligned.
- **Reinstall after commit:** run `uv tool install --reinstall --editable .` (from the repo root) so the installed `pix` reflects the latest code.

## Spec map

- [`spec/README.md`](spec/README.md) — overview and command table
- [`spec/nas-app.md`](spec/nas-app.md) — the architecture: sacred originals, sidecars, derived tiers, the app, ingest, identity
- [`spec/clips.md`](spec/clips.md) — clips: video splitting + stills from video, `archived` audience (built; distributions pending)
- [`spec/import.md`](spec/import.md) — the device import loop `pix import` runs on (written for the removed CLI; read with nas-app §9)
- [`spec/metadata-cleanup.md`](spec/metadata-cleanup.md) — clean downloads: nothing identifying leaves, cleaned on the fly (photos and videos built)
- [`spec/roadmap.md`](spec/roadmap.md) — designed-but-unbuilt features
- [`spec/perf-backlog.md`](spec/perf-backlog.md) — performance ideas against already-implemented code

The old CLI-pipeline (`migrate`/`organize`/`dedupe`/`export`…) and its specs were removed 2026-10-07; commit `e1f3853` has them.

## Environment

- Primarily developed on Windows + PowerShell (so commands/paths in docs lean that way); the tool isn't Windows-only by design. Machine-specific details (paths, drives) are supplied per session rather than written here.
