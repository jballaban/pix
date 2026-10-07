"""Gzip for the pages and the JSON, and for nothing else.

A grid page is half a megabyte of markup for three hundred thumbnails —
the same few dozen words three hundred times — and nothing between the app
and a phone compressed it: not uvicorn, and not the NAS's reverse proxy. It
gzips to about a tenth, which over a phone's connection is most of the wait
for the page to appear.

Its own rather than Starlette's `GZipMiddleware`, for two reasons. That one
compresses every type unless told otherwise, and the way to tell it depends on
the Starlette version — which is whatever the container image was built with,
not what this repository locks. And everything else the app sends is already
compressed or must not be touched: JPEG and video gain nothing and cost the
NAS's CPU, and a video is served in ranges, which a compressed body breaks.
So this names what it compresses rather than what it leaves alone, and
anything it does not name passes straight through, unbuffered.
"""

from __future__ import annotations

import gzip
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

#: What is worth compressing: text the app writes itself.
TYPES: tuple[bytes, ...] = (b"text/html", b"application/json")
#: Below this the gzip header and the CPU cost more than they save.
MINIMUM: int = 1024
#: Fast rather than small: the page is waiting on it, and level 5 is within a
#: few percent of 9 on markup this repetitive.
LEVEL: int = 5


def _wants_gzip(scope: Scope) -> bool:
    for k, v in scope.get("headers", ()):
        if k == b"accept-encoding":
            return b"gzip" in v.lower()
    return False


class Compress:
    """ASGI middleware: gzip a whole HTML or JSON response, if asked for."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive,
                       send: Send) -> None:
        if scope["type"] != "http" or not _wants_gzip(scope):
            await self.app(scope, receive, send)
            return
        start: Message | None = None
        chunks: list[bytes] = []

        async def wrapped(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                headers = {k.lower(): v for k, v in message.get("headers", ())}
                kind = headers.get(b"content-type", b"").split(b";")[0].strip()
                if (message.get("status") == 200 and kind in TYPES
                        and b"content-encoding" not in headers):
                    start = message         # held until the body is whole
                    return
                await send(message)
                return
            if start is None or message["type"] != "http.response.body":
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            body = b"".join(chunks)
            headers = [(k, v) for k, v in start.get("headers", ())
                       if k.lower() != b"content-length"]
            if len(body) >= MINIMUM:
                body = gzip.compress(body, compresslevel=LEVEL)
                headers += [(b"content-encoding", b"gzip"),
                            (b"vary", b"Accept-Encoding")]
            headers.append((b"content-length", str(len(body)).encode()))
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, wrapped)
