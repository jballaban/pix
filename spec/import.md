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
Provenance rides in a **sidecar** per file: `<name>.importinfo` (YAML, matching the
`.stashinfo` / `.errorinfo` convention), **written when the file reaches
`VERIFIED`** (see below), recording:

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

**Manifest = a regenerable cache** (a table in `.pix/local/cache.db`), not
only-copy state. It's the union of two durable sources and rebuilds from them:

- **committed** — `pix:ImportId` tags on library jpg/mp4 files.
- **pending** — `.importinfo` sidecars in `.pix/local/import/`.

**Per-object decision (one unified procedure).** For each enumerated device object:

1. key (`PUID+size`) in the manifest (pending or committed) → **skip**;
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

**Delete semantics** (consequence of the above): delete a file from the library
and, while the manifest cache is intact, it is **not** re-imported. Only if the
cache is lost *and* regenerated does a since-deleted file re-download (rare;
findable by date).

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
  skip-and-report policy for unknowns and a defined home for sidecars. (Same for the
  `.importissue` marker — ingest must ignore it and never treat a `failed`/
  `needs-session` file as ready to migrate.)
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
