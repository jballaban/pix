"""pix — personal media library CLI."""

from pathlib import Path

# Bump on every commit that changes runtime behavior. The CLI prints this
# as the first line of every run so dev and tester are always aligned.
__version__ = "0.1.372"


def exiftool_config_path() -> Path:
    """Absolute path to pix's ExifTool config (registers the pix XMP namespace)."""
    return Path(__file__).parent / "exiftool_config.cfg"


def banner(schema_version: int | None = None) -> None:
    """Print the one-line version banner.

    Always shows the pix tool version. Includes the library schema
    version when known (after the command has resolved its library
    root). Format: `pix 0.1.36, schema v7` or `pix 0.1.36` on its
    own when there's no library context (or resolution failed).

    `typer` is imported **inside** the function, not at module scope. Importing
    `pix` should not require the CLI stack: the app container runs
    `pix.nas.web` from a slim image with only FastAPI in it, and a top-level
    `import typer` made merely importing the package fail there.
    """
    import typer

    if schema_version is None:
        typer.echo(f"pix {__version__}")
    else:
        typer.echo(f"pix {__version__}, schema v{schema_version}")
