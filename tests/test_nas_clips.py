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
                      audience=(decisions.ARCHIVED,), deleted=True,
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
    assert r.status_code == 409 and "archive it" in r.text, r.text
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
        "hidden": False, "hiddenName": decisions.ARCHIVED, "clips": cut}),
        encoding="utf-8")
    harness = Path(__file__).parent / "js" / "splice.js"
    result = subprocess.run(["node", str(harness), str(script), str(state),
                             scenario], capture_output=True, text=True,
                            timeout=60)
    assert result.stdout.strip().endswith("OK"), result.stdout + result.stderr
    return [json.loads(line) for line in result.stdout.splitlines()[:-1]]


def _clip(name: str, start: float, end: float) -> dict[str, Any]:
    return {"name": name, "start": start, "end": end, "deleted": False}


def _saved(sent: list[dict[str, Any]]) -> dict[str, Any]:
    """The one save the page sent — and nothing else it wrote."""
    [only] = sent
    assert only["url"] == "/api/clips/save", only
    return only["body"]


def test_a_new_clip_is_made_with_in_and_out_and_saved(tmp_path: Path) -> None:
    body = _saved(_drive(tmp_path, "new-keys", []))
    assert body["clips"] == [{"id": "new1", "start": 10, "end": 20,
                              "copy_of": None, "absorbs": []}]
    assert body["deleted"] == []


def test_nothing_is_written_until_save(tmp_path: Path) -> None:
    assert _drive(tmp_path, "nothing", []) == []
    assert _drive(tmp_path, "discard", []) == []


def test_a_split_part_says_which_clip_it_copies(tmp_path: Path) -> None:
    body = _saved(_drive(tmp_path, "split", [_clip("b.mp4~aaaa", 0, 30)]))
    assert body["clips"] == [
        {"id": "b.mp4~aaaa", "start": 0, "end": 12, "copy_of": None,
         "absorbs": []},
        {"id": "new1", "start": 12, "end": 30, "copy_of": "b.mp4~aaaa",
         "absorbs": []}]


def test_a_join_says_which_clip_it_absorbed(tmp_path: Path) -> None:
    body = _saved(_drive(tmp_path, "join", [
        _clip("b.mp4~aaaa", 0, 10), _clip("b.mp4~bbbb", 10, 20)]))
    assert body["clips"] == [{"id": "b.mp4~aaaa", "start": 0, "end": 20,
                              "copy_of": None, "absorbs": ["b.mp4~bbbb"]}]


def test_a_delete_names_the_clip(tmp_path: Path) -> None:
    body = _saved(_drive(tmp_path, "delete", [_clip("b.mp4~aaaa", 0, 10)]))
    assert body["clips"] == [] and body["deleted"] == ["b.mp4~aaaa"]


def test_a_new_clip_over_another_removes_it(tmp_path: Path) -> None:
    """Asked first, and answered yes here."""
    body = _saved(_drive(tmp_path, "swallow", [_clip("b.mp4~bbbb", 12, 15)]))
    assert [c["id"] for c in body["clips"]] == ["new1"]
    assert body["deleted"] == ["b.mp4~bbbb"]


def test_a_still_is_taken_at_the_millisecond(tmp_path: Path) -> None:
    body = _saved(_drive(tmp_path, "still", []))
    assert [(c["start"], c["end"]) for c in body["clips"]] == [
        (12.346, 12.346)]


def test_hiding_the_original_is_the_hidden_audience(tmp_path: Path) -> None:
    [sent] = _drive(tmp_path, "hide", [])
    assert sent == {"url": "/api/decide",
                    "body": {"folder": "init_2026", "name": "b.mp4",
                             "add_audience": [decisions.ARCHIVED]}}


# --- cuts (spec/clips.md §6) ---------------------------------------------------

from pix.nas import cut  # noqa: E402
from pix.nas import paths  # noqa: E402


def test_nothing_but_a_copy_is_ever_run() -> None:
    """The app's first media tool must not be how encoding reaches the Atom."""
    cut.copy_only(["ffmpeg", "-i", "a", "-c", "copy", "b"])
    for bad in (["-c:v", "libx264"], ["-vf", "scale=2"], ["-c", "aac"]):
        with pytest.raises(cut.CutError):
            cut.copy_only(["ffmpeg", "-i", "a", *bad, "b"])


def test_the_command_is_a_copy_from_the_start(tmp_path: Path) -> None:
    args = cut.command("ffmpeg", tmp_path / "f" / "b.mp4", 1.0, 3.5,
                       tmp_path / "b.mp4~aaaa@1-3.5.cut.mp4", created=None)
    assert args[args.index("-c") + 1] == "copy"
    assert args.index("-ss") < args.index("-i"), "must seek by keyframe"
    assert "pix:ClipId=aaaa" in args and "pix:ClipRange=1-3.5" in args


def test_a_cut_is_named_by_its_range(tmp_path: Path) -> None:
    """So one made for a range that has since moved is stale by its name."""
    media = tmp_path / "f" / "b.mp4~aaaa"
    a = paths.cut_path(media, tmp_path / "render", 1.0, 3.5)
    assert a.name == "b.mp4~aaaa@1-3.5.cut.mp4"
    assert a != paths.cut_path(media, tmp_path / "render", 1.0, 3.6)


def test_a_start_snaps_to_the_nearest_keyframe_it_may_use() -> None:
    keys = (0.0, 1.0, 2.0, 3.0)
    assert cut.snap(1.4, keys) == 1.0
    assert cut.snap(1.6, keys) == 2.0
    # Never back over the clip before it.
    assert cut.snap(1.4, keys, lo=1.2) == 2.0
    assert cut.snap(1.4, keys, lo=3.5) is None


def test_two_touching_ranges_stay_touching_when_they_snap() -> None:
    got = web._snapped((0.0, 1.0, 2.0, 3.0), [(0, 1.4), (1.4, 4)], [])
    assert got == [(0.0, 1.0), (1.0, 4)]


def test_a_snapped_start_never_overlaps_a_sibling() -> None:
    got = web._snapped((0.0, 1.0, 2.0, 3.0), [(1.3, 4)], [(0, 1.3)])
    assert got == [(2.0, 4)]


@pytest.fixture
def real(app_env: dict[str, Path], writable: Path,
         monkeypatch: pytest.MonkeyPatch) -> Path:
    """`b.mp4` as a real four-second H.264 video, a keyframe every second,
    indexed — and cuts made before a request returns."""
    import shutil
    import subprocess

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg is not installed")
    out = writable / "b.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=160x90:rate=25",
         "-f", "lavfi", "-i", "sine=frequency=440",
         "-t", "4", "-c:v", "libx264", "-g", "25", "-keyint_min", "25",
         "-sc_threshold", "0", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(out)], check=True, timeout=60)
    share = app_env["share"]
    (share / "meta" / "init_2026" / "b.mp4.json").write_text(json.dumps({
        "file": "b.mp4", "folder": "init_2026", "size": out.stat().st_size,
        "mtime_ns": 1,
        "exif": {"QuickTime:Duration": "4 s",
                 "QuickTime:CompressorID": "avc1",
                 "QuickTime:VideoFrameRate": 25,
                 "QuickTime:CreateDate": "2026:08:30 19:00:00",
                 "EXIF:DateTimeOriginal": "2026:08:30 15:00:00",
                 "XMP:EventAuto": "Italy - Sicily"},
    }), encoding="utf-8")
    _rebuild(app_env)
    monkeypatch.setattr(web._CUTS, "immediate", True)
    return writable


def _cuts(name: str) -> list[Path]:
    folder = web.RENDER_DIR / "init_2026"
    return sorted(folder.glob(f"{name}@*.cut.mp4")) if folder.is_dir() else []


def test_the_keyframes_are_read_without_decoding(real: Path) -> None:
    assert cut.keyframes(real / "b.mp4") == (0.0, 1.0, 2.0, 3.0)


def test_a_clip_is_cut_from_a_keyframe_and_a_viewer_can_then_see_it(
    client: TestClient, real: Path, app_env: dict[str, Path],
    add_user: Callable[..., None], sign_in: Callable[[str, str], TestClient]
) -> None:
    [clip] = _make(client, (1.3, 3.2))

    got = decisions.read(real / clip)
    assert got is not None and (got.clip_in, got.clip_out) == (1.0, 3.2)
    [made] = _cuts(clip)
    assert made.name == clip + "@1-3.2.cut.mp4"
    assert _have(app_env, clip)["size"] == made.stat().st_size

    client.post("/api/decide", json={"folder": "init_2026", "name": clip,
                                     "add_audience": ["kid"]})
    add_user("kid", "pw")
    kid = sign_in("kid", "pw")
    assert clip in kid.get("/browse").text
    assert kid.get(f"/media/init_2026/{clip}").content == made.read_bytes()
    r = kid.get(f"/download/init_2026/{clip}?original=1")
    assert r.content == made.read_bytes()
    assert clip + ".mp4" in r.headers["content-disposition"]


def test_a_cut_says_what_it_is(real: Path, client: TestClient) -> None:
    import subprocess

    [clip] = _make(client, (1, 3))
    [made] = _cuts(clip)
    tags = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags",
         "-of", "json", str(made)], capture_output=True, text=True,
        check=True).stdout
    found = json.loads(tags)["format"]["tags"]
    assert found["pix:ClipId"] == clips.id_of(clip)
    assert found["pix:ClipRange"] == "1-3"
    assert found["creation_time"].startswith("2026-08-30T19:00:01")


def test_moving_a_clip_replaces_its_cut(real: Path, client: TestClient) -> None:
    [clip] = _make(client, (1, 3))
    client.post("/api/clips/range", json={
        "folder": "init_2026", "name": clip, "start": 2, "end": 3.5})
    assert [p.name for p in _cuts(clip)] == [clip + "@2-3.5.cut.mp4"]


def test_splitting_on_a_keyframe(real: Path, client: TestClient) -> None:
    [clip] = _make(client, (0, 4))
    r = client.post("/api/clips/split", json={
        "folder": "init_2026", "name": clip, "at": 2.2})
    assert r.status_code == 200, r.text
    assert r.json()["at"] == 2.0
    assert len(_cuts(clip)) == 1 and len(_cuts(r.json()["second"])) == 1


def test_binning_the_source_can_keep_its_clips_as_files(
    real: Path, client: TestClient, app_env: dict[str, Path]
) -> None:
    [clip] = _make(client, (1, 3))
    client.post("/api/decide", json={"folder": "init_2026", "name": clip,
                                     "add_people": ["Mum"]})
    r = client.post("/api/clips/free", json={"folder": "init_2026",
                                             "source": "b.mp4"})
    assert r.status_code == 200, r.text
    [own] = r.json()["freed"]
    assert own == clip + ".mp4"

    assert (real / own).is_file()
    assert decisions.read(real / own) == Decision(event="Italy - Sicily",
                                                   people=("Mum",))
    assert not decisions.sidecar_path(real / clip).exists()
    assert _row(app_env, clip) is None
    assert decisions.read(real / "b.mp4") == Decision(deleted=True)
    row = _have(app_env, own)
    assert row["kind"] == "video" and row["clip_of"] is None
    assert row["effective_date"] == "2026-08-30-15:00:01"
    record = json.loads((app_env["share"] / "meta" / "init_2026" /
                         (own + ".json")).read_text(encoding="utf-8"))
    assert record["placeholder"] is True
    # Its old source's picture stands in until `process` makes its own.
    assert client.get(f"/thumb/init_2026/{own}").status_code == 200


def test_clips_are_not_kept_as_files_before_they_are_cut(
    client: TestClient, video: Path
) -> None:
    _make(client, (0, 30))
    r = client.post("/api/clips/free", json={"folder": "init_2026",
                                             "source": "b.mp4"})
    assert r.status_code == 409 and "being cut" in r.text, r.text


def test_purging_a_clip_removes_its_cut(
    real: Path, client: TestClient
) -> None:
    [clip] = _make(client, (1, 3))
    client.post("/api/decide", json={"folder": "init_2026", "name": clip,
                                     "deleted": True})
    r = client.post("/api/purge", json={"files": [
        {"folder": "init_2026", "name": clip}]})
    assert r.json()["purged"] == 1, r.text
    assert _cuts(clip) == []


def test_process_replaces_a_placeholder_record(tmp_path: Path) -> None:
    from pix.nas import derive

    path = tmp_path / "x.mp4.json"
    path.write_text(json.dumps({"placeholder": True}), encoding="utf-8")
    assert derive._placeholder(path)  # pyright: ignore[reportPrivateUsage]
    path.write_text(json.dumps({"file": "x.mp4"}), encoding="utf-8")
    assert not derive._placeholder(path)  # pyright: ignore[reportPrivateUsage]


def test_a_clip_to_the_real_end_is_not_refused_for_rounding() -> None:
    """ExifTool writes a long duration in whole seconds, so the page's end
    of the video is a fraction past the one on record."""
    clips.check(300, 344.3, siblings=[], duration=344)
    with pytest.raises(clips.ClipError):
        clips.check(300, 346, siblings=[], duration=344)



def test_neighbours_join_across_the_stretch_between(
    client: TestClient, video: Path
) -> None:
    """Taking away the marker at the end of a clip grows it to the next one,
    and the uncut stretch between them comes with it."""
    a, b = _make(client, (0, 10), (20, 30))
    r = client.post("/api/clips/merge", json={
        "folder": "init_2026", "first": a, "second": b})
    assert r.status_code == 200, r.text
    got = decisions.read(video / a)
    assert got is not None and (got.clip_in, got.clip_out) == (0.0, 30.0)


def test_a_join_never_swallows_a_clip_between(
    client: TestClient, video: Path
) -> None:
    a, _, c = _make(client, (0, 10), (12, 15), (20, 30))
    r = client.post("/api/clips/merge", json={
        "folder": "init_2026", "first": a, "second": c})
    assert r.status_code == 409, r.text


def test_a_cut_moves_both_clips_at_once(
    client: TestClient, video: Path
) -> None:
    a, b = _make(client, (0, 10), (10, 20))
    r = client.post("/api/clips/boundary", json={
        "folder": "init_2026", "first": a, "second": b, "at": 14})
    assert r.status_code == 200, r.text
    first, second = decisions.read(video / a), decisions.read(video / b)
    assert first is not None and second is not None
    assert (first.clip_out, second.clip_in) == (14.0, 14.0)
    assert history.recent(1)[0].summary.startswith("moved the cut")



def test_the_splice_page_draws_the_filmstrip(
    client: TestClient, video: Path, app_env: dict[str, Path]
) -> None:
    from pix.nas import paths as tier_paths

    _playable(app_env)
    info = tier_paths.strip_info_path(video / "b.mp4", web.STRIP_DIR)
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_text(json.dumps({"n": 15, "w": 160, "h": 90}), encoding="utf-8")
    html = client.get("/splice/init_2026/b.mp4").text
    state = json.loads(html.split("const SPLICE=", 1)[1].split(";</script>")[0])
    assert state["strip"] == {"n": 15, "w": 160, "h": 90,
                              "url": "/strip/init_2026/b.mp4"}


def test_the_splice_page_offers_four_speeds() -> None:
    js = web._SPLICE_JS
    assert "const RATES=[0.5,1,1.5,2];" in js
    assert "v.playbackRate=r" in js


# --- a clip shows its source only to someone who may see it (§7) ------------

def _shared_clip(client: TestClient, add_user: Callable[..., None],
                 sign_in: Callable[[str, str], TestClient],
                 *, source_too: bool) -> tuple[str, TestClient]:
    [clip] = _make(client, (1, 3))
    names = [clip, "b.mp4"] if source_too else [clip]
    client.post("/api/decide/bulk", json={"add_audience": ["kid"], "files": [
        {"folder": "init_2026", "name": n} for n in names]})
    add_user("kid", "pw")
    return clip, sign_in("kid", "pw")


def test_a_viewer_given_only_the_clip_learns_nothing_of_its_source(
    client: TestClient, real: Path, add_user: Callable[..., None],
    sign_in: Callable[[str, str], TestClient]
) -> None:
    """Not a badge saying it was cut from something, not a link to it, and
    not a picture of it: each would say there is more footage than they were
    given, and the picture would show them some of it."""
    clip, kid = _shared_clip(client, add_user, sign_in, source_too=False)

    html = kid.get("/browse").text
    assert clip in html, "the clip itself is theirs to see"
    assert 'class="clip-mark"' not in html
    assert kid.get(f"/api/file/init_2026/{clip}").json()["clip"] is None
    # No picture of its own yet, and not the source's instead.
    assert kid.get(f"/thumb/init_2026/{clip}").status_code == 404


def test_a_viewer_who_may_see_the_source_sees_where_the_clip_came_from(
    client: TestClient, real: Path, add_user: Callable[..., None],
    sign_in: Callable[[str, str], TestClient]
) -> None:
    clip, kid = _shared_clip(client, add_user, sign_in, source_too=True)

    assert 'class="clip-mark"' in kid.get("/browse").text
    got = kid.get(f"/api/file/init_2026/{clip}").json()["clip"]
    assert got["source"] == "b.mp4" and got["splice"] is None
    assert got["open"].endswith("#open:init_2026%2Fb.mp4")
    assert kid.get(f"/thumb/init_2026/{clip}").status_code == 200


def test_the_curator_can_go_from_a_clip_to_its_timeline(
    client: TestClient, real: Path
) -> None:
    [clip] = _make(client, (1, 3))
    got = client.get(f"/api/file/init_2026/{clip}").json()["clip"]
    assert got["splice"].startswith("/splice/init_2026/b.mp4#")
    assert (got["start"], got["end"]) == (1.0, 3.0)


def test_process_gives_a_clip_pictures_of_its_own(
    client: TestClient, real: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """From a frame inside the clip, so a viewer given only the clip gets a
    picture, and one they are entitled to."""
    from pix.nas import derive

    [clip] = _make(client, (1, 3))
    monkeypatch.setattr(derive, "MASTER_DIR", web.MASTER_DIR)
    monkeypatch.setattr(derive, "THUMB_DIR", web.THUMB_DIR)
    monkeypatch.setattr(derive, "LARGE_DIR", web.LARGE_DIR)
    monkeypatch.setattr(derive, "PREVIEW_DIR", web.PREVIEW_DIR)

    assert real / clip in derive.pending_files()
    summary = derive.ProcessSummary()
    derive._derive_one(real / clip, summary, derive.threading.Lock(),  # pyright: ignore[reportPrivateUsage]
                       derive._ExifPool(), {"cancelling": 0})  # pyright: ignore[reportPrivateUsage]
    assert summary.thumbs == 1, summary.failed
    assert (web.THUMB_DIR / "init_2026" / (clip + ".jpg")).is_file()
    assert real / clip not in derive.pending_files()



def test_cut_from_stays_in_the_view_it_was_opened_from(
    client: TestClient, real: Path
) -> None:
    """The filters somebody built up are not the link's to throw away: where
    the source is in the view, the page goes to it there."""
    [clip] = _make(client, (1, 3))
    inside = client.get(f"/api/file/init_2026/{clip}?kind=video").json()["clip"]
    assert inside["in_view"] is True
    outside = client.get(f"/api/file/init_2026/{clip}?kind=image").json()["clip"]
    assert outside["in_view"] is False
    assert "#open:" in outside["open"]


def test_a_link_can_open_the_preview_it_lands_on() -> None:
    js = web._BROWSE_JS
    assert "want.startsWith('open:')" in js
    assert "window.addEventListener('hashchange',land)" in js


def test_a_videos_details_list_its_clips(
    client: TestClient, real: Path
) -> None:
    a, b = _make(client, (0, 1), (2, 3))
    got = client.get("/api/file/init_2026/b.mp4").json()["clips"]
    assert [c["name"] for c in got] == [a, b]
    # A clip names its source, and no more: the source lists the rest.
    assert client.get(f"/api/file/init_2026/{a}").json()["clips"] == []


def test_a_viewer_sees_only_the_clips_they_may_see(
    client: TestClient, real: Path, add_user: Callable[..., None],
    sign_in: Callable[[str, str], TestClient]
) -> None:
    """And none of a clip's siblings unless they may see its source: *these
    came from one video* says there is one."""
    a, b = _make(client, (0, 1), (2, 3))
    client.post("/api/decide/bulk", json={"add_audience": ["kid"], "files": [
        {"folder": "init_2026", "name": a},
        {"folder": "init_2026", "name": b}]})
    add_user("kid", "pw")
    kid = sign_in("kid", "pw")
    assert kid.get(f"/api/file/init_2026/{a}").json()["clips"] == []

    client.post("/api/decide", json={"folder": "init_2026", "name": "b.mp4",
                                     "add_audience": ["kid"]})
    client.post("/api/decide", json={"folder": "init_2026", "name": b,
                                     "remove_audience": ["kid"]})
    listed = kid.get("/api/file/init_2026/b.mp4").json()["clips"]
    assert [c["name"] for c in listed] == [a]


def test_splice_is_in_the_preview_for_the_administrator(
    client: TestClient, video: Path, add_user: Callable[..., None],
    sign_in: Callable[[str, str], TestClient]
) -> None:
    assert 'id="viewsplice"' in client.get("/browse").text
    add_user("kid", "pw")
    assert 'id="viewsplice"' not in sign_in("kid", "pw").get("/browse").text


# --- saving a draft by identity ------------------------------------------------

def _save(client: TestClient, clips_: list[dict[str, Any]],
          deleted: list[str] | None = None) -> Any:
    return client.post("/api/clips/save", json={
        "folder": "init_2026", "source": "b.mp4", "clips": clips_,
        "deleted": deleted or []})


def _draft(name: str, start: float, end: float, **more: Any) -> dict[str, Any]:
    return {"id": name, "start": start, "end": end, **more}


def test_a_retrimmed_clip_keeps_everything_decided_about_it(
    client: TestClient, video: Path
) -> None:
    """However its ends moved in between: only the end state is saved, and
    the clip is still itself."""
    [a] = _make(client, (0, 10))
    client.post("/api/decide", json={"folder": "init_2026", "name": a,
                                     "add_people": ["Mum"]})
    r = _save(client, [_draft(a, 2, 12)])
    assert r.status_code == 200, r.text
    got = decisions.read(video / a)
    assert got is not None
    assert (got.clip_in, got.clip_out, got.people) == (2.0, 12.0, ("Mum",))


def test_a_saved_split_copies_and_a_saved_join_merges(
    client: TestClient, video: Path
) -> None:
    a, b = _make(client, (0, 10), (20, 30))
    client.post("/api/decide", json={"folder": "init_2026", "name": a,
                                     "add_tags": ["beach"]})
    client.post("/api/decide", json={"folder": "init_2026", "name": b,
                                     "add_people": ["Mum"]})
    r = _save(client, [_draft(a, 0, 5, absorbs=[]),
                       _draft("new1", 5, 30, copy_of=a, absorbs=[b])])
    assert r.status_code == 200, r.text
    part = r.json()["names"]["new1"]
    first, second = decisions.read(video / a), decisions.read(video / part)
    assert first is not None and second is not None
    assert (first.clip_out, first.tags) == (5.0, ("beach",))
    assert (second.clip_in, second.clip_out) == (5.0, 30.0)
    assert second.tags == ("beach",) and second.people == ("Mum",)
    assert not decisions.sidecar_path(video / b).exists()


def test_a_saved_delete_goes_to_the_bin(client: TestClient, video: Path) -> None:
    [a] = _make(client, (0, 10))
    r = _save(client, [], deleted=[a])
    assert r.status_code == 200, r.text
    got = decisions.read(video / a)
    assert got is not None and got.deleted


def test_a_new_clip_starts_with_the_videos_tags(
    client: TestClient, video: Path
) -> None:
    r = _save(client, [_draft("new1", 1, 4)])
    assert r.status_code == 200, r.text
    got = decisions.read(video / r.json()["names"]["new1"])
    assert got == Decision(event="Italy - Sicily", clip_in=1.0, clip_out=4.0)


def test_a_draft_made_against_other_clips_is_refused(
    client: TestClient, video: Path
) -> None:
    """A saved clip the draft does not account for means the clips changed
    since the page loaded."""
    _make(client, (0, 10))
    r = _save(client, [_draft("new1", 20, 30)])
    assert r.status_code == 409 and "reload" in r.text, r.text


def test_a_draft_that_overlaps_itself_is_refused(
    client: TestClient, video: Path
) -> None:
    r = _save(client, [_draft("new1", 0, 10), _draft("new2", 5, 15)])
    assert r.status_code == 409 and "overlap" in r.text, r.text


def test_one_save_is_one_thing_to_take_back(
    client: TestClient, video: Path
) -> None:
    a, b = _make(client, (0, 10), (20, 30))
    _save(client, [_draft(a, 0, 30, absorbs=[b])])
    op = history.recent(1)[0]
    assert op.summary == "edited the clips of b.mp4"
    client.post("/history/revert", data={"id": op.id})
    first, second = decisions.read(video / a), decisions.read(video / b)
    assert first is not None and second is not None
    assert (first.clip_out, second.clip_in) == (10.0, 20.0)


# --- a still's file, and a playback render for HEVC (spec/clips.md §6) --------

def _process_clip(clip: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Run `process` over one clip, pointed at the app's tiers."""
    from pix.nas import derive

    for tier in ("MASTER_DIR", "THUMB_DIR", "LARGE_DIR", "PREVIEW_DIR",
                 "RENDER_DIR", "META_DIR"):
        monkeypatch.setattr(derive, tier, getattr(web, tier))
    summary = derive.ProcessSummary()
    derive._derive_one(clip, summary, derive.threading.Lock(),  # pyright: ignore[reportPrivateUsage]
                       derive._ExifPool(), {"cancelling": 0})  # pyright: ignore[reportPrivateUsage]
    ix.refresh(ix.connect(web.DB_PATH), "init_2026", clip.name,
               meta_dir=web.META_DIR, master_dir=web.MASTER_DIR)
    return summary


def test_a_still_becomes_a_photograph_a_viewer_can_have(
    client: TestClient, real: Path, monkeypatch: pytest.MonkeyPatch,
    add_user: Callable[..., None], sign_in: Callable[[str, str], TestClient]
) -> None:
    import subprocess

    [still] = _make(client, (1.5, 1.5))
    client.post("/api/decide", json={"folder": "init_2026", "name": still,
                                     "add_audience": ["kid"]})
    add_user("kid", "pw")
    kid = sign_in("kid", "pw")
    assert still not in kid.get("/browse").text, "shown before it has a file"

    summary = _process_clip(real / still, monkeypatch)
    assert summary.stills == 1, summary.failed
    shot = web.RENDER_DIR / "init_2026" / (still + "@1.5.still.jpg")
    assert shot.is_file()
    taken = subprocess.run(["exiftool", "-s3", "-DateTimeOriginal", str(shot)],
                           capture_output=True, text=True).stdout.strip()
    assert taken == "2026:08:30 15:00:01", taken

    assert still in kid.get("/browse").text
    r = kid.get(f"/download/init_2026/{still}?original=1")
    assert r.content == shot.read_bytes()
    assert still + ".jpg" in r.headers["content-disposition"]


@pytest.fixture
def hevc(app_env: dict[str, Path], writable: Path,
         monkeypatch: pytest.MonkeyPatch) -> Path:
    """`b.mp4` as four seconds of HEVC, which no browser here will play."""
    import shutil
    import subprocess

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    out = writable / "b.mp4"
    done = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
         "testsrc=size=160x90:rate=25", "-t", "4", "-c:v", "libx265",
         "-x265-params", "keyint=25:min-keyint=25:log-level=error",
         "-tag:v", "hvc1", "-pix_fmt", "yuv420p", str(out)],
        capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        pytest.skip("this ffmpeg cannot encode HEVC")
    share = app_env["share"]
    (share / "meta" / "init_2026" / "b.mp4.json").write_text(json.dumps({
        "file": "b.mp4", "folder": "init_2026", "size": out.stat().st_size,
        "mtime_ns": 1,
        "exif": {"QuickTime:Duration": "4 s", "QuickTime:CompressorID": "hvc1",
                 "EXIF:DateTimeOriginal": "2026:08:30 15:00:00"},
    }), encoding="utf-8")
    _rebuild(app_env)
    monkeypatch.setattr(web._CUTS, "immediate", True)
    return writable


def test_a_clip_of_hevc_is_seen_once_it_has_a_render(
    client: TestClient, hevc: Path, app_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch, add_user: Callable[..., None],
    sign_in: Callable[[str, str], TestClient]
) -> None:
    """Its cut is lossless HEVC and plays nowhere; the render is what a
    viewer is given."""
    import subprocess

    [clip] = _make(client, (1, 3))
    assert _cuts(clip), "the NAS still cuts it"
    assert _have(app_env, clip)["size"] is None
    client.post("/api/decide", json={"folder": "init_2026", "name": clip,
                                     "add_audience": ["kid"]})
    add_user("kid", "pw")
    kid = sign_in("kid", "pw")
    assert clip not in kid.get("/browse").text

    summary = _process_clip(hevc / clip, monkeypatch)
    assert summary.renders == 1, summary.failed
    play = web.RENDER_DIR / "init_2026" / (clip + "@1-3.play.mp4")
    codec = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=codec_name", "-of", "csv=p=0", str(play)],
        capture_output=True, text=True).stdout.strip()
    assert codec == "h264"

    assert clip in kid.get("/browse").text
    assert kid.get(f"/media/init_2026/{clip}").content == play.read_bytes()
    # The original is still the lossless cut, in its own codec.
    original = kid.get(f"/download/init_2026/{clip}?original=1").content
    assert original == _cuts(clip)[0].read_bytes()


def test_moving_a_clip_throws_away_its_render(
    client: TestClient, hevc: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    [clip] = _make(client, (1, 3))
    _process_clip(hevc / clip, monkeypatch)
    assert list((web.RENDER_DIR / "init_2026").glob(f"{clip}@*.play.mp4"))
    client.post("/api/clips/range", json={
        "folder": "init_2026", "name": clip, "start": 2, "end": 3})
    assert not list((web.RENDER_DIR / "init_2026").glob(f"{clip}@*.play.mp4"))
    assert _cuts(clip), "the cut is the NAS's, and it re-cut"


def test_a_video_with_photos_can_keep_them_as_files(
    client: TestClient, real: Path, app_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch
) -> None:
    [clip, still] = _make(client, (1, 3), (3.5, 3.5))
    _process_clip(real / still, monkeypatch)
    r = client.post("/api/clips/free", json={"folder": "init_2026",
                                             "source": "b.mp4"})
    assert r.status_code == 200, r.text
    assert sorted(r.json()["freed"]) == sorted([clip + ".mp4", still + ".jpg"])
    assert _have(app_env, still + ".jpg")["kind"] == "image"


def test_the_scan_finds_a_still_with_no_file(
    client: TestClient, real: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pictures made already, and still pending: its file is what a viewer
    is given, and the scan has to find that missing too."""
    from pix.nas import derive

    [still] = _make(client, (1.5, 1.5))
    for tier in ("MASTER_DIR", "THUMB_DIR", "LARGE_DIR", "PREVIEW_DIR",
                 "RENDER_DIR", "META_DIR"):
        monkeypatch.setattr(derive, tier, getattr(web, tier))
    for tier in ("THUMB_DIR", "LARGE_DIR", "PREVIEW_DIR"):
        d = getattr(web, tier) / "init_2026"
        d.mkdir(parents=True, exist_ok=True)
        (d / (still + ".jpg")).write_bytes(b"x")
    assert real / still in derive.pending_files()


def test_the_index_carries_a_clips_hashes(
    client: TestClient, video: Path, app_env: dict[str, Path]
) -> None:
    """What `process` notes of a clip's own files reaches its row — which is
    what an import looks a returning copy up by — and the note is not taken
    for a file of its own."""
    [clip] = _make(client, (0, 10))
    note = app_env["share"] / "meta" / "init_2026" / f"{clip}.json"
    note.write_text(json.dumps({"file": clip, "folder": "init_2026",
                                "clip": True, "content_hash": "m:cut",
                                "render_hash": "m:play"}), encoding="utf-8")
    _rebuild(app_env)
    row = _have(app_env, clip)
    assert (row["content_hash"], row["render_hash"]) == ("m:cut", "m:play")
    assert row["clip_of"] == "b.mp4", "the note became a row of its own"
