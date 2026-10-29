"""OCR over the finished video: the captions are there (gate 5), and no credential is (gate 7).

The page check during capture reads the DOM; it cannot see a canvas,
CSS-generated text, a closed shadow root or a toast that came and went
between two checks. So the render reads the pixels too: it decodes the
finished MP4, counts every frame (exactly ``video_frames``), keeps one of each
distinct frame (by its decoded checksum), and reads every one of those with
every OCR engine on the machine: macOS Vision through a small Swift program
built here, and tesseract. A frame is refused when its text holds:

* a needle (a value from needles.txt), or part of one, read fuzzily. Both
  sides lose case, whitespace, ``_``, ``-`` and ``:``, and the characters OCR
  confuses are folded together (0/O/@, 1/l/I/|, f/t, 2/Z, 5/S, 8/B, and
  Cyrillic and Greek look-alikes): ``contract.normalise``. The needle is then
  cut into pieces (``chunks``): the whole of it when it is 24 characters or
  fewer, else 24-character pieces 8 apart; and, when it is longer than 12,
  also 12-character pieces 4 apart. A frame is refused when some stretch of
  its text is within 30 % edits of a piece (``NEEDLE_THRESHOLD``). So these
  are found: a whole needle of up to 24 characters with up to 30 % of them
  misread, lost or added; one of a longer needle's 24-character pieces with
  up to 7; and any 12 or more characters of a needle in a row, read as
  written (cut off by the edge of the page or a column, or wrapped onto two
  lines), or one of its 12-character pieces with up to 3 misread. Fewer than
  12 characters of a needle longer than that are not looked for: a stretch
  that short cannot be told apart from ordinary page text.

  Three needles are public: the values every Quick Start stack carries in
  docker-compose.yml, its two demo app keys and its SECRET_KEY default
  (``contract.PUBLIC_VALUES``). Each is a readable part, the product's own
  words, then a random part of 28 or 32 hex characters. Such a needle keeps
  only the pieces that start inside its random part (``needle_pieces``), so
  the apps' names and words on screen are not taken for their keys. Still
  found are the whole value, any 12 or more of its random characters in a
  row, one of its kept 12-character pieces with up to 3 misread, and, for the
  app keys, the key shape below. Not found is the readable part, alone or
  followed by fewer than 9, 8 or 10 of the random characters (the two app
  keys in ``PUBLIC_VALUES``' order, then the SECRET_KEY default). A needle
  is public only when it is exactly one of those values, as a raw string;
  every other needle is matched in all of its pieces, as above. The scan's
  numbers count the public ones (``public_needles``);
* key-shaped text: a word starting ``eptk`` (the API key prefix) with 5 or
  more key characters after it, or ``eyJ`` (a JSON web token) with 20 or more;
* (U4) a phrase that must never be on screen: a local address, an error, a
  disconnected live view, a test leftover.

Before any of that, a canary frame with made-up values is put through the
same encode: a value at 16 px, and a key and 32 hex characters at 14 px, all
#0F172A, which every engine must read (every API key's random part is hex,
and of the three public values gate 7 looks for that part's pieces alone),
and a value in the faint small style
``templates.FAINT_CANARY_PX`` px ``templates.FAINT_CANARY_COLOUR`` (13 px
#94A3B8), which at least one engine must read. Otherwise the scan is not run
and the render refuses ("OCR self-test ... scan not run"). The faint style is
under anything the dashboard draws at the capture's zoom of 4/3 (measured in
``frontend/src``: its faintest text is slate-400 or gray-400, 2.56 and 2.54
to 1 on white, at 12 px at the smallest, so 16 px in the frame; its smallest
text is 11 px, 14.7 px in the frame). Text smaller or fainter than the faint
canary is not claimed. One engine is enough for the faint value because the
engines miss it in different places (measured: on the canary page Vision read
it at 1.00 and tesseract at 0.25 to 0.50 in 7 runs; on a page with nothing
else, the other way round), and a frame is refused when either engine reads a
needle. An engine that fails on any frame stops the scan too, as does a
decode or an extraction that does not give exactly the frames asked for: a
scan that read nothing never counts as a clean one. No refusal text ever
holds a needle or the text OCR read.
"""

from __future__ import annotations

import json
import platform
import re
import secrets
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from showcase import contract
from showcase.contract import fold_glyphs, normalise
from showcase.render import encode, probe, templates
from showcase.render.children import child_env
from showcase.render.gates import Refused
from showcase.render.timeline import OutputCue
from showcase.render.workdir import WorkDir

NEEDLE_THRESHOLD = 0.7
CAPTION_THRESHOLD = 0.9
CAPTION_ABSENT_BELOW = 0.5
#: A needle longer than ``CHUNK`` is matched in pieces of this many characters,
#: ``CHUNK_STEP`` apart; one longer than ``SHORT_CHUNK`` also in pieces of that
#: many, ``SHORT_CHUNK_STEP`` apart. Each step is one more than the edits its
#: piece allows (7 in 24, 3 in 12), so any run of a needle's characters on
#: screen as long as a piece holds one piece with no more edits than allowed.
CHUNK = 24
CHUNK_STEP = 8
SHORT_CHUNK = 12
SHORT_CHUNK_STEP = 4
#: Samples inside each cue: 0.3 s after it starts and 0.3 s before it ends.
CAPTION_SAMPLE_FRAMES = 9
BATCH = 150

#: Key-shaped text: a word starting ``eptk`` (the API key prefix), then more
#: key characters than a 4-character preview would show; and a word starting
#: ``eyJ`` (a JSON web token) with 20 or more. OCR splits long values with
#: stray spaces, so one space, ``_`` or ``-`` between characters does not end
#: the value; anything else (a full stop, an ellipsis) does.
KEY_SHAPES = (
    ("eptk", 5, "key-shaped text (eptk and 5 or more characters)"),
    ("eyj", 20, "token-shaped text (eyJ and 20 or more characters)"),
)
FORBIDDEN_TEXT = (
    "localhost",
    "127.0.0.1",
    "docs journey",
    "disconnected",
    "connecting…",
    "connecting...",
    "something went wrong",
    "not listed here yet",
)


def shaped(text: str, prefix: str, minimum: int) -> bool:
    """True if a word starts with ``prefix`` and ``minimum`` value characters follow it."""
    folded = fold_glyphs(text).lower()
    for match in re.finditer(rf"(?<![a-z0-9]){prefix}", folded):
        rest = folded[match.end() : match.end() + 120].lstrip(" _-")
        count, after_separator = 0, False
        for char in rest:
            if char.isascii() and char.isalnum():
                count += 1
                after_separator = False
                if count >= minimum:
                    return True
            elif char in " _-" and not after_separator:
                after_separator = True
            else:
                break
    return False


def best_distance(pattern: str, text: str) -> int:
    """The fewest edits that turn ``pattern`` into some substring of ``text``.

    Myers' bit-parallel algorithm (1999), as Hyyro states it for searching:
    one pass over the text, one machine-word step per character.
    """
    m = len(pattern)
    if m == 0:
        return 0
    peq: Dict[str, int] = {}
    for i, char in enumerate(pattern):
        peq[char] = peq.get(char, 0) | (1 << i)
    mask = (1 << m) - 1
    high = 1 << (m - 1)
    pv, mv, score, best = mask, 0, m, m
    for char in text:
        eq = peq.get(char, 0)
        xv = eq | mv
        xh = ((((eq & pv) + pv) & mask) ^ pv) | eq
        ph = (mv | ~(xh | pv)) & mask
        mh = pv & xh
        if ph & high:
            score += 1
        elif mh & high:
            score -= 1
        ph = (ph << 1) & mask
        mh = (mh << 1) & mask
        pv = (mh | ~(xv | ph)) & mask
        mv = ph & xv
        if score < best:
            best = score
    return best


def _windows(needle: str, size: int, step: int) -> List[str]:
    starts = list(range(0, len(needle) - size + 1, step))
    if starts[-1] != len(needle) - size:
        starts.append(len(needle) - size)
    return [needle[s : s + size] for s in starts]


def chunks(normalised_needle: str) -> List[str]:
    """The pieces a needle is matched in (see the module's docstring)."""
    if not normalised_needle:
        raise ValueError("an empty needle would match any text")
    pieces = (
        [normalised_needle]
        if len(normalised_needle) <= CHUNK
        else _windows(normalised_needle, CHUNK, CHUNK_STEP)
    )
    if len(normalised_needle) > SHORT_CHUNK:
        pieces += _windows(normalised_needle, SHORT_CHUNK, SHORT_CHUNK_STEP)
    return list(dict.fromkeys(pieces))


def _placed(normalised_needle: str) -> List[Tuple[int, str]]:
    """``chunks``' pieces in order, duplicates kept, each with where it starts."""
    n = normalised_needle
    sizes = [(len(n), 1)] if len(n) <= CHUNK else [(CHUNK, CHUNK_STEP)]
    if len(n) > SHORT_CHUNK:
        sizes.append((SHORT_CHUNK, SHORT_CHUNK_STEP))
    placed: List[Tuple[int, str]] = []
    for size, step in sizes:
        starts = list(range(0, len(n) - size + 1, step))
        if starts[-1] != len(n) - size:
            starts.append(len(n) - size)
        placed += [(start, n[start : start + size]) for start in starts]
    return placed


def _public_start(needle: str) -> Optional[int]:
    """The normalised start of a public value's random part; None for any other needle.

    A needle is public only when it is exactly, as a raw string, the readable
    part and the random part of an entry of ``contract.PUBLIC_VALUES``, read
    at each call. No other form of the value counts: not in another case, not
    with a space or a newline added, not with another separator.
    """
    for readable, random_part in contract.PUBLIC_VALUES:
        if needle == readable + random_part:
            return len(normalise(readable))
    return None


def needle_pieces(needle: str) -> List[str]:
    """The pieces ``findings`` looks for a needle in (see the module's docstring).

    For every needle these are ``chunks(normalise(needle))``, except a public
    value's: only those of its pieces that start inside its random part, so
    its readable part, the apps' own words, is never looked for. They are a
    subset of ``chunks``, so a public value is refused in fewer frames than
    before and never in more. If none would remain, the needle keeps all of
    ``chunks``: the rule only ever drops pieces, never a needle.
    """
    normalised = normalise(needle)
    pieces = chunks(normalised)
    start = _public_start(needle)
    if start is None:
        return pieces
    kept = [piece for at, piece in _placed(normalised) if at >= start]
    return list(dict.fromkeys(kept)) or pieces


def allowed_edits(length: int) -> int:
    """The most edits a piece of this length may have and still match."""
    edits = 0
    while edits < length and 1 - (edits + 1) / length >= NEEDLE_THRESHOLD:
        edits += 1
    return edits


def _could_match(piece: str, normalised_text: str) -> bool:
    """False only if ``piece`` cannot be within its allowed edits of any stretch of the text.

    Cut the piece into one part more than the edits it allows: each edit
    touches at most one part, so a match leaves some part intact in the text.
    """
    parts = allowed_edits(len(piece)) + 1
    bounds = [round(i * len(piece) / parts) for i in range(parts + 1)]
    return any(
        piece[bounds[i] : bounds[i + 1]] in normalised_text for i in range(parts)
    )


def needle_similarity(normalised_needle: str, normalised_text: str) -> float:
    """The best similarity of any piece of the needle to any window of the text."""
    best = 0.0
    for piece in chunks(normalised_needle):
        best = max(best, 1 - best_distance(piece, normalised_text) / len(piece))
    return best


def _pieces_match(pieces: Sequence[str], normalised_text: str) -> Optional[float]:
    """The best similarity of any of ``pieces`` to the text, or None under the threshold.

    A piece that cannot match is skipped before the edit distance is computed.
    """
    best: Optional[float] = None
    for piece in pieces:
        if not _could_match(piece, normalised_text):
            continue
        similarity = 1 - best_distance(piece, normalised_text) / len(piece)
        if similarity >= NEEDLE_THRESHOLD and (best is None or similarity > best):
            best = similarity
    return best


def needle_match(normalised_needle: str, normalised_text: str) -> Optional[float]:
    """``needle_similarity`` when it reaches ``NEEDLE_THRESHOLD``, else None.

    The same answer, faster: a piece that cannot match is skipped before the
    edit distance is computed.
    """
    return _pieces_match(chunks(normalised_needle), normalised_text)


def levenshtein(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            )
        previous = current
    return previous[-1]


def text_similarity(a: str, b: str) -> float:
    """How alike two texts are once normalised: 1 - edits / the longer length."""
    na, nb = normalise(a), normalise(b)
    if not na and not nb:
        return 1.0
    return 1 - levenshtein(na, nb) / max(len(na), len(nb))


def findings(text: str, needles: Sequence[str]) -> List[Tuple[str, str]]:
    """What a frame's OCR text gives away: (gate, description). Never the text itself."""
    found: List[Tuple[str, str]] = []
    folded = normalise(text)
    for number, needle in enumerate(needles, start=1):
        similarity = _pieces_match(needle_pieces(needle), folded)
        if similarity is not None:
            found.append(
                ("7", f"needle {number} of {len(needles)} at {similarity:.2f}")
            )
    for prefix, minimum, what in KEY_SHAPES:
        if shaped(text, prefix, minimum):
            found.append(("7", what))
    spaced = " ".join(text.lower().split())
    for phrase in FORBIDDEN_TEXT:
        if phrase in spaced:
            found.append(("U4", f"forbidden text {phrase!r}"))
    return found


class OcrError(RuntimeError):
    pass


@dataclass
class Engine:
    name: str
    binary: str

    def read(
        self, paths: Sequence[Path]
    ) -> Dict[Path, str]:  # pragma: no cover - overridden
        raise NotImplementedError


class Tesseract(Engine):
    def _one(self, path: Path) -> Tuple[Path, str]:
        result = subprocess.run(
            [self.binary, str(path), "stdout", "--psm", "11", "-l", "eng"],
            capture_output=True,
            text=True,
            check=False,
            env=child_env(),
        )
        if result.returncode != 0:
            raise OcrError(f"tesseract exited {result.returncode} on {path.name}")
        return path, result.stdout

    def read(self, paths: Sequence[Path]) -> Dict[Path, str]:
        with ThreadPoolExecutor(max_workers=8) as pool:
            return dict(pool.map(self._one, paths))


class Vision(Engine):
    def _batch(self, paths: Sequence[Path]) -> Dict[Path, str]:
        result = subprocess.run(
            [self.binary, *[str(p) for p in paths]],
            capture_output=True,
            text=True,
            check=False,
            env=child_env(),
        )
        if result.returncode != 0:
            raise OcrError(
                f"vision_ocr exited {result.returncode}: {result.stderr.strip()[:200]}"
            )
        lines = result.stdout.splitlines()
        if len(lines) != len(paths):
            raise OcrError(f"vision_ocr answered {len(lines)} of {len(paths)} images")
        texts: Dict[Path, str] = {}
        for path, line in zip(paths, lines):
            record = json.loads(line)
            if record.get("file") != str(path) or not isinstance(
                record.get("text"), str
            ):
                raise OcrError(
                    f"vision_ocr answered for the wrong image at {path.name}"
                )
            texts[path] = record["text"]
        return texts

    def read(self, paths: Sequence[Path]) -> Dict[Path, str]:
        groups = [list(paths[i : i + 40]) for i in range(0, len(paths), 40)]
        texts: Dict[Path, str] = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            for part in pool.map(self._batch, groups):
                texts.update(part)
        return texts


def build_vision(work: WorkDir) -> Optional[Vision]:
    """Compile the Vision reader on macOS; None where there is no Vision."""
    swiftc = shutil.which("swiftc")
    if platform.system() != "Darwin" or not swiftc:
        return None
    binary = work.path / "vision_ocr"
    source = Path(__file__).with_name("vision_ocr.swift")
    result = subprocess.run(
        [swiftc, "-O", str(source), "-o", str(binary)],
        capture_output=True,
        text=True,
        check=False,
        env=child_env(),
    )
    if result.returncode != 0:
        raise OcrError(
            f"swiftc could not build the Vision reader: {result.stderr.strip()[:300]}"
        )
    return Vision(name="vision", binary=str(binary))


def engines(work: WorkDir) -> List[Engine]:
    found: List[Engine] = []
    vision = build_vision(work)
    if vision:
        found.append(vision)
    tesseract = shutil.which("tesseract")
    if tesseract:
        found.append(Tesseract(name="tesseract", binary=tesseract))
    return found


def _read(engine: Engine, paths: Sequence[Path], gate: str) -> Dict[Path, str]:
    try:
        texts = engine.read(paths)
    except (OcrError, OSError, json.JSONDecodeError) as exc:
        raise Refused(
            gate, f"OCR failed ({engine.name}: {exc}); the scan is not counted"
        ) from exc
    if set(texts) != set(paths):
        raise Refused(
            gate,
            f"{engine.name} read {len(texts)} of {len(paths)} frames; the scan is not counted",
        )
    return texts


def self_test(
    found: Sequence[Engine],
    draw_canary: Callable[[str, str, str, str, Path], None],
    work: WorkDir,
) -> Dict[str, Dict[str, float]]:
    """Read made-up values through the video's encode; refuse if the scan could not see them.

    Every engine must read the value, the key and the 32 hex characters drawn
    in dark text; at least one must read the value drawn in the faint small
    style (``templates.FAINT_CANARY_PX`` px ``templates.FAINT_CANARY_COLOUR``).
    """
    if not found:
        raise Refused(
            "7",
            "no OCR engine (macOS Vision or tesseract) on this machine; scan not run",
        )
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    needle = "CANARY-" + "".join(secrets.choice(alphabet) for _ in range(16))
    faint = "FAINT-" + "".join(secrets.choice(alphabet) for _ in range(16))
    key_like = "eptk_" + "".join(
        secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(24)
    )
    hex_value = secrets.token_hex(16)
    canary_dir = work.path / "canary"
    canary_dir.mkdir()
    png = canary_dir / "canary.png"
    draw_canary(needle, key_like, faint, hex_value, png)
    decoded = encode.png_to_h264_and_back(png, canary_dir)
    numbers: Dict[str, Dict[str, float]] = {"canary": {}, "faint": {}, "hex": {}}
    problems = []
    for engine in found:
        try:
            text = engine.read([decoded])[decoded]
        except (OcrError, OSError, KeyError, json.JSONDecodeError) as exc:
            problems.append(
                f"OCR self-test: {engine.name} failed ({exc}); scan not run"
            )
            continue
        similarity = needle_similarity(normalise(needle), normalise(text))
        numbers["canary"][engine.name] = round(similarity, 3)
        numbers["faint"][engine.name] = round(
            needle_similarity(normalise(faint), normalise(text)), 3
        )
        if similarity < NEEDLE_THRESHOLD:
            problems.append(
                f"OCR self-test: {engine.name} read the canary at {similarity:.2f} "
                f"(needs {NEEDLE_THRESHOLD:.2f}); scan not run"
            )
        if not shaped(text, "eptk", 5):
            problems.append(
                f"OCR self-test: {engine.name} did not see the key-shaped canary; scan not run"
            )
        hex_similarity = needle_similarity(normalise(hex_value), normalise(text))
        numbers["hex"][engine.name] = round(hex_similarity, 3)
        if hex_similarity < NEEDLE_THRESHOLD:
            problems.append(
                f"OCR self-test: {engine.name} read the hex canary at {hex_similarity:.2f} "
                f"(needs {NEEDLE_THRESHOLD:.2f}); scan not run"
            )
    if not problems and max(numbers["faint"].values()) < NEEDLE_THRESHOLD:
        readings = ", ".join(
            f"{name} {value:.2f}" for name, value in numbers["faint"].items()
        )
        problems.append(
            f"OCR self-test: no engine read the faint canary "
            f"({templates.FAINT_CANARY_PX} px {templates.FAINT_CANARY_COLOUR}: {readings}; "
            f"needs {NEEDLE_THRESHOLD:.2f}), so the scan could miss the dashboard's faintest text; "
            "scan not run"
        )
    work.remove(canary_dir)
    if problems:
        raise Refused("7", problems)
    return numbers


def scan(
    video: Path,
    expected_frames: int,
    needles: Sequence[str],
    found: Sequence[Engine],
    work: WorkDir,
    check_disk: Callable[[], None],
) -> Tuple[Dict[str, object], Dict[str, object]]:
    """Gates 7 and U4 over every distinct frame of the finished video.

    The scan checks its own inputs rather than trusting its callers: some
    needles, each long enough to look for; at least one engine; exactly
    ``expected_frames`` decoded (never zero); and exactly the frames asked for
    in every extraction. Anything else refuses, and the scan is not counted.
    """
    if not needles:
        raise Refused("7", "0 needles: refusing to scan")
    short = [
        number
        for number, needle in enumerate(needles, start=1)
        if len(normalise(needle)) < contract.MIN_NEEDLE_CHARS
    ]
    if short:
        raise Refused(
            "7",
            f"needles {short} are under {contract.MIN_NEEDLE_CHARS} characters without "
            "their case and separators; refusing to scan",
        )
    if not found:
        raise Refused("7", "no OCR engine; refusing to scan")
    try:
        sums = probe.framemd5(video)
    except (RuntimeError, ValueError) as exc:
        raise Refused("7", f"could not decode the video for OCR ({exc})") from exc
    if expected_frames <= 0 or len(sums) != expected_frames:
        raise Refused(
            "7",
            f"decoded {len(sums)} frames, expected {expected_frames}; the scan is not counted",
        )
    first: Dict[str, int] = {}
    for index, checksum in enumerate(sums):
        first.setdefault(checksum, index)
    unique = sorted(first.values())
    check_disk()
    texts: Dict[int, Dict[str, str]] = {index: {} for index in unique}
    batch_root = work.path / "ocr"
    for number, start in enumerate(range(0, len(unique), BATCH)):
        batch = unique[start : start + BATCH]
        batch_dir = batch_root / f"batch-{number:03d}"
        try:
            paths = encode.extract_frames(video, batch, batch_dir)
        except RuntimeError as exc:
            raise Refused(
                "7",
                f"could not extract frames for OCR ({exc}); the scan is not counted",
            ) from exc
        if len(paths) != len(batch) or len(set(paths)) != len(batch):
            raise Refused(
                "7",
                f"extracted {len(set(paths))} distinct frames of the {len(batch)} asked for "
                f"(batch {number}); the scan is not counted",
            )
        for engine in found:
            read = _read(engine, paths, "7")
            for index, path in zip(batch, paths):
                texts[index][engine.name] = read[path]
        work.remove(batch_dir)
    counts = {
        engine.name: sum(1 for t in texts.values() if engine.name in t)
        for engine in found
    }
    for name, count in counts.items():
        if count != len(unique):
            raise Refused(
                "7",
                f"{name} read {count} of {len(unique)} distinct frames; the scan is not counted",
            )
    cache: Dict[str, List[Tuple[str, str]]] = {}
    hits: Dict[str, List[str]] = {"7": [], "U4": []}
    for index in unique:
        for engine_name, text in texts[index].items():
            if text not in cache:
                cache[text] = findings(text, needles)
            for gate, what in cache[text]:
                hits[gate].append(
                    f"frame {index} ({index / contract.FPS:.2f} s): {what}, read by {engine_name}"
                )
    scan_numbers: Dict[str, object] = {
        "frames": len(sums),
        "distinct_frames": len(unique),
        "ocr_reads": counts,
        "needles": len(needles),
        "public_needles": sum(
            1 for needle in needles if _public_start(needle) is not None
        ),
        "hits": len(hits["7"]),
    }
    if hits["7"]:
        raise Refused(
            "7",
            hits["7"][:20]
            + ([f"... and {len(hits['7']) - 20} more"] if len(hits["7"]) > 20 else []),
        )
    u4_numbers: Dict[str, object] = {
        "phrases": len(FORBIDDEN_TEXT),
        "hits": len(hits["U4"]),
    }
    if hits["U4"]:
        raise Refused("U4", hits["U4"][:20])
    return scan_numbers, u4_numbers


def caption_samples(cues: Sequence[OutputCue]) -> List[Tuple[int, int, str]]:
    """(frame, cue number, kind) to read for gate 5: present early, present late, gone after."""
    samples = []
    for position, cue in enumerate(cues):
        samples.append((cue.start_frame + CAPTION_SAMPLE_FRAMES, cue.number, "start"))
        samples.append((cue.end_frame - 1 - CAPTION_SAMPLE_FRAMES, cue.number, "end"))
        if position + 1 < len(cues):
            following = cues[position + 1]
            samples.append(
                (
                    (following.start_frame + following.end_frame) // 2,
                    cue.number,
                    "after",
                )
            )
    return samples


def caption_problems(
    cues: Sequence[OutputCue],
    samples: Sequence[Tuple[int, int, str]],
    read: Dict[int, List[str]],
) -> Tuple[List[str], Dict[str, object]]:
    """Gate 5's decision, from the band text every engine read at each sample frame."""
    by_number = {cue.number: cue for cue in cues}
    good = {cue.number: True for cue in cues}
    problems: List[str] = []
    lowest = 1.0
    for frame, number, kind in samples:
        cue = by_number[number]
        similarity = max(
            (text_similarity(text, cue.text) for text in read[frame]), default=0.0
        )
        at = f"{frame / contract.FPS:.3f} s"
        if kind in ("start", "end"):
            lowest = min(lowest, similarity)
            if similarity < CAPTION_THRESHOLD:
                good[number] = False
                problems.append(
                    f"cue {number} not in the frame at {at} (similarity {similarity:.2f})"
                )
        elif similarity >= CAPTION_ABSENT_BELOW:
            good[number] = False
            problems.append(
                f"cue {number} still in the frame at {at}, during cue {number + 1} (similarity {similarity:.2f})"
            )
    confirmed = sum(good.values())
    if confirmed != len(cues) and not problems:
        problems.append(f"{confirmed} cues confirmed, the cue sheet has {len(cues)}")
    return problems, {
        "cues": len(cues),
        "confirmed": confirmed,
        "lowest_similarity": round(lowest, 3),
    }


def check_captions(
    video: Path, cues: Sequence[OutputCue], found: Sequence[Engine], work: WorkDir
) -> Dict[str, object]:
    """Gate 5: every burned-in caption is there, in sync, and gone when the next one comes."""
    samples = caption_samples(cues)
    frames = sorted({frame for frame, _, _ in samples})
    try:
        paths = encode.extract_frames(
            video, frames, work.path / "captions", crop_band=True
        )
    except RuntimeError as exc:
        raise Refused("5", f"could not extract caption frames ({exc})") from exc
    read: Dict[int, List[str]] = {frame: [] for frame in frames}
    for engine in found:
        texts = _read(engine, paths, "5")
        for frame, path in zip(frames, paths):
            read[frame].append(texts[path])
    work.remove(work.path / "captions")
    problems, numbers = caption_problems(cues, samples, read)
    numbers["engines"] = [engine.name for engine in found]
    if problems:
        raise Refused("5", problems)
    return numbers


def canary_drawer(browser) -> Callable[[str, str, str, str, Path], None]:
    def draw(
        needle: str, key_like: str, faint: str, hex_value: str, path: Path
    ) -> None:
        browser.card(templates.canary_html(needle, key_like, faint, hex_value), path)

    return draw
