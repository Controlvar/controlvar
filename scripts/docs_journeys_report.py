# SPDX-FileCopyrightText: 2026 The Controlvar authors
# SPDX-License-Identifier: Apache-2.0
"""What the docs journeys' runs add up to: one rolling issue per failing guide,
and the launch walkthroughs a run keeps.

Two commands read with ``gh`` only, and one reads the run directory:

``deployed``
    Run by ``docs-journeys.yml`` before the journeys: which commit the
    published documentation site was built from. The site is deployed from
    release tags (``docs.yml``), so it lags ``main``; the docs-site journey
    compares it with that commit's ``mkdocs.yml`` and ``docs/``, not with
    ``main``'s. It is the newest deployment to the ``github-pages`` environment,
    by ``created_at``, whose latest status is ``success``. A newer deployment
    does not mark the one before it ``inactive`` here (measured on 2026-10-06:
    the latest status of each of the last eight deployments was ``success``,
    none ``inactive``), so what picks the live one is its date; a newer one
    that failed or is still deploying is passed over. Writes ``sha`` and
    ``ref`` under ``--out``; fails when there is none.

``report``
    Run after the journeys. It reads this run's
``verdicts.json`` files (written by the runner, ``tests/acceptance/docs``: one
from the ``journeys`` job and one from the ``demo`` job, each a ``--verdicts``
file or a ``<dir>/*/verdicts.json`` under ``--verdicts-dir``; a journey in two
of them is refused), the verdicts of the scheduled runs before it on ``main``
(each run uploads its own as the artifacts ``ARTIFACTS`` name, read together the
same way) and the open ``docs-journey-failure``
issues, decides what to post for each guide, renders the text with
``qa_render`` and writes it under ``--out``: one directory per post under
``posts/`` holding ``action`` (``open``, ``comment`` or ``close``), ``issue``
(the number to comment on or close), ``title.txt`` (for ``open``) and
``body.md``. ``gh`` is only read here; posting is the workflow's, with
``--body-file``. The rules (``.github/qa-templates/README.md``):

* a guide that fails opens its issue on the ``OPEN_AFTER``-th scheduled run in
  a row in which it failed, when none is open; one red night can be the site
  or the network for a moment, two in a row are not, and at one run a night a
  third would add a day before anyone hears;
* while its issue is open, a failing guide comments only when its failing step
  differs from the step the issue or its latest comment names;
* the first run in which the guide passes (or is PARTIAL: nothing failed)
  comments its report header and closes the issue;
* a run with no verdicts (the journeys did not start), or one in which the
  guide did not run, counts as neither a failure nor a pass.

Only a scheduled run on ``main`` posts. Any other run (a dispatch, a run on
another branch) decides the same way, as if it were the next scheduled run, and
prints what it would post: a dry run.

``recordings``
    Run by ``docs-journeys.yml`` after the journeys, in every run that records
    (a scheduled run, any run on ``main`` or a release tag, and a dispatch with
    ``record_video``): the presence check that makes T138's "recordings R1-R8
    in that run's artifacts" mechanical. It reads
    ``tests/acceptance/docs/recordings.toml`` and the run directory's
    ``recordings/``, and fails unless every walkthrough R1-R8 has a table that
    is not ``pending``, and each such table's ``recordings/<name>.webm`` is
    there, not empty, a WebM of 1280x720 whose header gives a length above 0
    and at most its ``max_seconds``. A ``pending`` table's file must not be
    there (the change that records it removes the ``pending`` line), and
    every file under ``recordings/`` must be a table's. It prints each file's
    name, size and length, and the problems as annotations: names and numbers
    only.

Text is rendered only from ``docs-journey-issue.tmpl`` (the issue, and the
comment when the failing step changes; its body is the guide's
``docs-guide-fail.tmpl`` header) and ``docs-guide-pass.tmpl`` or
``docs-guide-partial.tmpl`` (the closing comment: the guide's header in the
run that recovered). Printed text is guide paths, step numbers, counts and
fixed sentences only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qa_render  # a sibling script, not a package

REPO_ROOT = Path(__file__).resolve().parents[1]
#: The launch walkthroughs: their names, guides, lengths (QA plan UX D11.4).
RECORDINGS_TOML = REPO_ROOT / "tests" / "acceptance" / "docs" / "recordings.toml"
#: The walkthroughs T138's checklist asks every counting run for.
WALKTHROUGHS = ("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8")
#: A walkthrough's frame: the runner's viewport (``execute.VIEWPORT``).
FRAME = (1280, 720)
WORKFLOW = "docs-journeys.yml"
BRANCH = "main"
LABEL = "docs-journey-failure"
ARTIFACT = "docs-journeys-verdicts"
#: Each job's verdicts artifact: the journeys job's, then the demo job's.
ARTIFACTS = (ARTIFACT, "docs-journeys-demo-verdicts")
ISSUE = "docs-journey-issue.tmpl"
#: Scheduled runs in a row in which a guide failed that open its issue.
OPEN_AFTER = 2
#: How many scheduled runs back the report looks for the start of a streak.
HISTORY_LIMIT = 10
BOT_LOGINS = frozenset({"github-actions", "github-actions[bot]", "app/github-actions"})
FAIL, PASS, PARTIAL = "FAIL", "PASS", "PARTIAL"
WORDS = (FAIL, PASS, PARTIAL)
#: The closing comment's template, by the verdict of the run that recovered.
RECOVERED = {PASS: "docs-guide-pass.tmpl", PARTIAL: "docs-guide-partial.tmpl"}

Gh = Callable[[List[str]], str]


class GhError(RuntimeError):
    """A ``gh`` call failed; the message names the command, never its output."""


class VerdictsError(ValueError):
    """A ``verdicts.json`` that is not what the runner writes."""


def run_gh(args: List[str]) -> str:
    """``gh <args>``, its stdout."""
    try:
        done = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GhError(f"gh {' '.join(args[:2])}: {type(error).__name__}") from None
    if done.returncode != 0:
        raise GhError(f"gh {' '.join(args[:2])} failed (exit {done.returncode})")
    return done.stdout


def gh_json(gh: Gh, args: List[str]) -> Any:
    return json.loads(gh(args) or "null")


# ---------------------------------------------------------------------------
# The published site's source
# ---------------------------------------------------------------------------
ENVIRONMENT = "github-pages"
_SHA = re.compile(r"[0-9a-f]{40}")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")


class DeployedError(RuntimeError):
    """No deployment of the site can be named."""


def deployed(gh: Gh, repo: str) -> tuple:
    """(sha, ref) of the live deployment of the documentation site."""
    deployments = gh_json(
        gh, ["api", f"repos/{repo}/deployments?environment={ENVIRONMENT}&per_page=30"]
    )
    # Newest first by date, not by the order the API lists them in.
    newest_first = sorted(
        deployments or [],
        key=lambda deployment: str(deployment.get("created_at") or ""),
        reverse=True,
    )
    for deployment in newest_first:
        statuses = gh_json(
            gh,
            ["api", f"repos/{repo}/deployments/{deployment['id']}/statuses?per_page=1"],
        )
        if statuses and statuses[0].get("state") == "success":
            sha, ref = str(deployment.get("sha")), str(deployment.get("ref"))
            if not _SHA.fullmatch(sha) or not _REF.fullmatch(ref):
                raise DeployedError("the live deployment names no commit")
            return sha, ref
    raise DeployedError(f"no deployment to {ENVIRONMENT} is live")


def deployed_command(
    args: argparse.Namespace, env: Mapping[str, str], gh: Gh = run_gh
) -> int:
    sha, ref = deployed(gh, env["GITHUB_REPOSITORY"])
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "sha").write_text(sha + "\n", encoding="ascii")
    (args.out / "ref").write_text(ref + "\n", encoding="ascii")
    print(f"the published site was built from {ref} ({sha})")
    return 0


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------
class Verdict(NamedTuple):
    guide: str
    word: str
    step: int
    template: str
    values: Mapping[str, str]


def read_verdicts(path: Path) -> Dict[str, Verdict]:
    """Each journey's verdict in a run's ``verdicts.json``; VerdictsError if malformed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise VerdictsError(f"{path.name}: {type(error).__name__}") from None
    journeys = data.get("journeys") if isinstance(data, dict) else None
    if not isinstance(journeys, dict):
        raise VerdictsError(f"{path.name}: no journeys")
    found: Dict[str, Verdict] = {}
    for journey, entry in journeys.items():
        if not isinstance(entry, dict) or entry.get("word") not in WORDS:
            raise VerdictsError(f"{path.name}: journey {journey!r} has no verdict")
        values = entry.get("values") or {}
        if not isinstance(values, dict):
            raise VerdictsError(f"{path.name}: journey {journey!r}: values")
        step = entry.get("step")
        found[str(journey)] = Verdict(
            str(entry.get("guide", "")),
            entry["word"],
            step if isinstance(step, int) else 0,
            str(entry.get("template", "")),
            {str(k): str(v) for k, v in values.items()},
        )
    return found


def merge_verdicts(parts: Sequence[Mapping[str, Verdict]]) -> Dict[str, Verdict]:
    """One run's verdicts from each job's file; VerdictsError for a journey in two."""
    merged: Dict[str, Verdict] = {}
    for part in parts:
        for journey, verdict in part.items():
            if journey in merged:
                raise VerdictsError(f"journey {journey!r} is in two verdicts files")
            merged[journey] = verdict
    return merged


def verdict_paths(files: Sequence[Path], directory: Optional[Path]) -> List[Path]:
    """The ``--verdicts`` files, then each ``<directory>/*/verdicts.json``."""
    found = list(files)
    if directory is not None:
        found += sorted(directory.glob("*/verdicts.json"))
    if not found:
        raise VerdictsError("no verdicts.json to read")
    return found


class Run(NamedTuple):
    run_id: int
    status: str
    conclusion: str


def scheduled_runs(gh: Gh, repo: str, limit: int = HISTORY_LIMIT) -> List[Run]:
    """The workflow's scheduled runs on main, newest first."""
    items = gh_json(
        gh,
        [
            "run",
            "list",
            "--repo",
            repo,
            "--workflow",
            WORKFLOW,
            "--branch",
            BRANCH,
            "--event",
            "schedule",
            "--limit",
            str(limit),
            "--json",
            "databaseId,status,conclusion,createdAt",
        ],
    )
    items = sorted(
        items or [], key=lambda item: item.get("createdAt", ""), reverse=True
    )
    return [
        Run(
            int(item["databaseId"]),
            item.get("status") or "",
            item.get("conclusion") or "",
        )
        for item in items
    ]


def run_verdicts(
    gh: Gh, repo: str, run: Run, scratch: Path
) -> Optional[Dict[str, Verdict]]:
    """A finished run's verdicts, every job's together, or None when it left
    none (or is unfinished, or one of them cannot be read)."""
    if run.status != "completed":
        return None
    listing = gh_json(
        gh, ["api", f"repos/{repo}/actions/runs/{run.run_id}/artifacts?per_page=100"]
    )
    names = {
        artifact.get("name")
        for artifact in (listing or {}).get("artifacts", [])
        if not artifact.get("expired")
    }
    present = [name for name in ARTIFACTS if name in names]
    if not present:
        return None
    parts = []
    for name in present:
        target = scratch / str(run.run_id) / name
        gh(
            [
                "run",
                "download",
                str(run.run_id),
                "--repo",
                repo,
                "--name",
                name,
                "--dir",
                str(target),
            ]
        )
        try:
            parts.append(read_verdicts(target / "verdicts.json"))
        except VerdictsError:
            return None
    try:
        return merge_verdicts(parts)
    except VerdictsError:
        return None


def failed_in_a_row(
    journey: str, history: Sequence[Optional[Mapping[str, Verdict]]]
) -> int:
    """Runs before this one, newest first, in which *journey* failed, back to
    the last one in which it did not; a run without it is passed over."""
    count = 0
    for verdicts in history:
        if verdicts is None or journey not in verdicts:
            continue
        if verdicts[journey].word != FAIL:
            break
        count += 1
    return count


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------
class OpenIssue(NamedTuple):
    number: int
    guide: str
    step: Optional[int]  # the failing step the issue or its latest comment names


_TITLE = re.compile(r"^Docs journey \((?P<guide>[^()\s]+)\) is red$")
_STEP = re.compile(r": FAIL at step ([0-9]+)$", re.M)


def _login(item: Mapping[str, Any]) -> str:
    author = item.get("author") or {}
    return str(author.get("login") or "")


def _named_step(text: str) -> Optional[int]:
    first = text.lstrip().split("\n", 1)[0]
    match = _STEP.search(first)
    return int(match.group(1)) if match else None


def open_issues(gh: Gh, repo: str) -> Dict[str, OpenIssue]:
    """The open issues this workflow filed, by guide path (the newest per guide)."""
    issues = gh_json(
        gh,
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--label",
            LABEL,
            "--limit",
            "100",
            "--json",
            "number,title,author",
        ],
    )
    found: Dict[str, OpenIssue] = {}
    for issue in sorted(issues or [], key=lambda item: int(item["number"])):
        match = _TITLE.match(str(issue.get("title") or ""))
        if not match or _login(issue) not in BOT_LOGINS:
            continue
        number = int(issue["number"])
        view = (
            gh_json(
                gh,
                [
                    "issue",
                    "view",
                    str(number),
                    "--repo",
                    repo,
                    "--json",
                    "body,comments",
                ],
            )
            or {}
        )
        step = _named_step(str(view.get("body") or ""))
        for comment in view.get("comments") or []:
            if _login(comment) in BOT_LOGINS:
                named = _named_step(str(comment.get("body") or ""))
                if named is not None:
                    step = named
        found[match.group("guide")] = OpenIssue(number, match.group("guide"), step)
    return found


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------
class Post(NamedTuple):
    action: str  # open | comment | close
    guide: str
    issue: Optional[int]
    title: Optional[str]
    body: str


class Line(NamedTuple):
    """One guide's decision, as printed."""

    guide: str
    text: str


def decide(
    current: Mapping[str, Verdict],
    history: Sequence[Optional[Mapping[str, Verdict]]],
    issues: Mapping[str, OpenIssue],
) -> List[Post | Line]:
    """What to post for this run, guide by guide; ``Line`` for nothing."""
    out: List[Post | Line] = []
    for journey in sorted(current):
        verdict = current[journey]
        issue = issues.get(verdict.guide)
        if verdict.word == FAIL:
            if verdict.template != "docs-guide-fail.tmpl":
                raise VerdictsError(f"{journey}: a FAIL without its header's values")
            rendered = qa_render.render(ISSUE, verdict.values)
            if issue is None:
                streak = failed_in_a_row(journey, history) + 1
                if streak >= OPEN_AFTER:
                    out.append(
                        Post("open", verdict.guide, None, rendered.title, rendered.body)
                    )
                else:
                    out.append(
                        Line(
                            verdict.guide,
                            f"FAIL at step {verdict.step}; failed {streak} scheduled"
                            f" run(s) in a row; the issue opens at {OPEN_AFTER}",
                        )
                    )
            elif issue.step != verdict.step:
                out.append(
                    Post("comment", verdict.guide, issue.number, None, rendered.body)
                )
            else:
                out.append(
                    Line(
                        verdict.guide,
                        f"FAIL at step {verdict.step}, as issue #{issue.number} says",
                    )
                )
        elif issue is not None:
            template = RECOVERED[verdict.word]
            if verdict.template != template:
                raise VerdictsError(
                    f"{journey}: a {verdict.word} without its header's values"
                )
            body = qa_render.render(template, verdict.values).body
            out.append(Post("close", verdict.guide, issue.number, None, body))
        else:
            out.append(Line(verdict.guide, f"{verdict.word}; no open issue"))
    return out


def describe(item: Post | Line) -> str:
    if isinstance(item, Line):
        return f"{item.guide}: {item.text}"
    if item.action == "open":
        return f"{item.guide}: open its issue"
    if item.action == "comment":
        return f"{item.guide}: comment on issue #{item.issue}: the failing step changed"
    return (
        f"{item.guide}: comment its passing header on issue #{item.issue} and close it"
    )


def write_posts(items: Sequence[Post | Line], out: Path) -> int:
    posts = out / "posts"
    posts.mkdir(parents=True, exist_ok=True)
    count = 0
    for item in items:
        if not isinstance(item, Post):
            continue
        count += 1
        target = posts / f"{count:02d}"
        target.mkdir()
        (target / "action").write_text(item.action + "\n", encoding="ascii")
        if item.issue is not None:
            (target / "issue").write_text(f"{item.issue}\n", encoding="ascii")
        if item.title is not None:
            (target / "title.txt").write_text(item.title + "\n", encoding="ascii")
        (target / "body.md").write_text(item.body, encoding="ascii")
    return count


def report(args: argparse.Namespace, env: Mapping[str, str], gh: Gh = run_gh) -> int:
    repo = env["GITHUB_REPOSITORY"]
    this_run = env.get("GITHUB_RUN_ID", "")
    counted = args.event == "schedule" and args.ref == f"refs/heads/{BRANCH}"
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    (out / "posts").mkdir(exist_ok=True)
    files = args.verdicts if isinstance(args.verdicts, list) else [args.verdicts]
    paths = verdict_paths(
        [path for path in files if path is not None],
        getattr(args, "verdicts_dir", None),
    )
    current = merge_verdicts([read_verdicts(path) for path in paths])
    history: List[Optional[Dict[str, Verdict]]] = []
    if any(verdict.word == FAIL for verdict in current.values()):
        with tempfile.TemporaryDirectory() as scratch:
            for run in scheduled_runs(gh, repo):
                if str(run.run_id) == this_run:
                    continue
                history.append(run_verdicts(gh, repo, run, Path(scratch)))
    issues = open_issues(gh, repo)
    items = decide(current, history, issues)
    posts = write_posts(items, out) if counted else 0
    if counted:
        print(f"a scheduled run on {BRANCH}: {posts} post(s)")
    else:
        print(
            f"dry run: a {args.event} run on {args.ref} posts nothing; a scheduled run"
            f" on {BRANCH} with these results would do this"
        )
    print(
        f"guides in this run: {len(current)}; scheduled runs read before it:"
        f" {len(history)}; open issues: {len(issues)}"
    )
    for item in items:
        print(("would: " if not counted else "") + describe(item))
    return 0


# ---------------------------------------------------------------------------
# recordings: the presence check
# ---------------------------------------------------------------------------
class WebmError(ValueError):
    """The bytes are not a WebM file this check can read."""


_EBML = 0x1A45DFA3
_DOCTYPE = 0x4282
_SEGMENT = 0x18538067
_INFO = 0x1549A966
_TIMESTAMP_SCALE = 0x2AD7B1
_DURATION = 0x4489
_TRACKS = 0x1654AE6B
_TRACK_ENTRY = 0xAE
_VIDEO = 0xE0
_PIXEL_WIDTH = 0xB0
_PIXEL_HEIGHT = 0xBA
_CLUSTER = 0x1F43B675


def _vint(data: bytes, pos: int, marker: bool) -> tuple:
    """(value, length, unknown) of the EBML variable-length integer at *pos*.

    An element ID keeps its length marker (*marker*); a size drops it, and a
    size of all ones is "unknown" (the element runs to its parent's end).
    """
    if pos >= len(data):
        raise WebmError("the file is cut short")
    first = data[pos]
    if first == 0:
        raise WebmError("an EBML number longer than 8 bytes")
    length, mask = 1, 0x80
    while not first & mask:
        mask >>= 1
        length += 1
    if pos + length > len(data):
        raise WebmError("the file is cut short")
    value = first if marker else first & (mask - 1)
    for byte in data[pos + 1 : pos + length]:
        value = (value << 8) | byte
    unknown = not marker and value == (1 << (7 * length)) - 1
    return value, length, unknown


def _children(data: bytes, start: int, end: int):
    """Each (id, data start, data end) element between *start* and *end*."""
    pos = start
    while pos < end:
        element, length, _ = _vint(data, pos, True)
        pos += length
        size, length, unknown = _vint(data, pos, False)
        pos += length
        stop = end if unknown else pos + size
        if stop > len(data):
            raise WebmError("the file is cut short")
        yield element, pos, stop
        pos = stop


def _number(data: bytes, start: int, end: int) -> int:
    return int.from_bytes(data[start:end], "big")


def webm_facts(data: bytes) -> tuple:
    """(seconds, width, height) from a WebM file's header; WebmError if it has none.

    The length is the Segment Info's Duration times its timestamp scale (in
    nanoseconds, 1 ms when absent); the frame is the first video track's.
    """
    if not data.startswith(_EBML.to_bytes(4, "big")):
        raise WebmError("no EBML header")
    elements = _children(data, 0, len(data))
    first = next(elements, None)
    if first is None or first[0] != _EBML:
        raise WebmError("no EBML header")
    doc_type = b""
    for element, start, end in _children(data, first[1], first[2]):
        if element == _DOCTYPE:
            doc_type = data[start:end].rstrip(b"\0")
    if doc_type != b"webm":
        raise WebmError("not a WebM document")
    segment = next((e for e in elements if e[0] == _SEGMENT), None)
    if segment is None:
        raise WebmError("no segment")
    scale, duration, frame = 1_000_000, None, None
    for element, start, end in _children(data, segment[1], segment[2]):
        if element == _INFO:
            for child, c_start, c_end in _children(data, start, end):
                if child == _TIMESTAMP_SCALE:
                    scale = _number(data, c_start, c_end)
                elif child == _DURATION:
                    width = c_end - c_start
                    if width not in (4, 8):
                        raise WebmError("a duration that is not a float")
                    (duration,) = struct.unpack(
                        ">f" if width == 4 else ">d", data[c_start:c_end]
                    )
        elif element == _TRACKS and frame is None:
            for child, c_start, c_end in _children(data, start, end):
                if child != _TRACK_ENTRY or frame is not None:
                    continue
                for part, p_start, p_end in _children(data, c_start, c_end):
                    if part != _VIDEO:
                        continue
                    sizes = {
                        kind: _number(data, k_start, k_end)
                        for kind, k_start, k_end in _children(data, p_start, p_end)
                        if kind in (_PIXEL_WIDTH, _PIXEL_HEIGHT)
                    }
                    if len(sizes) == 2:
                        frame = (sizes[_PIXEL_WIDTH], sizes[_PIXEL_HEIGHT])
        elif element == _CLUSTER:
            break
        if duration is not None and frame is not None:
            break
    if duration is None:
        raise WebmError("no duration in its header")
    if frame is None:
        raise WebmError("no video track")
    return duration * scale / 1e9, frame[0], frame[1]


def recording_problems(
    registry: Mapping[str, Mapping[str, Any]], run_dir: Path
) -> tuple:
    """(lines to print, problems) for the run directory's walkthroughs."""
    lines: List[str] = []
    problems: List[str] = []
    for number in WALKTHROUGHS:
        tables = [e for e in registry.values() if e.get("walkthrough") == number]
        if not [table for table in tables if not table.get("pending")]:
            problems.append(
                f"walkthrough {number} has no recording in recordings.toml that is"
                " not pending"
            )
    folder = run_dir / "recordings"
    for name, entry in registry.items():
        path = folder / f"{name}.webm"
        if entry.get("pending"):
            if path.exists():
                problems.append(
                    f"{name} is recorded, but recordings.toml marks it pending;"
                    " remove its pending line"
                )
            else:
                lines.append(f"{name}: pending")
            continue
        if not path.is_file():
            problems.append(f"{name} is missing from recordings/")
            continue
        data = path.read_bytes()
        if not data:
            problems.append(f"{name} is empty")
            continue
        try:
            seconds, width, height = webm_facts(data)
        except WebmError as error:
            problems.append(
                f"{name} is not a WebM recording this check can read: {error}"
            )
            continue
        limit = entry.get("max_seconds")
        lines.append(
            f"{name}: {len(data)} bytes, {seconds:.1f} s of at most {limit} s,"
            f" {width}x{height}"
        )
        if (width, height) != FRAME:
            problems.append(f"{name} is {width}x{height}, not {FRAME[0]}x{FRAME[1]}")
        if not seconds > 0:
            problems.append(f"{name} has no length")
        elif not isinstance(limit, (int, float)) or seconds > limit:
            problems.append(f"{name} is {seconds:.1f} s, longer than {limit} s")
    if folder.is_dir():
        for path in sorted(folder.iterdir()):
            if path.suffix != ".webm" or path.stem not in registry:
                problems.append(
                    f"recordings/{path.name} is not a walkthrough of recordings.toml"
                )
    return lines, problems


def recordings_command(args: argparse.Namespace) -> int:
    try:
        registry = tomllib.loads(args.registry.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        print(f"::error title=Docs journeys recordings::{args.registry.name}: {error}")
        return 1
    lines, problems = recording_problems(registry, args.run_dir)
    for line in lines:
        print(line)
    required = sum(1 for entry in registry.values() if not entry.get("pending"))
    print(
        f"walkthroughs: {required} required, {len(registry) - required} pending,"
        f" {len(problems)} problem(s)"
    )
    for problem in problems:
        print(f"::error title=Docs journeys recordings::{problem}")
    return 1 if problems else 0


def main(
    argv: Optional[List[str]] = None,
    env: Optional[Mapping[str, str]] = None,
    gh: Optional[Gh] = None,
) -> int:
    env = os.environ if env is None else env
    gh = run_gh if gh is None else gh
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    rep = commands.add_parser("report")
    rep.add_argument("--verdicts", type=Path, action="append", default=[])
    rep.add_argument("--verdicts-dir", type=Path)
    rep.add_argument("--event", required=True)
    rep.add_argument("--ref", required=True)
    rep.add_argument("--out", required=True, type=Path)
    dep = commands.add_parser("deployed")
    dep.add_argument("--out", required=True, type=Path)
    rec = commands.add_parser("recordings")
    rec.add_argument("--run-dir", required=True, type=Path)
    rec.add_argument("--registry", type=Path, default=RECORDINGS_TOML)
    args = parser.parse_args(argv)
    if args.command == "recordings":
        return recordings_command(args)
    try:
        if args.command == "deployed":
            return deployed_command(args, env, gh)
        return report(args, env, gh)
    except (GhError, DeployedError, VerdictsError, qa_render.RenderError) as error:
        print(f"::error title=Docs journeys::{error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
