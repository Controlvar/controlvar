"""The showcase capture directory contract (#1066): ``showcase/contract.py``.

The capture and the render meet at one directory. These tests pin the shared
numbers and plant each defect ``validate_capture_dir`` exists to refuse, and
read the refusal it gives.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest
from showcase import contract
from showcase.capture import config

from backend.tests.unit.showcase.capture_dirs import (
    NEEDLES,
    default_cues,
    default_manifest,
    edit_json,
    jpeg_bytes,
    make_capture,
)

pytestmark = [pytest.mark.unit]


def refusal(root) -> str:
    with pytest.raises(contract.CaptureRefused) as caught:
        contract.validate_capture_dir(root)
    return str(caught.value)


# --- The shared numbers --------------------------------------------------------


def test_the_frame_is_1080p_with_the_band_under_the_page():
    assert (contract.OUT_W, contract.OUT_H, contract.FPS) == (1920, 1080, 30)
    assert contract.PAGE_W == contract.OUT_W
    assert contract.PAGE_H + contract.BAND_H == contract.OUT_H
    # yuv420p needs even dimensions for every part the encode joins
    assert contract.PAGE_H % 2 == 0 and contract.BAND_H % 2 == 0


def test_cards_are_whole_frames_and_the_length_bounds_are_the_plans():
    assert contract.TITLE_FRAMES * 1000 == contract.TITLE_CARD_MS * contract.FPS
    assert contract.END_FRAMES * 1000 == contract.END_CARD_MS * contract.FPS
    assert (contract.MIN_VIDEO_MS, contract.MAX_VIDEO_MS) == (55_000, 90_000)
    assert contract.DISK_FLOOR_BYTES == 4 * 1024**3


def test_frame_rounding_is_consistent_with_the_title_shift():
    for frames in range(3000):
        shifted = contract.ms_for_frames(contract.TITLE_FRAMES + frames)
        assert shifted == contract.TITLE_CARD_MS + contract.ms_for_frames(frames)
        assert contract.frames_for_ms(contract.ms_for_frames(frames)) == frames
    assert [contract.ms_for_frames(f) for f in range(4)] == [0, 33, 67, 100]


def test_reading_time_is_the_ux_rule():
    assert contract.reading_time_ms("Short.") == 2000
    # 9 words: 0.5 + 9/3 = 3.5 s; 51 characters / 15 = 3.4 s
    assert (
        contract.reading_time_ms("One command starts the whole stack on your machine.")
        == 3500
    )
    # 84 characters in 12 words: 84/15 = 5.6 s beats 0.5 + 12/3 = 4.5 s
    text = "Aaaaaa bbbbbb cccccc dddddd eeeeee ffffff gggggg hhhhhh iiiiii jjjjjj kkkkkk llllll"
    assert len(text) == 83 and contract.reading_time_ms(text) == 5534


def test_captions_wrap_into_at_most_two_lines_of_42():
    lines = contract.wrap_caption(
        "Results give the lift, the p-value and a recommended winner."
    )
    assert lines == ("Results give the lift, the p-value and a", "recommended winner.")
    assert all(len(line) <= contract.MAX_LINE_CHARS for line in lines)
    assert (
        contract.caption_problems(
            "Results give the lift, the p-value and a recommended winner."
        )
        == []
    )


@pytest.mark.parametrize(
    "text, expected",
    [
        ("", "is empty"),
        (
            "one two three four five six seven eight nine ten eleven twelve thirteen",
            "has 13 words; at most 12",
        ),
        (
            "Wordy captions about everything that happens here quickly become three lines long.",
            "needs 3 lines",
        ),
        ("Line one\nline two", "line break"),
        ("A --> B", "'-->'"),
        (" padded", "leading, trailing"),
        ("x" * 43, "word run of 43 characters"),
    ],
)
def test_caption_text_rules_refuse(text, expected):
    problems = contract.caption_problems(text)
    assert any(expected in p for p in problems), problems


# --- JPEG frame headers -----------------------------------------------------------


def test_jpeg_size_reads_the_frame_header(tmp_path):
    path = tmp_path / "f.jpg"
    path.write_bytes(jpeg_bytes(1920, 940))
    assert contract.jpeg_size(path) == (1920, 940)
    path.write_bytes(jpeg_bytes(1280, 720, progressive=True))
    assert contract.jpeg_size(path) == (1280, 720)


@pytest.mark.parametrize(
    "data, expected",
    [
        (b"\x89PNG\r\n", "not a JPEG"),
        (b"\xff\xd8\xff\xd9", "no frame header"),
        (b"\xff\xd8\xff\xe0\x00", "damaged segment length"),
        (b"\xff\xd8", "ends before"),
    ],
)
def test_jpeg_size_refuses_what_is_not_a_frame(tmp_path, data, expected):
    path = tmp_path / "f.jpg"
    path.write_bytes(data)
    with pytest.raises(ValueError, match=expected):
        contract.jpeg_size(path)


# --- A capture directory -----------------------------------------------------------


def test_a_good_capture_directory_validates(tmp_path):
    capture = contract.validate_capture_dir(make_capture(tmp_path / "capture"))
    assert capture.sheet.slug == "01-getting-started"
    assert len(capture.sheet.cues) == 10
    assert [f.t_ms for f in capture.frames] == [0, 10_000, 20_000, 30_000, 40_000]
    assert capture.needle_count == len(NEEDLES)
    assert capture.sheet.capture_frames == 1500
    assert contract.video_frames(capture.sheet.duration_ms) == 90 + 1500 + 120
    assert capture.sheet.cues[1].focus == contract.Focus(x=100, y=100, w=400, h=60)


def test_refusal_lists_every_problem_and_never_a_needle(tmp_path):
    root = make_capture(tmp_path / "capture", frame_size=(1280, 720))
    os.chmod(root / "needles.txt", 0o644)
    edit_json(root / "manifest.json", lambda m: m["gates"]["6"].update(ok=False))
    text = refusal(root)
    assert "raw frame 000001.jpg is 1280x720; the page area is 1920x940" in text
    assert "needles.txt has mode 0644; it must be 0600" in text
    assert "capture gate 6 is not ok" in text
    for needle in NEEDLES:
        assert needle not in text


def test_an_upscale_is_refused_before_any_encode(tmp_path):
    # Gate 3: a 1280x720 frame would pass every check on the final MP4 once scaled.
    root = make_capture(tmp_path / "capture")
    (root / "frames" / "000003.jpg").write_bytes(jpeg_bytes(1280, 720))
    assert "raw frame 000003.jpg is 1280x720" in refusal(root)


def test_frames_must_move_forward_and_exist(tmp_path):
    root = make_capture(tmp_path / "a", frame_times=[0, 5000, 5000, 9000])
    assert "t_ms 5000 is not after the previous frame's 5000" in refusal(root)
    root = make_capture(tmp_path / "b")
    (root / "frames" / "000002.jpg").unlink()
    assert "frame 000002.jpg is listed but missing" in refusal(root)
    root = make_capture(tmp_path / "c", frame_times=[400, 5000])
    assert "the first frame comes 400 ms after the recording starts" in refusal(root)
    root = make_capture(tmp_path / "d", frame_times=[0, 51_000])
    assert (
        "the last frame comes at 51000 ms, after the recording's end at 50000 ms"
        in refusal(root)
    )


def test_overlapping_cues_are_refused(tmp_path):
    cues = default_cues()
    cues[4]["start_ms"] = cues[3]["end_ms"] - 100
    assert "cues 4 and 5 overlap by 0.100 s" in refusal(
        make_capture(tmp_path / "c", cues=cues)
    )


def test_a_cue_too_short_to_read_is_refused(tmp_path):
    cues = default_cues()
    cues[0]["end_ms"] = cues[0]["start_ms"] + 2000
    text = refusal(make_capture(tmp_path / "c", cues=cues))
    assert "cue 1: 51 characters and 9 words in 2.000 s, needs 3.500 s" in text


def test_a_long_stretch_without_a_caption_is_refused(tmp_path):
    cues = default_cues()
    del cues[5]
    text = refusal(make_capture(tmp_path / "c", cues=cues))
    assert "5.167 s without a caption between cues 5 and 6" in text


def test_the_video_length_counts_the_cards(tmp_path):
    cues = default_cues()[:9]
    # 45.8 s of recording + 7 s of cards = 52.8 s: under 55 s
    text = refusal(make_capture(tmp_path / "c", cues=cues, duration_ms=45_800))
    assert "the video would be 52.800 s with its cards; a showcase is 55-90 s" in text


def test_needles_must_be_private_present_and_long_enough(tmp_path):
    root = make_capture(tmp_path / "a", needles=[])
    (root / "needles.txt").write_text("", encoding="utf-8")
    os.chmod(root / "needles.txt", 0o600)
    assert "needles.txt holds 0 needles: refusing to scan with no needles" in refusal(
        root
    )
    root = make_capture(tmp_path / "b", needles=["short", "LONG-ENOUGH-VALUE"])
    text = refusal(root)
    assert (
        "needles.txt line 1 is 5 characters without its case and separators; "
        "a needle has at least 8" in text
    )
    assert "short" not in text.split("refused:")[1].replace(
        "line 1 is 5 characters", ""
    )
    root = make_capture(
        tmp_path / "c", needles=["LONG-ENOUGH-VALUE", "", "OTHER-LONG-VALUE"]
    )
    assert "needles.txt line 2 is blank" in refusal(root)
    root = make_capture(tmp_path / "d")
    (root / "needles.txt").unlink()
    assert "needles.txt is missing" in refusal(root)


@pytest.mark.regression
@pytest.mark.parametrize(
    "needle, length",
    [
        ("--------", 0),  # passed, then divided by zero in the OCR scan
        ("_-_-_-_-_-_-", 0),
        ("a_b_c_d_e", 5),
        ("A B C D E F G", 7),
    ],
)
def test_a_needle_is_long_enough_once_normalised(tmp_path, needle, length):
    """The scan compares normalised text, so the minimum is counted there too."""
    assert len(needle) >= contract.MIN_NEEDLE_CHARS
    root = make_capture(tmp_path / "c", needles=["LONG-ENOUGH-VALUE", needle])
    text = refusal(root)
    assert (
        f"needles.txt line 2 is {length} characters without its case and separators; "
        f"a needle has at least {contract.MIN_NEEDLE_CHARS}" in text
    )
    assert needle not in text.split("refused:")[1]


def test_contract_normalise_is_the_one_the_scan_uses():
    from showcase.render import ocr

    assert ocr.normalise is contract.normalise
    assert contract.normalise("eptk_DEMO 0-1:2") == "eptkdemoolz"
    for needle, length in (("--------", 0), ("a_b_c_d_e", 5), ("A B C D E F G", 7)):
        assert len(contract.normalise(needle)) == length


def test_read_needles_returns_the_values_for_the_scan(tmp_path):
    root = make_capture(tmp_path / "c")
    assert contract.read_needles(root / "needles.txt") == NEEDLES


def test_the_manifest_must_match_the_shared_numbers(tmp_path):
    def change(m):
        m["band_h"] = 120
        m["page_h"] = 960
        m["viewport"]["device_scale_factor"] = 1.5
        m["profile"] = "enterprise"
        m["commit"] = "abc123"

    root = make_capture(tmp_path / "c")
    edit_json(root / "manifest.json", change)
    text = refusal(root)
    assert f"manifest band_h 120 is not {contract.BAND_H}" in text
    assert f"manifest page_h 960 is not {contract.PAGE_H}" in text
    assert "manifest viewport" in text and "'device_scale_factor': 1.5" in text
    assert "manifest profile 'enterprise' is not one of ['core', 'full']" in text
    assert "manifest commit 'abc123' is not a full commit hash" in text


def test_every_required_capture_gate_must_be_reported(tmp_path):
    manifest = default_manifest()
    del manifest["gates"]["C1"]
    manifest["gates"]["extra"] = {"ok": True, "numbers": {}, "note": "x"}
    text = refusal(make_capture(tmp_path / "c", manifest=manifest))
    assert "capture gate C1 is not in the manifest" in text
    assert "capture gate extra has keys missing [] and unexpected ['note']" in text


def test_a_gate_reported_ok_as_a_string_is_not_ok(tmp_path):
    root = make_capture(tmp_path / "c")
    edit_json(root / "manifest.json", lambda m: m["gates"]["9"].update(ok="true"))
    assert "capture gate 9 is not ok" in refusal(root)


def test_cue_sheet_shape_is_strict(tmp_path):
    root = make_capture(tmp_path / "a")
    edit_json(
        root / "cues.json",
        lambda c: c["cues"][0].update(focus={"x": 0, "y": 900, "w": 10, "h": 100}),
    )
    assert "cue 1: focus 0,900 10x100 is not inside the page area 1920x940" in refusal(
        root
    )
    root = make_capture(tmp_path / "b")
    edit_json(root / "cues.json", lambda c: c["cues"][0].update(colour="red"))
    assert "cue 1 has keys missing [] and unexpected ['colour']" in refusal(root)
    root = make_capture(tmp_path / "c")
    edit_json(
        root / "cues.json", lambda c: c.update(version="0.26.3", slug="Getting Started")
    )
    text = refusal(root)
    assert "version '0.26.3' is not vX.Y.Z" in text and "slug 'Getting Started'" in text


def test_output_names_carry_the_short_sha256():
    names = contract.output_names("01-getting-started", "ab" * 32)
    assert names == {
        "mp4": "01-getting-started-abababababab.mp4",
        "vtt": "01-getting-started-abababababab.vtt",
        "srt": "01-getting-started-abababababab.srt",
        "transcript": "01-getting-started-abababababab.txt",
        "review": "01-getting-started-abababababab-review",
    }


def test_the_contract_module_needs_only_the_standard_library():
    source = (
        contract.__file__ and open(contract.__file__, encoding="utf-8").read()
    ) or ""
    imported = {
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.startswith(("import ", "from "))
        and not line.startswith("from __future__")
    }
    assert imported <= {
        "fractions",
        "hashlib",
        "json",
        "math",
        "re",
        "stat",
        "dataclasses",
        "pathlib",
        "wave",
        "typing",
    }, imported


def test_cues_json_is_what_the_fixture_writes(tmp_path):
    # The fixture is the contract's worked example; keep it a valid one.
    root = make_capture(tmp_path / "c")
    data = json.loads((root / "cues.json").read_text(encoding="utf-8"))
    assert set(data) == contract.CUES_KEYS
    assert all(set(cue) == contract.CUE_KEYS for cue in data["cues"])


# --- A video's own length cap ----------------------------------------------------

SHOWCASE = Path(__file__).resolve().parents[4] / "tests" / "acceptance" / "showcase"
V9 = "09-design-in-depth"


def test_the_length_cap_table_is_exact():
    """Video 9 alone may run longer, to 150 s, and nothing above that: a new
    entry or a longer cap is a plan change, not an edit."""
    assert contract.MAX_VIDEO_MS_BY_VIDEO == {V9: 150_000}
    assert contract.MAX_VIDEO_MS == 90_000


def test_every_video_given_its_own_cap_is_a_registered_video():
    """A misspelt slug would leave its video on the default cap without a word."""
    assert set(contract.MAX_VIDEO_MS_BY_VIDEO) <= set(config.VIDEOS)


@pytest.mark.parametrize("slug", list(config.VIDEOS))
def test_each_registered_video_has_its_cap(slug):
    """Every video but video 9 keeps 90 s."""
    expected = 150_000 if slug == V9 else 90_000
    assert contract.max_video_ms(slug) == expected


@pytest.mark.parametrize("slug", ["01-synthetic", "90-app-probe", "v", ""])
def test_a_slug_the_table_does_not_name_gets_the_default_and_never_raises(slug):
    """A probe, a synthetic capture or a test's own slug is held to 90 s."""
    assert contract.max_video_ms(slug) == 90_000


def test_the_cap_is_read_at_each_call(monkeypatch):
    """Not bound when the module loads: a changed default or table applies."""
    monkeypatch.setattr(contract, "MAX_VIDEO_MS", 60_000)
    assert contract.max_video_ms("01-getting-started") == 60_000
    assert contract.max_video_ms(V9) == 150_000
    monkeypatch.setitem(contract.MAX_VIDEO_MS_BY_VIDEO, "01-getting-started", 70_000)
    assert contract.max_video_ms("01-getting-started") == 70_000


def test_video_9s_projection_cap_is_145_5_s():
    share = contract.PROJECTION_SHARE
    assert round(contract.max_video_ms(V9) * share) == 145_500
    assert round(contract.max_video_ms("01-getting-started") * share) == 87_300


def end_to_end_cues(recording_ms: int):
    """Short captions end to end, 4 s each, over *recording_ms* of recording."""
    return [
        contract.Cue(
            start_ms=at, end_ms=at + 4000, text="Open the experiment.", focus=None
        )
        for at in range(0, recording_ms, 4000)
    ]


@pytest.mark.parametrize(
    "slug, recording_ms, refused",
    [
        (V9, 124_000, None),
        (
            "01-getting-started",
            124_000,
            "the video would be 131.000 s with its cards; a showcase is 55-90 s",
        ),
        (
            V9,
            144_000,
            "the video would be 151.000 s with its cards; a showcase is 55-150 s",
        ),
        ("01-getting-started", 80_000, None),
    ],
)
def test_the_cue_sheet_holds_each_video_to_its_own_cap(slug, recording_ms, refused):
    problems = contract.cue_timing_problems(
        end_to_end_cues(recording_ms), recording_ms, slug
    )
    assert problems == ([] if refused is None else [refused])


def test_a_capture_directory_is_held_to_its_videos_cap(tmp_path):
    """The render's reading of cues.json passes the slug on: the same sheet
    passes as video 9 and is refused as video 1."""
    cues = [
        {"start_ms": c.start_ms, "end_ms": c.end_ms, "text": c.text, "focus": None}
        for c in end_to_end_cues(124_000)
    ]
    root = make_capture(tmp_path / "v1", cues=cues, duration_ms=124_000)
    assert "a showcase is 55-90 s" in refusal(root)
    root = make_capture(tmp_path / "v9", cues=cues, duration_ms=124_000)
    edit_json(root / "cues.json", lambda sheet: sheet.update(slug=V9))
    problems = []
    contract._read_cues(root, problems)
    assert problems == []


def test_only_max_video_ms_reads_the_caps_and_every_reader_asks_it():
    """The partial wiring this guards against: the voice step projecting video
    9 against 150 s and gate 1 refusing it at 90 s, after the sitting and the
    take. Read from the source, as the gain's wiring is."""
    names = ("MAX_VIDEO_MS", "MAX_VIDEO_MS_BY_VIDEO")
    lookups, reads = [], []
    for path in sorted(SHOWCASE.rglob("*.py")):
        where = path.relative_to(SHOWCASE).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        inside = {
            id(node)
            for function in ast.walk(tree)
            if isinstance(function, ast.FunctionDef) and function.name == "max_video_ms"
            for node in ast.walk(function)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func).endswith(
                "max_video_ms"
            ):
                lookups.append((where, ast.unparse(node)))
            if isinstance(node, ast.alias) and node.name in names:
                reads.append((where, f"imports {node.name}"))
            loaded = isinstance(getattr(node, "ctx", None), ast.Load)
            name = getattr(node, "attr", None) or getattr(node, "id", None)
            if loaded and name in names and id(node) not in inside:
                reads.append((where, node.lineno))
    assert sorted(lookups) == [
        ("capture/run.py", "contract.max_video_ms(board.slug)"),
        ("contract.py", "max_video_ms(slug)"),
        ("render/probe.py", "contract.max_video_ms(slug)"),
        ("voice/step.py", "contract.max_video_ms(slug)"),
    ]
    assert reads == []
