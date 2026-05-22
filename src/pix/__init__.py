"""pix — personal media library CLI."""

from pathlib import Path

# Bump on every commit that changes runtime behavior. The CLI prints this
# as the first line of every run so dev and tester are always aligned.
__version__ = "0.1.31"


def exiftool_config_path() -> Path:
    """Absolute path to pix's ExifTool config (registers the pix XMP namespace)."""
    return Path(__file__).parent / "exiftool_config.cfg"
