"""ffmpeg for a narrated video's sound (voice PR C1): the mux and the measurements.

* ``mux_argv``: the silent encode's video stream copied as it is (``-c:v
  copy``), the narration track encoded as AAC LC, 48 kHz, mono, with the
  fixed gain ``contract.VOICE_GAIN_DB`` applied by ffmpeg's ``volume`` filter
  in floating point. No ``-shortest``, no ``atrim``, no limiter, no loudnorm:
  the track is exactly as long as the video, built so (``narration.build_wav``).
* ``video_md5``: the video stream's packets, hashed (G16: the mux left the
  gated silent encode's video untouched).
* ``decode``: the finished file's sound as 48 kHz mono 16-bit samples (G13).
* ``loudness``: ffmpeg's ebur128 with true peak, on the whole sound or on one
  sentence's samples, counted on the file's own timeline (G17). The summary is logged at the info level, so the
  command line has no ``-v``; ``contract.parse_ebur128`` reads it
  and refuses output without one.

Every program runs with the render's allow-listed environment.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from showcase import contract
from showcase.render.children import child_env

FFMPEG = "ffmpeg"
AAC_BITRATE = "128k"


def gain_filter() -> str:
    return f"volume={contract.VOICE_GAIN_DB:g}dB:precision=float"


def mux_argv(silent: Path, track: Path, output: Path) -> List[str]:
    return [
        FFMPEG,
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-n",
        "-i",
        str(silent),
        "-i",
        str(track),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-af",
        gain_filter(),
        "-c:a",
        "aac",
        "-b:a",
        AAC_BITRATE,
        "-ar",
        str(contract.VOICE_RATE),
        "-ac",
        "1",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(output),
    ]


def _run(
    argv: List[str], what: str, *, text: bool = True
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        argv, capture_output=True, text=text, check=False, env=child_env()
    )
    if result.returncode != 0:
        err = result.stderr if text else result.stderr.decode("utf-8", "replace")
        raise RuntimeError(
            f"ffmpeg {what} exited {result.returncode}: {err.strip()[-300:]}"
        )
    return result


_MD5 = re.compile(r"^MD5=([0-9a-f]{32})$", re.MULTILINE)


def parse_md5(text: str) -> str:
    found = _MD5.findall(text)
    if len(found) != 1:
        raise ValueError(f"ffmpeg's md5 output is not one MD5= line: {text[:120]!r}")
    return found[0]


def video_md5(path: Path) -> str:
    """The md5 of the first video stream's packets, copied, not decoded."""
    result = _run(
        [
            FFMPEG,
            "-hide_banner",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-c",
            "copy",
            "-f",
            "md5",
            "-",
        ],
        "md5",
    )
    return parse_md5(result.stdout)


#: Decoding keeps the sound on the file's own timeline: a stream that starts
#: late is padded with silence from 0, never shifted to 0 (measured: without
#: it, ``-itsoffset 0.3`` in the mux decodes exactly as the right file does).
TIMELINE = "aresample=async=1:first_pts=0"


def decode_argv(path: Path) -> List[str]:
    return [
        FFMPEG,
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-af",
        TIMELINE,
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


def decode(path: Path) -> bytes:
    """The first audio stream, decoded on the file's timeline to 48 kHz mono
    signed 16-bit little-endian."""
    return _run(decode_argv(path), "decode", text=False).stdout


def loudness_argv(path: Path, window: Optional[Tuple[int, int]] = None) -> List[str]:
    """ebur128 with true peak; *window* is a ``(first, end)`` sample range.

    A window's samples are counted on the file's own timeline (``TIMELINE``
    before the ``atrim``), as G13 decodes them: without it a sound stream that
    starts late is trimmed as if it started at 0, so each sentence's level
    would be read from where its speech should be, not where it is.
    """
    chain = "ebur128=peak=true"
    if window is not None:
        chain = (
            f"{TIMELINE},atrim=start_sample={window[0]}:end_sample={window[1]}," + chain
        )
    return [
        FFMPEG,
        "-nostdin",
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-af",
        chain,
        "-f",
        "null",
        "-",
    ]


def loudness(
    path: Path, window: Optional[Tuple[int, int]] = None
) -> Tuple[float, float]:
    """``(integrated LUFS, true peak dBTP)``; ValueError without a summary."""
    result = _run(loudness_argv(path, window), "ebur128")
    return contract.parse_ebur128(result.stderr[-65536:])
