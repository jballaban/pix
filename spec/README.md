# pix spec — overview

This folder holds the design spec for `pix`. Each file covers one scope;
cross-references between files are explicit. Code is the source of truth: where a
spec and the code disagree, the code wins.

## What pix is

A personal media library hosted on a Synology NAS: an immutable archive of
original photos and videos, with every human decision kept in an `.xmp` sidecar
beside the file, and a web app to browse and curate it. The architecture and its
rationale are in [nas-app.md](nas-app.md).

Until 2026-10-07 this repo also held an earlier design — a desktop CLI that
normalized a library in place (`migrate`, `hash`, `dedupe`, `organize`, `sync`,
`export`, tag checkout). That code and its specs were removed, and the NAS
architecture's `pix2` command became `pix`. Commit `e1f3853` has the old
code and specs.

## Commands

The desktop CLI (`pix`) moves files in and makes what the app shows; the app,
running in a container on the NAS, does everything else.

| Command | What it does |
|---|---|
| `pix import device` / `pix import folder <source> --name <n>` | Pull from a phone or a folder into local staging ([import.md](import.md), [nas-app.md §9](nas-app.md#9-ingest--the-desktop-cli)) |
| `pix upload` | Staging → master on the NAS, over SMB |
| `pix process` | Master → thumbnails, previews, renders, filmstrips, stills, and the index |
| `pix index` | Rebuild the app's index from the meta tier and the sidecars |
| `pix passwd` | Hash a password for an account |
| `pix where` | Show where the library lives |

## Specs

- [nas-app.md](nas-app.md) — the architecture: sacred originals, sidecars, the
  derived tiers, the app, ingest, identity
- [clips.md](clips.md) — clips: video splitting and stills from video
- [import.md](import.md) — the device import loop `pix import` is built on
- [roadmap.md](roadmap.md) — designed-but-unbuilt features
- [perf-backlog.md](perf-backlog.md) — performance ideas against built code
