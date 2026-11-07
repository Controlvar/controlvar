"""Dependabot bumps never merge Monday to Friday, 09:00-17:00 Pacific (D297).

``.github/workflows/dependabot-automerge.yml`` is split in two, and this file
pins both halves:

* the ``automerge`` job, on the pull request, decides as before and only
  labels an eligible bump; nothing in it merges, and no step anywhere in the
  workflow arms ``--auto`` (an armed auto-merge fires the moment the last
  check goes green, at any hour) or passes ``--admin``;
* the ``sweep`` job, hourly on a schedule, is the one place that merges. It
  reads the clock in America/Los_Angeles, before it lists anything and again
  before every merge, and merges a labelled, mergeable Dependabot pull
  request at the commit it read only when that clock is outside the window.

The sweep's script is run here under bash with a ``gh`` that records what it
is asked and a ``date`` that answers for a chosen instant through the real
``date`` and the real time-zone data, so daylight saving is the system's, not
a table's. The edges are pinned on both sides of both 2026 daylight-saving
changes, and every boundary is also planted wrong to show the edge table
catches it.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pytest

from backend.tests.unit.infrastructure import _workflow_graph as wg

pytestmark = pytest.mark.unit

WORKFLOW = wg.WORKFLOWS / "dependabot-automerge.yml"
LABEL = "dependabot-automerge"
PACIFIC = ZoneInfo("America/Los_Angeles")

BASH = shutil.which("bash")
JQ = shutil.which("jq")
REAL_DATE = shutil.which("date")

needs_tools = pytest.mark.skipif(
    BASH is None or JQ is None or REAL_DATE is None,
    reason="needs bash, jq and date (every GitHub-hosted Ubuntu runner has them)",
)

# --------------------------------------------------------------------------
# The edge table
# --------------------------------------------------------------------------

#: (the instant, in UTC; the Pacific wall clock it is; what the sweep does).
#: "merges" means it asked gh to merge the one mergeable bump; "waits" means
#: it listed nothing and merged nothing.
EDGES: List[Tuple[str, str, str]] = [
    # An ordinary week, Pacific Standard Time (UTC-8).
    ("2026-11-06T16:59:00Z", "Fri 2026-11-06 08:59 PST", "merges"),
    ("2026-11-06T17:00:00Z", "Fri 2026-11-06 09:00 PST", "waits"),
    ("2026-11-07T00:59:00Z", "Fri 2026-11-06 16:59 PST", "waits"),
    ("2026-11-07T01:00:00Z", "Fri 2026-11-06 17:00 PST", "merges"),
    ("2026-11-07T20:00:00Z", "Sat 2026-11-07 12:00 PST", "merges"),
    ("2026-11-09T07:59:00Z", "Sun 2026-11-08 23:59 PST", "merges"),
    ("2026-11-09T17:00:00Z", "Mon 2026-11-09 09:00 PST", "waits"),
    # Daylight saving ends on Sunday 2026-11-01: the Friday before is PDT
    # (UTC-7), the Monday after is PST (UTC-8). A fixed UTC-8 reads the
    # Friday an hour early; a fixed UTC-7 reads the Monday an hour late.
    ("2026-10-30T15:59:00Z", "Fri 2026-10-30 08:59 PDT", "merges"),
    ("2026-10-30T16:00:00Z", "Fri 2026-10-30 09:00 PDT", "waits"),
    ("2026-10-30T23:59:00Z", "Fri 2026-10-30 16:59 PDT", "waits"),
    ("2026-10-31T00:00:00Z", "Fri 2026-10-30 17:00 PDT", "merges"),
    ("2026-11-02T16:30:00Z", "Mon 2026-11-02 08:30 PST", "merges"),
    ("2026-11-02T17:00:00Z", "Mon 2026-11-02 09:00 PST", "waits"),
    # Daylight saving begins on Sunday 2026-03-08: PST before, PDT after.
    ("2026-03-06T16:30:00Z", "Fri 2026-03-06 08:30 PST", "merges"),
    ("2026-03-06T17:00:00Z", "Fri 2026-03-06 09:00 PST", "waits"),
    ("2026-03-09T15:59:00Z", "Mon 2026-03-09 08:59 PDT", "merges"),
    ("2026-03-09T16:00:00Z", "Mon 2026-03-09 09:00 PDT", "waits"),
]


def _epoch(instant: str) -> int:
    return int(
        datetime.strptime(instant, "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )


def test_the_edge_table_says_what_each_instant_is_in_pacific_time():
    """The table's own arithmetic, checked against the time-zone database, so
    a wrong instant cannot make a wrong boundary look right."""
    wrong = [
        (instant, label, got)
        for instant, label, _ in EDGES
        if (
            got := datetime.fromtimestamp(_epoch(instant), PACIFIC).strftime(
                "%a %Y-%m-%d %H:%M %Z"
            )
        )
        != label
    ]
    assert not wrong, wrong


# --------------------------------------------------------------------------
# Reading the workflow
# --------------------------------------------------------------------------


def _doc() -> Dict[str, Any]:
    return wg.load(WORKFLOW)


def _job(doc: Dict[str, Any], job_id: str) -> Dict[str, Any]:
    jobs = doc.get("jobs") or {}
    assert job_id in jobs, f"{WORKFLOW.name} has no job {job_id!r}: {sorted(jobs)}"
    return jobs[job_id]


def _step(doc: Dict[str, Any], job_id: str, step_id: str) -> Dict[str, Any]:
    steps = _job(doc, job_id).get("steps") or []
    found = [s for s in steps if s.get("id") == step_id]
    assert len(found) == 1, f"{job_id} has {len(found)} steps with id {step_id!r}"
    return found[0]


def _sweep_script(doc: Optional[Dict[str, Any]] = None) -> str:
    return str(_step(doc or _doc(), "sweep", "sweep")["run"])


# --------------------------------------------------------------------------
# A gh that records, a date that answers for a chosen instant
# --------------------------------------------------------------------------

#: `pr list` answers the next listing in $GH_PRS (a list of listings; the
#: last repeats). `api .../issues/N/events` answers N's events from
#: $GH_EVENTS, by default one `labeled` event by github-actions[bot]. Both
#: go through the real jq with the expression the script passed.
_FAKE_GH = """\
import json, os, re, subprocess, sys

args = sys.argv[1:]
with open(os.environ["GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")


def jq(expression, document):
    # gh's --jq prints a null as an empty line, where `jq -r` prints "null".
    gh_like = "(" + expression + ') | if . == null then "" else . end'
    out = subprocess.run(
        [os.environ["GH_JQ"], "-r", gh_like],
        input=json.dumps(document), check=True, capture_output=True, text=True,
    )
    sys.stdout.write(out.stdout)


if args[:2] == ["pr", "list"]:
    counter = os.environ["GH_PRS"] + ".calls"
    n = int(open(counter).read()) if os.path.exists(counter) else 0
    with open(counter, "w") as handle:
        handle.write(str(n + 1))
    listings = json.load(open(os.environ["GH_PRS"]))
    jq(args[args.index("--jq") + 1], listings[min(n, len(listings) - 1)])
    sys.exit(0)
if args[:2] == ["pr", "merge"]:
    # The worst case: a gh that reads its stdin, which inside the sweep's
    # `while read` would swallow the rows still to come.
    sys.stdin.read()
    sys.exit(int(os.environ.get("GH_MERGE_STATUS", "0")))
events = [a for a in args if re.fullmatch(r"repos/[^/]+/[^/]+/issues/[0-9]+/events", a)]
if args[:1] == ["api"] and events:
    number = events[0].split("/")[-2]
    default = [
        {
            "event": "labeled",
            "label": {"name": os.environ["LABEL"]},
            "actor": {"login": "github-actions[bot]"},
        }
    ]
    every = json.load(open(os.environ["GH_EVENTS"])) if os.environ.get("GH_EVENTS") else {}
    jq(args[args.index("--jq") + 1], every.get(number, default))
    sys.exit(0)
if args[:1] == ["api"] and "--method" not in args:
    sys.exit(int(os.environ.get("GH_API_STATUS", "0")))
if args[:1] == ["api"] or args[:2] == ["label", "create"]:
    sys.exit(0)
sys.stderr.write("fake gh: not modelled: %r\\n" % (args,))
sys.exit(2)
"""

#: Records how long it was asked to sleep, and does not.
_FAKE_SLEEP = '#!/bin/bash\necho "$*" >> "$SLEEP_LOG"\n'

#: Each call answers for the next instant in $FAKE_CLOCK (the last repeats),
#: through the real date, with the TZ the caller set (or $FAKE_TZ instead).
_FAKE_DATE = """\
import os, sys

clock = os.environ["FAKE_CLOCK"]
calls = clock + ".calls"
n = int(open(calls).read()) if os.path.exists(calls) else 0
instants = open(clock).read().split()
epoch = instants[min(n, len(instants) - 1)]
with open(calls, "w") as handle:
    handle.write(str(n + 1))
env = dict(os.environ)
if env.get("FAKE_TZ"):
    env["TZ"] = env["FAKE_TZ"]
real = env["REAL_DATE"]
flag = ["-d", "@" + epoch] if env["DATE_FLAVOUR"] == "gnu" else ["-r", epoch]
os.execve(real, [real, *flag, *sys.argv[1:]], env)
"""


def _date_flavour() -> str:
    gnu = subprocess.run(
        [str(REAL_DATE), "-d", "@0", "+%s"], capture_output=True, text=True
    )
    return "gnu" if gnu.returncode == 0 and gnu.stdout.strip() == "0" else "bsd"


def _pr(number: int, state: str = "CLEAN", draft: bool = False) -> Dict[str, Any]:
    return {
        "number": number,
        "url": f"https://github.com/o/r/pull/{number}",
        "isDraft": draft,
        "mergeStateStatus": state,
        "headRefOid": f"{number:040x}",
    }


def _labelled(login: str, label: str = LABEL) -> Dict[str, Any]:
    return {"event": "labeled", "label": {"name": label}, "actor": {"login": login}}


def _unlabelled(login: str, label: str = LABEL) -> Dict[str, Any]:
    return {"event": "unlabeled", "label": {"name": label}, "actor": {"login": login}}


BOT = "github-actions[bot]"


class Run:
    def __init__(
        self, result: subprocess.CompletedProcess, log: Path, sleeps: Path
    ) -> None:
        self.returncode = result.returncode
        self.output = result.stdout + result.stderr
        lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        self.calls: List[List[str]] = [json.loads(line) for line in lines]
        self.sleeps = (
            sleeps.read_text(encoding="utf-8").split() if sleeps.exists() else []
        )

    @property
    def merges(self) -> List[List[str]]:
        return [c for c in self.calls if c[:2] == ["pr", "merge"]]

    @property
    def listings(self) -> List[List[str]]:
        return [c for c in self.calls if c[:2] == ["pr", "list"]]

    @property
    def merged(self) -> List[str]:
        return [m[-1].rsplit("/", 1)[-1] for m in self.merges]

    @property
    def outcome(self) -> str:
        if self.returncode != 0:
            return f"exit {self.returncode}"
        return "merges" if self.merges else "waits"


def _run(
    tmp_path: Path,
    script: str,
    instants: List[int],
    prs: Optional[List[Dict[str, Any]]] = None,
    listings: Optional[List[List[Dict[str, Any]]]] = None,
    events: Optional[Dict[int, List[Dict[str, Any]]]] = None,
    **extra_env: str,
) -> Run:
    for leftover in tmp_path.iterdir():
        if leftover.is_file():
            leftover.unlink()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, source in (("gh", _FAKE_GH), ("date", _FAKE_DATE)):
        program = tmp_path / f"fake_{name}.py"
        program.write_text(source, encoding="utf-8")
        wrapper = bin_dir / name
        wrapper.write_text(
            f'#!/bin/bash\nexec "{sys.executable}" -I "{program}" "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
    sleeper = bin_dir / "sleep"
    sleeper.write_text(_FAKE_SLEEP, encoding="utf-8")
    sleeper.chmod(0o755)
    clock = tmp_path / "clock"
    clock.write_text("\n".join(str(i) for i in instants) + "\n", encoding="utf-8")
    listing = tmp_path / "prs.json"
    if listings is None:
        listings = [prs if prs is not None else [_pr(1)]]
    listing.write_text(json.dumps(listings))
    if events is not None:
        events_file = tmp_path / "events.json"
        events_file.write_text(json.dumps({str(n): e for n, e in events.items()}))
        extra_env = {"GH_EVENTS": str(events_file), **extra_env}
    log = tmp_path / "gh.log"
    sleeps = tmp_path / "sleep.log"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "RUNNER_TEMP": str(tmp_path),
        "GH_LOG": str(log),
        "SLEEP_LOG": str(sleeps),
        "GH_JQ": str(JQ),
        "GH_PRS": str(listing),
        "FAKE_CLOCK": str(clock),
        "REAL_DATE": str(REAL_DATE),
        "DATE_FLAVOUR": _date_flavour(),
        "LABEL": LABEL,
        "GH_TOKEN": "x",
        "GH_REPO": "o/r",
        **extra_env,
    }
    result = subprocess.run(
        [str(BASH), "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        env=env,
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return Run(result, log, sleeps)


def _outcomes(tmp_path: Path, script: str) -> Dict[str, str]:
    return {
        label: _run(tmp_path, script, [_epoch(instant)]).outcome
        for instant, label, _ in EDGES
    }


EXPECTED = {label: outcome for _, label, outcome in EDGES}


# --------------------------------------------------------------------------
# The window, at its edges
# --------------------------------------------------------------------------


@needs_tools
@pytest.mark.parametrize("instant, label, expected", EDGES, ids=[e[1] for e in EDGES])
def test_the_sweep_merges_only_outside_the_window(tmp_path, instant, label, expected):
    run = _run(tmp_path, _sweep_script(), [_epoch(instant)])
    assert run.returncode == 0, run.output
    assert run.outcome == expected, (label, run.output)
    if expected == "waits":
        # It does not even look: the window is decided before the listing.
        assert run.calls == [], run.calls


def _replace_nth(text: str, old: str, new: str, n: int) -> str:
    """`text` with the n-th (1-based) occurrence of `old` replaced."""
    start = -1
    for _ in range(n):
        start = text.find(old, start + 1)
        assert start >= 0, f"the plant's anchor {old!r} is not there {n} times"
    return text[:start] + new + text[start + len(old) :]


#: A boundary planted wrong. Each must move at least one edge.
BOUNDARY_PLANTS = {
    "17:00 inside the window": ("-lt $((17 * 60))", "-le $((17 * 60))"),
    "09:00 outside the window": ("-ge $((9 * 60))", "-gt $((9 * 60))"),
    "a fixed UTC-8 all year": ("TZ=America/Los_Angeles", "TZ=PST8"),
    "a fixed UTC-7 all year": ("TZ=America/Los_Angeles", "TZ=PDT7"),
    "Saturday a weekday": ('[ "$day" -le 5 ]', '[ "$day" -le 6 ]'),
    "the clock read in UTC": ("TZ=America/Los_Angeles", "TZ=UTC"),
}


@needs_tools
@pytest.mark.parametrize("plant", sorted(BOUNDARY_PLANTS))
def test_each_boundary_planted_wrong_moves_an_edge(tmp_path, plant):
    old, new = BOUNDARY_PLANTS[plant]
    script = _sweep_script()
    assert old in script, f"{plant}: the plant's anchor {old!r} is not in the script"
    moved = {
        label: got
        for label, got in _outcomes(tmp_path, script.replace(old, new)).items()
        if got != EXPECTED[label]
    }
    assert moved, f"{plant}: no edge moved"


# --------------------------------------------------------------------------
# What the sweep merges, and how
# --------------------------------------------------------------------------

SATURDAY_NOON = _epoch("2026-11-07T20:00:00Z")
FRIDAY = {
    "08:59:00": _epoch("2026-11-06T16:59:00Z"),
    "08:59:30": _epoch("2026-11-06T16:59:30Z"),
    "09:00:00": _epoch("2026-11-06T17:00:00Z"),
}


@needs_tools
def test_the_sweep_lists_only_open_labelled_dependabot_pull_requests(tmp_path):
    run = _run(tmp_path, _sweep_script(), [SATURDAY_NOON])
    assert run.returncode == 0, run.output
    listing = [c for c in run.calls if c[:2] == ["pr", "list"]]
    assert len(listing) == 1, run.calls
    args = listing[0]
    for flag, value in (
        ("--state", "open"),
        ("--author", "app/dependabot"),
        ("--label", LABEL),
    ):
        assert flag in args and args[args.index(flag) + 1] == value, (flag, args)


@needs_tools
def test_the_sweep_merges_each_mergeable_bump_at_the_commit_it_read(tmp_path):
    prs = [
        _pr(1, "CLEAN"),
        _pr(2, "BLOCKED"),
        _pr(3, "UNSTABLE"),
        _pr(4, "CLEAN", draft=True),
        _pr(5, "HAS_HOOKS"),
        _pr(6, "UNKNOWN"),
        _pr(7, "DIRTY"),
    ]
    run = _run(tmp_path, _sweep_script(), [SATURDAY_NOON], prs)
    assert run.returncode == 0, run.output
    assert run.merges == [
        [
            "pr",
            "merge",
            "--squash",
            "--match-head-commit",
            f"{n:040x}",
            f"https://github.com/o/r/pull/{n}",
        ]
        for n in (1, 3, 5)
    ], run.merges


@needs_tools
def test_a_refused_merge_fails_the_run_and_the_rest_are_still_tried(tmp_path):
    run = _run(
        tmp_path,
        _sweep_script(),
        [SATURDAY_NOON],
        [_pr(1), _pr(2)],
        GH_MERGE_STATUS="1",
    )
    assert run.returncode == 1, run.output
    assert len(run.merges) == 2, run.merges
    assert "::error::" in run.output


@needs_tools
def test_without_the_stdin_redirect_a_merge_swallows_the_rest(tmp_path):
    """The plant for the two tests above: the merge's </dev/null removed, so
    the fake gh, which reads its stdin, takes the rows still to come."""
    script = _sweep_script()
    anchor = '"$url" </dev/null'
    assert anchor in script
    run = _run(
        tmp_path,
        script.replace(anchor, '"$url"'),
        [SATURDAY_NOON],
        [_pr(1), _pr(2), _pr(3)],
    )
    assert len(run.merges) == 1, run.merges


@needs_tools
def test_the_clock_is_read_again_before_each_merge(tmp_path):
    """Read once before the listing, then before each merge: the first bump
    merges at 08:59:30, the second meets 09:00 and waits."""
    instants = [FRIDAY["08:59:00"], FRIDAY["08:59:30"], FRIDAY["09:00:00"]]
    run = _run(tmp_path, _sweep_script(), instants, [_pr(1), _pr(2)])
    assert run.returncode == 0, run.output
    assert [m[-1] for m in run.merges] == ["https://github.com/o/r/pull/1"]


@needs_tools
def test_without_the_second_read_the_clock_test_fails(tmp_path):
    """The plant for the test above: the read inside the loop removed."""
    script = _replace_nth(_sweep_script(), "if in_window; then", "if false; then", n=2)
    instants = [FRIDAY["08:59:00"], FRIDAY["08:59:30"], FRIDAY["09:00:00"]]
    run = _run(tmp_path, script, instants, [_pr(1), _pr(2)])
    assert len(run.merges) == 2, run.merges


@needs_tools
def test_a_zone_that_does_not_resolve_merges_nothing(tmp_path):
    """A runner without the time-zone data reads America/Los_Angeles as UTC,
    silently; the sweep refuses instead of merging in a UTC window."""
    run = _run(
        tmp_path,
        _sweep_script(),
        [SATURDAY_NOON],
        FAKE_TZ="Nowhere/Missing",
    )
    assert run.returncode == 1, run.output
    assert run.calls == [], run.calls
    assert "not PST or PDT" in run.output


# --------------------------------------------------------------------------
# Mergeability GitHub has not computed yet
# --------------------------------------------------------------------------

#: The condition that sends the sweep round again, as the script writes it.
RELIST = """awk -F '\\t' '$4 == "UNKNOWN" { found = 1 } END { exit (found ? 0 : 1) }'"""


@needs_tools
def test_an_unknown_row_makes_the_sweep_wait_and_list_once_more(tmp_path):
    """GitHub computes mergeability when something asks for it, and after main
    moves most rows read UNKNOWN until then; the sweep's own listing is the
    ask, so it waits and reads the answer."""
    run = _run(
        tmp_path,
        _sweep_script(),
        [SATURDAY_NOON],
        listings=[[_pr(1, "UNKNOWN"), _pr(2)], [_pr(1), _pr(2)]],
    )
    assert run.returncode == 0, run.output
    assert len(run.listings) == 2, run.calls
    assert run.sleeps == ["30"], run.sleeps
    assert run.merged == ["1", "2"], run.merges


@needs_tools
def test_no_unknown_row_means_no_wait(tmp_path):
    run = _run(tmp_path, _sweep_script(), [SATURDAY_NOON], [_pr(1), _pr(2, "BLOCKED")])
    assert run.returncode == 0, run.output
    assert len(run.listings) == 1 and run.sleeps == [], (run.calls, run.sleeps)
    assert run.merged == ["1"]


@needs_tools
def test_a_row_still_unknown_after_the_second_listing_waits_for_a_later_run(
    tmp_path,
):
    run = _run(
        tmp_path,
        _sweep_script(),
        [SATURDAY_NOON],
        listings=[[_pr(1, "UNKNOWN")], [_pr(1, "UNKNOWN")]],
    )
    assert run.returncode == 0, run.output
    assert len(run.listings) == 2 and run.sleeps == ["30"], (run.calls, run.sleeps)
    assert run.merges == []


@needs_tools
def test_without_the_second_listing_an_unknown_row_waits_an_hour(tmp_path):
    """The plant for the tests above: the condition never true."""
    script = _sweep_script()
    assert RELIST in script, "the second listing's condition is not in the script"
    run = _run(
        tmp_path,
        script.replace(RELIST, "false"),
        [SATURDAY_NOON],
        listings=[[_pr(1, "UNKNOWN"), _pr(2)], [_pr(1), _pr(2)]],
    )
    assert run.merged == ["2"], run.merges


# --------------------------------------------------------------------------
# The label counts only when this workflow put it there
# --------------------------------------------------------------------------


@needs_tools
@pytest.mark.parametrize(
    "history, merges",
    [
        ([_labelled(BOT)], True),
        ([_labelled("someone")], False),
        ([_labelled(BOT), _unlabelled("someone"), _labelled("someone")], False),
        ([_labelled("someone"), _unlabelled("someone"), _labelled(BOT)], True),
        ([_labelled(BOT), _labelled("someone", label="dependencies")], True),
        ([], False),
        (
            [
                _labelled(BOT),
                _unlabelled(BOT),
                {"event": "labeled", "label": {"name": LABEL}, "actor": None},
            ],
            False,
        ),
    ],
    ids=[
        "added by the workflow",
        "added by someone else",
        "taken off and put back by someone else",
        "put back by the workflow",
        "another label added by someone else",
        "no labelling event at all",
        "put back by an event with no actor",
    ],
)
def test_the_sweep_merges_only_what_this_workflow_labelled(tmp_path, history, merges):
    run = _run(
        tmp_path,
        _sweep_script(),
        [SATURDAY_NOON],
        [_pr(1), _pr(2)],
        events={1: history},
    )
    # #2 carries the default history, labelled by the workflow: the sweep
    # goes on past a refused #1.
    assert run.merged == (["1", "2"] if merges else ["2"]), (run.merges, run.output)
    if merges:
        assert run.returncode == 0, run.output
    else:
        assert run.returncode == 1 and "::error::#1:" in run.output, run.output
    events = [c for c in run.calls if c[:1] == ["api"]]
    assert ["repos/o/r/issues/1/events" in c for c in events] == [True, False]
    assert all("--paginate" in c for c in events), events


@needs_tools
def test_the_labeller_check_planted_out_is_caught(tmp_path):
    """The plant for the test above: the refusal never taken."""
    script = _sweep_script()
    anchor = 'if [ "$labeller" != "github-actions[bot]" ]; then'
    assert anchor in script
    run = _run(
        tmp_path,
        script.replace(anchor, "if false; then"),
        [SATURDAY_NOON],
        [_pr(1)],
        events={1: [_labelled("someone")]},
    )
    assert run.merged == ["1"], run.merges


@needs_tools
@pytest.mark.parametrize("labelled, removes", [("true", True), ("false", False)])
def test_a_bump_the_decide_step_refuses_loses_the_label(tmp_path, labelled, removes):
    step = _step(_doc(), "automerge", "refuse")
    assert step["if"] == "steps.decide.outputs.ok != 'yes'"
    assert step["env"]["LABELLED"] == (
        "${{ contains(github.event.pull_request.labels.*.name, env.LABEL) }}"
    )
    run = _run(
        tmp_path,
        str(step["run"]),
        [SATURDAY_NOON],
        LABELLED=labelled,
        NUMBER="9",
        ECOSYSTEM="pip",
        UPDATE_TYPE="version-update:semver-major",
    )
    assert run.returncode == 0, run.output
    expected = [
        [
            "api",
            "--method",
            "DELETE",
            f"repos/o/r/issues/9/labels/{LABEL}",
            "--silent",
        ]
    ]
    assert run.calls == (expected if removes else []), run.calls


# --------------------------------------------------------------------------
# The pull-request job labels; only the sweep merges
# --------------------------------------------------------------------------


def _run_texts(doc: Dict[str, Any]) -> List[Tuple[str, str]]:
    return [
        (f"{job_id}/{step.get('id') or step.get('name')}", str(step["run"]))
        for job_id, job in (doc.get("jobs") or {}).items()
        for step in job.get("steps") or []
        if step.get("run") is not None
    ]


def merge_problems(doc: Dict[str, Any]) -> List[str]:
    """Every way the workflow could merge other than the sweep's own call."""
    problems = []
    for where, text in _run_texts(doc):
        if "gh pr merge" in text and where != "sweep/sweep":
            problems.append(f"{where}: merges; only the sweep may")
        for flag in ("--auto", "--admin"):
            if flag in text:
                problems.append(f"{where}: passes {flag}")
    return problems


def test_only_the_sweep_merges_and_nothing_arms_auto_merge():
    assert merge_problems(_doc()) == []


@pytest.mark.parametrize(
    "where, text, expected",
    [
        ("automerge", 'gh pr merge --auto --squash "$PR"', "only the sweep may"),
        ("automerge", 'gh pr merge --squash "$PR"', "only the sweep may"),
        ("sweep", 'gh pr merge --auto --squash "$url"', "passes --auto"),
        ("sweep", 'gh pr merge --admin --squash "$url"', "passes --admin"),
    ],
)
def test_a_planted_merge_is_caught(where, text, expected):
    doc = copy.deepcopy(_doc())
    _job(doc, where)["steps"].append({"run": text})
    assert any(expected in p for p in merge_problems(doc)), merge_problems(doc)


def test_each_job_runs_on_its_own_event_only():
    doc = _doc()
    assert set(wg.triggers(doc)) == {"pull_request_target", "schedule"}
    # `github.event.pull_request.draft == false` alone is true on a schedule:
    # GitHub compares null and false as numbers, both 0. So the event is named.
    assert _job(doc, "automerge")["if"] == (
        "github.event_name == 'pull_request_target'"
        " && github.actor == 'dependabot[bot]'"
        " && github.event.pull_request.draft == false"
    )
    assert _job(doc, "sweep")["if"] == "github.event_name == 'schedule'"
    crons = [entry["cron"] for entry in wg.triggers(doc)["schedule"]]
    assert len(crons) == 1, crons
    minute, hour, *rest = crons[0].split()
    # Hourly, at a fixed minute off :00 and :30, when schedules start latest.
    assert hour == "*" and rest == ["*", "*", "*"], crons
    assert minute.isdigit() and int(minute) not in (0, 30), crons


def test_the_label_is_one_name_written_once():
    doc = _doc()
    assert doc.get("env") == {"LABEL": LABEL}
    texts = dict(_run_texts(doc))
    assert '--label "$LABEL"' in texts["sweep/sweep"]
    assert '-f "labels[]=$LABEL"' in texts["automerge/mark"]


def test_the_label_is_added_only_for_an_eligible_bump():
    mark = _step(_doc(), "automerge", "mark")
    assert mark["if"] == "steps.decide.outputs.ok == 'yes'"


#: The label is added through the labels endpoint, which adds a label and
#: posts no text. (`gh pr edit` would be counted as a poster of text by
#: test_qa_templates_public_text.py, which a label is not.)
ADD_LABEL = [
    "api",
    "--method",
    "POST",
    "repos/o/r/issues/9/labels",
    "-f",
    f"labels[]={LABEL}",
    "--silent",
]


@needs_tools
@pytest.mark.parametrize(
    "label_exists, expected",
    [
        ("0", [["api", f"repos/o/r/labels/{LABEL}", "--silent"], ADD_LABEL]),
        (
            "1",
            [
                ["api", f"repos/o/r/labels/{LABEL}", "--silent"],
                ["label", "create", LABEL],
                ADD_LABEL,
            ],
        ),
    ],
)
def test_the_mark_step_creates_the_label_once_then_adds_it(
    tmp_path, label_exists, expected
):
    script = str(_step(_doc(), "automerge", "mark")["run"])
    run = _run(
        tmp_path, script, [SATURDAY_NOON], GH_API_STATUS=label_exists, NUMBER="9"
    )
    assert run.returncode == 0, run.output
    assert len(run.calls) == len(expected), run.calls
    assert [c[: len(e)] for c, e in zip(run.calls, expected)] == expected


# --------------------------------------------------------------------------
# Dependabot opens its pull requests after 17:00 Pacific
# --------------------------------------------------------------------------

DEPENDABOT = wg.REPO_ROOT / ".github" / "dependabot.yml"


def dependabot_schedule_problems(config: Dict[str, Any]) -> List[str]:
    """Every update entry whose schedule could open a pull request inside
    Monday to Friday 09:00-17:00 America/Los_Angeles: each names that zone
    and a time from 17:00 to 23:59, on its weekly or monthly interval."""
    problems = []
    updates = config.get("updates") or []
    if not updates:
        problems.append("no update entries")
    for entry in updates:
        where = f"{entry.get('package-ecosystem')} {entry.get('directory') or entry.get('directories')}"
        schedule = entry.get("schedule") or {}
        if schedule.get("timezone") != "America/Los_Angeles":
            problems.append(f"{where}: timezone {schedule.get('timezone')!r}")
        time = schedule.get("time")
        hour, _, minute = str(time).partition(":")
        if not (
            isinstance(time, str)
            and hour.isdigit()
            and minute.isdigit()
            and len(minute) == 2
            and 17 * 60 <= int(hour) * 60 + int(minute) < 24 * 60
        ):
            problems.append(f"{where}: time {time!r} is not from 17:00 to 23:59")
        interval = schedule.get("interval")
        if interval not in ("weekly", "monthly"):
            problems.append(f"{where}: interval {interval!r}")
        if interval == "weekly" and not schedule.get("day"):
            problems.append(f"{where}: a weekly entry with no day")
    return problems


def _dependabot() -> Dict[str, Any]:
    return wg.load(DEPENDABOT)


def test_every_dependabot_entry_opens_its_pull_requests_after_17_pacific():
    assert dependabot_schedule_problems(_dependabot()) == []


@pytest.mark.parametrize(
    "key, value, expected",
    [
        ("time", "12:00", "is not from 17:00 to 23:59"),
        ("time", "16:59", "is not from 17:00 to 23:59"),
        ("time", "7:30", "is not from 17:00 to 23:59"),
        ("time", None, "is not from 17:00 to 23:59"),
        ("timezone", "UTC", "timezone 'UTC'"),
        ("timezone", None, "timezone None"),
        ("interval", "daily", "interval 'daily'"),
    ],
)
def test_a_planted_schedule_is_caught(key, value, expected):
    config = copy.deepcopy(_dependabot())
    schedule = config["updates"][-1]["schedule"]
    if value is None:
        schedule.pop(key, None)
    else:
        schedule[key] = value
    problems = dependabot_schedule_problems(config)
    assert any(expected in p for p in problems), problems
