# Import — `pix import`

> **Status: building the device→disk import only. Ingestion is deferred.** The
> WPD/iOS claims below were validated against a real iPhone (serial `M2DF33MY06`)
> via `comtypes` — see [Validation results](#validation-results); several original
> assumptions were **wrong** (notably no `DCIM/NNNAPPLE` over MTP) and are
> corrected inline. **Scope of the current build:** pull files off the device and
> land them verified under `.pix/local/import/` — nothing consumes them yet. The
> **migrate ingest seam** (landed files → library) is explicitly out of scope and
> tracked as open work in
> [Ingestion seam — issues to resolve](#ingestion-seam--issues-to-resolve-deferred).
> Android behavior is still assumed, not measured.

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
*and* verification, re-run in a loop — see
[Import loop](#import-loop--traverse-download-verify).

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

**Import lands *everything* faithfully — no filtering.** Import is a dumb,
byte-exact copier: it takes every object under the camera-roll root as-is and has
**no format opinions**. Deciding what is junk (e.g. `.AAE` edit sidecars — 73
measured in the sample) or unsupported is a downstream **ingestion/migrate**
policy, not import's job. This keeps import simple and true-to-source, and defers
the risk of silently dropping something (e.g. whether an `.AAE`'s edit is already
a separate rendered object — unconfirmed). See
[Ingestion seam — issues to resolve](#ingestion-seam--issues-to-resolve-deferred).

## Landing & tracking

**Landing:** `.pix/local/import/<friendly-name>/<device-relative-path>`, e.g.
`.pix/local/import/Jamies-iPhone/202605_a/IMG_7399.HEIC` (iOS month-bucket path,
per [C1](#validation-results)). Chosen because
`.pix/local` is **excluded from sync** → pending imports don't double-upload
(they upload once, later, when migrate moves them into the library). Pending
imports have no off-machine backup, which is acceptable: **the phone still holds
them** (import never deletes), so a lost `.pix/local` just means re-import.

**Files are pristine** — never modified at import. Provenance rides in a
**sidecar** per file: `<name>.importinfo` (YAML, matching the `.stashinfo` /
`.errorinfo` convention), **written when the file reaches `VERIFIED`** (see
below), recording:

- device serial + friendly name
- stable object id (PUID, or the fallback composite — see below)
- device path (for `OriginalPath`)
- capture date, original filename, size

**On-disk states.** A file lands at its **final** `./import/…` path the moment its
download completes; the `.importinfo` sidecar's **presence is the verified marker**
(written only at `VERIFIED`, never at download). The three states are
self-describing on disk — which is exactly what the [loop's](#import-loop--traverse-download-verify)
resume reads:

| State | On disk |
|-------|---------|
| downloading | temp name (transient marker `*.__*`), *not* the final name |
| `DOWNLOADED` (unverified) | file at final path, **no sidecar** |
| `VERIFIED` | file at final path **+ `.importinfo` sidecar** |

- Download streams to the temp name, checks `bytes == size`, atomic-renames to the
  final path, reads it back off disk == stream. The rename is the commit, so a
  partial download can never masquerade as complete.
- The **sidecar write is the `DOWNLOADED → VERIFIED` commit**. Because migrate's
  ingest needs the sidecar for provenance, **migrate only ever touches VERIFIED
  files** — an unverified straggler is invisible to it, no extra check.
- Resume: temp `*.__*` → discard + re-download; final file **without** sidecar →
  re-verify; final file **with** sidecar → skip. Already-verified files are never
  re-verified.

**Migrate ingest → provenance becomes in-file (DEFERRED — not built here).** The
intent: migrate's import-ingest pre-pass reads the `.importinfo` sidecar, writes
the provenance **into the resulting library file** as pix tags, then drops the
sidecar (library stores only jpg/mp4, both embed XMP, so no library-side sidecar).
The two intended tags:

- `pix:ImportId` = `<serial>:<persistent-unique-id>` — the durable,
  convert-surviving skip key (survives HEIC→jpg per the CONVERT-preserves-metadata
  invariant). Uses the PUID, **not** the raw object id — see [I1](#validation-results).
- `pix:OriginalPath` = the **device** path (from the sidecar).

**This whole seam is unbuilt and under-designed** — destination folder, sidecar→tag
sequencing, crash windows, and several cross-spec conflicts are open. It is
explicitly **out of scope for the current import build** and tracked in
[Ingestion seam — issues to resolve](#ingestion-seam--issues-to-resolve-deferred).
Until it exists, `pix import` lands verified files that nothing yet consumes — by
design.

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

## Import loop — traverse, download, verify

Verification here is **device→disk only**: did the bytes land intact? It is *not*
about migrate, the library, or the manifest, and **nothing is persisted** — the
per-object fingerprint (a content hash) is throwaway run state used only to
compare two reads. There is no device-side checksum to trust (MTP exposes none —
confirmed [V2](#validation-results)), and USB already CRC-checks and retries every
packet, so the strongest practical guarantee is: **size matches + two independent
downloads agree + the on-disk file reads back equal to what we received.**

**Per-object state:** `NEW → DOWNLOADED (unverified) → VERIFIED`, plus `FAILED`.

- **Download** (`NEW`→`DOWNLOADED`): stream to a temp name (`*.__*`) → check
  `bytes-read == MTP-reported size` (truncation) and a clean complete read → atomic
  rename to the **final** path → read it back off disk and confirm it equals the
  stream (bad write). **No sidecar yet.** Catches truncation and disk-write
  corruption; the rename is the commit, so a partial can't look complete.
- **Verify** (`DOWNLOADED`→`VERIFIED`): a **later traversal** re-downloads the
  object and compares it to the **already-landed file** — the disk file is read #1,
  the re-download read #2, so nothing is carried in memory between them. Match →
  **write the `.importinfo` sidecar** (the commit to `VERIFIED`, per
  [Landing](#landing--tracking)). Mismatch → the read was flaky; re-download to
  break the tie (two that agree win).

**The loop.** One traversal is the drain-as-you-go DFS above. A traversal is
**dirty** if it downloaded anything — a new pull *or* a verify re-pull. After a
full traversal, **if dirty, traverse again**; a fully clean traversal (nothing new
enumerated, nothing left to verify) ends the run — which is exactly "no new files
*and* everything verified," reached naturally. One mechanism, three jobs:

- **discovery** — re-enumerating catches objects MTP **lazily reveals** on a later
  pass (observed: the first sweep can under-report);
- **verification** — the second (and if needed third) independent read per object;
- **resume** — an interrupted run just re-traverses; on-disk state is
  self-describing (see [Landing](#landing--tracking)): a landed file **with** a
  sidecar is `VERIFIED` (skip), **without** one is `DOWNLOADED` (only re-verified,
  never re-pulled as new), and a temp `*.__*` is discarded. The expensive first
  download is never repeated, and verified files aren't re-verified.

**Termination is file-level, not loop-level.** Each object carries an attempt
counter; when it exceeds N (size mismatch, read-back failure, or two reads that
never agree) the object is marked `FAILED` → logged, skipped, reported at the end,
and **no longer counts as dirty**. So one genuinely-flaky photo can neither spin
the run forever nor abort the other files. (A high global loop cap can back this
up, but the per-file counter is what actually converges the loop.)

**Session drop** (unplug / sleep / re-enumerate) is not a failure tier of its own:
re-open the session and the loop resumes from current on-disk state.

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

## Ingestion seam — issues to resolve (deferred)

The import-ingest pre-pass (landed files → library) is **not being built yet**;
`pix import` currently lands verified files that nothing consumes. These must be
resolved before that seam is designed/built. IDs in parentheses are from the
Fable design review.

- **ING-1 — Design the ingest pre-pass properly (B1).** The "analogous to
  errors/stash restore" hand-wave doesn't hold. Specify at migrate's worked-example
  fidelity: the **destination folder** in the library (migrate is otherwise
  in-place — `migrate.md`: "Files never move between folders" — so this needs an
  explicit carve-out); the **sidecar→tag sequencing** (for HEIC, `pix:ImportId`/
  `OriginalPath` can only be written during the CONVERT+TAG action); the **crash
  table** for every window (file moved but sidecar not; tags written but sidecar
  not dropped; sidecar dropped but CONVERT fails → `.pix/errors/` with no ImportId);
  and the manifest consequence (a mid-ingest file is in neither pending nor
  committed half — dedupe backstops the re-download; state it).
- **ING-2 — `EventAuto` month-bucket corruption (B2).** `pix:OriginalPath` =
  `…/202605_a/IMG_7399.JPG`; `tags.md` EventAuto strips `^[\d\-_. ]+` from the
  parent folder → `202605_a` → `"a"`, so **every imported photo would get
  `EventAuto="a"` (or `"b"`)**. Fix in ingest (suppress folder-derived EventAuto
  for device paths) or in the heuristic (reject single-letter residues / recognise
  `YYYYMM_x` buckets). Decide before any device `OriginalPath` is written — it's
  write-once. (Also: does import even record the raw month-bucket path, or a
  normalised one? Affects this.)
- **ING-3 — `.importinfo` vs migrate's fail-fast (S6/B1).** `.importinfo` is not in
  `EXTENSION_POLICY`; if sidecars sit in the migrate-walked tree, the
  unknown-extension abort kills the run. And since import lands **everything**, any
  stray phone extension (esp. Android) hits the same fail-fast. Ingest needs a
  skip-and-report policy for unknowns and a defined home for sidecars.
- **ING-4 — `OriginalPath` write-once override (B1).** Migrate's first-migrate logic
  sets `OriginalPath = current source path` (`library.md`); import wants the
  **device** path. Specify the override; getting it wrong is permanent.
- **ING-5 — Live Photo pairing at rename (S7).** `IMG_xxxx.HEIC` + `IMG_xxxx.MOV`
  share a capture time; migrate's canonical rename + `_NNN` content-hash tiebreaker
  scrambles the pairing (the failure mode `.insv` got a carve-out for). Decide
  whether pairing is preserved or explicitly not.
- **ING-6 — Invariant + delete-gate wording (N2/N3).** `implementation.md` says
  `.pix/local/` loss "never [costs] library data" — untrue while pending imports
  are the only local copy; amend it. The deferred phone-deletion action must gate
  on **"ingested + synced"**, not merely `VERIFIED`. Define when the manifest's
  committed half (scan of `pix:ImportId`) is rebuilt, and note cache.db loss becomes
  a behavioural change (delete semantics), not just a perf hit.

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
  stream. This is the empirical basis for verify-by-re-download (see
  [Import loop](#import-loop--traverse-download-verify)). **No device-side hash
  exists** — `GetSupportedProperties` returns count 0 on iOS and MTP has no
  checksum property, so verification must hash bytes we read ourselves.

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
