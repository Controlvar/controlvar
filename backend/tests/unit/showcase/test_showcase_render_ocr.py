"""The showcase render's OCR decisions (#1066; gates 5, 7 and U4).

The OCR engines themselves run only on a developer's machine. What is pinned
here is every decision made from their text: the fuzzy needle match (with the
misreadings measured on this project's frames: ``_`` read as a space, ``b``
read as ``o``, ``1`` read as ``l``), the key and token shapes, the forbidden
phrases, the caption read-back, and that an engine which fails or reads
nothing stops the scan instead of passing it.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest
from showcase import contract
from showcase.render import encode, ocr, probe, templates
from showcase.render.gates import Refused
from showcase.render.timeline import OutputCue
from showcase.render.workdir import WorkDir

pytestmark = [pytest.mark.unit]

NEEDLE = "VALUE-3F9A1C07B2E4D586"


def workdir(tmp_path: Path) -> WorkDir:
    return WorkDir(tmp_path, needles=tmp_path / "capture" / "needles.txt")


def brute_distance(pattern: str, text: str) -> int:
    """Semi-global edit distance by the textbook table, to check Myers against."""
    previous = [0] * (len(text) + 1)
    for i, p in enumerate(pattern, start=1):
        current = [i] + [0] * len(text)
        for j, t in enumerate(text, start=1):
            current[j] = min(
                previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (p != t)
            )
        previous = current
    return min(previous)


def test_myers_matches_the_textbook_table():
    rng = random.Random(1066)
    for _ in range(400):
        pattern = "".join(rng.choice("abcde") for _ in range(rng.randint(1, 30)))
        text = "".join(rng.choice("abcde") for _ in range(rng.randint(0, 80)))
        assert ocr.best_distance(pattern, text) == brute_distance(pattern, text), (
            pattern,
            text,
        )


def test_normalise_folds_what_ocr_confuses():
    assert ocr.normalise("O0@ 1lI| fT 2Z 5S 8B _-:") == "ooollllttzzssbb"
    assert (
        ocr.normalise("eptk_demo_shoplab")
        == ocr.normalise("EPTK DEMO SHOPLAB")
        == "eptkdemoshoplab"
    )


@pytest.mark.parametrize(
    "ocr_text",
    [
        f"Shown once: {NEEDLE}",
        "Shown once: VALUE 3F9A1C07B2E4D586",  # dash read as a space
        "Shown once: VALUE-3F9AlC07B2E4D5B6",  # 1 read as l, 8 as B
        "shown VALUE-3F9A1C07B2E4D5",  # cut off at the edge: a part of it
        "VALUE-3F9A1C0\n7B2E4D586",  # wrapped onto two lines
    ],
)
def test_a_needle_is_found_through_misreadings(ocr_text):
    found = ocr.findings(ocr_text, [NEEDLE, "OTHER-VALUE-XYZXYZXYZ"])
    assert [gate for gate, _ in found] == ["7"]
    assert found[0][1].startswith("needle 1 of 2 at ")
    for _, what in found:
        assert NEEDLE not in what and "3F9A" not in what


def test_ordinary_dashboard_text_is_not_a_needle():
    text = (
        "Controlvar Experiments Feature Flags Admin\nHomepage hero copy test Active 2 variants 12,408 users\n"
        "Checkout button colour Draft 3 variants\nAPI key eptk_ab12... created today\nSign in as admin@demo.com"
    )
    assert ocr.findings(text, [NEEDLE, "Demo1234!x"]) == []


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Header eptk_shown_in_full_here", "key-shaped"),
        ("eptk wholevalue", "key-shaped"),
        ("shown eyJ" + "abcdefghij" * 2, "token-shaped"),
        ("Live view Disconnected", "forbidden text 'disconnected'"),
        ("Open http://localhost:28401 in a browser", "forbidden text 'localhost'"),
        ("Something  went\nwrong", "forbidden text 'something went wrong'"),
    ],
)
def test_shapes_and_forbidden_phrases_are_found(text, expected):
    found = ocr.findings(text, [NEEDLE])
    assert any(expected in what for _, what in found), found


@pytest.mark.parametrize(
    "text",
    [
        "Header eptk_3ny\u0431vjeep4w47pu723gccx5m",  # Vision read the 6 as Cyrillic be (measured)
        "Header eptk_gbj fqu9uSxkyemgms fms jbes",  # tesseract split the value with spaces (measured)
    ],
)
def test_a_key_is_found_through_measured_misreadings(text):
    assert [g for g, _ in ocr.findings(text, [NEEDLE])] == ["7"]


@pytest.mark.parametrize(
    "text",
    [
        "Changes we kept keep every flag in step",  # 'kept keep' holds e-p-t-k only across a space
        "They just add a variant and the rest follows without any extra work at all",
        "API key eptk_ab12... created today",
    ],
)
def test_ordinary_words_are_not_key_or_token_shaped(text):
    assert ocr.findings(text, [NEEDLE]) == []


def test_the_key_list_display_is_not_key_shaped():
    for shown in ("API key eptk_ab12... created", "eptk_ab12…", "eptk ab12 ..."):
        assert ocr.findings(shown, [NEEDLE]) == [], shown


def test_forbidden_text_belongs_to_u4_and_credentials_to_7():
    gates = {g for g, _ in ocr.findings(f"Disconnected {NEEDLE}", [NEEDLE])}
    assert gates == {"7", "U4"}


def test_a_long_needle_is_matched_in_pieces():
    long_needle = "eyJ" + "A1b2C3d4E5f6G7h8I9j0" * 5
    pieces = ocr.chunks(ocr.normalise(long_needle))
    assert {len(p) for p in pieces} == {ocr.CHUNK, ocr.SHORT_CHUNK}
    assert sum(len(p) == ocr.CHUNK for p in pieces) > 1
    # 30 characters of it on screen is enough
    assert (
        ocr.needle_similarity(
            ocr.normalise(long_needle), ocr.normalise(long_needle[10:40])
        )
        >= 0.99
    )


# --- Engines that fail never pass --------------------------------------------------------------


class FakeEngine(ocr.Engine):
    def __init__(self, name, answer):
        super().__init__(name=name, binary="fake")
        self.answer = answer

    def read(self, paths):
        return self.answer(paths)


def test_a_crashed_engine_stops_the_scan():
    def crash(paths):
        raise ocr.OcrError("tesseract exited 1 on frame-000123.png")

    with pytest.raises(Refused) as caught:
        ocr._read(FakeEngine("tesseract", crash), [Path("a.png")], "7")
    assert (
        "OCR failed (tesseract: tesseract exited 1 on frame-000123.png); the scan is not counted"
        in str(caught.value)
    )


def test_an_engine_that_skips_a_frame_stops_the_scan():
    with pytest.raises(
        Refused, match="vision read 1 of 2 frames; the scan is not counted"
    ):
        ocr._read(
            FakeEngine("vision", lambda paths: {paths[0]: "text"}),
            [Path("a.png"), Path("b.png")],
            "7",
        )


def test_the_self_test_refuses_an_engine_that_reads_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(encode, "png_to_h264_and_back", lambda png, work: png)
    blind = FakeEngine("tesseract", lambda paths: dict.fromkeys(paths, ""))
    with pytest.raises(Refused) as caught:
        ocr.self_test(
            [blind],
            lambda needle, key, faint, path: path.write_bytes(b"png"),
            workdir(tmp_path),
        )
    text = str(caught.value)
    assert (
        "OCR self-test: tesseract read the canary at 0.00 (needs 0.70); scan not run"
        in text
    )
    assert "did not see the key-shaped canary; scan not run" in text


def test_the_self_test_passes_an_engine_that_reads_the_canary(tmp_path, monkeypatch):
    monkeypatch.setattr(encode, "png_to_h264_and_back", lambda png, work: png)
    drawn = {}

    def draw(needle, key, faint, path):
        drawn["text"] = f"Shown once: {needle}\nHeader {key}\nLast edited by {faint}"
        path.write_bytes(b"png")

    reader = FakeEngine("vision", lambda paths: dict.fromkeys(paths, drawn["text"]))
    assert ocr.self_test([reader], draw, workdir(tmp_path)) == {
        "canary": {"vision": 1.0},
        "faint": {"vision": 1.0},
    }
    assert not (tmp_path / "canary").exists()


def test_no_engine_at_all_is_a_refusal(tmp_path):
    with pytest.raises(Refused, match="no OCR engine"):
        ocr.self_test([], lambda *a: None, workdir(tmp_path))


def test_an_empty_needle_list_is_refused_before_decoding(tmp_path):
    with pytest.raises(Refused, match="0 needles: refusing to scan"):
        ocr.scan(tmp_path / "v.mp4", 1710, [], [], workdir(tmp_path), lambda: None)


# --- Gate 5: the burned-in captions ---------------------------------------------------------------


CUES = [
    OutputCue(
        number=1,
        start_frame=96,
        end_frame=239,
        lines=("One command starts the whole stack.",),
    ),
    OutputCue(
        number=2,
        start_frame=245,
        end_frame=388,
        lines=("Open the dashboard in your browser.",),
    ),
]


def test_caption_samples_are_inside_each_cue_and_after_it():
    samples = ocr.caption_samples(CUES)
    assert samples == [
        (105, 1, "start"),
        (229, 1, "end"),
        (316, 1, "after"),
        (254, 2, "start"),
        (378, 2, "end"),
    ]


def test_captions_read_back_pass():
    read = {
        105: ["One command starts the whole stack."],
        229: ["One command starts the whole stack."],
    }
    read.update(
        {
            316: ["Open the dashboard in your browser."],
            254: ["Open the dashboard in your browser."],
        }
    )
    read[378] = ["Open the dashbcard in your browser."]
    problems, numbers = ocr.caption_problems(CUES, ocr.caption_samples(CUES), read)
    assert problems == [] and numbers["confirmed"] == 2


def test_a_missing_caption_layer_is_refused():
    read = {frame: [""] for frame, _, _ in ocr.caption_samples(CUES)}
    problems, numbers = ocr.caption_problems(CUES, ocr.caption_samples(CUES), read)
    assert "cue 1 not in the frame at 3.500 s (similarity 0.00)" in problems
    assert numbers["confirmed"] == 0


def test_a_caption_that_stays_too_long_is_refused():
    read = {
        frame: ["One command starts the whole stack."]
        for frame, _, _ in ocr.caption_samples(CUES)
    }
    problems, _ = ocr.caption_problems(CUES, ocr.caption_samples(CUES), read)
    assert (
        "cue 1 still in the frame at 10.533 s, during cue 2 (similarity 1.00)"
        in problems
    )
    assert any(p.startswith("cue 2 not in the frame") for p in problems)


def test_contract_fps_is_the_one_the_samples_assume():
    assert ocr.CAPTION_SAMPLE_FRAMES == round(0.3 * contract.FPS)


# --- Review round 1 (#1077): what the scan finds, and what it refuses to start on -----------------

#: Made-up text of the kind a dashboard frame holds, for the no-false-match checks.
DASHBOARD_TEXT = (
    "Controlvar Experiments Feature Flags Segments Reports Admin Audit Log\n"
    "Experiments 4 experiments in the demo data New experiment\n"
    "Homepage hero copy test Active 2 variants 12,408 users Started 3 days ago\n"
    "Checkout button colour Draft 3 variants 0 users\n"
    "Pricing page layout Completed 2 variants 48,120 users p-value 0.031 lift +4.2%\n"
    "API keys Name Prefix Scopes Created Last used eptk_ab12... Storefront demo key\n"
    "Signed in as admin@demo.com Change password Log out Superuser\n"
    "Rollout schedule Stage 2 of 3 25% Safety check passed Error rate 0.4%\n"
)


@pytest.mark.regression
def test_a_short_needle_cut_off_at_the_edge_is_found():
    """A 22-character needle with only its first 15 characters on screen.

    It passed before short needles were cut into pieces: the whole needle was
    7 edits from the text, 0.68.
    """
    needle = "QX7K-MPL2-ZRV9-WTN4-HJ6"
    assert 13 < len(ocr.normalise(needle)) <= ocr.CHUNK
    found = ocr.findings(f"Device code {needle[:15]}", [needle])
    assert [gate for gate, _ in found] == ["7"], found


@pytest.mark.regression
@pytest.mark.parametrize("length", [13, 16, 20, 23, 24, 25, 31, 40, 52, 64])
def test_any_12_characters_of_a_needle_in_a_row_are_found(length):
    """The guarantee the docstring gives, for every place the 12 can start."""
    rng = random.Random(length)
    alphabet = "acdeghjkmnpqrvwxy34679"  # no character normalise folds or drops
    needle = "".join(rng.choice(alphabet) for _ in range(length))
    for start in range(length - 12 + 1):
        shown = needle[start : start + 12]
        found = ocr.findings(f"Created by the import {shown} yesterday", [needle])
        assert [gate for gate, _ in found] == ["7"], (start, shown)


def test_each_piece_step_keeps_the_guarantee():
    for size, step in (
        (ocr.SHORT_CHUNK, ocr.SHORT_CHUNK_STEP),
        (ocr.CHUNK, ocr.CHUNK_STEP),
    ):
        assert step <= ocr.allowed_edits(size) + 1, (size, step)
    assert ocr.allowed_edits(12) == 3 and ocr.allowed_edits(24) == 7
    assert contract.MIN_NEEDLE_CHARS <= ocr.SHORT_CHUNK <= ocr.CHUNK


def test_the_fast_match_gives_the_same_answer_as_the_full_one():
    rng = random.Random(77)
    for _ in range(600):
        needle = "".join(rng.choice("abcdef") for _ in range(rng.randint(8, 40)))
        text = "".join(rng.choice("abcdefgh") for _ in range(rng.randint(0, 120)))
        if rng.random() < 0.5 and len(needle) > 3:
            at = rng.randint(0, len(text))
            cut = needle[rng.randint(0, 3) : len(needle) - rng.randint(0, 3)]
            text = text[:at] + cut + text[at:]
        full = ocr.needle_similarity(needle, text)
        fast = ocr.needle_match(needle, text)
        if full >= ocr.NEEDLE_THRESHOLD:
            assert fast == pytest.approx(full), (needle, text)
        else:
            assert fast is None, (needle, text)


def test_short_pieces_do_not_match_ordinary_dashboard_text():
    rng = random.Random(1077)
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ0123456789abcdef"
    needles = [
        "".join(rng.choice(alphabet) for _ in range(rng.randint(8, 64)))
        for _ in range(300)
    ] + [
        "Example-Pass-9921!",
        "eptk_demo_teststore_" + "a1b2c3d4" * 4,
        "local-only-sample-" + "f0e1d2c3" * 4,
    ]
    assert ocr.findings(DASHBOARD_TEXT, needles) == []


@pytest.mark.regression
def test_an_empty_needle_is_refused_not_divided_by():
    with pytest.raises(ValueError, match="empty needle"):
        ocr.chunks("")


@pytest.mark.regression
def test_the_scan_refuses_a_needle_too_short_once_normalised(tmp_path, monkeypatch):
    """Before, ``--------`` reached the match and divided by zero."""
    fake_video(monkeypatch, ["a" * 32])
    with pytest.raises(Refused, match=r"needles \[2\] are under 8 characters"):
        ocr.scan(
            tmp_path / "v.mp4",
            1,
            [NEEDLE, "--------"],
            [reader_by_frame({0: "Experiments"})],
            workdir(tmp_path),
            lambda: None,
        )


# --- The scan checks the frames it is given ---------------------------------------------------------


def fake_video(monkeypatch, sums, extract=None):
    """A video whose decoded checksums are ``sums``; frames extract as files named for their index."""
    monkeypatch.setattr(probe, "framemd5", lambda video: list(sums))

    def default_extract(video, indices, out_dir, **kwargs):
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for index in sorted(set(indices)):
            path = out_dir / f"frame-{index:06d}.png"
            path.write_bytes(b"png")
            paths.append(path)
        return paths

    monkeypatch.setattr(encode, "extract_frames", extract or default_extract)


def reader_by_frame(texts):
    """An engine that reads each extracted frame's text from ``texts`` by frame index."""
    return FakeEngine(
        "vision",
        lambda paths: {p: texts.get(int(p.stem.split("-")[1]), "") for p in paths},
    )


def test_the_scan_finds_a_needle_in_one_frame(tmp_path, monkeypatch):
    fake_video(monkeypatch, ["a" * 32, "b" * 32, "b" * 32])
    engine = reader_by_frame({1: f"One-time password {NEEDLE}"})
    with pytest.raises(Refused) as caught:
        ocr.scan(
            tmp_path / "v.mp4", 3, [NEEDLE], [engine], workdir(tmp_path), lambda: None
        )
    assert "frame 1 (0.03 s): needle 1 of 1 at 1.00, read by vision" in str(
        caught.value
    )
    assert not (tmp_path / "ocr" / "batch-000").exists()


def test_a_decoded_frame_count_that_is_not_the_video_s_is_refused(
    tmp_path, monkeypatch
):
    fake_video(monkeypatch, ["a" * 32] * 1709)
    with pytest.raises(Refused, match="decoded 1709 frames, expected 1710"):
        ocr.scan(
            tmp_path / "v.mp4",
            1710,
            [NEEDLE],
            [reader_by_frame({})],
            workdir(tmp_path),
            lambda: None,
        )


@pytest.mark.regression
def test_zero_frames_is_never_a_clean_scan(tmp_path, monkeypatch):
    fake_video(monkeypatch, [])
    with pytest.raises(Refused, match="decoded 0 frames, expected 0"):
        ocr.scan(
            tmp_path / "v.mp4",
            0,
            [NEEDLE],
            [reader_by_frame({})],
            workdir(tmp_path),
            lambda: None,
        )


@pytest.mark.regression
def test_an_extraction_that_returns_the_wrong_frames_is_refused(tmp_path, monkeypatch):
    """Frame 1 holds the needle, but the extraction hands back frame 0 twice.

    Before the scan checked what it was given, frame 1 was never read and the
    scan passed.
    """

    def duplicate(video, indices, out_dir, **kwargs):
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "frame-000000.png"
        path.write_bytes(b"png")
        return [path for _ in sorted(set(indices))]

    fake_video(monkeypatch, ["a" * 32, "b" * 32], duplicate)
    engine = reader_by_frame({1: f"One-time password {NEEDLE}"})
    with pytest.raises(Refused, match="extracted 1 distinct frames of the 2 asked for"):
        ocr.scan(
            tmp_path / "v.mp4", 2, [NEEDLE], [engine], workdir(tmp_path), lambda: None
        )


# --- The faint canary ---------------------------------------------------------------------------------


class Canary:
    """Draws the canary page as text; ``reader(faint=...)`` reads it back with or without the faint line."""

    def __init__(self):
        self.lines = {}

    def draw(self, needle, key, faint, path):
        self.lines = {
            "dark": f"One-time password {needle}\nX-API-Key {key}",
            "faint": f"Last edited by {faint}",
        }
        path.write_bytes(b"png")

    def reader(self, *, faint):
        def read(paths):
            text = self.lines["dark"] + ("\n" + self.lines["faint"] if faint else "")
            return dict.fromkeys(paths, text)

        return read


def test_the_faint_canary_is_the_faintest_dashboard_style_or_fainter():
    assert templates.FAINT_CANARY_PX == 13
    assert templates.FAINT_CANARY_COLOUR == "#94A3B8"
    page = templates.canary_html("CANARY-AAAA", "eptk_bbbb", "FAINT-CCCC")
    assert (
        ".faint{font-size:13px;color:#94A3B8;" in page
        and '<div class="faint">Last edited by FAINT-CCCC</div>' in page
    )


@pytest.mark.regression
def test_the_self_test_refuses_when_no_engine_reads_the_faint_canary(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(encode, "png_to_h264_and_back", lambda png, work: png)
    canary = Canary()
    engines = [
        FakeEngine("vision", canary.reader(faint=False)),
        FakeEngine("tesseract", canary.reader(faint=False)),
    ]
    with pytest.raises(Refused) as caught:
        ocr.self_test(engines, canary.draw, workdir(tmp_path))
    text = str(caught.value)
    assert (
        "OCR self-test: no engine read the faint canary (13 px #94A3B8: vision 0."
        in text
    )
    assert "scan not run" in text
    assert not (tmp_path / "canary").exists()


def test_one_engine_reading_the_faint_canary_is_enough(tmp_path, monkeypatch):
    monkeypatch.setattr(encode, "png_to_h264_and_back", lambda png, work: png)
    canary = Canary()
    engines = [
        FakeEngine("vision", canary.reader(faint=False)),
        FakeEngine("tesseract", canary.reader(faint=True)),
    ]
    result = ocr.self_test(engines, canary.draw, workdir(tmp_path))
    assert result["canary"] == {"vision": 1.0, "tesseract": 1.0}
    assert result["faint"]["tesseract"] == 1.0
    assert result["faint"]["vision"] < ocr.NEEDLE_THRESHOLD
