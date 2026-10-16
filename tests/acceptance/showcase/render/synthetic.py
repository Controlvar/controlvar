"""Made-up capture directories, to prove each render gate refuses what it should.

``python -m showcase.render.synthetic <dir> [options]`` writes a capture
directory that passes every gate: dashboard-like frames drawn by Chromium
(the page changes every 2.5 s, like a walkthrough), ten captions covering the
recording, two made-up needles, and a manifest with every capture gate ok.
Each option plants one defect instead:

``--plant-needle N``   draw needle 1 in frame N (gate 7 must refuse)
``--plant-part N:K``   draw only the first K characters of needle 1 in frame N
                       (gate 7, for K of 12 or more)
``--plant-faint N``    draw needle 1 in frame N in the OCR canary's faint style,
                       ``templates.FAINT_CANARY_PX`` px ``FAINT_CANARY_COLOUR`` (gate 7)
``--plant-key N``      draw a key-shaped value in frame N (gate 7)
``--plant-text N:T``   draw the text T in frame N (U4, for a forbidden phrase)
``--black N``          make frame N an all-black page (gate 11)
``--frame-size WxH``   draw every frame at another size (gate 3)

``--voice-cache DIR`` makes a narrated capture directory instead: a storyboard's
headlines on the band (``--story``, default video 1), its ``say:`` sentences
with their real clips copied from a voice cache (read only: give it a copy),
the times laid out by the TILE rule. With it, ``--plant-say N`` puts needle 1
into sentence N (with a key for the planted words and sentence N's sound, as
if the narration lint had been skipped), for gate 7's scan of the WebVTT file,
the SRT file and the transcript.

The needles are random for each directory and are never a real credential.
Nothing here starts a stack, and its output belongs in a scratch directory.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from showcase import contract
from showcase.render import templates
from showcase.render.chromium import Chromium

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
DURATION_MS = 50_000
FRAME_EVERY_MS = 2500
ROWS = [
    ("Homepage hero copy test", "Active", "2 variants", "12,408 users"),
    ("Checkout button colour", "Draft", "3 variants", "0 users"),
    ("Pricing page layout", "Completed", "2 variants", "48,120 users"),
    ("Onboarding email timing", "Paused", "2 variants", "3,977 users"),
]


def _cues() -> List[Dict[str, object]]:
    cue_ms, gap_ms = 4780, 200
    return [
        {
            "start_ms": gap_ms + i * (cue_ms + gap_ms),
            "end_ms": gap_ms + i * (cue_ms + gap_ms) + cue_ms,
            "text": text,
            "focus": {"x": 120, "y": 180 + 60 * (i % 4), "w": 1200, "h": 56},
        }
        for i, text in enumerate(CUE_TEXTS)
    ]


def page_html(
    step: int, extra: Optional[str] = None, black: bool = False, faint: bool = False
) -> str:
    if black:
        return (
            "<!doctype html><html><body style='margin:0;background:#000'></body></html>"
        )
    rows = "".join(
        f"<tr class='{'on' if i == step % len(ROWS) else ''}'>"
        + "".join(f"<td>{html.escape(c)}</td>" for c in row)
        + "</tr>"
        for i, row in enumerate(ROWS)
    )
    style = (
        f" style='font-family:inherit;font-size:{templates.FAINT_CANARY_PX}px;"
        f"color:{templates.FAINT_CANARY_COLOUR}'"
        if faint
        else ""
    )
    planted = f"<div class='planted'{style}>{html.escape(extra)}</div>" if extra else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        "body{margin:0;font-family:-apple-system,system-ui,sans-serif;color:#0F172A;background:#F8FAFC}"
        "header{height:84px;background:#FFFFFF;border-bottom:1px solid #E2E8F0;display:flex;align-items:center;"
        "padding:0 48px;gap:40px;font-size:20px}"
        "header b{font-size:24px}main{padding:40px 64px}h1{font-size:34px;margin:0 0 8px}"
        ".sub{color:#475569;font-size:20px;margin-bottom:28px}"
        "table{border-collapse:collapse;width:100%;background:#fff;font-size:20px}"
        "td{padding:16px 20px;border-bottom:1px solid #E2E8F0}tr.on td{background:#EFF6FF}"
        ".key{margin-top:28px;font-size:18px;color:#334155}"
        ".planted{margin-top:24px;font-family:ui-monospace,Menlo,monospace;font-size:18px}"
        "</style></head><body>"
        "<header><b>Experimently</b><span>Experiments</span><span>Feature Flags</span><span>Admin</span></header>"
        f"<main><h1>Experiments</h1><div class='sub'>Step {step + 1}: four experiments in the demo data</div>"
        f"<table>{rows}</table><div class='key'>API key eptk_ab12… created today</div>{planted}</main>"
        "</body></html>"
    )


def quickstart_stack(commit: str, profile: str = "core") -> Dict[str, object]:
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


def manifest() -> Dict[str, object]:
    return {
        "ref": "v0.26.3",
        "commit": "0" * 40,
        "next_version": "16.3.5",
        "lock_next_version": "16.3.8",
        "profile": "core",
        "viewport": dict(contract.VIEWPORT),
        "zoom": 4 / 3,
        "page_h": contract.PAGE_H,
        "band_h": contract.BAND_H,
        "voice": contract.SILENT,
        "gates": {
            name: {"ok": True, "numbers": {"synthetic": True}}
            for name in contract.REQUIRED_CAPTURE_GATES
        },
        "synthetic": True,
        "stack": quickstart_stack("0" * 40),
    }


def write(
    root: Path,
    *,
    plant_needle: Optional[int] = None,
    plant_part: Optional[Tuple[int, int]] = None,
    plant_faint: Optional[int] = None,
    plant_key: Optional[int] = None,
    plant_text: Optional[Dict[int, str]] = None,
    black: Optional[int] = None,
    frame_size: tuple = (contract.PAGE_W, contract.PAGE_H),
) -> Path:
    root.mkdir(parents=True, exist_ok=False)
    frames_dir = root / "frames"
    frames_dir.mkdir()
    needles = [
        "VALUE-" + secrets.token_hex(8).upper(),
        "VALUE-" + secrets.token_hex(8).upper(),
    ]
    key_like = "eptk_" + secrets.token_hex(12)
    lines = []
    with Chromium() as browser:
        for index, t_ms in enumerate(range(0, DURATION_MS, FRAME_EVERY_MS)):
            extra = None
            if index == plant_needle:
                extra = f"One-time password: {needles[0]}"
            elif plant_part and index == plant_part[0]:
                extra = f"Device code {needles[0][: plant_part[1]]}"
            elif index == plant_faint:
                extra = f"Last edited by {needles[0]}"
            elif index == plant_key:
                extra = f"X-API-Key: {key_like}"
            elif plant_text and index in plant_text:
                extra = plant_text[index]
            name = f"{index + 1:06d}.jpg"
            browser.page_jpeg(
                page_html(
                    index, extra, black=index == black, faint=index == plant_faint
                ),
                frames_dir / name,
                width=frame_size[0],
                height=frame_size[1],
            )
            lines.append(json.dumps({"file": name, "t_ms": t_ms}))
    (root / "frames.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    sheet = {
        "slug": "01-synthetic",
        "title": "A synthetic walkthrough",
        "version": "v0.26.3",
        "duration_ms": DURATION_MS,
        "cues": _cues(),
    }
    (root / "cues.json").write_text(
        json.dumps(sheet, indent=2) + "\n", encoding="utf-8"
    )
    needles_path = root / "needles.txt"
    fd = os.open(needles_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(needles) + "\n")
    (root / "manifest.json").write_text(
        json.dumps(manifest(), indent=2) + "\n", encoding="utf-8"
    )
    return root


#: The narrated directory's spacing: between band cues, and a few cues held
#: longer than their sentences need (so a last sentence takes the rest).
VOICED_GAP_MS = 200
VOICED_EXTRA_FRAMES = 18


def voiced_lines(
    slug: str, cache: Path
) -> List[Tuple[str, List[Tuple[str, Path, Dict[str, object]]]]]:
    """Each beat's headline and its sentences, each with its cached clip and entry.

    The storyboard is read as the capture reads it; each clip is the cache
    entry for the sentence's key, checked against its entry.json.
    """
    from showcase.capture import config, storyboard

    board = storyboard.load(Path(__file__).resolve().parents[1] / config.VIDEOS[slug])
    beats = []
    for scene in board.scenes:
        for beat in scene.beats:
            sentences = []
            for text in beat.say:
                key = contract.clip_key(contract.VOICES[0], contract.polly_text(text))
                entry_dir = cache / key
                entry = json.loads(
                    (entry_dir / "entry.json").read_text(encoding="utf-8")
                )
                clip = entry_dir / "clip.wav"
                if contract.file_sha256(clip) != entry["clip_sha256"]:
                    raise SystemExit(
                        f"cache entry {key[:12]}: clip.wav is not its entry's"
                    )
                sentences.append((text, clip, entry))
            if not sentences:
                raise SystemExit(f"{slug}: a beat has no narration")
            beats.append((beat.cue, sentences))
    return beats


def write_voiced(
    root: Path,
    *,
    cache: Path,
    story: str = "01-getting-started",
    plant_say: Optional[int] = None,
) -> Path:
    """A narrated capture directory that passes every gate, from real clips."""
    import shutil
    import wave as wav_module

    beats = voiced_lines(story, cache)
    root.mkdir(parents=True, exist_ok=False)
    (root / "frames").mkdir()
    voice_dir = root / contract.VOICE_DIR
    voice_dir.mkdir()
    needles = [
        "VALUE-" + secrets.token_hex(8).upper(),
        "VALUE-" + secrets.token_hex(8).upper(),
    ]
    cues, sentences = [], []
    at_ms = VOICED_GAP_MS
    number = 0
    for index, (headline, said) in enumerate(beats):
        parts = []
        for text, clip, _entry in said:
            with wav_module.open(str(clip), "rb") as handle:
                parts.append((text, handle.getnframes()))
        start = contract.frames_for_ms(at_ms)
        length = sum(contract.slot_frames(t, n) for t, n in parts)
        length += VOICED_EXTRA_FRAMES if index % 3 == 2 else 0
        tiles = contract.tile_frames(start, start + length, parts)
        assert tiles is not None
        cues.append(
            {
                "start_ms": contract.ms_for_frames(start),
                "end_ms": contract.ms_for_frames(start + length),
                "text": headline,
                "focus": {"x": 120, "y": 180 + 60 * (index % 4), "w": 1200, "h": 56},
            }
        )
        for line, ((text, clip, _entry), (s_frame, e_frame)) in enumerate(
            zip(said, tiles), start=1
        ):
            number += 1
            name = f"{number:03d}.wav"
            shutil.copyfile(clip, voice_dir / name)
            if plant_say == number:
                text = f"The code is {needles[0]}."
            sentences.append(
                {
                    "cue": index + 1,
                    "line": line,
                    "text": text,
                    "key": contract.clip_key(
                        contract.VOICES[0], contract.polly_text(text)
                    ),
                    "file": name,
                    "samples": parts[line - 1][1],
                    "sha256": contract.file_sha256(voice_dir / name),
                    "start_ms": contract.ms_for_frames(s_frame),
                    "end_ms": contract.ms_for_frames(e_frame),
                }
            )
        at_ms = contract.ms_for_frames(start + length) + VOICED_GAP_MS
    duration = at_ms
    lines = []
    with Chromium() as browser:
        for index, t_ms in enumerate(range(0, duration, FRAME_EVERY_MS)):
            name = f"{index + 1:06d}.jpg"
            browser.page_jpeg(
                page_html(index),
                root / "frames" / name,
                width=contract.PAGE_W,
                height=contract.PAGE_H,
            )
            lines.append(json.dumps({"file": name, "t_ms": t_ms}))
    (root / "frames.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    sheet = {
        "slug": story,
        "title": "A synthetic narrated walkthrough",
        "version": "v0.26.3",
        "duration_ms": duration,
        "cues": cues,
    }
    (root / "cues.json").write_text(
        json.dumps(sheet, indent=2) + "\n", encoding="utf-8"
    )
    fd = os.open(root / "needles.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(needles) + "\n")
    data = manifest()
    data["voice"] = contract.VOICES[0]
    for name in contract.VOICED_CAPTURE_GATES:
        data["gates"][name] = {"ok": True, "numbers": {"synthetic": True}}
    (root / "manifest.json").write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8"
    )
    voice = {
        "voice": contract.VOICES[0],
        "engine": contract.VOICE_ENGINE,
        "tempo": contract.TEMPO,
        "recipe": contract.VOICE_RECIPE,
        "rate": contract.VOICE_RATE,
        "sentences": sentences,
    }
    (root / contract.VOICE_FILE).write_text(
        json.dumps(voice, indent=2) + "\n", encoding="utf-8"
    )
    return root


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m showcase.render.synthetic", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("dir", type=Path)
    parser.add_argument("--plant-needle", type=int)
    parser.add_argument("--plant-part", metavar="N:K")
    parser.add_argument("--plant-faint", type=int)
    parser.add_argument("--plant-key", type=int)
    parser.add_argument("--plant-text", action="append", default=[], metavar="N:TEXT")
    parser.add_argument("--black", type=int)
    parser.add_argument("--frame-size", default=f"{contract.PAGE_W}x{contract.PAGE_H}")
    parser.add_argument("--voice-cache", type=Path)
    parser.add_argument("--story", default="01-getting-started")
    parser.add_argument("--plant-say", type=int)
    args = parser.parse_args(argv)
    if args.voice_cache is not None:
        root = write_voiced(
            args.dir, cache=args.voice_cache, story=args.story, plant_say=args.plant_say
        )
        sys.stdout.write(f"{root}\n")
        return 0
    width, height = (int(v) for v in args.frame_size.split("x"))
    texts = {
        int(item.split(":", 1)[0]): item.split(":", 1)[1] for item in args.plant_text
    }
    root = write(
        args.dir,
        plant_needle=args.plant_needle,
        plant_part=(
            tuple(int(v) for v in args.plant_part.split(":"))
            if args.plant_part
            else None
        ),
        plant_faint=args.plant_faint,
        plant_key=args.plant_key,
        plant_text=texts,
        black=args.black,
        frame_size=(width, height),
    )
    sys.stdout.write(f"{root}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
