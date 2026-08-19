# Export (sketched)

`pix export <template> <out-path>` produces a read-only, derived view of the library at a separate location — copies or hard links, shaped by a template. Used to ship a curated subset (e.g. `{year:2023}/{event}/`) to an external location without disturbing the canonical library.

Design TBD. Sketch of what it needs:

- **Read-only.** Export never edits the library or the source files' metadata. Failure to write an export entry never affects library state.
- **Copy vs link.** Default to hard links when `<out-path>` is on the same volume; copy when cross-volume. User-overridable per invocation.
- **Multi-valued tags work.** Each (file, tag-value) pair produces one entry in the output (one hard link or copy). `{person}` or `{face}` in templates produces a folder per identity.
- **Filter semantics.** Files excluded by an explicit filter just don't appear in the output (no `(filtered)/` folder), unlike [organize](organize.md) and [checkout](tag-editing.md) which must account for every file. See [tags.md](tags.md#folder-categories-per-operation).
- **Template grammar** is shared with organize/checkout; see [tags.md](tags.md#template-grammar).

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
