"""A narrated video's text and sound, as pure functions (voice PR C1).

A narrated capture (``contract.read_voice``) keeps the band's short headline
per cue and adds the sentences spoken over it. The render turns them into:

* the WebVTT and SRT cues: one per sentence, at the sentence's own frames
  (``sentence_cues``), which tile the band cues (gate 4, ``tile_problems``);
* the transcript: the sentences, one paragraph per band cue
  (``transcript_text``; G6, ``transcript_problems``);
* the narration track: each clip copied, sample for sample, to its sentence's
  first frame at ``contract.SAMPLES_PER_FRAME`` samples a frame, and zero
  everywhere else (``build_wav``; G15, ``placement_problems``);
* the speech check on the finished file's decoded sound (G13,
  ``speech_problems``) and the loudness verdict (G17, ``loudness_problems``).

Everything here is the standard library, so the CI unit job runs it; the
ffmpeg calls that feed it are in ``sound``.
"""

from __future__ import annotations

import re
import statistics
import sys
import wave
from array import array
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from showcase import contract
from showcase.render.timeline import OutputCue
from showcase.render.vtt import VttCue, VttError, timestamp

SPF = contract.SAMPLES_PER_FRAME

#: G13: speech is any sample at or above -40 dBFS.
SPEECH_DBFS = -40.0
SPEECH_LEVEL = 32768 * 10 ** (SPEECH_DBFS / 20)
#: G13: a sentence's speech starts and ends within one frame (33 ms) of where
#: its clip, placed on its cue's first frame, puts it.
SYNC_TOLERANCE_SAMPLES = contract.VOICE_RATE * 33 // 1000
#: Samples scanned at a time when looking for speech.
_CHUNK = 480


# --- Cues -------------------------------------------------------------------


def sentence_cues(narration: contract.Narration) -> List[OutputCue]:
    """The WebVTT and SRT cues of a narrated video, in output frames."""
    return [
        OutputCue(
            number=s.number,
            start_frame=contract.TITLE_FRAMES + s.start_frame,
            end_frame=contract.TITLE_FRAMES + s.end_frame,
            lines=s.lines,
        )
        for s in narration.sentences
    ]


def _frame(ms: int) -> int:
    return contract.frames_for_ms(ms)


def tile_problems(
    parsed: Sequence[VttCue],
    band: Sequence[OutputCue],
    narration: contract.Narration,
) -> List[str]:
    """Gate 4's TILE rule on cues as read back from a file.

    Each band cue's sentences start on its first frame, follow one another
    with no gap, end on its last frame, and each but the last lasts exactly
    its slot (``contract.slot_frames``).
    """
    if len(parsed) != len(narration.sentences):
        return [
            f"{len(parsed)} cues read back, the narration has {len(narration.sentences)}"
        ]
    problems: List[str] = []
    for number, cue in enumerate(band, start=1):
        group = [
            (s, parsed[s.number - 1]) for s in narration.sentences if s.cue == number
        ]
        at = cue.start_frame
        for k, (sentence, got) in enumerate(group, start=1):
            start, end = _frame(got.start_ms), _frame(got.end_ms)
            if start != at:
                problems.append(
                    f"sentence {sentence.number} starts on frame {start}; cue {number}"
                    f"'s {'first frame' if k == 1 else 'previous sentence ends on'}"
                    f" is {at}"
                )
            if k < len(group):
                slot = contract.slot_frames(sentence.text, sentence.samples)
                if end - start != slot:
                    problems.append(
                        f"sentence {sentence.number} lasts {end - start} frames; its"
                        f" slot is {slot}"
                    )
            elif end != cue.end_frame:
                problems.append(
                    f"sentence {sentence.number} ends on frame {end}; cue {number}"
                    f" ends on {cue.end_frame}"
                )
            at = end
    return problems


# --- SRT --------------------------------------------------------------------

_SRT_TIMING = re.compile(
    r"^(?P<h1>[0-9]{2}):(?P<m1>[0-5][0-9]):(?P<s1>[0-5][0-9]),(?P<ms1>[0-9]{3})"
    r" --> "
    r"(?P<h2>[0-9]{2}):(?P<m2>[0-5][0-9]):(?P<s2>[0-5][0-9]),(?P<ms2>[0-9]{3})$"
)


def srt_timestamp(ms: int) -> str:
    return timestamp(ms).replace(".", ",")


def write_srt(cues: Sequence[OutputCue]) -> str:
    """SubRip: the same cues as the WebVTT file, numbered from 1."""
    blocks = [
        "\n".join(
            [
                str(cue.number),
                f"{srt_timestamp(cue.start_ms)} --> {srt_timestamp(cue.end_ms)}",
                *cue.lines,
            ]
        )
        for cue in cues
    ]
    return "\n\n".join(blocks) + "\n"


def _ms(match: re.Match, suffix: str) -> int:
    return (
        int(match[f"h{suffix}"]) * 3_600_000
        + int(match[f"m{suffix}"]) * 60_000
        + int(match[f"s{suffix}"]) * 1000
        + int(match[f"ms{suffix}"])
    )


def parse_srt(text: str) -> List[VttCue]:
    """Parse the subset of SubRip the render writes; anything else is an error."""
    if text.startswith("﻿"):
        raise VttError("the SRT file starts with a byte-order mark")
    if not text.endswith("\n") or text.endswith("\n\n"):
        raise VttError("the SRT file must end with exactly one line break")
    if "\r" in text:
        raise VttError("the SRT file has carriage returns")
    cues: List[VttCue] = []
    for number, block in enumerate(text[:-1].split("\n\n"), start=1):
        lines = block.split("\n")
        if len(lines) < 3 or any(not line.strip() for line in lines):
            raise VttError(
                f"SRT block {number} is not a number, a timing line and text"
            )
        if lines[0] != str(number):
            raise VttError(f"SRT block {number} is numbered {lines[0]!r}")
        match = _SRT_TIMING.match(lines[1])
        if not match:
            raise VttError(
                f"SRT cue {number}: timing line {lines[1]!r} is not"
                " 'HH:MM:SS,mmm --> HH:MM:SS,mmm'"
            )
        if any("-->" in line for line in lines[2:]):
            raise VttError(f"SRT cue {number}: its text holds '-->'")
        cues.append(
            VttCue(
                identifier=lines[0],
                start_ms=_ms(match, "1"),
                end_ms=_ms(match, "2"),
                lines=tuple(lines[2:]),
            )
        )
    return cues


def srt_problems(srt: Sequence[VttCue], vtt: Sequence[VttCue]) -> List[str]:
    """The SRT file says exactly what the WebVTT file says: cues, times and lines."""
    problems: List[str] = []
    if len(srt) != len(vtt):
        problems.append(f"the SRT file has {len(srt)} cues, the WebVTT file {len(vtt)}")
    for number, (got, want) in enumerate(zip(srt, vtt), start=1):
        if (got.start_ms, got.end_ms) != (want.start_ms, want.end_ms):
            problems.append(
                f"SRT cue {number} is at {got.start_ms}-{got.end_ms} ms; the WebVTT"
                f" cue is at {want.start_ms}-{want.end_ms} ms"
            )
        if got.lines != want.lines:
            problems.append(f"SRT cue {number}'s text differs from the WebVTT cue's")
    return problems


# --- The transcript (G6) ----------------------------------------------------


def transcript_text(narration: contract.Narration) -> str:
    """The narration as plain text: one paragraph per band cue, in order."""
    cues = sorted({s.cue for s in narration.sentences})
    paragraphs = [" ".join(s.text for s in narration.for_cue(cue)) for cue in cues]
    return "\n\n".join(paragraphs) + "\n"


def _collapse(text: str) -> str:
    return " ".join(text.split())


def transcript_problems(
    text: str, vtt: Sequence[VttCue], narration: contract.Narration
) -> List[str]:
    """G6: the transcript, as written, is the WebVTT file's sentences in order,
    one paragraph per band cue."""
    if not text.endswith("\n") or text.endswith("\n\n") or "\r" in text:
        return [
            "the transcript must end with exactly one line break, and hold no carriage return"
        ]
    paragraphs = text[:-1].split("\n\n")
    cues = sorted({s.cue for s in narration.sentences})
    if len(vtt) != len(narration.sentences):
        return [
            f"the WebVTT file has {len(vtt)} cues, the narration {len(narration.sentences)}"
        ]
    if len(paragraphs) != len(cues):
        return [
            f"the transcript has {len(paragraphs)} paragraphs; the video has {len(cues)} cues"
        ]
    problems: List[str] = []
    for paragraph, cue in zip(paragraphs, cues):
        said = " ".join(
            " ".join(vtt[s.number - 1].lines) for s in narration.for_cue(cue)
        )
        if _collapse(paragraph) != _collapse(said):
            problems.append(
                f"the transcript's paragraph {cue} is not what the WebVTT file says over cue {cue}"
            )
    return problems


# --- The narration track (G15) ----------------------------------------------


def offsets(narration: contract.Narration) -> List[int]:
    """Each sentence's first sample in the finished video: its first output frame
    times ``SAMPLES_PER_FRAME`` (the title card is silent)."""
    return [(contract.TITLE_FRAMES + s.start_frame) * SPF for s in narration.sentences]


def _clip_bytes(path: Path) -> bytes:
    with wave.open(str(path), "rb") as clip:
        return clip.readframes(clip.getnframes())


def build_wav(
    narration: contract.Narration,
    total_frames: int,
    out: Path,
    *,
    places: Optional[Sequence[int]] = None,
) -> List[int]:
    """Write the narration track to *out*: 48 kHz, mono, 16-bit, exactly
    ``total_frames`` frames long; each clip at its offset, zero elsewhere.

    The gain is not applied here: ffmpeg applies it when it encodes, in
    floating point (``sound.mux_argv``). Returns the offsets used.
    """
    at = list(offsets(narration) if places is None else places)
    track = bytearray(total_frames * SPF * 2)
    for sentence, start in zip(narration.sentences, at):
        data = _clip_bytes(narration.clip(sentence))
        if start < 0 or start * 2 + len(data) > len(track):
            raise ValueError(
                f"sentence {sentence.number}'s clip does not fit in the {total_frames}-frame video"
            )
        track[start * 2 : start * 2 + len(data)] = data
    with wave.open(str(out), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(contract.VOICE_RATE)
        wav.writeframes(bytes(track))
    return at


def placement_problems(
    wav_path: Path, narration: contract.Narration, total_frames: int
) -> Tuple[List[str], Dict[str, Any]]:
    """G15: the track as written holds each clip byte for byte at its sentence's
    first frame, and nothing else."""
    with wave.open(str(wav_path), "rb") as wav:
        shape = (wav.getframerate(), wav.getnchannels(), wav.getsampwidth())
        data = wav.readframes(wav.getnframes())
    problems: List[str] = []
    numbers: Dict[str, Any] = {
        "samples": len(data) // 2,
        "expected_samples": total_frames * SPF,
        "clips": len(narration.sentences),
    }
    if shape != (contract.VOICE_RATE, 1, 2):
        return [f"the narration track is {shape}, not 48000 Hz mono 16-bit"], numbers
    if len(data) != total_frames * SPF * 2:
        problems.append(
            f"the narration track is {len(data) // 2} samples; the video's {total_frames}"
            f" frames are {total_frames * SPF}"
        )
    rest = bytearray(data)
    for sentence, start in zip(narration.sentences, offsets(narration)):
        clip = _clip_bytes(narration.clip(sentence))
        if data[start * 2 : start * 2 + len(clip)] != clip:
            problems.append(
                f"sentence {sentence.number}'s clip is not at sample {start}"
                f" (frame {start // SPF})"
            )
        rest[start * 2 : start * 2 + len(clip)] = bytes(len(clip))
    stray = re.search(rb"[^\x00]", rest)
    if stray is not None:
        problems.append(
            f"the narration track has sound at sample {stray.start() // 2}, outside"
            " every clip's place"
        )
    return problems, numbers


# --- Speech in the finished file (G13) ---------------------------------------


def samples_from_s16le(data: bytes) -> array:
    values = array("h")
    values.frombytes(data[: len(data) // 2 * 2])
    if sys.byteorder != "little":
        values.byteswap()
    return values


def _first_loud(
    values: Sequence[int], start: int, end: int, level: float
) -> Optional[int]:
    at = start
    while at < end:
        stop = min(at + _CHUNK, end)
        chunk = values[at:stop]
        if max(chunk) >= level or -min(chunk) >= level:
            for i in range(at, stop):
                if abs(values[i]) >= level:
                    return i
        at = stop
    return None


def _last_loud(
    values: Sequence[int], start: int, end: int, level: float
) -> Optional[int]:
    at = end
    while at > start:
        begin = max(at - _CHUNK, start)
        chunk = values[begin:at]
        if max(chunk) >= level or -min(chunk) >= level:
            for i in range(at - 1, begin - 1, -1):
                if abs(values[i]) >= level:
                    return i
        at = begin
    return None


def clip_speech(path: Path, gain_db: float = contract.VOICE_GAIN_DB) -> Tuple[int, int]:
    """Where a clip's own speech starts and ends, in samples, with the render's gain."""
    values = samples_from_s16le(_clip_bytes(path))
    level = SPEECH_LEVEL / 10 ** (gain_db / 20)
    first = _first_loud(values, 0, len(values), level)
    last = _last_loud(values, 0, len(values), level)
    if first is None or last is None:
        raise ValueError(f"{path.name} holds no speech above {SPEECH_DBFS:g} dBFS")
    return first, last


def speech_problems(
    values: Sequence[int],
    windows: Sequence[Tuple[int, int]],
    expected: Sequence[Tuple[int, int]],
) -> Tuple[List[str], Dict[str, Any]]:
    """G13 on the finished file's decoded sound.

    *windows* are the sentences' cues as read back from the WebVTT file, in
    samples; *expected* where each sentence's speech should start and end
    (its cue's first sample plus its clip's own onset and end). Each window
    must hold speech starting and ending within ``SYNC_TOLERANCE_SAMPLES`` of
    that, and nothing outside the windows (the cards included) may reach
    ``SPEECH_DBFS``.
    """
    problems: List[str] = []
    found = 0
    worst = 0
    rate = contract.VOICE_RATE
    for number, ((start, end), (onset, offset)) in enumerate(
        zip(windows, expected), start=1
    ):
        first = _first_loud(values, start, min(end, len(values)), SPEECH_LEVEL)
        last = _last_loud(values, start, min(end, len(values)), SPEECH_LEVEL)
        if first is None or last is None:
            problems.append(f"sentence {number}: no speech inside its cue")
            continue
        found += 1
        for what, got, want in (("starts", first, onset), ("ends", last, offset)):
            worst = max(worst, abs(got - want))
            if abs(got - want) > SYNC_TOLERANCE_SAMPLES:
                problems.append(
                    f"sentence {number}: speech {what} at {got / rate:.3f} s, planned"
                    f" {want / rate:.3f} s ({(got - want) * 1000 / rate:+.0f} ms)"
                )
    edges = [0]
    for start, end in windows:
        edges += [start, end]
    edges.append(len(values))
    for begin, stop in zip(edges[0::2], edges[1::2]):
        loud = _first_loud(values, begin, min(stop, len(values)), SPEECH_LEVEL)
        if loud is not None:
            problems.append(
                f"sound at {loud / rate:.3f} s reaches {SPEECH_DBFS:g} dBFS outside"
                " every sentence's cue"
            )
            break
    if found != len(windows):
        problems.append(
            f"{found} sentences have speech; the narration has {len(windows)}"
        )
    numbers = {
        "sentences": len(windows),
        "spans": found,
        "worst_ms": round(worst * 1000 / rate, 1),
        "tolerance_ms": round(SYNC_TOLERANCE_SAMPLES * 1000 / rate, 1),
        "level_dbfs": SPEECH_DBFS,
    }
    return problems, numbers


def windows_from(parsed: Sequence[VttCue]) -> List[Tuple[int, int]]:
    """The sentences' cues, as read back from the WebVTT file, in samples."""
    return [(_frame(c.start_ms) * SPF, _frame(c.end_ms) * SPF) for c in parsed]


# --- Loudness of the finished file (G17) --------------------------------------


def loudness_problems(
    integrated: float, peak: float, sentences: Sequence[float]
) -> Tuple[List[str], Dict[str, Any]]:
    """G17: integrated loudness, true peak, and each sentence against the median."""
    problems: List[str] = []
    target, tolerance = contract.LOUDNESS_TARGET_LUFS, contract.FINAL_LOUDNESS_LU
    if abs(round(integrated - target, 3)) > tolerance:
        problems.append(
            f"integrated loudness {integrated:.1f} LUFS; the target is {target:g} +-{tolerance:g}"
        )
    if peak > contract.FINAL_PEAK_DBTP:
        problems.append(
            f"true peak {peak:.1f} dBTP; at most {contract.FINAL_PEAK_DBTP:g}"
        )
    median = statistics.median(sentences) if sentences else float("nan")
    for number, level in enumerate(sentences, start=1):
        if abs(round(level - median, 3)) > contract.SENTENCE_SPREAD_LU:
            problems.append(
                f"sentence {number} is {level:.1f} LUFS, {level - median:+.1f} from the"
                f" median {median:.1f}; at most +-{contract.SENTENCE_SPREAD_LU:g}"
            )
    numbers = {
        "integrated_lufs": integrated,
        "true_peak_dbtp": peak,
        "sentence_median_lufs": round(median, 2),
        "sentence_range_lufs": [min(sentences), max(sentences)] if sentences else [],
        "gain_db": contract.VOICE_GAIN_DB,
    }
    return problems, numbers
