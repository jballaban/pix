"""pix — personal media library CLI."""

from pathlib import Path

__version__ = "0.1.0"


def exiftool_config_path() -> Path:
    """Absolute path to pix's ExifTool config (registers the pix XMP namespace)."""
    return Path(__file__).parent / "exiftool_config.cfg"
