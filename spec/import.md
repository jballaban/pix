# Import — `pix import`

> **Status: core assumptions validated on a real iPhone (iOS, over WPD); Android
> still unvalidated.** The WPD/iOS claims below were measured against an iPhone
> (device serial `M2DF33MY06`) using Python + `comtypes` — see
> [Validation results](#validation-results). Several assumptions were **wrong as
> originally written** (notably camera-roll structure — no `DCIM/NNNAPPLE` over
> MTP) and have been corrected inline. Android behavior is still assumed, not
> measured.

## Purpose & scope

`pix import` pulls **new** photos and videos directly off a connected phone
(iPhone / Android, USB) and lands them on disk. That is its *entire* job.

- It does **not** convert, rename, tag-normalize, dedupe, or organize — the
  existing pipeline (`migrate` → `hash` → `dedupe` → `organize`, i.e. `sync`)
  does all of that afterward.
- It **never modifies the source files** (device copies land byte-for-byte) and
  **never deletes from the device** (phone-side deletion is a deferred, explicitly
  gated future action — see [Deferred](#deferred--out-of-scope)).
- It is **incremental**: already-imported files are skipped *without being
  re-downloaded* (MTP transfer is the expensive part), driven by a manifest.

## Where it fits

```
pix import  →  .pix/local/import/<device>/…   (pristine files + sidecars)
                     │
pix migrate  ────────┘  ingest pre-pass: move into library, normalize,
                        write provenance in-file, drop sidecar
pix organize ─────────  arrange into the template
```

Import is the missing front-end; migrate gains a small **import-ingest pre-pass**
(analogous to its existing `errors/` and `stash/` restore passes) that pulls
landed files into the library.

## Device access (MTP / WPD)

Phones present over USB as **Windows Portable Devices (WPD)** — an MTP/PTP
device in the shell namespace, **not** a drive letter, so there is no path to
`os.scandir`. Access is via the WPD COM API. **`pywin32` does not wrap WPD**;
the working path (validated) is **`comtypes`**, which auto-generates the COM
interfaces from `PortableDeviceApi.dll` + `PortableDeviceTypes.dll`.

**comtypes gotchas found during validation** (matter for the build):

- `IEnumPortableDeviceObjectIDs::Next` returns an `LPWSTR[]` array, but comtypes
  surfaces **only the first element** — so either call `Next(1)` (one object id
  per round-trip, what the probe does) or hand-write a memberspec to get true
  batching. Batching matters for enumeration cost (see W3).
- `IPortableDeviceManager::GetDevices` uses the count-then-array two-call pattern;
  comtypes returns the in/out params as a list `[array, count]`.
- Device object ids are short opaque tokens (`o1058`, `oF99`, `s10001`), not paths.

**Single-lane model.** An MTP device gives one serialized command/response
session (confirmed — [W2](#validation-results): ~40 MB/s single lane). So the architecture is *not*
"enumerate thread + download thread":

- **One MTP thread** owns the device and **interleaves** enumerate and download:
  enumerate a chunk (a camera-roll subfolder → object list + cheap metadata),
  queue it, download a few, enumerate the next chunk, repeat. This makes the
  progress **denominator grow progressively** instead of blocking on a full scan.
- **One worker thread** (or main) does off-device work: write bytes, verify,
  update the manifest, drive the UI. This overlaps disk/verify with the next MTP
  read (modest win) and keeps the UI responsive.

Interleaving buys **early progress and a live denominator**, not raw throughput
(the USB pipe is the ceiling).

**Progress model.** Two moving counters — `downloaded` (numerator) and `found`
(denominator, still growing while enumeration continues) — plus bytes and a
rolling rate (MB/s, files/min). No hard ETA (the denominator is not yet final);
per-file byte progress for large videos.

## Device registry & naming

Each device has a stable serial (confirmed — [D1](#validation-results)). On connect,
look it up in a registry mapping `serial → friendly name`; on an unknown serial,
**prompt the user to name it** (so data is labeled by a human name, not a UUID).

Registry storage: durable, non-regenerable, tiny — lives in the **synced `.pix/`
durable tier** (e.g. `.pix/devices.yaml`), *not* `.pix/local`, so device names
survive a cache wipe.

## What we import — camera roll only

"Camera roll" resolves per platform. Platform is decided from the WPD device
manufacturer (`Apple Inc.` → iOS rules; USB VID `0x05AC`).

- **iOS (measured):** iOS does **not** expose `DCIM/NNNAPPLE` over MTP. It
  presents a single functional object **"Internal Storage"** whose direct
  children are **capture-month buckets** named `YYYYMM_a` / `YYYYMM_b`
  (the `_b`/`_c` suffix is overflow when a month is large) — e.g.
  `Internal Storage/202605_a/IMG_7399.JPG`. On this device the roll was 27 such
  folders. So the iOS import target is simply **everything under "Internal
  Storage"** — there is no `DCIM/` level to scope to, and no non-camera-roll
  siblings were present to exclude.
- **Android (assumed, unvalidated):** `DCIM/Camera/` specifically (Android's
  `DCIM` also holds other apps' folders and `.thumbnails`).

The device's own sub-structure is **preserved** on landing (import is true to
the source; migrate flattens later), so the month-bucket names are harmless.

**`.AAE` sidecars (new, measured).** iOS surfaces Apple edit sidecars
(`IMG_xxxx.AAE`, non-destructive edit instructions) over MTP — 73 in the sample
roll. They are **not** originals. **Import skips `.AAE`** (migrate would only
treat them as junk); the edited render is already a separate object in the roll.

## Landing & tracking

**Landing:** `.pix/local/import/<friendly-name>/<device-relative-path>`, e.g.
`.pix/local/import/Jamies-iPhone/DCIM/100APPLE/IMG_0001.HEIC`. Chosen because
`.pix/local` is **excluded from sync** → pending imports don't double-upload
(they upload once, later, when migrate moves them into the library). Pending
imports have no off-machine backup, which is acceptable: **the phone still holds
them** (import never deletes), so a lost `.pix/local` just means re-import.

**Files are pristine** — never modified at import. Provenance rides in a
**sidecar** per file: `<name>.importinfo` (YAML, matching the `.stashinfo` /
`.errorinfo` convention), recording:

- device serial + friendly name
- stable object id (PUID, or the fallback composite — see below)
- device path (for `OriginalPath`)
- capture date, original filename, size

**Migrate ingest → provenance becomes in-file.** When migrate's import-ingest
pre-pass moves a landed file into the library, it reads the `.importinfo`
sidecar and writes the provenance **into the resulting library file** as pix
tags — then drops the sidecar. Because the **library stores only jpg/mp4** (both
embed XMP), no library-side sidecar is ever needed. The two tags:

- `pix:ImportId` = `<serial>:<persistent-unique-id>` — the durable,
  convert-surviving skip key (survives HEIC→jpg per the CONVERT-preserves-metadata
  invariant). Uses the PUID, **not** the raw object id — see [I1](#validation-results).
- `pix:OriginalPath` = the **device** path (from the sidecar) — more meaningful
  and permanent than the transient staging path.

Add `pix:ImportId` to `metadata_filter` so it's cached and queryable.

## Incremental import (skip already-imported)

The skip decision must happen **before** downloading, so the key must be
derivable from **cheap MTP metadata only** (never a content hash — that needs
the download we're avoiding):

- **Preferred key:** WPD persistent unique id (`WPD_OBJECT_PERSISTENT_UNIQUE_ID`)
  — **validated stable** on iOS across unplug/replug *and* a full reboot (146/146
  unchanged; the object id itself was also stable, but the PUID is the documented
  contract) — see [I1](#validation-results). Still unvalidated on Android.
- **Fallback key:** `(filename, size, capture-date)` composite (all cheap object
  properties, all confirmed readable without download). Filename alone is unsafe —
  iPhones recycle `IMG_0001`, and non-camera filenames are randomized 4-letter
  stems (`FLAO4412`), so name collisions are real; size+date disambiguate.

**Manifest = a regenerable cache** (a table in `.pix/local/cache.db`), not
only-copy state. It's the union of two durable sources and rebuilds from them:

- **committed** — `pix:ImportId` tags on library jpg/mp4 files.
- **pending** — `.importinfo` sidecars in `.pix/local/import/`.

Skip an enumerated device object iff its key is in the manifest (pending or
committed). Because the manifest is a cache, losing `.pix/local` is safe: the
committed half regenerates from library tags; the pending half is re-downloaded
from the phone (they land dated-old and are easy to find/purge).

**dedupe is the correctness backstop.** The manifest is purely a download-skip
optimization; anything that slips through and is re-downloaded gets caught by
`dedupe`'s content hash after the fact. So the manifest may be best-effort.

**Delete semantics** (consequence of the above): delete a file from the library
and, while the manifest cache is intact, it is **not** re-imported. Only if the
cache is lost *and* regenerated does a since-deleted file re-download (rare;
findable by date).

## Retry, resume, verification

**Three failure tiers:**

- **File-level transient** (read timeout, single-object error) → retry that
  object N times with backoff.
- **Session drop** (device unplugged / sleeps / re-enumerates) → re-open the
  session and resume, skipping manifest-completed files.
- **Permanent** (unreadable / access denied / decode) → log, skip, continue;
  never abort the whole run. Report at the end.

The manifest doubles as the **resume checkpoint**: a file is recorded as imported
only after a verified, atomic landing, so an interrupted import re-runs and skips
completed files for free.

**Atomic landing + verification:** download to a temp name → verify
`bytes-read == MTP-reported size` and a clean complete read → atomic rename to
final → write the `.importinfo` sidecar → record in the manifest. Never leave a
partial file that looks complete. (No source content hash is available without
downloading, so verification is size-match + clean read.)

## iOS caveats — originals & optimized storage

Two iOS settings can corrupt a library import; the **first is worse**:

1. **"Keep Originals" vs "Automatic" transfer** (Settings → Photos → Transfer to
   Mac or PC). On **Automatic**, iOS transcodes over USB — HEIC→JPEG, HEVC→H.264
   — so you'd import lossy re-encodes. **Detection must use file extension, not
   the WPD format GUID** (measured: HEIC and JPEG both report format
   `{38010000-…}`; MOV and MP4 both report `{300D0000-…}` — the GUID buckets by
   broad type, never by codec/container). The reliable tell is the **extension
   mix**: Keep-Originals rolls contain `.HEIC` (confirmed present alongside `.JPG`
   on the test device); an all-`.JPG`, zero-`.HEIC` roll is the Automatic-mode
   signature. A hard warning (or gate) on that signature is warranted.
   Prerequisite: set **Keep Originals**.
2. **"Optimize iPhone Storage"** — full-res lives in iCloud; the device may hold
   only a downscaled copy, and MTP only sees what's physically present.
   **Not reliably detectable** ([S2](#validation-results), untested). Best-effort: warn on
   improbably-small-for-type files (strongest signal on video); collect into an
   end-of-run report. Prerequisite: **Download Originals to this iPhone**.

Policy: print both prerequisites on connecting an iPhone; **warn, don't
fail-hard** on the size heuristic (too many false positives on photos);
fail/hard-warn only on the detectable format-mismatch case.

## CLI (proposed — refine during build)

- `pix import` — auto-detect connected device(s); if one, use it; if several,
  prompt. Unknown serial → prompt for a name.
- Flags TBD: `--device <name>`, `--dry-run` (enumerate + report new-vs-skip
  counts, no download), maybe `--to <path>` to relocate the landing root.

## Deferred / out of scope

- **iCloud import** — a different access model entirely (API + auth, not USB);
  its own future spec. Explicitly out of scope here.
- **Phone-side deletion after import** — a future, explicitly gated action
  ("yes, NOW delete from the phone"), never implicit.
- **Android edge devices / non-DCIM sources**, screenshots, other albums.

## Validation results

Measured on a real **iPhone (iOS) over WPD** — device serial `M2DF33MY06`, USB
VID `0x05AC` — using Python + `comtypes`, ~900-file sampled roll. Status is one
of **CONFIRMED** / **CORRECTED** (assumption was wrong; spec above fixed) /
**UNVALIDATED** (not yet tested). **Android is entirely unvalidated.**

### W — access & enumeration
- **W1 — CONFIRMED.** WPD enumerates objects and streams content from Python via
  `comtypes` (not `pywin32` — it does not wrap WPD). Auto-gen from
  `PortableDeviceApi.dll` + `PortableDeviceTypes.dll`.
- **W2 — CONFIRMED.** Single-lane. Serial read = 40.0 MB/s; a second session opens
  fine *sequentially*, but two sessions streaming *concurrently* had one rejected
  with `0x800700AA "resource in use"`, and aggregate throughput was 41 MB/s
  (ratio 1.02 vs serial — no win). The USB/MTP pipe is a hard ~40 MB/s single lane
  and concurrent stream access is actively refused → interleave enumerate+download
  on **one** MTP thread; parallel download threads are pointless and error-prone.
- **W3 — CONFIRMED.** Cold enumeration ≈ **27 objects/s** (one `Next` + one
  `GetValues` per object through comtypes). A large roll is minutes of enumerate
  before any download → the interleave (live denominator, early progress) is
  justified. Levers: batched `Next` (needs a hand-written memberspec) and
  `IPortableDevicePropertiesBulk`. (Windows' warm WPD property cache makes a
  second pass far faster, but cold is the number that matters.)
- **W4 — CONFIRMED.** `ORIGINAL_FILE_NAME`, `SIZE`, `DATE_CREATED`, and
  `PERSISTENT_UNIQUE_ID` all read as cheap properties, no content transfer. Two
  gotchas: **`WPD_OBJECT_NAME` is empty on iOS** (use `ORIGINAL_FILE_NAME`), and
  `DATE_CREATED` is a **string** `YYYY/MM/DD:HH:MM:SS.fff`, not `VT_DATE`.

### D — device identity
- **D1 — CONFIRMED.** `WPD_DEVICE_SERIAL_NUMBER` = `M2DF33MY06`, identical across
  unplug/replug **and** a full reboot. Manufacturer `Apple Inc.` drives iOS
  platform detection.

### I — incremental identity (the one the user most wanted to confirm)
- **I1 — CONFIRMED (iOS).** `WPD_OBJECT_PERSISTENT_UNIQUE_ID` exists per object
  (form `{0000XXXX-0000-0000-YYXX-000000000000}`, derived from the object id) and
  was **stable across both unplug/replug and a full power-cycle** — 146/146
  sampled objects unchanged, and the raw object id was also stable. The PUID is
  the documented-stable contract we key on. Fallback `(filename,size,date)` also
  confirmed viable (all three cheap-readable). **Android unvalidated** — MTP
  handle churn is more likely there; retest before trusting the PUID cross-platform.

### C — camera roll & formats
- **C1 — CORRECTED.** iOS does **not** expose `DCIM/NNNAPPLE` over MTP. It exposes
  `Internal Storage/<YYYYMM>_<a|b>/` capture-month buckets (27 folders measured).
  Import target = everything under "Internal Storage". Android (`DCIM/Camera/`)
  still assumed.
- **C2 — CONFIRMED.** Live Photos present as correlatable stem pairs
  (`IMG_xxxx.{HEIC|JPG}` + `IMG_xxxx.MOV`); 50 pairs in the sample.
- **C3 (new) — CONFIRMED.** WPD **format GUID does not distinguish codec/container**
  — HEIC and JPEG both report `{38010000-…}`; MOV and MP4 both `{300D0000-…}`.
  Any format decision (transcode detection, image-vs-video) must use the
  **extension**. Also: `.AAE` edit sidecars appear over MTP and are skipped.

### S — iOS settings
- **S1 — CONFIRMED (mechanism) / partially tested.** The transcode tell is real
  but is **extension-based, not GUID-based** (see C3). `.HEIC` present alongside
  `.JPG` on the test device ⇒ Keep-Originals. The Automatic-mode signature
  (all-`.JPG`, zero-`.HEIC`) was not directly induced by toggling the setting.
- **S2 — UNVALIDATED.** Optimized-storage-over-MTP behavior (do undownloaded
  originals appear / downscale / vanish; any resolution property to beat the size
  heuristic) was not tested — the test device had originals present.

### V — verification / transfer
- **V1 — CONFIRMED.** A 168 MB `.MOV` read fully; bytes-read **exactly matched**
  the reported `SIZE` (size-match verification works); 256 KB optimal buffer,
  ~40 MB/s over USB; chunked reads → per-file byte progress works. Mid-file resume
  not tested — assume whole-file retry.

### Probe tooling
The validation probe (`comtypes`-based WPD walker: `list` / `report` / `snapshot`
/ `compare` modes) lives outside the repo (session scratchpad). It is the
reproducible harness for re-running these checks — notably **I1/D1 on Android**
and **S2** — before/while building.

## Cross-references

- [README.md](README.md#operations) — ops table (import is the new front-end op).
- [migrate.md](migrate.md) — the import-ingest pre-pass seam (sidecar → in-file
  tags); mirrors the existing `errors`/`stash` restore passes.
- [library.md](library.md) — `.pix/local/import/` landing; `pix:ImportId` /
  `pix:OriginalPath` provenance.
- [implementation.md](implementation.md#sync-client-interaction) — `.pix/local`
  is already sync-excluded, so pending imports don't sync.
- [tags.md](tags.md) — add `pix:ImportId` to the tag model + `metadata_filter`.
