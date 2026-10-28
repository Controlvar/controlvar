"""Voice PR C2 (#1066 voiceover): a narrated capture, paced by its clips.

The capture reads the voice step's cache (read only) for each ``say:``
sentence's clip, holds every beat for its sentences' slots (the TILE rule),
lets the narration run over the beat's exit but never past it (G20), checks
each spoken number on the page (G2), and writes ``voice.json`` with the clips
copied beside it. No browser, Docker, network, AWS or ffmpeg: the director is
driven through video 1's real storyboard on a stand-in page and clock, and the
cache is written here with the standard library.
"""

from __future__ import annotations

import json
import os
import sys
import types
import wave
from pathlib import Path
from typing import Dict, List, Optional

import pytest
from showcase import contract
from showcase.capture import build, config, director, guard, run, storyboard, voiced
from showcase.capture.procs import Children
from showcase.capture.stack import Ports

from backend.tests.unit.showcase.capture_dirs import default_manifest, jpeg_bytes

pytestmark = [pytest.mark.unit]

SHOWCASE = Path(__file__).resolve().parents[4] / "tests" / "acceptance" / "showcase"
V1 = "01-getting-started"


def board(slug: str = V1) -> storyboard.Storyboard:
    return storyboard.load(run.board_path(slug))


# --- A voice cache, written as the voice step writes it -------------------------------


def tone(samples: int) -> bytes:
    """A clip's samples: a little silence, then a steady tone to the end."""
    lead = min(2400, samples // 4)
    return bytes(lead * 2) + (4000).to_bytes(2, "little", signed=True) * (
        samples - lead
    )


def write_entry(root: Path, text: str, samples: int) -> Path:
    """One cache entry for *text*, as ``voice.cache`` files it."""
    request = contract.polly_text(text)
    key = contract.clip_key("Ruth", request)
    entry = root / key
    entry.mkdir(parents=True)
    (entry / contract.CACHE_SOURCE).write_bytes(b"MP3 " + request.encode())
    with wave.open(str(entry / contract.CACHE_CLIP), "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(contract.VOICE_RATE)
        clip.writeframes(tone(samples))
    record = {
        "key": key,
        **contract.clip_key_fields("Ruth", request),
        "request_characters": len(request),
        "source_sha256": contract.file_sha256(entry / contract.CACHE_SOURCE),
        "raw_samples": int(samples * 0.9),
        "clip_sha256": contract.file_sha256(entry / contract.CACHE_CLIP),
        "samples": samples,
    }
    assert set(record) == contract.CACHE_ENTRY_KEYS
    (entry / contract.CACHE_ENTRY).write_text(json.dumps(record), encoding="utf-8")
    return entry


def seconds_for(text: str) -> float:
    """Ruth at 90% says about 13 characters a second (measured 8.6-17.4 raw)."""
    return 0.4 + len(text) / 13.0


@pytest.fixture
def cache(tmp_path) -> Path:
    """Video 1's clips, one a sentence, in a cache root of its own."""
    root = tmp_path / "voice-cache"
    root.mkdir(mode=0o700)
    for _scene, _beat, _line, text in storyboard.narration_lines(board()):
        write_entry(root, text, round(seconds_for(text) * contract.VOICE_RATE))
    return root


def listing(root: Path) -> Dict[str, float]:
    return {
        str(p.relative_to(root)): p.stat().st_mtime_ns for p in sorted(root.rglob("*"))
    }


# --- The clips: found, read only, or refused with the command that makes them ---------


def test_every_sentence_has_its_clip_in_order_and_the_cache_is_not_touched(cache):
    before = listing(cache)
    clips = voiced.find_clips(board(), "Ruth", cache)
    lines = storyboard.narration_lines(board())
    assert [(c.scene, c.beat, c.line, c.text) for c in clips] == lines
    assert all(c.path.parent.parent == cache for c in clips)
    assert listing(cache) == before


@pytest.mark.regression
def test_a_missing_clip_is_refused_with_the_command_that_makes_it(cache):
    clips = voiced.find_clips(board(), "Ruth", cache)
    for path in clips[4].path.parent.iterdir():
        path.unlink()
    clips[4].path.parent.rmdir()
    with pytest.raises(
        voiced.VoiceRefused,
        match=r"01-getting-started: 1 of 16 sentences have no Ruth clip .*first:"
        r" scene dashboard beat 2 line 1.*make showcase-voice VIDEO=01-getting-started",
    ):
        voiced.find_clips(board(), "Ruth", cache)


def test_no_cache_at_all_is_refused_with_the_command(tmp_path):
    with pytest.raises(
        voiced.VoiceRefused,
        match="no voice cache at .* make showcase-voice VIDEO=01-getting-started",
    ):
        voiced.find_clips(board(), "Ruth", tmp_path / "nowhere")
    assert not (tmp_path / "nowhere").exists()


def test_a_damaged_entry_is_refused_as_the_voice_step_refuses_it(cache):
    clips = voiced.find_clips(board(), "Ruth", cache)
    with wave.open(str(clips[2].path), "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(contract.VOICE_RATE)
        clip.writeframes(tone(4800))
    with pytest.raises(voiced.VoiceRefused, match="clip.wav is not the clip made"):
        voiced.find_clips(board(), "Ruth", cache)


def test_a_cache_inside_a_git_checkout_is_refused(cache, tmp_path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(voiced.VoiceRefused, match="inside the git checkout"):
        voiced.find_clips(board(), "Ruth", cache)


def test_a_silent_capture_or_a_storyboard_without_narration_needs_no_clip(tmp_path):
    assert voiced.find_clips(board(), contract.SILENT, tmp_path / "nowhere") == []
    probe = board("90-app-probe")
    assert voiced.voice_for(probe, "Ruth") == contract.SILENT
    assert voiced.voice_for(board(), "Ruth") == "Ruth"
    assert voiced.voice_for(board(), contract.SILENT) == contract.SILENT
    with pytest.raises(voiced.VoiceRefused, match="'Joanna' is not one of"):
        voiced.voice_for(board(), "Joanna")


def options(tmp_path, **changes) -> run.Options:
    made = run.Options(
        videos=[V1],
        ref="v0.34.2",
        repo=tmp_path,
        work_dir=tmp_path / "work",
        profile="core",
        ports=Ports(28400, 28401, 28402),
        out=tmp_path / "videos",
        voice="Ruth",
        voice_cache=tmp_path / "voice-cache",
    )
    for key, value in changes.items():
        setattr(made, key, value)
    return made


def test_the_preflight_finds_the_clips_and_refuses_a_video_over_the_cap(
    cache, tmp_path, monkeypatch
):
    voice, clips = run.narration_clips(board(), options(tmp_path))
    assert voice == "Ruth" and len(clips) == 16
    monkeypatch.setattr(contract, "MAX_VIDEO_MS", 60_000)
    with pytest.raises(voiced.VoiceRefused, match="cut narration by the UX cut list"):
        run.narration_clips(board(), options(tmp_path))


def test_the_preflight_runs_the_clip_check_before_any_stack(tmp_path, monkeypatch):
    """A missing clip stops the run in the preflight, before Docker, npm or uvicorn."""
    monkeypatch.setattr(run, "refuse_missing_ffmpeg", lambda: None)
    monkeypatch.setattr(guard, "inside_git_checkout", lambda path: None)
    import importlib.metadata

    monkeypatch.setattr(
        importlib.metadata, "version", lambda name: config.PLAYWRIGHT_VERSION
    )
    with pytest.raises(guard.Refused, match="make showcase-voice VIDEO=01-getting"):
        run.preflight(options(tmp_path, render=False), {"HOME": str(tmp_path / "h")})


@pytest.mark.regression
@pytest.mark.parametrize("inside", ["work", "videos"])
def test_a_voice_cache_inside_the_work_root_or_the_output_is_refused(tmp_path, inside):
    """The work root and the output are where the tool writes and deletes."""
    made = options(tmp_path, voice_cache=tmp_path / inside / "voice-cache")
    flag = "--work-dir" if inside == "work" else "--out"
    with pytest.raises(guard.Refused, match=f"--voice-cache .* is inside {flag}"):
        run.refuse_voice_cache_inside(made)
    run.refuse_voice_cache_inside(options(tmp_path))
    # A silent capture reads no cache, wherever it is.
    run.refuse_voice_cache_inside(
        options(tmp_path, voice="none", voice_cache=tmp_path / inside / "c")
    )


def test_the_preflight_refuses_a_voice_cache_inside_the_work_root(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(guard, "inside_git_checkout", lambda path: None)
    with pytest.raises(guard.Refused, match="is inside --work-dir"):
        run.preflight(
            options(tmp_path, render=False, voice_cache=tmp_path / "work" / "c"),
            {"HOME": str(tmp_path / "h")},
        )


def test_the_voice_is_ruth_unless_the_caller_says_none(tmp_path):
    environ = {"HOME": str(tmp_path), "TMPDIR": str(tmp_path)}
    made = run.parse_args([V1, "--ref", "v0.34.2"], environ)
    assert made.voice == "Ruth"
    assert made.voice_cache == tmp_path.joinpath(*contract.VOICE_CACHE_HOME)
    silent = run.parse_args([V1, "--ref", "v0.34.2"], {**environ, "VOICE": "none"})
    assert silent.voice == contract.SILENT
    given = run.parse_args(
        [V1, "--ref", "v0.34.2", "--voice-cache", str(tmp_path / "c")], environ
    )
    assert given.voice_cache == tmp_path / "c"
    with pytest.raises(guard.Refused, match="--voice 'Ruthless' is one of"):
        run.parse_args([V1, "--ref", "v0.34.2", "--voice", "Ruthless"], environ)


# --- G20: the exit floor is what the director's step code waits -----------------------


class Clock:
    def __init__(self):
        self.ms = 1_000_000.0

    def now(self):
        return self.ms


class Overlay:
    def __exit__(self, *args):
        pass


class Screencast:
    def start(self, **kwargs):
        pass

    def stop(self):
        pass

    def show_overlay(self, html):
        return Overlay()


class Mouse:
    def move(self, x, y):
        pass

    def click(self, x, y):
        pass


class Element:
    """Any element: on the page, on top, holding *text*; as a field it starts
    empty and holds what is typed into it (gate 12 reads it back)."""

    def __init__(self, page):
        self.page = page
        self.value = ""

    def nth(self, index):
        return self

    def wait_for(self, state, timeout):
        pass

    def bounding_box(self):
        return {"x": 400.0, "y": 300.0, "width": 300.0, "height": 40.0}

    def evaluate(self, script, *args):
        if script == "el => el.tagName":
            return "A"
        if script == director.ON_TOP_JS:
            return True
        if script == "el => String(el.type || '').toLowerCase()":
            return "password"
        raise AssertionError(f"an unexpected element script: {script[:40]}")

    def inner_text(self):
        return self.page.drawn

    def press_sequentially(self, text, delay):
        self.value += text

    def fill(self, text):
        self.value = text

    def input_value(self):
        return self.value


DRAWN_JS = "the stand-in's page text and fields"


class StandIn:
    """Enough of a page for a whole storyboard: every element found at once, a
    clock that moves only when the director waits, and *drawn* as its text."""

    def __init__(self, clock: Clock, drawn: str):
        self.clock = clock
        self.drawn = drawn
        self.url = "http://127.0.0.1:28401/"
        self.mouse = Mouse()
        self.screencast = Screencast()
        self.waited: List[int] = []

    def wait_for_timeout(self, ms):
        self.waited.append(ms)
        self.clock.ms += ms

    def _element(self, *args, **kwargs):
        return Element(self)

    get_by_role = get_by_text = get_by_label = get_by_test_id = locator = _element

    def wait_for_url(self, url, timeout):
        pass

    def wait_for_load_state(self, state, timeout):
        pass

    def goto(self, url):
        pass

    def set_content(self, html):
        pass

    def evaluate(self, script, *args):
        if script == DRAWN_JS:
            return self.drawn, []
        if script == director.HEADER_JS:
            return {"links": 3, "wrapped": [], "cut": []}
        if script == director.VIEWPORT_TEXT_JS:
            return self.drawn
        if script == director.BROKEN_JS:
            return {"overlay": False, "alerts": []}
        raise AssertionError(f"an unexpected page script: {script[:40]}")


#: Every number video 1 says, as its pages print them.
SHOWN = (
    "Experiments Homepage Hero Copy Test 3 experiments, 2 flags +15.0% improvement"
    " on Signup Conversion Winner Expand to 50% 10% 25% Error rate"
)


@pytest.fixture
def stage(tmp_path, monkeypatch):
    """A director on a stand-in page and clock, and a function to make one."""
    clock = Clock()
    monkeypatch.setattr(director, "_now_ms", clock.now)
    stand_in = types.ModuleType("docs_runner.execute")
    stand_in.DRAWN_JS = DRAWN_JS
    monkeypatch.setitem(sys.modules, "docs_runner.execute", stand_in)

    def make(pacing: Optional[voiced.Pacing] = None, drawn: str = SHOWN):
        page = StandIn(clock, drawn)
        made = director.Director(
            context=object(),
            page=page,
            frames=tmp_path / "work" / V1 / "capture" / "frames",
            dashboard="http://127.0.0.1:28401",
            api_port=28400,
            dashboard_port=28401,
            redactor=director.redaction.Redactor(),
            forbidden=(),
            work=guard.WorkRoot(tmp_path / "work"),
            pacing=pacing,
        )
        made.where = "scene dashboard beat 1"
        return made, page

    return make


def steps_of_every_kind() -> List[object]:
    target = storyboard.Locator(role="link", name="Sign in", exact=True)
    return [
        storyboard.Click(click=target),
        storyboard.Move(move=target),
        storyboard.Look(look=target, ms=1800),
        storyboard.WaitFor(wait_for=target),
        storyboard.Type(type=target, text="Demo1234!", secret=True),
    ]


@pytest.mark.parametrize("step", steps_of_every_kind(), ids=lambda s: type(s).__name__)
def test_the_exit_floor_is_what_the_step_code_waits(stage, step):
    """Driven through ``Director.step`` on the stand-in clock: the step takes at
    least its floor, and at most one cursor tick (15 ms) a glide more."""
    made, page = stage()
    made.recorder.t0 = page.clock.ms
    began = page.clock.ms
    made.step(step)
    took = page.clock.ms - began
    floor = voiced.exit_floor_ms([step])
    glides = 0 if isinstance(step, (storyboard.Look, storyboard.WaitFor)) else 1
    assert floor <= took <= floor + 15 * glides, (floor, took)


@pytest.mark.regression
def test_a_click_exits_floor_is_pinned():
    """A floor typed apart from the step code would drift from it: a change to
    the click's pace must be seen here first (POINTER_REST_MS alone fails it)."""
    click = storyboard.Click(click=storyboard.Locator(css="a"))
    assert voiced.exit_floor_ms([click]) == 1100
    assert voiced.exit_floor_ms([click, click]) == 2200
    assert voiced.exit_floor_ms([storyboard.ScrollTo(scroll_to=click.click)]) == 0


# --- Pacing a beat ----------------------------------------------------------------------


def beat_of(**fields) -> storyboard.Beat:
    return storyboard.Beat(
        cue="Open the dashboard in your browser.",
        focus=storyboard.Locator(role="link", name="Sign in", exact=True),
        **fields,
    )


def test_a_silent_beat_is_held_as_before():
    beat = beat_of()
    assert voiced.Pacing.hold_ms(beat, None) == storyboard.min_cue_ms(beat.cue)


def test_a_narrated_beat_holds_its_slots_less_its_exits_floor():
    voice = voiced.BeatVoice(frames=150, sentences=("One.",))  # 5 s
    click = storyboard.Click(click=storyboard.Locator(css="a"))
    assert voiced.Pacing.hold_ms(beat_of(), voice) == 5100
    assert voiced.Pacing.hold_ms(beat_of(exit=[click]), voice) == 4000
    # A payoff's hold is silent: its narration ends before it, exit or not.
    assert voiced.Pacing.hold_ms(beat_of(exit=[click], payoff=True), voice) == 5100
    # Never under the headline's own reading time.
    short = voiced.BeatVoice(frames=3, sentences=("One.",))
    assert voiced.Pacing.hold_ms(beat_of(), short) == storyboard.min_cue_ms(
        beat_of().cue
    )


# --- The whole of video 1, paced, written and read back ---------------------------------


def record_v1(make, clips) -> director.Director:
    made, _page = make(voiced.Pacing(clips))
    for scene in board().scenes:
        made.scene(scene)
    return made


def capture_from(made, tmp_path, clips, *, voice="Ruth") -> Path:
    """The directory ``run.write_capture`` makes from the director's recording."""
    capture = tmp_path / "work" / V1 / "capture"
    cues = made.cues
    duration = made.recorder.now()
    (capture / "frames").mkdir(parents=True, exist_ok=True)
    gates = default_manifest()["gates"]
    if made.pacing.narrated:
        for name, (ok, data, _why) in run.voiced_checks(made, board()).items():
            gates[name] = {"ok": ok, "numbers": data}
    work = guard.WorkRoot(tmp_path / "work")
    entries = [
        {"file": f"{n:06d}.jpg", "t_ms": t}
        for n, t in enumerate(range(0, duration, 2000), start=1)
    ]
    for entry in entries:
        (capture / "frames" / entry["file"]).write_bytes(
            jpeg_bytes(contract.PAGE_W, contract.PAGE_H)
        )
    run.write_capture(
        capture,
        board=board(),
        source=build.Source(ref="v0.34.2", commit="0" * 40, version="0.34.2"),
        options=options(tmp_path),
        versions={"next_used": "16.3.5", "next_lockfile": "16.3.5"},
        facts={
            "entries": entries,
            "duration_ms": duration,
            "cues": cues,
            "needles": ["VALUE-ONE-TWO-THREE"],
            "zoom": 4 / 3,
            "voice": voice,
            "clips": clips,
        },
        gates={"12": {"ok": True, "numbers": {}}, **gates},
        children=Children(work, {}),
        wall_seconds=1.0,
        work=work,
    )
    return capture


def test_video_1_paced_by_its_clips_validates_with_every_sentence_tiled(
    stage, cache, tmp_path
):
    # The real video 1, as E0's D84 preflight passes it: every departure stated.
    assert storyboard.departure_problems(board()) == []
    clips = voiced.find_clips(board(), "Ruth", cache)
    made = record_v1(stage, clips)
    assert made.tally.narration_checks == 16
    assert len(made.tally.voiced_beats) == len(made.cues) == 11
    capture = capture_from(made, tmp_path, clips)
    read = contract.validate_capture_dir(capture)
    assert read.manifest.voice == "Ruth"
    assert [s.text for s in read.narration.sentences] == [c.text for c in clips]
    assert sorted(p.name for p in (capture / "voice").iterdir()) == [
        f"{n:03d}.wav" for n in range(1, 17)
    ]
    for sentence, clip in zip(read.narration.sentences, clips):
        assert (
            capture / "voice" / sentence.file
        ).read_bytes() == clip.path.read_bytes()
    manifest = json.loads((capture / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["gates"]["G20"]["numbers"]["tile"]["sentences"] == 16
    assert manifest["gates"]["G2"]["numbers"]["narration_checks"] == 16
    # Each narrated beat ran at least as long as its sentences' slots.
    for beat in made.tally.voiced_beats:
        assert beat["held_ms"] >= beat["slots_ms"] - 17, beat


@pytest.mark.regression
def test_a_beat_shortened_under_its_clip_is_refused_after_its_exit(
    stage, cache, monkeypatch
):
    """The tamper: hold a narrated beat for its headline only. The exit is
    filmed, then the capture is refused; it never waits on after the exit."""
    clips = voiced.find_clips(board(), "Ruth", cache)
    monkeypatch.setattr(
        voiced.Pacing,
        "hold_ms",
        staticmethod(lambda beat, voice: storyboard.min_cue_ms(beat.cue)),
    )
    with pytest.raises(
        director.CaptureFailed,
        match=r"G20: beat 1 of stack-card: the narration outlasts the beat's exit by"
        r" [0-9]+ ms; lengthen the beat's hold or shorten the narration",
    ):
        record_v1(stage, clips)


@pytest.mark.regression
def test_a_clip_longer_than_the_slot_its_beat_was_held_for_is_refused(
    stage, cache, tmp_path
):
    """The tamper: the beat is paced by a clip 2 s shorter than the one copied."""
    clips = voiced.find_clips(board(), "Ruth", cache)
    paced = list(clips)
    paced[1] = voiced.Clip(
        **{**clips[1].__dict__, "samples": clips[1].samples - 96_000}
    )
    made = record_v1(stage, paced)
    with pytest.raises(
        voiced.VoiceRefused,
        match=r"cue 2 \(scene stack-card beat 2 line 1\) is [0-9]+ frames; its 2"
        r" sentences' slots need [0-9]+: a clip is longer than the slot",
    ):
        capture_from(made, tmp_path, clips)


def test_a_clip_that_changed_after_the_beat_was_paced_is_refused(
    stage, cache, tmp_path
):
    clips = voiced.find_clips(board(), "Ruth", cache)
    made = record_v1(stage, clips)
    with wave.open(str(clips[5].path), "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(contract.VOICE_RATE)
        clip.writeframes(tone(clips[5].samples)[:-2] + b"\x01\x00")
    with pytest.raises(voiced.VoiceRefused, match="not the one the beat was paced"):
        capture_from(made, tmp_path, clips)


@pytest.mark.regression
def test_a_spoken_number_the_page_does_not_show_is_refused(stage, cache):
    """G2 at capture: video 1 says 25%; a page without it refuses at that beat."""
    clips = voiced.find_clips(board(), "Ruth", cache)
    made, _page = stage(voiced.Pacing(clips), drawn=SHOWN.replace(" 25%", ""))
    with pytest.raises(director.CaptureFailed, match="G2: .*the narration says 25%"):
        for scene in board().scenes:
            made.scene(scene)
    assert made.tally.spoken and made.tally.narration_checks <= 16


def test_the_numbers_are_checked_once_a_sentence_exactly(stage, cache, tmp_path):
    clips = voiced.find_clips(board(), "Ruth", cache)
    made = record_v1(stage, clips)
    made.tally.narration_checks -= 1
    ok, numbers, why = run.voiced_checks(made, board())["G2"]
    assert not ok and why == "15 narration number checks for 16 sentences"


def test_a_narrated_manifest_without_g2_or_g20_is_refused(stage, cache, tmp_path):
    clips = voiced.find_clips(board(), "Ruth", cache)
    capture = capture_from(record_v1(stage, clips), tmp_path, clips)
    path = capture / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["gates"]["G20"]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(
        contract.CaptureRefused, match="capture gate G20 is not in the manifest"
    ):
        contract.validate_capture_dir(capture)


# --- EM C5: VOICE=none on a narrated storyboard is today's silent capture ----------------


@pytest.mark.regression
def test_voice_none_on_a_narrated_storyboard_writes_no_narration(stage, tmp_path):
    """The silent fallback: no clips looked up, the beats held as before, no
    voice.json, no voice/, and the manifest says none."""
    voice, clips = run.narration_clips(board(), options(tmp_path, voice="none"))
    assert (voice, clips) == (contract.SILENT, [])
    made = record_v1(stage, clips)
    assert made.tally.narration_checks == 0 and made.tally.voiced_beats == []
    # Each cue held for its headline as a silent capture holds it, no longer.
    assert made.tally.holds_ms == silent_holds(stage)
    capture = capture_from(made, tmp_path, [], voice=contract.SILENT)
    assert not (capture / "voice.json").exists() and not (capture / "voice").exists()
    read = contract.validate_capture_dir(capture)
    assert read.manifest.voice == contract.SILENT and read.narration is None


def silent_holds(make) -> List[int]:
    """Video 1's holds with no pacing at all: the director as it was before C2."""
    made, _page = make(None)
    for scene in board().scenes:
        made.scene(scene)
    return made.tally.holds_ms


def test_the_capture_never_writes_the_voice_cache(cache, stage, tmp_path):
    before = listing(cache)
    clips = voiced.find_clips(board(), "Ruth", cache)
    capture_from(record_v1(stage, clips), tmp_path, clips)
    assert listing(cache) == before
    assert os.stat(cache).st_mode & 0o777 == 0o700
