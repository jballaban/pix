"""Gzip for pages and JSON, and nothing else (`pix.nas.webapp.compress`)."""

from __future__ import annotations

import gzip

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.testclient import TestClient

from pix.nas.webapp.compress import MINIMUM, Compress

BIG = "<p>a thumbnail</p>" * 400


def _client() -> TestClient:
    app = FastAPI()
    app.add_middleware(Compress)  # pyright: ignore[reportArgumentType]

    @app.get("/page")
    def page() -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        return HTMLResponse(BIG)

    @app.get("/json")
    def json_() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return JSONResponse({"html": BIG})

    @app.get("/small")
    def small() -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        return HTMLResponse("<p>hi</p>")

    @app.get("/jpeg")
    def jpeg() -> Response:  # pyright: ignore[reportUnusedFunction]
        return Response(b"\xff\xd8" + b"x" * 5000, media_type="image/jpeg")

    @app.get("/range")
    def range_() -> Response:  # pyright: ignore[reportUnusedFunction]
        return Response(b"<p>" * 2000, status_code=206,
                        media_type="text/html")

    return TestClient(app)


def _raw(c: TestClient, url: str, gz: bool = True) -> tuple[dict[str, str], bytes]:
    """Headers and the bytes as sent, before the client undoes any gzip."""
    with c.stream("GET", url,
                  headers={"Accept-Encoding": "gzip" if gz else "identity"}) as r:
        return dict(r.headers), b"".join(r.iter_raw())


def test_a_page_and_json_leave_gzipped() -> None:
    """Half a megabyte of markup is the same few words three hundred times;
    nothing between the app and a phone was compressing it."""
    c = _client()
    for url in ("/page", "/json"):
        h, body = _raw(c, url)
        assert h["content-encoding"] == "gzip", url
        assert int(h["content-length"]) == len(body) < len(BIG) // 5
        assert BIG.encode()[:40] in gzip.decompress(body)
        assert "accept-encoding" in h["vary"].lower()


def test_nothing_is_compressed_that_was_not_asked_for_or_not_worth_it() -> None:
    c = _client()
    h, _ = _raw(c, "/page", gz=False)
    assert "content-encoding" not in h
    h, body = _raw(c, "/small")
    assert "content-encoding" not in h and len(body) < MINIMUM


def test_media_and_ranges_pass_straight_through() -> None:
    """A JPEG gains nothing and costs the NAS's CPU; a range is part of a
    file, which a compressed body would break."""
    c = _client()
    h, body = _raw(c, "/jpeg")
    assert "content-encoding" not in h and body.startswith(b"\xff\xd8")
    h, body = _raw(c, "/range")
    assert "content-encoding" not in h and body.startswith(b"<p>")
