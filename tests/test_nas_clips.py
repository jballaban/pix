"""Clips — a stretch or a frame of a source video (spec/clips.md).

A clip is virtual: a sidecar beside its source holding a range and its own
decisions, and an index row made from the source's. The source is never
touched, and nothing about making a clip hides it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pix.nas import clips
from pix.nas import decisions
from pix.nas import history
from pix.nas import index as ix
from pix.nas import web
from pix.nas.decisions import Decision


# --- the model ---------------------------------------------------------------

def test_a_clip_name_says_which_video_it_came_from() -> None:
    assert clips.source_of("IMG_4471.MOV~k3fa") == "IMG_4471.MOV"
    assert clips.source_of(clips.name_of("a.mp4", "zz22")) == "a.mp4"


def test_a_tilde_in_a_real_name_is_not_a_clip() -> None:
    """Old cameras put `~` in names. Reading one as a clip would hide a real
    photograph behind a source that is not there."""
    assert clips.source_of("IMG~1.JPG") is None
    assert clips.source_of("IMG_4471.MOV~ABCD") is None
    assert clips.source_of("~k3fa") is None


def test_ids_never_collide_with_a_sibling() -> None:
    taken = {clips.new_id(()) for _ in range(50)}
    assert clips.new_id(taken) not in taken


def test_clips_may_touch_but_not_overlap() -> None:
    clips.check(10, 20, siblings=[(0, 10), (20, 30)], duration=30)
    with pytest.raises(clips.ClipError, match="overlap"):
        clips.check(5, 15, siblings=[(0, 10)], duration=30)


def test_a_still_never_overlaps_anything() -> None:
    clips.check(5, 5, siblings=[(0, 10)], duration=30)
    clips.check(0, 10, siblings=[(5, 5)], duration=30)


def test_a_clip_cannot_run_off_the_end() -> None:
    with pytest.raises(clips.ClipError, match="30s long"):
        clips.check(20, 40, siblings=[], duration=30)


def test_a_clip_is_dated_where_it_starts() -> None:
    capture, effective, precision = clips.date(
        "2026:08:30 15:00:00", None, 90.4, None)
    assert effective is not None
    assert effective.isoformat() == "2026-08-30T15:01:30.400000"
    assert capture == "2026:08:30 15:01:30"
    assert precision == 19


def test_a_source_known_only_to_the_day_has_no_time_to_add_to() -> None:
    """Adding seconds to a made-up midnight would publish a precision nobody
    has."""
    _, effective, precision = clips.date(None, "2026-08-30-*:*:*", 90, None)
    assert effective is not None and effective.hour == 0
    assert precision == 10


def test_a_clips_own_override_wins() -> None:
    _, effective, _ = clips.date("2026:08:30 15:00:00", None, 90,
                                 "1987-*-*-*:*:*")
    assert effective is not None and effective.year == 1987


def test_a_clip_starts_with_its_sources_content_but_not_its_place() -> None:
    """Deleted, stacked and hidden are about where the source sits in the
    grid. A clip born hidden would vanish the moment it was made."""
    source = Decision(event="Sicily", tags=("beach",), people=("Mum",),
                      audience=(decisions.HIDDEN,), deleted=True,
                      stacked_under="f/x.mp4")
    got = clips.inherited(source, None)
    assert got == Decision(event="Sicily", tags=("beach",), people=("Mum",))


def test_a_clip_takes_the_event_the_source_is_showing() -> None:
    """Most of the library's events are in embedded tags, not sidecars."""
    assert clips.inherited(None, "Italy - Sicily").event == "Italy - Sicily"


def test_joining_two_clips_unions_the_sets_and_the_latest_wins() -> None:
    a = Decision(event="Old", tags=("beach",), audience=("family",))
    b = Decision(event="New", tags=("sunset",), people=("Mum",))
    got = clips.merged(a, 1.0, b, 2.0)
    assert got.event == "New"
    assert got.tags == ("beach", "sunset")
    assert got.people == ("Mum",) and got.audience == ("family",)
    assert clips.merged(a, 3.0, b, 2.0).event == "Old"


def test_a_clip_range_survives_the_sidecar(tmp_path: Path) -> None:
    media = tmp_path / "a.mp4~k3fa"
    decisions.write(media, Decision(clip_in=0.0, clip_out=12.345,
                                    event="Sicily"))
    got = decisions.read(media)
    assert got == Decision(clip_in=0.0, clip_out=12.345, event="Sicily")
    assert got is not None and got.is_clip and not got.is_still


def test_half_a_range_is_refused(tmp_path: Path) -> None:
    with pytest.raises(decisions.DecisionError, match="both ends"):
        decisions.write(tmp_path / "a~k3fa", Decision(clip_in=1.0))


# --- the routes ----------------------------------------------------------------

@pytest.fixture
def video(app_env: dict[str, Path], writable: Path) -> Path:
    """`b.mp4`, 75 seconds long and dated, in master and in the index."""
    (writable / "b.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    share = app_env["share"]
    (share / "meta" / "init_2026" / "b.mp4.json").write_text(json.dumps({
        "file": "b.mp4", "folder": "init_2026", "size": 20, "mtime_ns": 1,
        "exif": {"QuickTime:Duration": "75 s",
                 "EXIF:DateTimeOriginal": "2026:08:30 15:00:00",
                 "XMP:EventAuto": "Italy - Sicily"},
    }), encoding="utf-8")
    _rebuild(app_env)
    return writable


def _rebuild(app_env: dict[str, Path]) -> None:
    share = app_env["share"]
    ix.build(app_env["db"], meta_dir=share / "meta",
             master_dir=share / "master")


def _make(client: TestClient, *ranges: tuple[float, float]) -> list[str]:
    r = client.post("/api/clips/make", json={
        "folder": "init_2026", "source": "b.mp4",
        "clips": [{"start": a, "end": b} for a, b in ranges]})
    assert r.status_code == 200, r.text
    return list(r.json()["made"])


def _row(app_env: dict[str, Path], name: str) -> sqlite3.Row | None:
    return ix.one(ix.connect(app_env["db"]), "init_2026", name)


def _have(app_env: dict[str, Path], name: str) -> sqlite3.Row:
    row = _row(app_env, name)
    assert row is not None, f"{name} is not indexed"
    return row


def test_making_clips_leaves_the_source_alone(
    client: TestClient, video: Path, app_env: dict[str, Path]
) -> None:
    made = _make(client, (0, 30), (30, 60))

    assert len(made) == 2
    assert all(clips.source_of(n) == "b.mp4" for n in made)
    assert decisions.read(video / "b.mp4") is None, "the source was written"
    assert (video / "b.mp4").read_bytes() == b"\x00\x00\x00\x18ftypmp42"
    first = decisions.read(video / made[0])
    assert first == Decision(event="Italy - Sicily", clip_in=0.0,
                             clip_out=30.0)
    # Nothing hides the source: it is still in the grid beside its clips.
    html = client.get("/browse").text
    assert 'data-name="b.mp4"' in html
    assert all(f'data-name="{n}"' in html for n in made)


def test_a_clip_is_dated_by_where_it_starts_in_its_source(
    client: TestClient, video: Path, app_env: dict[str, Path]
) -> None:
    [late] = _make(client, (40, 50))
    row = _have(app_env, late)
    assert row["effective_date"] == "2026-08-30-15:00:40"
    assert row["kind"] == "video" and row["duration"] == 10
    assert row["clip_of"] == "b.mp4"


def test_correcting_the_sources_date_moves_its_clips(
    client: TestClient, video: Path, app_env: dict[str, Path]
) -> None:
    [late] = _make(client, (40, 50))
    r = client.post("/api/decide", json={
        "folder": "init_2026", "name": "b.mp4",
        "date_override": "2025-*-*-*:*:*"})
    assert r.status_code == 200, r.text
    assert _have(app_env, late)["effective_date"] == "2025-08-30-15:00:40"


def test_a_still_is_a_photograph(
    client: TestClient, video: Path, app_env: dict[str, Path]
) -> None:
    [still] = _make(client, (12.5, 12.5))
    row = _have(app_env, still)
    assert row["kind"] == "image" and row["duration"] is None
    assert "Still" in client.get("/browse").text


def test_clips_cannot_overlap(client: TestClient, video: Path) -> None:
    _make(client, (0, 30))
    r = client.post("/api/clips/make", json={
        "folder": "init_2026", "source": "b.mp4",
        "clips": [{"start": 20, "end": 40}]})
    assert r.status_code == 409 and "overlap" in r.text, r.text


def test_only_a_video_can_be_cut(client: TestClient, video: Path) -> None:
    r = client.post("/api/clips/make", json={
        "folder": "init_2026", "source": "a.jpg",
        "clips": [{"start": 0, "end": 1}]})
    assert r.status_code == 409, r.text


def test_moving_a_clips_ends_keeps_what_was_decided_about_it(
    client: TestClient, video: Path
) -> None:
    [clip] = _make(client, (0, 30))
    client.post("/api/decide", json={"folder": "init_2026", "name": clip,
                                     "add_people": ["Mum"]})
    r = client.post("/api/clips/range", json={
        "folder": "init_2026", "name": clip, "start": 5, "end": 25})
    assert r.status_code == 200, r.text
    got = decisions.read(video / clip)
    assert got is not None
    assert (got.clip_in, got.clip_out, got.people) == (5.0, 25.0, ("Mum",))


def test_splitting_copies_the_decisions_to_the_new_half(
    client: TestClient, video: Path
) -> None:
    [clip] = _make(client, (0, 30))
    client.post("/api/decide", json={"folder": "init_2026", "name": clip,
                                     "add_tags": ["beach"]})
    r = client.post("/api/clips/split", json={
        "folder": "init_2026", "name": clip, "at": 12})
    assert r.status_code == 200, r.text
    second = r.json()["second"]

    first = decisions.read(video / clip)
    other = decisions.read(video / second)
    assert first is not None and other is not None
    assert (first.clip_in, first.clip_out) == (0.0, 12.0)
    assert (other.clip_in, other.clip_out) == (12.0, 30.0)
    assert first.tags == other.tags == ("beach",)


def test_joining_two_clips_and_taking_it_back(
    client: TestClient, video: Path, app_env: dict[str, Path]
) -> None:
    a, b = _make(client, (0, 30), (30, 60))
    client.post("/api/decide", json={"folder": "init_2026", "name": b,
                                     "add_people": ["Mum"]})
    r = client.post("/api/clips/merge", json={
        "folder": "init_2026", "first": b, "second": a})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == a, "the earlier one survives"

    kept = decisions.read(video / a)
    assert kept is not None
    assert (kept.clip_in, kept.clip_out, kept.people) == (0.0, 60.0, ("Mum",))
    assert not decisions.sidecar_path(video / b).exists()
    assert _row(app_env, b) is None

    op = history.recent(1)[0]
    client.post("/history/revert", data={"id": op.id})
    back = decisions.read(video / b)
    assert back is not None and (back.clip_in, back.clip_out) == (30.0, 60.0)
    assert _row(app_env, b) is not None
    kept = decisions.read(video / a)
    assert kept is not None and kept.clip_out == 30.0


def test_reverting_the_cut_takes_the_clips_away(
    client: TestClient, video: Path, app_env: dict[str, Path]
) -> None:
    made = _make(client, (0, 30))
    op = history.recent(1)[0]
    r = client.post("/history/revert", data={"id": op.id},
                    follow_redirects=False)
    assert r.status_code == 303
    assert not decisions.sidecar_path(video / made[0]).exists()
    assert _row(app_env, made[0]) is None


def test_a_source_cannot_be_binned_while_it_has_clips(
    client: TestClient, video: Path
) -> None:
    """*I only want the clips* is why somebody bins the source — the one
    gesture that would destroy them. Hiding it is the answer."""
    [clip] = _make(client, (0, 30))
    r = client.post("/api/decide", json={
        "folder": "init_2026", "name": "b.mp4", "deleted": True})
    assert r.status_code == 409 and "hide it" in r.text, r.text
    assert decisions.read(video / "b.mp4") is None

    # With its clips in the same gesture, it goes.
    r = client.post("/api/decide/bulk", json={
        "deleted": True, "files": [
            {"folder": "init_2026", "name": "b.mp4"},
            {"folder": "init_2026", "name": clip}]})
    assert r.status_code == 200, r.text
    assert decisions.read(video / "b.mp4") == Decision(deleted=True)


def test_restoring_a_clip_restores_its_source(
    client: TestClient, video: Path
) -> None:
    [clip] = _make(client, (0, 30))
    client.post("/api/decide/bulk", json={"deleted": True, "files": [
        {"folder": "init_2026", "name": "b.mp4"},
        {"folder": "init_2026", "name": clip}]})

    r = client.post("/api/decide", json={
        "folder": "init_2026", "name": clip, "deleted": False})
    assert r.status_code == 200, r.text
    assert decisions.read(video / "b.mp4") is None, "the source stayed binned"


def test_purging_a_source_purges_its_clips(
    client: TestClient, video: Path, app_env: dict[str, Path]
) -> None:
    [clip] = _make(client, (0, 30))
    client.post("/api/decide/bulk", json={"deleted": True, "files": [
        {"folder": "init_2026", "name": "b.mp4"},
        {"folder": "init_2026", "name": clip}]})

    r = client.post("/api/purge", json={"files": [
        {"folder": "init_2026", "name": "b.mp4"}]})
    assert r.status_code == 200 and r.json()["purged"] == 1, r.text
    assert not (video / "b.mp4").exists()
    assert not decisions.sidecar_path(video / clip).exists()
    assert _row(app_env, clip) is None


def test_a_viewer_never_sees_a_clip_that_has_no_files(
    client: TestClient, video: Path, add_user: Callable[..., None],
    sign_in: Callable[[str, str], TestClient]
) -> None:
    """Until it is cut it can only play as its source, and handing a viewer
    the source is handing them all of it."""
    [clip] = _make(client, (0, 30))
    client.post("/api/decide/bulk", json={"add_audience": ["kid"], "files": [
        {"folder": "init_2026", "name": "b.mp4"},
        {"folder": "init_2026", "name": clip}]})
    add_user("kid", "pw")
    kid = sign_in("kid", "pw")

    assert clip not in kid.get("/browse").text
    assert kid.get(f"/media/init_2026/{clip}").status_code == 404
    assert kid.get(f"/thumb/init_2026/{clip}").status_code == 404


def test_a_curator_sees_a_clip_through_its_source(
    client: TestClient, video: Path, app_env: dict[str, Path]
) -> None:
    [clip] = _make(client, (0, 30))
    thumb = app_env["thumb"] / "init_2026" / "b.mp4.jpg"
    assert client.get(f"/thumb/init_2026/{clip}").content == thumb.read_bytes()
    assert client.get(f"/media/init_2026/{clip}").status_code == 200
    # Not a download of the source: that would be the wrong file.
    assert client.get(f"/download/init_2026/{clip}").status_code == 404


def test_a_rebuild_finds_the_clips(
    client: TestClient, video: Path, app_env: dict[str, Path]
) -> None:
    made = _make(client, (0, 30), (5, 5))
    _rebuild(app_env)
    rows = ix.clips_of(ix.connect(app_env["db"]), "init_2026", "b.mp4")
    assert sorted(str(r["name"]) for r in rows) == sorted(made)


def test_a_clip_with_no_source_is_not_indexed(
    app_env: dict[str, Path], writable: Path
) -> None:
    decisions.write(writable / "gone.mp4~k3fa",
                    Decision(clip_in=0.0, clip_out=1.0))
    _rebuild(app_env)
    assert _row(app_env, "gone.mp4~k3fa") is None


def test_the_viewer_plays_a_clip_between_its_ends() -> None:
    js = web._BROWSE_JS
    assert "#t=${clip[0]},${clip[1]}" in js


# --- the splice page (spec/clips.md §9) --------------------------------------

def _playable(app_env: dict[str, Path]) -> None:
    """Make `b.mp4` H.264, which a browser plays as it is."""
    meta = app_env["share"] / "meta" / "init_2026" / "b.mp4.json"
    record = json.loads(meta.read_text(encoding="utf-8"))
    record["exif"]["QuickTime:CompressorID"] = "avc1"
    record["exif"]["QuickTime:VideoFrameRate"] = 25
    meta.write_text(json.dumps(record), encoding="utf-8")


def test_the_splice_page_shows_the_video_and_its_clips(
    client: TestClient, video: Path, app_env: dict[str, Path]
) -> None:
    _playable(app_env)
    [clip] = _make(client, (0, 30))
    html = client.get("/splice/init_2026/b.mp4").text
    assert 'src="/media/init_2026/b.mp4"' in html
    state = json.loads(html.split("const SPLICE=", 1)[1].split(";</script>")[0])
    assert state["fps"] == 25 and state["duration"] == 75
    assert [c["name"] for c in state["clips"]] == [clip]


def test_a_video_that_will_not_play_waits_for_processing(
    client: TestClient, video: Path
) -> None:
    """HEVC with no render: the page could not show what it is cutting."""
    html = client.get("/splice/init_2026/b.mp4").text
    assert "waiting for processing" in html
    assert "const SPLICE=" not in html


def test_splicing_a_clip_opens_its_source_on_it(
    client: TestClient, video: Path
) -> None:
    [clip] = _make(client, (0, 30))
    r = client.get(f"/splice/init_2026/{clip}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/splice/init_2026/b.mp4#")


def test_a_photograph_has_nothing_to_splice(
    client: TestClient, video: Path
) -> None:
    assert "only a video" in client.get("/splice/init_2026/a.jpg").text


def test_splicing_is_an_administrators(
    client: TestClient, video: Path, add_user: Callable[..., None],
    sign_in: Callable[[str, str], TestClient]
) -> None:
    add_user("kid", "pw")
    kid = sign_in("kid", "pw")
    assert kid.get("/splice/init_2026/b.mp4").status_code in (401, 403)
    assert 'data-act="splice"' not in kid.get("/browse").text
    assert 'data-act="splice"' in client.get("/browse").text


def test_the_grid_says_what_splice_would_open(
    client: TestClient, video: Path
) -> None:
    [clip] = _make(client, (0, 30))
    html = client.get("/browse").text
    assert 'data-name="b.mp4"' in html
    cell = html.split(f'data-name="{clip}"', 1)[1].split(">", 1)[0]
    assert 'data-splice="b.mp4"' in cell
    photo = html.split('data-name="a.jpg"', 1)[1].split(">", 1)[0]
    assert 'data-splice=""' in photo


def _drive(tmp_path: Path, scenario: str,
           cut: list[dict[str, Any]]) -> list[dict[str, Any]]:
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    script = tmp_path / "splice.js"
    script.write_text(web._SPLICE_JS, encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "folder": "init_2026", "source": "b.mp4", "duration": 75, "fps": 25,
        "hidden": False, "hiddenName": decisions.HIDDEN, "clips": cut}),
        encoding="utf-8")
    harness = Path(__file__).parent / "js" / "splice.js"
    result = subprocess.run(["node", str(harness), str(script), str(state),
                             scenario], capture_output=True, text=True,
                            timeout=60)
    assert result.stdout.strip().endswith("OK"), result.stdout + result.stderr
    return [json.loads(line) for line in result.stdout.splitlines()[:-1]]


def test_split_on_a_fresh_video_makes_two_clips_of_the_whole(
    tmp_path: Path
) -> None:
    """Three splits are four clips, which is how cutting a video up is
    thought about."""
    [sent] = _drive(tmp_path, "split-fresh", [])
    assert sent["url"] == "/api/clips/make"
    assert sent["body"]["clips"] == [{"start": 0, "end": 30},
                                     {"start": 30, "end": 75}]


def test_split_inside_a_clip_cuts_that_clip(tmp_path: Path) -> None:
    [sent] = _drive(tmp_path, "split-inside", [
        {"name": "b.mp4~aaaa", "start": 0, "end": 30, "deleted": False}])
    assert sent == {"url": "/api/clips/split",
                    "body": {"folder": "init_2026", "name": "b.mp4~aaaa",
                             "at": 10}}


def test_a_still_is_taken_at_the_millisecond(tmp_path: Path) -> None:
    [sent] = _drive(tmp_path, "still", [])
    assert sent["body"]["clips"] == [{"start": 12.346, "end": 12.346}]


def test_hiding_the_original_is_the_hidden_audience(tmp_path: Path) -> None:
    [sent] = _drive(tmp_path, "hide", [])
    assert sent == {"url": "/api/decide",
                    "body": {"folder": "init_2026", "name": "b.mp4",
                             "add_audience": [decisions.HIDDEN]}}
