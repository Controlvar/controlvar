"""ffmpeg for the voice step (#1066 voiceover): the stretch, the raw length, the loudness.

Every ffmpeg runs through ``_ffmpeg`` with the tool's allow-listed environment
(``guard.tool_env``: ``PATH``, ``HOME``, ``TMPDIR``), never the AWS settings.

* ``stretch``: the 90% pace, ``atempo`` once, to a 48 kHz mono 16-bit WAV.
* ``raw_samples``: Polly's answer decoded at the same rate, to count its samples
  (G9 compares the two counts; a clip's length is always its sample count).
* ``loudness``: ffmpeg's ``ebur128`` with true peak. Its summary is logged at
  the info level (nothing at ``-v error``) and labels the true peak ``dBFS``;
  ``parse_ebur128`` reads exactly that and refuses output without a summary.
"""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path
from typing import List, Tuple

from showcase import contract
from showcase.capture import guard

#: Seconds one ffmpeg run may take (a clip is a few seconds of audio).
FFMPEG_TIMEOUT_S = 120


class AudioFailed(Exception):
    """ffmpeg could not do what the step needs, or its answer is not usable."""


def _ffmpeg(args: List[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["ffmpeg", *args],
            env=guard.tool_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=FFMPEG_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        raise AudioFailed("ffmpeg is not installed (brew install ffmpeg)") from None
    except subprocess.TimeoutExpired:
        raise AudioFailed(
            f"ffmpeg did not finish within {FFMPEG_TIMEOUT_S} s"
        ) from None


def refuse_missing_ffmpeg() -> None:
    """``ffmpeg -version``; AudioFailed when it does not answer."""
    if _ffmpeg(["-hide_banner", "-version"]).returncode != 0:
        raise AudioFailed("ffmpeg is not installed (brew install ffmpeg)")


def stretch_args(source: Path, clip: Path) -> List[str]:
    """The one stretch: ``atempo`` at ``contract.TEMPO``, to 48 kHz mono s16 WAV."""
    return [
        "-nostdin",
        "-v",
        "error",
        "-n",
        "-i",
        str(source),
        "-af",
        f"atempo={contract.TEMPO}",
        "-ar",
        str(contract.VOICE_RATE),
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        "-bitexact",
        "-f",
        "wav",
        str(clip),
    ]


def stretch(source: Path, clip: Path) -> None:
    done = _ffmpeg(stretch_args(source, clip))
    if done.returncode != 0 or not clip.is_file():
        raise AudioFailed("ffmpeg could not stretch Polly's answer to 90% pace")


def raw_samples(source: Path) -> int:
    """Polly's answer as decoded samples at ``contract.VOICE_RATE``, mono."""
    done = _ffmpeg(
        [
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(source),
            "-ar",
            str(contract.VOICE_RATE),
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            "-f",
            "s16le",
            "-",
        ]
    )
    if done.returncode != 0 or not done.stdout:
        raise AudioFailed("ffmpeg could not decode Polly's answer")
    return len(done.stdout) // 2


def wav_samples(path: Path) -> int:
    """A clip's sample count; AudioFailed unless it is 48 kHz, mono, 16-bit PCM."""
    try:
        with wave.open(str(path), "rb") as clip:
            shape = (clip.getframerate(), clip.getnchannels(), clip.getsampwidth())
            frames = clip.getnframes()
    except (OSError, EOFError, wave.Error):
        raise AudioFailed(f"{path.name} is not a WAV file") from None
    if shape != (contract.VOICE_RATE, 1, 2):
        raise AudioFailed(
            f"{path.name} is {shape[0]} Hz, {shape[1]} channel(s), {8 * shape[2]}-bit;"
            f" a clip is {contract.VOICE_RATE} Hz, mono, 16-bit"
        )
    return frames


#: ``ffmpeg -nostdin -hide_banner -nostats -i CLIP -af ebur128=peak=true -f null -``:
#: no ``-v``, since the summary is logged at the info level.
def loudness_args(clip: Path) -> List[str]:
    return [
        "-nostdin",
        "-hide_banner",
        "-nostats",
        "-i",
        str(clip),
        "-af",
        "ebur128=peak=true",
        "-f",
        "null",
        "-",
    ]


def parse_ebur128(text: str) -> Tuple[float, float]:
    """``contract.parse_ebur128``, with its refusal as an ``AudioFailed``."""
    try:
        return contract.parse_ebur128(text)
    except ValueError as error:
        raise AudioFailed(str(error)) from None


def loudness(clip: Path) -> Tuple[float, float]:
    done = _ffmpeg(loudness_args(clip))
    if done.returncode != 0:
        raise AudioFailed("ffmpeg could not measure the clip's loudness")
    return parse_ebur128(done.stderr[-65536:].decode("utf-8", "replace"))
