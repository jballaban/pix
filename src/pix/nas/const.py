"""Build constants for the NAS architecture (see spec/nas-app.md).

There is **no config file and no library root**. The two paths that matter are
constants here, the way `config.EXTENSION_POLICY` already is: one obvious place
to change, no resolution logic, nothing to lose. Config earns its keep when
there is a second machine or a second NAS; there is neither.
"""

from __future__ import annotations

from pathlib import Path

# --- local (desktop) ---------------------------------------------------------

#: Local working root. Nothing permanent lives here — only staging awaiting
#: upload, which `upload` clears once it has verified the copy.
#:
#: **On G:, deliberately, for two measured reasons.** A folder import hardlinks
#: rather than copies, and hardlinks cannot cross volumes — so staging must share
#: a volume with the legacy library at `G:\pix` or seeding falls back to real
#: copies. It cannot: the 2022 folder alone is 946GB and 2023 is 816GB, against
#: 819GB free on F:. Second, Synology Drive syncs per top-level folder
#: (`.SynologyWorkingDirectory` sits in `G:\pix` and `G:\photo`, not at `G:\`),
#: so the sibling folder `G:\pix2` (named to match the tool) is outside
#: every sync scope and staging never uploads itself.
LOCAL_ROOT: Path = Path(r"G:\pix2")

#: One subfolder per `--name`, accumulating across import runs until uploaded.
IMPORT_ROOT: Path = LOCAL_ROOT

# --- NAS ---------------------------------------------------------------------

#: The share root. A **new, empty** share, deliberately not the existing `pix`
#: one — that holds the current library and is Synology Drive-synced, so the
#: two trees coexist untouched through the transition and the old share can be
#: retired on its own schedule.
#:
#: Reached over SMB directly, never through Synology Drive —
#: routing master through a sync client would make it a *synced* folder again
#: and drag back every problem this architecture dropped (re-upload on rename,
#: conflict copies, working-directory noise).
MASTER_SHARE: Path = Path(r"\\nas\pix2")

#: Sacred originals plus their `.xmp` decision sidecars. Backed up.
MASTER_DIR: Path = MASTER_SHARE / "master"

#: Derived tiers. Disposable, never backed up, regenerable from master.
RENDER_DIR: Path = MASTER_SHARE / "render"
THUMB_DIR: Path = MASTER_SHARE / "thumb"
PREVIEW_DIR: Path = MASTER_SHARE / "preview"

#: Probed facts, one JSON per master file. Derived like the rest — but it is what
#: makes the app's index cheap to rebuild: reading these is minutes where
#: re-probing 62k media files is hours. `process` is already opening every file
#: to decode it, so extracting the metadata at the same time is near-free.
META_DIR: Path = MASTER_SHARE / "meta"

#: Per-folder download ledger, written during upload. Its first line is a
#: header describing the source, which is what makes the known-device registry
#: derivable instead of stored.
LEDGER_NAME: str = ".import.jsonl"
