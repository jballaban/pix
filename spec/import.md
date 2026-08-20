# Import — `pix import`

> **Status.** The **device→disk import and the ingest seam are both built**
> (WPD/iOS validated against a real iPhone, serial `M2DF33MY06`, via `comtypes` —
> see [Validation results](#validation-results); several original assumptions were
> **wrong**, notably no `DCIM/NNNAPPLE` over MTP, and are corrected inline). The
> ingest pre-pass (landed files → library) is described in
> [Ingestion (migrate pre-pass)](#ingestion-migrate-pre-pass) and verified
> end-to-end. Android behavior is still assumed, not measured.

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

- **One MTP thread** owns the device and interleaves enumerate + download as a
  **depth-first, drain-as-you-go walk**: at each folder, enumerate its children,
  **download the files right there** (files before descending), then recurse into
  subfolders. Downloads begin seconds in — there is **no full-tree pre-scan** — so
  a device yanked mid-run has already yielded everything shallow. This is
  deliberate: optimize for **getting bytes off the device** before it can vanish.
- **One worker thread** (or main) does off-device work: write bytes, verify,
  drive the UI — overlapping disk/verify with the next MTP read.

Interleaving buys **early bytes-off-device and a live denominator**, not raw
throughput (the USB pipe is the ~40 MB/s ceiling). The same walk drives discovery
*and* validation, re-run in a loop — see
[Import loop](#import-loop--download-validate-recover).

**Progress model.** Two moving counters — `downloaded` (numerator) and `found`
(denominator, still growing while enumeration continues) — plus bytes and a
rolling rate (MB/s, files/min). No hard ETA (the denominator is not yet final);
per-file byte progress for large videos.

## Device registry & naming

Each device has a stable serial (confirmed — [D1](#validation-results)). On connect,
look it up in a registry mapping `serial → friendly name`; on an unknown serial,
**prompt the user to name it** (so data is labeled by a human name, not a UUID).
The friendly name becomes a **folder name** under `.pix/local/import/`, so sanitize
it to a filesystem-safe form. (Android serials can be duplicate/garbage on cheap
devices — a known hazard, filed under the Android-unvalidated banner.)

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

**Import lands everything faithfully except an explicit non-media skip-list.**
Import is a dumb, byte-exact copier with **no format opinions** — it takes every
object under the camera-roll root as-is, *including unknown extensions* (so it
never silently drops something it doesn't recognise). The **one** exception is a
small, explicit denylist of known non-media companions, currently just **`.AAE`**
(Apple's non-destructive edit-instruction sidecars — 73 in the sample; not media,
never wanted). Accepted consequence: for a photo edited non-destructively (stored
as original + `.AAE`), skipping the `.AAE` means the **original** lands, not the
edited render. A `.__*`-style temp or the sync-client working dirs are not on a
phone, so nothing else needs filtering. Landed media is additionally run through a
**read-only validation probe** (a corruption tripwire, not a format gate — see
[Import loop](#import-loop--download-validate-recover)); it never modifies or blocks
a file, and formats pix can't parse are exempt (verified on transfer integrity
alone).

## Landing & tracking

**Landing:** `.pix/local/import/<friendly-name>/<device-relative-path>`, e.g.
`.pix/local/import/Jamies-iPhone/202605_a/IMG_7399.HEIC` (iOS month-bucket path,
per [C1](#validation-results)). Chosen because
`.pix/local` is **excluded from sync** → pending imports don't double-upload
(they upload once, later, when migrate moves them into the library). Pending
imports have no off-machine backup, which is acceptable: **the phone still holds
them** (import never deletes), so a lost `.pix/local` just means re-import.

**Sidecars live in a per-folder `.manifest/`, separate from the media — so
culling media never forgets an import.** Each landing folder gets a `.manifest/`
child holding one sidecar per media file at the **same leaf name**:

    .pix/local/import/Jamies-iPhone/202605_a/
    ├─ IMG_7399.HEIC              ← media (cull freely)
    ├─ IMG_7400.HEIC
    └─ .manifest/
       ├─ IMG_7399.HEIC.importinfo
       └─ IMG_7400.HEIC.importinfo

The **sidecar, not the media, is the durable pending skip-record.** Deleting
media *files* (everything in the folder except `.manifest/`) leaves the skip
records intact → those objects are **not** re-downloaded. Deleting the **whole
folder** — which takes its `.manifest/` too — is the deliberate "re-pull this
batch" gesture. `.manifest/` is kept **visible** on purpose: its presence is the
signal that the folder's imports are persisted. (Caveat: a `Ctrl+A → Delete`
*inside* a folder sweeps `.manifest/` too and causes a re-download — cull by
selecting the media files, not select-all.) The sibling path is deterministic, so
ingest finds a media file's sidecar at `<parent>/.manifest/<name>.importinfo`, and
folder-cleanup must treat a folder that still holds a `.manifest/` as **non-empty**.

**Path safety.** `ORIGINAL_FILE_NAME` from the device is untrusted: sanitize for
NTFS (strip/replace invalid chars, reserved names `CON`/`AUX`/…, trailing dots and
spaces), and open all paths `\\?\`-prefixed for length (per
[implementation.md](implementation.md)). Two distinct device objects can sanitize
to the **same** landing path — detect the collision and disambiguate with a short
PUID-derived suffix, so object B is never mistaken for object A's straggler. The
`.importinfo` sidecar is itself written **temp-then-rename**, so a crash mid-write
can't leave a corrupt sidecar that reads as `VERIFIED`.

**Concurrency.** `pix import` takes the library write lock (`.pix/local/lock`) like
every other write-mode op — a long import blocks migrate/organize for its duration.
Accepted: import is a deliberate, user-initiated session, and the lock is what
keeps a future ingest pre-pass from racing a running import over the same tree.

**Files are pristine** — never modified at import (validation is **read-only**).
Provenance rides in a **sidecar** per file, in the folder's `.manifest/` child
(`.manifest/<name>.importinfo`, YAML, matching the `.stashinfo` / `.errorinfo`
convention — see [Landing & tracking](#landing--tracking)), **written when the file
reaches `VERIFIED`** (see below), recording:

- device serial + friendly name
- stable object id (PUID, or the fallback composite — see below)
- device path (for `OriginalPath`)
- capture date, original filename, size

A second, **mutually-exclusive** marker — `<name>.importissue` (YAML) — records a
media-integrity dead-end (`state: needs-session` or `state: failed`, plus attempt
count and last error). A file has *either* `.importinfo` (good) *or* `.importissue`
(problem) *or* neither (downloaded, not yet probed) — never both.

**On-disk states.** A file lands at its **final** `./import/…` path the moment its
download completes; a marker's presence describes what happened next. All states are
self-describing on disk — which is exactly what the
[loop's](#import-loop--download-validate-recover) resume reads:

| State | On disk |
|-------|---------|
| downloading | temp name (transient marker `*.__*`), *not* the final name |
| `DOWNLOADED` (unprobed) | file at final path, **no marker** |
| `VERIFIED` | file at final path **+ `.importinfo`** |
| `needs-session` | file at final path **+ `.importissue` (`state: needs-session`)** |
| `failed` (terminal) | file at final path **+ `.importissue` (`state: failed`)** |

- Download streams to the temp name, checks `bytes == size`, atomic-renames to the
  final path, reads it back off disk == stream. The rename is the commit, so a
  partial download can never masquerade as complete.
- The **`.importinfo` write is the `DOWNLOADED → VERIFIED` commit**, made only after
  the local media-check passes. Because migrate's ingest needs the sidecar for
  provenance, **migrate only ever touches VERIFIED files** — an unprobed straggler
  or an `.importissue` file is invisible to it, no extra check.
- Resume: temp `*.__*` → discard + re-download; final file with `.importinfo` →
  skip; with `.importissue failed` → skip + report; with `.importissue needs-session`
  → retry once on this (fresh) session; with **no** marker → re-probe locally.
  Already-`VERIFIED` files are never re-touched.

**Migrate ingest → provenance becomes in-file.** Migrate's import-ingest pre-pass
(designed in [Ingestion](#ingestion-migrate-pre-pass)) reads the `.importinfo`
sidecar, writes the provenance **into the resulting library file** as pix tags, then
drops the sidecar (library stores only jpg/mp4, both embed XMP, so no library-side
sidecar). The two tags:

- `pix:ImportId` = `<serial>:<persistent-unique-id>` — the durable,
  convert-surviving skip key (survives HEIC→jpg per the CONVERT-preserves-metadata
  invariant). Uses the PUID, **not** the raw object id — see [I1](#validation-results).
- `pix:OriginalPath` = the **device** path, taken from the sidecar's `device_path`
  (frozen at import, *before* any merge — see [Ingestion](#ingestion-migrate-pre-pass)).

## Incremental import (skip already-imported)

The skip decision must happen **before** downloading, so the key must be
derivable from **cheap MTP metadata only** (never a content hash — that needs
the download we're avoiding):

- **Preferred key:** `PUID + size` — `WPD_OBJECT_PERSISTENT_UNIQUE_ID`
  **validated stable** on iOS across unplug/replug *and* a full reboot (146/146
  unchanged; the object id itself was also stable, but the PUID is the documented
  contract) — see [I1](#validation-results). **`+ size`** guards optimized-storage:
  if the phone once served a downscaled proxy (imported under that PUID) and later
  holds the full-res original, the size differs → the object is treated as new and
  re-imported rather than masked forever by the bare PUID. Still unvalidated on
  Android.
- **Fallback key:** `(filename, size, capture-date)` composite (all cheap object
  properties, all confirmed readable without download). Filename alone is unsafe —
  iPhones recycle `IMG_0001`, and non-camera filenames are randomized 4-letter
  stems (`FLAO4412`), so name collisions are real; size+date disambiguate.
  `capture-date` is normalised to a canonical ISO-8601 UTC string before it enters
  the key (iOS serves a `YYYY/MM/DD:…` string; Android may differ).

**Manifest = the union of two on-disk sources**, recomputed each run — there is no
separate ledger file (an earlier draft named a `.pix/local/cache.db` table; the
code derives it live instead, and the `.manifest/` split below is what makes the
pending half durable):

- **committed** — `pix:ImportId` tags on library jpg/mp4 files.
- **pending** — `.importinfo` sidecars in the per-folder `.manifest/` subfolders
  under `.pix/local/import/` (see [Landing & tracking](#landing--tracking)). Because
  the sidecar isn't in the media you cull, "delete the photos, keep the folder" is a
  durable "don't re-download."

**Per-object decision (one unified procedure).** For each enumerated device object:

1. key (`PUID+size`) in the manifest (pending or committed) → **skip**;
1b. else `(filename.lower(), size)` in the device's **import-seed manifest** (see
   below) → **skip** (`seed-skip`);
2. else `.importinfo` present at its path → already `VERIFIED` → **skip**;
3. else `.importissue` present:
   - `state: failed` → terminal → **skip + report** (clearable by deleting the marker);
   - `state: needs-session` → **fresh-session retry** — re-download + media-check
     once; pass → `VERIFIED`, fail → promote to `state: failed`;
4. else a landing file exists at its path **without** any marker → it's a
   `DOWNLOADED` straggler from a prior run → cheap **size pre-check** (enumerated
   `SIZE` vs the landed file): mismatch → the object changed on-device (edit,
   optimized-storage rehydration) → **re-download fresh**; match → **re-probe
   locally** (media-check → `VERIFIED`, or enter the recovery ladder) — don't re-pull
   as new;
5. else → **download**.

Because the manifest is a cache, losing `.pix/local` is safe: the committed half
regenerates from library tags; the pending half is re-downloaded from the phone
(they land dated-old and are easy to find/purge).

**dedupe is the correctness backstop.** The manifest is purely a download-skip
optimization; anything that slips through and is re-downloaded gets caught by
`dedupe`'s content hash after the fact. So the manifest may be best-effort.

**Delete semantics** (consequence of the above):

- Delete **media files** in a landing folder (leaving its `.manifest/` sidecars) →
  those objects stay skipped, **not** re-downloaded.
- Delete a **whole landing folder** (taking its `.manifest/` too) → the deliberate
  **re-pull** gesture; those objects re-download next run.
- Delete a file **from the library** after it migrated in (so it carried a
  `pix:ImportId`, and its pending sidecar was already consumed at ingest) → the
  skip record went with it, so it **re-downloads** next run (dedupe can't backstop
  what's no longer in the library). This post-migrate case is the one residual gap
  the `.manifest/` split doesn't cover; a durable committed-side ledger could close
  it later, but the common cull-before-migrate flow is now safe.

### Import-seed manifests (transitional, from the deprecated external MTP tool)

A prior MTP tool tracked what it had already pulled in per-device manifest JSONs.
To avoid re-downloading everything on the first pix imports, those lists **seed**
the skip set. They are a **one-time bridge**, not a permanent feature:

- **No CLI.** Normalized manifests are placed by hand at
  `.pix/import-manifests/manifest.<friendly>.json` (durable/synced tier — *not*
  `.pix/local`, since they're the only copy and not regenerable). Canonical schema:
  `{"friendly", "source", "device_ids", "files": [[name, size], …]}`. The two
  legacy on-disk formats (a `{Version,DevicePath,LastSeen}` dict, and a
  `path → "rel-size"` string map) are normalized **once, out of band**; pix reads
  only the canonical form.
- **Attribution = friendly name from the filename.** `manifest.james.json` seeds
  the device whose friendly name resolves to `james`. The legacy `device_ids`
  aren't usable for matching (VID/PID only; two different iPhones share
  `05ac:12a8`), so the friendly name is the sole bridge. A mismatch (device named
  differently) is a silent no-op → import **reports the seed-skip count** so a
  naming mismatch is visible.
- **Match key = `(filename.lower(), size)`.** These lists predate this system and
  carry **no PUID**, so they can't ride the primary `PUID+size` key — the loop
  checks this fingerprint as a **separate secondary index**, scoped to the one
  matching device. `(name, size)` is near-unique in practice (0/0/2 internal
  collisions across the three real manifests). Accepted risk: unlike an
  over-download (which `dedupe` backstops), a **false seed-skip is a permanent
  miss** — but a name+exact-size collision between genuinely different photos is
  negligible.
- **Lifecycle.** Delete a device's `manifest.<friendly>.json` once its phone-side
  copies are gone (the seed is then worthless). When the folder exists but holds
  **no** `manifest.*.json`, the feature is spent → import prints a **deprecation
  warning** to remove the whole seed path (loader, secondary check, this section).

## Import loop — download, validate, recover

Verification here is **device→disk only** and asks one thing: *did an intact,
readable media file land?* It is not about migrate, the library, or the manifest.
There are **two independent failure axes**, handled separately:

1. **Transfer integrity** — did the bytes arrive intact? Established cheaply *at
   download time* (size + disk read-back), **no second transfer**. USB already
   CRC-checks and retries every packet and reads are deterministic ([V2](#validation-results)),
   so out-of-order / dropped packets are handled below MTP — a byte-length-correct
   read that also reads back off disk is trusted.
2. **Media integrity** — do the landed bytes actually parse as the media they
   claim to be? Checked by a **local, read-only media probe**. Only a *hard*
   failure triggers the (rare) recovery ladder that re-transfers.

**Per-object state:** `NEW → DOWNLOADED → VERIFIED`, plus two **persisted**
media-integrity dead-ends (`needs-session`, `failed`) and a transient run-state
`FAILED` for transfer errors.

### Download (`NEW → DOWNLOADED`) — transfer integrity
Stream to a temp name (`*.__*`) → check `bytes-read == MTP-reported size`
(truncation) → atomic-rename to the **final** path → read it back off disk and
confirm it equals the stream (bad write). The rename is the commit, so a partial
can never masquerade as complete. **No sidecar yet.** A transfer error here (size
mismatch, read-back failure, a device read that throws) increments a per-object
attempt counter and, at **N = 3**, marks the object **`FAILED`** — *run-state
only, not persisted* — so one flaky read neither spins the run nor aborts the
others; a later run re-attempts (the fault may have cleared).

### Validate (`DOWNLOADED → VERIFIED`) — media integrity, no re-transfer on the happy path
Immediately after a file lands, run a **local media-check** against the on-disk
bytes — no MTP:

- **Images** (`.jpg/.jpeg/.heic/.heif/.png/.gif/.tif/.tiff/.webp`): Pillow
  (`pillow-heif` registered for HEIC) opens and loads the image; an exception is a
  hard fail.
- **Video** (`.mov/.mp4/.m4v/…`): `ffprobe -v error -show_format -show_streams`;
  a nonzero exit or emitted error is a hard fail.
- **Everything else** (unknown / non-media extensions, which import lands
  faithfully): **no validator applies → verified on transfer integrity alone.**
  Import keeps *no* format opinion on formats it can't parse.

**Hard errors only** — warnings pass. The probe is a **corruption tripwire, not a
conformance gate**: the point is to keep re-transfers vanishingly rare, so that
when one does happen it is a real signal (visible to the operator), not noise. All
validators are **already pix dependencies** (Pillow + pillow-heif; ffprobe via
`convert.py`) — the import stage gains wiring, not a dependency.

A **clean** probe → write the `.importinfo` sidecar (the `VERIFIED` commit; see
[Landing](#landing--tracking)). **That is the entire happy path: one download, a
local probe, done.**

### Recovery ladder (media-check hard-fail only)
A hard media-check failure means *bytes landed at the right length but don't
parse.* Since reads are deterministic, the cause is usually a genuinely corrupt
source file or a wedged MTP session — so recovery escalates the **freshness of
the transfer**. Every step is **logged to the console *and* to a durable
`.pix/import-verify.log`** (one line per event), so the operator sees it happen
and can judge "once in a blue moon vs. a lot" across runs:

1. **Same-session re-download (×1).** Re-pull on the current session and re-probe.
   Pass → `VERIFIED`. Catches a one-off transfer hiccup.
2. **`needs-session` (persisted).** If the immediate re-download still won't parse,
   drop a `.importissue` marker (`state: needs-session`) beside the file and move
   on — the current MTP session can't fix it; a full USB reset might.
3. **Fresh-session retry (×1).** Reset detection is **operator-driven**:
   PUID/serial are stable across reconnect *and* reboot ([I1](#validation-results)/[D1](#validation-results)),
   so nothing in WPD proves a physical reconnect. So at the **start of every run**,
   if `needs-session` markers exist, print *"N file(s) need a device reconnect to
   retry — if you haven't unplugged/replugged since the last run, quit, reconnect,
   and re-run."* then retry each once (trusting the operator reconnected). Pass →
   `VERIFIED`.
4. **`failed` (persisted, terminal).** If the fresh-session retry still fails,
   rewrite the marker to `state: failed`: the fault is almost certainly the
   **source file itself**, not MTP — pix has exhausted what it can do. The file
   stays put (flagged, never `VERIFIED`, so migrate never touches it) and is listed
   in every run's end report for the user to resolve on the phone. **Clearable:**
   delete the `.importissue` marker (or the landed file) to force a fresh full
   attempt — e.g. after re-exporting the photo.

**Accepted tradeoff.** If the operator re-runs *without* actually reconnecting, a
file that truly needed a reset is marked `failed` prematurely. That is why `failed`
is clearable and always reported, never silently dropped.

### The loop — one dirty-loop of drain-as-you-go DFS
Because validation is now a **local** step folded into the download pass, there is
**one** traversal loop, not the old two (download-all, then re-download-all to
verify). A depth-first, files-before-folders walk (see
[Device access](#device-access-mtp--wpd)) handles each object by the
[per-object decision](#incremental-import-skip-already-imported); the walk repeats
until a full pass downloads **nothing new**. One mechanism, three jobs:

- **Discovery** — re-enumeration catches objects MTP reveals lazily (the first
  sweep can under-report).
- **Recovery** — the same-session re-download and the fresh-session `needs-session`
  retry ride inline on the walk.
- **Resume** — an interrupted run just re-runs; on-disk state is self-describing
  (see [Landing](#landing--tracking)): `.importinfo` → skip; `.importissue failed`
  → skip + report; `.importissue needs-session` → retry once; landed file with **no**
  marker → **re-probe** locally (not re-download — bytes were byte-checked when they
  landed); temp `*.__*` → discard + re-download.

**Termination is file-level.** New downloads make a pass "dirty" and re-loop for
discovery; transfer errors cap at N = 3 (→ transient `FAILED`); the media-recovery
ladder is bounded per file (one same-session retry, then one fresh-session retry,
then terminal `failed`). No object can spin the loop; a high global pass cap backs
this up.

**Cost.** The happy path is **one download per file** — the mandatory second
transfer of the old model is gone. Re-transfers happen only on a hard media-check
failure (rare by design). The only unavoidable repeated work is the cold
enumeration sweep for discovery (~27 obj/s cold, then warm-cached far faster —
[W3](#validation-results)). Progress names the current row's action
(download/verify/skip) and throttles console writes (~100 ms).

**Start-of-run sweep.** Before traversing, delete stale `*.__*` temps under the
landing root (interrupted partial downloads). Complete-but-unverified files (final
name, no marker) are **kept** and re-probed by the per-object procedure.

**Session drop** (unplug / sleep). On any mid-run device error a **liveness probe**
(re-read a device property) distinguishes a dropped session from a transient
per-object glitch: a live device → handle per the axis above (transfer error →
retry/cap; media fail → recovery ladder); a dropped session → **end the run
gracefully** (report progress, exit non-zero, no traceback). No in-run reconnect —
the user replugs and **re-runs**, resuming from on-disk state. Nothing is corrupted
or lost across the drop.

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

## CLI (implemented)

`pix import <path>` — resolve the library from `<path>`, take the library lock,
and land the selected device's camera roll under `.pix/local/import/<device>/`.

- **Device selection is registry-driven.** A lone connected device, or exactly
  one **known** device (previously named, in `.pix/devices.yaml`), is
  auto-selected. Otherwise — multiple known, or none known among several — pix
  **prompts a numbered picker** when interactive; non-interactively it exits
  listing the devices and asking for `--device <serial-or-name-substring>`.
  `--device` always overrides (matches serial/friendly/model substring).
- Unknown serial → **prompt** for a friendly name (TTY only; a stdin that can't
  be read falls back to the WPD name instead of hanging). `--name <friendly>`
  assigns it without prompting. Names persist in `.pix/devices.yaml` and are
  **reused on every future run** — a device is named once; `--name` also renames.
- `--dry-run` — enumerate and report new-vs-already-imported counts; download
  nothing (read-only: no lock, no run folder).
- Not built: `--to <path>` (relocate landing root); a per-run `--limit`.

## Deferred / out of scope

- **iCloud import** — a different access model entirely (API + auth, not USB);
  its own future spec. Explicitly out of scope here.
- **Phone-side deletion after import** — a future, explicitly gated action
  ("yes, NOW delete from the phone"), never implicit.
- **Android edge devices / non-DCIM sources**, screenshots, other albums.

## Ingestion (migrate pre-pass)

> **Status: implemented** (v0.1.193; `src/pix/ingest.py` + migrate plan-gen/apply
> hooks). Section IDs (ING-n) trace to the original Fable design review, now
> resolved.

The ingest pre-pass pulls **`VERIFIED`** landed files (those with an `.importinfo`
sidecar) from `.pix/local/import/` into the library, where the normal pipeline
(migrate → hash → dedupe → organize) takes over. It runs as a **pre-pass of
migrate**, mirroring the existing `errors/`/`stash/` restore passes — but with one
difference that drives the whole design: those passes restore a file to an *origin
that was already in the media tree*; import files have **no such origin**, so the
pre-pass genuinely **moves files into the tree** for the first time.

### Flow

```
.pix/local/import/james/Internal Storage/202605_a/IMG_7399.HEIC   (sync-excluded)
   │  ingest (migrate pre-pass): MOVE + sidecar rides along
   ▼
<root>/incoming/IMG_7399.HEIC   (+ IMG_7399.HEIC.importinfo)      (now synced library content)
   │  migrate: CONVERT (HEIC→jpg) + RENAME (canonical) + TAG (ImportId, OriginalPath, *Auto)
   │           then drop the sidecar
   ▼
<root>/incoming/2026-05-31_194431.jpg   (EventAuto = "james - 20260720")
   │  hash + dedupe: a re-import collapses against the existing library automatically
   ▼
organize → <root>/2026/james - 20260720/…   (empties incoming/, reaped when empty)
```

### Trigger & scope (ING-1)
A **migrate pre-pass**, active only when `<root>/incoming/` lies within the folder
being migrated (so `pix migrate 2014/` doesn't drag imports in; `pix sync <root>`
does). Runs under the library lock migrate already holds. No new CLI surface — the
post-import flow is just `pix import` then `pix sync <root>`.

### Destination — one flat `incoming/` (ING-1)
Ingest moves each `VERIFIED` file to a single flat `<root>/incoming/<name>`. This
is migrate's **one carve-out** from "files never move between folders" (`migrate.md`)
— confined to this move; migrate itself stays in-place afterward. Device month-bucket
folders are **dropped** (the original path is preserved in the tag, not the layout —
see below). Two device files that flatten to the same name are disambiguated with a
short suffix on the move; the collision is transient anyway, since migrate's canonical
RENAME renames everything by capture date immediately after.

**Cleanup.** A file's `.importinfo` sidecar is dropped once its tags commit (or
folded into `.errorinfo` on CONVERT failure — see crash windows). After draining,
ingest reaps the now-empty device folders under `.pix/local/import/` bottom-up (a
folder still holding an unprobed or `.importissue` file is not empty → kept); the
`import/` container itself stays. The emptied `incoming/` is reaped by organize's
own empty-folder sweep once it moves the files out.

### Provenance — captured at import, written at migrate (ING-4)
`OriginalPath`'s **value is frozen at import**, before any merge: the `.importinfo`
sidecar records `device_path` (full, e.g. `Internal Storage/202605_a/IMG_7399.HEIC`)
the moment the file verifies. The sidecar **travels with its file** into `incoming/`.
Migrate then writes the tags, valued from the sidecar:

- `pix:OriginalPath` = the sidecar's `device_path` — a **write-once override** of
  migrate's default ("first-migrate sets `OriginalPath` = current source path", which
  in `incoming/` would wrongly be the flattened location). Migrate still treats the
  file as a first-migrate and writes its `*Auto` baselines; only the `OriginalPath`
  *value* is overridden.
- `pix:ImportId` = `<serial>:<puid>` — the durable, convert-surviving skip key.

**Sequencing:** for a keep file (jpg/mp4) the tags are written by `RENAME+TAG`; for a
convert file (HEIC→jpg, MOV→mp4) by `CONVERT+RENAME+TAG` on the output (the
CONVERT-preserves-metadata invariant carries them across). The sidecar is dropped
**only after** the tags commit.

### Event — synthetic, per device+day (ING-2)
Imported files must **not** derive `EventAuto` from their transient folder
(`incoming/`, or the old month-bucket `202605_a` → `"a"`). Instead, when a file
carries an `.importinfo` sidecar, migrate sets:

    EventAuto = "<friendly> - <imported_at:YYYYMMDD>"      e.g. "james - 20260720"

`imported_at` is a field the `.importinfo` sidecar records at verify time; the
friendly name is used **verbatim** (lowercase stays lowercase). **Fallback:** a
pre-v0.1.193 sidecar (no `device_name`/`imported_at`) uses its `friendly` (WPD
name) for the device name and, lacking a date, sets the event to that name alone —
never a fabricated date, never a silent no-event. This groups a batch
into its own event so organize keeps it separate for review (delete junk, etc.). The
injection happens once, during the ingest migrate; after organize places the files
into `<root>/<year>/james - 20260720/`, a later migrate re-derives the same event
from the folder name (it round-trips). Because `{year}` comes from each file's own
capture date, a batch spanning years fans out across `2014/james - 20260720/`,
`2026/james - 20260720/`, … — by design (any template token before `{event}` does).

### Live Photo MOVs are dropped (ING-5)
A Live Photo is an image plus a short motion `.mov` sharing its stem. At ingest, a
`.mov` is **dropped** (not moved into the library) when it **both** (a) shares its
stem with a sibling image (`.heic/.heif/.jpg/.jpeg`) in the same import folder **and**
(b) has a duration ≤ **5 s** (read via ffprobe). Both conditions guard a real short
clip that happens to share a name. Dropped MOVs are **soft-dropped to the run folder**
(recoverable), not hard-deleted, and the phone still holds the original. Standalone
MOVs and long paired MOVs are ingested normally.

### Sidecars & unknowns in the walk (ING-3)
Only `VERIFIED` files (with `.importinfo`) are ingested; `.importissue`
(`needs-session`/`failed`) files **never** move into the tree. Inside `incoming/`,
migrate must treat `.importinfo` as a consumed companion (read it, then drop it),
never as a walk target — and neither `.importinfo` nor any stray non-policy extension
may trip migrate's unknown-extension fail-fast: ingest/migrate **skip-and-report**
unknowns rather than aborting.

### Crash windows (ING-1)

| Crash point | On replay |
|---|---|
| File moved to `incoming/`, sidecar not yet moved | Sidecar moves **first** (or file+sidecar as a unit); a file in `incoming/` without its sidecar is treated as a normal library file (no ImportId) — acceptable, dedupe backstops it. |
| Tags written, sidecar not yet dropped | File already carries `ImportId` → migrate sees provenance present, just drops the leftover sidecar (idempotent). |
| CONVERT fails after the move | File → `.pix/errors/`; the sidecar's `device_path`/`ImportId` **fold into `.errorinfo`** so provenance survives to the version-bump retry. Never a `.pix/errors/` entry silently missing its ImportId. |
| Mid-ingest (in neither manifest half) | The file is momentarily absent from both the pending (sidecar gone from `.pix/local/import`) and committed (tag not yet written) halves; a concurrent re-import would re-download it, and **dedupe backstops** the duplicate. |

### Manifest committed-half & delete gate (ING-6)
The manifest's **committed** half is the set of `pix:ImportId` tags across library
jpg/mp4 — rebuilt by scanning the library (cached in `cache.db`). This is the durable
skip that survives a `.pix/local` wipe. Consequences to fold into the specs:

- `implementation.md`'s "`.pix/local/` loss never costs library data" is **false while
  pending imports are the only local copy** — amend it: a lost `.pix/local` before
  ingest means re-importing from the phone (the phone is the backup), not data loss,
  but it is not "free."
- The future phone-side delete action gates on **ingested *and* synced**, never on
  `VERIFIED` alone — a `VERIFIED` file still living only in the sync-excluded
  `.pix/local` has no off-machine copy yet.
- `cache.db` loss becomes a **behavioral** change here (re-scan rebuilds the committed
  half; a since-deleted library file could re-import), not just a perf hit.

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
- **V2 (new) — CONFIRMED.** Reads are **deterministic**: the same 43 MB object read
  twice hashed identically, and the landed file read back off disk matched the
  stream. This underpins the recovery ladder's logic: because a same-session re-read
  reproduces bytes deterministically, media-check recovery escalates to a **fresh
  session** rather than trusting a same-session re-pull (see
  [Import loop](#import-loop--download-validate-recover)). **No device-side hash
  exists** — `GetSupportedProperties` returns count 0 on iOS and MTP has no checksum
  property, so any verification must hash bytes we read ourselves.

### Probe tooling
The validation probe (`comtypes`-based WPD walker: `list` / `report` / `snapshot`
/ `compare` / `w2` modes) is committed at **`tools/probe_wpd.py`** (comtypes is an
optional Windows-only `probe` dep group). It is the reproducible harness for
re-running these checks — notably **I1/D1 on Android** and **S2** — before/while
building.

## Cross-references

- [README.md](README.md#operations) — ops table (import is the new front-end op).
- [migrate.md](migrate.md) — the import-ingest pre-pass seam (sidecar → in-file
  tags); mirrors the existing `errors`/`stash` restore passes.
- [library.md](library.md) — `.pix/local/import/` landing; `pix:ImportId` /
  `pix:OriginalPath` provenance.
- [implementation.md](implementation.md#sync-client-interaction) — `.pix/local`
  is already sync-excluded, so pending imports don't sync.
- [tags.md](tags.md) — add `pix:ImportId` to the tag model + `metadata_filter`.
