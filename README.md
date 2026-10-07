# pix

A personal photo and video library hosted on a Synology NAS: an archive of
original files that are never rewritten, the decisions about them kept in
`.xmp` sidecars beside them, and a web app to browse, curate, share and clip
them.

> **Status: pre-1.0, and developed in the open.** pix is built for and used on
> one multi-terabyte family library, on one NAS, from one Windows desktop. Paths
> are build constants in `src/pix/nas/const.py`, not configuration.

## How it fits together

- **The desktop** runs the `pix` CLI: it pulls from phones and folders
  (`pix import`), copies to the NAS (`pix upload`), and makes everything the app
  shows — thumbnails, previews, playable renders, stills — because the NAS's CPU
  cannot (`pix process`).
- **The NAS** holds the archive (`master/`, sacred originals plus sidecars) and
  the derived tiers beside it, and runs the web app in a container. The app is
  the only thing that writes decisions, and it never touches an original.

The design and its reasons are in [`spec/nas-app.md`](spec/nas-app.md); start
with [`spec/README.md`](spec/README.md).

## Requirements

- **Python 3.12+** and [uv](https://docs.astral.sh/uv/)
- **[ExifTool](https://exiftool.org/)** and **[ffmpeg](https://ffmpeg.org/)**
  (`ffmpeg` + `ffprobe`) on your `PATH`
- Windows 11 for the desktop side (device import uses WPD/MTP)

## Install

```sh
git clone https://github.com/jballaban/pix
uv tool install --editable ./pix
```

This puts a `pix` executable on your `PATH`. Deploying the app to the NAS is in
[`deploy/README.md`](deploy/README.md).

## Use

```sh
pix import device                     # pull new photos off a connected phone
pix import folder E:\DCIM --name sd   # or from a folder
pix upload                            # staging -> master on the NAS
pix process                           # thumbnails, previews, renders, index
```

Everything else — tagging, events, people, sharing, clips — happens in the app.

## License

[MIT](LICENSE).
