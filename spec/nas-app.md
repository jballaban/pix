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

So a sidecar is four fields — `audience`, `event`, `tags`, and a date
override.

**`audience` is who may see the file**, and it replaced a `tier` of
`none`/`photo`/`top`. Those were two questions wearing one name — *has this
been reviewed* and *how good is it* — and neither was the question actually
being asked, which is **who is this for**. A household has photographs the
children should not see and photographs that belong on the television; that
is one axis, not a quality ranking. *The best ones* is simply an audience
that happens to be small, which also disposes of the nested-tier storage
problem in [§7](#7-distributions): audiences are flat, so nothing is stored
three times for being in `top`, `photos` and `general` at once.

An audience is a **name**, and some names happen to have a login. `private`
has none, so nothing can ever sign in as it — which makes *keep this but
show it to nobody* an ordinary value rather than a special state. Access can
be granted to a person or to a role (`family`, `parents`, `tv`) and the check
cannot tell them apart, which is what stops roles becoming a second
mechanism. The owner is never in the list: an administrator sees everything
by definition.

**No audience means undecided**, so review state needs no separate flag and
the *New* filter is simply *shared with nobody*.

**`tags` are free text, many per file.** They are the axis `event` is not:
an event is *when and where* and a file has one, a tag is *what about it*
and a file has as many as anyone cares to add. Trying to serve both from one
field is what made the old library's event names drift into ad-hoc labels.

**The date override may pin only some components.** It is
`YYYY-MM-DD-HH:MM:SS` with `*` in any slot, and the effective date is the
probed capture date with each non-`*` component replaced — the grammar
[tags.md](tags.md) already defines, now shared by both architectures in
`pix.datestr`. *This scan is from 1987* is a complete answer, and one that
does not require inventing a month, a day and a time. Fabricating them is
not a harmless default: a made-up `1987-01-01 00:00:00` is indistinguishable
from a real one a year later.
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

#### Serialization

An XMP packet in RDF attribute form, using the **`pix` namespace already
registered for ExifTool** in `exiftool_config.cfg` (`http://pix.local/`).
Reusing it rather than minting a new one is what makes the read-through
symmetric: `pix:EventOverride` means the same thing whether it came from a
sidecar or from the tags embedded in a legacy file, so the index treats the
two as one cascade instead of two vocabularies it has to reconcile.

| decision | pix property | also written as |
|---|---|---|
| event | `pix:EventOverride` | `Iptc4xmpExt:Event` |
| tags | — | `dc:subject`, the standard keyword bag |
| audience | `pix:Audience` | — nothing standard expresses *who may see this* |
| date override | `pix:DateOverride` | `photoshop:DateCreated`, but only when
  the override pins a whole timestamp — a partial date has no ISO 8601 form,
  and filling the holes to produce one would publish a precision the curator
  explicitly did not claim |

Tags live **only** in `dc:subject`, with no `pix:` twin. It is the industry
keyword field, every tool round-trips it, and nothing in pix used it before —
so there is no legacy vocabulary to reconcile and no second home that could
come to disagree with the first.

Written temp-then-rename, so a kill mid-write cannot leave a half-written
packet that parses as a decision nobody made. A sidecar that exists but will
not parse reads as *no decision* while still counting as a sidecar — master
is the record, so damage there has to stay visible rather than looking like
"never reviewed".

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
- **Two write paths, and the difference is the file set.** A *rebuild* is what
  discovers which files exist, so it is wholesale and belongs to ingest —
  `pix2 index`, and the tail of `pix2 process`. A *refresh* rewrites the single
  row whose decision just changed, and is what the app runs: reading 62k records
  to record one tiering is not an interface anyone uses twice. A refresh
  re-derives from the same two inputs a rebuild uses and never adds or removes
  rows, so the two cannot disagree — a later rebuild can only confirm it.
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
  ([§16](#16-open-questions)), an *edit* change would invalidate a render — a tag
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

**Renders are not built yet, and video needs them more than expected.** Master is
seeded from the already-normalised library
([§14](#14-seeding-the-existing-library)), so for **images** it is JPG throughout
and nothing needs converting. For **video** that reasoning was wrong: measured
across the seeded year, **421 of 724 clips are `hvc1` (HEVC) against 303 `avc1`**,
so 58% will not play in a browser at all. The app streams master directly — there
is no delivery rendition to serve instead — so those clips show their poster frame
and refuse to start.

The H.264 render tier is therefore not polish for a future delivery tree; it is
what most of the video library needs to be watchable in the app itself.

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
plaintext** — a credentials file on a share reachable over SMB is exactly how
a reused password leaks.

**Enforced server-side on every route that returns bytes**, not only on the
listings. A grid that omits a photograph while `/preview/...` still serves it
is not access control, it is a tidier index — anyone can type a URL. An
unshared file answers **404**, the same as one that does not exist: 403 would
confirm there is something there to ask for.

The viewer's scope comes from the credentials and rides on every query. It is
deliberately **not** one of the URL filters, so a viewer cannot widen their
own view by editing the address bar — the one thing a URL-shaped filter model
must not allow. An empty scope is not the same as no scope: somebody granted
nothing sees nothing, and conflating the two is the classic way an access
check becomes an access grant.

**Sessions, not HTTP Basic.** Basic cannot log out — browsers cache the
credentials and offer no way to clear them — which makes *switch to the
admin account and back* impossible, and that switch is the normal way this
app is used. So there is a login form and a signed cookie. Basic is still
*accepted* for scripting but never *challenged* for: with no
`WWW-Authenticate` header a browser never starts caching one.

**`admin` is built in and hard-coded**, so there is no way to lock yourself
out by editing a file and no way to delete the only account that can grant
access. It is never an audience: an administrator sees everything already.
Its password ships as a hash of a known initial value rather than as
plaintext — the repository has a remote, and a password committed to one is
a published password — and the app says so until it is changed.

**People and roles are managed in the app**, in `app/users.json` on the
share. Adding a person is a household event, not a deployment: it should not
need a shell, a text editor and a container restart. That file is
configuration and not archive — lose it and you lose the logins, not a
photograph or a decision about one — which is why it may be a file where
[§4](#4-metadata--xmp-sidecars)'s metadata may not.

Deleting an account **leaves the grants alone**. A share is a decision
recorded in master, and removing a login is not a statement about the
photographs; recreating the name restores the access and nothing had to be
rewritten across the archive.

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

**Finishing an event writes `tier: none` for everything unpromoted.** It is
not a dedicated button: it is *filter to this event and status new, select
all, reject* — the same outcome through the general mechanism, which is one
fewer thing that only one page could do. Positive
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

#### The surface: filter, select, apply

One grid, and a **persistent top bar** that never scrolls away, because the
filters are the address of what you are looking at — losing them two thousand
thumbnails down is losing your place. Picking an event on the landing page is
that page with `?event=`, so there is one surface to learn rather than a
browser and a separate editor.

**The landing page is the same grid at a coarser zoom.** It was a fixed table
of years and their events, which answered two questions well and every other
one not at all: *which cameras is this library from*, *what is still undecided
in July*, *which days of the trip have the most photographs*. Those are the
same questions the grid answers about files, asked of the same rows with a
`GROUP BY` — so the landing page takes the same filters and the same grouping,
and draws each section as one folder instead of as a heading with its contents
beneath it. A folder's cover is that section's first photograph, so it looks
like what is inside it rather than like a name somebody chose.

Opening a folder is this view plus what the folder is, which means **a grouping
has to be expressible as a filter to be drilled into**. `camera` and `source`
became filters because of this page — `source` being the name the import was
given (`james`, `alina`, the folder tree that seeded the library), read once
per folder from its ledger header. It is the more useful of the two: a phone is
replaced every few years and a camera model says which one it was, where this
says whose it was. Every import from that phone lands in a master folder of its
own, so the folder is no use as a filter and the name is. Two are not: *no day* and *no month* mean *dated less
precisely than that*, and the date filter answers `undated` or a prefix with
nothing in between — so those folders say they cannot be opened rather than
opening something larger than what was clicked.

**Taking a copy away** is one gesture with two possible meanings, and the app
only asks which where there are two. The *original* is what came off the camera
and is what master holds; the *playable copy* is the H.264 rendition, which
exists only for the clips a browser will not play as they are. For every
photograph, and for the third of the clips that were already H.264, they are
the same file — so Download downloads, and the choice appears only when the
selection holds something the question applies to.

One file is a link. A selection is a **posted form**, streamed back as a zip:
posted because five hundred names do not fit in an address, a form rather than
a fetch because the browser has to own the transfer — a fetch holds every byte
in this page's memory before a file appears anywhere. Stored rather than
deflated, since everything in the archive is already compressed and deflating
would spend the processor to save nothing on the one path where throughput is
the whole experience.

Filters live in the **URL**, which makes a view a link: shareable,
bookmarkable, and survivable across a reload. It also makes the browser's
back button mean *the filter I had before*, which is the only undo a filter
needs.

| filter | values |
|---|---|
| event | existing names |
| year | calendar years, from the **effective** date |
| tag | existing tags |
| shared with | a person, a role, or *new* — shared with nobody |
| type | photo / video / other |
| size | small-short / medium / large-long |

**One size filter, not two.** For a clip the question is length, for a photo
it is weight, and they are the same question — *is this a throwaway?* A
3-second fragment and a 50KB image are the same kind of suspect, so making
the curator pick the right control first would be asking them to know the
answer before the question. Thresholds are build constants in `nas.index`,
tuned against the seeded year.

Selection is a **checkbox per thumbnail**, with shift-click for a range and
ctrl-click to add — the convention every photo tool already uses. The bar
shows the count and the actions that apply to it.

**Every dropdown is ranked by the current view, in three bands**: values
already used by files matching *every* other active filter, then by files
matching *any* of them, then the rest of the library. Looking at
`year:2026 + tag:tv` and reaching for an event name, the events used in that
exact slice come first. A library ends up with hundreds of events and tags,
and an alphabetical list of all of them buries the handful that apply. Each
dropdown also takes a **new** value typed inline, because the alternative is
a separate create-then-assign step for what is one decision.

The column being set is excluded from its own scope — filtering on
`event:X` and reaching for an event, `X` is the one option nobody wants.

#### Three passes

| Pass | Unit | Gestures |
|---|---|---|
| 1 — events | proposed clusters | name / merge / split — hundreds in total |
| 2 — keep | one event at a time | share the keepers; finish by sending the rest to `private` |
| 3 — highlights | the shared 20-50 | add the small audience — `tv`, say |

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
- **`upload` verifies each file as it lands, not in a pass at the end.** A
  trailing pass is not cancellable (Ctrl+C there escapes the worker pool
  uncaught), is sequential where the copy is 32-wide, reads every file back cold
  instead of while the NAS may still have it cached, and can only report that
  *the batch* failed rather than which file. A mismatch removes the bad file from
  master, keeps it out of the ledger, and leaves staging intact.
- **`upload` also needs two mechanics.** Copy to a **marker-named temp and rename
  into place** — the `*.__*` convention [`export`](#7-distributions) already uses — because
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
or dies on, and it is the largest remaining unknown in [§16](#16-open-questions).

## 15. Identity — when two files are the same photograph

**Status: identity is built; resolving duplicates is deferred.** `process`
writes the hashes and the index carries them (v0.1.344–346), so the library can
be *asked* the question. Grouping, the proposal and the page are specified below
and not built. The other three relations are named and bounded so that building
the first does not accidentally decide them.

Recorded 2026-09-16, amended 2026-09-17.

"Are these the same?" is four questions, and the mistake would be to answer them
all with a stack. They differ in what the claim *is* — fact or judgement — and
therefore in who gets to make it.

| | The claim | Test | Gesture |
|---|---|---|---|
| **Duplicate** | the same coded image, twice | content hashes match | keep one, remove the rest |
| **Derivative** | the same photograph, degraded | perceptual match, one strictly poorer | remove the poorer — *only* if the better one is here |
| **Stack** | the same shot, several frames | capture window, one camera | one speaks for the rest |
| **Round trip** | something pix itself made, come home | its own stamp | skip, and say so |

**Association is not on this list**, and deliberately. "Several photographs of
the same general thing" is an *intent*, it is many-to-many, and it is already
expressible: that is what an event and a tag are, and folding a grouping in the
grid is what collapsing one looks like. Making it exclusive and hierarchical
would produce a worse stack; making it many-to-many would produce a worse tag.

### Identity, in the meta tier

`process` already opens every master file to derive from it, so identity costs
one more pass over bytes that are already in hand. These are ordinary metadata
and live where the rest of it does — beside the probed facts in
[`meta`](#2-tier-layout), and so in the index that is built from them.

One record carries three: the master's **content hash** and **perceptual hash**,
and the content hash of its **render**. The third is there because a render is
the file that leaves the building — it is what the app hands out, so it is what
comes back — and its bytes are a re-encode, which means the master's own hash
cannot recognise it. It is written when the render is made, and picked up from
an existing render whenever the record is rebuilt.

**The content hash covers the coded image data only** — the quantisation and
Huffman tables, the frame header and the entropy-coded scan for JPEG; the
primary item's coded extents for HEIC; the `mdat` samples for MP4, MOV and
`.insv`. Every `APPn` segment is skipped: EXIF, XMP, ICC and embedded
thumbnails.

That exclusion is the whole point. The case this exists for is a photograph
AirDropped to a second phone and imported from there — the second device
re-wraps the file, rewrites its metadata and renames it, and changes not one bit
of the image the camera encoded. A hash of the file says *different*; a person
says *obviously the same*. Skipping the metadata makes the machine agree with
the person, with no threshold to tune.

**Not a hash of the decoded pixels**, which is the obvious alternative and is
equally free at process time. Two versions of libjpeg can differ in the last bit
of an IDCT, so a decoder upgrade would silently change every hash in the archive
with nothing to distinguish that from a real change. The coded stream needs no
decoder at all and is therefore stable for the life of the archive, which is the
property an archive wants.

**The perceptual hash is stored at the same time and used by nothing yet.** It is
what a derivative needs, and `process` holds the decoded pixels exactly once in
the life of a file. Deciding later costs a full re-read of 2.3TB off spinning
disks behind the Atom; deciding now costs a column.

### Duplicates

Two master files whose content hashes match. This is a **fact**, not a guess —
no distance, no threshold, and nothing to refuse on the grounds that the app got
it wrong. The only judgement is which copy stays.

Two independent choices, and running them together is what made this look hard:

**Which file survives** — the oldest import. For a true duplicate the images are
interchangeable, so nothing is at stake in the picture; what is at stake is
provenance, and the earlier folder is the closer record of how the photograph
arrived.

**Which facts survive** — layered, which is the read-through
[§4](#4-metadata--xmp-sidecars) already applies to one file, applied across a
set:

1. **Decisions from any copy.** They are sidecar facts, so carrying them costs
   nothing and touches no bytes. Tags and audience **union** — a set cannot
   conflict with itself.
2. **Single-valued decisions** — event, date override, stack membership —
   resolve by **latest wins**, then the survivor's, then the oldest file's, then
   lexicographically by `folder/name`. The ladder terminates, so every group
   gets a complete proposal and none ever lands on a human being told *I cannot
   tell*.
3. **Embedded metadata** — the survivor's own EXIF wins wherever it has any.
   Where the survivor is silent and another copy is not, that value lands as an
   **override** on the survivor's sidecar, attributed, because the survivor's
   file never said it and must not be made to appear to.
4. **Live beats binned** — if one copy is in the bin and another is live, the
   live one wins. Merging a living photograph into a purge candidate would
   delete it as a side effect of tidying.

*Latest wins* requires a time, and a sidecar carries none: the fields are event,
date override, tags, audience, deleted, stacked-under and no-stack. So the
sidecar gains **`xmp:MetadataDate`**, the standard tag for exactly this, written
on every decision. It follows the pattern §4 already uses — a `pix:` field
beside its standard twin — and being standard it is readable by everything that
reads the rest of the sidecar.

The alternative was to need no timestamp at all: *the survivor's decision wins*.
It was rejected because it loses the case that motivated the merge — the newer
copy is often the one that has been curated, and the point of the exercise is
that choosing the older **file** must not throw away the newer **thinking**.

#### The page, and why this one earns a page

Suggestions reach the curator through controls that already exist, which is why
[stacks have no review page](#suggested-stacks--a-guess-is-a-view-not-a-decision).
Duplicates are the exception, for a reason specific to them: **they are visually
identical by construction.** A grid of thumbnails shows the same photograph
twice and communicates nothing. What has to be on screen is what *differs* — the
folders, the dates, the decisions, which is older — and that is a table.

One row per group: the proposed survivor preselected, the losers beside it, and
only the fields that actually differ, each showing the values found and which
one won. Reading that list *is* the review. Accept is one click; accept-all is
one more.

Two overrides, each one click: **choose a different file to win**, and **choose
a different value** among those present.

**This page chooses between values that exist; it never invents one.** No text
fields, no tag picker, no access menu — one control per differing field and
nothing else. A value neither copy has is an ordinary edit: accept the merge,
then edit the survivor in the grid, where that already works. Without this line
the page grows into a second, worse copy of the editing surface.

**Rejecting a group means *keep both*, and that is a decision.** It needs
recording the way `pix:NoStack` records the refusal of a suggestion, or the next
index build proposes it again and the queue never empties.

Accepting is an ordinary write, so it lands in the operation log with its undo
like every other one, and the page can say what it has already done rather than
only what is left.

#### The grid is left alone

Folding a duplicate behind its survivor was considered and **deferred**. The
precedent points that way — guessed stacks fold by default, on weaker evidence
than this — and folding a duplicate is the safer of the two, because the images
are identical by construction and so nothing visible is lost.

It is still a change to what the library looks like in exchange for tidying
something that is, by the numbers, rare: the seeded library holds **zero**
duplicate groups across 10,126 files. Both copies stay on screen, and the
backlog is reported instead by the **activity bell** — which is what that
control is for, and what its own note anticipated when it said the next thing
worth reporting would otherwise have become a second phrase in the bar.

That also disposes of a question folding would have forced: what a folded row
shows when the two copies disagree. Nothing is folded, so both say what they
say, and the merge is a proposal on a page rather than a preview in a grid.

### Purging leaves a tombstone

Deleting is already two states: **binned** keeps the photograph and hides it;
**purged** removes it. So by the time something is purged there is only one
reason left — the bytes are not wanted — and a tombstone needs no field saying
which of several reasons applied, because there are not several. Unwanted and
redundant both end in the same act, and anything worth keeping was never purged.

`.removed.jsonl`, one per master folder beside `.import.jsonl`: both hashes, the
name, the capture date, when it was purged and by whom. One line each, in the
same append-only, readable-without-pix shape the import ledger already has.

This makes purging **idempotent**, which today it is not: purge a file, re-import
the folder it came from, and it returns with nobody any the wiser.

A file matching a tombstone **imports, and is then presented for decision.** It
is not refused at the door. A refusal would be a silent discard, and a photograph
that disappears without anyone learning it did is the failure this architecture
exists to prevent — the same reasoning that makes a skipped import a recorded
skip rather than a quiet one.

### Round trips

A render downloaded from the app and re-imported is a duplicate no hash can see:
it is a different encode, so its content hash differs from the master's by
design. Nothing links it back except what pix chooses to write.

So renders and delivery copies are stamped when they are **made** — by `process`
and by the bake in [§7](#7-distributions), never at download time. Stamping on
the way out would mean rewriting metadata per request on the Atom, and would
stop *download the original* from returning quite the original.

- **`pix:SourceId`** — the content hash of the master it came from. An identity
  rather than a path, so it survives any later reorganisation.
- **`pix:ArtifactId`** — the content hash of the artifact itself, as made.
- **`pix:SourceFile`** — the readable `folder/name`, for a human holding the file
  in twenty years with no pix to ask.

Then the import needs nothing but the file in hand. Its own coded data still
matching `ArtifactId` means an untouched round trip: skip it, and record the
skip. Not matching means it was edited after it left — a crop, a trim — which
makes it a new photograph that happens to know its parent. A stamp-only rule
would have discarded that edit silently.

**Masters need no stamp.** A downloaded original that comes home is caught by the
content hash already, even if something re-tagged it in transit — which is
precisely what a metadata-blind hash is for.

**Nor does a render depend on its stamp.** The stamp is metadata, and metadata
is exactly what a messaging app strips on the way through; the render's content
hash is intrinsic to the file and survives anything short of re-encoding it. So
an import is checked against **both** columns, and a match on a render resolves
to the master that render belongs to — which is always present, because
destroying a master sweeps its render with it. The stamp remains worth writing:
it is what distinguishes an untouched round trip from one that was edited after
it left, which no hash of the incoming file can answer on its own.

**The stamp is an optimisation, not a guarantee.** Messaging strips metadata, so
a render sent to family and sent back arrives bare and falls through to the
perceptual path like any other derivative. Every mechanism here is a cheaper
route to an answer the perceptual hash reaches more slowly.

### Derivatives — named, not designed

A photograph re-encoded and downscaled by WhatsApp or a text message: same image,
stripped metadata, strictly poorer. It is not a duplicate — recompression changes
the coded data — and it is not a burst, because it is the same frame rather than
another one.

Two things are already known about it, recorded here so the work above does not
quietly decide them:

- **Today's suggestions cannot see these files at all.** Stack guesses are
  metadata-only — a capture window on one camera, plus colliding names — and a
  messaging copy has neither a camera nor a full-precision date. They are not
  slipping through the net; they were never eligible for it.
- **A derivative whose original is absent is not a duplicate — it is the only
  copy.** A photograph a cousin took and sent is a real photograph in the
  library. The gesture can only ever be *remove the poorer when the better one is
  present*, never *remove derivatives*.

### What this does not change

Stacks keep their definition, their flatness and their controls. The `band`
filter keeps finding re-compressed messaging images by weight, which is a crude
handle on derivatives and stays useful until identity gives a precise one.

## 16. Open questions

*(Resolved in discussion: **what makes two files the same photograph, and what
to do about each kind** — [§15](#15-identity--when-two-files-are-the-same-photograph);
import-ledger identity — [§9](#9-ingest--the-desktop-cli);
distributions and the curation scale — [§7](#7-distributions); multi-user, auth and
Synology Photos write-back — [§8](#8-the-app); Btrfs — [§10](#10-hardware);
the sidecar/index model — [§4](#4-metadata--xmp-sidecars); seeding — [§14](#14-seeding-the-existing-library);
**where a judgement about a set lives** — see stacks, below.)*

### Stacks — and why they are flat

*"These two are the same shot"* looked like it had nowhere to live. It is a
human decision, so [§4](#4-metadata--xmp-sidecars) puts it in master — but it is
about a *set*, and a per-file sidecar seemed an awkward home for a fact about
several files.

It was never about the set. Each file records the one it defers to
(`pix:StackedUnder`); the file that speaks records nothing, because being
spoken for is the decision and speaking is what is left. So it is per file after
all, and losing a folder costs those files and their deference together — the
rule everything else here already follows.

**Stacks do not nest**, and that is a decision rather than an omission. It was
reconsidered once and kept, for two reasons that are about the interface rather
than the storage — the sidecar model allows nesting perfectly well, so this
stays reversible:

- **A count would stop being answerable.** A badge saying *4 photographs
  stacked here* can promise either what is directly behind the file or what is
  anywhere beneath it. The first lets a stack of two hold twenty; the second
  means opening it shows fewer than it said. The honest version needs a
  recursive count on every thumbnail in a two-thousand-cell grid.
- **`Unstack` would stop meaning one thing.** Flat, it is unambiguous: on a
  file behind another, take that one out; on the file that speaks, take the
  stack apart. Nested, the same click on the same file could mean *lift this
  and its own members out of the stack it is in* or *dissolve it*, and both
  read correctly.

Stacking a stack therefore **merges**: its members come up with it, because a
stack's members follow the file that speaks for them wherever it goes. Out of a
stack and they come out; into another and they go in. That one rule is also
what makes taking a whole stack apart a single gesture.

### Suggested stacks — a guess is a view, not a decision

Most of a burst is one photograph shot eight times, and a library holds
thousands of them. The app can see that — same camera, seconds apart, or the
generated names that collided because the shutter did — but seeing it is not
deciding it. `pix:NoStack` records the refusal; nothing records the acceptance,
because an accepted guess is an ordinary stack made the ordinary way.

It lives in the **index**, as `suggested_under` beside `stacked_under`: a
projection of the library, rebuilt with it, and never authoritative. That is
what lets one clause decide what a listing holds — the grid, the header count
and the *did this leave the view* check cannot disagree, because they all ask
the same question of the same column. A decision recomputes the guesses around
the file it touched, so refusing one clears it and taking the refusal back
brings it back; discovering a *new* one is a build's business, since only a
build has seen the whole library at once.

It reaches the curator as two controls that already existed, which is why there
is no review page:

- **The `Stacks` filter**, which is about stacks in general, a guess being one
  the app made rather than you: *everything* (the default — every stack folded
  behind the photograph that speaks for it, guesses included), *only stacks*,
  *only suggested* (the shelf of what is still to answer), *no suggestions*
  (the stacks a person made, and nothing else).
- **Grouping by stack** — sections are stacks, and their members come out from
  behind what speaks for them. With *only suggested* that is the whole review:
  every guess, open, one section each, answered by scrolling.

The filter narrows and the grouping arranges, which is the same division every
other pair of controls here follows.

**Folding is the default, and a viewer never gets it.** Most of a burst is one
photograph shot eight times, and a library that shows all eight is the pile you
started with — so the app folds on its own evidence. That is a curator's call:
somebody who cannot accept or refuse a guess has no way to see what was folded
away, so for them a stack is only ever one a person made. It is not enough to
drop the parameter for them now that folding is what the default does.

Folded, a guess behaves like a stack in the one way that matters: a decision
made about what is on screen reaches everything behind it. The badge is the
difference — the same count in a different colour, because *somebody put these
together* and *these look alike and nobody has said yet* are different claims,
and a curator deciding what to trust has to see which is which.

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
- **Whether `tier` is baked into delivery copies.** The sidecar serialization
  itself is settled ([§4](#4-metadata--xmp-sidecars)); what is still open is
  whether the delivery copy carries the tier that selected it. Nothing reads it
  there, but it costs nothing and aids debugging.
- **Face detection** remains deferred, and `{person}` depends on it — which is what
  the people-grouped ad-hoc distribution would need.
- **Reclaiming space on the NAS.** Emptying `#recycle` did not return space;
  Btrfs snapshots and Synology Drive version history are the usual causes, and they
  need sorting before the `raw/` deletion in
  [§14](#14-seeding-the-existing-library) can actually free anything.
- **Folder-source import details** — the `.manifest/` shape for a source with no
  PUID, and how `(relative path, size)` behaves against a tree that is not
  immutable while the import runs.
