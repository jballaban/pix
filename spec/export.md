# Export (sketched)

`pix export <template> <out-path>` produces a read-only, derived view of the library at a separate location — copies or hard links, shaped by a template. Used to ship a curated subset (e.g. `{year:2023}/{event}/`) to an external location without disturbing the canonical library.

Design TBD. Sketch of what it needs:

- **Read-only.** Export never edits the library or the source files' metadata. Failure to write an export entry never affects library state.
- **Copy vs link.** Default to hard links when `<out-path>` is on the same volume; copy when cross-volume. User-overridable per invocation.
- **Multi-valued tags work.** Each (file, tag-value) pair produces one entry in the output (one hard link or copy). `{person}` or `{face}` in templates produces a folder per identity.
- **Filter semantics.** Files excluded by an explicit filter just don't appear in the output (no `(filtered)/` folder), unlike [organize](organize.md) and [checkout](tag-editing.md) which must account for every file. See [tags.md](tags.md#folder-categories-per-operation).
- **Template grammar** is shared with organize/checkout; see [tags.md](tags.md#template-grammar).
