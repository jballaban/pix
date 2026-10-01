# Clips — video splitting, and stills from video

**Status: built, but for delivery** (v0.1.414–436). Round trips — a clip,
still or render that comes back through an import — are recognised by stamp
and content hash (spec/nas-app.md §15). Clips in distributions wait for the
delivery trees themselves, which are in the backlog
([roadmap.md](roadmap.md)). Extends
[nas-app.md](nas-app.md); where the two disagree, this file is the newer intent
for clips, and the code remains the source of truth for what exists.

Recorded 2026-09-28.

---

## 1. What a clip is

A **clip** is a stretch of one source video; a **still** is one frame of it.
Both are *virtual*: no new master bytes are created and the source is never
touched. The definition is a human decision, so it lives in master as a
sidecar; the pixels are derived, so they live in the render tier, regenerable
from source plus definition. This is the model
[nas-app.md §16](nas-app.md#16-open-questions) anticipated for trims —
parameters in the sidecar, result in the render.

**Not a derivative.** [§15](nas-app.md#15-identity--when-two-files-are-the-same-photograph)
already uses that word for a degraded copy (a WhatsApp re-encode), whose rule
is *remove the poorer when the better is present*. A clip is not poorer and
must never fall under that rule, so it gets its own name.

Materialising clips as new master files was the alternative, and was rejected:
every clip would be a re-encode backed up forever, every re-splice would create
or delete master files, and master would stop meaning *what came off a
device*.

Eventually crops and other edits are expected to follow the same pattern. They
are not designed here.

## 2. A clip is a library item

Each clip and still is a full item — its own people, tags, event, audience,
rating, bin state — because tagging Mum in one clip and not another is exactly
the kind of decision the library exists to hold.

```
master/.../IMG_C.MOV              the source, untouched
master/.../IMG_C.MOV.xmp          C's own decisions, if any
master/.../IMG_C.MOV~k3f.xmp      clip: its range + its decisions
master/.../IMG_C.MOV~p9q.xmp      still: its timestamp + its decisions
render/.../IMG_C.MOV~k3f.mp4      the clip's cut (§6)
```

The index gets a row per clip, keyed `(folder, name)` like any file, with the
name `IMG_C.MOV~k3f`. **The relation to the source is the name**, the same way
a render path is computed from a master path with no lookup table — so no
field records it.

**Nothing rolls up.** C's record never gains the union of its clips' people.
Stored decisions are per item; a search for Mum returns the clip she is in,
not the source. A view of C that wants the union computes it.

### Identity

The suffix is a **short random id**, unique among its siblings, never a
position. Ordinals renumber the moment a clip is split, and every clip after
it silently inherits its neighbour's tags. Clips display ordered by their
in-point, so the id needs no meaning.

| Gesture | Identity |
|---|---|
| Drag an edge | Both clips keep their ids and decisions |
| Split a clip | The first half keeps the id; the second gets a new one and a **copy** of the decisions |
| Remove a split (merge) | The earlier id survives; decisions merge by the §15 ladder — sets union, single values latest-wins by `xmp:MetadataDate` |
| Bin a clip | Ordinary bin; that stretch is simply in no clip |

Refusing a merge whose halves disagree was the alternative; the ladder already
answers the question without handing it to a person. Every gesture lands in
the operation log with its undo.

### Ranges, not split points

A clip is an in/out pair. Ranges may leave gaps (drop the dead footage) and a
single range is a trim, so *split* is a gesture that produces two adjacent
ranges rather than the model itself. **Ranges never overlap**: two clips over
the same footage are two copies of it, which is what dedupe exists to catch.
Stills are points and never conflict with a range.

### Inheritance — copied at creation

A new clip **copies** C's event, people, tags, audience and rating, then is
independent. Live inheritance (*own value, else C's*) fails on the set-valued
fields: taking Mum off one clip when she is inherited needs a stored *not Mum*,
a subtraction layer nothing else in the model has. Deleted, stacked-under and
no-stack are not copied — they are about C's place in the grid, not its
content — and neither is `archived` (§3), for the same reason: a clip is
usually made in order to archive its source, and one born archived would vanish
the moment it was made. C's own sidecar is left as it was.

**The date is the exception, and is live**: a clip's effective date is C's
effective date plus its in-point (a still's, plus its timestamp), unless the
clip carries its own override. It is single-valued and derived, so it has none
of the problem above, and correcting C's date moves every clip with it. Clips
tied to the same second sort by in-point.

## 3. Archiving — an audience, and an explicit act

Nothing is archived automatically. C, its clips and its stills sit side by
side, and the curator archives whatever they like. An automatic rule (*C is
archived while it has clips*) was considered first; it forced questions — does a still archive C? what
of a stack C speaks for? — that an explicit act simply does not raise.

**`archived` is a built-in audience value**, reserved like `admin`. It was
`hidden` until 2026-10-01; every sidecar and log line was rewritten when it was
renamed, so there is no alias:

- **Exclusive.** Setting it clears every other audience; adding any audience
  clears it. Archived-and-shared is a contradiction.
- **A decision.** An archived file is not *New*.
- **Out of the admin's default view too**, with a filter to bring it back.
  That is what distinguishes it from `private` (a role with no members, which
  an administrator still sees): hiding is about the curator's own grid, not
  only about everyone else's. *Archived* rather than *hidden* because that is
  what it is for — kept, and not in front of you — where *hidden* read as a
  secret.
- **Admin-only**, like every edit today — so splicing is admin-only as well.

It is not the bin. Binned is *on its way to being purged*; archived is *keep
this, and don't show it*. It is also not specific to clips: anything can be
archived. The splice page offers **Archive original** as a one-click convenience —
the same generic act, where it is wanted.

Archiving a file that speaks for a stack archives the stack, by the existing rule
that a decision on the head reaches everything behind it.

## 4. Stacking — video is deferred

Whether and how video stacks needs its own thinking. Until then:

- The suggester does not propose video stacks.
- The page and the API both refuse to stack a video.
- Video stacks already recorded in sidecars are **left alone** — unstacking
  them automatically would reverse a person's decision silently — and a video
  in one cannot be spliced.

Stills are images and stack like any image. A still taken from a Live Photo's
`.MOV` will look very like the `.HEIC` beside it, and a suggestion to stack
them is arguably right.

## 5. Deleting

A clip is C's bytes plus a range; its files are disposable. **It cannot
outlive C.** And *I only want the clips, get rid of the rest* is the natural
reason to delete C — exactly the gesture that would destroy them.

Binning C while it has clips that are not binned offers two choices:

- **Don't** — archive C instead.
- **Bin anyway, and make the clips independent.** Each clip's cut (§6) is
  copied into C's master folder as a real file with its sidecar, becoming an
  ordinary video with no source. A still's JPG likewise. This is a copy, not
  an encode, so the NAS does it. It happens at bin time, so restoring C later
  re-links nothing. It is offered only once every clip has its files —
  so until cuts exist (step 4) the refusal is all there is.

The new file is named for the clip (`IMG_C.MOV~k3fa.mp4`), and the app
writes it a **placeholder meta record** from what the clip's row knew, so it
stays in the grid rather than vanishing until the desktop runs; its old
source's pictures stand in meanwhile. `process` replaces the placeholder
rather than trusting it. Only the binning goes into History: the new files are
real files, and removing one is an ordinary delete.

A materialised clip is a pix-made file in master — the same compromise as
[seeding](nas-app.md#14-seeding-the-existing-library), the best copy that
still exists — and because the cut is a stream copy it is the source's own
samples, not a re-encode. It still records its provenance: the stamp (§6)
names the source it came from.

Binning or purging a single clip touches nothing else. Purging C after its
clips are binned purges them with it. Restoring a clip whose source is binned
restores the source. A source that disappears outside the app leaves its clip
sidecars orphaned, and they are swept like any orphaned `.xmp`.

## 6. Files

| File | What | Made by | For |
|---|---|---|---|
| **cut** | lossless stream copy (`-c copy`) of C's own samples into `.mp4`, source codec | **the NAS**, seconds after the splice | download (*original*), distributions, materialising |
| **playback render** | H.264 of the clip's range, only when the source codec will not play in a browser — `…~k3fa@12.5-40.play.mp4` | desktop `process` | playing in the app; what a viewer is given |
| **thumb / preview** | as for any video | desktop | grid, preview |
| **filmstrip** | one sprite per video: up to 40 frames, 90px tall, evenly across it, each its own keyframe seek; a JSON beside it says how many | desktop `process`, into the `strip` tier | the splice page's timeline, which shows as many as fit at 64px or wider — more as it zooms |
| **still** | JPG, top quality, 4:4:4, full resolution, orientation applied, HDR tone-mapped, dated as the frame was taken — `…~p9q2@3.5.still.jpg` | desktop `process`, from the **master** | everything a photo is for |

**One video format and one photo format in the library, however an item was
made.** A `.MOV` source cuts to `.mp4` (a lossless container change); a still
is JPG even beside a HEIC.

**A cut starts on a keyframe.** Stream copy cannot begin between keyframes.
The splice page snaps in-points to keyframes, so the stored in-point *is*
where the file starts, in every player — rather than relying on an MP4 edit
list that some players ignore. Out-points are frame-exact. Stills can be any
frame, since they are decoded anyway.

**The NAS may stream-copy; it still never encodes**
([nas-app.md §10](nas-app.md#10-hardware)). A copy neither decodes nor
encodes, so the Atom's missing AVX is irrelevant. The app image gains static
`ffmpeg` and `ffprobe`, and a guard admits only `-c copy`, so nothing can drift
into encoding on the Atom. `ffprobe` reads keyframe positions from the file's
index for the timeline.

**The cut is stamped as it is written**, by ffmpeg — `pix:ClipId`, its range
as `pix:ClipRange`, `pix:SourceFile`, and `creation_time` as the source's own
QuickTime clock plus where the clip starts. This departs from
[§5](nas-app.md#5-renders)'s *renders carry no pix metadata*, deliberately: a
cut copies C's container metadata and would otherwise claim C's start time.
**A date override is not baked into the cut** — it is a piece of the source
as recorded, and overrides are applied to what is delivered (§7), as for any
file. So correcting C's date moves its clips' rows, and never re-cuts them.
Stills are stamped with exiftool on the desktop.

**A cut is named by its range** — `IMG_C.MOV~k3fa@12.5-40.cut.mp4` in the
render tier — so one made for a range that has since moved is stale by name
alone, with no stamp to read and nothing left pointing at it after a crash.
A clip's row records the size of the file a viewer would be given (its
playback render, or its cut where the source's codec plays in a browser), and
that size is what lets a viewer see it.

### Staleness is keyed on the range

| Change | cut | desktop files |
|---|---|---|
| range (drag, split, merge) | re-cut at once | deleted at once; `process` remakes |
| C's date | untouched — the override is applied on delivery | — |
| tags, people, audience, event | untouched | untouched |

Not on timestamps: a sidecar changes with every tag, and *sidecar newer than
render* would re-encode a clip every time someone is tagged in it. A cut's
name carries the range it was cut from, so a mismatch is stale on sight; the
app, starting, schedules any living clip whose cut is missing.

Stale files are deleted rather than left until replaced: an old render shows
footage no longer in the clip. The re-cut is debounced a few seconds after the
last gesture, so dragging an edge does not cut at every position.

## 7. Access and delivery

**Viewers see a clip only once its files exist.** Before then it can only play
as C clamped to a range in the browser, which would hand a viewer all of C — an
HTTP range cannot be limited to a stretch of time. Curators preview that way,
since they may see C anyway; for viewers an unready clip is a file that does
not exist yet. Sharing a clip never shares C.

**A clip shows its source only to someone who may see the source** — an
administrator, or a viewer the source is shared with. To anyone else a clip
is simply a video: no *Clip* badge, no *cut from* link in its details, and no
picture of the source standing in for its own. Each of those would say there
is more footage than they were given, and the picture would show them some
of it. So a clip's own pictures are made by `process` from a frame inside
the clip — a tenth of the way in, or a still's own frame — and until they
exist such a viewer sees none. For someone who may see the source, the
details link to it, and for the administrator to its timeline as well.

| Consumer | whole video | clip |
|---|---|---|
| download, *original* | master | cut |
| download, default | render if any, else master | playback render if any, else cut |
| distribution copy | render where one exists, else master | the same substitution |

**A delivered clip's content can change**, which [§7](nas-app.md#7-distributions)
said never happens. When a range changes, its copy is removed from the tree
until the new cut lands, then replaced **in place, under the same canonical
name**, so anything that refers to it by path keeps the entry. Canonical
names come from the clip's effective date, so a video's clips arrive in order.

**Round trips** resolve by the stamp: `pix:ClipId` beside `pix:SourceId`,
`pix:ArtifactId` and `pix:SourceFile`. Without it, a returning cut looks like
an edited copy of C and imports as a new photograph.

**Dedupe** excludes clip↔source and clip↔sibling pairs from perceptual
matching — their relation is a fact, not a guess.

## 8. What can be spliced

| Source | |
|---|---|
| H.264 | yes |
| HEVC with a render | yes — the page plays the render and cuts the master; timestamps match, keyframes are the master's |
| HEVC, unprocessed | disabled, *waiting for processing* |
| `.insv` / `.insp` | no — nothing to play |
| Live Photo `.MOV` | yes, no special case; stills are the useful part |
| a clip | opens its **source's** splice page — clips of clips never exist |
| a materialised clip | an ordinary video, so it is a source of its own |
| a video in a stack | no (§4) |

## 9. The splice page

Video above, timeline below: clip bars (gaps where there is none), still pins,
faint keyframe ticks where an in-point can snap, the filmstrip, the playhead.
The selected clip loops. Long videos zoom — pinch, or ctrl+wheel — and scroll
with the playhead kept in view. Layout follows size and input, never platform.

**Two tools, and nothing placed at random.** A single marker gesture could
not say what it meant: a mark beside a clip might extend it or start
another, and one inside a clip might split it or trim it. So:

- **New clip** (N): drag across the timeline, or set its start and end at
  the playhead (I, O).
- **Edit clip**: click a clip, then drag its ends or set them at the
  playhead (I, O), **Split here** (S), **Join next** (J), **Delete**.

A clip made or moved over others **wins**: one it overlaps is trimmed back to
its edge, one it covers is removed, one it lands inside is split around it.

**A draft, saved by identity.** Nothing is written until **Save**; **Discard**
throws the changes away, **Undo** (Ctrl+Z) steps back, and leaving with
unsaved changes asks. Every clip carries who it is through the edit — a
trimmed clip is the same clip however often its ends moved, a split part
says which clip it copies, a join says which clip it absorbed, a delete
names the clip — and `/api/clips/save` applies exactly that: no inference
from where clips ended up, and one History entry for the whole save.

**It asks whenever a file is made or removed**, and never for moving or
trimming: Split, Join and Delete each confirm, and so does a new or moved
clip that would remove or split another. A new clip in uncut footage, like
Take photo, is itself the request, and does not ask. Deleted clips go to the
bin; joined ones merge into the survivor. Archive original is not part of the
draft: it is a decision about the video, and is immediate.

This replaced two earlier versions. In the first every gesture wrote at once
and Split cut whatever was under the playhead; in the second, markers made
the clips — which read naturally until a click near a clip could mean two
things.

| | mouse / touch | keyboard |
|---|---|---|
| make a clip | **New clip**, then drag, or Start/End here | `N`, then `I` `O` |
| edit a clip | click it: drag its ends; Start/End here; Split here; Join next; Delete | `I` `O`, `S`, `J`, `Delete` |
| save / throw away / step back | **Save** · **Discard** · **Undo** | `Ctrl+S`, `Ctrl+Z` |
| take a still | **Take photo** | `P` |
| move the playhead | click the timeline; **drag across the picture**; tap it to play | space, `,` `.` a frame, shift for a keyframe |
| speed | the speed dropdown | `[` `]` |
| archive the original | **Archive original** (lit while archived) — immediate | `H` |

Stills are frame-exact through `requestVideoFrameCallback`; `currentTime` is
not.

The page only splices. Tagging a clip happens in the grid; each bar links to
its clip. Keyframes are read from the master's packet flags (`ffprobe`), so
ticks appear once the NAS has read them.

## 10. Build order

1. `archived` (then `hidden`), and deferring video stacks — each useful on its own.
2. The clip model: sidecars, index rows, date, copy-at-creation, the bin dialog.
3. The splice page, previewing by clamping C.
4. NAS cuts, and the image rebuild.
5. Desktop: playback renders, stills, filmstrip.
6. Round trips — built. Distributions — in the backlog, with the delivery
   trees themselves: a clip goes in like any video, its own date written into
   the copy, replaced in place under the same name when its range changes.

**Vocabulary:** code comments that say *clip* for any video are reworded, so
the word means only this.
