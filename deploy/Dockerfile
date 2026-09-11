# Base image for the pix2 app (spec/nas-app.md §8).
#
# Dependencies only — the application source is bind-mounted, so iterating means
# copying .py files and restarting rather than rebuilding and re-pushing ~100MB.
# Bake a release image later if reproducibility ever matters more than the edit
# loop.
#
# Deliberately slim: the app never decodes anything, because `process` already
# made everything it displays. So no Pillow, no ffmpeg, no exiftool. That is
# what keeps it viable on the RS820+'s no-AVX Atom.
FROM python:3.12-slim

# uvicorn's standard extra pulls uvloop/httptools, which matter more here than
# usual: the Atom has little headroom, and the app is I/O-bound on SMB reads.
RUN pip install --no-cache-dir \
        "fastapi>=0.115" \
        "uvicorn[standard]>=0.30" \
    && rm -rf /root/.cache

# The bind-mounted source lands here; `pix` is imported from it.
# `PIX2_SHARE` is not optional here. The default in `const.py` is a Windows UNC
# path, which on Linux is a relative filename rather than a network path — the
# container sees the same share as a bind mount, so it is told so directly.
ENV PIX2_SHARE=/volume1/pix2 \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

EXPOSE 8000

# Single worker: SQLite is opened per request and the index is read-mostly, so
# concurrency buys nothing here and costs memory the NAS does not have spare.
CMD ["uvicorn", "pix.nas.web:app", "--host", "0.0.0.0", "--port", "8000"]
