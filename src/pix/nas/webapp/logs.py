"""The app's own logs, on the share: what was asked of it, and what went wrong.

Two files under `app/logs/`, both rotating so neither grows without limit:

* **`activity.log`** — one line per request that does something: when, which
  machine, who, what (method, address, and the start of a write's body), the
  status, how long. Pictures and video are left out: a page of the grid is
  hundreds of them, and they would bury the click that mattered.
* **`errors.log`** — every refusal the app gave (4xx, with the reason it gave)
  and every failure (5xx, with the full traceback), plus anything a
  background thread raised. Kept longer, because it is small and it is what
  an investigation starts from.

Not `operations.jsonl`, which is History: the decisions people made, to be
read and reverted. This is the app's account of itself — including what it
refused and what broke, which History never records because nothing
happened.

On the share because that is where they can be read from without getting
into the container: a failure on a phone is diagnosed from a desk by opening
the file. Each line names the machine, because a development server on the
desktop writes to the same files.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
import traceback
from collections.abc import Awaitable, Callable, MutableMapping
from logging.handlers import RotatingFileHandler
from typing import Any, cast

from fastapi import HTTPException, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response

from pix.nas import webroots

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

#: Five files of 5 MB: weeks of clicking, and small enough to open anywhere.
ACTIVITY_BYTES, ACTIVITY_KEEP = 5 * 1024 * 1024, 5
#: Ten of 10 MB: errors are rarer and worth keeping further back.
ERRORS_BYTES, ERRORS_KEEP = 10 * 1024 * 1024, 10
#: How much of a write's body goes in its line: enough to say which files.
BODY_CHARS: int = 400
#: Fetched by the hundred for every page; logged only when they fail.
QUIET: tuple[str, ...] = ("/thumb/", "/preview/", "/large/", "/strip/",
                          "/media/", "/static/", "/healthz", "/favicon",
                          "/icon", "/manifest", "/sw.js", "/api/keyframes/")

activity = logging.getLogger("pix.activity")
errors = logging.getLogger("pix.errors")
HOST = socket.gethostname()
_installed = False


def install() -> None:
    """Open both logs. Once per process; a no-op after that.

    At startup rather than import, so importing the app — which every test
    does — opens nothing. The directory is read at call time, like every
    other root (`webroots`).
    """
    global _installed
    if _installed:
        return
    _installed = True
    try:
        webroots.LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return              # no share: the app still serves, unlogged
    fmt = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    for logger, name, size, keep, level in (
            (activity, "activity.log", ACTIVITY_BYTES, ACTIVITY_KEEP,
             logging.INFO),
            (errors, "errors.log", ERRORS_BYTES, ERRORS_KEEP,
             logging.WARNING)):
        handler = RotatingFileHandler(webroots.LOG_DIR / name, maxBytes=size,
                                      backupCount=keep, encoding="utf-8",
                                      delay=True)
        handler.setFormatter(fmt)
        logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = False
    # A cut or a still made on a background thread fails where no request
    # can report it; this is where it is reported instead.
    before = threading.excepthook

    def on_thread_error(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is not None:
            errors.error("%s thread %s failed\n%s", HOST,
                         args.thread.name if args.thread else "?",
                         "".join(traceback.format_exception(
                             args.exc_type, args.exc_value,
                             args.exc_traceback)).rstrip())
        before(args)
    threading.excepthook = on_thread_error


def _who(scope: Scope) -> str:
    # `request.state` is backed by this dict (`signed_in` sets `user`).
    state: object = scope.get("state")
    who: object = (cast("dict[str, object]", state).get("user")
                   if isinstance(state, dict) else None)
    return str(who) if who else "-"


def _what(scope: Scope) -> str:
    query = scope.get("query_string", b"").decode("latin-1")
    return f'{scope.get("method", "?")} {scope.get("path", "?")}' + (
        f"?{query}" if query else "")


class Activity:
    """ASGI middleware: one `activity.log` line per request, and a traceback
    in `errors.log` for anything that escapes a route."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive,
                       send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        began = time.perf_counter()
        status = 500
        body: list[bytes] = []
        size = 0
        writes = scope.get("method") in ("POST", "PUT", "PATCH", "DELETE")

        async def heard() -> Message:
            nonlocal size
            message = await receive()
            if writes and message.get("type") == "http.request" \
                    and size < BODY_CHARS:
                chunk: bytes = message.get("body", b"")
                body.append(chunk[:BODY_CHARS - size])
                size += len(chunk)
            return message

        async def said(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, heard, said)
        except Exception:
            errors.error("%s %s %s raised\n%s", HOST, _who(scope),
                         _what(scope), traceback.format_exc().rstrip())
            raise
        finally:
            path = str(scope.get("path", ""))
            if status >= 400 or not path.startswith(QUIET):
                sent = b"".join(body).decode("utf-8", "replace")
                if size > BODY_CHARS:
                    sent += "…"
                activity.info("%s %s %s %d %dms%s", HOST, _who(scope),
                              _what(scope), status,
                              (time.perf_counter() - began) * 1000,
                              f" {sent}" if sent else "")


async def refused(request: Request, exc: Exception) -> Response:
    """A refusal, said to `errors.log` with its reason, then answered as
    FastAPI would have. Signing in is not an error, and is left out."""
    if isinstance(exc, HTTPException) and exc.status_code != 401:
        errors.warning("%s %s %s %d %s", HOST, _who(request.scope),
                       _what(request.scope), exc.status_code, exc.detail)
    if isinstance(exc, RequestValidationError):
        errors.warning("%s %s %s 422 %s", HOST, _who(request.scope),
                       _what(request.scope), exc.errors())
        return await request_validation_exception_handler(request, exc)
    assert isinstance(exc, HTTPException)
    return await http_exception_handler(request, exc)
