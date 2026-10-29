"""A narrated capture (voice PR C2): the clips it is paced by, and ``voice.json``.

The voice step (``python -m showcase.voice``, ``make showcase-voice``) makes
one clip a ``say:`` sentence into its cache, outside every repository. The
capture never calls AWS: it only reads that cache.

* ``find_clips``, before anything starts: every sentence's clip, checked
  against its entry by ``contract.cache_entry_problems`` (the voice step's own
  check; the capture never imports the voice step); a missing clip is refused
  with the command that makes it, a damaged entry as the voice step refuses
  it. Nothing in the cache is written, repaired or removed.
* ``Pacing``: what the director holds each beat for. A beat's sentences need
  their slots (``contract.slot_frames``: the longer of the clip and the
  reading time, plus ``contract.VOICE_GUARD_MS``), all inside the beat's band
  cue (the TILE rule). The narration may run on over the beat's exit, but
  never past it: a beat whose exit ends before its slots do is refused
  (``outlasts``), never topped up with a wait after the page has moved on.
  ``exit_floor_ms`` is the least time an exit's steps keep the camera rolling,
  as the director's own step code waits; ``test_showcase_voiced.py`` drives
  that code on a stand-in clock and pins the two together (G20).
* ``write_voice``, after the recording: each clip copied into
  ``<capture>/voice/NNN.wav`` (checked again by its sha256 and its sample
  count), its time laid out by the TILE rule from the recorded cue, and
  ``voice.json``; ``contract.validate_capture_dir`` then reads it back.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from showcase import contract
from showcase.capture import config, guard, storyboard


class VoiceRefused(guard.Refused):
    """The narration's clips are not what the capture needs. The message says why."""


@dataclass(frozen=True)
class Clip:
    """One sentence's clip in the voice cache."""

    scene: str
    beat: int
    line: int
    text: str
    key: str
    path: Path
    samples: int
    sha256: str

    @property
    def where(self) -> str:
        return f"scene {self.scene} beat {self.beat} line {self.line}"


def voice_for(board: storyboard.Storyboard, asked: str) -> str:
    """The voice a video is captured with: *asked*, or none for a storyboard
    without narration. Refused for a voice the tool does not have."""
    if asked not in (contract.SILENT, *contract.VOICES):
        raise VoiceRefused(
            f"voice {asked!r} is not one of {[contract.SILENT, *contract.VOICES]}"
        )
    if not storyboard.narration_lines(board):
        return contract.SILENT
    return asked


def make_voice_hint(slug: str) -> str:
    return (
        f"AWS_PROFILE=<your profile> make showcase-voice VIDEO={slug} makes them"
        " (the capture never calls AWS)"
    )


def find_clips(board: storyboard.Storyboard, voice: str, cache_dir: Path) -> List[Clip]:
    """Every narration sentence's clip, in order; empty for a silent capture.

    Read only: the cache root is not created, and no entry is changed.
    """
    lines = storyboard.narration_lines(board)
    if voice == contract.SILENT or not lines:
        return []
    root = Path(cache_dir)
    if root.is_symlink() or not root.is_dir():
        raise VoiceRefused(
            f"{board.slug}: no voice cache at {root}, so none of its {len(lines)}"
            f" clips: {make_voice_hint(board.slug)}"
        )
    checkout = guard.inside_git_checkout(root.resolve())
    if checkout is not None:
        raise VoiceRefused(
            f"the voice cache {root} is inside the git checkout {checkout}"
        )
    clips: List[Clip] = []
    missing: List[str] = []
    for scene, beat, line, text in lines:
        request = contract.polly_text(text)
        key = contract.clip_key(voice, request)
        entry = root / key
        if not entry.exists() and not entry.is_symlink():
            missing.append(f"scene {scene} beat {beat} line {line}")
            continue
        problems = contract.cache_entry_problems(entry, key, voice, request)
        if problems:
            raise VoiceRefused(
                f"{board.slug} scene {scene} beat {beat} line {line}: cache entry"
                f" {key[:12]} is refused: {'; '.join(problems)}. The capture never"
                " repairs an entry: the voice step's own run names what to do"
            )
        data = json.loads((entry / contract.CACHE_ENTRY).read_text(encoding="utf-8"))
        clips.append(
            Clip(
                scene=scene,
                beat=beat,
                line=line,
                text=text,
                key=key,
                path=entry / contract.CACHE_CLIP,
                samples=data["samples"],
                sha256=data["clip_sha256"],
            )
        )
    if missing:
        raise VoiceRefused(
            f"{board.slug}: {len(missing)} of {len(lines)} sentences have no {voice}"
            f" clip in {root} (first: {missing[0]}): {make_voice_hint(board.slug)}"
        )
    return clips


def projected_ms(board: storyboard.Storyboard, clips: Sequence[Clip]) -> int:
    """The finished video's length as the voice step projects it, cards included."""
    samples = {(c.scene, c.beat, c.line): c.samples for c in clips}
    return (
        contract.TITLE_CARD_MS
        + storyboard.voiced_estimate_ms(board, samples)
        + contract.END_CARD_MS
    )


# --- Pacing --------------------------------------------------------------------------


def exit_floor_ms(steps: Sequence[Any]) -> int:
    """The least recorded time *steps* take, as ``Director.step`` waits.

    Only the waits the step code makes on the page's clock count (the cursor's
    glide, the rest before a click, the click's ring, a look's hold, the paint
    after a wait, a field's pause); anything the browser itself takes is extra.
    A step not listed counts 0: an under-estimate only holds the beat longer
    before its exit.
    """
    total = 0
    for step in steps:
        if isinstance(step, storyboard.Click):
            total += config.POINTER_MS + config.POINTER_REST_MS + config.CLICK_RING_MS
        elif isinstance(step, storyboard.Move):
            total += config.POINTER_MS
        elif isinstance(step, storyboard.Look):
            total += step.ms
        elif isinstance(step, storyboard.Type):
            total += config.POINTER_MS + config.FIELD_PAUSE_MS
        elif isinstance(step, storyboard.WaitFor):
            total += config.PAINT_MS
    return total


@dataclass(frozen=True)
class BeatVoice:
    """One beat's narration, for the director."""

    #: The sentences' slots, in frames, summed: the band cue holds at least this.
    frames: int
    sentences: Tuple[str, ...]

    @property
    def ms(self) -> int:
        return -(-self.frames * 1000 // contract.FPS)


class Pacing:
    """Each narrated beat's slots, by ``(scene id, beat number)``; empty when silent."""

    def __init__(self, clips: Sequence[Clip] = ()):
        grouped: Dict[Tuple[str, int], List[Clip]] = {}
        for clip in clips:
            grouped.setdefault((clip.scene, clip.beat), []).append(clip)
        self.beats: Dict[Tuple[str, int], BeatVoice] = {
            where: BeatVoice(
                frames=sum(contract.slot_frames(c.text, c.samples) for c in group),
                sentences=tuple(c.text for c in group),
            )
            for where, group in grouped.items()
        }

    @property
    def narrated(self) -> bool:
        return bool(self.beats)

    def beat(self, scene: str, number: int) -> Optional[BeatVoice]:
        return self.beats.get((scene, number))

    @staticmethod
    def hold_ms(beat: storyboard.Beat, voice: Optional[BeatVoice]) -> int:
        """How long the cue is held before its payoff and exit, from its start.

        Silent: the headline's reading time and margin, and a console call's
        least hold (``min_hold_ms``). Narrated: at least the sentences' slots
        and the margin; on a payoff beat all of it before the still hold (the
        payoff is silent), otherwise less the exit's floor, since the narration
        may run on over the exit.
        """
        minimum = storyboard.min_hold_ms(beat)
        if voice is None:
            return minimum
        spoken = voice.ms + config.CUE_MARGIN_MS
        if not beat.payoff:
            spoken -= exit_floor_ms(beat.exit)
        return max(minimum, spoken)

    @staticmethod
    def outlasts(start_ms: int, end_ms: int, voice: Optional[BeatVoice]) -> int:
        """By how many ms the narration outlasts the recorded cue (0 when it fits)."""
        if voice is None:
            return 0
        held = contract.frames_for_ms(end_ms) - contract.frames_for_ms(start_ms)
        short = voice.frames - held
        return 0 if short <= 0 else -(-short * 1000 // contract.FPS)


# --- voice.json ------------------------------------------------------------------------


def write_voice(
    capture: Path,
    *,
    voice: str,
    clips: Sequence[Clip],
    cues: Sequence[Mapping[str, Any]],
    work: guard.WorkRoot,
) -> Dict[str, Any]:
    """Copy the clips in and write ``voice.json``; the numbers for the manifest.

    *cues* are the recorded cues, in the storyboard's order, one a beat.
    Refused (``VoiceRefused``) when the clips do not fit their cues by the
    TILE rule, or a clip is not the one the beat was paced by.
    """
    import wave

    by_beat: List[List[Clip]] = []
    for clip in clips:
        if clip.line == 1:
            by_beat.append([])
        by_beat[-1].append(clip)
    if len(by_beat) != len(cues):
        raise VoiceRefused(
            f"{len(by_beat)} narrated beats for {len(cues)} recorded cues"
        )
    voice_dir = capture / contract.VOICE_DIR
    if voice_dir.exists():
        work.remove(voice_dir)
    voice_dir.mkdir()
    sentences: List[Dict[str, Any]] = []
    slack: List[int] = []
    number = 0
    for index, (group, cue) in enumerate(zip(by_beat, cues), start=1):
        start = contract.frames_for_ms(int(cue["start_ms"]))
        end = contract.frames_for_ms(int(cue["end_ms"]))
        tiles = contract.tile_frames(start, end, [(c.text, c.samples) for c in group])
        if tiles is None:
            need = sum(contract.slot_frames(c.text, c.samples) for c in group)
            raise VoiceRefused(
                f"cue {index} ({group[0].where}) is {end - start} frames; its"
                f" {len(group)} sentences' slots need {need}: a clip is longer than"
                " the slot the beat was held for"
            )
        slack.append(
            contract.ms_for_frames(end - start)
            - contract.ms_for_frames(
                sum(contract.slot_frames(c.text, c.samples) for c in group)
            )
        )
        for clip, (s_frame, e_frame) in zip(group, tiles):
            number += 1
            name = f"{number:03d}.wav"
            target = voice_dir / name
            shutil.copyfile(clip.path, target)
            with wave.open(str(target), "rb") as handle:
                samples = handle.getnframes()
            if samples != clip.samples or contract.file_sha256(target) != clip.sha256:
                raise VoiceRefused(
                    f"{clip.where}: the clip copied is not the one the beat was paced"
                    " by (the voice cache changed during the capture)"
                )
            sentences.append(
                {
                    "cue": index,
                    "line": clip.line,
                    "text": clip.text,
                    "key": clip.key,
                    "file": name,
                    "samples": clip.samples,
                    "sha256": clip.sha256,
                    "start_ms": contract.ms_for_frames(s_frame),
                    "end_ms": contract.ms_for_frames(e_frame),
                }
            )
    data = {
        "voice": voice,
        "engine": contract.VOICE_ENGINE,
        "tempo": contract.TEMPO,
        "recipe": contract.VOICE_RECIPE,
        "rate": contract.VOICE_RATE,
        "sentences": sentences,
    }
    (capture / contract.VOICE_FILE).write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "sentences": len(sentences),
        "cues": len(cues),
        "least_slack_ms": min(slack) if slack else None,
    }
