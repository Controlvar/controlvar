"""``python -m showcase.voice <slug|all> --voice Ruth [--cache DIR]``: the voice step's run.

See ``showcase.voice`` for what it does and its exit status. The order matters:
everything that can refuse without a call (the narration, the table, the
cache directory, ffmpeg, every existing entry, the character cap, the AWS
settings) is checked before the first call to Polly, and nothing is measured
until every clip exists.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from showcase import contract
from showcase.capture import config, guard, narration, storyboard
from showcase.voice import audio, polly
from showcase.voice.cache import (
    CacheRefused,
    CacheRoot,
    Entry,
    default_root,
    entry_record,
)

HERE = Path(__file__).resolve().parents[1]

#: Exit status.
OK = 0
REFUSED = 1
STOPPED = 2

#: Characters a second Polly's raw speech may have (Ruth measured 8.6-17.4): an
#: answer outside this is not a reading of its sentence.
PLAUSIBLE_CPS = (6.0, 25.0)


class Stop(Exception):
    """The run is refused before it measures anything (exit 2)."""


class Refusal(Exception):
    """A clip, an entry or a video is refused (exit 1)."""


@dataclass(frozen=True)
class Line:
    slug: str
    scene: str
    beat: int
    line: int
    text: str
    request: str
    key: str

    @property
    def where(self) -> str:
        return f"{self.slug} {self.scene} beat {self.beat} line {self.line}"


@dataclass
class Run:
    calls: int = 0
    characters: int = 0
    billed: int = 0
    refusals: List[str] = field(default_factory=list)


def dollars(characters: int) -> float:
    return characters * contract.DOLLARS_PER_MILLION_CHARACTERS / 1_000_000


def load_lines(
    videos: List[str], voice: str, say: Callable[[str], None]
) -> Tuple[Dict[str, storyboard.Storyboard], List[Line]]:
    """The storyboards and their sentences; Stop when the narration is refused (G1, G3)."""
    problems = contract.spoken_problems()
    if problems:
        raise Stop("the pronunciation table is refused: " + "; ".join(problems))
    boards: Dict[str, storyboard.Storyboard] = {}
    lines: List[Line] = []
    for slug in videos:
        try:
            board = storyboard.load(HERE / config.VIDEOS[slug])
        except storyboard.StoryboardError as error:
            raise Stop(str(error)) from None
        # G1 and G3 again, as the voice step's own check before any call.
        refused = narration.board_problems(board)
        if refused:
            raise Stop(f"{slug}: the narration is refused: " + "; ".join(refused))
        found = storyboard.narration_lines(board)
        if not found:
            say(f"{slug}: no narration (a silent video); nothing to make")
            continue
        boards[slug] = board
        for scene, beat, number, text in found:
            request = contract.polly_text(text)
            if "<prosody" in request.lower():
                raise Stop(f"{slug} {scene} beat {beat} line {number}: holds <prosody")
            lines.append(
                Line(
                    slug=slug,
                    scene=scene,
                    beat=beat,
                    line=number,
                    text=text,
                    request=request,
                    key=contract.clip_key(voice, request),
                )
            )
    return boards, lines


def make_entry(
    cache: CacheRoot, line: Line, voice: str, environ: Mapping[str, str], run: Run
) -> None:
    """Ask Polly for *line*, stretch it once, check it, and file it in the cache.

    Nothing is filed unless every check passes; the unfinished entry is
    removed on every way out.
    """
    unfinished = cache.unfinished(line.key)
    try:
        source = unfinished / "source.mp3"
        run.calls += 1
        billed = polly.synthesize(voice, line.request, source, environ)
        run.billed += billed
        if billed != len(line.request):
            raise Refusal(
                f"Polly billed {billed} characters for {len(line.request)}"
                " sent; nothing was cached"
            )
        clip = unfinished / "clip.wav"
        audio.stretch(source, clip)
        raw = audio.raw_samples(source)
        samples = audio.wav_samples(clip)
        problem = contract.stretch_problem(raw, samples)
        if problem:
            raise Refusal(f"G9: the clip {problem}; nothing was cached")
        speech_s = raw / contract.VOICE_RATE
        rate = len(line.request) / speech_s
        if not PLAUSIBLE_CPS[0] <= rate <= PLAUSIBLE_CPS[1]:
            raise Refusal(
                f"Polly's answer is {speech_s:.3f} s for"
                f" {len(line.request)} characters ({rate:.1f} a second), not a reading"
                " of the sentence; nothing was cached"
            )
        record = entry_record(
            key=line.key,
            voice=voice,
            text=line.request,
            request_characters=billed,
            source=source,
            raw_samples=raw,
            clip=clip,
            samples=samples,
        )
        (unfinished / "entry.json").write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        cache.publish(unfinished, line.key)
    finally:
        if unfinished.exists():
            cache.remove_unfinished(unfinished)


def measure(entry: Entry) -> Tuple[int, float, float]:
    """``(samples, LUFS, dBTP)`` for a filed clip; Refusal when G9 fails on it."""
    raw = audio.raw_samples(entry.source)
    problem = contract.stretch_problem(raw, entry.samples)
    if problem:
        raise Refusal(f"G9: the cached clip {entry.key[:12]} {problem}")
    lufs, peak = audio.loudness(entry.clip)
    return entry.samples, lufs, peak


def run(
    video: str,
    *,
    voice: str,
    cache_dir: Optional[Path],
    environ: Mapping[str, str],
    say: Callable[[str], None],
    character_cap: Optional[int] = None,
) -> int:
    if voice not in contract.VOICES:
        raise Stop(f"unknown voice {voice!r}; one of: {', '.join(contract.VOICES)}")
    if video != "all" and video not in config.VIDEOS:
        raise Stop(f"unknown video {video!r}; one of: {', '.join(config.VIDEOS)}, all")
    videos = list(config.VIDEOS) if video == "all" else [video]
    boards, lines = load_lines(videos, voice, say)
    if not lines:
        say("nothing to make")
        return OK
    try:
        cache = CacheRoot(cache_dir if cache_dir is not None else default_root(environ))
    except CacheRefused as error:
        raise Stop(str(error)) from None
    lock = guard.RunLock(cache.path)
    try:
        lock.acquire()
    except guard.Refused as error:
        raise Stop(str(error)) from None
    try:
        cap = contract.VOICE_CHARACTER_CAP if character_cap is None else character_cap
        return _run_locked(boards, lines, cache, voice, environ, say, cap)
    finally:
        lock.release()


def _run_locked(
    boards: Mapping[str, storyboard.Storyboard],
    lines: List[Line],
    cache: CacheRoot,
    voice: str,
    environ: Mapping[str, str],
    say: Callable[[str], None],
    character_cap: int,
) -> int:
    try:
        audio.refuse_missing_ffmpeg()
    except audio.AudioFailed as error:
        raise Stop(str(error)) from None
    for leftover in cache.leftovers():
        cache.remove_unfinished(leftover)
    state = Run()
    first: Dict[str, Line] = {}
    for line in lines:
        first.setdefault(line.key, line)
    entries: Dict[str, Entry] = {}
    for key, line in first.items():
        try:
            entry = cache.find(key, voice, line.request)
        except CacheRefused as error:
            say(f"REFUSED {line.where}: {error}")
            return REFUSED
        if entry is not None:
            entries[key] = entry
    missing = [line for key, line in first.items() if key not in entries]
    if missing:
        characters = sum(len(line.request) for line in missing)
        if characters > character_cap:
            raise Stop(
                f"{len(missing)} clips to make, {characters:,} characters: above the cap"
                f" of {character_cap:,} a run; nothing was sent"
            )
        if not environ.get("AWS_PROFILE"):
            raise Stop(
                f"{len(missing)} clips to make, and AWS_PROFILE is not set: run"
                " AWS_PROFILE=<your profile> make showcase-voice VIDEO=<slug|all>"
            )
        if not environ.get("HOME") or not environ.get("PATH"):
            raise Stop("the AWS CLI needs PATH and HOME, and one of them is not set")
        say(
            f"{len(missing)} clips to make, {characters:,} characters (cap"
            f" {character_cap:,}), about ${dollars(characters):.4f}: asking Polly"
        )
        for line in missing:
            try:
                make_entry(cache, line, voice, environ, state)
            except polly.PollyFailed as error:
                say(
                    f"STOPPED {line.where}: {error}. Nothing was cached for this line;"
                    " the clips already made are kept"
                )
                return STOPPED
            except (Refusal, audio.AudioFailed, CacheRefused) as error:
                say(f"REFUSED {line.where}: {error}")
                return REFUSED
            state.characters += len(line.request)
            entries[line.key] = cache.find(line.key, voice, line.request)  # type: ignore[assignment]
        if state.billed != state.characters:
            say(
                f"REFUSED: Polly billed {state.billed:,} characters for"
                f" {state.characters:,} sent"
            )
            return REFUSED
    made = {line.key for line in missing}
    measured: Dict[str, Tuple[int, float, float]] = {}
    status = OK
    for slug, board in boards.items():
        clips: List[Tuple[str, float, float]] = []
        samples: Dict[Tuple[str, int, int], int] = {}
        for line in (x for x in lines if x.slug == slug):
            if line.key not in measured:
                try:
                    measured[line.key] = measure(entries[line.key])
                except (Refusal, audio.AudioFailed) as error:
                    say(f"REFUSED {line.where}: {error}")
                    return REFUSED
            count, lufs, peak = measured[line.key]
            samples[(line.scene, line.beat, line.line)] = count
            clips.append(
                (f"{line.scene} beat {line.beat} line {line.line}", lufs, peak)
            )
            state_word = "new" if line.key in made else "cached"
            say(
                f"{line.where}: {count / contract.VOICE_RATE:6.3f} s  {lufs:6.1f} LUFS"
                f"  {peak:6.1f} dBTP  {state_word:6}  {line.text}"
            )
        projected = (
            contract.TITLE_CARD_MS
            + storyboard.voiced_estimate_ms(board, samples)
            + contract.END_CARD_MS
        )
        limit = contract.MAX_VIDEO_MS * contract.PROJECTION_SHARE
        mean = sum(c[1] for c in clips) / len(clips)
        loudest = max(c[2] for c in clips)
        gain = contract.voice_gain_db(slug)
        say(
            f"{slug}: {len(clips)} lines; projected {projected / 1000:.1f} s of"
            f" {contract.MAX_VIDEO_MS / 1000:.0f} s (refused above {limit / 1000:.1f} s);"
            f" mean loudness {mean:.1f} LUFS ({mean + gain:.1f} with the {gain:g} dB"
            f" gain); loudest true peak {loudest:.1f} dBTP ({loudest + gain:.1f} with it)"
        )
        problems = contract.loudness_problems(clips, gain)
        if projected > limit:
            problems.append(
                f"projected {projected / 1000:.1f} s, above {limit / 1000:.1f} s: cut"
                " narration by the UX cut list"
            )
        for problem in problems:
            say(f"REFUSED {slug}: {problem}")
            status = REFUSED
    say(
        f"Polly calls: {state.calls}; characters sent: {state.characters:,};"
        f" about ${dollars(state.characters):.4f}"
    )
    return status


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m showcase.voice",
        description=(
            "Make the showcase videos' narration clips with Amazon Polly, into a"
            " cache outside every repository (#1066)."
        ),
    )
    parser.add_argument("video", help=f"{', '.join(config.VIDEOS)} or all")
    parser.add_argument("--voice", required=True, help=" or ".join(contract.VOICES))
    parser.add_argument(
        "--cache",
        type=Path,
        default=None,
        help="the cache directory (default ~/.cache/experimently-showcase/voice)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    # The AWS CLI's environment is built from this copy (polly.aws_env); nothing
    # else the step starts gets more than PATH, HOME and TMPDIR.
    environ = dict(os.environ)
    guard.strip_environment(os.environ)

    def say(text: str) -> None:
        sys.stdout.write(f"[voice] {text}\n")
        sys.stdout.flush()

    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return run(
            args.video,
            voice=args.voice,
            cache_dir=args.cache.expanduser() if args.cache else None,
            environ=environ,
            say=say,
        )
    except Stop as error:
        say(f"refused: {error}")
        return STOPPED
