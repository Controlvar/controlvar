# SPDX-FileCopyrightText: 2026 The Controlvar authors
# SPDX-License-Identifier: Apache-2.0
"""What the synthetic check's runs add up to: its issue, and its freshness.

Two commands, both reading the run history of ``synthetic.yml`` on ``main``
with ``gh`` (read only; posting is the workflow's, with ``--body-file``):

``report``
    Run by ``synthetic.yml`` after the check. It decides what to do with the
    one rolling ``synthetic-failure`` issue for the target, renders the text
    with ``qa_render`` and writes it to ``--out``: ``action`` (``none``,
    ``open``, ``comment`` or ``close``), ``issue`` (the number to comment on or
    close), ``title.txt`` and ``body.md`` (what to post) and ``summary.md``
    (the run's step summary). The rules (``.github/qa-templates/README.md``):

    * a failed run opens the issue when it is the 3rd failed run in a row and
      none is open; while one is open, a failed run comments only when its
      failing step differs from the step the issue or its latest comment names;
    * the first run that passes comments that it recovered and closes it;
    * a run whose check job was skipped (``SYNTH_ENABLED`` not ``true``),
      cancelled, or failed before the check itself ran counts as neither a
      failure nor a pass: it neither breaks a streak nor continues one.

``freshness``
    Run by ``nightly-qa.yml``. It prints the cadence of the scheduled runs in
    the last 48 hours (the largest gap between their creation times) and, when
    the check is enabled, the largest gap between scheduled runs that passed
    all eight steps. That gap is judged against ``max_gap_minutes`` in
    ``scripts/synthetic_freshness.toml``; until that file exists, the job says
    "threshold not yet set" and passes. The threshold is committed only after
    48 hours of measured gaps, at 1.5 times the largest at least. A night
    that is not judged (dark, or no threshold yet) passes even when ``gh``
    cannot read the run history; a judged night fails.

A run passed all eight steps when its check job succeeded and its step named
``ran 8 of 8`` (which runs only when the check reports ``ran 8 of 8``)
succeeded; a run whose check job was skipped never counts as a pass.

Printed text is counts, durations, step numbers and fixed sentences only.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, Callable, List, Mapping, NamedTuple, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qa_render  # a sibling script, not a package

WORKFLOW = "synthetic.yml"
BRANCH = "main"
CHECK_JOB = "Synthetic check (staging)"
CHECK_STEP = "Run the eight steps"
MARKER_STEP = "ran 8 of 8"
TARGET = "staging"
LABEL = "synthetic-failure"
#: Failed runs in a row that open the issue, per target (UX D2).
OPEN_AFTER = {"staging": 3, "production": 2}
BOT_LOGINS = frozenset({"github-actions", "github-actions[bot]", "app/github-actions"})
#: How many runs back the report looks for the start of a streak.
HISTORY_LIMIT = 100

WINDOW = _dt.timedelta(hours=48)
FRESHNESS_LIMIT = 400
THRESHOLD_FILE = Path(__file__).resolve().parent / "synthetic_freshness.toml"

GREEN, RED, NEUTRAL = "green", "red", "neutral"

Gh = Callable[[List[str]], Any]


class GhError(RuntimeError):
    """A ``gh`` call failed; the message names the command, never its output."""


def run_gh(args: List[str]) -> Any:
    """``gh <args>``, its stdout parsed as JSON."""
    try:
        done = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GhError(f"gh {' '.join(args[:2])}: {type(error).__name__}") from None
    if done.returncode != 0:
        raise GhError(f"gh {' '.join(args[:2])} failed (exit {done.returncode})")
    return json.loads(done.stdout or "null")


class Run(NamedTuple):
    run_id: int
    created_at: _dt.datetime
    status: str
    conclusion: str


def parse_time(value: str) -> _dt.datetime:
    when = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return when if when.tzinfo else when.replace(tzinfo=_dt.timezone.utc)


def list_runs(gh: Gh, repo: str, limit: int, event: Optional[str] = None) -> List[Run]:
    """Runs of the synthetic workflow on main, newest first."""
    args = [
        "run",
        "list",
        "--repo",
        repo,
        "--workflow",
        WORKFLOW,
        "--branch",
        BRANCH,
        "--limit",
        str(limit),
        "--json",
        "databaseId,createdAt,status,conclusion",
    ]
    if event:
        args += ["--event", event]
    runs = [
        Run(
            int(item["databaseId"]),
            parse_time(item["createdAt"]),
            item.get("status") or "",
            item.get("conclusion") or "",
        )
        for item in gh(args) or []
    ]
    return sorted(runs, key=lambda run: run.created_at, reverse=True)


def classify_jobs(jobs: Sequence[Mapping[str, Any]]) -> str:
    """GREEN, RED or NEUTRAL, from a run's jobs (``gh run view --json jobs``)."""
    check = [job for job in jobs if job.get("name") == CHECK_JOB]
    if len(check) != 1:
        return NEUTRAL
    steps = {
        step.get("name"): step.get("conclusion") for step in check[0].get("steps") or []
    }
    conclusion = check[0].get("conclusion")
    if conclusion == "success" and steps.get(MARKER_STEP) == "success":
        return GREEN
    if conclusion == "failure" and steps.get(CHECK_STEP) == "failure":
        return RED
    return NEUTRAL


def run_state(gh: Gh, repo: str, run: Run) -> str:
    """A finished run's state; a skipped or unfinished one is NEUTRAL unread."""
    if run.status != "completed" or run.conclusion not in ("success", "failure"):
        return NEUTRAL
    view = gh(["run", "view", str(run.run_id), "--repo", repo, "--json", "jobs"])
    return classify_jobs((view or {}).get("jobs") or [])


def red_streak(gh: Gh, repo: str, runs: Sequence[Run]) -> List[Run]:
    """The failed runs, newest first, back to the last pass; NEUTRAL is passed over."""
    streak: List[Run] = []
    for run in runs:
        state = run_state(gh, repo, run)
        if state == GREEN:
            break
        if state == RED:
            streak.append(run)
    return streak


class OpenIssue(NamedTuple):
    number: int
    step: Optional[int]  # the failing step the issue or its latest comment names


_BODY_STEP = re.compile(r"^Failing step: ([1-8]) of 8\.", re.M)
_COMMENT_STEP = re.compile(r"it is now step ([1-8]) of 8\.")


def _login(item: Mapping[str, Any]) -> str:
    author = item.get("author") or {}
    return str(author.get("login") or "")


def find_open_issue(gh: Gh, repo: str, title: str) -> Optional[OpenIssue]:
    """The open issue the check filed for its target, if there is one."""
    issues = (
        gh(
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
                "50",
                "--json",
                "number,title,author",
            ]
        )
        or []
    )
    ours = [
        int(issue["number"])
        for issue in issues
        if issue.get("title") == title and _login(issue) in BOT_LOGINS
    ]
    if not ours:
        return None
    number = max(ours)
    view = (
        gh(["issue", "view", str(number), "--repo", repo, "--json", "body,comments"])
        or {}
    )
    step: Optional[int] = None
    match = _BODY_STEP.search(str(view.get("body") or ""))
    if match:
        step = int(match.group(1))
    for comment in view.get("comments") or []:
        if _login(comment) not in BOT_LOGINS:
            continue
        match = _COMMENT_STEP.search(str(comment.get("body") or ""))
        if match:
            step = int(match.group(1))
    return OpenIssue(number, step)


def format_date_time(when: _dt.datetime) -> str:
    return when.astimezone(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def format_duration(span: _dt.timedelta) -> str:
    minutes = max(0, int(span.total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


class Decision(NamedTuple):
    action: str  # none | open | comment | close
    issue: Optional[int]
    title: Optional[str]
    body: Optional[str]
    summary: Optional[str]


def decide(
    *,
    current: str,
    step: Optional[str],
    status: Optional[str],
    streak: Sequence[Run],
    issue: Optional[OpenIssue],
    run_link: str,
    now: _dt.datetime,
    target: str = TARGET,
) -> Decision:
    """What to post for this run. ``streak`` is the failed runs before it."""
    if current == RED:
        count = len(streak) + 1
        first = streak[-1].created_at if streak else now
        values = {
            "check": target,
            "count_runs": str(count),
            "date_time": format_date_time(first),
            "run_link": run_link,
            "step": str(step),
            "status": str(status),
        }
        rendered = qa_render.render("synthetic-issue.tmpl", values)
        if issue is None:
            if count >= OPEN_AFTER[target]:
                return Decision(
                    "open", None, rendered.title, rendered.body, rendered.body
                )
            return Decision("none", None, None, None, rendered.body)
        if issue.step != int(str(step)):
            changed = qa_render.render(
                "synthetic-step-changed.tmpl",
                {
                    key: values[key]
                    for key in ("check", "step", "status", "run_link", "count_runs")
                },
            )
            return Decision("comment", issue.number, None, changed.body, rendered.body)
        return Decision("none", issue.number, None, None, rendered.body)
    if current == GREEN and issue is not None:
        first = streak[-1].created_at if streak else now
        recovered = qa_render.render(
            "synthetic-recovered.tmpl",
            {
                "date_time": format_date_time(now),
                "count_runs": str(len(streak)),
                "duration": format_duration(now - first),
                "run_link": run_link,
            },
        )
        return Decision("close", issue.number, None, recovered.body, recovered.body)
    return Decision("none", None, None, None, None)


def current_state(result: str, ran: str, step: str) -> str:
    """This run's state from its check job's result and outputs."""
    if result == "success" and ran == "8":
        return GREEN
    if result == "failure" and re.fullmatch(r"[1-8]", step or ""):
        return RED
    return NEUTRAL


def report(args: argparse.Namespace, env: Mapping[str, str], gh: Gh = run_gh) -> int:
    repo = env["GITHUB_REPOSITORY"]
    run_id = env["GITHUB_RUN_ID"]
    run_link = f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/actions/runs/{run_id}"
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    current = current_state(args.result, args.ran, args.step)
    decision = Decision("none", None, None, None, None)
    if current != NEUTRAL:
        runs = [
            run
            for run in list_runs(gh, repo, HISTORY_LIMIT)
            if str(run.run_id) != run_id
        ]
        streak = red_streak(gh, repo, runs)
        title = qa_render.render_title("synthetic-issue.tmpl", {"check": TARGET})
        issue = find_open_issue(gh, repo, title)
        decision = decide(
            current=current,
            step=args.step,
            status=args.status,
            streak=streak,
            issue=issue,
            run_link=run_link,
            now=_dt.datetime.now(_dt.timezone.utc),
        )
        print(
            f"this run: {current}; failed runs in a row before it: {len(streak)}; "
            f"open issue: {'yes' if issue else 'no'}; action: {decision.action}"
        )
    else:
        print(f"this run: {current}; nothing to post")
    (out / "action").write_text(decision.action + "\n", encoding="ascii")
    if decision.issue is not None:
        (out / "issue").write_text(f"{decision.issue}\n", encoding="ascii")
    if decision.title is not None:
        (out / "title.txt").write_text(decision.title + "\n", encoding="ascii")
    if decision.body is not None:
        (out / "body.md").write_text(decision.body, encoding="ascii")
    if decision.summary is not None:
        (out / "summary.md").write_text(decision.summary, encoding="ascii")
    return 0


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


def largest_gap(
    times: Sequence[_dt.datetime], now: _dt.datetime, window: _dt.timedelta = WINDOW
) -> Optional[_dt.timedelta]:
    """The largest gap between consecutive ``times`` that ends inside the
    window, counting the gap from the last one to ``now``. A gap that starts
    before the window and ends inside it counts whole; the time before the
    first known run does not count (a check enabled an hour ago is not an
    old gap). None when there is no time at all."""
    if not times:
        return None
    start = now - window
    points = sorted(times) + [now]
    gaps = [b - a for a, b in zip(points, points[1:]) if b >= start]
    return max(gaps) if gaps else None


def read_threshold(path: Path = THRESHOLD_FILE) -> Optional[int]:
    """``max_gap_minutes`` from the threshold file; None when it is absent."""
    if not path.exists():
        return None
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    value = data.get("max_gap_minutes")
    if set(data) != {"max_gap_minutes"} or type(value) is not int or value <= 0:
        raise ValueError(
            f"{path.name}: exactly one key, max_gap_minutes, a positive integer"
        )
    return value


def green_times(
    gh: Gh, repo: str, runs: Sequence[Run], start: _dt.datetime
) -> List[_dt.datetime]:
    """Creation times of the runs that passed all eight steps, inside the
    window plus the last one before it (the start of a gap into the window)."""
    found: List[_dt.datetime] = []
    for run in runs:  # newest first
        if run_state(gh, repo, run) != GREEN:
            continue
        found.append(run.created_at)
        if run.created_at < start:
            break
    return found


def freshness(
    enabled: str,
    env: Mapping[str, str],
    gh: Gh = run_gh,
    now: Optional[_dt.datetime] = None,
    threshold_file: Path = THRESHOLD_FILE,
) -> int:
    """Exit status 1 only for a night that is judged (enabled, with a
    threshold) and stale or unreadable, or for a bad SYNTH_ENABLED or
    threshold file. A night that is not judged passes even when the run
    history cannot be read: a transient gh failure is not silence."""
    repo = env["GITHUB_REPOSITORY"]
    now = now or _dt.datetime.now(_dt.timezone.utc)
    start = now - WINDOW
    print("Synthetic check freshness: scheduled runs on main, last 48 h")
    if enabled not in ("", "false", "true"):
        print(
            "::error title=Synthetic check freshness::SYNTH_ENABLED must be true or false"
        )
        return 1
    threshold: Optional[int] = None
    if enabled == "true":
        try:
            threshold = read_threshold(threshold_file)
        except (ValueError, tomllib.TOMLDecodeError) as error:
            print(f"::error title=Synthetic check freshness::{error}")
            return 1
    judged = threshold is not None

    def unreadable(error: GhError) -> int:
        if judged:
            print(
                "::error title=Synthetic check freshness::the run history could "
                "not be read"
            )
            print(f"the run history could not be read ({error})")
            return 1
        print(f"the run history could not be read ({error}): nothing is judged")
        return 0

    try:
        runs = list_runs(gh, repo, FRESHNESS_LIMIT, event="schedule")
    except GhError as error:
        if not judged and enabled == "true":
            print(f"threshold not yet set ({threshold_file.name} is absent)")
        return unreadable(error)
    recent = [run.created_at for run in runs if run.created_at >= start]
    cadence = largest_gap([run.created_at for run in runs], now)
    print(
        f"scheduled runs: {len(recent)}; largest gap between them: "
        + (format_duration(cadence) if cadence is not None else "no run at all")
    )
    if enabled != "true":
        print(
            "SYNTH_ENABLED is not true: the check is dark, so freshness is not judged (disabled)"
        )
        return 0
    try:
        greens = green_times(gh, repo, runs, start)
    except GhError as error:
        return unreadable(error)
    gap = largest_gap(greens, now)
    in_window = sum(1 for when in greens if when >= start)
    print(
        f"runs that passed all eight steps (ran 8 of 8): {in_window}; largest gap: "
        + (format_duration(gap) if gap is not None else "no such run")
    )
    if threshold is None:
        print(
            f"threshold not yet set ({threshold_file.name} is absent): nothing is judged"
        )
        return 0
    limit = _dt.timedelta(minutes=threshold)
    if gap is None or gap > limit:
        print(
            "::error title=Synthetic check freshness::no green synthetic run for "
            "longer than the threshold"
        )
        print(
            f"threshold: {format_duration(limit)}; largest gap: "
            + (format_duration(gap) if gap is not None else "no green run at all")
            + ": stale"
        )
        return 1
    print(
        f"threshold: {format_duration(limit)}; largest gap: {format_duration(gap)}: fresh"
    )
    return 0


def main(
    argv: Optional[List[str]] = None, env: Optional[Mapping[str, str]] = None
) -> int:
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    rep = commands.add_parser("report")
    rep.add_argument("--result", required=True)
    rep.add_argument("--ran", default="")
    rep.add_argument("--step", default="")
    rep.add_argument("--status", default="")
    rep.add_argument("--out", required=True, type=Path)
    commands.add_parser("freshness")
    args = parser.parse_args(argv)
    try:
        if args.command == "report":
            return report(args, env)
        return freshness(
            env.get("SYNTH_ENABLED", ""), env, threshold_file=THRESHOLD_FILE
        )
    except (GhError, qa_render.RenderError) as error:
        print(f"::error title=Synthetic check::{error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
