"""Gates on the encoded file itself: length (1), format (2), and black stretches (11).

Every decision is a pure function of what ffprobe or ffmpeg printed, so the
unit tests feed them saved output with each defect planted. ffmpeg and
ffprobe exit 0 over most of these defects (libx264 writes yuv444p without a
word when ``-pix_fmt`` is missing), so the gates read values and exact
counts, never an exit status alone.
"""

from __future__ import annotations

import json
import re
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from showcase import contract
from showcase.render.children import child_env

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
EXPECTED_RATE = f"{contract.FPS}/1"


def ffprobe(path: Path) -> Dict[str, Any]:
    """Streams and format, with every frame decoded and counted."""
    result = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-count_frames",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=child_env(),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe exited {result.returncode}: {result.stderr.strip()[:300]}"
        )
    return json.loads(result.stdout)


def length_problems(
    info: Dict[str, Any], name: str, expected_frames: int
) -> Tuple[List[str], Dict[str, Any]]:
    """Gate 1: the finished file is 55-90 s, and exactly as long as its frames."""
    duration = float(info.get("format", {}).get("duration", "nan"))
    numbers = {"duration_s": duration, "expected_s": expected_frames / contract.FPS}
    problems: List[str] = []
    if not contract.MIN_VIDEO_MS / 1000 <= duration <= contract.MAX_VIDEO_MS / 1000:
        problems.append(
            f"{name} is {duration:.1f} s; a showcase is "
            f"{contract.MIN_VIDEO_MS // 1000}-{contract.MAX_VIDEO_MS // 1000} s"
        )
    if not abs(duration - expected_frames / contract.FPS) < 0.5 / contract.FPS:
        problems.append(
            f"{name} is {duration:.3f} s; its {expected_frames} frames make {expected_frames / contract.FPS:.3f} s"
        )
    return problems, numbers


def format_problems(
    info: Dict[str, Any], expected_frames: int
) -> Tuple[List[str], Dict[str, Any]]:
    """Gate 2: one H.264 1920x1080 yuv420p constant-30-fps video stream and nothing else."""
    streams = info.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video"]
    others = [s.get("codec_type") for s in streams if s.get("codec_type") != "video"]
    problems: List[str] = []
    if len(video) != 1:
        problems.append(f"{len(video)} video streams; expected exactly 1")
    if others:
        problems.append(f"streams other than video: {others}; expected none (no audio)")
    if not video:
        return problems, {"streams": len(streams)}
    v = video[0]
    numbers = {
        "codec": v.get("codec_name"),
        "profile": v.get("profile"),
        "size": f"{v.get('width')}x{v.get('height')}",
        "pix_fmt": v.get("pix_fmt"),
        "sar": v.get("sample_aspect_ratio"),
        "r_frame_rate": v.get("r_frame_rate"),
        "avg_frame_rate": v.get("avg_frame_rate"),
        "frames": v.get("nb_read_frames"),
        "color_range": v.get("color_range"),
        "color_space": v.get("color_space"),
    }
    if v.get("codec_name") != "h264":
        problems.append(f"codec {v.get('codec_name')}, expected h264")
    if v.get("profile") not in ("High", "Main"):
        problems.append(f"profile {v.get('profile')}, expected High or Main")
    if (v.get("width"), v.get("height")) != (contract.OUT_W, contract.OUT_H):
        problems.append(
            f"{v.get('width')}x{v.get('height')}, expected {contract.OUT_W}x{contract.OUT_H}"
        )
    if v.get("pix_fmt") != "yuv420p":
        problems.append(f"pix_fmt {v.get('pix_fmt')}, expected yuv420p")
    if v.get("sample_aspect_ratio") not in (None, "1:1", "0:1", "N/A"):
        problems.append(
            f"sample aspect ratio {v.get('sample_aspect_ratio')}, expected 1:1"
        )
    r_rate, avg_rate = v.get("r_frame_rate"), v.get("avg_frame_rate")
    if r_rate != avg_rate:
        problems.append(
            f"variable frame rate: r_frame_rate {r_rate}, avg_frame_rate {avg_rate}"
        )
    elif _rate(r_rate) != Fraction(contract.FPS):
        problems.append(f"frame rate {r_rate}, expected {EXPECTED_RATE}")
    if str(v.get("nb_read_frames")) != str(expected_frames):
        problems.append(
            f"{v.get('nb_read_frames')} frames decoded, expected {expected_frames}"
        )
    if v.get("color_range") != "tv":
        problems.append(f"color range {v.get('color_range')}, expected tv")
    if v.get("color_space") != "bt709":
        problems.append(f"color space {v.get('color_space')}, expected bt709")
    return problems, numbers


def _rate(value: Any) -> Fraction:
    try:
        return Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return Fraction(0)


def top_level_boxes(path: Path) -> List[str]:
    """The MP4's top-level box types, in file order."""
    boxes: List[str] = []
    size = path.stat().st_size
    with open(path, "rb") as fh:
        offset = 0
        while offset < size:
            fh.seek(offset)
            header = fh.read(8)
            if len(header) < 8:
                raise ValueError(f"truncated box header at byte {offset}")
            length = int.from_bytes(header[:4], "big")
            kind = header[4:8].decode("latin-1")
            if length == 1:
                large = fh.read(8)
                if len(large) < 8:
                    raise ValueError(f"truncated box size at byte {offset}")
                length = int.from_bytes(large, "big")
            elif length == 0:
                length = size - offset
            if length < 8:
                raise ValueError(f"box {kind!r} at byte {offset} has length {length}")
            boxes.append(kind)
            offset += length
    return boxes


def faststart_problems(boxes: Sequence[str]) -> List[str]:
    """Gate 2: the index (moov) comes before the media (mdat), so playback starts at once."""
    if "moov" not in boxes or "mdat" not in boxes:
        return [f"top-level boxes {list(boxes)} lack moov or mdat"]
    if boxes.index("moov") > boxes.index("mdat"):
        return ["moov after mdat: the file is not faststart"]
    return []


def parse_framemd5(text: str) -> List[str]:
    """The per-frame checksums of ffmpeg's framemd5 output, in decode order."""
    sums: List[str] = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = [f.strip() for f in line.split(",")]
        if len(fields) != 6 or not re.fullmatch(r"[0-9a-f]{32}", fields[5]):
            raise ValueError(f"unexpected framemd5 line {line[:80]!r}")
        sums.append(fields[5])
    return sums


def framemd5(path: Path) -> List[str]:
    result = subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-f",
            "framemd5",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=child_env(),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg framemd5 exited {result.returncode}: {result.stderr.strip()[:300]}"
        )
    return parse_framemd5(result.stdout)


_BLACK = re.compile(
    r"black_start:\s*(?P<start>[0-9.]+)\s+black_end:\s*(?P<end>[0-9.]+)"
)


def parse_blackdetect(stderr: str) -> List[Tuple[float, float]]:
    return [(float(m["start"]), float(m["end"])) for m in _BLACK.finditer(stderr)]


def black_problems(
    intervals: Sequence[Tuple[float, float]], total_frames: int
) -> List[str]:
    """Gate 11 on the file: no black page longer than 0.5 s outside the cards."""
    first = contract.TITLE_FRAMES / contract.FPS
    last = (total_frames - contract.END_FRAMES) / contract.FPS
    problems = []
    for start, end in intervals:
        inside_start, inside_end = max(start, first), min(end, last)
        if inside_end - inside_start > 0.5:
            problems.append(
                f"black page from {inside_start:.2f} s to {inside_end:.2f} s, outside the cards"
            )
    return problems


def blackdetect(path: Path) -> List[Tuple[float, float]]:
    result = subprocess.run(
        [
            FFMPEG,
            "-v",
            "info",
            "-nostdin",
            "-i",
            str(path),
            "-vf",
            f"crop={contract.PAGE_W}:{contract.PAGE_H}:0:0,blackdetect=d=0.5:pix_th=0.10",
            "-an",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=child_env(),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg blackdetect exited {result.returncode}: {result.stderr.strip()[-300:]}"
        )
    return parse_blackdetect(result.stderr)


def missing_filters() -> List[str]:
    """The ffmpeg filters this render needs that the installed ffmpeg lacks."""
    result = subprocess.run(
        [FFMPEG, "-hide_banner", "-filters"],
        capture_output=True,
        text=True,
        check=False,
        env=child_env(),
    )
    names = {
        line.split()[1]
        for line in result.stdout.splitlines()
        if len(line.split()) > 2 and line.startswith(" ")
    }
    needed = {
        "overlay",
        "pad",
        "concat",
        "trim",
        "scale",
        "format",
        "setsar",
        "crop",
        "select",
        "blackdetect",
    }
    return sorted(needed - names)
