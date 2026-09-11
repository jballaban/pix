#!/bin/sh
# Fail with an explanation rather than a crash loop.
#
# The two things that can be wrong at startup are a missing archive mount and an
# unusable source override. Without this check either one kills uvicorn with a
# bare traceback, Container Manager restarts the container, and the operator
# watches it flap with no idea why.
set -e

fail() {
    echo "=============================================================="
    echo "pix2 cannot start: $1"
    echo
    echo "$2"
    echo "=============================================================="
    # Sleep rather than exit: an immediate exit makes Container Manager restart
    # the container, and the message scrolls past before it can be read.
    sleep 3600
    exit 1
}

SHARE="${PIX2_SHARE:-/volume1/pix2}"

# The archive mount is required — everything the app serves lives there.
[ -d "$SHARE" ] || fail \
    "the archive is not mounted at $SHARE." \
    "In Container Manager, add a volume:
    /pix2   ->   $SHARE    (read/write)

Read/write because curation writes .xmp decisions into master."

# The source is baked into the image; /app/src is an optional override for
# development. Only complain if it is mounted but wrong, which is a mistake —
# absent is the normal case.
if [ -d /app/src ] && [ -n "$(ls -A /app/src 2>/dev/null)" ]; then
    [ -f /app/src/pix/nas/web.py ] || fail \
        "/app/src is mounted but does not contain the pix source." \
        "It must hold 'pix' DIRECTLY, so that
    /app/src/pix/nas/web.py
exists. Copying the repo's src folder *into* src gives
    /app/src/src/pix
which fails exactly like this.

Or simply remove the /app/src mount: the image already contains the app."
    echo "pix2: using mounted source at /app/src"
else
    echo "pix2: using built-in source"
fi

echo "pix2: archive at $SHARE"
exec "$@"
