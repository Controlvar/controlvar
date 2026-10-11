"""The capture directory: what the showcase capture writes and the render reads.

Both halves of the showcase tool import this module, so a number that both
must agree on (the caption band's height, the frame rate, the card lengths) is
written once, here. ``validate_capture_dir`` is the one reading of a capture
directory: the capture can run it on its own output before handing over, and
the render runs it before anything else and refuses a directory it rejects.

A capture directory ``<work>/<slug>/capture/`` holds:

``frames/NNNNNN.jpg``
    The page area of each screencast frame, ``PAGE_W`` x ``PAGE_H`` (JPEG,
    quality 95). Frames arrive only when the page changes.
``frames.jsonl``
    One ``{"file": "000001.jpg", "t_ms": 0}`` per line, ``t_ms`` the
    milliseconds since the recording started, strictly increasing. The first
    frame comes at most ``FIRST_FRAME_MAX_MS`` after the start; the render
    shows it from the start.
``cues.json``
    ``{"slug", "title", "version", "duration_ms", "cues": [{"start_ms",
    "end_ms", "text", "focus"}]}``. Times are relative to the recording's
    start; ``focus`` is ``{"x", "y", "w", "h"}`` in frame pixels, or null. The
    title and end cards are not in the frames: the render adds them with
    ``TITLE_CARD_MS`` and ``END_CARD_MS`` and shifts the WebVTT times to match.
``needles.txt``
    Every value the capture registered as one the video must never show, one
    per line, file mode 0600. The render reads it for its OCR scan of the
    finished video and deletes it on every way out, pass or refuse. It is
    never copied, printed or written anywhere else.
``manifest.json``
    ``{"ref", "commit", "next_version", "lock_next_version", "profile",
    "viewport", "zoom", "page_h", "band_h", "gates"}``. ``gates`` maps each
    capture gate to ``{"ok": bool, "numbers": {...}}``; the render refuses a
    capture with a gate that is not ok, or without one of
    ``REQUIRED_CAPTURE_GATES``, and copies the numbers onto the review page.

Every check reports all the problems it finds, in one ``CaptureRefused``.
This module uses the standard library only, so the unit tests that pin it run
in the CI unit job, which installs neither Playwright nor ffmpeg.
"""

from __future__ import annotations

import json
import math
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# --- The frame -----------------------------------------------------------

#: The output video and the page area are this wide.
PAGE_W = 1920
OUT_W = PAGE_W
OUT_H = 1080
#: The caption band under the page area. Captions never cover the page.
BAND_H = 140
#: The page area's height: the screencast's viewport and every frame.
PAGE_H = OUT_H - BAND_H
FPS = 30

# --- The video's length --------------------------------------------------

TITLE_CARD_MS = 3000
END_CARD_MS = 4000
#: The finished video, cards included, is 55 to 75 seconds long (inclusive).
MIN_VIDEO_MS = 55_000
MAX_VIDEO_MS = 75_000

# --- Captions --------------------------------------------------------------

#: At most two lines of 42 characters, and 12 words.
MAX_CUE_WORDS = 12
MAX_LINE_CHARS = 42
MAX_CUE_LINES = 2
#: A cue stays up for at least max(2.0 s, 0.5 s + words / 3, characters / 15).
MIN_CUE_MS = 2000
CUE_BASE_MS = 500
WORDS_PER_SECOND = 3
CHARS_PER_SECOND = 15
#: No stretch of the recording longer than this goes without a caption: from
#: the start to the first cue, between cues, and from the last cue to the end.
MAX_CUE_GAP_MS = 1000
FIRST_FRAME_MAX_MS = 100

# --- Values the video must never show ----------------------------------------

NEEDLES_MODE = 0o600
#: A needle is at least this many characters once ``normalise`` has dropped
#: its case and separators: a shorter value cannot be told apart from
#: ordinary page text by OCR (and ``--------`` normalises to nothing at all).
MIN_NEEDLE_CHARS = 8

#: What OCR confuses, folded together: 0/O/@, 1/l/I/|, f/t, 2/Z, 5/S, 8/B.
_CONFUSABLE = str.maketrans(
    {
        "0": "o",
        "O": "o",
        "@": "o",
        "1": "l",
        "I": "l",
        "|": "l",
        "f": "t",
        "F": "t",
        "T": "t",
        "2": "z",
        "Z": "z",
        "5": "s",
        "S": "s",
        "8": "b",
        "B": "b",
    }
)
_DROPPED = re.compile(r"[\s_\-:]+")
#: Cyrillic and Greek letters OCR returns for their Latin look-alikes
#: (measured: Vision read the digit 6 of a key as Cyrillic be).
_HOMOGLYPHS = str.maketrans(
    {
        "\u0430": "a",
        "\u0410": "A",
        "\u0432": "b",
        "\u0412": "B",
        "\u0431": "6",
        "\u0435": "e",
        "\u0415": "E",
        "\u043a": "k",
        "\u041a": "K",
        "\u043c": "m",
        "\u041c": "M",
        "\u043d": "h",
        "\u041d": "H",
        "\u043e": "o",
        "\u041e": "O",
        "\u0440": "p",
        "\u0420": "P",
        "\u0441": "c",
        "\u0421": "C",
        "\u0442": "t",
        "\u0422": "T",
        "\u0443": "y",
        "\u0423": "Y",
        "\u0445": "x",
        "\u0425": "X",
        "\u0455": "s",
        "\u0405": "S",
        "\u0456": "i",
        "\u0406": "I",
        "\u0458": "j",
        "\u0408": "J",
        "\u0437": "3",
        "\u0417": "3",
        "\u04cf": "l",
        "\u03b1": "a",
        "\u03bf": "o",
        "\u039f": "O",
        "\u03c1": "p",
        "\u03a1": "P",
        "\u03c4": "t",
        "\u03ba": "k",
        "\u039a": "K",
        "\u03b9": "i",
        "\u03bd": "v",
    }
)


def fold_glyphs(text: str) -> str:
    """Latin letters for their Cyrillic and Greek look-alikes."""
    return text.translate(_HOMOGLYPHS)


def normalise(text: str) -> str:
    """Fold what OCR confuses, drop case and separators (whitespace, ``_``, ``-``, ``:``).

    The render's OCR scan compares needles and frame text in this form, so a
    needle's length is counted in it too (``MIN_NEEDLE_CHARS``).
    """
    return _DROPPED.sub("", fold_glyphs(text).translate(_CONFUSABLE).lower())


# --- The machine -------------------------------------------------------------

#: Neither half starts a video, or decodes one for OCR, below this much free
#: disk space.
DISK_FLOOR_BYTES = 4 * 1024**3

PROFILES = ("core", "full")

#: The capture gates every video reports in its manifest. The names follow the
#: approved plan (#1066): the QA gate numbers, C1 and C3 from the principal
#: engineer's conditions, U1, U2, U4 and U5 from the UX checks, and the machine
#: checks (the child environment's allow-list, the disk floor readings, and
#: the teardown that leaves no labelled container).
REQUIRED_CAPTURE_GATES = (
    "3",
    "6",
    "9",
    "10",
    "11",
    "12",
    "14",
    "15a",
    "15b",
    "15c",
    "C1",
    "C3",
    "U1",
    "U2",
    "U4",
    "U5",
    "env",
    "disk",
    "teardown",
)

FRAME_NAME = re.compile(r"^[0-9]{6}\.jpg$")
SLUG = re.compile(r"^[0-9]{2}(?:-[a-z0-9]+)+$")
VERSION = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.]+)?$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

CUES_KEYS = frozenset({"slug", "title", "version", "duration_ms", "cues"})
CUE_KEYS = frozenset({"start_ms", "end_ms", "text", "focus"})
FOCUS_KEYS = frozenset({"x", "y", "w", "h"})
FRAME_KEYS = frozenset({"file", "t_ms"})
MANIFEST_KEYS = frozenset(
    {
        "ref",
        "commit",
        "next_version",
        "lock_next_version",
        "profile",
        "viewport",
        "zoom",
        "page_h",
        "band_h",
        "gates",
    }
)
VIEWPORT = {"width": PAGE_W, "height": PAGE_H, "device_scale_factor": 1}
GATE_KEYS = frozenset({"ok", "numbers"})


class CaptureRefused(Exception):
    """A capture directory the render will not use, with every reason found."""

    def __init__(self, root: Path, problems: Sequence[str]) -> None:
        self.root = root
        self.problems = list(problems)
        lines = "\n".join(f"- {p}" for p in self.problems)
        super().__init__(f"capture directory {root} refused:\n{lines}")


@dataclass(frozen=True)
class Focus:
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class Cue:
    start_ms: int
    end_ms: int
    text: str
    focus: Optional[Focus]

    @property
    def start_frame(self) -> int:
        return frames_for_ms(self.start_ms)

    @property
    def end_frame(self) -> int:
        return frames_for_ms(self.end_ms)

    @property
    def lines(self) -> Tuple[str, ...]:
        return wrap_caption(self.text)


@dataclass(frozen=True)
class Frame:
    file: str
    t_ms: int


@dataclass(frozen=True)
class CueSheet:
    slug: str
    title: str
    version: str
    duration_ms: int
    cues: Tuple[Cue, ...]

    @property
    def capture_frames(self) -> int:
        """How many output frames the recording itself fills."""
        return frames_for_ms(self.duration_ms)


@dataclass(frozen=True)
class Manifest:
    ref: str
    commit: str
    next_version: str
    lock_next_version: str
    profile: str
    viewport: Dict[str, Any]
    zoom: float
    page_h: int
    band_h: int
    gates: Dict[str, Dict[str, Any]]
    raw: Dict[str, Any]


@dataclass(frozen=True)
class Capture:
    root: Path
    frames: Tuple[Frame, ...]
    sheet: CueSheet
    manifest: Manifest
    needle_count: int

    @property
    def frames_dir(self) -> Path:
        return self.root / "frames"

    @property
    def needles_path(self) -> Path:
        return self.root / "needles.txt"


# --- Time in frames ----------------------------------------------------------


def frames_for_ms(ms: int) -> int:
    """The output frame a time falls on, rounded to the nearest frame."""
    return (ms * FPS + 500) // 1000


def ms_for_frames(frames: int) -> int:
    """The time of an output frame, rounded to the millisecond.

    Every caption time, in the burned-in video and in the WebVTT file, is a
    frame count turned into milliseconds by this function, so the two cannot
    drift apart.
    """
    return (frames * 1000 + FPS // 2) // FPS


TITLE_FRAMES = frames_for_ms(TITLE_CARD_MS)
END_FRAMES = frames_for_ms(END_CARD_MS)


def video_frames(duration_ms: int) -> int:
    """The finished video's frame count: title card, recording, end card."""
    return TITLE_FRAMES + frames_for_ms(duration_ms) + END_FRAMES


# --- Caption text ------------------------------------------------------------


def wrap_caption(text: str) -> Tuple[str, ...]:
    """Break a caption into lines of at most ``MAX_LINE_CHARS``, word by word.

    A single word longer than a line gets a line of its own (and is then too
    long, which ``caption_problems`` reports). The burned-in caption and the
    WebVTT cue use these same lines.
    """
    lines: List[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if current and len(candidate) > MAX_LINE_CHARS:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return tuple(lines)


def reading_time_ms(text: str) -> int:
    """The shortest time a caption may stay up."""
    words = len(text.split())
    chars = len(text)
    return max(
        MIN_CUE_MS,
        CUE_BASE_MS + math.ceil(words * 1000 / WORDS_PER_SECOND),
        math.ceil(chars * 1000 / CHARS_PER_SECOND),
    )


def caption_problems(text: str) -> List[str]:
    """What is wrong with a caption's text, if anything."""
    problems: List[str] = []
    if not isinstance(text, str) or not text.strip():
        return ["is empty"]
    if text != text.strip() or "  " in text:
        problems.append("has leading, trailing or doubled spaces")
    if _CONTROL.search(text):
        problems.append("holds a line break or another control character")
    if "-->" in text:
        problems.append("holds '-->', which ends a WebVTT cue's timing line")
    words = len(text.split())
    if words > MAX_CUE_WORDS:
        problems.append(f"has {words} words; at most {MAX_CUE_WORDS}")
    lines = wrap_caption(text)
    if len(lines) > MAX_CUE_LINES:
        problems.append(
            f"needs {len(lines)} lines of {MAX_LINE_CHARS} characters; at most {MAX_CUE_LINES}"
        )
    for line in lines:
        if len(line) > MAX_LINE_CHARS:
            problems.append(
                f"has a word run of {len(line)} characters; a line holds {MAX_LINE_CHARS}"
            )
    return problems


def cue_timing_problems(cues: Sequence[Cue], duration_ms: int) -> List[str]:
    """Order, overlap, reading time and gaps, on the frame-rounded times shown."""
    problems: List[str] = []
    total_frames = frames_for_ms(duration_ms)
    previous: Optional[Cue] = None
    for number, cue in enumerate(cues, start=1):
        if not 0 <= cue.start_ms < cue.end_ms <= duration_ms:
            problems.append(
                f"cue {number}: {cue.start_ms}-{cue.end_ms} ms is not inside the recording (0-{duration_ms} ms)"
            )
        if previous is not None:
            if cue.start_ms < previous.start_ms:
                problems.append(f"cues {number - 1} and {number} are out of order")
            elif cue.start_ms < previous.end_ms:
                problems.append(
                    f"cues {number - 1} and {number} overlap by {(previous.end_ms - cue.start_ms) / 1000:.3f} s"
                )
            else:
                gap = ms_for_frames(cue.start_frame) - ms_for_frames(previous.end_frame)
                if gap > MAX_CUE_GAP_MS:
                    problems.append(
                        f"{gap / 1000:.3f} s without a caption between cues {number - 1} and {number}; "
                        f"at most {MAX_CUE_GAP_MS / 1000:.3f} s"
                    )
        shown = ms_for_frames(cue.end_frame) - ms_for_frames(cue.start_frame)
        need = reading_time_ms(cue.text)
        if shown < need:
            problems.append(
                f"cue {number}: {len(cue.text)} characters and {len(cue.text.split())} words "
                f"in {shown / 1000:.3f} s, needs {need / 1000:.3f} s"
            )
        previous = cue
    if cues:
        lead = ms_for_frames(cues[0].start_frame)
        if lead > MAX_CUE_GAP_MS:
            problems.append(
                f"{lead / 1000:.3f} s without a caption before cue 1; at most {MAX_CUE_GAP_MS / 1000:.3f} s"
            )
        tail = ms_for_frames(total_frames) - ms_for_frames(cues[-1].end_frame)
        if tail > MAX_CUE_GAP_MS:
            problems.append(
                f"{tail / 1000:.3f} s without a caption after cue {len(cues)}; at most {MAX_CUE_GAP_MS / 1000:.3f} s"
            )
    total_ms = ms_for_frames(video_frames(duration_ms))
    if not MIN_VIDEO_MS <= total_ms <= MAX_VIDEO_MS:
        problems.append(
            f"the video would be {total_ms / 1000:.3f} s with its cards; a showcase is "
            f"{MIN_VIDEO_MS // 1000}-{MAX_VIDEO_MS // 1000} s"
        )
    return problems


# --- JPEG frame size ---------------------------------------------------------

# Start-of-frame markers (SOF0-SOF15 less DHT, JPG and DAC), which carry the size.
_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def jpeg_size(path: Path) -> Tuple[int, int]:
    """(width, height) of a JPEG file, read from its frame header alone."""
    with open(path, "rb") as fh:
        if fh.read(2) != b"\xff\xd8":
            raise ValueError("not a JPEG file")
        while True:
            byte = fh.read(1)
            if not byte:
                raise ValueError("ends before its frame header")
            if byte != b"\xff":
                raise ValueError("has a damaged marker")
            marker_byte = fh.read(1)
            while marker_byte == b"\xff":
                marker_byte = fh.read(1)
            if not marker_byte:
                raise ValueError("ends before its frame header")
            marker = marker_byte[0]
            if marker == 0x01 or 0xD0 <= marker <= 0xD8:
                continue
            if marker in (0xD9, 0xDA):
                raise ValueError("has no frame header before its image data")
            length = int.from_bytes(fh.read(2), "big")
            if length < 2:
                raise ValueError("has a damaged segment length")
            if marker in _SOF_MARKERS:
                header = fh.read(5)
                if len(header) != 5:
                    raise ValueError("ends inside its frame header")
                return int.from_bytes(header[3:5], "big"), int.from_bytes(
                    header[1:3], "big"
                )
            fh.seek(length - 2, 1)


# --- needles.txt -------------------------------------------------------------


def needles_problems(path: Path) -> Tuple[List[str], int]:
    """Problems with needles.txt, and how many values it holds. Never the values."""
    if not path.is_file() or path.is_symlink():
        return [
            "needles.txt is missing (or is not a plain file): refusing to scan with no needles"
        ], 0
    problems: List[str] = []
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != NEEDLES_MODE:
        problems.append(
            f"needles.txt has mode {mode:04o}; it must be {NEEDLES_MODE:04o}"
        )
    try:
        values = _needle_lines(path)
    except UnicodeDecodeError:
        return problems + ["needles.txt is not UTF-8 text"], 0
    if not values:
        problems.append("needles.txt holds 0 needles: refusing to scan with no needles")
    for number, value in enumerate(values, start=1):
        if not value.strip():
            problems.append(f"needles.txt line {number} is blank")
            continue
        # Counted as the OCR scan compares it: ``a_b_c_d_e`` is 5 characters
        # there, and ``--------`` is none.
        length = len(normalise(value))
        if length < MIN_NEEDLE_CHARS:
            problems.append(
                f"needles.txt line {number} is {length} characters without its case and "
                f"separators; a needle has at least {MIN_NEEDLE_CHARS}"
            )
    return problems, len(values)


def _needle_lines(path: Path) -> List[str]:
    text = path.read_text(encoding="utf-8")
    if text.endswith("\n"):
        text = text[:-1]
    return [line.rstrip("\r") for line in text.split("\n")] if text else []


def read_needles(path: Path) -> List[str]:
    """The values in needles.txt, after the same checks the capture directory gets."""
    problems, _ = needles_problems(path)
    if problems:
        raise CaptureRefused(path.parent, problems)
    return _needle_lines(path)


# --- Output names ------------------------------------------------------------


def output_names(slug: str, mp4_sha256: str) -> Dict[str, str]:
    """The finished files' names. Each carries the MP4's short sha256.

    The render never overwrites a file, so the video someone approved by its
    sha256 is the one on disk under that name.
    """
    stem = f"{slug}-{mp4_sha256[:12]}"
    return {"mp4": f"{stem}.mp4", "vtt": f"{stem}.vtt", "review": f"{stem}-review"}


# --- Reading a capture directory ---------------------------------------------


def _load_json(path: Path, problems: List[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        problems.append(f"{path.name} is missing")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        problems.append(f"{path.name} is not valid JSON ({exc.__class__.__name__})")
    return None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (_is_int(value) or isinstance(value, float)) and math.isfinite(value)


def _keys_problem(what: str, value: Any, expected: frozenset) -> Optional[str]:
    if not isinstance(value, dict):
        return f"{what} is not an object"
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        return f"{what} has keys missing {missing} and unexpected {extra}"
    return None


def _read_frames(root: Path, problems: List[str]) -> List[Frame]:
    path = root / "frames.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        problems.append("frames.jsonl is missing")
        return []
    frames: List[Frame] = []
    seen = set()
    for number, line in enumerate(lines, start=1):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            problems.append(f"frames.jsonl line {number} is not JSON")
            continue
        bad = _keys_problem(f"frames.jsonl line {number}", entry, FRAME_KEYS)
        if bad:
            problems.append(bad)
            continue
        name, t_ms = entry["file"], entry["t_ms"]
        if not isinstance(name, str) or not FRAME_NAME.match(name):
            problems.append(
                f"frames.jsonl line {number}: file {name!r} is not NNNNNN.jpg"
            )
            continue
        if not _is_int(t_ms) or t_ms < 0:
            problems.append(
                f"frames.jsonl line {number}: t_ms {t_ms!r} is not a whole number of ms >= 0"
            )
            continue
        if name in seen:
            problems.append(f"frames.jsonl line {number}: {name} is listed twice")
        seen.add(name)
        if frames and t_ms <= frames[-1].t_ms:
            problems.append(
                f"frames.jsonl line {number}: t_ms {t_ms} is not after the previous frame's {frames[-1].t_ms}"
            )
        frames.append(Frame(file=name, t_ms=t_ms))
    if not frames and not any("frames.jsonl" in p for p in problems):
        problems.append("frames.jsonl lists no frames")
    if frames and frames[0].t_ms > FIRST_FRAME_MAX_MS:
        problems.append(
            f"the first frame comes {frames[0].t_ms} ms after the recording starts; at most {FIRST_FRAME_MAX_MS} ms"
        )
    return frames


def _check_frame_files(
    root: Path, frames: Sequence[Frame], problems: List[str]
) -> None:
    """Gate 3: every frame is a real page-area frame, not a smaller one to be upscaled."""
    for frame in frames:
        path = root / "frames" / frame.file
        if not path.is_file():
            problems.append(f"frame {frame.file} is listed but missing")
            continue
        try:
            width, height = jpeg_size(path)
        except (OSError, ValueError) as exc:
            problems.append(f"frame {frame.file} {exc}")
            continue
        if (width, height) != (PAGE_W, PAGE_H):
            problems.append(
                f"raw frame {frame.file} is {width}x{height}; the page area is {PAGE_W}x{PAGE_H}, "
                "and the render never scales a frame up"
            )


def _read_cues(root: Path, problems: List[str]) -> Optional[CueSheet]:
    data = _load_json(root / "cues.json", problems)
    if data is None:
        return None
    bad = _keys_problem("cues.json", data, CUES_KEYS)
    if bad:
        problems.append(bad)
        return None
    before = len(problems)
    slug, title, version, duration_ms = (
        data["slug"],
        data["title"],
        data["version"],
        data["duration_ms"],
    )
    if not isinstance(slug, str) or not SLUG.match(slug):
        problems.append(f"slug {slug!r} is not NN-words-like-this")
    if not isinstance(title, str) or not title.strip() or _CONTROL.search(title):
        problems.append("title is empty or holds a control character")
    if not isinstance(version, str) or not VERSION.match(version):
        problems.append(f"version {version!r} is not vX.Y.Z")
    if not _is_int(duration_ms) or duration_ms <= 0:
        problems.append(f"duration_ms {duration_ms!r} is not a positive whole number")
    if not isinstance(data["cues"], list) or not data["cues"]:
        problems.append("cues.json has no cues")
    if len(problems) > before:
        return None
    cues: List[Cue] = []
    for number, raw in enumerate(data["cues"], start=1):
        bad = _keys_problem(f"cue {number}", raw, CUE_KEYS)
        if bad:
            problems.append(bad)
            continue
        if not (_is_int(raw["start_ms"]) and _is_int(raw["end_ms"])):
            problems.append(
                f"cue {number}: start_ms and end_ms must be whole numbers of ms"
            )
            continue
        text = raw["text"]
        for problem in caption_problems(text if isinstance(text, str) else ""):
            problems.append(f"cue {number}: the caption {problem}")
        focus = raw["focus"]
        parsed_focus: Optional[Focus] = None
        if focus is not None:
            bad = _keys_problem(f"cue {number} focus", focus, FOCUS_KEYS)
            if bad:
                problems.append(bad)
            elif not all(_is_number(focus[k]) for k in ("x", "y", "w", "h")):
                problems.append(f"cue {number}: focus x, y, w and h must be numbers")
            else:
                parsed_focus = Focus(
                    x=focus["x"], y=focus["y"], w=focus["w"], h=focus["h"]
                )
                if not (
                    parsed_focus.w > 0
                    and parsed_focus.h > 0
                    and parsed_focus.x >= 0
                    and parsed_focus.y >= 0
                    and parsed_focus.x + parsed_focus.w <= PAGE_W
                    and parsed_focus.y + parsed_focus.h <= PAGE_H
                ):
                    problems.append(
                        f"cue {number}: focus {parsed_focus.x:g},{parsed_focus.y:g} "
                        f"{parsed_focus.w:g}x{parsed_focus.h:g} is not inside the page area {PAGE_W}x{PAGE_H}"
                    )
        cues.append(
            Cue(
                start_ms=raw["start_ms"],
                end_ms=raw["end_ms"],
                text=text if isinstance(text, str) else "",
                focus=parsed_focus,
            )
        )
    if len(cues) == len(data["cues"]):
        problems.extend(cue_timing_problems(cues, duration_ms))
    return CueSheet(
        slug=slug,
        title=title,
        version=version,
        duration_ms=duration_ms,
        cues=tuple(cues),
    )


def _read_manifest(root: Path, problems: List[str]) -> Optional[Manifest]:
    data = _load_json(root / "manifest.json", problems)
    if data is None:
        return None
    if not isinstance(data, dict):
        problems.append("manifest.json is not an object")
        return None
    missing = sorted(MANIFEST_KEYS - set(data))
    if missing:
        problems.append(f"manifest.json is missing {missing}")
        return None
    before = len(problems)
    for key in ("ref", "next_version", "lock_next_version"):
        if not isinstance(data[key], str) or not data[key].strip():
            problems.append(f"manifest {key} is empty")
    if not isinstance(data["commit"], str) or not COMMIT.match(data["commit"]):
        problems.append(f"manifest commit {data['commit']!r} is not a full commit hash")
    if data["profile"] not in PROFILES:
        problems.append(
            f"manifest profile {data['profile']!r} is not one of {list(PROFILES)}"
        )
    if data["viewport"] != VIEWPORT:
        problems.append(f"manifest viewport {data['viewport']!r} is not {VIEWPORT!r}")
    if not _is_number(data["zoom"]) or data["zoom"] <= 0:
        problems.append(f"manifest zoom {data['zoom']!r} is not a positive number")
    if data["page_h"] != PAGE_H:
        problems.append(f"manifest page_h {data['page_h']!r} is not {PAGE_H}")
    if data["band_h"] != BAND_H:
        problems.append(f"manifest band_h {data['band_h']!r} is not {BAND_H}")
    gates = data["gates"]
    if not isinstance(gates, dict):
        problems.append("manifest gates is not an object")
    else:
        for name in REQUIRED_CAPTURE_GATES:
            if name not in gates:
                problems.append(f"capture gate {name} is not in the manifest")
        for name, entry in gates.items():
            bad = _keys_problem(f"capture gate {name}", entry, GATE_KEYS)
            if bad:
                problems.append(bad)
            elif entry["ok"] is not True:
                problems.append(f"capture gate {name} is not ok")
            elif not isinstance(entry["numbers"], dict):
                problems.append(f"capture gate {name}: numbers is not an object")
    if len(problems) > before:
        return None
    return Manifest(
        ref=data["ref"],
        commit=data["commit"],
        next_version=data["next_version"],
        lock_next_version=data["lock_next_version"],
        profile=data["profile"],
        viewport=data["viewport"],
        zoom=data["zoom"],
        page_h=data["page_h"],
        band_h=data["band_h"],
        gates=gates,
        raw=data,
    )


def validate_capture_dir(root: Path) -> Capture:
    """Read and check a capture directory; raise ``CaptureRefused`` with every problem found."""
    root = Path(root)
    if not root.is_dir():
        raise CaptureRefused(root, ["it is not a directory"])
    problems: List[str] = []
    frames = _read_frames(root, problems)
    if frames:
        _check_frame_files(root, frames, problems)
    sheet = _read_cues(root, problems)
    if sheet is not None and frames and frames[-1].t_ms > sheet.duration_ms:
        problems.append(
            f"the last frame comes at {frames[-1].t_ms} ms, after the recording's end at {sheet.duration_ms} ms"
        )
    needle_problems, needle_count = needles_problems(root / "needles.txt")
    problems.extend(needle_problems)
    manifest = _read_manifest(root, problems)
    if problems or sheet is None or manifest is None:
        raise CaptureRefused(root, problems or ["it could not be read"])
    return Capture(
        root=root,
        frames=tuple(frames),
        sheet=sheet,
        manifest=manifest,
        needle_count=needle_count,
    )
