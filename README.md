# pix

A command-line tool for wrangling a large, personal photo and video library —
normalizing formats, giving every file a consistent date-based name, organizing
folders by date and event, and de-duplicating — at terabyte scale.

> **Status: pre-1.0, and developed in the open.** pix has been built and used
> against a single multi-terabyte personal library. It's careful by design
> (destructive operations conserve what they replace — see *Safety* below), but
> it is young, opinionated, and primarily exercised on **Windows 11**. Behavior
> and on-disk formats may still change before 1.0. **Keep backups of anything
> you point it at.**

## What it does

You point pix at a folder of media; it normalizes each file **in place**:

- **Canonical names** — every file is renamed from its capture date, e.g.
  `2023-08-15_143205.jpg`, derived from EXIF/QuickTime metadata (falling back to
  the original filename/folder when that's all there is).
- **Format normalization** — a built-in policy decides per extension: keep
  (`.jpg`/`.mp4`/…), convert (`.heic`→`.jpg`, camcorder/`.mov`/`.dng`→ their
  archival form), or drop junk (`Thumbs.db`, …). Video converges on **HEVC** for
  space; raw photos develop to JPG where possible.
- **Organize** — rearrange the whole library into a folder shape you choose,
  e.g. `{year}/{event}/{month}`.
- **Hash + dedupe** — content-hash every file and collapse duplicates, merging
  the metadata you've invested onto the survivor.
- **360 media** — Insta360 `.insv`/`.insp` are kept verbatim (their proprietary
  reframe data is preserved) but still tagged and organized.

Everything is plan-first: each command shows you what it will do and waits for
confirmation before touching anything.

## Requirements

- **Python 3.12+** and [uv](https://docs.astral.sh/uv/)
- **[ExifTool](https://exiftool.org/)** and **[ffmpeg](https://ffmpeg.org/)**
  (`ffmpeg` + `ffprobe`) on your `PATH`
- Developed and tested on **Windows 11**. Other platforms aren't verified yet.
  A library and the folders it migrates are assumed to be on the **same volume**
  (pix relies on fast same-volume renames).

## Install

```sh
git clone https://github.com/jballaban/pix
uv tool install --editable ./pix
```

This puts a `pix` executable on your `PATH`.

## Quick start

```sh
pix init D:\photos              # establish a library root (creates .pix\)
pix sync D:\photos\imports      # migrate → hash → dedupe → organize, in one go
```

`sync` is the non-interactive pipeline. To run the steps yourself (each prompts
before applying):

```sh
pix migrate D:\photos\imports                 # normalize files in place
pix hash D:\photos                            # populate the content-hash cache
pix dedupe D:\photos                          # collapse duplicates
pix organize D:\photos "{year}/{event}/{month}"   # reshape the library
```

`pix meta <file>` shows what date sources and tags pix sees for one file.

## Settings (`.pix/pix.yaml`)

A small, optional, hand-editable file holding library-specific settings:

```yaml
runs_dir: 'E:\pix-runs'                  # put run folders / conserved originals on another volume
organize:
  template: '{year}/{event}/{month}'     # the library's default shape (set by `pix organize`)
```

The **format policy is not configurable per library** — it's built into pix, so
updating the tool updates the policy everywhere.

## Safety

pix never destroys data without conserving it first. Every migrate run writes
the originals it replaces (converted/deleted files) into
`.pix/runs/<run-id>/data/`, so a run is reversible in principle. **Those run
folders accumulate** and are yours to delete when you're confident — `runs_dir`
lets you park them on a roomier drive. Note that some conversions (e.g. HEVC
re-encode) are **lossy**, and HEVC playback needs the Windows HEVC Video
Extension.

## Design docs

The [`spec/`](spec/) directory documents the design and rationale of each
operation — start with [`spec/README.md`](spec/README.md). Code is the source of
truth; the specs explain *why*.

## License

[MIT](LICENSE).
