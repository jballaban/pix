# Import — `pix import`

> **Status: designed; assumptions pending validation.** This spec is written
> against *expected* MTP/WPD/iOS behavior. Everything in
> [Assumptions to validate](#assumptions-to-validate) must be confirmed against
> real devices (iPhone + Android over USB) before building — several design
> choices depend on them. Treat this file as a plan to verify, not settled fact.

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
`os.scandir`. Access is via the WPD API (COM; `pywin32` or a wrapper) — see
[assumption W1](#w-access--enumeration).

**Single-lane model.** An MTP device gives one serialized command/response
session (assumed — [W2](#w-access--enumeration)). So the architecture is *not*
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

Each device has a stable serial (assumed — [D1](#d-device-identity)). On connect,
look it up in a registry mapping `serial → friendly name`; on an unknown serial,
**prompt the user to name it** (so data is labeled by a human name, not a UUID).

Registry storage: durable, non-regenerable, tiny — lives in the **synced `.pix/`
durable tier** (e.g. `.pix/devices.yaml`), *not* `.pix/local`, so device names
survive a cache wipe.

## What we import — camera roll only

"Camera roll" resolves per platform (confirm — [C1](#c-camera-roll--formats)):

- **iOS:** all of `DCIM/` (the `NNNAPPLE` folders).
- **Android:** `DCIM/Camera/` specifically (Android's `DCIM` also holds other
  apps' folders and `.thumbnails`).

Platform is decided from the WPD device manufacturer (Apple → iOS rules). The
device's own sub-structure under the camera-roll root is **preserved** on
landing (import is true to the source; migrate flattens later).

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

- `pix:ImportId` = `<serial>:<object-id>` — the durable, convert-surviving skip
  key (survives HEIC→jpg per the CONVERT-preserves-metadata invariant).
- `pix:OriginalPath` = the **device** path (from the sidecar) — more meaningful
  and permanent than the transient staging path.

Add `pix:ImportId` to `metadata_filter` so it's cached and queryable.

## Incremental import (skip already-imported)

The skip decision must happen **before** downloading, so the key must be
derivable from **cheap MTP metadata only** (never a content hash — that needs
the download we're avoiding):

- **Preferred key:** WPD persistent unique id (`WPD_OBJECT_PERSISTENT_UNIQUE_ID`)
  — stable for the same object across reconnects (assumed — [I1](#i-incremental-identity)).
- **Fallback key:** `(filename, size, capture-date)` composite (all cheap object
  properties). Filename alone is unsafe — iPhones recycle `IMG_0001` past 9999.

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
   — so you'd import lossy re-encodes. This is likely **detectable** (camera-roll
   objects arriving as `.JPG`/H.264 where HEIC/HEVC were expected — confirm
   [S1](#s-ios-settings)); a hard warning (or gate) is warranted. Prerequisite:
   set **Keep Originals**.
2. **"Optimize iPhone Storage"** — full-res lives in iCloud; the device may hold
   only a downscaled copy, and MTP only sees what's physically present.
   **Not reliably detectable** ([S2](#s-ios-settings)). Best-effort: warn on
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

## Assumptions to validate

Each is a **testable claim + method**. Validate on a real iPhone (and an Android)
over USB before building. Group by area; IDs are referenced above.

### W — access & enumeration
- **W1.** WPD is reachable from Python well enough to enumerate objects and
  stream content (via `pywin32` COM to the WPD API, or a maintained wrapper).
  *Test:* enumerate DCIM and read one file's bytes.
- **W2.** An MTP device serializes commands / permits effectively one session;
  concurrent device ops give no throughput win. *Test:* attempt overlapping
  enumerate+read; measure vs. serial.
- **W3.** Enumeration is slow enough (per-object round-trips) to justify the
  interleave. *Test:* time a full camera-roll enumeration on a large roll.
- **W4.** Object **size** and **capture/modified date** are available as cheap
  properties *without* reading content. *Test:* read those props; confirm no bulk
  transfer occurs.

### D — device identity
- **D1.** WPD exposes a **stable device serial** (`WPD_DEVICE_SERIAL_NUMBER`)
  that is identical across disconnect/reconnect (and ideally reboot). *Test:*
  read it, reconnect, compare.

### I — incremental identity (the one the user most wants to confirm)
- **I1.** `WPD_OBJECT_PERSISTENT_UNIQUE_ID` exists per object and is **stable
  across disconnect/reconnect** for the same photo. *Test:* record IDs for a set
  of photos, disconnect, reconnect, re-enumerate, confirm unchanged. **If it
  fails, we fall back to `(filename,size,date)`** — so also capture those to
  confirm the fallback is viable.

### C — camera roll & formats
- **C1.** Camera-roll structure is `DCIM/NNNAPPLE/` (iOS) and `DCIM/Camera/`
  (Android). *Test:* enumerate device root; record the actual tree.
- **C2.** Live Photos present as two correlatable objects (`IMG_1234.HEIC` +
  `IMG_1234.MOV`). *Test:* inspect a Live Photo's objects.

### S — iOS settings
- **S1.** "Automatic" transfer surfaces camera-roll photos as `.JPG` (and video
  as H.264) — i.e. the format-mismatch tell is real and detectable. *Test:*
  toggle the setting; observe object extensions/types over MTP.
- **S2.** Optimized-storage behavior over MTP: do not-downloaded originals appear
  at all, appear downscaled, or are absent? Is an original-resolution property
  exposed to beat the size heuristic? *Test:* with optimized storage on and some
  photos not-downloaded, enumerate and inspect size vs. any resolution property.

### V — verification / transfer
- **V1.** Large-file reads can be chunked for byte-progress; whether a partial
  read is resumable mid-file (likely not — assume whole-file retry). *Test:*
  interrupt a video read; observe.

## Cross-references

- [README.md](README.md#operations) — ops table (import is the new front-end op).
- [migrate.md](migrate.md) — the import-ingest pre-pass seam (sidecar → in-file
  tags); mirrors the existing `errors`/`stash` restore passes.
- [library.md](library.md) — `.pix/local/import/` landing; `pix:ImportId` /
  `pix:OriginalPath` provenance.
- [implementation.md](implementation.md#sync-client-interaction) — `.pix/local`
  is already sync-excluded, so pending imports don't sync.
- [tags.md](tags.md) — add `pix:ImportId` to the tag model + `metadata_filter`.
