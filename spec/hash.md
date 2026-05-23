# Hash

`pix hash <library-root>` populates the **per-file content-hash cache** at `<library-root>\.pix\cache\<absolute-path-mirror>\<filename>.hash` for every file missing or stale. It does not touch file metadata. It does not invoke ExifTool. Its only outputs are tiny JSON cache files.

## Why cache, not metadata

`pix:ContentHash` was originally specced as an XMP field on each file, written by migrate. That was wrong:

- **Hash is a derived fact about file bytes**, not user-curated data. It belongs alongside other derived state (the metadata snapshot cache, future face embeddings) rather than in the file's metadata payload.
- **Per-file ExifTool write is expensive at TB scale.** Writing the hash via `-overwrite_original` cost two round-trips per file (sidecar capture + write) — dominant in apply on a first-migrate pass.
- **Cache invalidation falls out for free.** A cache entry keyed by `(file_size, mtime_ns)` auto-invalidates the moment the file is modified by anything (CONVERT, external editor, restored backup). No manual "clear stale hash on CONVERT" logic needed in migrate.
- **The only consumer is `pix dedupe`,** which already reads from `.pix/`. Nothing outside pix wants this hash.

The trade is that the hash doesn't travel with the file if the user copies media to another machine without `.pix/`. Acceptable — `pix hash` on the destination recomputes, same cost as a fresh first-migrate of the new library.

## Scope

Library-wide. `pix hash <path>` resolves the library root (walks up from `<path>`, then PIX_ROOT, then CWD) and operates on every file under that root that's missing a valid cached hash.

Subfolder-scoped hash runs are not supported in v1 — the hash population strategy is "exhaustive over the whole library."

## Cache schema

One file per library file. Path mirrors the source:

```
<library-root>\.pix\cache\<absolute-path-mirror>\<filename>.hash
```

Contents (JSON, one tiny file):

```json
{
  "size": 4823641,
  "mtime_ns": 1729542183471829400,
  "hash": "5f8c2a...",
  "computed_at": "2026-05-23T15:32:01"
}
```

A cache entry is **valid** if `(size, mtime_ns)` match the live file. Otherwise it's **stale** — same as missing, treated as "needs hashing."

Atomic write: write `.hash.tmp` next to the target, fsync, rename over `.hash`. Same crash protection as the metadata cache.

## Format-aware hashing

The hash is **format-aware**: metadata sections are excluded so that TAG writes (which mutate the file's EXIF/XMP/IPTC bytes) don't invalidate the cache entry. Without this, every migrate run would invalidate every hash, and `pix hash` would have to re-run after every migrate.

- **JPEG** (`.jpg`, `.jpeg`) — skip every APPn segment (`0xFF 0xE0` through `0xFF 0xEF`, where EXIF/XMP/IPTC/Photoshop/ICC metadata lives); BLAKE3 the rest including the entropy-coded scan data. Handles `FF 00` escape and restart markers (`FF D0`–`FF D7`) within scan data.
- **MP4 / ISO BMFF** (`.mp4`, `.mov`, `.m4v`, `.3gp`) — walk the box tree; BLAKE3 only the concatenated payload of every top-level `mdat` box. Everything else (`ftyp`, `moov`, `udta`, `meta`, `uuid`, `free`, …) is structure or metadata and is excluded.
- **Other** — raw bytes (fallback). Rare in canonical libraries; JPEG/MP4 covers the policy.

Hash algorithm: BLAKE3-256, hex-encoded.

**TAG writes do not invalidate the hash by content,** but they **do** change the file's `mtime_ns` — so the cache entry goes stale and `pix hash` recomputes on next run. The recomputed value will be the same (format-aware hashing excludes the metadata bytes that changed). This is wasted work; the alternative is keying the cache on a metadata-stripped digest or trusting some other freshness signal, both more complex than just running `pix hash` after big migrate batches. Future optimization, not a v1 concern.

## Workflow

Three phases, no editable plan:

1. **Acquire library lock** (see [README.md → Concurrency](README.md#concurrency)). Fail fast if another pix op is running.
2. **Allocate run folder.** Create `<library>\.pix\runs\<run-id>\` with the standard timestamp. Contains `apply.log` only. No `plan.txt` (the work set is mechanical — every file with a missing or stale cache entry); no `data/` (hash writes are purely additive and live in cache; conservation doesn't apply).
3. **Discover.** Walk the library; for each file, check the cache. Collect the "needs hashing" set. Print `N files need hashing.` and prompt `Proceed? [Y/n]` (default Y; no edit option — there's no per-file decision to make).
4. **Apply.** Sequential, one file at a time. For each file: compute the format-aware hash, write the cache entry atomically. Log Started / Completed / Failed to apply.log.

Sequential in v1. Hashing parallelizes cleanly (per-file independent, no shared mutable state) and is the obvious candidate for a future worker-pool pass; see `spec/perf-backlog.md`.

## Apply per file

1. `stat()` the file to capture `(size, mtime_ns)` for the cache entry.
2. Open file, compute format-aware BLAKE3.
3. Write cache entry: `.hash.tmp` + fsync + rename.
4. Append `Completed` line to apply.log.

No ExifTool. No file mutation. No conservation capture. The only on-disk artifacts are the new `.hash` cache file and the apply.log line.

## Console output

Same policy as migrate / dedupe / organize:

- **During discovery:** single rewriting `NN% - hash-scan <path> (Xs)` line. Phase headers and per-file decisions go to a plan.log in the run folder.
- **During apply:** single rewriting `NN% - L042 HASH <path> (Xs)` line.
- **After apply:** `Hashed N file(s) in <duration>.` (Or `Hashed N file(s); M failed — see <apply.log>.` if anything failed.)

`(Xs)` follows the tiered duration format defined in [migrate.md → Duration format](migrate.md#duration-format).

Errors print to stderr.

## Failure handling

A hash-write failure on one file does **not** halt the run. The file is logged to `apply.log` as `Failed` with the error; the loop continues to the next file. At end of run, the summary prints the failure count and exits non-zero if any failed.

This differs from migrate (which halts on first failure) because hash failures are non-blocking — the file is still usable, just absent from the dedup index. The next `pix hash` run retries.

Hard timeouts (per [implementation.md → Subprocess hardening](implementation.md#subprocess-hardening)) on the hash compute still halt the run if a single file pathologically wedges. That's the "something is broken" signal; non-fatal failures are normal-permissions / file-vanished errors.

## Idempotence

A library where every file has a valid cache entry produces an immediate no-op (`0 files need hashing.` exits cleanly). Re-running with no changes produces no work.

## Concurrency

Acquires the library-wide lock at `<library>\.pix\lock` for the duration of the run. See [README.md → Concurrency](README.md#concurrency).

## Conservation invariant

Hash writes replace nothing — no prior cache entry, or a stale one that's recomputable from the file's current bytes. There's no preserved data, no `data/` subfolder, no rollback storage. Rollback of a `pix hash` run is "delete the affected `.hash` files from cache" — trivially recomputable. This is the only pix write op that legitimately needs no conservation capture; documented as a vacuous case of the [README.md cross-cutting invariant](README.md#cross-cutting-invariants).

## When to run

There's no enforcement. Recommended usage:

- After a `pix migrate` of a fresh library, before the first `pix dedupe`.
- Periodically, or whenever `pix dedupe` refuses with "files missing hashes" (it points the user here).
- As a long-running background task; restartable, so killing and resuming is cheap.

Migrate does not auto-invoke or mention hash. The two operations are orthogonal: migrate handles per-file in-place normalization, hash handles cache population.
