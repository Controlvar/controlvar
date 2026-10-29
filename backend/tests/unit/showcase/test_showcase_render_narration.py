"""A narrated capture in the render (voice PR C1): voice.json, TILE, captions,
transcript, placement, and the sound gates' decisions.

Everything here is pure Python on stdlib-written clips and on ffprobe and
ffmpeg output saved from the real binaries (``fixtures/``), so it runs in the
CI unit job, which has no ffmpeg. The same gates on a real narrated MP4 are
tamper-tested on a developer's machine (``showcase.render.synthetic
--voice-cache``).
"""

from __future__ import annotations

import ast
import copy
import inspect
import json
import shutil
import wave
from html import escape as html_escape
from pathlib import Path

import pytest
from showcase import contract
from showcase.capture import config
from showcase.render import narration, pipeline, probe, review, sound, timeline, vtt

from backend.tests.unit.showcase.capture_dirs import (
    CLIP_LEAD,
    CLIP_SAMPLES,
    CLIP_TONE,
    VOICED_SAY,
    clip_bytes,
    edit_json,
    make_capture,
    make_voiced_capture,
    write_clip,
)

pytestmark = [pytest.mark.unit]

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SHOWCASE = Path(__file__).resolve().parents[4] / "tests" / "acceptance" / "showcase"


def refused(root: Path) -> str:
    with pytest.raises(contract.CaptureRefused) as caught:
        contract.validate_capture_dir(root)
    return str(caught.value)


@pytest.fixture
def voiced(tmp_path: Path) -> Path:
    return make_voiced_capture(tmp_path / "capture")


def spoken(root: Path) -> contract.Narration:
    capture = contract.validate_capture_dir(root)
    assert capture.narration is not None
    return capture.narration


# --- voice.json and the manifest's voice ------------------------------------


def test_a_narrated_capture_reads_back_its_sentences(voiced: Path):
    capture = contract.validate_capture_dir(voiced)
    assert capture.manifest.voice == "Ruth"
    texts = [s.text for s in capture.narration.sentences]
    assert texts == [t for said in VOICED_SAY for t in said]
    assert len(texts) == 15
    assert [s.cue for s in capture.narration.sentences][:4] == [1, 1, 2, 3]


def test_a_silent_capture_has_no_narration(tmp_path: Path):
    """EM condition 5 (T154): VOICE=none is today's capture, with no narration."""
    capture = contract.validate_capture_dir(make_capture(tmp_path / "c"))
    assert capture.manifest.voice == contract.SILENT
    assert capture.narration is None


def test_narration_frozen_under_voice_none_is_refused(voiced: Path):
    """The tamper for EM condition 5: a voice.json in a capture that says none."""
    edit_json(voiced / "manifest.json", lambda m: m.update(voice="none"))
    assert "the manifest says voice none, but voice.json or voice/ is there" in refused(
        voiced
    )


def test_a_narrated_capture_that_lost_its_voice_json_is_refused(voiced: Path):
    (voiced / "voice.json").unlink()
    assert "voice.json is missing: a narrated capture lost its narration" in refused(
        voiced
    )


def test_the_manifest_must_say_its_voice(tmp_path: Path):
    root = make_capture(tmp_path / "c")
    edit_json(root / "manifest.json", lambda m: m.pop("voice"))
    assert "manifest.json is missing ['voice']" in refused(root)
    edit_json(root / "manifest.json", lambda m: m.update(voice="Danielle"))
    assert "manifest voice 'Danielle' is not one of ['none', 'Ruth']" in refused(root)


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda v: v.update(profile="dev"), "unexpected ['profile']"),
        (lambda v: v.update(tempo="1.0"), "voice.json tempo '1.0' is not '0.9'"),
        (lambda v: v.update(rate=24000), "voice.json rate 24000 is not 48000"),
        (lambda v: v.update(recipe=True), "voice.json recipe True is not 1"),
        (lambda v: v.update(sentences=[]), "voice.json has no sentences"),
        (
            lambda v: v["sentences"][0].update(request_id="x"),
            "voice.json sentence 1 has keys missing [] and unexpected ['request_id']",
        ),
    ],
)
def test_voice_json_has_exactly_its_keys_and_values(voiced: Path, change, message):
    edit_json(voiced / "voice.json", change)
    assert message in refused(voiced)


def test_a_sentence_edited_after_capture_no_longer_matches_its_clip(voiced: Path):
    """G8 at the render: the key is recomputed from the frozen text."""
    edit_json(
        voiced / "voice.json",
        lambda v: v["sentences"][1].update(text="It runs on any machine."),
    )
    assert (
        "voice.json sentence 2: its key is not the key of its text spoken by Ruth"
        in (refused(voiced))
    )


def test_a_clip_copied_over_another_is_refused(voiced: Path):
    shutil.copyfile(
        voiced / "voice" / "003.wav", voice_file := voiced / "voice" / "004.wav"
    )
    assert voice_file.exists()
    assert "voice.json sentence 4: voice/004.wav is not the clip voice.json names" in (
        refused(voiced)
    )


def test_a_clip_at_another_rate_is_refused(voiced: Path):
    path = voiced / "voice" / "002.wav"
    write_clip(path, clip_bytes(2), rate=24_000)
    edit_json(
        voiced / "voice.json",
        lambda v: v["sentences"][1].update(sha256=contract.file_sha256(path)),
    )
    assert "voice/002.wav is 24000 Hz, 1 channel(s), 16-bit" in refused(voiced)


def test_a_clip_voice_json_does_not_list_is_refused(voiced: Path):
    write_clip(voiced / "voice" / "099.wav", clip_bytes(9))
    assert "voice/ holds files voice.json does not list: ['099.wav']" in refused(voiced)


def test_a_linked_clip_is_refused(voiced: Path, tmp_path: Path):
    outside = tmp_path / "elsewhere.wav"
    shutil.move(voiced / "voice" / "001.wav", outside)
    (voiced / "voice" / "001.wav").symlink_to(outside)
    assert "voice/001.wav is missing (or is not a plain file)" in refused(voiced)


def test_every_cue_is_narrated(tmp_path: Path):
    root = make_voiced_capture(tmp_path / "c")
    edit_json(
        root / "voice.json",
        lambda v: v.update(sentences=[s for s in v["sentences"] if s["cue"] != 10]),
    )
    (root / "voice" / "015.wav").unlink()
    assert "voice.json narrates cues 1-9 of 10" in refused(root)


def test_sentences_come_in_order(voiced: Path):
    edit_json(voiced / "voice.json", lambda v: v["sentences"][1].update(line=3))
    assert "voice.json sentence 2 is cue 1 line 3; expected cue 1 line 2" in refused(
        voiced
    )


def test_a_sentence_too_long_for_a_caption_is_refused(voiced: Path):
    text = "That is the whole loop, start to finish, on one single machine today."
    edit_json(
        voiced / "voice.json",
        lambda v: v["sentences"][14].update(
            text=text, key=contract.clip_key("Ruth", contract.polly_text(text))
        ),
    )
    root = voiced
    assert "voice.json sentence 15: the sentence has 13 words; at most 12" in refused(
        root
    )


# --- The TILE rule ------------------------------------------------------------


def test_the_tile_rule_in_frames():
    parts = [("Open the dashboard.", 48_000), ("Choose Sign in.", 24_000)]
    # Slots: max(1,000 ms clip, 2,000 ms reading) + 50 = 2,050 ms = 61.5 frames, so 62.
    assert contract.slot_frames(*parts[0]) == 62
    assert contract.tile_frames(100, 300, parts) == [(100, 162), (162, 300)]
    assert contract.tile_frames(100, 223, parts) is None
    assert contract.tile_frames(100, 224, parts) == [(100, 162), (162, 224)]


@pytest.mark.parametrize(
    "sentence, field, delta, message",
    [
        (
            0,
            "start_ms",
            33,
            "sentence 1 (cue 1 line 1) is frames 7-68; the TILE rule puts it at 6-68",
        ),
        (
            0,
            "end_ms",
            33,
            "sentence 1 (cue 1 line 1) is frames 6-69; the TILE rule puts it at 6-68",
        ),
        (
            1,
            "end_ms",
            -33,
            "sentence 2 (cue 1 line 2) is frames 69-148; the TILE rule puts it at 68-149",
        ),
    ],
)
def test_a_sentence_off_its_tile_by_one_frame_is_refused(
    voiced: Path, sentence, field, delta, message
):
    def shift(v):
        v["sentences"][sentence][field] += delta
        if field == "end_ms" and sentence == 0:
            v["sentences"][1]["start_ms"] += delta
        if delta < 0:
            v["sentences"][1]["start_ms"] += 33

    edit_json(voiced / "voice.json", shift)
    assert message in refused(voiced)


def test_a_cue_too_short_for_its_sentences_is_refused(voiced: Path):
    edit_json(
        voiced / "voice.json", lambda v: v["sentences"][0].update(samples=200_000)
    )
    path = voiced / "voice" / "001.wav"
    write_clip(path, bytes(400_000))
    edit_json(
        voiced / "voice.json",
        lambda v: v["sentences"][0].update(sha256=contract.file_sha256(path)),
    )
    assert "cue 1 is 143 frames; its 2 sentences' slots need 194" in refused(voiced)


# --- Gate 4: the WebVTT file holds the narration, tiled ------------------------


def band_cues(root: Path):
    return timeline.output_cues(contract.validate_capture_dir(root).sheet)


def total(root: Path) -> int:
    return contract.video_frames(contract.validate_capture_dir(root).sheet.duration_ms)


def gate4(root: Path, text: str):
    story = spoken(root)
    parsed = vtt.parse_vtt(text)
    return vtt.check_vtt(
        parsed, narration.sentence_cues(story), total(root)
    ) + narration.tile_problems(parsed, band_cues(root), story)


def test_the_narrated_webvtt_file_passes_gate_4(voiced: Path):
    text = vtt.write_vtt(narration.sentence_cues(spoken(voiced)))
    assert gate4(voiced, text) == []
    assert "00:00:03.200 --> 00:00:05.267\nOne command starts it." in text


def test_a_webvtt_file_written_from_the_headlines_fails_gate_4(voiced: Path):
    text = vtt.write_vtt(band_cues(voiced))
    problems = gate4(voiced, text)
    assert "10 cues parsed, the cue sheet has 15" in problems
    assert "10 cues read back, the narration has 15" in problems


def test_a_sentence_cue_one_frame_off_its_band_start_fails_gate_4(voiced: Path):
    text = vtt.write_vtt(narration.sentence_cues(spoken(voiced)))
    planted = text.replace("00:00:03.200 --> ", "00:00:03.233 --> ", 1)
    problems = gate4(voiced, planted)
    assert "sentence 1 starts on frame 97; cue 1's first frame is 96" in problems
    assert (
        "cue 1 is at 3233-5267 ms; the burned-in caption is at 3200-5267 ms" in problems
    )


def test_a_sentence_cue_shorter_than_its_reading_time_fails_gate_4(voiced: Path):
    """The reading-time rule is unchanged for sentences (no check_vtt rule loosened)."""
    text = vtt.write_vtt(narration.sentence_cues(spoken(voiced)))
    planted = text.replace(
        "00:00:03.200 --> 00:00:05.267", "00:00:03.200 --> 00:00:04.700", 1
    )
    planted = planted.replace("00:00:05.267 --> ", "00:00:04.700 --> ", 1)
    problems = gate4(voiced, planted)
    assert "cue 1: 22 characters in 1.500 s, needs 2.000 s" in problems
    assert "sentence 1 lasts 45 frames; its slot is 62" in problems


# --- SRT and the transcript (G6) and gate 7 on the files ----------------------


def test_the_srt_file_says_what_the_webvtt_file_says(voiced: Path):
    cues = narration.sentence_cues(spoken(voiced))
    srt = narration.write_srt(cues)
    assert srt.startswith(
        "1\n00:00:03,200 --> 00:00:05,267\nOne command starts it.\n\n2\n"
    )
    parsed = vtt.parse_vtt(vtt.write_vtt(cues))
    assert narration.srt_problems(narration.parse_srt(srt), parsed) == []
    moved = srt.replace(
        "00:00:03,200 --> 00:00:05,267", "00:00:03,200 --> 00:00:05,300", 1
    )
    assert narration.srt_problems(narration.parse_srt(moved), parsed) == [
        "SRT cue 1 is at 3200-5300 ms; the WebVTT cue is at 3200-5267 ms"
    ]
    reworded = srt.replace("One command starts it.", "One command starts all.", 1)
    assert narration.srt_problems(narration.parse_srt(reworded), parsed) == [
        "SRT cue 1's text differs from the WebVTT cue's"
    ]


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda t: "﻿" + t, "starts with a byte-order mark"),
        (lambda t: t.replace("\n", "\r\n"), "carriage returns"),
        (lambda t: t + "\n", "exactly one line break"),
        (
            lambda t: t.replace("00:00:03,200", "00:00:03.200", 1),
            "is not 'HH:MM:SS,mmm --> HH:MM:SS,mmm'",
        ),
        (lambda t: t.replace("1\n", "0\n", 1), "SRT block 1 is numbered '0'"),
    ],
)
def test_the_srt_parser_is_strict(voiced: Path, change, message):
    srt = narration.write_srt(narration.sentence_cues(spoken(voiced)))
    with pytest.raises(vtt.VttError, match=message.replace("(", r"\(")):
        narration.parse_srt(change(srt))


def test_the_transcript_is_the_webvtt_sentences_in_order(voiced: Path):
    story = spoken(voiced)
    parsed = vtt.parse_vtt(vtt.write_vtt(narration.sentence_cues(story)))
    text = narration.transcript_text(story)
    assert text.startswith(
        "One command starts it. It runs on your machine.\n\nOpen the dashboard.\n\n"
    )
    assert len(text[:-1].split("\n\n")) == 10
    assert narration.transcript_problems(text, parsed, story) == []


def test_one_word_changed_in_the_transcript_only_fails_g6(voiced: Path):
    story = spoken(voiced)
    parsed = vtt.parse_vtt(vtt.write_vtt(narration.sentence_cues(story)))
    text = narration.transcript_text(story).replace("whole loop", "full loop")
    assert narration.transcript_problems(text, parsed, story) == [
        "the transcript's paragraph 10 is not what the WebVTT file says over cue 10"
    ]
    merged = narration.transcript_text(story).replace("\n\nOpen", " Open", 1)
    assert narration.transcript_problems(merged, parsed, story) == [
        "the transcript has 9 paragraphs; the video has 10 cues"
    ]


def test_gate_7_reads_each_published_text_file_as_written(tmp_path: Path):
    files = {}
    for label, body in (
        ("WebVTT file", "WEBVTT\n\n1\n00:00:03.200 --> 00:00:05.000\nNothing here.\n"),
        (
            "SRT file",
            "1\n00:00:03,200 --> 00:00:05,000\nThe code is ZEBRA-VALUE-987-ALPHA.\n",
        ),
        ("transcript", "The code is ZEBRA VALUE 987 ALPHA.\n"),
    ):
        files[label] = tmp_path / label.replace(" ", "-")
        files[label].write_text(body, encoding="utf-8")
    needles = ["ZEBRA-VALUE-987-ALPHA", "QUOKKA-VALUE-123-BRAVO"]
    assert pipeline.published_text_hits(files, needles) == [
        "the SRT file holds needle 1 of 2",
        "the transcript holds needle 1 of 2",
    ]


# --- G15: the narration track ---------------------------------------------------


def track_samples(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (
            48_000,
            1,
            2,
        )
        return wav.readframes(wav.getnframes())


def test_each_clip_is_placed_on_its_sentences_first_frame(voiced: Path, tmp_path: Path):
    story = spoken(voiced)
    frames = total(voiced)
    track = tmp_path / "narration.wav"
    narration.build_wav(story, frames, track)
    data = track_samples(track)
    # Typed from the frame numbers, not computed by the code under test:
    # cue 1 starts at 200 ms = recording frame 6 = video frame 96 (after the
    # 90-frame title card) = sample 96 x 1,600 = 153,600; its second sentence
    # starts 62 frames later, on video frame 158 = sample 252,800.
    assert narration.offsets(story)[:2] == [153_600, 252_800]
    assert len(data) == frames * 1600 * 2
    assert data[153_600 * 2 : (153_600 + CLIP_SAMPLES) * 2] == clip_bytes(1)
    assert data[252_800 * 2 : (252_800 + CLIP_SAMPLES) * 2] == clip_bytes(2)
    assert data[: 153_600 * 2] == bytes(153_600 * 2)
    assert narration.placement_problems(track, story, frames) == (
        [],
        {"samples": frames * 1600, "expected_samples": frames * 1600, "clips": 15},
    )


def test_a_clip_one_frame_late_fails_g15(voiced: Path, tmp_path: Path):
    story = spoken(voiced)
    frames = total(voiced)
    track = tmp_path / "narration.wav"
    places = narration.offsets(story)
    places[2] += 1600
    narration.build_wav(story, frames, track, places=places)
    problems, _ = narration.placement_problems(track, story, frames)
    # Cue 2 starts at 5,180 ms = recording frame 155 = video frame 245 =
    # sample 392,000; the clip was put one frame (1,600 samples) later.
    assert places[2] == 393_600
    assert problems == ["sentence 3's clip is not at sample 392000 (frame 245)"]


def test_a_track_shorter_than_the_video_fails_g15(voiced: Path, tmp_path: Path):
    story = spoken(voiced)
    frames = total(voiced)
    track = tmp_path / "narration.wav"
    narration.build_wav(story, frames - 9, track)
    problems, _ = narration.placement_problems(track, story, frames)
    assert problems == [
        f"the narration track is {(frames - 9) * 1600} samples; the video's {frames} frames are {frames * 1600}"
    ]


# --- G13: speech where the cues say ---------------------------------------------


def speech_track(length: int, bursts):
    values = [0] * length
    for start, end in bursts:
        for i in range(start, end):
            values[i] = 5000 if i % 2 else -5000
    return values


WINDOWS = [(48_000, 144_000), (144_000, 240_000)]
EXPECTED = [(48_000 + 2_640, 48_000 + 21_839), (144_000 + 2_640, 144_000 + 21_839)]


def test_speech_where_planned_passes_g13():
    values = speech_track(300_000, [(s, e + 1) for s, e in EXPECTED])
    problems, numbers = narration.speech_problems(values, WINDOWS, EXPECTED)
    assert problems == []
    assert numbers["spans"] == 2 and numbers["worst_ms"] == 0.0


def test_a_clip_300_ms_late_fails_g13():
    late = 14_400
    values = speech_track(
        300_000,
        [
            (EXPECTED[0][0], EXPECTED[0][1] + 1),
            (EXPECTED[1][0] + late, EXPECTED[1][1] + late + 1),
        ],
    )
    problems, _ = narration.speech_problems(values, WINDOWS, EXPECTED)
    assert problems == [
        "sentence 2: speech starts at 3.355 s, planned 3.055 s (+300 ms)",
        "sentence 2: speech ends at 3.755 s, planned 3.455 s (+300 ms)",
    ]


def test_sound_over_a_card_fails_g13():
    values = speech_track(300_000, [(s, e + 1) for s, e in EXPECTED] + [(1_000, 2_000)])
    problems, _ = narration.speech_problems(values, WINDOWS, EXPECTED)
    assert problems == [
        "sound at 0.021 s reaches -40 dBFS outside every sentence's cue"
    ]


def test_a_clip_cut_off_fails_g13():
    values = speech_track(
        300_000,
        [
            (EXPECTED[0][0], EXPECTED[0][1] + 1),
            (EXPECTED[1][0], EXPECTED[1][0] + 4_800),
        ],
    )
    problems, _ = narration.speech_problems(values, WINDOWS, EXPECTED)
    assert problems == ["sentence 2: speech ends at 3.155 s, planned 3.455 s (-300 ms)"]


def test_a_sentence_with_no_speech_fails_g13():
    values = speech_track(300_000, [(EXPECTED[0][0], EXPECTED[0][1] + 1)])
    problems, numbers = narration.speech_problems(values, WINDOWS, EXPECTED)
    assert problems == [
        "sentence 2: no speech inside its cue",
        "1 sentences have speech; the narration has 2",
    ]
    assert numbers["spans"] == 1


def test_a_clips_own_speech_is_measured_with_the_gain(tmp_path: Path):
    path = tmp_path / "clip.wav"
    write_clip(path, clip_bytes(1))
    first, last = narration.clip_speech(path, contract.DEFAULT_VOICE_GAIN_DB)
    assert CLIP_LEAD <= first < CLIP_LEAD + 20
    assert CLIP_LEAD + CLIP_TONE - 20 < last < CLIP_LEAD + CLIP_TONE


# --- G16: one AAC stream, from the data; the video untouched --------------------


def ffprobe_fixture(name: str):
    return json.loads((FIXTURES / "ffprobe" / name).read_text(encoding="utf-8"))


def test_the_expected_sound_comes_from_the_capture(voiced: Path, tmp_path: Path):
    assert probe.expected_audio_streams(contract.validate_capture_dir(voiced)) == 1
    silent = contract.validate_capture_dir(make_capture(tmp_path / "silent"))
    assert probe.expected_audio_streams(silent) == 0


def test_the_real_narrated_mp4_passes_g16():
    info = ffprobe_fixture("narrated.json")
    frames = int(info["streams"][0]["nb_read_frames"])
    problems, numbers = probe.format_problems(info, frames, audio_streams=1)
    assert problems == []
    assert numbers["audio"]["codec"] == "aac" and numbers["audio"]["channels"] == 1


def test_the_real_silent_mp4_passes_with_no_sound_expected_and_fails_with_one():
    info = ffprobe_fixture("silent.json")
    frames = int(info["streams"][0]["nb_read_frames"])
    assert probe.format_problems(info, frames, audio_streams=0)[0] == []
    assert probe.format_problems(info, frames, audio_streams=1)[0] == [
        "0 audio streams; expected 1 (the narration)"
    ]


def test_a_narrated_mp4_where_none_is_expected_fails():
    info = ffprobe_fixture("narrated.json")
    frames = int(info["streams"][0]["nb_read_frames"])
    assert probe.format_problems(info, frames, audio_streams=0)[0] == [
        "1 audio streams; expected 0 (no audio)"
    ]


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda a: a.update(sample_rate="44100"), "sound at 44100 Hz, expected 48000"),
        (lambda a: a.update(channels=2), "sound in 2 channels, expected 1 (mono)"),
        (lambda a: a.update(profile="HE-AAC"), "sound aac HE-AAC, expected aac LC"),
        (
            lambda a: a.update(start_time="0.300000"),
            "sound starts at 0.300000 s, expected 0",
        ),
        (
            lambda a: a.update(duration=str(float(a["duration"]) - 0.3)),
            "the sound is ",
        ),
    ],
)
def test_g16_refuses_a_planted_sound_defect(change, message):
    info = ffprobe_fixture("narrated.json")
    frames = int(info["streams"][0]["nb_read_frames"])
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    change(audio)
    problems = probe.format_problems(info, frames, audio_streams=1)[0]
    assert len(problems) == 1 and problems[0].startswith(message)


def test_two_sound_streams_fail_g16():
    info = ffprobe_fixture("narrated.json")
    frames = int(info["streams"][0]["nb_read_frames"])
    info["streams"].append(copy.deepcopy(info["streams"][1]))
    assert probe.format_problems(info, frames, audio_streams=1)[0] == [
        "2 audio streams; expected 1 (the narration)"
    ]


#: Each registered video's gain (T216) as the mux's filter, the review page's
#: words and G17's record write it: the 4.5 dB every video had before T216,
#: except video 9's 3.5 dB.
BEFORE_T216 = ("volume=4.5dB:precision=float", "4.5 dB gain", 4.5)
VIDEO_9 = ("volume=3.5dB:precision=float", "3.5 dB gain", 3.5)
GAINS = [
    (slug, *(VIDEO_9 if slug == "09-design-in-depth" else BEFORE_T216))
    for slug in config.VIDEOS
]
GAIN_IDS = list(config.VIDEOS)


@pytest.mark.parametrize("slug, volume, _words, _recorded", GAINS, ids=GAIN_IDS)
def test_the_mux_copies_the_video_and_applies_the_videos_gain_in_floating_point(
    slug, volume, _words, _recorded
):
    argv = sound.mux_argv(
        Path("s.mp4"), Path("n.wav"), Path("o.mp4"), contract.voice_gain_db(slug)
    )
    assert argv == [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-n",
        "-i",
        "s.mp4",
        "-i",
        "n.wav",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-af",
        volume,
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "48000",
        "-ac",
        "1",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        "o.mp4",
    ]


def test_the_video_md5_is_read_from_ffmpegs_one_md5_line():
    assert (
        sound.parse_md5("MD5=0123456789abcdef0123456789abcdef\n")
        == "0123456789abcdef0123456789abcdef"
    )
    with pytest.raises(ValueError):
        sound.parse_md5("")


# --- G17: loudness of the finished file -----------------------------------------


def test_the_real_narrated_mp4s_loudness_is_read_from_its_summary():
    commands = json.loads((FIXTURES / "ebur128" / "commands.json").read_text())
    expected = commands["narrated-mp4.txt"]
    text = (FIXTURES / "ebur128" / "narrated-mp4.txt").read_text(encoding="utf-8")
    assert contract.parse_ebur128(text) == (
        expected["integrated_lufs"],
        expected["true_peak_dbtp"],
    )
    assert expected["measure"] == " ".join(
        ["ffmpeg", *sound.loudness_argv(Path("narrated.mp4"))[1:]]
    )
    with pytest.raises(ValueError, match="no loudness summary"):
        contract.parse_ebur128("")


@pytest.mark.regression
def test_a_sentences_loudness_is_read_on_the_files_own_timeline():
    """The window was trimmed from a decode that moves a late stream to 0, so a
    sentence's level was read where its speech should be (#1252 review)."""
    argv = sound.loudness_argv(Path("narrated.mp4"), (144_000, 192_000))
    chain = argv[argv.index("-af") + 1].split(",")
    assert chain == [
        "aresample=async=1:first_pts=0",
        "atrim=start_sample=144000:end_sample=192000",
        "ebur128=peak=true",
    ]
    assert chain[0] == sound.TIMELINE
    # The whole file's reading is unchanged: the recorded fixture's command.
    whole = sound.loudness_argv(Path("narrated.mp4"))
    assert whole[whole.index("-af") + 1] == "ebur128=peak=true"


def test_loudness_on_target_passes_g17():
    problems, numbers = narration.loudness_problems(
        -19.3, -2.1, [-19.0, -19.6, -18.4], contract.DEFAULT_VOICE_GAIN_DB
    )
    assert problems == []
    assert numbers["sentence_median_lufs"] == -19.0


@pytest.mark.parametrize(
    "integrated, peak, levels, message",
    [
        (-19.3, -0.8, [-19.0], "true peak -0.8 dBTP; at most -1"),
        (-17.7, -2.0, [-19.0], "integrated loudness -17.7 LUFS; the target is -19 +-1"),
        (-20.2, -2.0, [-19.0], "integrated loudness -20.2 LUFS; the target is -19 +-1"),
        (
            -19.0,
            -2.0,
            [-19.0, -19.2, -16.8],
            "sentence 3 is -16.8 LUFS, +2.2 from the median -19.0; at most +-2",
        ),
    ],
)
def test_g17_refuses_loudness_off_target(integrated, peak, levels, message):
    assert narration.loudness_problems(
        integrated, peak, levels, contract.DEFAULT_VOICE_GAIN_DB
    )[0] == [message]


# --- G19: the published file names itself ----------------------------------------


def test_the_name_and_the_review_page_come_from_the_published_file(tmp_path: Path):
    voiced_mp4, silent_mp4 = tmp_path / "voiced.mp4", tmp_path / "silent.mp4"
    voiced_mp4.write_bytes(b"narrated")
    silent_mp4.write_bytes(b"silent")
    digest = pipeline.sha256(voiced_mp4)
    names = contract.output_names("01-getting-started", digest)
    assert pipeline.identity_problems(voiced_mp4, names, f"<td>{digest}</td>") == []
    wrong = contract.output_names("01-getting-started", pipeline.sha256(silent_mp4))
    assert pipeline.identity_problems(voiced_mp4, wrong, f"<td>{digest}</td>") == [
        f"the video is named {wrong['mp4']}, but its sha256 starts {digest[:12]}"
    ]
    assert pipeline.identity_problems(voiced_mp4, names, "<td></td>") == [
        f"the review page does not give the video's sha256 {digest}"
    ]


def test_output_names_include_the_srt_and_the_transcript():
    names = contract.output_names("01-getting-started", "ab" * 32)
    assert names["srt"] == "01-getting-started-abababababab.srt"
    assert names["transcript"] == "01-getting-started-abababababab.txt"


# --- The review page --------------------------------------------------------------


def page(root: Path, narrated: bool) -> str:
    capture = contract.validate_capture_dir(root)
    names = contract.output_names(capture.sheet.slug, "ab" * 32)
    rows = (
        review.narration_rows(capture.narration, timeline.output_cues(capture.sheet))
        if narrated
        else None
    )
    return review.review_html(
        sheet=capture.sheet,
        manifest=capture.manifest,
        names=names,
        mp4_sha256="ab" * 32,
        vtt_sha256="cd" * 32,
        contact=[],
        render_gates={},
        gain_db=contract.voice_gain_db(capture.sheet.slug),
        narration=rows,
        srt_sha256="ef" * 32 if narrated else None,
        transcript_sha256="12" * 32 if narrated else None,
    )


def test_the_review_page_shows_headline_and_narration_side_by_side(tmp_path: Path):
    say = [list(s) for s in VOICED_SAY]
    say[1] = ["Install the SDK."]
    html = page(make_voiced_capture(tmp_path / "c", say=say), True)
    assert (
        "<td>Open the dashboard in your browser.</td><td>Install the SDK.</td>" in html
    )
    assert "Amazon Polly, generative engine, Ruth (en-US)" in html
    assert "ef" * 32 in html and "12" * 32 in html
    assert "01-getting-started-abababababab.srt" in html
    for item in review.NARRATED_CHECKLIST:
        assert html_escape(item) in html
    assert "<td>SDK</td><td>S.D.K.</td><td>3</td>" in html
    assert 'data-start="5.267"' in html and "Play sentence 15" in html
    assert "<script>" in html


def test_the_silent_review_page_is_unchanged(tmp_path: Path):
    html = page(make_capture(tmp_path / "c"), False)
    assert "Script (" not in html and "<script>" not in html and "Said as" not in html
    assert "none (silent)" in html
    for item in review.CHECKLIST:
        assert html_escape(item) in html


def test_the_sound_is_decoded_on_the_files_own_timeline():
    """G13: a stream that starts late is padded from 0, not moved to 0 (measured on
    the Mac: without this filter, ``-itsoffset 0.3`` in the mux passed G13)."""
    argv = sound.decode_argv(Path("narrated.mp4"))
    assert argv[argv.index("-af") + 1] == "aresample=async=1:first_pts=0"
    assert argv[-3:] == ["-f", "s16le", "-"]


def test_sound_outside_every_clip_fails_g15(voiced: Path, tmp_path: Path):
    story = spoken(voiced)
    frames = total(voiced)
    track = tmp_path / "narration.wav"
    narration.build_wav(story, frames, track)
    with wave.open(str(track), "rb") as wav:
        data = bytearray(wav.readframes(wav.getnframes()))
    data[20:22] = (900).to_bytes(2, "little", signed=True)
    write_clip(track, bytes(data))
    problems, _ = narration.placement_problems(track, story, frames)
    assert problems == [
        "the narration track has sound at sample 10, outside every clip's place"
    ]


# --- T216: each video's gain, read once ---------------------------------------


@pytest.mark.parametrize("slug, _volume, words, recorded", GAINS, ids=GAIN_IDS)
def test_the_review_page_and_g17_state_the_videos_gain(
    tmp_path: Path, slug, _volume, words, recorded
):
    root = make_voiced_capture(tmp_path / "c")
    edit_json(root / "cues.json", lambda sheet: sheet.update(slug=slug))
    assert f"after synthesis; {words}; AAC, 48 kHz, mono" in page(root, True)
    _problems, numbers = narration.loudness_problems(
        -19.0, -3.0, [-19.0, -19.2], contract.voice_gain_db(slug)
    )
    assert numbers["gain_db"] == recorded


@pytest.mark.parametrize(
    "consumer",
    [
        contract.loudness_problems,
        sound.gain_filter,
        sound.mux_argv,
        narration.clip_speech,
        narration.loudness_problems,
        review.review_html,
        pipeline._narrate,
    ],
    ids=lambda function: f"{function.__module__}.{function.__name__}",
)
def test_no_gain_consumer_falls_back_to_a_gain_of_its_own(consumer):
    """A call that leaves the gain out fails, rather than taking 4.5 dB."""
    gain = inspect.signature(consumer).parameters["gain_db"]
    assert gain.default is inspect.Parameter.empty


def gain_given(call: ast.Call) -> str:
    keyword = [k.value for k in call.keywords if k.arg == "gain_db"]
    return ast.unparse(keyword[0] if keyword else call.args[-1])


def test_the_render_reads_the_videos_gain_once_and_every_consumer_gets_that_value():
    """The partial wiring the PE named (T216): G10 at 3.5 dB and a mux at 4.5 dB
    would land video 9 about 1 dB hot. The render has no test that runs ffmpeg
    here, so its wiring is read from its source."""
    tree = ast.parse((SHOWCASE / "render" / "pipeline.py").read_text(encoding="utf-8"))
    stores = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id == "gain_db"
        and isinstance(node.ctx, ast.Store)
    ]
    assigned = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and any(t in stores for t in node.targets)
    ]
    assert len(stores) == 1
    assert assigned == ["gain_db = contract.voice_gain_db(sheet.slug)"]
    consumers = {
        "_narrate",
        "narration.clip_speech",
        "narration.loudness_problems",
        "review.review_html",
        "sound.mux_argv",
    }
    given = sorted(
        (ast.unparse(node.func), gain_given(node))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func) in consumers
    )
    assert given == [(name, "gain_db") for name in sorted(consumers)]

    lookups, reads = [], []
    for path in sorted(SHOWCASE.rglob("*.py")):
        where = path.relative_to(SHOWCASE).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and ast.unparse(node.func).endswith(
                "voice_gain_db"
            ):
                lookups.append((where, ast.unparse(node)))
            name = getattr(node, "attr", None) or getattr(node, "id", None)
            if where != "contract.py" and name in (
                "DEFAULT_VOICE_GAIN_DB",
                "VOICE_GAIN_DB_BY_VIDEO",
            ):
                reads.append((where, node.lineno))
    assert lookups == [
        ("render/pipeline.py", "contract.voice_gain_db(sheet.slug)"),
        ("voice/step.py", "contract.voice_gain_db(slug)"),
    ]
    assert reads == []
