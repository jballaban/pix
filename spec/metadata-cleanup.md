# Metadata — what leaves, and what the old pix left behind

**Status: parked, undecided.** Discussed 2026-10-07 and stopped at the open
questions in [§5](#5-open-questions) on purpose; nothing here is built or
agreed. Pick up from [§6](#6-to-pick-this-up). Extends
[nas-app.md](nas-app.md), and would amend its §1 (sacred originals), §14
(seeding) and §15 (round trips) if adopted.

Two problems surfaced together, and they are related but separable:

- **A. What a download hands out.** Everything leaving the app should carry
  no identifying metadata — no location, no people, no tags, no access —
  except an opaque pix reference so a returning copy can still be
  recognised.
- **B. What the old pix wrote into the originals.** Seeded files carry the
  old CLI's custom tags, names and paths. The old library is **not fully
  seeded yet**, so more of these files are still to come.

---

## 1. What leaks today (measured 2026-10-07)

A download (`/download`, `/download.zip`) hands over the stored bytes
unchanged — the master, its render, or a clip's cut. **Video playback
(`/media`) sends the same whole file** to the browser, so a viewer can save it
from there too; downloads are not the only way out.

| File | What it carries |
|---|---|
| Seeded master JPG | full GPS (position, altitude, heading, speed), make/model/software, Apple `PhotoIdentifier`, embedded thumbnail, face regions, old `pix:` tags (`EventOverride: Banff Skiing - Ballabans`, `OriginalPath: G:\pix\raw\tmp\mtp\james\…`), a `UserComment` with that path |
| Seeded master MP4 | GPS, make/model, `pix:ImportId` (phone serial), `pix:OriginalPath` |
| GoPro master | camera and lens serial numbers, firmware; a GPMF telemetry track (on GoPros this normally carries a GPS track) stored as data samples inside `mdat` |
| **Render** (the default download) | GPS location, and a comment with the old path, a person's name and a USB device id — `process` copied the source's metadata across |
| Clip cut | `pix:SourceFile` (folder/name), `pix:ClipId`, `pix:ClipRange`; the GoPro telemetry track is already dropped |
| Thumb / large / preview | clean |

**Names leak too.** Seeded masters are named for their old path —
`G_pix_2026_Banff Skiing - Ballabans_2026-03-20_110149.jpg` — and a download
uses that name. A zip puts each file inside a folder named after its master
folder.

### Embedded legacy tags across master

From every meta record (16,195 masters; all in the three `init_*` folders, i.e.
the 2026 year — about 106 GB of a library of ~62k files / ~2.5 TB):

| Embedded | Files | What it is |
|---|---|---|
| `pix:DateAuto`, `pix:OriginalPath` | 16,005 | old derived date; the pre-seed path |
| `pix:EventAuto` / `pix:EventOverride` | 12,578 / 5,881 | the event, from the old folder name / set by hand |
| `pix:MergeEvent` / `pix:MergeDate` | 2,474 / 430 | old dedupe bookkeeping |
| `pix:ImportId` | 4,613 | phone serial + object id, from the old importer |
| `pix:Rating` | 906 | old 1–5 rating (592 are 1s, 217 are 2s) |
| `NORMALIZE_*` comment | 3,077 | an older tool's paths, names, USB ids |
| GPS / face regions / serial numbers | 8,478 / 4,041 / 190 | the camera's own (the face regions are Apple's — old pix never detected faces) |

No file carries an embedded `pix:DateOverride`.

## 2. What depends on the embedded tags

1. **Events.** The index reads a file's event through to its embedded tags
   when no sidecar names one (`index.inherited_event`) — that is how seeding
   skipped writing sidecars. **6,236 files have their event only there.**
   Stripping before moving it into a sidecar would lose them.
2. **The seeding safety check.** [nas-app.md §14](nas-app.md#14-seeding-the-existing-library)
   step 1 verifies `raw/` is covered by `OriginalPath` lineage before `raw/`
   (+3.4 TB) is archived and deleted. It is not known whether that has run.
3. **The details panel** shows `OriginalPath` and the old event fields
   (`webapp/api.py`).
4. **Not** device import: its skip set comes from the ledgers, not
   `pix:ImportId`.

**Anything kept has to go into the master tier** — a sidecar or a ledger —
never the meta tier alone. Meta is regenerated from master, so a value that
lives only there is gone the next time `process` runs after a strip.

## 3. Thinking so far — A, delivery

**The content hash is already the reference id.** It covers the coded image
only (JPEG tables and scan; the MP4's `mdat` bytes), so stripping metadata
does not change it, and a stripped download that comes home still matches its
master or its render. An embedded id is a bonus, not the mechanism: one opaque
`pix:SourceId` (the master's content hash) would name the source of a render,
cut or still, whose own hashes differ, and would tell an untouched return from
an edited one. It would replace `pix:SourceFile`, which is a path. Messaging
apps strip it anyway.

**What has to stay, or the file breaks:** orientation (or iPhone photos turn
sideways); the ICC profile (or colours shift); a date (or phones file it under
the day it was downloaded). Video rotation is in the track header, not the
metadata, and survives. The HDR gain map — extra images after the JPEG's
end — would be lost; HDR photos would then show as standard range.

**The date is the one decision that would go out.** Writing the effective
date (override applied) into the copy is the only "write on the fly" this
needs, and it is cheap below.

**Candidate mechanism — redaction maps, applied while streaming:**

- `process`, which already reads every file, records in the meta record a
  short list of byte ranges to overwrite, each with replacement bytes **of
  the same length**: MP4 metadata boxes renamed `free` and zeroed; JPEG EXIF
  rewritten to a minimal one (orientation, date) padded to its old length;
  XMP blanked; data after the JPEG's end zeroed; the date fields' positions
  noted for filling in.
- The app applies the list as it streams. The output is the **same length**,
  so Range requests, video seeking, zips and `/media` all keep working, and
  the NAS does no parsing, decoding or encoding.
- Renders, cuts and stills are made clean to begin with, carrying only the
  date and `pix:SourceId`.

Rejected: a sanitised copy of every file (doubles storage); `exiftool` or
`ffmpeg` per request on the NAS (a temporary copy of every multi-GB video,
and the image has no exiftool).

Consequences noted:
- a file with no map yet cannot be served to a viewer;
- one `process` pass gives every existing master and render a map;
- zeroing GoPro telemetry samples changes `mdat`, so its hash no longer
  matches — `process` would record a *delivered hash* beside `render_hash`;
- HEIC/MOV from future phone imports are ISO base-media too, and HEIC keeps
  orientation outside EXIF;
- [§7 distributions](nas-app.md#7-distributions) were to bake events and
  people *into* copies for household devices — that needs its own rule.

## 4. Thinking so far — B, the originals

[Rule #1](nas-app.md#1-why-sacred-originals-and-not-self-describing-files)
says master bytes are never modified. The counter-argument: seeded files are
not device originals — the old pix already converted them and wrote these
tags — so removing pix's own writes reverts pix, not the camera. The
metadata-blind content hash can prove, per file, that the image is untouched.

Sorting what is there:

- **Decisions → sidecar:** the event (override, else auto). Ratings: a tag,
  or dropped — the current model has no rating.
- **Provenance → ledger, admin-only:** `OriginalPath`, if kept beyond the
  lineage check.
- **Drop:** `DateAuto`, `MergeEvent`, `MergeDate`, `ImportId`, `NORMALIZE_*`,
  `XMPToolkit`.
- **Camera data** (GPS, make/model, serials, face regions): the genuine
  original — useful for maps and faces later — and A keeps it from leaving.
  Leaning keep.

Options:

- **Leave master alone.** Move decisions to sidecars, stop reading embedded
  tags, rely on A for everything outbound. No risk, rule #1 absolute; but
  names and paths stay in the archive — visible in admin *original*
  downloads, backups, and over SMB.
- **Strip on the way in, for the years still to seed.** `upload` already
  streams each staged file to a temp path on the NAS, hashes, verifies and
  renames it. For a legacy file, ExifTool would write the cleaned copy to that
  temp path in the same single pass. Never edit staging: folder import
  hardlinks from `G:\pix`, so an edit there would land in the old library.
  - verification adds *content hash of the clean copy = content hash of the
    source*; the blake3 check still covers what was written;
  - the sidecar (event, rating?) is written in the same step;
  - the ledger line keeps the **source** size — it is the folder-import skip
    key (`ledger.committed_folder_keys`), so recording the cleaned size would
    re-import the year as duplicates — and gains `original_path`.
- **A one-off rewrite of the 16k already seeded**, the same cleaner,
  hash-verified per file (temp, verify, swap). About 106 GB. Btrfs snapshots
  keep the old copies until they age out; Hyper Backup treats every file as
  changed; size and mtime change, so meta goes stale — confirm `process` then
  re-probes and does not regenerate every thumbnail.

Leaning: one shared *legacy cleaner* → seed the rest through it → run it over
the 16k → drop the index's read-through → amend rule #1 to exactly *pix's own
legacy tags are removed from seeded files, once, on the way in, with the
content hash proving the image is unchanged*. Doing this **before seeding any
more years** keeps the problem from growing.

Master **filenames** are a separate question. Renaming is allowed by rule #1,
but names key sidecars, renders, clips, history and the index; naming
downloads by date fixes the leak far more cheaply.

## 5. Open questions

1. Has the §14 step 1 `raw/` lineage check run? If not, `OriginalPath` must
   survive until it has.
2. `OriginalPath` afterwards: keep it in the ledger as hidden provenance, or
   drop it?
3. The 906 old ratings: a tag such as `rating:2`, or drop them?
4. Camera data in master (GPS, serials, Apple's face regions): keep it and
   redact on the way out, or strip it from the archive too?
5. Originals: leave master alone, or strip on the way in plus a one-off
   rewrite of the 16k? Amend rule #1?
6. Downloads by an administrator: should *original* still be the exact bytes,
   as a backup path, with everyone else given the redacted copy?
7. Write the effective date into downloads? Drop the time-zone offset, which
   says roughly where a photo was taken?
8. Name downloads by effective date (`2026-03-20_110149.jpg`) and flatten zip
   folders?
9. GoPro telemetry: zero it in downloads, at the cost of a delivered-hash
   column?
10. Distributions: same rule as downloads, or do household copies keep events
    and people baked in?

## 6. To pick this up

- [ ] Answer §5 — at least 1, 4, 5 and 6, which decide the shape.
- [ ] Check the numbers are still current (the survey reads every meta record;
      more years may have been seeded since).
- [ ] Write the decision into nas-app.md §1, §14 and §15, and turn this file
      into a design.
- [ ] Hold further seeding until the cleaner exists, or accept a larger
      retro rewrite.
