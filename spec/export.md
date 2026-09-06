# Export

**Status: implemented** (v0.1.208) — `pix export [<name>]` reconciles the named
delivery distributions configured in `pix.yaml`. The one part still outstanding
is the H.264 compatibility rendition for video (see the BIG TODO below); until
it lands, video is copied as-is, so an export can contain HEVC that some
clients won't play.

Code: `pix/export.py` (engine), `pix/export_manifest.py` (manifest),
`pix/commands/export.py` (command), `exports:` parsing in `pix/config.py`.

`pix export` produces a read-only, derived view of the library at a separate location — real byte copies, shaped by a template. Used to ship a curated subset (e.g. `rating:4,5` into `{year}/{event}/`) to an external location without disturbing the canonical library.

## Why export is a separate tier — master vs delivery

pix's library is the **master** tier: organized, deduped, tagged, and kept at the
best fidelity that's practical (fidelity-first video is remux-only; images are
JPG-normalized). It is **not** a raw archive — first migrate lossily normalizes
images (HEIC/PNG/DNG → JPG) and soft-deletes originals to prunable run folders —
and it is deliberately **not** a universally-compatible delivery copy either
(library video is codec-mixed: legacy HEVC + native H.264/MPEG-2).

**Export is the delivery tier.** It is where the two things the master
intentionally *doesn't* do happen: **curation** (a hand-picked subset, e.g. by
rating) and **compatibility** (lossy renditions like H.264 that would be wrong to
force onto the master). Lossy transforms are legitimate here precisely because an
export is derived and disposable — it never touches master bytes.

Consequence, decided with the owner: the master is consumed as the organized
best-fidelity copy; anything shared or viewed on compatibility-sensitive
clients (Synology Photos, phones, TVs) goes through an export. This is why
**library-wide H.264 is rejected** (it would collapse delivery concerns into the
master, reversing [video-redesign.md §0](video-redesign.md)) and why the H.264
requirement below is **export-scoped**.

Invariants:

- **Read-only.** Export never edits the library or the source files' metadata. Failure to write an export entry never affects library state.
- **Copy only — no hard links.** An export is always a real byte copy, even when the target is on the library's volume. Hard-linking was considered and rejected: (1) it saves no *upload* (a sync client uploads content, not inodes), only local disk, which is negligible for curated subsets; (2) it welds the delivery copy to a master that pix deliberately mutates (TAG/CONVERT/organize rewrite files via write-temp+rename, which breaks the link); (3) re-encoded video (H.264) is new bytes with nothing to link to anyway. A copy is independent, so the master can change underneath it freely.
- **Multi-valued tags work** — *designed, not built*. Each (file, tag-value) pair would produce one entry in the output. `{person}`/`{face}` don't exist yet (face detection is deferred), so today every template token is single-valued and one file yields exactly one entry.
- **Filter semantics.** Files excluded by an explicit filter just don't appear in the output (no `(filtered)/` folder), unlike [organize](organize.md) and [checkout](tag-editing.md) which must account for every file. See [tags.md](tags.md#folder-categories-per-operation).
- **Template grammar** is shared with organize/checkout; see [tags.md](tags.md#template-grammar).

## Distributions: named config

A distribution is a **standing, named** delivery target that gets re-provisioned regularly (not a one-off invocation). They live under an `exports:` section in the library's [`pix.yaml`](library.md), each a `(path, filter, template)` triple:

```yaml
exports:
  general:
    path: 'D:\SynologyDrive\Photos-General'
    filter: 'rating:3,4,5'
    template: '{year}/{event}'
  top:
    path: 'D:\SynologyDrive\Photos-Top'
    filter: 'rating:5'
    template: '{year}/{event}'
```

- **`pix export <name>`** reconciles that one distribution; **bare `pix export`** reconciles them all (the "reprovision everything after a curation session" gesture).
- **`filter` is separate from `template`.** `filter` selects *which* files (a template-grammar filter expression, typically over `rating` — see [tags.md](tags.md#single-valued-vs-multi-valued)); `template` shapes the *output folders*. Keeping them separate is what lets `filter: rating:5` select the top tier **without** materializing a `5/` folder in the output. Files failing the filter simply don't appear (no `(filtered)/` folder — see the folder-category table in [tags.md](tags.md#folder-categories-per-operation)).
- **Curation drives it.** The `rating` tag ([tags.md](tags.md#rating-curation-standard-field)) is the selection signal; tiers nest for free (`rating:5` ⊂ `rating:3,4,5`). A photo rated in pix (folder-shuffle checkout) or in an external tool (Windows/Lightroom/Synology Photos) flows into the next `pix export`.
- **Overlap is duplicated bytes, by design.** Because tiers nest, a `rating:5` photo physically exists in both the `top` and `general` trees. Accepted (each distribution is an independent, independently-syncable tree); if a drive fills up, add another and point a distribution's `path` at it.

## Provisioning: copy + delta reconcile (never wipe-and-redo)

Re-provisioning is a **reconcile**, not a re-copy. Each run computes the desired set `{target-relpath → source-content-hash}` from the distribution's filter+template, diffs it against what's already provisioned, and touches **only the delta**:

- **new** member → copy in
- **dropped** member (rating fell below the tier) → delete from target
- **changed** member (source bytes differ) → replace
- **unchanged** → *never touched* — this is the whole point: unchanged files never re-upload on the Synology side.

**Path churn is the one unavoidable re-upload.** If a member's effective `event`/`year` changes, its target relpath changes, so reconcile does delete-old + copy-new = one file re-uploaded. Inherent to a template-shaped mirror; rare for curated files, and scoped to that file.

**Diff cheaply via a master-side manifest.** Hashing the (possibly network-backed) target every run is slow, so each distribution keeps a manifest at `.pix/local/exports/<name>.json` (recomputable state, sync-excluded like the cache/lock — mirrors [`.pix/local/cache.db`](implementation.md#cache-store)). Each row is `target-relpath → source content hash + the target's size and mtime_ns as written`. That fingerprint is deliberately the same `(size, mtime_ns)` key the cache uses, which makes verifying the whole target **one `stat` per member** — no bytes read back. The manifest also records the target path, so a repointed distribution is detected rather than trusted.

**Identity is the content hash**, which is metadata-invariant for JPEG/MP4. A tag-only edit in the master therefore does *not* re-ship the file — sync traffic tracks real content change. The cost is that a delivered copy's embedded tags can lag; membership changes (what a re-rating usually causes) still flow through, because they change the desired set.

### Target validation — the rule that makes removal safe

> **Export only ever touches paths its own manifest records.** Everything else in the target is foreign: reported, never modified, never deleted.

Without that, a mistyped `path:` pointed at a real photo folder would be a data-loss event. With it, the worst case is a confusing report. Before planning, one walk of the target plus a stat per member classifies:

| State | Meaning | Response |
|---|---|---|
| in sync | stat matches the manifest | untouched — the point of the whole design |
| missing | ours, gone from the target | re-COPY (additive), reported |
| modified | ours, changed under us | **drift** |
| foreign | never ours | **drift** — never touched |
| stale | ours, no longer in the desired set | REMOVE |

NAS/sync artifacts (`@eaDir`, `#recycle`, `desktop.ini`, …) are skipped, not counted as foreign — they'd otherwise stop every run against a Synology target.

**Three tiers of ceremony:**

1. **Purely additive** (new members, re-copies of missing ones) — normal: prompt interactively, silent under `--no-prompt`.
2. **Removals / replacements the manifest fully explains** — prompt as usual, with removals broken out in the summary and sorted **first** in `plan.txt`. `--no-prompt` covers these: a curation session produces removals every time, so blocking them would make scripted export useless.
3. **Unexplained drift** (modified or foreign) — **hard stop**: describe and exit non-zero *even under `--no-prompt`*. pix cannot distinguish a hand-curated target from a half-landed sync from a misconfigured path, and that ambiguity is where the damage lives.

**Path churn renders as `MOVE`, not REMOVE+COPY** — same content hash, new relpath, paired into one line when unambiguous. "Deleted 400, added 400" is exactly the alarming shape a reviewer shouldn't have to decode. (Honest caveat: on a sync client a move within the tree still re-uploads; MOVE saves re-reading the master, not the upload.)

**Lost manifest → adopt by hash, not a full re-provision.** Re-provisioning into a tree we don't own would leave every existing file foreign and land duplicates beside them. Instead pix hashes the target files sitting where desired members belong and adopts the ones that match; anything else stays foreign and surfaces as drift. One-time cost, only on manifest loss or a repointed target.

**Copies land on a `.__export__` temp and are renamed into place**, so an interrupted copy never leaves a plausible-looking partial — and the marker infix means a sync client excluding `*.__*` won't upload the partial either.

**Read-only w.r.t. the master.** Nothing in provisioning reads-modifies-writes a library file; the master is a pure source. Failure to write a target entry never affects library state (the top-of-file read-only invariant). Removals from the target are plain deletes — the export is derived and disposable, so the [conservation invariant](README.md#cross-cutting-invariants) (soft-delete to run folders) applies to the *master*, not to a regenerable delivery mirror. Removals are logged, not staged.

## BIG TODO — video must ship as H.264 (compatibility rendition)

**Requirement.** Videos placed into an export must be **H.264 (AVC) in MP4**.
H.264/MP4 is the universal-playback floor — every browser, phone, TV, and NAS
(incl. Synology Photos) plays it with **no server-side transcoding**. The whole
point of an export is a shareable, drop-in-and-it-plays copy; depending on the
consumer's NAS/device to transcode HEVC is exactly the fragility we want gone.

**Why this is not automatic today.** The library standardizes the *container*
(→ MP4) but **not the codec** — `convert_to_mp4` is a lossless `-c copy` remux
(see [video-redesign.md §0](video-redesign.md)), so library video is a **mix of
codecs**: legacy HEVC from the retired v0.1.122 transcode era, plus native
camera codecs (H.264, HEVC, MPEG-2 from old `.mts`/`.vob`/`.mpg`) carried through
untouched. HEVC clips may **fail to play** in Synology Photos / shared clients
unless the NAS happens to support hardware HEVC transcoding.

**Scope: export only — NOT the library.** Converting the *library* to H.264 is
explicitly **rejected**: it reverses the remux-only fidelity decision settled
2026-06-07 ([video-redesign.md §0](video-redesign.md)), reintroduces lossy
re-encoding (the exact class of harm that redesign killed), and roughly doubles
storage. The library stays lossless-first (remux-only); the **export** is where a
compatibility re-encode is legitimate, because it's a derived, disposable copy
that never touches source bytes — consistent with export's read-only invariant.

**Design points to resolve when this is built:**

- **Only re-encode what needs it.** An export member already H.264-in-MP4 is
  copied as-is (no re-encode); only non-H.264 (HEVC, MPEG-2, …) members get
  transcoded to H.264. Probe codec (ffprobe, already cached) to decide.
- **Cache the rendition — don't re-encode every provision.** Export reconcile
  is incremental (delta-only); an H.264 rendition of a given source (keyed on
  content hash) must be produced once and reused across runs, or every re-provision
  re-transcodes. Where the rendition cache lives (main-side `.pix/local/`, keyed
  by source hash) is TBD.
- **Encoder + quality.** libx264 CRF target (visually-lossless-ish, e.g. CRF
  ~18–20) vs. hardware h264_nvenc for speed. Note the retired GPU pipeline's
  rotation bug ([video-redesign.md §2#1](video-redesign.md)) — any encode path
  must bake rotation correctly (CPU autorotate, or NVENC with autorotate
  preserved). Preserve `pix:*`/EXIF/rotation across the re-encode.
- **This is the one place transcode re-enters pix.** It resurrects a slice of
  the encode surface (`convert.py` re-encode branch, encoder selection) that
  video-redesign deliberately retired — scoped to export, gated behind it, and
  never run against the library. Keep it isolated so it can't leak into migrate.

Until built, the interim reality is: **exports may contain HEVC that some clients
can't play.** That gap is the reason this is a big TODO, not a nice-to-have.
