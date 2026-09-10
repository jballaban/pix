# NAS-hosted app — architecture directive

**Status: designed in discussion, nothing built.** This document supersedes the
architecture the rest of `spec/` describes. It is a directive, not an
implementation plan: it records *what was decided and why*, so the decisions
don't have to be re-derived. Where it conflicts with another spec file, this
file is the newer intent — but the **code** still reflects the old architecture,
and remains the source of truth for what exists today.

Recorded 2026-09-10.

---

## 0. The decision

Today pix is a CLI whose every design choice follows from one assumption: **the
filesystem is the UI**. Canonical filenames, `organize`'s template-shaped
folders, `tag checkout`'s hard-link workspace, tags baked into files so Explorer
can see them, the plan/apply/run-folder ceremony that makes independent CLI
invocations safe against each other.

The new architecture replaces that assumption with an app hosted on the
Synology, and replaces the mutable library with an **immutable archive**.

Two decisions drive everything else:

1. **Originals are sacred.** The master holds files exactly as they came off the
   device. Bytes are never modified — no format conversion, no tag writes, no
   re-encoding. Renaming is permitted (it does not touch bytes); everything else
   is not.
2. **The app is optional.** Every fact about a file lives in a file, in a
   standard format, readable without pix. The app is a lens over the archive,
   never its owner.

## 1. Why sacred originals (and not self-describing files)

Two coherent architectures were considered:

| | **A — self-describing files** | **B — sacred originals** |
|---|---|---|
| Master | normalized, renamed, tags written in | pristine, untouched |
| Metadata home | inside the media file | beside it |
| Cost | originals are destroyed by normalization | metadata needs a durable home |

**B was chosen because B contains A, and A destroys B.** An archive of pristine
originals plus metadata can generate A's library on demand at any time — that is
exactly what publishing a distribution does. The reverse does not exist: once a
HEIC has become a JPG and the original is gone, no later decision recovers it.
One choice is reversible and the other is permanent.

The second reason is that an append-only archive **destroys nothing**, so the
[conservation invariant](README.md#cross-cutting-invariants) is satisfied by
construction rather than by machinery. See [§11](#11-what-this-deletes).

## 2. Tier layout

```
/pix/master/{device}_{datetime}/{filepath}_{filename}.{ext}       originals — sacred
/pix/master/{device}_{datetime}/{filepath}_{filename}.{ext}.xmp   metadata — the record
/pix/render/{device}_{datetime}/{filepath}_{filename}.{ext}.{jpg|mp4}
/pix/thumb/{device}_{datetime}/{filepath}_{filename}.{ext}.jpg
/pix/rules.yaml                                                   distribution definitions
/photo, /tv, /book, ...                                           distributions
<distribution>/.pix-export.json                                   per-tree manifest
```

| Tier | Precious? | Backed up? | Regenerable from |
|---|---|---|---|
| master originals | **yes** | yes | nothing — this is the archive |
| sidecars | **yes** | yes | nothing — this is the record |
| renders | no | no | master |
| thumbnails | no | no | master/render |
| distributions | no | no | master + sidecars + rules |
| index | no | no | sidecars |

Master and the tiers derived from it are **separate Synology shared folders on
one volume**. Shared folders are the unit of Hyper Backup selection and of Btrfs
snapshots, so this gives independent backup policy per tier; and because they sit
on one volume they remain one filesystem, so byte-sharing between them stays
available if the runtime supports it.

## 3. Master

`{device}_{datetime}` is the **import** folder — a record of an ingestion event,
not an organizational scheme. A photo taken in January and imported in March
lands in the March folder. Import date, not capture date.

Within it, the device-side path is flattened into the filename
(`100APPLE/IMG_4471.HEIC` → `100APPLE_IMG_4471.HEIC`) so that **the name carries
its own provenance**: pull one file out of the tree and it still says where it
came from. The device's original filename is kept as the tail, so nothing is
lost.

Master is append-only. Files are never deleted, never converted, never renamed
after landing. Exact byte-duplicates *may* be removed (an identical copy carries
zero additional information); near-duplicates are recorded, never deleted.

## 4. Metadata — XMP sidecars

One sidecar per master file, named **full-filename plus `.xmp`**
(`IMG_4471.HEIC.xmp`, not `IMG_4471.xmp`).

The full-name form is required, not merely tidier: iPhone Live Photos produce
`IMG_4471.HEIC` and `IMG_4471.MOV` in the same import, and the basename-replacing
convention (Lightroom's, for raw files) would collide on the most common device
in the library.

**Why sidecars and not a database, and not the files themselves:**

- Not the files — that would modify sacred originals.
- Not a database — a central DB is the single dependency the owner explicitly
  refused; lose it and the archive is anonymous.
- Not "metadata lives in the delivery copies" — this was seriously considered and
  rejected on arithmetic. It requires a full-file twin for *every* file you want
  to tag, conversion needed or not, including 1.55TB of `.insv` that has no
  delivery use. It roughly doubles both storage and offsite backup.

Sidecars cost ~2KB each, sit in the same folder as the files they describe, and
are standard XMP that Lightroom, Bridge, darktable and Capture One already read.
This is the raw-photography workflow — negative plus sidecar — which is the
established answer to exactly this constraint.

**Consequence:** losing a folder loses its originals *and* their metadata
together, which is the owner's stated rule ("lose the file, lose what relates to
it") at folder granularity.

A rating change is therefore a 2KB write, not a multi-gigabyte file rewrite.

## 5. Renders

A render is a **format conversion only**, produced when the master file is not
already delivery-ready. One per file — no size or quality profiles.

- Named by appending the target extension to the full master name
  (`IMG_4471.HEIC.jpg`), so a render path is computed mechanically from a master
  path and vice versa, with no lookup table.
- **High quality by default** — near-visually-lossless JPEG, high-bitrate H.264.
  Consumers downscale for themselves (Synology Photos makes its own thumbnails;
  TVs downscale). This is what lets one render serve `/photo`, `/tv` *and*
  `/book`, where a delivery-compressed copy would be a visible loss on print.
- Renders carry **no pix metadata**. Metadata is baked at distribution time
  instead, which means a tag change never invalidates a render and never
  re-transcodes anything.
- Whether a render is needed is an **extension** question for images and a
  **codec** question for video: an `.mp4` containing HEVC still needs one.
- `.insv` can never have a meaningful render (a flat transcode yields
  dual-fisheye that nothing displays usefully) and therefore never gets one.

Renders are disposable and excluded from backup.

## 6. Thumbnails

A separate tier, one small JPEG per master file, ~20GB for the whole library,
disposable.

It exists because the app must present a grid of 100k+ items on hardware that
cannot afford to decode originals on demand (see [§9](#9-hardware)). It is also
what makes `.insv` **taggable without being viewable**: a 360 clip gets a
thumbnail extracted from its embedded LRV proxy — a single-frame extract, not a
transcode — appears in the app, can be assigned an event, and is then findable
later so it can be opened in Insta360 Studio. The app is a **catalogue** for
material it cannot display.

Three independent properties, rather than a file-state enum:

| Property | Condition |
|---|---|
| **taggable** | has a thumbnail — i.e. everything |
| **viewable in-app** | is JPG/MP4, or has a render |
| **deliverable** | passes the distribution's `extensions` allowlist |

`.insv` is taggable, not viewable, not deliverable — and needs no special-casing
to get there.

## 7. Distributions

> A distribution contains a **copy** of the master (or of the render, where one
> exists), with metadata baked in, arranged in the structure the distribution
> defines.

Each is a standing named rule (filter + template + extension allowlist) in
`/pix/rules.yaml`, materialized into its own shared folder — `/photo` for
Synology Photos, `/tv` to sync to televisions, `/book` for print.

- **Copies, not links.** The runtime the app gets on the NAS is not known, so the
  design must not depend on hard links or reflinks. Copying is also what today's
  `pix export` already does and is known to work. If link support is detected at
  runtime it is a pure optimization; correctness never depends on it.
- **Never link to master.** A distribution folder is managed by other software —
  Synology Photos writes ratings back into files — and a link would carry that
  write through into a sacred original. Distributions resolve to a copy, always.
- **Metadata is baked into the copy** at creation. This is what makes the tree
  self-describing to Synology Photos and to anything else, and it is the second,
  independent carrier of the curation: if every sidecar were lost, every file
  ever delivered still holds its rating and event.
- **A date override must rewrite the EXIF capture date** in the copy, not merely
  sit in an XMP field — otherwise a corrected date is invisible to the consumer
  that sorts by it.
- **Canonical naming happens here and only here.** Master keeps provenance names;
  the copy gets a canonical name. Because a name is assigned when a copy is
  created and never changed afterward, the
  [stable-collision-suffix problem](roadmap.md) never arises.
- **Distributions are one-way.** Whether Synology Photos' write-back is captured
  is deliberately out of scope; treat those trees as output.

**Update semantics** — all cheap, none requiring a re-transcode:

| Change | Response |
|---|---|
| tag edited | exiftool write into the existing copy — no re-copy |
| membership gained/lost | copy in / delete |
| template value changed (event renamed) | **move** within the tree — a directory op |
| content changed | never happens; master is immutable |

The last two used to be the expensive cases, because moves re-uploaded through
Synology Drive ([implementation.md](implementation.md#sync-client-interaction)).
On the NAS they are local.

**Cost.** Distributions are the only unbounded number in the design:

> distribution bytes = (delivered content) × (number of tiers each file appears in)

Nested tiers multiply — a 5-star photo in `general`, `photos` and `top` is stored
three times. Distributions must stay curated subsets; a full-library mirror is
another ~2.3TB and makes the array tight immediately.

## 8. Ingest — two phases

Ingest is the one part that stays a **CLI on the Windows desktop**, because a
phone is a USB/MTP device attached to a specific machine.

**Phase 1 — import + upload.** Pull new files off the device, let the importer
cull anything that shouldn't be persisted, name them per [§3](#3-master), upload
to master. Fast; no conversion.

**Phase 2 — process.** Generate thumbnails for everything and renders for what
can have one, and upload them. Run whenever convenient.

The app operates on the **ready set** and shows the rest as a visible backlog
("1,247 files awaiting processing"), cleared by running phase 2. **The app never
transcodes.**

Two properties worth preserving:

- **The desktop's only unique capability is encoding.** Hashing, duplicate
  detection, sidecar writes and index maintenance are all fine on the NAS. Phase
  2 must stay narrowly "find unprocessed files, transcode, upload" — otherwise
  the desktop quietly becomes load-bearing again.
- **Master is adoption-based.** Phase 1 is really just "get correctly-named files
  into a folder", so a manual copy, another machine, or a future Android tool all
  work without the app knowing about them.

## 9. Hardware

| | |
|---|---|
| NAS | Synology RS820+, DSM 7.4.1-90080, 18GB RAM |
| CPU | Intel Atom C3538 — 4 cores @ 2.1GHz, Denverton/Goldmont |
| Storage | 2 × HAT5300-8T in RAID1 → ~7TB usable, 2 bays free |
| Desktop | Ryzen 9 9950X + RTX 5090 |

**The NAS cannot encode.** The C3538 has **no integrated GPU** (Synology lists no
transcoding engine for this model) and Goldmont-era Atoms have **no AVX/AVX2** —
precisely the instruction set x264/x265 depend on. Software 4K HEVC decode plus
H.264 encode on four 2.1GHz cores with no vector acceleration is a small fraction
of realtime. This is why encoding lives on the desktop, and it is not a
preference.

**What the NAS is fine at:** exiftool writes (the app's hot path), remuxes
(`-c copy`, I/O bound), HEIC→JPG for stills, thumbnails, hashing (disk-bound),
index maintenance, and all the copying distributions require. Container Manager
runs on this model; 18GB is far more than a web app plus a SQLite index needs.

**This is also why the app is restricted to JPG/MP4.** The constraint was adopted
for conversion reasons, but it is equally what makes browsing viable: HEIC decode
and HEVC frame extraction on this CPU would make the UI painful no matter who did
the converting.

## 10. Storage and backup budget

| Tier | Approx | Backed up |
|---|---|---|
| master + sidecars | 2.3TB (1.55TB of it `.insv`) | **yes** |
| renders | delivered subset only; disposable | no |
| thumbnails | ~20GB | no |
| distributions | delivered content × tiers | no |
| **offsite** | **~2.3TB** | |

Master only ever grows — near-duplicates are marked, not deleted. The relief
valve is the two empty bays.

## 11. What this deletes

An immutable archive destroys nothing, so most of pix's machinery has no reason
to exist:

- **`migrate`** — no in-place normalization, no tag writes, no format policy
- **Conservation / soft-delete / `runs/` folders** — nothing is ever replaced
- **The plan → confirm → apply ceremony** — it exists to make destruction
  reviewable
- **`rollback`** ([roadmap.md](roadmap.md)) — nothing to roll back to
- **`EXTENSION_POLICY`** as a destructive mapping — it becomes "does this need a
  render?"
- **`organize`** — folder shape is a view, and views are distributions
- **`tag checkout` / `--commit` / the freeze** — no hard-link workspace, so no
  inode identity to protect; tagging is a direct edit
- **The library lock** — one long-lived process owns the archive; concurrency
  becomes an internal queue and DB transactions rather than defence against
  competing CLI invocations
- **Stable collision suffixes** ([roadmap.md](roadmap.md)) — names are assigned
  at copy creation and never changed
- **Sync-client re-upload avoidance** ([implementation.md](implementation.md#sync-client-interaction))
  — the master no longer travels through Synology Drive

## 12. What survives

- **`pix import`** ([import.md](import.md)) — becomes phase 1, and shrinks
- **Format-aware content hashing** — identity that ignores metadata, so a tag-only
  edit doesn't read as a different file
- **The export reconcile engine** ([export.md](export.md)) — desired-set diff,
  per-tree manifest, target validation, drift-stops-rather-than-guesses. It was
  designed for exactly this job.
- **The tag-filter and template grammar** ([tags.md](tags.md))
- **Perceptual dedupe** ([dedupe.md](dedupe.md)) — as *detection*, recording that
  two files are the same rather than deleting one
- **The H.264 delivery requirement** ([export.md](export.md#big-todo--video-must-ship-as-h264-compatibility-rendition))
  — and it stops being contentious: transcoding was only ever risky because it
  destroyed the original, and now the original is preserved forever

## 13. Open questions

- **Import-ledger identity.** `pix import` currently avoids re-downloading by
  reading `pix:OriginalPath` from the file; under this architecture nothing is
  written to master files. The naming scheme nearly solves it (folder = device,
  filename = device-side path, so "do I have this?" is a directory listing) —
  but device counters recycle (`IMG_9999` → `IMG_0001`), so name alone is not a
  safe key across years. Needs size or capture-date alongside it.
- **What the distributions actually are** — the only unbounded number in
  [§10](#10-storage-and-backup-budget). Needs a real estimate of what fraction of
  the library gets rated into a tier, and how many tiers.
- **App deployment and authentication** — Container Manager specifics, and how
  family members sign in.
- **Synology Photos write-back** — currently declared out of scope; revisit if
  rating inside Photos turns out to be wanted.
- **Btrfs vs ext4** on the volume — determines whether reflinks are available as
  an optimization for distribution copies.
- **Multi-user editing** — the original motivation for a hosted UI. With no
  checkout and no freeze this is nearly free, but the write-queue and conflict
  semantics are unspecified.
