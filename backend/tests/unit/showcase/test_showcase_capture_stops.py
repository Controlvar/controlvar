"""The showcase capture's stops, deletions and measurements (#1066, review of #1079).

- A Ctrl-C (SIGINT to the process group), like SIGTERM and SIGHUP, runs the
  teardown from the signal handler and exits with 128 + the signal.
- A gate-6 refusal deletes the recording's frames, and every deletion goes
  through ``WorkRoot.remove``.
- The zoom is measured on the page, refused when it is not ``config.ZOOM`` on
  <body>, and the measured value is what the manifest records.
- The needle check reads the kept values after the page, not before.
- Every program the capture starts, the browser and ``ffmpeg -version``
  included, is given an environment (read from the source).

``director`` is imported without Playwright here (the CI unit job has none):
the tests drive it with stand-in page objects.
"""

from __future__ import annotations

import ast
import json
import os
import signal
import subprocess
import sys
import types
from pathlib import Path

import pytest
from showcase import contract
from showcase.capture import build, config, director, gates, guard, run, storyboard
from showcase.capture.procs import Children
from showcase.capture.stack import Ports

from backend.tests.unit.showcase.capture_dirs import (
    default_cues,
    default_manifest,
    jpeg_bytes,
)

pytestmark = [pytest.mark.unit]

ACCEPTANCE = Path(__file__).resolve().parents[4] / "tests" / "acceptance"
CAPTURE = ACCEPTANCE / "showcase" / "capture"


# --- Stopping signals -------------------------------------------------------------


def test_the_stop_signals_are_exactly_these():
    assert run.STOP_SIGNALS == (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


def test_main_installs_the_handler_for_every_stop_signal(monkeypatch):
    before = {s: signal.getsignal(s) for s in run.STOP_SIGNALS}

    def stop_here(argv, environ):
        raise guard.Refused("stop after the handlers are in")

    stripped = []
    # main cuts os.environ to the allow-list first; here that would cut this
    # pytest session's own (test_showcase_capture_guard.py runs main for real).
    monkeypatch.setattr(guard, "strip_environment", lambda environ: stripped.append(1))
    monkeypatch.setattr(run, "parse_args", stop_here)
    try:
        assert run.main([]) == 2
        assert stripped == [1]
        for signum in run.STOP_SIGNALS:
            assert signal.getsignal(signum) is run._stop_on_signal, signum
    finally:
        for signum, handler in before.items():
            signal.signal(signum, handler)


CHILD = """
import pathlib, sys, time
sys.path.insert(0, sys.argv[1])
from showcase.capture import run
marker = pathlib.Path(sys.argv[2])
run.TEARDOWN.append(lambda: marker.write_text("stack down", encoding="utf-8"))
run.install_stop_handlers()
print("ready", flush=True)
while True:
    time.sleep(0.05)
"""


@pytest.mark.regression
@pytest.mark.parametrize(
    "signum, status",
    [(signal.SIGINT, 130), (signal.SIGTERM, 143), (signal.SIGHUP, 129)],
)
def test_a_stop_signal_to_the_process_group_tears_down_and_exits(
    tmp_path, signum, status
):
    """Ctrl-C reached ``finally`` and spun inside Playwright, leaving the stack up.

    The signal goes to the whole process group, as a terminal sends it.
    """
    marker = tmp_path / "teardown"
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(ACCEPTANCE), str(marker)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env={**os.environ, "PYTHONPATH": str(ACCEPTANCE)},
    )
    try:
        assert child.stdout.readline().strip() == "ready", child.stderr.read()
        os.killpg(child.pid, signum)
        assert child.wait(timeout=30) == status, child.stderr.read()
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
    assert marker.read_text(encoding="utf-8") == "stack down"


# --- Deletions go through WorkRoot.remove ---------------------------------------------


class Page:
    """Enough of a Playwright page for the director's checks."""

    def __init__(self, answers=None):
        self.answers = answers or {}

    def evaluate(self, script, *args):
        answer = self.answers[script]
        return answer() if callable(answer) else answer


def make_director(tmp_path, page, work=None):
    work = work or guard.WorkRoot(tmp_path / "work")
    frames = work.path / "video" / "capture" / "frames"
    redactor = director.redaction.Redactor()
    made = director.Director(
        context=object(),
        page=page,
        frames=frames,
        dashboard="http://127.0.0.1:28401",
        api_port=28400,
        dashboard_port=28401,
        redactor=redactor,
        forbidden=(),
        work=work,
    )
    return made, redactor, work


def frame(recorder, t, data=b"jpeg"):
    recorder._n += 1
    path = recorder.dir / f"raw-{recorder._n:06d}.jpg"
    path.write_bytes(data)
    recorder.frames.append((t, path))
    return path


@pytest.mark.regression
def test_a_frame_replaced_at_the_same_millisecond_is_removed_through_the_work_root(
    tmp_path,
):
    made, _, work = make_director(tmp_path, Page())
    first = frame(made.recorder, 0, b"first")
    frame(made.recorder, 0, b"second")
    frame(made.recorder, 40, b"third")
    entries = made.recorder.finish()
    assert entries == [
        {"file": "000001.jpg", "t_ms": 0},
        {"file": "000002.jpg", "t_ms": 40},
    ]
    assert not first.exists()
    assert (made.recorder.dir / "000001.jpg").read_bytes() == b"second"


def test_the_recorder_deletes_nothing_outside_the_work_root(tmp_path):
    """A frames directory outside the work root: the replaced frame is refused, not deleted."""
    made, _, _ = make_director(tmp_path, Page())
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    made.recorder.dir = outside
    first = frame(made.recorder, 0)
    frame(made.recorder, 0)
    with pytest.raises(guard.Refused, match="not under"):
        made.recorder.finish()
    assert first.exists()


@pytest.mark.regression
def test_a_gate_6_refusal_drops_the_recording(tmp_path):
    work = guard.WorkRoot(tmp_path / "work")
    frames = work.path / "video" / "capture" / "frames"
    frames.mkdir(parents=True)
    (frames / "raw-000001.jpg").write_bytes(b"a kept value, drawn")
    run.drop_recording(
        director.CaptureFailed("gate 6", "a kept value is drawn; recording dropped"),
        frames,
        work,
    )
    assert not frames.exists()


def test_another_refusal_keeps_the_frames_to_look_at(tmp_path):
    work = guard.WorkRoot(tmp_path / "work")
    frames = work.path / "video" / "capture" / "frames"
    frames.mkdir(parents=True)
    run.drop_recording(
        director.CaptureFailed("gate 9", "focus off the page"), frames, work
    )
    assert frames.is_dir()


def test_dropping_a_recording_outside_the_work_root_is_refused(tmp_path):
    work = guard.WorkRoot(tmp_path / "work")
    frames = tmp_path / "elsewhere" / "frames"
    frames.mkdir(parents=True)
    with pytest.raises(guard.Refused, match="not under"):
        run.drop_recording(director.CaptureFailed("gate 6", "drawn"), frames, work)
    assert frames.is_dir()


class FakeStack:
    def __init__(self, **kwargs):
        self.api_url = "http://127.0.0.1:28400"
        self.dashboard_url = "http://127.0.0.1:28401"
        self.docker_env = {}
        self.facts = {
            "docker_port": ["28402"],
            "served_profile": "core",
            "api_launcher": "fake",
        }

    def up(self):
        pass

    def down(self):
        pass


@pytest.mark.regression
def test_capture_video_drops_the_frames_when_gate_6_refuses(tmp_path, monkeypatch):
    """The refusal said "recording dropped" while the frames stayed on disk."""
    board = storyboard.load(CAPTURE.parent / config.VIDEOS["01-getting-started"])
    monkeypatch.setattr(run, "Stack", FakeStack)
    monkeypatch.setattr(run, "_api", lambda *a, **k: {"access_token": "x" * 40})
    monkeypatch.setattr(run, "seed_counts", lambda *a: board.seed_counts.model_dump())

    def record(board, stack, options, token, frames_dir, gates_, log, work, pacing):
        (frames_dir / "raw-000001.jpg").write_bytes(b"a kept value, drawn")
        raise director.CaptureFailed(
            "gate 6", "scene dashboard beat 3: a kept value is drawn; recording dropped"
        )

    monkeypatch.setattr(run, "record", record)
    work = guard.WorkRoot(tmp_path / "work")
    work.path.mkdir(parents=True)
    options = run.Options(
        videos=["01-getting-started"],
        ref="v0.0.0",
        repo=tmp_path,
        work_dir=work.path,
        profile="core",
        ports=Ports(28400, 28401, 28402),
        out=tmp_path / "videos",
        floor=0,
        # FakeStack stands in for the uvicorn stack's Stack.
        stack="uvicorn",
    )
    with pytest.raises(director.CaptureFailed, match="recording dropped"):
        run.capture_video(
            "01-getting-started",
            options=options,
            source=build.Source(ref="v0.0.0", commit="0" * 40, version="0.0.0"),
            tree=tmp_path / "tree",
            work=work,
            children=Children(work, {}),
            versions={},
            environ={},
            log=lambda line: None,
        )
    frames = work.path / "01-getting-started" / "capture" / "frames"
    assert frames.parent.is_dir() and not frames.exists()


# --- The zoom is measured -------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "measured, why",
    [
        ({"html": 1.0, "body": 1.0}, "<body> is zoomed 1, not 1.333333"),
        (
            {"html": 4 / 3, "body": 1.0},
            "<html> is zoomed 1.33333; the zoom belongs on <body>",
        ),
        ({"html": 1.0, "body": 1.25}, "<body> is zoomed 1.25"),
        ({"html": 1.0, "body": None}, "the page's zoom could not be read"),
    ],
)
def test_a_page_without_the_zoom_on_body_is_refused(measured, why):
    ok, numbers, text = gates.zoom(measured)
    assert not ok and why in text
    assert numbers["body"] == measured["body"]


def test_the_zoom_on_body_passes_and_is_reported():
    ok, numbers, _ = gates.zoom({"html": 1.0, "body": 1.3333333})
    assert ok and numbers == {"body": 1.3333333, "html": 1.0, "wanted_body": 1.333333}


def test_the_director_measures_the_zoom_on_the_page(tmp_path):
    made, _, _ = make_director(
        tmp_path, Page({director.ZOOM_JS: {"html": 1.0, "body": 1.0}})
    )
    with pytest.raises(director.CaptureFailed, match="C1 zoom: <body> is zoomed 1"):
        made.measure_zoom()
    made.page = Page({director.ZOOM_JS: {"html": 1.0, "body": 1.3333333}})
    assert made.measure_zoom()["body"] == 1.3333333


def test_the_recording_measures_the_zoom_before_the_overlay_self_test():
    """``run.record`` needs a browser, so its order is read from the source."""
    tree = ast.parse((CAPTURE / "run.py").read_text(encoding="utf-8"))
    record = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "record"
    )
    lines = {}
    for node in ast.walk(record):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            lines.setdefault(node.func.attr, node.lineno)
    assert "measure_zoom" in lines, "record never measures the zoom"
    assert lines["measure_zoom"] < lines["self_test_overlay"]
    facts = next(
        node
        for node in ast.walk(record)
        if isinstance(node, ast.Dict)
        and any(getattr(k, "value", None) == "needles" for k in node.keys)
    )
    assert "zoom" in [getattr(k, "value", None) for k in facts.keys]


@pytest.mark.regression
def test_the_manifest_records_the_measured_zoom(tmp_path):
    """It recorded ``config.ZOOM`` whatever the page had."""
    capture = tmp_path / "work" / "01-getting-started" / "capture"
    (capture / "frames").mkdir(parents=True)
    entries = []
    for number, t_ms in enumerate([0, 10_000, 20_000, 30_000, 40_000], start=1):
        name = f"{number:06d}.jpg"
        (capture / "frames" / name).write_bytes(
            jpeg_bytes(contract.PAGE_W, contract.PAGE_H)
        )
        entries.append({"file": name, "t_ms": t_ms})
    board = storyboard.load(CAPTURE.parent / config.VIDEOS["01-getting-started"])
    work = guard.WorkRoot(tmp_path / "work")
    gate_set = default_manifest()["gates"]
    run.write_capture(
        capture,
        board=board,
        source=build.Source(ref="v0.26.4", commit="0" * 40, version="0.26.4"),
        options=run.Options(
            videos=["01-getting-started"],
            ref="v0.26.4",
            repo=tmp_path,
            work_dir=work.path,
            profile="core",
            ports=Ports(28400, 28401, 28402),
            out=tmp_path / "videos",
        ),
        versions={"next_used": "16.3.5", "next_lockfile": "16.3.5"},
        facts={
            "entries": entries,
            "duration_ms": 50_000,
            "cues": default_cues(),
            "needles": ["VALUE-ONE-TWO-THREE"],
            "zoom": 1.25,
        },
        gates=gate_set,
        children=Children(work, {}),
        wall_seconds=1.0,
    )
    manifest = json.loads((capture / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["zoom"] == 1.25


# --- The needle check reads the values after the page --------------------------------------


@pytest.fixture
def page_text_script(monkeypatch):
    """docs_runner.execute (which holds the page-text script) imports Playwright;
    stand in for it, so these run in the CI unit job too."""
    stand_in = types.ModuleType("docs_runner.execute")
    stand_in.DRAWN_JS = "read the page's text and fields"
    monkeypatch.setitem(sys.modules, "docs_runner.execute", stand_in)
    return stand_in.DRAWN_JS


@pytest.mark.regression
def test_a_value_kept_while_the_page_is_read_is_looked_for_in_that_text(
    tmp_path, page_text_script
):
    """The page's answer came with a token the redactor kept during the read."""
    token = "made-up-session-value-" + "q" * 20

    def read_page():
        redactor.add(token)
        return [f"Signed in. Your session: {token}", []]

    made, redactor, _ = make_director(tmp_path, Page({page_text_script: read_page}))
    made.where = "scene dashboard beat 2"
    with pytest.raises(
        director.CaptureFailed, match="a kept value is drawn in the page's text"
    ):
        made.check_needles()


def test_a_page_without_a_kept_value_passes_the_needle_check(
    tmp_path, page_text_script
):
    made, _, _ = make_director(
        tmp_path, Page({page_text_script: ["Experiments: 3 listed", []]})
    )
    made.check_needles()
    assert made.tally.needle_checks == 1


# --- Every program gets an environment ----------------------------------------------------


def program_starts(name: str):
    """``x.run``, ``x.Popen`` and ``x.launch`` calls, and a bare ``run([argv...])``
    (``build``'s replaceable ``run=subprocess.run`` parameter)."""
    tree = ast.parse((CAPTURE / name).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in (
            "run",
            "Popen",
            "launch",
            "check_output",
            "call",
        ):
            yield node
        elif (
            isinstance(func, ast.Name)
            and func.id == "run"
            and node.args
            and isinstance(node.args[0], ast.List)
        ):
            yield node


@pytest.mark.parametrize("name", sorted(p.name for p in CAPTURE.glob("*.py")))
def test_every_program_the_capture_starts_is_given_an_environment(name):
    for call in program_starts(name):
        keywords = [k.arg for k in call.keywords]
        if None in keywords:  # ``**kw`` passed on to a call that requires env
            continue
        assert "env" in keywords, f"{name}:{call.lineno} starts a program without env="


def test_the_environment_scan_finds_the_calls_it_checks():
    found = sum(1 for p in CAPTURE.glob("*.py") for _ in program_starts(p.name))
    assert found >= 15, found


@pytest.mark.regression
def test_the_ffmpeg_check_gets_the_allow_list(monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "staging")
    monkeypatch.setenv("SHOWCASE_PARENT_SENTINEL", "must-not-pass")
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "ffmpeg version 8", "")

    run.refuse_missing_ffmpeg(run=fake_run)
    assert set(seen["env"]) <= set(guard.PASSED)
    assert "AWS_PROFILE" not in seen["env"]
    assert "SHOWCASE_PARENT_SENTINEL" not in seen["env"]


def test_a_missing_ffmpeg_is_refused():
    def missing(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    with pytest.raises(guard.Refused, match="ffmpeg is not installed"):
        run.refuse_missing_ffmpeg(run=missing)
