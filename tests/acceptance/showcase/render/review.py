"""The review page: what a person watches and checks before a video is approved.

One static HTML file beside its contact-sheet images, readable offline:

* the video itself (``../<name>.mp4``), with the WebVTT file as a track;
* the sha256 of the MP4 and of the WebVTT file (an approval names these);
* the five-item watch checklist;
* a contact sheet: one frame per cue, 0.5 s in, with its time, its text, its
  reading speed, and an empty column for the source that makes the caption
  true (U2 and U6 are checked here, by a person);
* every render gate's numbers and every capture gate's numbers;
* for a narrated video: the voice, the SRT file's and the transcript's
  sha256, the script table (each sentence beside its band headline, with its
  time, slot, clip length, the slack after the clip and its cache key, and a
  button that plays it), and the "Said as" table of the pronunciations used.

It is built only after every gate passed, from frames of the finished video,
and the render refuses to write it if it holds any needle.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from showcase import contract
from showcase.render import templates
from showcase.render.ocr import normalise

CHECKLIST = (
    "Watch it once full-screen, at normal speed, with the sound off.",
    "Watch it once on a phone. Every caption and every number should be readable.",
    "Check that the demo data reads as made up: no real person, company or address.",
    f"Open the end card's address ({templates.SITE}) and check that it is the right site.",
    "Check each caption's claim against the product, and each number against the page it is shown over.",
)
#: A narrated video's checklist: the first item becomes these five (the UX
#: draft's review checks), and the other four stay.
NARRATED_CHECKLIST = (
    "Watch it full-screen with the sound off, as a feed plays it: the headlines alone tell the story.",
    "Watch it with the sound on, without reading the script.",
    "Listen once without watching: Ruth alone tells the story.",
    "Turn the captions on once: every word Ruth says appears, and nothing she does not say.",
    'Play each "Said as" line.',
) + CHECKLIST[1:]
VOICE_LINE = (
    "Amazon Polly, generative engine, {voice} (en-US); 90% pace by one time-stretch"
    " (atempo {tempo}) after synthesis; {gain:g} dB gain; AAC, 48 kHz, mono"
)
#: The slack after a clip below which the script table flags a sentence.
TIGHT_SLACK_MS = 300

#: How long into each cue its contact-sheet frame is taken.
CONTACT_OFFSET_FRAMES = contract.FPS // 2


@dataclass(frozen=True)
class ContactFrame:
    number: int
    start_ms: int
    end_ms: int
    text: str
    image: str
    focus: Optional[contract.Focus]

    @property
    def seconds(self) -> float:
        return (self.end_ms - self.start_ms) / 1000

    @property
    def chars_per_second(self) -> float:
        return len(self.text) / self.seconds if self.seconds else 0.0


def _numbers(value: Any) -> str:
    return html.escape(json.dumps(value, sort_keys=True, ensure_ascii=False))


def _gate_rows(gates: Mapping[str, Mapping[str, Any]]) -> str:
    return "".join(
        f"<tr><td>{html.escape(name)}</td><td>{'ok' if entry.get('ok') else 'NOT OK'}</td>"
        f"<td><code>{_numbers(entry.get('numbers', {}))}</code></td></tr>"
        for name, entry in gates.items()
    )


@dataclass(frozen=True)
class ScriptRow:
    """One narrated sentence, as the script table shows it."""

    number: int
    cue: int
    headline: str
    text: str
    start_ms: int
    end_ms: int
    clip_ms: int
    slot_ms: int
    key: str

    @property
    def slack_ms(self) -> int:
        return self.end_ms - self.start_ms - self.clip_ms


def narration_rows(spoken: contract.Narration, band) -> List[ScriptRow]:
    """The script table's rows: each sentence at its output time, beside its headline."""
    headlines = {cue.number: cue.text for cue in band}
    rows = []
    for s in spoken.sentences:
        start = contract.ms_for_frames(contract.TITLE_FRAMES + s.start_frame)
        end = contract.ms_for_frames(contract.TITLE_FRAMES + s.end_frame)
        rows.append(
            ScriptRow(
                number=s.number,
                cue=s.cue,
                headline=headlines.get(s.cue, ""),
                text=s.text,
                start_ms=start,
                end_ms=end,
                clip_ms=contract.clip_ms(s.samples),
                slot_ms=contract.sentence_slot_ms(s.text, s.samples),
                key=s.key,
            )
        )
    return rows


def said_as(rows: List[ScriptRow]) -> List[Dict[str, Any]]:
    """The pronunciations this video uses: the written form, what Polly is sent,
    and the first sentence that says it."""
    used = []
    for written, said in contract.SPOKEN:
        for row in rows:
            if contract.polly_text(row.text, ((written, said),)) != row.text:
                used.append({"written": written, "said": said, "row": row})
                break
    return used


_PLAYER_JS = (
    "(function(){var v=document.getElementById('video'),stop=null,cur=null;"
    "function mark(r){if(cur){cur.removeAttribute('aria-current');}cur=r;"
    "if(r){r.setAttribute('aria-current','true');}}"
    "document.querySelectorAll('button[data-start]').forEach(function(b){"
    "b.addEventListener('click',function(){v.currentTime=parseFloat(b.dataset.start);"
    "stop=parseFloat(b.dataset.end);mark(document.getElementById(b.dataset.row));v.play();});});"
    "v.addEventListener('timeupdate',function(){if(stop!==null&&v.currentTime>=stop)"
    "{v.pause();stop=null;}});})();"
)


def _script_html(rows: List[ScriptRow], voice: str) -> str:
    body = "".join(
        f'<tr id="s{row.number}">'
        f"<td>{row.number}</td><td>{row.cue}</td>"
        f"<td>{html.escape(row.headline)}</td>"
        f"<td>{html.escape(row.text)}</td>"
        f"<td>{row.start_ms / 1000:.3f}-{row.end_ms / 1000:.3f} s</td>"
        f"<td>{row.clip_ms / 1000:.3f} s / {row.slot_ms / 1000:.3f} s</td>"
        f"<td>{row.slack_ms / 1000:.3f} s{' (tight)' if row.slack_ms < TIGHT_SLACK_MS else ''}</td>"
        f"<td><code>{html.escape(row.key[:12])}</code></td>"
        f'<td><button type="button" data-row="s{row.number}" data-start="{row.start_ms / 1000:.3f}"'
        f' data-end="{row.end_ms / 1000:.3f}">Play sentence {row.number}</button></td>'
        "</tr>"
        for row in rows
    )
    said = said_as(rows)
    said_rows = "".join(
        f"<tr><td>{html.escape(item['written'])}</td><td>{html.escape(item['said'])}</td>"
        f"<td>{item['row'].number}</td>"
        f'<td><button type="button" data-row="s{item["row"].number}"'
        f' data-start="{item["row"].start_ms / 1000:.3f}" data-end="{item["row"].end_ms / 1000:.3f}">'
        f"Play {html.escape(item['written'])}</button></td></tr>"
        for item in said
    )
    said_block = (
        "<h2>Said as</h2><table><tr><th>Written</th><th>Sent to Polly as</th>"
        f"<th>First said in sentence</th><th></th></tr>{said_rows}</table>"
        if said
        else "<h2>Said as</h2><p>No word in this script is in the pronunciation table.</p>"
    )
    return (
        f"<h2>Script ({html.escape(voice)})</h2>"
        "<p>Each sentence is spoken over its headline. Clip / slot: the clip at 90% pace"
        " and the time the TILE rule gives it; slack: the sentence's time after its clip"
        f" (under {TIGHT_SLACK_MS / 1000:.1f} s is marked tight).</p>"
        "<table><tr><th>#</th><th>Cue</th><th>Headline (band)</th><th>Said (captions)</th>"
        "<th>Time</th><th>Clip / slot</th><th>Slack</th><th>Cache key</th><th></th></tr>"
        f"{body}</table>{said_block}"
    )


def stack_facts(stack: Any) -> List[Tuple[str, str]]:
    """The approval record's stack lines: the stack, its project and its images."""
    if not isinstance(stack, dict):
        return [("Stack", "not named")]
    images = stack.get("images") if isinstance(stack.get("images"), dict) else {}
    rows = [
        ("Stack", str(stack.get("name"))),
        ("Compose project", str(stack.get("project"))),
        ("Image tag", str(stack.get("image_tag"))),
    ]
    for service, label in (("api", "API image"), ("frontend", "Dashboard image")):
        entry = images.get(service) if isinstance(images.get(service), dict) else {}
        rows.append((label, f"{entry.get('image')} {entry.get('id')}"))
    return rows


def review_html(
    *,
    sheet: contract.CueSheet,
    manifest: contract.Manifest,
    names: Mapping[str, str],
    mp4_sha256: str,
    vtt_sha256: str,
    contact: List[ContactFrame],
    render_gates: Mapping[str, Mapping[str, Any]],
    narration: Optional[List[ScriptRow]] = None,
    srt_sha256: Optional[str] = None,
    transcript_sha256: Optional[str] = None,
) -> str:
    title = f"{sheet.title} ({sheet.slug}) review"
    voice_line = (
        VOICE_LINE.format(
            voice=manifest.voice, tempo=contract.TEMPO, gain=contract.VOICE_GAIN_DB
        )
        if narration
        else "none (silent)"
    )
    facts = [
        ("Video", names["mp4"]),
        ("Captions", names["vtt"]),
        ("MP4 sha256", mp4_sha256),
        ("WebVTT sha256", vtt_sha256),
    ]
    if narration:
        facts += [
            ("SRT captions", names["srt"]),
            ("SRT sha256", srt_sha256 or ""),
            ("Transcript", names["transcript"]),
            ("Transcript sha256", transcript_sha256 or ""),
        ]
    facts += [
        ("Voice", voice_line),
        ("Version shown", sheet.version),
        ("Ref", manifest.ref),
        ("Commit", manifest.commit),
        ("Profile", manifest.profile),
        *stack_facts(manifest.raw.get("stack")),
        (
            "next (installed / lockfile)",
            f"{manifest.next_version} / {manifest.lock_next_version}",
        ),
        ("Zoom", f"{manifest.zoom:g}"),
        (
            "Length",
            f"{contract.ms_for_frames(contract.video_frames(sheet.duration_ms)) / 1000:.3f} s",
        ),
    ]
    fact_rows = "".join(
        f"<tr><th>{html.escape(k)}</th><td><code>{html.escape(v)}</code></td></tr>"
        for k, v in facts
    )
    checklist = "".join(
        f'<li><label><input type="checkbox"> {html.escape(item)}</label></li>'
        for item in (NARRATED_CHECKLIST if narration else CHECKLIST)
    )
    sheet_rows = "".join(
        "<tr>"
        f"<td>{frame.number}</td>"
        f'<td><img src="{html.escape(frame.image)}" alt="cue {frame.number}" width="480" height="270"></td>'
        f"<td>{frame.start_ms / 1000:.3f}-{frame.end_ms / 1000:.3f} s<br>{frame.seconds:.2f} s, "
        f"{frame.chars_per_second:.1f} characters a second</td>"
        f"<td>{html.escape(frame.text)}</td>"
        f"<td>{'-' if frame.focus is None else html.escape(f'{frame.focus.x:g},{frame.focus.y:g} {frame.focus.w:g}x{frame.focus.h:g}')}</td>"
        "<td></td>"
        "</tr>"
        for frame in contact
    )
    extra = {k: v for k, v in manifest.raw.items() if k not in contract.MANIFEST_KEYS}
    extra_block = (
        f"<h2>Capture measurements</h2><pre>{_numbers(extra)}</pre>" if extra else ""
    )
    css = (
        "body{font-family:-apple-system,system-ui,sans-serif;margin:24px auto;max-width:1100px;padding:0 16px;"
        "color:#0F172A;background:#FFFFFF}"
        "video{width:100%;max-width:1100px;background:#000}"
        "table{border-collapse:collapse;width:100%;margin:12px 0 28px}"
        "th,td{border-bottom:1px solid #E2E8F0;padding:6px 8px;text-align:left;vertical-align:top;font-size:14px}"
        "code,pre{font-family:ui-monospace,Menlo,monospace;font-size:12px;white-space:pre-wrap;word-break:break-all}"
        "img{display:block;max-width:100%;height:auto}"
        "li{margin:6px 0}"
        "tr[aria-current]{font-weight:700;outline:2px solid #0F172A}"
        "button{font:inherit;font-size:13px;padding:4px 8px}"
    )
    script = _script_html(narration, voice_line) if narration else ""
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{css}</style></head><body>"
        f"<h1>{html.escape(title)}</h1>"
        f'<video id="video" controls preload="metadata" src="../{html.escape(names["mp4"])}">'
        f'<track kind="captions" srclang="en" label="English" src="../{html.escape(names["vtt"])}"></video>'
        f"<table>{fact_rows}</table>"
        f"<h2>Before approving</h2><ol>{checklist}</ol>"
        f"{script}"
        "<h2>Contact sheet</h2>"
        "<table><tr><th>#</th><th>Frame (0.5 s in)</th><th>Time</th><th>Caption</th><th>Focus</th>"
        f"<th>Source (doc line or code)</th></tr>{sheet_rows}</table>"
        f"<h2>Render gates</h2><table><tr><th>Gate</th><th></th><th>Numbers</th></tr>{_gate_rows(render_gates)}</table>"
        f"<h2>Capture gates</h2><table><tr><th>Gate</th><th></th><th>Numbers</th></tr>{_gate_rows(manifest.gates)}</table>"
        f"{extra_block}"
        + (f"<script>{_PLAYER_JS}</script>" if narration else "")
        + "</body></html>\n"
    )


def holds_needle(text: str, needles: List[str]) -> List[int]:
    """Which needles (by number) a text holds, as written or with its separators dropped."""
    folded = normalise(text)
    return [
        number
        for number, needle in enumerate(needles, start=1)
        if needle in text or (normalise(needle) and normalise(needle) in folded)
    ]


def contact_frames(
    cues, focus: Dict[int, Optional[contract.Focus]]
) -> List[Dict[str, Any]]:
    """Which output frame each cue's contact-sheet image comes from."""
    return [
        {
            "number": cue.number,
            "frame": cue.start_frame + CONTACT_OFFSET_FRAMES,
            "focus": focus.get(cue.number),
        }
        for cue in cues
    ]
