"""Long-running ExifTool subprocess (`-stay_open True`).

Spawns one ExifTool process at the start of apply, sends commands via
stdin, and reads framed responses (each response terminated by a `{ready}`
sentinel). Avoids the ~200ms-per-spawn overhead at TB scale.

Pyexiftool would give us this for free, but a 60-line wrapper keeps us off
an extra dependency and makes the protocol explicit for the spec.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import TracebackType
from typing import IO

from pix import exiftool_config_path
from pix.metadata import require_exiftool


_READY_SENTINEL: str = "{ready}\n"


class ExifToolSession:
    """Wraps one long-running ExifTool subprocess.

    Lifecycle: create (spawns subprocess) → multiple execute() calls →
    close() (cleanly shuts the subprocess down). Use as a context manager
    to guarantee shutdown.
    """

    def __init__(self, exe: str | None = None) -> None:
        self._exe: str = exe or require_exiftool()
        self._proc: subprocess.Popen[str] = subprocess.Popen(
            [
                self._exe,
                "-config",
                str(exiftool_config_path()),
                "-stay_open",
                "True",
                "-@",
                "-",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        # mypy/pyright can't infer that Popen with PIPE has non-None streams
        if self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError("Failed to attach pipes to exiftool subprocess")
        self._stdin: IO[str] = self._proc.stdin
        self._stdout: IO[str] = self._proc.stdout

    def execute(self, *args: str) -> str:
        """Send one batch of arguments to ExifTool; return its stdout output."""
        for arg in args:
            self._stdin.write(arg + "\n")
        self._stdin.write("-execute\n")
        self._stdin.flush()

        out: list[str] = []
        while True:
            line = self._stdout.readline()
            if not line:
                raise RuntimeError(
                    "exiftool subprocess exited unexpectedly during execute"
                )
            if line == _READY_SENTINEL:
                break
            out.append(line)
        return "".join(out)

    def write_tags(self, file: Path, tags: dict[str, str]) -> None:
        """Write `tags` to `file` in place via `-overwrite_original`."""
        if not tags:
            return
        args: list[str] = []
        for key, value in tags.items():
            # ExifTool tag-set syntax: `-<key>=<value>`
            args.append(f"-{key}={value}")
        args.append("-overwrite_original")
        args.append(str(file))
        self.execute(*args)

    def copy_metadata_and_write_tags(
        self,
        source: Path,
        dest: Path,
        tags: dict[str, str],
    ) -> None:
        """Copy all metadata from `source` to `dest` and write `tags` in one call.

        Used by the CONVERT step (pixel/container conversion happens via
        Pillow/ffmpeg without metadata; this call then layers the source's
        EXIF/XMP/IPTC into the converted file alongside the pix:* writes).
        """
        args: list[str] = [
            "-tagsFromFile",
            str(source),
            "-all:all",  # copy every readable tag from source
        ]
        for key, value in tags.items():
            args.append(f"-{key}={value}")
        args.append("-overwrite_original")
        args.append(str(dest))
        self.execute(*args)

    def export_xmp_sidecar(
        self, file: Path, sidecar_path: Path
    ) -> None:
        """Write `file`'s metadata to a fresh XMP sidecar at `sidecar_path`."""
        # `-o <path>` creates a new file from the metadata; combined with
        # all-tag selection this captures everything ExifTool can read.
        # If the sidecar already exists, ExifTool refuses to overwrite; we
        # remove first to keep the op idempotent.
        if sidecar_path.exists():
            sidecar_path.unlink()
        self.execute("-o", str(sidecar_path), str(file))

    def close(self) -> None:
        """Shut down the ExifTool subprocess cleanly."""
        if self._proc.poll() is not None:
            return
        try:
            self._stdin.write("-stay_open\nFalse\n")
            self._stdin.flush()
            self._stdin.close()
        except (BrokenPipeError, ValueError):
            # Subprocess already gone; nothing to do.
            pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()

    def __enter__(self) -> "ExifToolSession":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
