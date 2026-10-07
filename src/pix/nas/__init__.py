"""The NAS-hosted architecture (see spec/nas-app.md).

The whole of pix: the `pix` console script and the web app. It was built
alongside an earlier CLI-pipeline architecture as `pix2`; that architecture is
gone, and what this package still borrows from `src/pix/` is the import
plumbing it shared.

The desktop commands, with no config and no library root:

    pix import device                        interactive device pull
    pix import folder <source> --name <n>    folder pull (SD card, legacy library)
    pix upload                               staging -> master, over SMB
    pix process                              master -> thumbnails/previews/renders
"""

from __future__ import annotations
