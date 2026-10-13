"""What a reader of a run sees: a report per guide, and the run's summary.

Per guide, in Markdown and in HTML: a header saying the verdict and whether a
reader following the guide can finish it, the table of steps (what the guide
says to do, what was expected before the run, what was seen, the result) and,
for each FAIL, the expected snapshot and the observed snapshot side by side. A
FAIL whose expected or observed snapshot is missing is refused
(``ReportError``): a report that drops one of the two would leave the reader
comparing against nothing.

The run's summary (``summary.md``, also the job summary): the counts, one row
per guide that ran, the launch walkthroughs (``recordings.toml``: each one's
length and its guide's verdict, so a walkthrough reads PASS only when its guide
passed in the same run), every page of the inventory not covered by a journey
that ran, with its class and reason, and how each flow of #939 is verified,
where a journey not written yet shows as planned in the pull request the
inventory names, never as verified.

The headers are the QA templates (``.github/qa-templates/docs-guide-*.tmpl``
for a guide, ``docs-run-*.tmpl`` for the summary), rendered by
``scripts/qa_render.py``, the one renderer of those templates, because the
workflow posts them: the summary is the run's step summary, and a failing
guide's header is the body of its issue. That needs what a run in this
repository's Actions has: a commit and a link to the run (``RunInfo``). A run
without them (one on a laptop, say) is posted nowhere; its headers say the
same in a plain line of their own.
"""

from __future__ import annotations

import html
import importlib
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from docs_runner import registry
from docs_runner.log import FAIL, NOT_RUN, PASS, STRUCTURAL_ONLY, Record

REPO_ROOT = Path(__file__).resolve().parents[4]

#: The longest snapshot shown inline; the file itself is always complete.
SNAPSHOT_LINES = 200


class ReportError(ValueError):
    """The records cannot be reported honestly."""


@dataclass(frozen=True)
class GuideRun:
    """One journey's outcome in this run."""

    journey: str
    guide: str
    title: str
    stack: str
    records: Tuple[Record, ...] = ()
    #: The journey file was refused before any step ran: why, one line each.
    refused: Tuple[str, ...] = ()
    #: The run itself stopped before the steps (the stack did not come up).
    error: str = ""
    #: Files of this journey the end-of-run scan removed: they held a value the
    #: run made up, kept or signed in for (``redaction.clear``).
    scrubbed: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Verdict:
    word: str  # PASS | FAIL | PARTIAL
    line: str  # "PASS", "FAIL at step 3", "PARTIAL: 2 not run"
    sentence: str
    first_failure: Optional[Record] = None


def verdict(run: GuideRun) -> Verdict:
    if run.refused:
        return Verdict(
            "FAIL",
            "FAIL: the journey file was refused",
            "The runner refused the journey file, so no step ran.",
        )
    if run.error:
        return Verdict(
            "FAIL",
            "FAIL before step 1",
            f"No step ran: {run.error}",
        )
    if run.scrubbed:
        return Verdict(
            "FAIL",
            "FAIL: its files held a value the run kept",
            f"{len(run.scrubbed)} of this journey's files held a value the run made"
            " up, kept or signed in for, so they were removed and the run"
            " directory was not uploaded.",
        )
    if not run.records:
        return Verdict("FAIL", "FAIL: no step ran", "The journey recorded no step.")
    failed = [record for record in run.records if record.result == FAIL]
    if failed:
        first = failed[0]
        return Verdict(
            "FAIL",
            f"FAIL at step {first.step}",
            "A reader following this guide cannot finish it."
            f' First failing step: {first.step}, "{first.heading}".',
            first,
        )
    if not any(record.result == PASS for record in run.records):
        reasons = sorted({record.reason for record in run.records})
        return Verdict(
            "PARTIAL",
            f"PARTIAL: {len(run.records)} not run",
            f"No step of this guide ran here: {len(run.records)} not run"
            f" ({', '.join(reasons)}).",
        )
    not_run = [
        record
        for record in run.records
        if record.result == NOT_RUN and record.reason != registry.DOC_EXAMPLES
    ]
    if not_run:
        reasons = sorted({record.reason for record in not_run})
        return Verdict(
            "PARTIAL",
            f"PARTIAL: {len(not_run)} not run",
            "A reader following this guide can finish the steps that ran;"
            f" {len(not_run)} not run ({', '.join(reasons)}).",
        )
    by_doc_examples = sum(
        1 for record in run.records if record.reason == registry.DOC_EXAMPLES
    )
    tail = (
        f" {by_doc_examples} step(s) are run by Doc Examples instead."
        if by_doc_examples
        else ""
    )
    return Verdict(
        "PASS", "PASS", "A reader following this guide can finish it." + tail
    )


# ---------------------------------------------------------------------------
# The headers: the QA templates, rendered by scripts/qa_render.py
# ---------------------------------------------------------------------------
def qa_render():
    """``scripts/qa_render.py``, the one renderer of ``.github/qa-templates/``."""
    scripts = str(REPO_ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return importlib.import_module("qa_render")


@dataclass(frozen=True)
class RunInfo:
    """The run a report belongs to: its UTC date, its commit, its link."""

    date: str
    sha: str
    run_link: str = ""

    @property
    def published(self) -> bool:
        """True when the templates accept the commit and the link: a CI run."""
        renderer = qa_render()
        try:
            renderer.check_value("date", self.date)
            renderer.check_value("sha", self.sha)
            renderer.check_value("run_link", self.run_link)
        except renderer.RenderError:
            return False
        return True


_FOLD = str.maketrans(
    {
        "\u2014": "-",
        "\u2013": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
    }
)
_NOT_IN_A_HEADING = re.compile(r"[@<>${}`\\]")
_URL_SHAPE = re.compile(r"[a-z][a-z0-9+.-]*://|www\.", re.I)


def heading_value(text: str) -> str:
    """A guide's heading as the templates take one: plain ASCII on one line.

    Dashes and quotes become their ASCII forms, accents are dropped, and the
    characters a template heading refuses become spaces. The guide's own
    heading stays in the table below the header.
    """
    folded = unicodedata.normalize("NFKD", text.translate(_FOLD))
    folded = folded.encode("ascii", "ignore").decode("ascii")
    folded = _URL_SHAPE.sub(" ", _NOT_IN_A_HEADING.sub(" ", folded))
    folded = " ".join(folded.split())[:120].strip()
    return folded or "untitled"


def step_heading(heading: str, step_id: str) -> str:
    """A failing step as a template names it: its heading, then its own id.

    Several steps can share one heading (the docs-site journey's steps all
    point at the home page's), so the heading alone does not say which step
    failed. The id is the label the journey file gives the step, and it is
    kept whole when the heading has to be cut to fit (an id longer than 118
    characters is itself cut).
    """
    label = f" ({step_id})"
    named = heading_value(heading)[: max(120 - len(label), 0)].rstrip() + label
    return named[:120].strip()


def header_template(run: GuideRun, info: RunInfo) -> Tuple[str, Dict[str, str]]:
    """The template of *run*'s header and its values."""
    result = verdict(run)
    ran = [record for record in run.records if record.reason != registry.DOC_EXAMPLES]
    passed = sum(1 for record in ran if record.result == PASS)
    values = {
        "heading_guide": heading_value(run.title),
        "guide_path": run.guide,
        "date": info.date,
        "sha": info.sha,
    }
    if result.word == "PASS":
        return "docs-guide-pass.tmpl", {**values, "count_steps": str(passed)}
    if result.word == "PARTIAL":
        not_run = sum(1 for record in ran if record.result == NOT_RUN)
        return "docs-guide-partial.tmpl", {
            **values,
            "count_not_run": str(not_run),
            "count_steps_pass": str(passed),
            "count_steps": str(len(ran)),
        }
    first = result.first_failure
    return "docs-guide-fail.tmpl", {
        **values,
        "step_number": str(first.step if first is not None else 1),
        "heading_step": (
            step_heading(first.heading, first.step_id)
            if first is not None
            else heading_value(run.title)
        ),
        "run_link": info.run_link,
        "count_steps_pass": str(passed),
        "count_steps": str(max(len(ran), 1)),
    }


def render(name: str, values: Mapping[str, str]) -> str:
    """Template *name* rendered; ReportError when the renderer refuses it."""
    renderer = qa_render()
    try:
        return renderer.render(name, values).body
    except renderer.RenderError as error:
        raise ReportError(f"{name}: {error}") from None


def guide_header(run: GuideRun, info: RunInfo) -> str:
    """The header of *run*'s report, Markdown: a template, or the plain line."""
    if info.published:
        return render(*header_template(run, info))
    result = verdict(run)
    return (
        f"# {run.title} ({run.guide}), {info.date}, commit {info.sha}: {result.line}\n"
        f"\n{result.sentence}\n"
    )


def _note(run: GuideRun, result: Verdict) -> str:
    """What the verdict says that the template header does not."""
    if run.refused or run.error or result.word == "PARTIAL":
        return result.sentence
    if (
        result.word == "PASS"
        and result.sentence != "A reader following this guide can finish it."
    ):
        return result.sentence.removeprefix(
            "A reader following this guide can finish it. "
        )
    return ""


def _header_html(markdown: str) -> str:
    parts = []
    for block in markdown.strip().split("\n\n"):
        text = " ".join(block.split())
        if text.startswith("# "):
            parts.append(f"<h1>{html.escape(text[2:])}</h1>")
        else:
            parts.append(f"<p>{html.escape(text)}</p>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------
@dataclass
class _Side:
    caption: str
    image: str = ""
    texts: List[Tuple[str, str]] = field(default_factory=list)  # (file, contents)


def _read(run_dir: Path, name: str) -> str:
    lines = (run_dir / name).read_text(encoding="utf-8").splitlines()
    if len(lines) > SNAPSHOT_LINES:
        lines = lines[:SNAPSHOT_LINES] + [f"... ({name} has the rest)"]
    return "\n".join(lines)


def _sides(record: Record, run_dir: Path) -> Tuple[_Side, _Side]:
    """The expected and the observed snapshot of a FAIL, or ReportError."""
    where = f"{record.guide} step {record.step} ({record.step_id})"
    if not record.expected_snapshot:
        raise ReportError(f"{where}: FAIL with no expected snapshot")
    if not record.snapshot:
        raise ReportError(f"{where}: FAIL with no observed snapshot")
    expected = _Side(f"Expected: {record.expected}")
    if record.expected_snapshot != STRUCTURAL_ONLY:
        if not (run_dir / record.expected_snapshot).is_file():
            raise ReportError(
                f"{where}: the expected snapshot {record.expected_snapshot} is missing"
            )
        expected.texts.append(
            (record.expected_snapshot, _read(run_dir, record.expected_snapshot))
        )
    else:
        expected.caption = f"Expected (structural only): {record.expected}"
    if not (run_dir / record.snapshot).is_file():
        raise ReportError(
            f"{where}: the observed snapshot {record.snapshot} is missing"
        )
    seen = _Side(f"Seen: {record.observed}")
    if record.snapshot.endswith(".png"):
        seen.image = record.snapshot
        aria = record.snapshot[: -len(".png")] + ".aria.yml"
        if (run_dir / aria).is_file():
            seen.texts.append((aria, _read(run_dir, aria)))
    else:
        seen.texts.append((record.snapshot, _read(run_dir, record.snapshot)))
    return expected, seen


def _side_html(side: _Side) -> str:
    parts = [f"<p><strong>{html.escape(side.caption)}</strong></p>"]
    if side.image:
        name = html.escape(side.image, quote=True)
        parts.append(f'<p><img src="{name}" alt="{name}" width="640"></p>')
    for name, text in side.texts:
        parts.append(
            f"<p><code>{html.escape(name)}</code></p><pre>{html.escape(text)}</pre>"
        )
    return "\n".join(parts)


def _side_by_side(record: Record, run_dir: Path) -> str:
    expected, seen = _sides(record, run_dir)
    return (
        "<table>\n<tr><th>Expected (written before the run)</th><th>Seen</th></tr>\n"
        f'<tr><td valign="top">\n{_side_html(expected)}\n</td>'
        f'<td valign="top">\n{_side_html(seen)}\n</td></tr>\n</table>\n'
        f"<p>Failure, as written before the run: {html.escape(record.failure_signature)}</p>"
    )


# ---------------------------------------------------------------------------
# Per guide
# ---------------------------------------------------------------------------
def _cell(text: str) -> str:
    return html.escape(" ".join(text.split()), quote=False).replace("|", "&#124;")


def _result(record: Record) -> str:
    return f"NOT RUN ({record.reason})" if record.result == NOT_RUN else record.result


WHAT_TO_DO = (
    "What to do: compare the two snapshots. Fix the guide if the product does what"
    " its API reference says, fix the product if the guide does, and decide when"
    " neither says."
)


def guide_markdown(
    run: GuideRun, run_dir: Path, *, date: str, sha: str, run_link: str = ""
) -> str:
    result = verdict(run)
    info = RunInfo(date, sha, run_link)
    lines = guide_header(run, info).rstrip("\n").split("\n") + [""]
    note = _note(run, result) if info.published else ""
    if note:
        lines += [note, ""]
    if run.refused:
        lines += ["The journey file was refused:", ""]
        lines += [f"- {_cell(problem)}" for problem in run.refused]
        return "\n".join(lines) + "\n"
    if run.error:
        return "\n".join(lines) + "\n"
    lines += [
        "| Step | What the guide says to do | Expected (written before the run) | Seen | Result |",
        "|---|---|---|---|---|",
    ]
    for record in run.records:
        seen = _cell(record.observed)
        if record.snapshot:
            seen += f" ([{_cell(record.snapshot)}]({record.snapshot}))"
        lines.append(
            f"| {record.step} | {_cell(record.heading)}: {_cell(record.action)}"
            f" | {_cell(record.expected)} | {seen} | {_result(record)} |"
        )
    failed = [record for record in run.records if record.result == FAIL]
    for record in failed:
        lines += [
            "",
            f'## Step {record.step}, "{_cell(record.heading)}": FAIL',
            "",
            _side_by_side(record, run_dir),
        ]
    if failed:
        lines += ["", WHAT_TO_DO]
    return "\n".join(lines) + "\n"


def guide_html(
    run: GuideRun, run_dir: Path, *, date: str, sha: str, run_link: str = ""
) -> str:
    result = verdict(run)
    info = RunInfo(date, sha, run_link)
    title = f"{run.title} ({run.guide}), {date}, commit {sha}: {result.line}"
    body = [_header_html(guide_header(run, info))]
    note = _note(run, result) if info.published else ""
    if note:
        body.append(f"<p>{html.escape(note)}</p>")
    if run.refused:
        body.append("<p>The journey file was refused:</p><ul>")
        body += [f"<li>{html.escape(problem)}</li>" for problem in run.refused]
        body.append("</ul>")
    elif run.records:
        body.append(
            "<table><tr><th>Step</th><th>What the guide says to do</th>"
            "<th>Expected (written before the run)</th><th>Seen</th><th>Result</th></tr>"
        )
        for record in run.records:
            seen = html.escape(record.observed)
            if record.snapshot:
                name = html.escape(record.snapshot, quote=True)
                seen += f' (<a href="{name}">{name}</a>)'
            body.append(
                f"<tr><td>{record.step}</td>"
                f"<td>{html.escape(record.heading)}: {html.escape(record.action)}</td>"
                f"<td>{html.escape(record.expected)}</td><td>{seen}</td>"
                f"<td>{html.escape(_result(record))}</td></tr>"
            )
        body.append("</table>")
        failed = [record for record in run.records if record.result == FAIL]
        for record in failed:
            body.append(
                f"<h2>Step {record.step}, &quot;{html.escape(record.heading)}&quot;:"
                " FAIL</h2>"
            )
            body.append(_side_by_side(record, run_dir))
        if failed:
            body.append(f"<p>{html.escape(WHAT_TO_DO)}</p>")
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:sans-serif;margin:16px}table{border-collapse:collapse}"
        "td,th{border:1px solid #999;padding:4px;vertical-align:top}"
        "pre{white-space:pre-wrap;max-width:640px}</style></head>\n<body>\n"
        + "\n".join(body)
        + "\n</body></html>\n"
    )


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
def not_covered(
    inventory: Mapping[str, Any], ran: Sequence[GuideRun]
) -> List[Tuple[str, str, str]]:
    """(page, class, reason) for every inventory page no journey covered in this run."""
    ran_ids = {run.journey for run in ran}
    rows = []
    for page, entry in sorted(inventory.get("pages", {}).items()):
        cls = str(entry.get("class", ""))
        reason = str(entry.get("reason", ""))
        if cls == "journey":
            journey = entry.get("journey", "")
            if journey in ran_ids:
                continue
            if entry.get("pending") is True:
                reason = f"journey {journey}: pending, expectations not written yet; {reason}"
            else:
                reason = f"journey {journey}: not run in this run; {reason}"
        rows.append((page, cls, reason))
    return rows


#: What each class of the inventory means for a flow, in a summary cell.
CLASS_MEANING = {
    "journey": "walked in a browser by its journey",
    "exec": "its shell examples run in Doc Examples",
    "workflow": "run by the workflow its entry names",
    "reference": "nothing to walk; the docs-site journey checks it answers with its heading",
    "aws": "NOT RUN: needs an AWS account",
    "device": "NOT RUN: needs a mobile toolchain and a device",
    "external": "NOT RUN: needs a third-party account or service",
}


def _family(cls: str) -> str:
    return "external" if cls.startswith("external:") else cls


def _planned(entry: Mapping[str, Any]) -> str:
    planned = str(entry.get("planned", ""))
    if planned in ("", "unassigned"):
        return "planned, no pull request assigned yet"
    return f"planned in {planned}"


def flow_rows(
    inventory: Mapping[str, Any], verdicts: Mapping[str, Verdict]
) -> Tuple[List[Tuple[str, str, str, str]], Dict[str, List[str]]]:
    """Per flow of #939: (flow, journeys, other pages and classes, in this run).

    And the journeys not written yet, by where they are planned. A planned
    journey is never counted as verified: the flow says it waits on it. Nor is
    a journey that ran with steps not run (PARTIAL): the flow is "partly
    verified", which the summary's count of verified flows leaves out.
    """
    pages = inventory.get("pages", {})
    journey_entry = {
        str(entry.get("journey")): entry
        for entry in pages.values()
        if entry.get("class") == "journey"
    }
    waiting: Dict[str, List[str]] = {}
    rows = []
    for flow in inventory.get("flow", []):
        cells, ran, failed, planned, not_run = [], [], [], [], []
        for journey in flow.get("journeys", []):
            entry = journey_entry.get(journey, {})
            if journey in verdicts:
                cells.append(f"{journey}: {verdicts[journey].line}")
                ran.append(journey)
                if verdicts[journey].word == FAIL:
                    failed.append(journey)
            elif entry.get("pending") is True:
                cells.append(f"{journey}: {_planned(entry)}")
                planned.append(f"{journey} ({_planned(entry)})")
                where = _planned(entry)
                if journey not in waiting.setdefault(where, []):
                    waiting[where].append(journey)
            else:
                cells.append(f"{journey}: not run in this run")
                not_run.append(journey)
        others = []
        for page in flow.get("pages", []):
            entry = pages.get(page, {})
            cls = str(entry.get("class", ""))
            others.append(f"{page} ({cls}: {entry.get('reason', '')})")
        for cls in flow.get("classes", []):
            count = sum(1 for entry in pages.values() if entry.get("class") == cls)
            others.append(
                f"every {cls} page ({count}): {CLASS_MEANING.get(_family(cls), cls)}"
            )
        partial = [j for j in ran if verdicts[j].word == "PARTIAL"]
        if failed:
            state = "FAIL: " + ", ".join(failed)
        elif planned:
            state = "not verified yet: waits on " + ", ".join(planned)
            if ran:
                state = f"partly: {', '.join(ran)} ran; " + state
        elif not_run:
            state = "not verified in this run: " + ", ".join(not_run) + " did not run"
        elif partial:
            verb = "has" if len(partial) == 1 else "have"
            state = f"partly verified: {', '.join(partial)} {verb} steps not run"
            verified = [journey for journey in ran if journey not in partial]
            if verified:
                state += "; verified by " + ", ".join(verified)
        elif ran:
            state = "verified by " + ", ".join(ran)
            if others:
                state += ", and by its pages' classes"
        else:
            state = "by its pages' classes"
        rows.append(
            (str(flow.get("text", "")), "; ".join(cells), "; ".join(others), state)
        )
    return rows, waiting


def summary_header(
    counts: Mapping[str, int], nav: int, missing: int, info: RunInfo, red: bool
) -> str:
    """The summary's first lines: ``docs-run-*.tmpl``, or the plain line."""
    if info.published:
        values = {
            "date": info.date,
            "sha": info.sha,
            "count_pass": str(counts["PASS"]),
            "count_fail": str(counts["FAIL"]),
            "count_partial": str(counts["PARTIAL"]),
            "count_nav": str(nav),
            "count_not_covered": str(missing),
        }
        if red:
            values["run_link"] = info.run_link
        return render("docs-run-red.tmpl" if red else "docs-run-green.tmpl", values)
    word = "RED" if red else "GREEN"
    return (
        f"Docs journeys, {info.date}, commit {info.sha}: {word} (a run outside this"
        " repository's Actions, posted nowhere)\n\n"
        f"Guides: {counts['PASS']} pass, {counts['FAIL']} fail, {counts['PARTIAL']}"
        f" partial, of {nav} pages in the nav. Not covered: {missing} pages, each"
        " listed below with its reason.\n"
    )


def walkthrough_rows(
    registry: Mapping[str, Mapping[str, Any]],
    recorded: Mapping[str, Mapping[str, Any]],
    verdicts: Mapping[str, Verdict],
    present: Callable[[str], bool],
) -> List[Tuple[str, str, str, str]]:
    """(name, guide, length, result) for each walkthrough of ``recordings.toml``.

    *recorded* is what the runner recorded, by name (``Walkthrough.outcome``);
    *present* says whether a file is still in the run directory (the end-of-run
    scan removes a journey's recordings with any file of it that held a value).
    The result is the guide's verdict, never better: a walkthrough reads PASS
    only when its guide passed in the same run.
    """
    rows = []
    for name, entry in registry.items():
        guide = str(entry.get("guide", ""))
        limit = f"at most {entry.get('max_seconds')} s"
        if entry.get("pending"):
            rows.append((name, guide, limit, f"pending: {entry['pending']}"))
            continue
        outcome = recorded.get(name)
        if outcome is None:
            rows.append((name, guide, limit, "not recorded in this run"))
            continue
        journey = str(outcome.get("journey", ""))
        if outcome.get("dropped"):
            rows.append((name, guide, limit, f"dropped: {outcome['dropped']}"))
            continue
        if not outcome.get("file") or not present(str(outcome["file"])):
            rows.append(
                (name, guide, limit, "removed by the end-of-run scan, or not written")
            )
            continue
        result = verdicts.get(journey)
        word = result.line if result is not None else "its guide did not run"
        rows.append(
            (name, guide, f"{outcome.get('seconds')} s, {limit}", f"{word} ({journey})")
        )
    return rows


def summary_markdown(
    runs: Sequence[GuideRun],
    inventory: Mapping[str, Any],
    *,
    date: str,
    sha: str,
    run_link: str = "",
    walkthroughs: Optional[Sequence[Tuple[str, str, str, str]]] = None,
) -> str:
    verdicts: Dict[str, Verdict] = {run.journey: verdict(run) for run in runs}
    counts = dict.fromkeys(("PASS", "FAIL", "PARTIAL"), 0)
    for result in verdicts.values():
        counts[result.word] += 1
    pages = inventory.get("pages", {})
    missing = not_covered(inventory, runs)
    red = bool(counts["FAIL"]) or not runs
    lines = (
        summary_header(
            counts, len(pages), len(missing), RunInfo(date, sha, run_link), red
        )
        .rstrip("\n")
        .split("\n")
    )
    lines.append("")
    if not runs:
        lines += ["No journey ran, so nothing was verified.", ""]
    if runs:
        lines += ["| Guide | Journey | Stack | Result |", "|---|---|---|---|"]
        for run in runs:
            lines.append(
                f"| {_cell(run.title)} ({_cell(run.guide)}) | {_cell(run.journey)}"
                f" | {_cell(run.stack)} | {_cell(verdicts[run.journey].line)} |"
            )
        lines.append("")
    if walkthroughs is not None:
        lines += [
            "## Launch walkthroughs",
            "",
            "Each recording is in the run's artifact as recordings/<name>.webm, and"
            " reads PASS only when its guide passed in this run.",
            "",
            "| Recording | Guide | Length | Result |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| {_cell(name)} | {_cell(guide)} | {_cell(length)} | {_cell(result)} |"
            for name, guide, length, result in walkthroughs
        ]
        lines.append("")
    lines += ["## Not covered in this run", ""]
    if missing:
        lines += ["| Page | Class | Reason |", "|---|---|---|"]
        lines += [
            f"| {_cell(page)} | {_cell(cls)} | {_cell(reason)} |"
            for page, cls, reason in missing
        ]
    else:
        lines.append("Every page in the nav was covered.")
    rows, waiting = flow_rows(inventory, verdicts)
    verified = sum(1 for row in rows if row[3].startswith("verified by"))
    lines += [
        "",
        "## How each flow of #939 is verified",
        "",
        f"{verified} of {len(rows)} flows are verified by journeys that ran in this"
        " run. A journey whose expectations are not written yet is not counted:"
        " its flow shows it as planned, in the pull request the inventory names.",
        "",
        "| Flow | Journeys | Other pages and classes | In this run |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {_cell(text)} | {_cell(journeys)} | {_cell(others)} | {_cell(state)} |"
        for text, journeys, others, state in rows
    ]
    if waiting:
        lines += ["", "Journeys not written yet:", ""]
        lines += [
            f"- {where}: {', '.join(journeys)}"
            for where, journeys in sorted(waiting.items())
        ]
    return "\n".join(lines) + "\n"


_PUBLISHED_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
_PUBLISHED_SHA = re.compile(r"[0-9a-f]{40}")


def published_source(environ: Mapping[str, str]) -> Dict[str, str]:
    """The ref and commit the published site was built from, as the workflow
    named them (``DOCS_JOURNEY_PUBLISHED_REF`` and ``DOCS_JOURNEY_PUBLISHED_SHA``);
    a value that is not a ref's or a commit's characters is recorded as ""."""
    ref = environ.get("DOCS_JOURNEY_PUBLISHED_REF", "")
    sha = environ.get("DOCS_JOURNEY_PUBLISHED_SHA", "")
    return {
        "ref": ref if _PUBLISHED_REF.fullmatch(ref) else "",
        "sha": sha if _PUBLISHED_SHA.fullmatch(sha) else "",
    }


def verdicts_record(
    runs: Sequence[GuideRun],
    info: RunInfo,
    published: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Each journey's verdict and its header's template and values.

    ``verdicts.json`` in the run directory: what ``scripts/docs_journeys_report.py``
    reads, in this run and in the runs before it, to keep each failing guide's
    issue. Empty values when the run is not one the templates accept.
    ``published`` is the ref and commit the published site was built from
    (``published_source``; empty when the run names none): what
    ``scripts/rc_evidence.py`` compares with the release tag.
    """
    journeys: Dict[str, Any] = {}
    for run in runs:
        result = verdict(run)
        name, values = header_template(run, info) if info.published else ("", {})
        first = result.first_failure
        journeys[run.journey] = {
            "guide": run.guide,
            "word": result.word,
            "step": first.step
            if first is not None
            else (1 if result.word == FAIL else 0),
            "template": name,
            "values": values,
        }
    return {
        "date": info.date,
        "sha": info.sha,
        "run_link": info.run_link,
        "published": {
            "ref": (published or {}).get("ref", ""),
            "sha": (published or {}).get("sha", ""),
        },
        "journeys": journeys,
    }
