"""Reserved folder-name sentinels for template-driven layouts.

When a template level's tag has no value, pix materializes a placeholder
folder (`(null)`); a file excluded by an explicit filter lands in
`(filtered)`; checkout's delete staging area is `(pending-delete)`. The
brackets keep these distinguishable from real tag values on disk — an
event literally named `null` no longer collides with the untagged
placeholder — and the convention is shared across organize, checkout,
and export so the same folder means the same thing everywhere.

The bracketed forms are the **rendered, on-disk** names. The template
query language still uses the bare keyword `null` as the filter token
(e.g. `{year:null,2020}`) — that's what the user types; it renders to the
`(null)` folder. See spec/tags.md → Template grammar and spec/organize.md.
"""

from __future__ import annotations

# Untagged: a level whose tag resolved to no value.
NULL_FOLDER: str = "(null)"

# Excluded: a file dropped by an explicit `{tag:...}` filter. Rendered by
# organize (`pix.organize.render_target_folder`); export drops excluded
# files instead of materializing this folder, and checkout rejects filtered
# templates for now (commit can't reverse `(filtered)` into a tag value).
FILTERED_FOLDER: str = "(filtered)"

# Checkout delete-staging area (checkout removal flow; deferred — see
# spec/tag-editing.md).
PENDING_DELETE_FOLDER: str = "(pending-delete)"
