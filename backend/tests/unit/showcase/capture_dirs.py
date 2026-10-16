"""Synthetic capture directories for the showcase unit tests (#1066).

The frames are JPEG headers with no image data: ``contract.jpeg_size`` reads
only the frame header, so the size checks run without an image library. The
tests that need real pixels (the render's encode and OCR) run on the
developer's machine through ``showcase.render.synthetic``, not here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from showcase import contract

REPO_ROOT = Path(__file__).resolve().parents[4]

NEEDLES = ["ZEBRA-VALUE-987-ALPHA", "QUOKKA-VALUE-123-BRAVO"]

#: Ten captions, each up long enough to read, 0.2 s apart, covering 50 s.
CUE_TEXTS = [
    "One command starts the whole stack on your machine.",
    "Open the dashboard in your browser.",
    "Sign in with the demo admin account.",
    "Your experiments, each with its status at a glance.",
    "Results give the lift and the p-value.",
    "And a recommended winner when there is one.",
    "Flags roll out in stages.",
    "With a safety check alongside.",
    "Every change is logged.",
    "That is the whole loop.",
]
CUE_MS = 4780
CUE_GAP_MS = 200
DURATION_MS = 50_000


def jpeg_bytes(width: int, height: int, *, progressive: bool = False) -> bytes:
    """A JPEG start, an APP0 segment and a frame header: enough for jpeg_size."""
    app0 = (
        b"\xff\xe0"
        + (16).to_bytes(2, "big")
        + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    )
    sof = (
        (b"\xff\xc2" if progressive else b"\xff\xc0")
        + (17).to_bytes(2, "big")
        + b"\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    )
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def default_cues() -> List[Dict[str, Any]]:
    cues = []
    for index, text in enumerate(CUE_TEXTS):
        start = CUE_GAP_MS + index * (CUE_MS + CUE_GAP_MS)
        cues.append(
            {
                "start_ms": start,
                "end_ms": start + CUE_MS,
                "text": text,
                "focus": {"x": 100, "y": 100, "w": 400, "h": 60} if index % 2 else None,
            }
        )
    return cues


def quickstart_stack(commit: str, profile: str = "core") -> Dict[str, Any]:
    """A manifest's ``stack`` as a Quick Start capture writes it (EM C5 of T166)."""
    tag = f"showcase-{profile}-{commit[:12]}"
    return {
        "name": "quickstart",
        "project": "showcase-20261015120000-abc123",
        "image_tag": tag,
        "images": {
            "tag": tag,
            "commit": commit,
            "api": {"image": f"experimently-api:{tag}", "id": "sha256:" + "a" * 64},
            "frontend": {
                "image": f"experimently-web:{tag}",
                "id": "sha256:" + "b" * 64,
            },
        },
    }


def default_manifest() -> Dict[str, Any]:
    return {
        "ref": "v0.26.3",
        "commit": "0123456789abcdef0123456789abcdef01234567",
        "next_version": "16.3.5",
        "lock_next_version": "16.3.8",
        "profile": "core",
        "viewport": {
            "width": contract.PAGE_W,
            "height": contract.PAGE_H,
            "device_scale_factor": 1,
        },
        "zoom": 4 / 3,
        "page_h": contract.PAGE_H,
        "band_h": contract.BAND_H,
        "voice": contract.SILENT,
        "gates": {
            name: {"ok": True, "numbers": {"checks": 1}}
            for name in contract.REQUIRED_CAPTURE_GATES
        },
        "stack": quickstart_stack("0123456789abcdef0123456789abcdef01234567"),
    }


def make_capture(
    root: Path,
    *,
    cues: Optional[List[Dict[str, Any]]] = None,
    duration_ms: int = DURATION_MS,
    frame_times: Optional[List[int]] = None,
    frame_size: tuple = (contract.PAGE_W, contract.PAGE_H),
    manifest: Optional[Dict[str, Any]] = None,
    needles: Optional[List[str]] = None,
) -> Path:
    """Write a capture directory that validates, unless an argument plants a defect."""
    root.mkdir(parents=True, exist_ok=True)
    frames_dir = root / "frames"
    frames_dir.mkdir(exist_ok=True)
    times = (
        frame_times if frame_times is not None else [0, 10_000, 20_000, 30_000, 40_000]
    )
    lines = []
    for number, t_ms in enumerate(times, start=1):
        name = f"{number:06d}.jpg"
        (frames_dir / name).write_bytes(jpeg_bytes(*frame_size))
        lines.append(json.dumps({"file": name, "t_ms": t_ms}))
    (root / "frames.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    sheet = {
        "slug": "01-getting-started",
        "title": "Get started",
        "version": "v0.26.3",
        "duration_ms": duration_ms,
        "cues": cues if cues is not None else default_cues(),
    }
    (root / "cues.json").write_text(json.dumps(sheet, indent=2), encoding="utf-8")
    needles_path = root / "needles.txt"
    needles_path.write_text(
        "\n".join(needles if needles is not None else NEEDLES) + "\n", encoding="utf-8"
    )
    os.chmod(needles_path, 0o600)
    (root / "manifest.json").write_text(
        json.dumps(manifest or default_manifest(), indent=2), encoding="utf-8"
    )
    return root


def edit_json(path: Path, change) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data), encoding="utf-8")


# --- Narrated capture directories (voice PR C1) ------------------------------

#: Two sentences over the even cues, one over the odd ones.
VOICED_SAY = [
    ["One command starts it.", "It runs on your machine."],
    ["Open the dashboard."],
    ["Sign in as admin.", "The Quick Start says how."],
    ["Every experiment has a status."],
    ["Results show the lift.", "The p-value is beside it."],
    ["The page recommends a winner."],
    ["Flags roll out in stages.", "Each waits for the last."],
    ["A safety check runs alongside."],
    ["Every change is logged.", "With who made it."],
    ["That is the whole loop."],
]
#: A clip: 55 ms of silence, a 0.4 s tone, 0.1 s of silence (0.555 s).
CLIP_LEAD = 2640
CLIP_TONE = 19200
CLIP_TAIL = 4800
CLIP_SAMPLES = CLIP_LEAD + CLIP_TONE + CLIP_TAIL


def clip_bytes(seed: int, *, rate: int = contract.VOICE_RATE) -> bytes:
    """A mono 16-bit clip: silence, a tone that differs per *seed*, silence."""
    import math

    out = bytearray(CLIP_LEAD * 2)
    step = 2 * math.pi * (300 + 20 * seed) / rate
    for i in range(CLIP_TONE):
        out += int(8000 * math.sin(step * i)).to_bytes(2, "little", signed=True)
    out += bytes(CLIP_TAIL * 2)
    return bytes(out)


def write_clip(
    path: Path, data: bytes, *, rate: int = contract.VOICE_RATE, channels: int = 1
) -> None:
    import wave

    with wave.open(str(path), "wb") as clip:
        clip.setnchannels(channels)
        clip.setsampwidth(2)
        clip.setframerate(rate)
        clip.writeframes(data)


def voiced_sentences(
    cues: List[Dict[str, Any]], say: List[List[str]]
) -> List[Dict[str, Any]]:
    """voice.json's sentences for *cues*, laid out by the TILE rule."""
    sentences = []
    number = 0
    for index, (cue, said) in enumerate(zip(cues, say), start=1):
        start = contract.frames_for_ms(cue["start_ms"])
        end = contract.frames_for_ms(cue["end_ms"])
        tiles = contract.tile_frames(start, end, [(t, CLIP_SAMPLES) for t in said])
        assert tiles is not None, f"cue {index} is too short for its sentences"
        for line, (text, (s, e)) in enumerate(zip(said, tiles), start=1):
            number += 1
            sentences.append(
                {
                    "cue": index,
                    "line": line,
                    "text": text,
                    "key": contract.clip_key("Ruth", contract.polly_text(text)),
                    "file": f"{number:03d}.wav",
                    "samples": CLIP_SAMPLES,
                    "sha256": "",
                    "start_ms": contract.ms_for_frames(s),
                    "end_ms": contract.ms_for_frames(e),
                }
            )
    return sentences


def make_voiced_capture(
    root: Path,
    *,
    say: Optional[List[List[str]]] = None,
    sentences: Optional[List[Dict[str, Any]]] = None,
    voice: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> Path:
    """A narrated capture directory that validates: ``make_capture``'s, with
    ``voice.json`` and a stdlib-written clip per sentence."""
    manifest = default_manifest()
    manifest["voice"] = "Ruth"
    for name in contract.VOICED_CAPTURE_GATES:
        manifest["gates"][name] = {"ok": True, "numbers": {}}
    make_capture(root, manifest=manifest, **kwargs)
    cues = json.loads((root / "cues.json").read_text(encoding="utf-8"))["cues"]
    listed = (
        sentences
        if sentences is not None
        else voiced_sentences(cues, say or VOICED_SAY)
    )
    clips = root / "voice"
    clips.mkdir()
    for number, sentence in enumerate(listed, start=1):
        path = clips / sentence["file"]
        write_clip(path, clip_bytes(number))
        sentence["sha256"] = contract.file_sha256(path)
    data = {
        "voice": "Ruth",
        "engine": contract.VOICE_ENGINE,
        "tempo": contract.TEMPO,
        "recipe": contract.VOICE_RECIPE,
        "rate": contract.VOICE_RATE,
        "sentences": listed,
    }
    data.update(voice or {})
    (root / "voice.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return root
