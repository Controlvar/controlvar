"""Render one capture directory into the finished video, its captions and its review page.

The order matters, and each step is a gate that refuses rather than warns:

1. ``disk``: at least ``contract.DISK_FLOOR_BYTES`` free where the work and
   the output go (checked again before the OCR decode).
2. ``capture``: ``contract.validate_capture_dir`` accepts the directory, which
   includes gate 3 (every frame is a real 1920x940 page, never one the encode
   would scale up) and every capture gate ok. ``stack``: it was captured on
   the Quick Start stack, with its project and image IDs named
   (``contract.stack_problems``).
3. ``7`` (text): no caption or card text holds a needle.
4. ``17``: every caption, as Chromium draws it, is at least 36 px, on at
   most two lines, none wider than the band allows.
5. ``7`` (self-test): every OCR engine reads a made-up canary back.
6. The encode, to ``<out>/<slug>.mp4.partial``.
7. ``1`` and ``2``: length, format, faststart. ``11``: no black page outside
   the cards. ``4``: the WebVTT file, parsed back strictly, matches the cue
   sheet. ``5``: every burned-in caption is read back from the frames.
   ``7`` and ``U4``: OCR of every distinct frame of the finished video.
8. A narrated capture (``voice.json``): the WebVTT file holds the sentences,
   not the band's headlines (``4`` checks them and the TILE rule), with an
   SRT file and a transcript beside it (``G6``); ``7`` reads all three files
   as written. Then the narration track (``G15``: every clip at its place),
   the mux onto the gated silent encode (``G16``: one AAC stream as long as
   the video, the video stream's packets unchanged), the speech heard where
   the WebVTT cues say (``G13``) and the loudness (``G17``). A silent capture
   skips all of this and ``G16`` expects no sound at all.
9. The review page, the sha256-named files (``G19``: named from the file
   that is published, the narrated one when there is narration), and only
   then the renames.

needles.txt is deleted on every path out of ``render``, pass or refuse, once
the render has started; its values live only in memory, and only until the
review page has been checked against them. So a refusal, even a passing one
such as low disk, means the capture has to be made again, and every refusal
says so ("re-capture needed").

Everything the render makes is written in its work directory, the ``.partial``
files and the review page included, and reaches the output directory only by
a hard link or a rename once every gate passed. The work directory is deleted
on every way out (emptied, if it was there before), through
``WorkDir.remove``, the only code in the render that deletes (EM condition
7e). The ``paths`` gate refuses an output directory that is the work
directory, or inside it, or around it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from showcase import contract
from showcase.render import (
    encode,
    narration,
    ocr,
    probe,
    review,
    sound,
    templates,
    timeline,
    vtt,
)
from showcase.render.chromium import Chromium, caption_problems
from showcase.render.gates import GateLog, Refused
from showcase.render.workdir import WorkDir, git_checkout_above

#: The last line of every refusal: needles.txt is gone, so this capture cannot
#: be rendered again.
RECAPTURE = (
    "needles.txt is deleted on every refusal, so this capture directory cannot be "
    "rendered again: re-capture needed"
)


@dataclass(frozen=True)
class RenderResult:
    mp4: Path
    vtt: Path
    review: Path
    mp4_sha256: str
    vtt_sha256: str
    gates: Dict[str, Dict[str, Any]]
    #: A narrated video's SRT file and transcript; None for a silent one.
    srt: Optional[Path] = None
    transcript: Optional[Path] = None
    srt_sha256: Optional[str] = None
    transcript_sha256: Optional[str] = None


def published_text_hits(files: Dict[str, Path], needles: List[str]) -> List[str]:
    """Gate 7 on the published text files (the WebVTT file, and for a narrated
    video the SRT file and the transcript), read as written, before any is placed."""
    return [
        f"the {label} holds needle {n} of {len(needles)}"
        for label, path in files.items()
        for n in review.holds_needle(path.read_text(encoding="utf-8"), needles)
    ]


def identity_problems(mp4: Path, names: Dict[str, str], page: str) -> List[str]:
    """G19: the file on disk, its name and the review page name one sha256."""
    digest = sha256(mp4)
    problems = []
    if not names["mp4"].endswith(f"-{digest[:12]}.mp4"):
        problems.append(
            f"the video is named {names['mp4']}, but its sha256 starts {digest[:12]}"
        )
    if digest not in page:
        problems.append(f"the review page does not give the video's sha256 {digest}")
    return problems


def _existing(path: Path) -> Path:
    while not path.exists():
        path = path.parent
    return path


def _device(path: Path) -> int:
    """The filesystem a path is (or would be created) on."""
    return os.stat(_existing(path)).st_dev


def _key(path: Path) -> Tuple[str, ...]:
    # Compared case-folded: the default macOS filesystem is case-insensitive, so
    # ``Videos`` and ``videos`` are one directory there. On a case-sensitive
    # filesystem this refuses two directories that differ only by case, which
    # errs the safe way.
    return tuple(part.casefold() for part in path.parts)


def _same(a: Path, b: Path) -> bool:
    return _key(a) == _key(b)


def _inside(child: Path, parent: Path) -> bool:
    child_key, parent_key = _key(child), _key(parent)
    return (
        len(child_key) > len(parent_key) and child_key[: len(parent_key)] == parent_key
    )


def _overlap(a: Path, b: Path) -> bool:
    return _same(a, b) or _inside(a, b) or _inside(b, a)


def paths_problems(capture_dir: Path, out_dir: Path, work: Path) -> List[str]:
    """The ``paths`` gate, on resolved paths: where the render may write and delete."""
    problems: List[str] = []
    for label, path in (("output", out_dir), ("work", work)):
        checkout = git_checkout_above(path)
        if checkout is not None:
            problems.append(
                f"the {label} directory {path} is inside the git checkout at {checkout}; "
                "rendered videos and frames never go into a repository"
            )
    if _same(out_dir, work):
        problems.append(
            f"the output and work directories are both {work}; the render deletes its "
            "work directory when it ends, so give the output a place of its own"
        )
    elif _inside(out_dir, work):
        problems.append(
            f"the output directory {out_dir} is inside the work directory {work}, "
            "which the render deletes when it ends"
        )
    elif _inside(work, out_dir):
        problems.append(
            f"the work directory {work} is inside the output directory {out_dir}; "
            "give them separate places"
        )
    if _overlap(work, capture_dir):
        problems.append(
            f"the work directory {work} and the capture directory {capture_dir} "
            "overlap; the render deletes its work directory when it ends"
        )
    if work.exists() and (not work.is_dir() or any(work.iterdir())):
        problems.append(
            f"the work directory {work} is not empty; give the render an empty or new one"
        )
    if _device(out_dir) != _device(work):
        problems.append(
            f"the output directory {out_dir} and the work directory {work} are on "
            "different filesystems; the finished files are linked into place from "
            "the work directory, so put both on one"
        )
    return problems


def check_disk(paths: Dict[str, Path], floor: int) -> Dict[str, int]:
    """Free bytes where each labelled path lives; refuse below the floor."""
    readings = {
        label: shutil.disk_usage(_existing(path)).free for label, path in paths.items()
    }
    low = [
        f"{free / 1024**3:.2f} GiB free for the {label} directory; the floor is {floor / 1024**3:.2f} GiB"
        for label, free in readings.items()
        if free < floor
    ]
    if low:
        raise Refused("disk", low)
    return readings


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _place(partial: Path, final: Path) -> None:
    """Link a finished file into place; never over an existing one.

    The ``.partial`` name stays in the work directory and goes with it.
    """
    os.link(partial, final)


def _narrate(
    expected: int,
    spoken: contract.Narration,
    silent: Path,
    voiced: Path,
    parsed: List[vtt.VttCue],
    total_frames: int,
    work: WorkDir,
    log: GateLog,
) -> Path:
    """The narration track onto the gated silent encode, and the sound's gates.

    Returns the narrated file, which is the one published and named.
    """
    track = work.path / "narration.wav"
    narration.build_wav(spoken, total_frames, track)
    problems, numbers = narration.placement_problems(track, spoken, total_frames)
    log.require("G15", problems, numbers)
    try:
        encode.encode(sound.mux_argv(silent, track, voiced), work.path / "mux.log")
    except RuntimeError as exc:
        raise Refused("G16", str(exc)) from exc
    work.remove(track)

    try:
        info = probe.ffprobe(voiced)
        silent_md5, voiced_md5 = sound.video_md5(silent), sound.video_md5(voiced)
    except (RuntimeError, ValueError) as exc:
        raise Refused("G16", f"the narrated file could not be read ({exc})") from exc
    problems, numbers = probe.format_problems(
        info, total_frames, audio_streams=expected
    )
    problems += probe.faststart_problems(probe.top_level_boxes(voiced))
    if voiced_md5 != silent_md5:
        problems.append(
            f"the narrated file's video stream (md5 {voiced_md5}) is not the gated"
            f" silent encode's ({silent_md5})"
        )
    numbers.update(video_md5=voiced_md5, silent_video_md5=silent_md5)
    log.require("G16", problems, numbers)

    try:
        values = narration.samples_from_s16le(sound.decode(voiced))
        windows = narration.windows_from(parsed)
        expected = []
        for sentence, (start, _end) in zip(spoken.sentences, windows):
            first, last = narration.clip_speech(spoken.clip(sentence))
            expected.append((start + first, start + last))
    except (RuntimeError, ValueError) as exc:
        raise Refused("G13", str(exc)) from exc
    problems, numbers = narration.speech_problems(values, windows, expected)
    numbers["decoded_samples"] = len(values)
    log.require("G13", problems, numbers)

    try:
        integrated, peak = sound.loudness(voiced)
        levels = [sound.loudness(voiced, window)[0] for window in windows]
    except (RuntimeError, ValueError) as exc:
        raise Refused("G17", str(exc)) from exc
    log.require("G17", *narration.loudness_problems(integrated, peak, levels))
    return voiced


def render(
    capture_dir: Path,
    out_dir: Path,
    work_dir: Optional[Path] = None,
    *,
    disk_floor: int = contract.DISK_FLOOR_BYTES,
) -> RenderResult:
    capture_dir = Path(capture_dir).resolve()
    out_dir = Path(out_dir).expanduser().resolve()
    needles_path = capture_dir / "needles.txt"
    work = WorkDir(
        Path(work_dir).expanduser().resolve()
        if work_dir
        else capture_dir.parent / "render",
        needles=needles_path,
    )
    floor = max(disk_floor, contract.DISK_FLOOR_BYTES)
    log = GateLog()
    started = created = False
    try:
        problems = paths_problems(capture_dir, out_dir, work.path)
        if problems:
            raise Refused("paths", problems)
        log.passed(
            "disk",
            {
                "free_bytes": check_disk({"output": out_dir, "work": work.path}, floor),
                "floor_bytes": floor,
            },
        )

        try:
            capture = contract.validate_capture_dir(capture_dir)
        except contract.CaptureRefused as exc:
            raise Refused("capture", exc.problems) from exc
        # EM C5 of T166: only a Quick Start capture becomes a video.
        stack_problems = contract.stack_problems(capture.manifest)
        if stack_problems:
            raise Refused("stack", stack_problems)
        sheet = capture.sheet
        spoken = capture.narration
        log.passed(
            "3",
            {
                "frames_checked": len(capture.frames),
                "frame_size": f"{contract.PAGE_W}x{contract.PAGE_H}",
            },
        )
        log.passed(
            "capture",
            {
                "cues": len(sheet.cues),
                "capture_gates": len(capture.manifest.gates),
                "needles": capture.needle_count,
                "voice": capture.manifest.voice,
                "sentences": len(spoken.sentences) if spoken else 0,
            },
        )
        needles = contract.read_needles(needles_path)

        missing_tools = [
            tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None
        ]
        if missing_tools:
            raise Refused("tools", f"{', '.join(missing_tools)} not found on PATH")
        missing = probe.missing_filters()
        if missing:
            raise Refused("tools", f"this ffmpeg lacks the filters {missing}")

        created = not work.path.exists()
        work.path.mkdir(parents=True, exist_ok=True)
        started = True
        partial_mp4 = work.path / f"{sheet.slug}.mp4.partial"
        partial_vtt = work.path / f"{sheet.slug}.vtt.partial"
        partial_review = work.path / f"{sheet.slug}-review.partial"
        partial_srt = work.path / f"{sheet.slug}.srt.partial"
        partial_txt = work.path / f"{sheet.slug}.txt.partial"
        partial_voiced = work.path / f"{sheet.slug}.voiced.mp4.partial"

        out_cues = timeline.output_cues(sheet)
        recording_frames = sheet.capture_frames
        total_frames = contract.video_frames(sheet.duration_ms)

        text_hits = (
            [
                f"cue {cue.number}'s caption holds needle {n} of {len(needles)}"
                for cue in out_cues
                for n in review.holds_needle(cue.text, needles)
            ]
            + [
                f"a card's text holds needle {n} of {len(needles)}"
                for text in templates.card_texts(sheet.title, sheet.version)
                for n in review.holds_needle(text, needles)
            ]
            + [
                f"sentence {s.number} holds needle {n} of {len(needles)}"
                for s in (spoken.sentences if spoken else ())
                for n in review.holds_needle(s.text, needles)
            ]
        )
        if text_hits:
            raise Refused("7", text_hits)

        pngs = work.path / "png"
        pngs.mkdir()
        band_files = [pngs / f"cue-{cue.number:02d}.png" for cue in out_cues]
        with Chromium() as browser:
            problems: List[str] = []
            fonts = []
            for cue, path in zip(out_cues, band_files):
                metrics = browser.caption(cue.lines, path)
                fonts.append(metrics["font_px"])
                problems += caption_problems(cue.number, metrics, len(cue.lines))
            log.require(
                "17",
                problems,
                {
                    "captions": len(band_files),
                    "font_px": min(fonts, default=0),
                    "max_lines": contract.MAX_CUE_LINES,
                },
            )
            empty_band = pngs / "band-empty.png"
            browser.caption((), empty_band)
            title_png, end_png = pngs / "title.png", pngs / "end.png"
            browser.card(templates.title_card_html(sheet.title), title_png)
            browser.card(templates.end_card_html(sheet.version), end_png)
            try:
                found = ocr.engines(work)
            except ocr.OcrError as exc:
                raise Refused("7", f"{exc}; scan not run") from exc
            canary = ocr.self_test(found, ocr.canary_drawer(browser), work)

        seq_dir = work.path / "sequence"
        encode.build_sequences(
            capture.frames_dir,
            [frame.file for frame in capture.frames],
            timeline.frame_schedule(capture.frames, recording_frames),
            band_files,
            timeline.band_schedule(sheet.cues, recording_frames),
            empty_band,
            seq_dir,
        )
        argv = encode.ffmpeg_argv(
            title_png, seq_dir, end_png, recording_frames, partial_mp4
        )
        try:
            encode.encode(argv, work.path / "ffmpeg.log")
        except RuntimeError as exc:
            raise Refused("encode", str(exc)) from exc
        work.remove(seq_dir)

        try:
            info = probe.ffprobe(partial_mp4)
        except (RuntimeError, ValueError) as exc:
            raise Refused("2", f"ffprobe could not read the encode ({exc})") from exc
        problems, numbers = probe.length_problems(
            info, f"{sheet.slug}.mp4", total_frames
        )
        log.require("1", problems, numbers)
        problems, numbers = probe.format_problems(info, total_frames)
        boxes = probe.top_level_boxes(partial_mp4)
        problems += probe.faststart_problems(boxes)
        numbers["boxes"] = boxes
        log.require("2", problems, numbers)
        try:
            black = probe.blackdetect(partial_mp4)
        except RuntimeError as exc:
            raise Refused("11", str(exc)) from exc
        log.require(
            "11", probe.black_problems(black, total_frames), {"black_intervals": black}
        )

        # Gate 4: the WebVTT file holds the band's headlines, or, for a
        # narrated video, the sentences, tiling the band cues (TILE). Every
        # check_vtt rule is the same for both.
        caption_cues = narration.sentence_cues(spoken) if spoken else out_cues
        partial_vtt.write_text(vtt.write_vtt(caption_cues), encoding="utf-8")
        try:
            parsed = vtt.parse_vtt(partial_vtt.read_text(encoding="utf-8"))
            problems = vtt.check_vtt(parsed, caption_cues, total_frames)
            if spoken:
                problems += narration.tile_problems(parsed, out_cues, spoken)
        except vtt.VttError as exc:
            parsed, problems = [], [str(exc)]
        log.require(
            "4",
            problems,
            {
                "cues": len(parsed),
                "cue_sheet": len(caption_cues),
                "band_cues": len(out_cues),
                "text": "narration" if spoken else "headlines",
            },
        )
        written = {"WebVTT file": partial_vtt}
        if spoken:
            partial_srt.write_text(narration.write_srt(caption_cues), encoding="utf-8")
            partial_txt.write_text(narration.transcript_text(spoken), encoding="utf-8")
            try:
                srt_cues = narration.parse_srt(partial_srt.read_text(encoding="utf-8"))
                problems = narration.srt_problems(srt_cues, parsed)
            except vtt.VttError as exc:
                srt_cues, problems = [], [str(exc)]
            transcript = partial_txt.read_text(encoding="utf-8")
            problems += narration.transcript_problems(transcript, parsed, spoken)
            log.require(
                "G6",
                problems,
                {
                    "srt_cues": len(srt_cues),
                    "transcript_paragraphs": len(transcript[:-1].split("\n\n")),
                    "transcript_words": len(transcript.split()),
                },
            )
            written["SRT file"] = partial_srt
            written["transcript"] = partial_txt
        file_hits = published_text_hits(written, needles)
        if file_hits:
            raise Refused("7", file_hits)

        log.passed("5", ocr.check_captions(partial_mp4, out_cues, found, work))
        scan_numbers, u4_numbers = ocr.scan(
            partial_mp4,
            total_frames,
            needles,
            found,
            work,
            lambda: log.passed(
                "disk-ocr",
                {
                    "free_bytes": check_disk({"work": work.path}, floor),
                    "floor_bytes": floor,
                },
            ),
        )
        scan_numbers["engines"] = [engine.name for engine in found]
        scan_numbers["canary_similarity"] = canary
        scan_numbers["files_scanned"] = list(written)
        log.passed("7", scan_numbers)
        log.passed("U4", u4_numbers)

        final_mp4 = partial_mp4
        if spoken:
            final_mp4 = _narrate(
                probe.expected_audio_streams(capture),
                spoken,
                partial_mp4,
                partial_voiced,
                parsed,
                total_frames,
                work,
                log,
            )
        else:
            # G16, silent: the data says no narration, so no sound at all.
            problems, numbers = probe.format_problems(
                info, total_frames, audio_streams=probe.expected_audio_streams(capture)
            )
            log.require(
                "G16", problems, {"audio_streams": numbers.get("audio_streams")}
            )

        mp4_sha, vtt_sha = sha256(final_mp4), sha256(partial_vtt)
        srt_sha = sha256(partial_srt) if spoken else None
        txt_sha = sha256(partial_txt) if spoken else None
        names = contract.output_names(sheet.slug, mp4_sha)
        made = ("mp4", "vtt", "review") + (("srt", "transcript") if spoken else ())
        finals = {key: out_dir / names[key] for key in made}
        existing = [str(p) for p in finals.values() if p.exists() or p.is_symlink()]
        if existing:
            raise Refused(
                "output",
                f"{existing} already exist; the render never overwrites a finished file",
            )

        partial_review.mkdir()
        focus = {number: cue.focus for number, cue in enumerate(sheet.cues, start=1)}
        wanted = review.contact_frames(out_cues, focus)
        images = encode.extract_frames(
            final_mp4,
            [w["frame"] for w in wanted],
            work.path / "contact",
            scale="960:540",
            ext="jpg",
        )
        contact = []
        for want, cue, image in zip(wanted, out_cues, images):
            name = f"cue-{cue.number:02d}.jpg"
            shutil.move(str(image), partial_review / name)
            contact.append(
                review.ContactFrame(
                    number=cue.number,
                    start_ms=cue.start_ms,
                    end_ms=cue.end_ms,
                    text=cue.text,
                    image=name,
                    focus=want["focus"],
                )
            )
        page = review.review_html(
            sheet=sheet,
            manifest=capture.manifest,
            names=names,
            mp4_sha256=mp4_sha,
            vtt_sha256=vtt_sha,
            contact=contact,
            render_gates=log.gates,
            narration=review.narration_rows(spoken, out_cues) if spoken else None,
            srt_sha256=srt_sha,
            transcript_sha256=txt_sha,
        )
        hits = review.holds_needle(page, needles)
        if hits:
            raise Refused("7", f"the review page would hold needles {hits}")
        log.require(
            "G19",
            identity_problems(final_mp4, names, page),
            {"mp4_sha256": mp4_sha, "narrated": spoken is not None},
        )
        (partial_review / "index.html").write_text(page, encoding="utf-8")
        (partial_review / "gates.json").write_text(
            json.dumps(
                {"render": log.gates, "capture": capture.manifest.gates}, indent=2
            )
            + "\n",
            encoding="utf-8",
        )

        out_dir.mkdir(parents=True, exist_ok=True)
        _place(partial_vtt, finals["vtt"])
        if spoken:
            _place(partial_srt, finals["srt"])
            _place(partial_txt, finals["transcript"])
        _place(final_mp4, finals["mp4"])
        partial_review.rename(finals["review"])
        return RenderResult(
            mp4=finals["mp4"],
            vtt=finals["vtt"],
            review=finals["review"],
            mp4_sha256=mp4_sha,
            vtt_sha256=vtt_sha,
            gates=log.gates,
            srt=finals.get("srt"),
            transcript=finals.get("transcript"),
            srt_sha256=srt_sha,
            transcript_sha256=txt_sha,
        )
    except Refused as exc:
        raise Refused(exc.gate, [*exc.problems, RECAPTURE]) from exc
    finally:
        work.remove(needles_path)
        if started and created:
            work.remove(work.path)
        elif started:
            for child in list(work.path.iterdir()):
                work.remove(child)
