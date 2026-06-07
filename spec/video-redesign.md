# Video handling — problem catalog & redesign directive

> **Purpose.** This is the seed document for redesigning how pix handles videos.
> The current approach (transcode everything to HEVC for space) has caused
> repeated, serious data-fidelity problems. The owner wants to **stop
> re-encoding and go back to remux-only**, solving storage some other way. This
> file captures the full context so a fresh session can pick it up cold.
>
> **Status:** directive / not yet designed or implemented. Confirm scope with
> the owner before ripping anything out. Nothing here is decided except the
> overall direction (drop transcode → remux-only).

---

## 1. Current architecture (what exists today)

**Canonical video codec = HEVC** (introduced v0.1.122). `pix migrate` converges
every video to HEVC-in-MP4:

- `src/pix/config.py` `EXTENSION_POLICY`: `mp4`/`m4v` = `keep`,
  `mov`/`avi`/`mts`/`mpg`/`mpeg`/`vob` = `convert_to_mp4`, `insp`/`insv` = `keep`
  (Insta360, never touched). "keep" is about the **container**; the **codec**
  policy still re-encodes non-HEVC (e.g. an H.264 `.mp4`) to HEVC.
- `src/pix/convert.py` `convert_to_mp4(src, dst, encoder, profile)`:
  - source already HEVC → **remux** (`ffmpeg -c copy -map_metadata 0
    -movflags +faststart`) — lossless rewrap.
  - otherwise → **re-encode** to HEVC via one of two encoders.
- **Hybrid CPU+GPU encoding** (v0.1.130): `ENCODER_X265` (libx265, CPU, CRF 22,
  `-preset medium`) and `ENCODER_NVENC` (hevc_nvenc, GPU, full-CUDA pipeline
  `-hwaccel cuda -hwaccel_output_format cuda`, preset p7, cq ~30).
  - `src/pix/apply.py` `_route_encoder()` (~line 120) + a GPU-slot pool
    (~lines 271–322) route 4K / CPU-overflow clips to the GPU; NVENC has a
    ~3× concurrency ceiling; on NVENC failure it falls back to x265.
- Caches per video: `.hash` (content hash, mdat-only), `.video` (ffprobe
  codec/profile/pix_fmt), `.vfp` (perceptual dHash fingerprint for dedupe).
- Box: AMD 9950X + RTX 5090. Library is single-user, **TB-scale, HD-heavy**.
- **Why it was built:** space savings on a large HD/4K library.

Relevant memories: `project_hevc_canonical_video`, `project_hybrid_gpu_encoding`,
`project_perceptual_dedupe`, `project_reconsider_reencode`.

---

## 2. Problem catalog (why we're reconsidering)

1. **NVENC silently dropped rotation (the worst).** The full-CUDA pipeline keeps
   frames on the GPU, so ffmpeg's autorotation (a CPU filter) was skipped and a
   rotated source's orientation was lost entirely — clips came out upside-down /
   sideways. **~1,126 library files were damaged.** Diagnosed and proven: CPU
   path bakes rotation; remux preserves the matrix; only NVENC re-encode broke
   it. Fixed forward in **v0.1.161** (rotated clips drop `-hwaccel_output_format
   cuda` so autorotate runs). Existing files recovered via a lossless
   rotation-tag-add (`pix rotate`, v0.1.162) using conserved originals to
   determine the angle.
2. **Re-encoding is lossy and irreversible.** Every transcode loses quality;
   re-running migrate/sync once re-encoded the whole H.264 library to HEVC
   (huge, lossy, conserved originals only in run folders).
3. **CPU vs GPU output diverges.** Different encoders → slightly different
   output and (as the rotation bug showed) different *behavior*. Mixed archive.
4. **Metadata fragility.** Re-encode/remux drops things: a remux/`-c copy
   -map_metadata 0` drops the `pix:*` XMP namespace (we had to re-apply via
   exiftool during the rotation recovery). Rotation matrices, side data, etc.
   are easy to lose across a transcode.
5. **Expense & churn.** Perceptual fingerprinting (`.vfp`) of videos is costly;
   cache leaks made it re-fingerprint the whole library (fixed v0.1.147). The
   transcode pipeline (prefetch pool, GPU routing, NVENC ceiling, fallback) is a
   large, bug-prone surface.
6. **Dedupe is no longer a pure hash compare (a transcode-induced kludge).**
   Exact byte-hash dedupe stopped catching duplicate *videos* because
   re-encoding the same source with different encoders/versions (x265 vs NVENC,
   or across runs) produces byte-different HEVC. So we added **perceptual video
   dedupe**: a dHash fingerprint (`.vfp`) sampled at fixed fractional
   timestamps, grouped within a Hamming **band [0, 30]** (same-resolution only;
   cross-res deferred), with a `--checkout`/`--commit` review gate (v0.1.132–134;
   see `spec/dedupe.md`). Images still dedupe by exact content hash; videos use
   this fuzzy "partial" comparison. It's extra complexity *and* a recurring
   cost/churn source (`.vfp` re-fingerprinting), and it exists almost entirely
   to paper over transcode non-determinism. **Under remux-only the rationale
   largely evaporates:** a lossless remux preserves the mdat bytes, so two
   imports of the same source are byte-identical again and exact `.hash` dedupe
   works — perceptual matching would only be needed for copies re-encoded
   *outside* pix.
7. **Undated `_000000_NNN` clusters.** Many videos have no real date → all
   canonicalize to the same midnight timestamp and pile up with suffixes;
   collision re-suffixing churns on every organize. (Not caused by transcode but
   tangled with video handling.)

---

## 3. The decision / direction

**Drop video re-encoding. Go back to remux-only.** Rewrap the source container
into MP4 with `-c copy` (codec preserved, lossless, fast, metadata/rotation
intact). This eliminates the entire transcode surface — and with it the
rotation class of bug, the CPU/GPU routing, NVENC, generational quality loss,
and most of the fragility above.

**Handle storage "in a different / better way" — TBD, but NOT lossy transcode.**

---

## 4. Open questions to resolve in the design pass

- **Canonical policy under remux-only.** Keep source codec, rewrap to `.mp4`?
  What about already-`.mp4` H.264 — leave as-is (no codec convergence)? Likely
  yes: remux only normalizes the *container*, never the codec.
- **Un-remuxable sources.** Some codecs can't `-c copy` into MP4 (e.g. certain
  AVI/DivX/MJPEG, old formats). Policy: keep original container? a rare,
  explicit re-encode? Need a fallback rule and how it's surfaced.
- **Storage strategy** (replace the transcode space win). Brainstorm: accept
  it (storage is cheap); cold/tiered storage; opt-in per-file transcode for a
  few huge outliers; better dedupe; lossless container optimization. Owner
  said "a different / better way."
- **Existing already-HEVC library + one-off migration.** pix is the *only*
  tooling for this library, so the redesign must include a migration plan for
  what's already there. The full measured state and the A/B/C migration options
  are in **§8** — settle that alongside the storage strategy.
- **Dedupe — can we drop the perceptual path?** It was added only because
  transcode made same-source copies byte-different (see catalog #6). With
  remux-only, exact `.hash` dedupe catches true byte duplicates again, so the
  fuzzy dHash-band comparison (and the `.vfp` cache, the `--checkout`/`--commit`
  review gate, the band tuning) may be removable — a big simplification.
  Counter-argument: it still catches copies someone re-encoded *outside* pix.
  Decide: keep perceptual as optional, or remove `.vfp`/perceptual entirely and
  return dedupe to pure exact-hash.
- **Rotation.** Remux preserves the rotation matrix (verified), so the rotation
  bug simply disappears under remux-only — `pix rotate` remains as the manual
  fix-up tool.

---

## 5. Code & spec map (where to work)

- `src/pix/convert.py` — `convert_to_mp4` (remux + re-encode branches),
  `VideoProfile`, `probe_video_profile`, `nvenc_available`, `_video_rotation`,
  `ENCODER_X265`/`ENCODER_NVENC`, `is_canonical_video_codec`.
- `src/pix/apply.py` — `_route_encoder`, the GPU-slot prefetch pool (the CPU/GPU
  concurrency machinery to retire).
- `src/pix/config.py` — `EXTENSION_POLICY` (the `convert_to_mp4` actions).
- `src/pix/video_cache.py`, `vfp_cache.py`, `video_fingerprint.py` — caches.
- `src/pix/commands/rotate.py` — the lossless rotation tag-add (keep).
- Tests: `tests/test_convert.py`, `tests/test_gpu_routing.py`,
  `tests/test_video_cache.py`, `tests/test_vfp_cache.py`,
  `tests/test_dedupe_perceptual.py`.
- Specs: `spec/migrate.md` (Canonical video codec section), `spec/dedupe.md`,
  `spec/implementation.md`. Update these when the policy changes.

---

## 6. Invariants to respect

- **Conservation** — never destroy data without a capture (run folders).
- **Canonical filename** + **same-volume** invariants (see `spec/library.md`).
- **Version bump** per behavior change; **reinstall** editable after commits.
- Migrate is **in-place**; only organize moves files.
- Don't break the `pix:*` tag model or the rotation tag-add recovery path.

---

## 7. How to use this doc (fresh session)

Read this file, then skim `src/pix/convert.py` and `src/pix/apply.py` (sections
2 & 5). Confirm the storage strategy, the un-remuxable fallback, and the
migration option (§8) with the owner before implementing. The end state: `pix
migrate` remuxes videos to MP4 (`-c copy`) and never re-encodes; the CPU/GPU
encode paths and `_route_encoder` pool are removed; storage is handled by
whatever strategy we agree on.


## 8. Current library state (measured 2026-06-07) & the one-off migration

**What's in the library now:**
- **9,676 videos, 100% HEVC** — the transcode is complete; no original-codec
  video remains in place (from the `.video` cache).
- **Mixed encoder, ~half/half:** a 200-file sample was **~54% x265 (CPU)** /
  **~46% hevc_nvenc (GPU)** (x265 leaves an SEI signature; NVENC doesn't). So
  quality/encode characteristics vary file-to-file across the archive.
- All of it is **lossily transcoded** from the originals.

**What's recoverable (conserved originals):**
- Run folders (`G:\pix\.pix\runs` + relocated `F:\.pix\runs`) hold **21,154
  conserved video captures**. A 300-file sample was **~59% H.264** (pre-transcode
  CONVERT-source originals) / ~40% HEVC (post-transcode dedupe captures) — so on
  the order of **~12k H.264 originals conserved**: a large fraction of the
  library is recoverable to its lossless source.
- **But not all.** The rotation recovery found **~1,694 current videos with no
  conserved original**. Matching original↔current is by canonical basename and
  is **unreliable for the undated `_000000_NNN` clusters** (false matches) — any
  migration must verify each pair (duration/content) before acting, exactly as
  the rotation recovery did.

**One-off migration options (for the remux-only target):**
- **A. Accept the current HEVC archive as-is.** Change only future imports to
  remux-only. Simplest; zero re-processing. Existing lossy/mixed-encoder HEVC
  stays — a permanent quality compromise, but done and stable. Conserved
  captures (21,154) become prunable dead weight.
- **B. Re-establish originals where conserved.** For each current video with a
  *verified* conserved H.264 original: **remux the original** (lossless
  H.264-in-MP4), transplant the `pix:*` tags + canonical name, replace the HEVC.
  Recovers lossless quality for the recoverable fraction (~80%?); the rest stay
  HEVC. Big, careful, verify-first batch (like the rotation recovery) with
  false-match guards. **Increases library size** (H.264 ≫ HEVC) — i.e. it brings
  back exactly the space the transcode was saving, so it only makes sense once
  the §4 storage strategy is settled.
- **C. Hybrid / opt-in.** Accept HEVC now; ship a tool to re-establish originals
  selectively (e.g. only NVENC-encoded ones, or only where it matters), over time.

**Linked:** option B's size cost is why the storage strategy (§4) must be
decided first. The 21,154 conserved captures are themselves substantial weight:
under A they're largely prunable; under B the H.264 ones get consumed into the
library, then the rest pruned.
