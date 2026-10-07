# Performance ideas

Optimization ideas against already-built code paths — each an idea + rough
impact + rough effort, not designed in detail. Land them when there are real
wall-clock numbers from large runs to attack. Each notes the file it'd touch.
These are *performance* refinements; designed-but-unbuilt *features* live in
[roadmap.md](roadmap.md).

Ideas against the old CLI pipeline (apply-phase parallelism, plan-gen, the
TAG round-trip, streaming content hashing) went with it on 2026-10-07; they are
at commit `e1f3853`.

### `pix process` with nothing to do (~16s, measured 2026-10-01)
The index tail is ~1s now (`index.update`); the rest is the run's own overhead
over SMB, which matters once `process` sits on a timer:
- `sweep_partials` — ~8s: `rglob` for temp markers over every derived tier on
  every run. Could run only after a run that did not finish cleanly (a marker
  written at start, removed at clean exit).
- `pending_files` — ~8s: seven listings per master folder. Folders whose
  master and tier listings are unchanged since the last run (directory mtimes)
  could be skipped.
