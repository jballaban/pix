"""The NAS-hosted architecture (see spec/nas-app.md).

A fresh module built alongside the existing CLI, exposed as the `pix2` console
script, so `pix` keeps working untouched until seeding is proven. When the old
architecture is amputated this package is promoted to `src/pix/` and the second
entry point disappears.

Four commands, no config and no library root:

    pix2 import device                        interactive device pull
    pix2 import folder <source> --name <n>    folder pull (SD card, legacy library)
    pix2 upload                               staging -> master, over SMB
    pix2 process                              master -> thumbnails/previews/renders
"""

from __future__ import annotations
