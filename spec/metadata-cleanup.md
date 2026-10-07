# Metadata — clean downloads

**Status: decided and built 2026-10-07** — photos and videos.
Extends [nas-app.md](nas-app.md) and amends its
[§15 round trips](nas-app.md#round-trips): downloads are now stamped as they
leave, not when the copy is made.

**The rule:** a download is someone deliberately making a copy, so it carries
nothing identifying — no location, no camera, no people, no tags, none of the
old pix's paths — only what the file needs to display correctly and an opaque
pix id so a copy that comes home can be recognised. The archive itself is left
alone.

---

## 1. What leaked (measured 2026-10-07)

A download (`/download`, `/download.zip`) handed over the stored bytes
unchanged — the master, its render, or a clip's cut.

| File | What it carries |
|---|---|
| Seeded master JPG | full GPS, make/model/software, Apple `PhotoIdentifier`, embedded thumbnail, face regions, old `pix:` tags (`EventOverride`, `OriginalPath: G:\pix\raw\tmp\mtp\james\…`), a `UserComment` with that path |
| Seeded master MP4 | GPS, make/model, `pix:ImportId` (phone serial), `pix:OriginalPath` |
| GoPro master | camera and lens serials, firmware, a telemetry track with a GPS trace in `mdat` |
| Render (the default download) | GPS, and a comment with the old path, a person's name and a USB device id — `process` copied the source's metadata across |
| Clip cut | `pix:SourceFile` (folder/name), `pix:ClipId`, `pix:ClipRange` |
| Thumb / large / preview | clean |

**Names leaked too.** Seeded masters are named for their old path —
`G_pix_2026_Banff Skiing - Ballabans_2026-03-20_110149.jpg` — and a download
used that name; a zip put each file in a folder named after its master folder.

## 2. Decided: the originals are left alone

The seeded library carries the old pix's own tags (`EventAuto`,
`EventOverride`, `OriginalPath`, `DateAuto`, `MergeEvent`, `ImportId`,
`Rating`, `NORMALIZE_*` comments). Rewriting ~16k seeded masters (and every
year still to seed) to remove them was considered and **declined**: the tags
are not that sensitive, they are visible only to the administrator (admin
views, SMB, backups), and once downloads are cleaned nothing carries them out
of the app. [Rule #1](nas-app.md#1-why-sacred-originals-and-not-self-describing-files)
stays absolute and seeding is not held up.

The camera's own data (GPS, make/model, serials, face regions) stays in master
too — it is the genuine original, and future phone imports carry the same.

Still true, and fine: the index reads a seeded file's event through to its
embedded tags when no sidecar names one (`index.inherited_event`).

## 3. What a download keeps

Everything else is dropped.

- **The date** — the *effective* date (override applied), as
  `DateTimeOriginal` and `DateTimeDigitized`, so a phone files it under the
  right day. No time-zone offset (it says roughly where), no sub-seconds.
- **Orientation**, or iPhone photos turn sideways.
- **The colour profile** (ICC, and EXIF `ColorSpace`), or colours shift.
- **Decoding markers** — JFIF (without its thumbnail) and Adobe `APP14`, which
  says how to read the colour channels.
- **`pix:SourceId`** — the master's content hash (`j:…` / `m:…`). Opaque
  outside the library; the index resolves it instantly. Replaces
  `pix:SourceFile` on everything that leaves, which was a `folder/name` path.

Dropped, for example: GPS, make/model/lens/serials/software, face regions,
embedded thumbnails, all XMP (old `pix:` tags included), IPTC, comments, MPF
and anything after the image's end — **the HDR gain map goes with it**, so an
iPhone HDR photo downloads as standard range.

## 4. Names

A download is named by its effective date: `2026-03-20_110149.jpg` (the
extension of what is sent, lower-cased). With no date, `pix_<8 hex of the
source id>`. A zip is **flat**, clashes numbered `_2`, `_3`, … — master folder
names never appear.

## 5. How — rewritten on the fly, no second copy

Metadata sits in blocks beside the coded image, not inside it, so cleaning is
copying the image data under a new header. Nothing is decoded or re-encoded;
the NAS does it in plain Python with no exiftool. No sanitised copy is stored.

The administrator's *original* (master rather than render) is cleaned the same
way. The exact bytes are reachable over SMB.

### Photos (JPEG) — built (`pix.nas.delivery.clean_jpeg`)

The segments are walked from the start:

- a new minimal `APP1` Exif — orientation, `ColorSpace`, the date;
- a new `APP1` XMP holding only `pix:SourceId`;
- `APP0` JFIF (thumbnail cut), `APP2` ICC chunks, `APP14` Adobe — kept;
- every other `APPn` and `COM` — dropped;
- tables, frame header, every scan (progressive files have several) — copied
  verbatim, in order, up to the first end-of-image; anything after it dropped.

A file that starts like a JPEG but cannot be walked is **refused**, never sent
as-is: an unparseable file is exactly the one whose metadata nobody checked.
Other formats (PNG, `.insv`, future HEIC) go out unchanged for now — see §8.

The whole file is read into memory (a few MB) and the result is sent with a
known length.

### Videos (MP4/MOV/M4V) — built (`delivery.plan_video`, `stream_video`)

A video's metadata lives in boxes inside `moov`; removing them would shift the
chunk offsets the file uses to find its data. Instead `moov` is read (a few KB
to a few MB, wherever it sits — phones put it first, GoPros last) and a list
of **same-length edits** is applied as the file streams past:

- every metadata box — top-level `uuid` (the old pix's XMP) and anything else
  not `ftyp`/`moov`/`mdat`, and inside `moov` everything but `mvhd`, `trak`,
  `iods` (`udta` with location/make/model, `meta` with a phone's keys) — is
  relabelled `free` and zeroed; existing `free`/`skip`/`wide` are zeroed;
- **only video and audio tracks are kept.** Every other track — GoPro
  telemetry (`gpmd`, with its GPS trace), timecode, `fdsc`, subtitles, a
  phone's timed metadata — has its `trak` relabelled `free`, **and the bytes
  of its samples zeroed**: its sample tables (`stco`/`co64`, `stsc`,
  `stsz`/`stz2`) give each chunk's exact range. Hiding the track alone would
  leave the GPS readable to anyone who digs;
- in kept tracks, `tref` (it may point at a freed track) and any
  `udta`/`meta` are freed, and the **handler names** (*GoPro AVC*, *Core Media
  Video*) and the video's **compressor name** are blanked;
- the dates stay in `mvhd`/`tkhd`/`mdhd` — moved by the curator's correction
  if there is one (effective − capture, in seconds; those times are UTC);
  rotation stays in the track matrix.

**`pix:SourceId` is appended** as a top-level XMP `uuid` box after the last
box — written anywhere earlier it would move data that offsets point at.
A last box that declares *to the end of the file* (or more than the file
holds) gets its true size first. `roundtrip.returned` reads a file's last
64KB as well as its first MB to find it. A video the index has no content
hash for yet goes unstamped: finding one would read the whole file first.

Junk after the last box (a trailer, a serial number) is not sent. A
**fragmented** file (`moof`) or a compressed `moov` is refused, as is
anything that cannot be walked. The response has a known length (the source
less any junk, plus the stamp); `Range` is not offered for downloads.

Measured on real masters (2026-10-07): 14 videos, 4 of them GoPro — exiftool
`-ee` finds no GPS, make, model, serial or handler names; GoPro `GPS5`/`GPSU`
FourCCs 18 → 0 in the bytes; audio kept where there was any; every copy
decodes cleanly in full; phone videos keep their content hash.

## 6. Recognising a copy that comes home

**The content hash already does most of it.** It covers the coded data only
([nas-app.md §15](nas-app.md#15-identity--when-two-files-are-the-same-photograph)),
so:

- a cleaned **photo** has the same content hash as what it was cleaned from —
  unless trailing data (a gain map) was dropped;
- a cleaned **video** whose data was untouched has the same hash too.

**The download hash** covers the rest: when what is sent hashes differently
from its source — a photo with a dropped gain map, a video with zeroed
telemetry — that hash is recorded, on the first download, in an append-only
`app/delivered.jsonl` on the share (`{"hash", "folder", "name"}`), once per
hash. It is computed from bytes already in hand (photos) or as the data
streams past (videos), so it costs nothing extra. The output is deterministic,
so one record per file is enough. It lives beside `users.json` rather than in
the index, which a rebuild drops.

`roundtrip.known_hashes` reads it alongside the index's masters, renders and
clips, and `roundtrip.returned` recognises `pix:SourceId` as well as the old
`pix:SourceFile` / `pix:ClipId`, so copies handed out before this still come
home.

## 7. Considered and rejected

- **Stripping the originals** (on the way in for unseeded years, plus a
  one-off rewrite of the ~16k seeded) — §2.
- **A sanitised copy of every file** — doubles storage.
- **`exiftool` / `ffmpeg` per request on the NAS** — a temporary copy of
  every multi-GB video, and the image has no exiftool.
- **Precomputed redaction maps** (byte ranges recorded by `process`, applied
  while streaming) — it existed to make video *playback* clean too; for
  downloads the header can simply be read at request time.

## 8. Not covered yet

- **Video playback** (`/media`) sends the whole file, so a viewer can save it
  from the browser. Treated as not a deliberate copy for now. The video
  edits are same-length except the appended stamp, so playback could apply
  them without the stamp and keep `Range` working.
- **HDR is lost for about a quarter of photos.** A sample of 160 real masters
  (2026-10-07): 84 carry a second image after the primary — 42 an Apple HDR
  gain map, 40 a large preview, 2 something else. All are dropped, so those
  downloads hash differently and get a download hash. Keeping the gain map
  would mean cleaning its own segments and rewriting the MPF index's offsets,
  which move when the primary's header shrinks.
- **Other formats** — PNG, `.insv`, future HEIC (ISO base-media, like MP4,
  with orientation outside EXIF) go out unchanged.
- **The details panel** shows *Original path* to every signed-in user
  (`webapp/api.py`), and that path can contain names. Should be admin-only.
- **Distributions** ([nas-app.md §7](nas-app.md#7-distributions)) were to bake
  events and people *into* household copies — they need their own rule.
