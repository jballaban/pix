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
construction rather than by machinery. See [§12](#12-what-this-deletes).

## 2. Tier layout

```
/pix/master/{device}_{datetime}/{filepath}_{filename}.{ext}       originals — sacred
/pix/master/{device}_{datetime}/{filepath}_{filename}.{ext}.xmp   metadata — the record
/pix/master/{device}_{datetime}/.import.jsonl                    download ledger — the skip record
/pix/render/{device}_{datetime}/{filepath}_{filename}.{ext}.{jpg|mp4}
/pix/thumb/{device}_{datetime}/{filepath}_{filename}.{ext}.jpg
/pix/preview/{device}_{datetime}/{filepath}_{filename}.{ext}.jpg
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
| previews | no | no | master/render |
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

**"Sacred" is about bytes, not about permanence.** A master file is never
converted, never tag-written, never renamed after landing — its content is
immutable. It *may* be deleted, deliberately, by the owner: deletion is the
explicit "I don't want this tracked" gesture, and it is a different act from a
tool silently destroying information during normalization, which is what
[§1](#1-why-sacred-originals-and-not-self-describing-files) was protecting
against.

Deletion **cascades**: the file's `.xmp` goes with it (lose the file, lose what
relates to it), and its render, thumbnail and distribution members are swept.
Reconciliation does this anyway, so orphans clean themselves up.

**The undo is the storage layer, not pix.** Btrfs snapshots and Hyper Backup
versions are a better safety net than pix's own run folders — more reliable, and
free. But they **expire**, where run folders persisted until manually pruned. A
deletion noticed six months later may be past the horizon, so retention wants
setting deliberately rather than by default.

**Seeded master is not pristine.** The existing ~2.3TB has already been through
the old pipeline — HEIC converted to JPG, video remuxed, originals soft-deleted
to run folders. Importing it gives the best copy that still exists, not the
original. Only imports made *after* this architecture lands are true originals.

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

A `tier` change is therefore a 2KB write, not a multi-gigabyte file rewrite.

### Nothing rebuildable lives in master

**Master holds only what cannot be recomputed**: the original bytes, and the
human decisions about them. Everything derivable lives outside it.

| Tier | Holds | Backed up |
|---|---|---|
| **master** | original bytes + a **decisions-only** sidecar | yes — none of it is recomputable |
| render / thumb | derived | no |
| **index** | all probed EXIF + a projection of the overrides | optional, as convenience |

So a sidecar is three fields — `tier`, `event`, and a date override.
Provenance needs no sidecar: `OriginalPath` is already embedded in the legacy
files, and new imports carry it in
[`.import.jsonl`](#9-ingest--the-desktop-cli).

An earlier draft had sidecars also cache the probed facts (capture date,
dimensions, duration, codec, hash) so the index could be rebuilt without opening
media files. **Rejected**: those facts are *already in the media file*, so
caching them in a sidecar duplicates recomputable data into the one tier that is
backed up forever. The "a copied folder is self-describing" property does not need
them — the EXIF travels inside the files themselves.

The cost of that rejection is that a full index rebuild must re-probe ~62k media
files, which on spinning disks behind an Atom is hours (MP4 is the bad case: the
`moov` atom can sit at the end of the file, so a 2.6GB `.insv` costs a seek to EOF
for a date). That is acceptable because full rebuilds are rare — corruption, a
schema change, moving the app — and because **the index can simply be backed up
too**, as a convenience rather than a dependency. ~62k rows is small; restoring it
turns hours of re-probing into copying a file back. That is the difference between
a cache you *may* back up and a record you *must*.

**In the steady state most files have no sidecar at all** — one exists only once a
human has decided something.

**The seeded library is not an exception**, because legacy decisions are already
*inside* the files:

```
[XMP]  DateAuto      : 2015-03-15-11:52:56
[XMP]  EventAuto     : a
[XMP]  OriginalPath  : G:\pix\raw\...
```

So **the index reads the `.xmp` if present and falls back to embedded `pix:*` tags
if not.** It is already opening those files to probe capture date, dimensions and
codec, so reading the legacy tags costs nothing. The first time a value is changed,
a sidecar is written and takes precedence from then on.

That deletes a migration step: [seeding](#14-seeding-the-existing-library) writes
no `.xmp` at all, and inherited events simply appear. (Many are poor — the old
`EventAuto` took them from device folder names, giving values like `a` — but
carrying them is right, since curation can fix them and discarding them cannot be
undone.)

**Two unrelated things are called "sidecar" in this design.** They share nothing
but the word:

| | written by | holds | lives |
|---|---|---|---|
| `.manifest/*.importinfo` | `import` ([§9](#9-ingest--the-desktop-cli)) | PUID, serial, device path, verify state | staging only; retired at upload |
| `.xmp` | **the app** | `tier`, `event`, date override | master, permanently |

Neither `import`, `upload` nor `process` ever writes an `.xmp`. A freshly imported
photo has no decisions attached to it, so it has no sidecar until someone makes
one.

It also makes review state fall out for free: **no sidecar means unreviewed**,
which is exactly what "`tier` absent = uncategorized"
([§7](#7-distributions)) already says. `tier: none` creates a sidecar, because
rejection *is* a decision.

**Facts, not interpretations** still holds, now as a rule for the index: it caches
the raw reading (`EXIF:DateTimeOriginal`), never the effective date after pix's
heuristics ran. Facts cannot go stale, because master files are immutable —
[§1](#1-why-sacred-originals-and-not-self-describing-files) paying off again.
Interpretations go stale whenever the logic changes, and persisting them would
resurrect the `_auto` re-derivation treadmill the old design had. Effective values
are computed live from facts plus decisions.

Standard XMP fields stay standard, so Lightroom and Bridge read any date
override; the pix-namespace properties they simply ignore.

### The index

The app needs a queryable index — you cannot scan master for every UI filter. It
is **SQLite, disposable, and never authoritative**: an aggregation over the probed
facts and the sidecars.

Storing the overrides centrally *instead* of in sidecars is the option to reject.
It recreates the single database this architecture exists to avoid, and breaks
"lose a folder, lose only that folder."

- **Authority order: sidecar first, index follows.** Write the sidecar; only on
  success update the index. Drift then only ever means "the index is behind,"
  which a rescan fixes — never "the record is wrong."
- **Staleness detection is the existing `(size, mtime_ns)` stat comparison**, the
  same key `cache.db` already uses.

This replaces `cache.db`, which was the same idea without a home: an aggregation
of probe results that the library had to be rescanned to rebuild.

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
  re-transcodes anything. (If non-destructive editing is ever built
  ([§15](#15-open-questions)), an *edit* change would invalidate a render — a tag
  change still would not.)
- Whether a render is needed is an **extension** question for images and a
  **codec** question for video: an `.mp4` containing HEVC still needs one.
- `.insv` can never have a meaningful render (a flat transcode yields
  dual-fisheye that nothing displays usefully) and therefore never gets one.

**Renders cover everything convertible, not just what gets delivered.** You
cannot decide whether a photo is worth keeping without looking at it, and the app
displays only JPG/MP4 — so a HEIC with no render is invisible at exactly the
moment you need to judge it. `pix process` renders anything that needs it,
regardless of whether it will ever reach a distribution.

Renders are disposable and excluded from backup.

## 6. Thumbnails and previews

A separate tier, one small JPEG per master file, ~20GB for the whole library,
disposable.

It exists because the app must present a grid of 100k+ items on hardware that
cannot afford to decode originals on demand (see [§10](#10-hardware)). It is also
what makes `.insv` **taggable without being viewable**: a 360 clip gets a
thumbnail extracted from its embedded LRV proxy — a single-frame extract, not a
transcode — appears in the app, can be assigned an event, and is then findable
later so it can be opened in Insta360 Studio. The app is a **catalogue** for
material it cannot display.

**Previews are a second derived size, and curation needs them.** A thumbnail is
for grids; you cannot tell sharp from soft at 200px, and judging is the entire
point of [curation](#8-the-app). Serving the full render instead means pushing
several MB per photo off an Atom while someone pages through hundreds. So a
preview tier sits between them. Same rules: derived, disposable, never backed up.

Built as `pix process` (`nas/derive.py`):

| | |
|---|---|
| thumb | 400px long edge, JPEG q82, ~20GB for the library |
| preview | 1600px long edge, JPEG q82, ~20GB |
| video poster | one frame at **10% of duration** |

The frame offset matters: first frames are routinely black or motion-blurred, and
a little way in is almost always representative. `ffmpeg -ss` is placed *before*
`-i` so it seeks by keyframe rather than decoding up to the offset — the
difference between minutes and milliseconds on a long clip.

**EXIF orientation is applied, not carried.** A derived JPEG has nowhere to carry
it, and a sideways thumbnail is worse than useless in a grid being scanned for one
photo.

**Renders are deliberately not built yet.** Master is seeded from the
already-normalised library ([§14](#14-seeding-the-existing-library)), so it is JPG
and MP4 throughout and almost nothing needs converting. The exceptions — legacy
HEVC clips needing H.264 for delivery — drag in codec probing and the encode path
for nearly no work today, so they wait until there is something to look at.

Three independent properties, rather than a file-state enum:

| Property | Condition |
|---|---|
| **taggable** | has a thumbnail — i.e. everything |
| **viewable in-app** | is JPG/MP4, or has a render |
| **deliverable** | passes the distribution's `extensions` allowlist |

`.insv` is taggable, not viewable, not deliverable — and needs no special-casing
to get there.

## 7. Distributions

### The curation scale

Curation is one decision per file, carried by **`tier`**:

| State | `tier` | Meaning |
|---|---|---|
| uncategorized | *absent* | not yet reviewed |
| not good enough | `none` | reviewed, **kept forever**, never delivered |
| photo-app worthy | `photo` | available to the family |
| top | `top` | the handful you show when you show a few |

Values are **ordered** — `top` implies `photo` — which matches the workflow:
curate an event down to what is worth showing, then curate that down to the best
ten.

**Rejection must be a positive mark, not an absence.** If "not good enough" were
just "left untagged," you could never tell what you had already been through from
what you had not — and on hundreds of photos per event that is the difference
between finishing a curation pass and repeating it. `tier: none` is also distinct
from [deletion](#3-master), which removes the file entirely.

**`tier` does not need to be a standard field, and that is the point.** Nothing
downstream reads it — membership is expressed by which tree a file physically
sits in, so the tag only drives pix's reconcile.

**There is no `rating`.** An earlier draft split "how good is this photo" from
"where should it go," keeping `rating` as an independent 0-5 quality mark. It was
**dropped**: `tier` already encodes quality (`photo` = good enough for the family,
`top` = best of the event), so the axes were never orthogonal in practice — and a
second decision per file, across 61,846 of them
([§14](#14-seeding-the-existing-library)), is a bad trade against the only work
that actually matters. Curation is one gesture per photo.

Nothing is lost in the delivery copies either: tier is recoverable from which tree
a file sits in, event from the folder path, date from EXIF. The
sidecars-are-lost safety net survives without it.

### The trees

Two standing distributions on the NAS, kept continuously reconciled:

| Tree | Filter | For |
|---|---|---|
| `/photo` | `tier:photo,top` | Synology Photos — the **primary** way the family views everything |
| top-10 | `tier:top` | dumb consumers: a TV that plays a folder, a book service that takes an upload |

The top tree duplicates a subset of `/photo`, which is fine — it is ~10 per event.
With `rating` gone there is no curation signal *inside* a delivery copy to filter
on, so membership can only be expressed by which tree a file sits in. That makes
the second tree necessary rather than merely convenient: it is the only way a
consumer gets only the handful.


**Everything else is ad-hoc, from the desktop.** A people-grouped set for LLM
training, a one-off book export — `pix export` over SMB against master, writing to
a local target, leaving nothing standing on the NAS. Standing trees need a
manifest, drift detection and continuous reconciliation; a one-off needs none of
it. (`{person}` depends on face detection, which is deferred and unbuilt.)

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
  ever delivered still holds its event and date, and its tier is recoverable from
  which tree it sits in.
- **A date override must rewrite the EXIF capture date** in the copy, not merely
  sit in an XMP field — otherwise a corrected date is invisible to the consumer
  that sorts by it.
- **Canonical naming happens here and only here.** Master keeps provenance names;
  the copy gets a canonical name. Because a name is assigned when a copy is
  created and never changed afterward, the
  [stable-collision-suffix problem](roadmap.md) never arises.
- **Distributions are one-way, and that is enforced rather than assumed.** Give
  the family **read-only** DSM permissions on `/photo` and write access only to
  the app's account. Synology Photos stays fully usable — albums, favorites,
  people and its own tags all live in its database, never in the media — but it
  cannot delete or add files. `@eaDir` thumbnail folders still appear, written by
  the indexer as system; the reconcile already skips them as NAS artifacts.
- **Drift reports rather than stops.** `export.md` hard-stops the whole run on
  unexplained drift, which was right when the target might be a hand-curated
  folder. Over a regenerable tree with an untouchable master it is too aggressive:
  a stray file should not stop `/photo` reconciling. Missing gets restored from
  master, modified gets overwritten, **foreign gets reported and never touched** —
  a photo someone dropped in exists nowhere else, so deleting it would destroy
  their only copy.
- **Path churn breaks Synology Photos albums.** Renaming an event moves the copies
  within the tree, and any album pointing at the old paths loses those entries.
  Nothing pix can prevent — worth knowing before renaming an event that albums
  hang off.

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

## 8. The app

Runs in Container Manager on the NAS. It **owns the archive**: the only process
that writes sidecars, deletes master files, and reconciles distributions. It never
transcodes ([§10](#10-hardware)).

### Who writes

The family curates, not just the owner — tagging and ranking are the whole point
of the app. Conflicts are **last-write-wins**, and that is sufficient: one process
serializes every sidecar write, so two people cannot corrupt an `.xmp`. Multiple
writers create a policy question, never an integrity one — which is why none of
the locking, merging or checkout machinery the old architecture needed has a
successor here.

**No attribution.** That a value was set matters; who set it does not. Nothing in
the data model carries a user.

### Access

App-managed accounts for the household. Passwords stored **hashed, never
plaintext** — a credentials file on a share reachable over SMB is exactly how a
reused password leaks.

**Synology SSO Server** (a DSM 7 package that acts as an OIDC provider) is the
upgrade path if DSM accounts should become the login. More setup than two users
justify on day one; revisit if access widens.

### Curation

Curation is the app's reason to exist. The library is 61,846 files at 0.15%
curated ([§14](#14-seeding-the-existing-library)), and whether that ever gets
worked through is a property of the interface, not of the archive.

**61,846 is not the real number, if it is built right.** Three levers, each
collapsing an order of magnitude:

- **Events are proposed, not assigned.** Cluster by capture-time gaps — a day's
  break is almost always a boundary. Naming groups is hundreds of decisions, not
  62k.
- **Promote keepers; do not reject rejects.** An event goes from several hundred
  photos to 20-50, so positive selection is roughly a tenth of the gestures, and
  the common case for any given photo is that it is never touched.
- **Near-duplicates collapse.** Most of a burst is one photo shot eight times.
  Group them, pick one, the rest follow. This is the payoff for image perceptual
  hashing ([roadmap.md](roadmap.md)), still unbuilt; video fingerprinting already
  exists.

**Finishing an event writes `tier: none` for everything unpromoted.** Positive
selection has one hole — if you never touch the rejects you cannot distinguish
*reviewed and rejected* from *not yet reviewed*, which is exactly what `tier:
none` is for ([§7](#7-distributions)). Closing it at the UI layer rather than the
data layer keeps the model per-file and unchanged: one click, a few hundred 2KB
sidecar writes in the background, no event-level state, and a real progress
reading — *2015: 14 of 22 events reviewed*.

#### Ranges select; they do not rule

Defining "France Trip, Dec 1-3" and having it **write `event` into every sidecar
in that range** is the gesture that makes event assignment cheap. The range is a
*selection*, evaluated once — not a stored rule that decides membership on the
fly.

Persisted ranges were considered. Their real advantage is that a rule keeps
applying: a spouse's phone imported a week later, a forgotten camera, a corrected
capture date — all would join the event with no work. That is a genuine recurring
benefit in a multi-device household.

**Rejected because a rule is a new *kind* of state.** Everything else here is
per-file sidecars: independently written, last-write-wins, no coordination, and
losing one costs one file. A rules document is shared and mutable — it reintroduces
concurrent editing between two curators, and a small single point of failure whose
loss re-orphans the library's grouping. That is precisely what this architecture
spent its design eliminating, reappearing at smaller scale. It also needs two
mechanisms where one would do, since ranges require per-file include/exclude
overrides layered on top, and "why is this photo in France Trip?" stops being
*read the tag* and becomes *evaluate a rule and its exceptions*.

As a selection gesture, the exceptions that motivated the doubt stop being
awkward. A stray WhatsApp image inside the range is just edited afterwards — the
same mechanism, not a second one. A plane photo from the day before needs no
"include from outside range" concept; you simply also select it. Selection is
selection.

**The auto-join benefit is recovered without rules**: at import, the app proposes
an event from **time-neighbours** — *these 40 new photos fall between two files
tagged France Trip; assign them?* Same practical result as a persisted range, as a
proposal rather than stored state, and it handles what rules cannot: a second
camera whose clock is off by hours still sits among its neighbours.

#### Three passes

| Pass | Unit | Gestures |
|---|---|---|
| 1 — events | proposed clusters | name / merge / split — hundreds in total |
| 2 — keep | one event at a time | promote to `photo`; finishing writes the rest to `none` |
| 3 — top | the promoted 20-50 | pick ~10 |

Three cheap passes with clear finish conditions, rather than one infinite browse.
Explorer failed at this precisely because it was a navigator with no notion of
*done*.

Seeded events are the starting point for pass 1, not scaffolding to discard. Most
inherited events are meaningful; some are junk (the old `EventAuto` took values
like `a` from device folder names). Both are fixed in the UI with the same
gestures, which is why seeding preserves them all
([§14](#14-seeding-the-existing-library)).

### The write queue

Exists for UI latency, not for cost. A `tier` change is a 2KB sidecar write, but
propagating it into the distribution copies is heavier, so the click returns
immediately and the reconcile follows behind.

## 9. Ingest — the desktop CLI

Ingest is the one part that stays a CLI on the Windows desktop, because a phone
is a USB/MTP device attached to a specific machine.

**There is no library root and no `.pix/` folder.** Nothing walks up looking for
scaffolding; `pix init` and per-library config are gone. On the desktop `pix` is a
**stateless global tool** — every command is a pure function of paths, and the only
state that exists anywhere is a per-machine config holding two of them.

| Command | Does |
|---|---|
| `pix import device` | interactive; auto-selects a single **known** device, else lists and prompts |
| `pix import folder <source> --name <n>` | a folder (SD card, shared folder, the legacy library) |
| `pix upload` | every pending staging folder → master; appends the ledger; clears staging |
| `pix process` | master → thumbnails, previews, renders for anything missing them |

**Transitional note.** This is built as a fresh module (`src/pix/nas/`) behind a
second console script — `pix2` — so the existing CLI keeps working untouched until
seeding is proven. It reuses what [§13](#13-what-survives) lists rather than
duplicating it. When the old architecture is amputated, `nas/` is promoted to
`src/pix/` and the second entry point disappears, so the temporary name never
outlives its purpose.

**No config, and no library root.** Paths are build constants in one module, the
way `EXTENSION_POLICY` already is:

| | |
|---|---|
| `G:\pix2\{name}\` | staging |
| `\\nas\pix2` | master |

**Staging lives on `G:`, and that is forced rather than arbitrary.** A folder
import hardlinks, and hardlinks cannot cross volumes — so staging must share a
volume with the legacy library at `G:\pix`, or seeding silently falls back to
real copies. It cannot afford to: measured, the 2022 folder alone is **946GB** and
2023 is **816GB**, against 819GB free on `F:`. A single year would not fit.

It also has to sit *outside* the Synology Drive sync scope, or staging would
upload itself through the sync client. Drive syncs per top-level folder —
`.SynologyWorkingDirectory` is present in `G:\pix` and `G:\photo` but not at
`G:\` — so the sibling folder `G:\pix2`, named to match the tool, is
outside every scope.

**No dry-runs and no run folders**, because nothing here is destructive — import
hardlinks, upload copies, process writes derived trees. The **one** destructive
step is clearing staging after upload, and it is gated per-folder on verification
(every file present at master at matching size), not on the run completing. A
folder that fails verification keeps its staging and says so. That single step is
the entire risk surface, which is what buys the loss of the plan/apply ceremony.

`import` and `upload` are separate on purpose: "no time, just get it off the phone"
has to be a complete gesture on its own.

**`import` has two source adapters, as subcommands.** They take genuinely
different arguments — a device import has no path and may need to *select* among
several connected phones, while a folder import has a path and nothing else — so
inferring the source from a string would be fragile, and an MTP device is not
addressable as a path at all. `upload` stays one code path regardless, which is
what makes [seeding](#14-seeding-the-existing-library) a normal upload rather than
a throwaway migration tool.

A folder source has no device to interrogate, so it takes `--name`; identity is
`(relative path, size)` rather than `PUID + size`, which keeps the run resumable
and idempotent. **Both adapters are permanent** — phones are MTP, but SD cards,
shared folders and anything that mounts as a drive letter are folder imports.

`--name` becomes the `{device}` component of the master folder, so a phone gives
`Jamies-iPhone_{upload-time}` and seeding gives `legacy_2015_{upload-time}`.

**Device selection already works this way** (`importer.py`): a `--device`
substring override, auto-select when exactly **one known** device is connected,
otherwise list and prompt; a new serial is named once and remembered, with
collision handling if two serials want the same name. It carries over unchanged
apart from where the registry lives — see below.

**A folder import hardlinks; it does not copy.** Source and import folder are on
one NTFS volume, so links are instant and free — and necessary, since there is
nowhere locally with room to duplicate a 2.5TB library. Every downstream
behaviour is unchanged: culling deletes the link while the `.manifest/` sidecar
survives as the durable skip record, deleting a whole folder is still the
"redo this batch" gesture, and `upload` reads through the link none the wiser.

**Staging nests under a source tag**, a flattening of the source's absolute path
(`G:\pix\2001` becomes `G_pix_2001`), and `rel` is relative to the *staging*
root so it carries that prefix. Both collisions this prevents are real: importing
two library years under one name would otherwise drop both `Australia Hockey/`
trees into the same place, **and** give them one skip key — so the second year
would be silently dropped as already-seen. It also means the flattened master
name is fully self-describing: `G_pix_2001_Australia Hockey_x.jpg`.

**Transport is SMB, not an app API.** An upload is potentially hundreds of GB, and
pushing that through a Python app on a 4-core Atom would be far slower than the
NAS's own Samba. It also keeps ingest working while the app is down or unbuilt —
which is the *app is optional* property doing real work. An HTTP endpoint for the
manifest query (one request instead of a few hundred small reads) is a later
optimization, and the only thing that would make off-LAN ingest possible.

### Imports accumulate; uploads create folders

The import folder is a plain per-machine working folder — not library-scoped —
holding one tree per device with the device-relative paths preserved, and a
per-folder `.manifest/` child exactly as [import.md](import.md#landing--tracking)
already specifies.

Successive `pix import` runs **append to the same tree**. Nothing is
re-downloaded, because the skip check consults both the local `.manifest/`
sidecars and master's `.import.jsonl` ledgers.

`pix upload` then creates **one** master folder per upload —
`{device}_{datetime}` where the datetime is the **upload** time, not an import
time. So an import that was interrupted, resumed a day later, and uploaded on the
third day produces a single folder holding all of it. A master folder means
"this is what landed on this date," which is the unit you would delete to re-pull.

Resuming an interrupted upload reuses the existing folder, skipping what is
already there by name and size.

### The NAS is required, and import fails without it

The skip manifest's **committed half lives on the NAS** (the ledgers), so `import`
has to read it before it can be incremental.

**Collecting it is cheap, because of the header.** List master, read the *first
line* of each `.import.jsonl` to learn its device, then read the bodies of only the
folders matching that serial. A phone with 20,000 photos across fifty upload
folders is a few MB of JSONL.

**Read it fresh every run — do not cache it locally.** A stale cache is worse than
none, because its failure mode is silently re-downloading.

**If the NAS is unreachable, `import` stops** — and the reason is not caution.
Without the committed half it is not a degraded operation, it is a *different*
one: "pull new photos" becomes "re-pull the phone's entire history," which is tens
of thousands of redundant MTP downloads that then upload into master as duplicates
nothing removes on its own, since [dedupe no longer deletes](#13-what-survives).
Failing is strictly better. **Check reachability before touching the device**, not
halfway through enumerating it.

This differs from the [device registry](#the-ledger), which degrades gracefully: a
missing friendly name means a prompt, a missing manifest means corrupting the
archive. One is convenience, the other is correctness.

(If offline import ever becomes a real need — pulling a phone while travelling —
the answer is a local cache of the committed manifest, accepting staleness when
another machine uploads. Speculative while imports happen at the desk.)

### Cancel and resume

**Every command is cancel-and-resume safe**, which matters because a long import is
routinely stopped and continued another day.

- **`import` already handles it.** [import.md](import.md)'s per-object ladder treats
  a landed file with no marker as a straggler from a cancelled run: a cheap size
  pre-check against the source decides re-download (the object changed on the
  device — an edit, or optimized-storage rehydration) versus re-probe locally (it
  did not). Sidecars are written temp-then-rename, so a kill mid-write cannot leave
  a corrupt sidecar that reads as `VERIFIED`.
- **`upload` needs two things.** Copy to a **marker-named temp and rename into
  place** — the `*.__*` convention [`export`](#7-distributions) already uses — because
  otherwise a truncated file at master looks real to a name-and-size check, and the
  marker pattern is already sync-excluded. And **read the existing ledger on
  resume**, skipping objects already recorded, so `.import.jsonl` gains no duplicate
  lines.
- **`process` is resumable by construction**: it generates what is missing, and
  missing is recomputed every run, so there is no state to corrupt.

A cancelled *folder* import costs almost nothing to redo, since staging is
hardlinks. It is `upload` where resumability earns its keep — that is the
multi-hour one.

### The ledger

`{device}_{datetime}/.import.jsonl` is appended **during** upload (not written at
the end, so a crash leaves a consistent partial record). Its **first line is a
header** describing the source, and every line after it is one object pulled in
that batch. Each entry carries its own `root`, because one staging folder can
accumulate from several sources and the header's list is only a cheap pre-filter:

```json
{"device_name":"Jamies-iPhone","serial":"...","source":"device","uploaded":"..."}
{"name":"legacy_2015","source":"folder","source_path":"G:\\pix\\2015","uploaded":"..."}
```

**The header makes the known-device registry derivable, so there is no registry
file.** Known devices are the union of the serials in master's ledger headers and
those in pending staging's `.manifest/*.importinfo` sidecars (which already record
`serial` and `device_name`) — so a phone imported but not yet uploaded is still
known. A stored `devices.yaml` could drift from reality; a derived one cannot,
which is the same rule the index follows.

The cost is that resolving it needs the NAS reachable: a listing plus one small
read per folder, cached for the run. Offline, it falls back to the local staging
folder names, so a device with pending staging still resolves and anything else
prompts for a name exactly as a fresh device would.

Per-object lines carry:

> PUID, device path, name, size, capture date, outcome — `kept` / `culled` / `failed`

This is the **committed half** of the skip manifest, replacing the `pix:ImportId`
tags that used to carry it — those lived inside library files, and nothing is
written to master files any more.

It has to record objects *downloaded*, not files *present*, because a culled file
has no media, no sidecar, and nothing else in master to attach a record to.
`failed` is recorded as retryable, never as a skip; the existing `needs-session`
vs terminal `failed` distinction ([import.md](import.md)) carries over unchanged.

Device-side identity stays **`PUID + size`**, with `(filename.lower(), size)` as
the seed fallback — iPhones recycle `IMG_0001`, so a name alone is not safe across
years.

**After a successful upload the import folder is cleared** — media *and*
`.manifest/`. The durable record has moved to the ledger, so nothing needs to stay
behind pinning the folder, and the import folder never grows without bound.

**Culling before upload is an optimization, not a decision you have to get
right.** Deleting media from the import folder leaves its `.manifest/` sidecar
intact, which becomes a `culled` ledger line and a permanent "don't re-download" —
saving the bandwidth and the space. But you can equally bulk-upload everything
without thinking and curate later in the app, because master files
[can be deleted after the fact](#3-master). The desktop cull is there for when you
already know; it is not the last chance.

### `pix process` runs against master

Not against the local import folder. Running it locally before upload would read
from local disk instead of over SMB, but it needs two code paths and could never
touch the existing ~2.3TB backlog or anything uploaded by other means. Against
master it is a single path that handles every case identically — and the network
is not the bottleneck anyway, since encoding is slower than gigabit.

It generates thumbnails and previews for everything, and renders for what can
have one — **all of it desktop-side**. Doing thumbnails on the NAS instead would
mean a second implementation inside the app for a job the desktop does faster;
the cost of keeping it here is that the first pass pulls the bulk of master over
SMB to decode it, which is hours, once.

**Transfers must be parallel.** Measured over SMB against this NAS: single-threaded
small-file throughput is 11-20 MB/s, against 28-34 MB/s at 32 threads. Sequential
transfer of ~62k files would take over a day, so a worker pool is a correctness
concern for the schedule rather than a later optimisation. The same applies to
`upload`. Open UNC paths with the `\\?\UNC\` prefix
([implementation.md](implementation.md)) — flattened names plus deep folders will
find the 260-character limit.

The app operates on the **ready set** and shows the rest as a visible backlog
("1,247 files awaiting processing"). **The app never transcodes.**

**Master is adoption-based.** `upload` is really just "get correctly-named files
into a folder," so a manual copy, another machine, or a future Android tool all
work without the app knowing about them. Such files carry no ledger entry and
could be re-downloaded later; `dedupe` is the backstop, as it is today.

## 10. Hardware

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

### Storage — Btrfs on SHR

The volume is **Btrfs on SHR with data protection**, which settles three things:

- **Checksums plus parity give bitrot detection *and repair*.** For an archive
  whose premise is that originals can never be regenerated, this is arguably the
  most valuable property available. Schedule regular scrubs on the master share.
- **Snapshots are real**, which is what makes deletion-with-an-undo
  ([§3](#3-master)) more than a hope. Snapshot Replication on the master share,
  retention set deliberately.
- **Reflinks exist**, but they only pay if the sequence is right. Copy-then-bake
  gains nothing, because exiftool writes a temp file and renames, fully allocating
  the copy. The pattern that works is **bake once into a delivery-ready artifact,
  then reflink that into each tree** — reflinked files are independent, so a later
  re-bake plus re-link stays at zero bytes per tree. The absolute saving is modest
  at ~0.15TB of delivery; the value is that a future tier costs nothing. Probe at
  runtime regardless: it is a filesystem ioctl through a container bind mount, and
  [§7](#7-distributions) requires correctness without it.

**This is also why the app is restricted to JPG/MP4.** The constraint was adopted
for conversion reasons, but it is equally what makes browsing viable: HEIC decode
and HEVC frame extraction on this CPU would make the UI painful no matter who did
the converting.

## 11. Storage and backup budget

| Tier | Approx | Backed up |
|---|---|---|
| master + sidecars | 2.3TB (1.55TB of it `.insv`) | **yes** |
| renders | whatever needs conversion; disposable | no |
| thumbnails | ~20GB | no |
| previews | ~20GB | no |
| `/photo` | ~0.15TB | no |
| top-10 tree | negligible — ~10 per event | no |
| **offsite** | **~2.3TB** | |

Curation is aggressive — hundreds of photos per event down to 20-50 — so the
delivery tiers are small. That is what keeps this comfortable on 7TB rather than
tight: the earlier worry about distributions dominating the array assumed a much
higher keep rate.

The render tier is the one that scales with imports rather than with curation,
since it covers everything convertible. It is disposable and unbacked, so it is
also the cheapest place to spend bytes. The relief valve for everything is the
two empty bays.

## 12. What this deletes

Most of pix's machinery exists to make destruction safe. An archive whose files
are never rewritten has almost none to make safe — and for the one destructive act
that remains, deliberate deletion, the undo is Btrfs snapshots and Hyper Backup
rather than anything pix builds ([§3](#3-master)):

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
- **`rating`** — `tier` subsumes it; see [§7](#7-distributions)
- **The library root** — no `.pix/` scaffolding, no root discovery, no `pix init`,
  no per-library config; the desktop tool is stateless but for two configured paths
- **`cache.db`** — its role passes to the app's index, which is the same
  aggregation with a permanent home rather than one rebuilt per run
  ([§4](#4-metadata--xmp-sidecars))
- **The library lock** — one long-lived process owns the archive; concurrency
  becomes an internal queue and DB transactions rather than defence against
  competing CLI invocations
- **Stable collision suffixes** ([roadmap.md](roadmap.md)) — names are assigned
  at copy creation and never changed
- **Sync-client re-upload avoidance** ([implementation.md](implementation.md#sync-client-interaction))
  — the master no longer travels through Synology Drive

## 13. What survives

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

## 14. Seeding the existing library

A one-time migration, distinct from the steady-state design above.

### The source is the current library, as-is

`pix/` on the NAS — already normalized (HEIC converted to JPG, video remuxed,
some legacy HEVC transcodes) — becomes master directly. **Neither the run folders
nor `raw/` are processed.**

Considered and declined: reconstructing pristine originals from
`F:\.pix\runs` (158 runs, 1.95TB, **54,325 `CONVERT` captures** — each one a
recoverable pre-conversion original). It would have been nearly free in compute —
original becomes master, today's library file becomes its render, nothing
transcodes — but it costs ~1TB more on the array and offsite.

What makes declining coherent is that **the originals are archived offline
instead**: `raw/` is copied to an external drive with a `sha256` manifest and
shelved. The pristine tier exists, cold, verified. The NAS holds the warm working
archive. It is also reversible while the run folders survive — master and render
are separate tiers, so a later pass could promote originals and demote today's
files to renders. Pruning `F:\.pix\runs` is what makes it permanent.

Accepted consequence: master holds already-lossy HEVC transcodes, so H.264
delivery renders for that subset re-encode from a lossy source. Bounded
generation loss.

### Legacy naming

`OriginalPath` cannot supply `{device}` — only a minority of the library came
through MTP (`G:\pix\raw\tmp\mtp\lola\...`); most predates any device
concept (`G:\pix\raw\media\{year}\...`, `G:\pix\f\{year}\...`). So the
bucket is synthetic, and the library's own `{year}/{event}/` maps onto
[§3](#3-master)'s flattening rule:

```
pix/2015/a/2015-03-15_115256.jpg
    ->  /pix/master/legacy_2015/a_2015-03-15_115256.jpg
        /pix/master/legacy_2015/a_2015-03-15_115256.jpg.xmp
```

- `legacy_{year}` in the `{device}_{datetime}` slot — visibly not a real import,
  and it keeps folders at a few thousand files rather than one directory of 62k.
- **The event prefix is load-bearing, not decorative.** Canonical names only
  disambiguate within a target folder, so `2015/a/` and `2015/b/` can both hold
  `2015-03-15_115256.jpg`; flattening without the prefix would collide.
- No `.import.jsonl` — the ledger exists to skip device downloads, and none of
  this will ever be re-pulled from a phone.
- `OriginalPath` rides into the sidecar, so true provenance survives regardless.

### Sequence — seeding is a folder import

Seeding runs the **production upload path**, not a migration tool: a folder-source
`pix import` from `G:\pix` on the desktop ([§9](#9-ingest--the-desktop-cli)),
then a normal `pix upload`. The largest batch the system will ever process is
therefore also the one that proves it, and the existing skip logic makes a
multi-hour upload resumable instead of restartable.

One import per year directory, with the device name carrying the year — so
`G:\pix\2015` lands as `legacy_2015_{upload-time}`. That keeps folders at a few
thousand files rather than one directory of 62k, and the upload timestamp in the
name is correct rather than noise: a master folder records an ingestion event
([§3](#3-master)).

| | | Frees / costs |
|---|---|---|
| 1 | Verify `raw/` coverage by `OriginalPath` lineage; review the remainder | — |
| 2 | Archive `raw/` offline (external drive + `sha256` manifest), then delete it | **+~3.4TB** |
| 3 | Folder-import + upload `G:\pix`, one run per year | ~2.5TB on the NAS, 9-24h |
| 4 | `pix process` — thumbnails, previews, renders (all desktop-side) | background |
| 5 | Once the new process is proven: archive and delete the old library | +~2.5TB |

**Both trees coexist during the transition** — the old NAS `pix/` and the new
master — which is the point: nothing is deleted until the new process has earned
it. After step 2 there is room for both.

**The sync is never cut.** Uploading creates a separate tree and touches nothing
in the synced folder, so Synology Drive sees no events at all until step 5.

**The seed writes no `.xmp` sidecars.** Legacy `EventAuto` / `DateAuto` /
`OriginalPath` values are embedded in the library files themselves, and the index
reads through to them when no sidecar exists
([§4](#4-metadata--xmp-sidecars)) — so inherited events appear without a migration
step, and a sidecar is created only when a value is first changed.

**Legacy folders get an `.import.jsonl` like any other.** An earlier draft
special-cased them as having none; using the real upload path means one gets
written anyway, and it is a genuine record of what was seeded and where each file
came from.

**Step 1 must use lineage, not content hash.** Conversion changed the bytes, so a
HEIC in `raw/` and the JPG it became have different hashes and containment would
flag every converted file as missing. `OriginalPath` is exact where hashing is
not. The unaccounted remainder separates into deliberately-dropped (migrate
`DELETE` lines, cross-checkable against the 158 run plans), never-processed
(formats the policy skips), and genuinely-missed — the last being the reason the
check is worth running at all.

**Measure before committing to step 3.** Run one year folder and time it; the
whole-library estimate spans 9-24 hours depending on how the Atom handles the
write path.

### Scale

**61,846 media files**, of which the existing exports hold 95 and 7 — curation is
at **0.15%**. Those 95 were selected by the old `rating` filter; translating them
into `tier` and discarding `rating` is trivial at that scale. The number's real
significance is different.

The app is not a convenience layer over a mostly-curated library. Its entire job
is making **61,846 uncategorized decisions tractable** — hundreds per event down
to 20-50. That moves the curation UI from a detail to the thing the project lives
or dies on, and it is the largest remaining unknown in [§15](#15-open-questions).

## 15. Open questions

*(Resolved in discussion: import-ledger identity — [§9](#9-ingest--the-desktop-cli);
distributions and the curation scale — [§7](#7-distributions); multi-user, auth and
Synology Photos write-back — [§8](#8-the-app); Btrfs — [§10](#10-hardware);
the sidecar/index model — [§4](#4-metadata--xmp-sidecars); seeding — [§14](#14-seeding-the-existing-library).)*

- **Curation UI — what remains.** The model is specced
  ([§8](#8-the-app)): three passes, promote-only, ranges as selection. Still open
  are the visual design itself, the keyboard grammar, and **near-duplicate
  grouping**, which is pass 2's biggest lever and depends on image perceptual
  hashing ([roadmap.md](roadmap.md)) that does not exist yet.
- **Ad-hoc `pix export` CLI surface.** The desktop one-off case
  ([§7](#7-distributions)) needs inline filter and template arguments; new CLI
  surface, unspecified.
- **Non-destructive edits — deferred, noted for posterity.** Cropping a photo or
  trimming a video keeps the original untouched and saves the result separately.
  This needs no new concepts: **parameters in the sidecar** (crop rect, trim
  points, rotation — they are human decisions like any other), **result in the
  render** (derived, regenerable from original + parameters). It is the
  raw-photography model the sidecar design already borrows from — develop settings
  beside the negative, the JPEG is an export.

  One rule it changes: [§5](#5-renders) says a tag change never invalidates a
  render. That stays true, but an **edit** change does — the render is what the
  edit produces. Video trims are also cheap here specifically because the render
  is already a transcode; a keyframe-aligned cut can be lossless, and a
  frame-accurate one costs nothing extra since the delivery encode is happening
  anyway. Identity is unaffected either way: the original's content hash never
  changes.
- **`tier` and the probed facts as stored XMP** — namespace and serialization are
  unspecified, as is whether `tier` is baked into delivery copies (nothing reads
  it there, but it costs nothing and aids debugging).
- **Where dedupe judgments live.** "These two are the same shot" is a human
  decision, so by the rule above it belongs in master — but it is inherently about
  a *pair*, and a per-file sidecar is an awkward home for it.
- **Face detection** remains deferred, and `{person}` depends on it — which is what
  the people-grouped ad-hoc distribution would need.
- **Reclaiming space on the NAS.** Emptying `#recycle` did not return space;
  Btrfs snapshots and Synology Drive version history are the usual causes, and they
  need sorting before the `raw/` deletion in
  [§14](#14-seeding-the-existing-library) can actually free anything.
- **Folder-source import details** — the `.manifest/` shape for a source with no
  PUID, and how `(relative path, size)` behaves against a tree that is not
  immutable while the import runs.
