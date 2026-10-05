"""The app's own logs (`pix.nas.webapp.logs`): what was asked, what went wrong."""

from __future__ import annotations

import logging
import logging.handlers
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import Body, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from pix.nas import webroots
from pix.nas.webapp import logs


@pytest.fixture
def logged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Both logs opened into a sandbox, and closed again after."""
    monkeypatch.setattr(webroots, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(logs, "_installed", False)
    hook = threading.excepthook
    logs.install()
    yield tmp_path / "logs"
    threading.excepthook = hook
    for logger in (logs.activity, logs.errors):
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)


def _read(d: Path, name: str) -> str:
    for logger in (logs.activity, logs.errors):
        for handler in logger.handlers:
            handler.flush()
    f = d / name
    return f.read_text(encoding="utf-8") if f.exists() else ""


def _app() -> TestClient:
    app = FastAPI()
    app.add_middleware(logs.Activity)  # pyright: ignore[reportArgumentType]
    app.add_exception_handler(HTTPException, logs.refused)
    app.add_exception_handler(RequestValidationError, logs.refused)

    @app.get("/page")
    def page() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"ok": "yes"}

    @app.get("/thumb/x")
    def thumb() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {}

    @app.post("/api/refuse")
    def refuse(  # pyright: ignore[reportUnusedFunction]
            body: Annotated[dict[str, str], Body()]) -> None:
        del body
        raise HTTPException(409, "its photos have no files yet")

    @app.post("/api/break")
    def broken() -> None:  # pyright: ignore[reportUnusedFunction]
        raise RuntimeError("the copy failed")

    return TestClient(app, raise_server_exceptions=False)


def test_every_request_that_does_something_is_a_line(logged: Path) -> None:
    c = _app()
    c.get("/page?event=Sicily")
    c.get("/thumb/x")
    c.post("/api/refuse", json={"folder": "f", "source": "v.mp4"})
    text = _read(logged, "activity.log")

    assert "GET /page?event=Sicily 200" in text
    # A write says what it was given — which files — so a failure can be
    # matched to the click that caused it.
    assert 'POST /api/refuse 409' in text and '"source":"v.mp4"' in text
    # Pictures are hundreds a page; they would bury the click that mattered.
    assert "/thumb/x" not in text
    assert logs.HOST in text


def test_a_refusal_is_an_error_line_with_its_reason(logged: Path) -> None:
    _app().post("/api/refuse", json={"folder": "f"})
    assert "409 its photos have no files yet" in _read(logged, "errors.log")


def test_a_failure_is_logged_with_its_traceback(logged: Path) -> None:
    r = _app().post("/api/break", json={})
    assert r.status_code == 500
    text = _read(logged, "errors.log")
    assert "POST /api/break raised" in text
    assert "RuntimeError: the copy failed" in text and "Traceback" in text
    assert "POST /api/break 500" in _read(logged, "activity.log")


def test_a_background_thread_failing_is_logged(logged: Path) -> None:
    def boom() -> None:
        raise ValueError("cut failed")
    t = threading.Thread(target=boom, name="pix-cut")
    t.start()
    t.join()
    text = _read(logged, "errors.log")
    assert "thread pix-cut failed" in text and "ValueError: cut failed" in text


def test_the_real_app_names_who_asked(logged: Path, client: TestClient) -> None:
    """Signed in, the line says who; a refusal says why."""
    client.get("/browse")
    client.post("/api/clips/free", json={"folder": "init_2026",
                                         "source": "b.mp4"})
    assert " admin GET /browse 200 " in _read(logged, "activity.log")
    assert "admin POST /api/clips/free 404 not in master" in _read(
        logged, "errors.log")


def test_both_logs_rotate(logged: Path) -> None:
    sizes = {h.baseFilename.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]:
             (h.maxBytes, h.backupCount)
             for lg in (logs.activity, logs.errors) for h in lg.handlers
             if isinstance(h, logging.handlers.RotatingFileHandler)}
    assert sizes == {"activity.log": (logs.ACTIVITY_BYTES, logs.ACTIVITY_KEEP),
                     "errors.log": (logs.ERRORS_BYTES, logs.ERRORS_KEEP)}
