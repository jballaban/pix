# Sync

`pix sync <path> [<template>]` is the "do everything reasonable" action: it runs the four main plan-applying commands back-to-back, non-interactively.

```
migrate → hash → dedupe → organize
```

It exists so the common end-to-end workflow — normalize new files, hash them, drop duplicates, reshape the library — is one command instead of four with four prompts.

## Ordering

The order is fixed and not arbitrary:

1. **migrate** — normalize files in place (convert, rename, write `_auto` tags). Must run first so later steps see canonical files with `pix:*` metadata.
2. **hash** — populate the content-hash cache. Both dedupe and organize consume it (dedupe to find duplicates, organize as the collision tiebreaker), so it has to precede them.
3. **dedupe** — remove duplicate copies. Before organize, so we don't shuffle duplicates into place and then delete them.
4. **organize** — reshape the survivors per the template.

## Arguments

- `<path>` scopes the **migrate** step (migrate is folder-scoped, like `pix migrate <folder>`) and resolves the library root for the **hash / dedupe / organize** steps (all three are library-wide).
- `<template>` is optional and forwarded to the organize step. Omitted, organize re-applies the library's stored template (and errors — halting sync — if none is stored). See [organize.md → Active template persistence](organize.md#active-template-persistence).

## Non-interactive apply

Each step is invoked with `no_prompt=True` — the same capability exposed standalone as **`--no-prompt`** on `migrate` / `hash` / `dedupe` / `organize`. The `Apply?` / `Proceed?` confirmation is skipped and the generated plan is applied in full. **Plans are still written** to each step's `.pix/runs/<run-id>/` folder, and per-run `apply.log` / conservation captures are unchanged — so an unattended sync is just as recoverable/auditable as the interactive commands. The editor (`e`) path is interactive-only and unreachable under `--no-prompt`.

## Stop on first error

Each sub-command raises `typer.Exit` (non-zero) on failure; that propagates out of sync and **aborts the chain** rather than letting a failure get buried under later steps. Concretely this includes:

- a hard error in any step (lock held, unknown extension in migrate, missing template in organize, an apply failure);
- migrate finishing with **CONVERT failures** (files quarantined to `.pix/errors/`) — migrate exits non-zero in that case, so sync stops there. Fix the quarantined inputs (or accept their loss) and re-run.

"Nothing to do" outcomes are **not** errors — an empty plan in any step returns cleanly and sync proceeds to the next.

Each step still acquires and releases the library lock independently (sequential, no nesting) and prints its own version banner; sync adds a `===== pix sync [n/4] <step> =====` header before each.

## Not included

`dedupe` **is** included (it's the do-everything action). `checkout`, `export`, `merge`, `upgrade`, and `init` are not part of sync — they're interactive, derived-view, or one-time setup/maintenance operations outside the routine normalize-and-shape loop.
