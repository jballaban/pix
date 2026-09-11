# The pix2 app image (spec/nas-app.md §8).
#
# **Self-contained.** The application source is baked in, so importing the image
# into Container Manager and creating a container is enough — no copying source
# onto the share, no second mount to get right. That is what anyone expects of
# an image, and an earlier build that shipped only dependencies produced exactly
# the confusing crash-loop it deserved.
#
# The source is still *overridable*: `PYTHONPATH` puts `/app/src` ahead of the
# baked copy, so bind-mounting source there shadows it and gives a seconds-long
# edit loop during development. Optional, not required.
#
# Deliberately slim: the app never decodes anything, because `process` already
# made everything it displays. No Pillow, no ffmpeg, no exiftool — which is what
# keeps it viable on the RS820+'s no-AVX Atom.
#
# Build from the repo root, so `src/` is in context:
#     docker build -f deploy/Dockerfile -t pix2-app:latest .
FROM python:3.12-slim

# uvicorn's standard extra pulls uvloop/httptools, which matter more here than
# usual: the Atom has little headroom, and the app is I/O-bound on SMB reads.
RUN pip install --no-cache-dir \
        "fastapi>=0.115" \
        "uvicorn[standard]>=0.30" \
    && rm -rf /root/.cache

# The baked application source.
COPY src /app/baked

# `/app/src` first so a bind mount shadows the baked copy; `/app/baked` is the
# fallback that makes the image work on its own.
#
# `PIX2_SHARE` is not optional. The default in `const.py` is a Windows UNC path,
# which on Linux is a relative filename rather than a network path — the
# container sees the same share as a bind mount, so it is told so directly.
ENV PIX2_SHARE=/volume1/pix2 \
    PYTHONPATH=/app/src:/app/baked \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Validates the mounts and explains the fix, rather than letting uvicorn die with
# a ModuleNotFoundError while the operator watches a restart loop.
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

EXPOSE 8000

# Single worker: SQLite is opened per request and the index is read-mostly, so
# concurrency buys nothing here and costs memory the NAS does not have spare.
CMD ["uvicorn", "pix.nas.web:app", "--host", "0.0.0.0", "--port", "8000"]
