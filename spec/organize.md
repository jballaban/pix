# Organize (sketched)

`pix organize <template>` physically rearranges files in the library to match a folder-shape template. It's the only operation that moves files between folders — [migrate](migrate.md) is in-place per-file; this is structural.

Design TBD. Sketch of what it needs:

- **Single-valued tags only.** A file has exactly one physical location, so templates can only reference single-valued tags (`date` and its derived components `{year}`/`{month}`/`{day}`/`{time}`, plus `event`). Multi-valued tags (`person`, `face`) belong to [checkout](tag-editing.md) and [export](export.md). See [tags.md](tags.md#single-valued-vs-multi-valued).
- **Same plan/edit/confirm flow as migrate.** Generate a plan, open in `$EDITOR`, show summary, `Apply? [y/N]`. Conservation captures any displaced state under `.pix/runs/<run-id>/` (see [library.md](library.md#file-layout)).
- **Idempotent.** A library already in the target template shape produces an empty plan. Re-running with no input changes produces no work.
- **Triggered by tag changes.** `commit` (see [tag-editing.md](tag-editing.md)) re-organizes the library automatically if a changed tag is part of the active template. Standalone `organize` is for switching templates or repairing drift.
- **Template grammar** is shared with checkout/export; see [tags.md](tags.md#template-grammar).
