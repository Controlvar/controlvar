"""The showcase render's gates on the encoded file (#1066; gates 1, 2 and 11).

Each decision is a pure function of ffprobe's or ffmpeg's output, so each
test plants one defect in a saved answer and reads the refusal. The defects
are the ones that pass silently otherwise: libx264 writes yuv444p with exit 0
when ``-pix_fmt`` is missing, a padded or cut encode is still a valid file,
and a file without faststart plays everywhere but streams nowhere.
"""

from __future__ import annotations

import copy

import pytest
from showcase import contract
from showcase.render import encode, probe

pytestmark = [pytest.mark.unit]

FRAMES = 1710

GOOD = {
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "profile": "High",
            "width": 1920,
            "height": 1080,
            "pix_fmt": "yuv420p",
            "sample_aspect_ratio": "1:1",
            "r_frame_rate": "30/1",
            "avg_frame_rate": "30/1",
            "nb_read_frames": "1710",
            "color_range": "tv",
            "color_space": "bt709",
        }
    ],
    "format": {"duration": "57.000000"},
}


def planted(change):
    info = copy.deepcopy(GOOD)
    change(info)
    return info


def test_the_good_answer_passes():
    assert probe.format_problems(GOOD, FRAMES)[0] == []
    assert probe.length_problems(GOOD, "v.mp4", FRAMES)[0] == []


@pytest.mark.parametrize(
    "change, message",
    [
        (
            lambda i: i["streams"][0].update(
                pix_fmt="yuv444p", profile="High 4:4:4 Predictive"
            ),
            "pix_fmt yuv444p, expected yuv420p",
        ),
        (
            lambda i: i["streams"][0].update(pix_fmt="yuvj420p"),
            "pix_fmt yuvj420p, expected yuv420p",
        ),
        (
            lambda i: i["streams"][0].update(avg_frame_rate="2997/100"),
            "variable frame rate",
        ),
        (
            lambda i: i["streams"][0].update(
                r_frame_rate="25/1", avg_frame_rate="25/1"
            ),
            "frame rate 25/1, expected 30/1",
        ),
        (
            lambda i: i["streams"][0].update(width=1280, height=720),
            "1280x720, expected 1920x1080",
        ),
        (
            lambda i: i["streams"][0].update(codec_name="vp8"),
            "codec vp8, expected h264",
        ),
        (
            lambda i: i["streams"][0].update(nb_read_frames="1700"),
            "1700 frames decoded, expected 1710",
        ),
        (
            lambda i: i["streams"][0].update(color_range="pc"),
            "color range pc, expected tv",
        ),
        (
            lambda i: i["streams"][0].update(sample_aspect_ratio="4:3"),
            "sample aspect ratio 4:3",
        ),
        (
            lambda i: i["streams"].append({"codec_type": "audio"}),
            "streams other than video: ['audio']",
        ),
        (
            lambda i: i["streams"].append(dict(i["streams"][0])),
            "2 video streams; expected exactly 1",
        ),
    ],
)
def test_gate_2_refuses_each_planted_format_defect(change, message):
    problems, _ = probe.format_problems(planted(change), FRAMES)
    assert any(message in p for p in problems), problems


def test_gate_1_refuses_a_short_video():
    info = planted(lambda i: i["format"].update(duration="40.000000"))
    problems, numbers = probe.length_problems(info, "01-getting-started.mp4", 1200)
    assert problems == ["01-getting-started.mp4 is 40.0 s; a showcase is 55-90 s"]
    assert numbers["duration_s"] == 40.0


def test_gate_1_refuses_a_file_longer_or_shorter_than_its_frames():
    info = planted(lambda i: i["format"].update(duration="58.000000"))
    problems, _ = probe.length_problems(info, "v.mp4", FRAMES)
    assert problems == ["v.mp4 is 58.000 s; its 1710 frames make 57.000 s"]


def box(kind: bytes, payload: bytes = b"") -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


def test_faststart_is_read_from_the_box_order(tmp_path):
    fast = tmp_path / "fast.mp4"
    fast.write_bytes(
        box(b"ftyp", b"isom")
        + box(b"moov", b"x" * 20)
        + box(b"free")
        + box(b"mdat", b"y" * 50)
    )
    assert probe.top_level_boxes(fast) == ["ftyp", "moov", "free", "mdat"]
    assert probe.faststart_problems(probe.top_level_boxes(fast)) == []
    slow = tmp_path / "slow.mp4"
    slow.write_bytes(
        box(b"ftyp", b"isom") + box(b"mdat", b"y" * 50) + box(b"moov", b"x" * 20)
    )
    assert probe.faststart_problems(probe.top_level_boxes(slow)) == [
        "moov after mdat: the file is not faststart"
    ]


def test_box_reader_follows_64_bit_and_to_the_end_sizes(tmp_path):
    large = (1).to_bytes(4, "big") + b"mdat" + (16 + 4).to_bytes(8, "big") + b"zzzz"
    path = tmp_path / "large.mp4"
    path.write_bytes(
        box(b"ftyp") + box(b"moov") + large + (0).to_bytes(4, "big") + b"free" + b"tail"
    )
    assert probe.top_level_boxes(path) == ["ftyp", "moov", "mdat", "free"]
    path.write_bytes(box(b"ftyp") + b"\x00\x00")
    with pytest.raises(ValueError, match="truncated box header"):
        probe.top_level_boxes(path)


def test_framemd5_lines_are_parsed_and_counted():
    text = (
        "#format: frame checksums\n#version: 2\n#tb 0: 1/30\n"
        "0,          0,          0,        1,  3110400, 0123456789abcdef0123456789abcdef\n"
        "0,          1,          1,        1,  3110400, fedcba9876543210fedcba9876543210\n"
    )
    assert probe.parse_framemd5(text) == [
        "0123456789abcdef0123456789abcdef",
        "fedcba9876543210fedcba9876543210",
    ]
    with pytest.raises(ValueError):
        probe.parse_framemd5("0, 0, 0, 1, 3110400, not-a-checksum\n")


def test_gate_11_ignores_the_cards_and_refuses_a_black_page():
    stderr = (
        "[blackdetect @ 0x1] black_start:0 black_end:3 black_duration:3\n"
        "[blackdetect @ 0x1] black_start:20.5 black_end:21.6 black_duration:1.1\n"
    )
    intervals = probe.parse_blackdetect(stderr)
    assert intervals == [(0.0, 3.0), (20.5, 21.6)]
    assert probe.black_problems(intervals, FRAMES) == [
        "black page from 20.50 s to 21.60 s, outside the cards"
    ]
    assert probe.black_problems([(0.0, 3.0), (53.0, 57.0)], FRAMES) == []


def test_the_encode_is_constant_rate_yuv420p_faststart_and_silent(tmp_path):
    argv = encode.ffmpeg_argv(
        tmp_path / "t.png",
        tmp_path / "seq",
        tmp_path / "e.png",
        1500,
        tmp_path / "o.partial",
    )
    joined = " ".join(argv)
    assert argv[argv.index("-pix_fmt") + 1] == "yuv420p"
    assert argv[argv.index("-movflags") + 1] == "+faststart"
    assert argv[argv.index("-c:v") + 1] == "libx264"
    assert argv[argv.index("-frames:v") + 1] == str(
        contract.TITLE_FRAMES + 1500 + contract.END_FRAMES
    )
    assert argv[argv.index("-color_range") + 1] == "tv"
    assert "-an" in argv and argv[argv.index("-f") + 1] == "mp4"
    # the concat FILTER joins the cards; the concat demuxer (whose durations drift) is never used
    assert "concat=n=3:v=1:a=0" in joined and "-f concat" not in joined
    assert f"overlay=x=0:y={contract.PAGE_H}" in joined
    assert (
        f"trim=end_frame={contract.TITLE_FRAMES}" in joined
        and f"trim=end_frame={contract.END_FRAMES}" in joined
    )
    assert "-n" in argv  # never overwrite


def test_sequences_are_links_to_the_capture_frames(tmp_path):
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    for name in ("000001.jpg", "000002.jpg"):
        (frames_dir / name).write_bytes(name.encode())
    band = tmp_path / "cue.png"
    band.write_bytes(b"cue")
    empty = tmp_path / "empty.png"
    empty.write_bytes(b"empty")
    count = encode.build_sequences(
        frames_dir,
        ["000001.jpg", "000002.jpg"],
        [0, 0, 1],
        [band],
        [None, 0, 0],
        empty,
        tmp_path / "seq",
    )
    assert count == 3
    assert (tmp_path / "seq/page/000003.jpg").read_bytes() == b"000002.jpg"
    assert (tmp_path / "seq/band/000001.png").read_bytes() == b"empty"
    assert (tmp_path / "seq/band/000002.png").read_bytes() == b"cue"
    assert (tmp_path / "seq/page/000001.jpg").stat().st_ino == (
        frames_dir / "000001.jpg"
    ).stat().st_ino
