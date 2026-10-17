"""The synthetic check's rolling issue and its freshness, from run history.

``scripts/synthetic_report.py`` reads the history of ``synthetic.yml`` on
``main`` (through a ``gh`` stand-in here) and decides:

* ``report``: one rolling ``synthetic-failure`` issue per target, opened on the
  3rd failed run in a row, a comment only when the failing step changes, a
  recovery comment that closes it. A dark run (check job skipped because
  ``SYNTH_ENABLED`` is not ``true``) neither breaks a failure streak nor
  continues one;
* ``freshness``: the largest gap between scheduled runs that passed all eight
  steps, judged against a committed threshold; with no threshold file it says
  "threshold not yet set" and passes, and a dark run never counts as green.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import Dict, List, Optional

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import synthetic_report as sr

NOW = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.timezone.utc)
REPO = "Controlvar/controlvar"
RUN_LINK = "https://github.com/Controlvar/controlvar/actions/runs/37456943999"
ENV = {"GITHUB_REPOSITORY": REPO, "GITHUB_RUN_ID": "37456943999"}
TITLE = "Synthetic check (staging) is red"
BOT = {"login": "github-actions"}

# What a run's jobs look like, by what happened to it.
SKIPPED = "dark"  # SYNTH_ENABLED not true: every job skipped


def jobs_of(kind: str) -> List[dict]:
    """The jobs list ``gh run view --json jobs`` returns for a run of kind."""
    if kind == "green":
        steps = [("Run the eight steps", "success"), ("ran 8 of 8", "success")]
        return [_job("success", steps), _report()]
    if kind == "red":
        steps = [("Run the eight steps", "failure"), ("ran 8 of 8", "skipped")]
        return [_job("failure", steps), _report()]
    if kind == "green-report-failed":  # the check passed, posting failed
        steps = [("Run the eight steps", "success"), ("ran 8 of 8", "success")]
        return [_job("success", steps), _report("failure")]
    if kind == "success-without-marker":
        return [
            _job(
                "success",
                [("Run the eight steps", "success"), ("ran 8 of 8", "skipped")],
            )
        ]
    if kind == "checkout-failed":
        steps = [
            ("Run actions/checkout@v7", "failure"),
            ("Run the eight steps", "skipped"),
        ]
        return [_job("failure", steps), _report("skipped")]
    if kind == SKIPPED:
        return [
            _job("skipped", []),
            _report("skipped"),
            {
                "name": "SYNTH_ENABLED is true or false",
                "conclusion": "skipped",
                "steps": [],
            },
        ]
    raise AssertionError(kind)


def _job(conclusion: str, steps) -> dict:
    return {
        "name": sr.CHECK_JOB,
        "conclusion": conclusion,
        "steps": [{"name": name, "conclusion": c} for name, c in steps],
    }


def _report(conclusion: str = "success") -> dict:
    return {"name": "Synthetic check issue", "conclusion": conclusion, "steps": []}


RUN_CONCLUSION = {
    "green": "success",
    "red": "failure",
    "green-report-failed": "failure",
    "success-without-marker": "success",
    "checkout-failed": "failure",
    SKIPPED: "skipped",
}


class FakeGh:
    """A stand-in for ``gh``: a run history (newest first) and the open issues."""

    def __init__(
        self,
        kinds: List[str],
        issues: Optional[List[dict]] = None,
        views: Optional[Dict[int, dict]] = None,
        spacing_min: int = 30,
        start: dt.datetime = NOW,
    ):
        self.runs = []
        for i, kind in enumerate(kinds):
            created = start - dt.timedelta(minutes=spacing_min * (i + 1))
            self.runs.append((1000 + i, created, kind))
        self.issues = issues or []
        self.views = views or {}
        self.calls: List[List[str]] = []

    @classmethod
    def at(cls, timed: List[tuple], **kwargs) -> "FakeGh":
        """Runs at explicit times: (hours before NOW, kind)."""
        gh = cls([], **kwargs)
        gh.runs = [
            (2000 + i, NOW - dt.timedelta(hours=h), kind)
            for i, (h, kind) in enumerate(timed)
        ]
        return gh

    def __call__(self, args: List[str]):
        self.calls.append(args)
        if args[:2] == ["run", "list"]:
            assert args[args.index("--workflow") + 1] == "synthetic.yml"
            assert args[args.index("--branch") + 1] == "main"
            return [
                {
                    "databaseId": run_id,
                    "createdAt": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "status": "completed",
                    "conclusion": RUN_CONCLUSION[kind],
                }
                for run_id, created, kind in self.runs
            ]
        if args[:2] == ["run", "view"]:
            kind = next(k for run_id, _, k in self.runs if str(run_id) == args[2])
            return {"jobs": jobs_of(kind)}
        if args[:2] == ["issue", "list"]:
            return self.issues
        if args[:2] == ["issue", "view"]:
            return self.views[int(args[2])]
        raise AssertionError(args)

    def viewed(self) -> List[str]:
        return [args[2] for args in self.calls if args[:2] == ["run", "view"]]


# ---------------------------------------------------------------------------
# Classifying a run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind, state",
    [
        ("green", sr.GREEN),
        ("red", sr.RED),
        ("green-report-failed", sr.GREEN),
        ("success-without-marker", sr.NEUTRAL),
        ("checkout-failed", sr.NEUTRAL),
        (SKIPPED, sr.NEUTRAL),
    ],
)
def test_a_run_is_green_only_with_the_marker_and_red_only_when_the_check_failed(
    kind, state
):
    assert sr.classify_jobs(jobs_of(kind)) == state


def test_a_dark_run_is_neutral_without_reading_its_jobs():
    gh = FakeGh([SKIPPED])
    run = sr.list_runs(gh, REPO, 10)[0]
    assert sr.run_state(gh, REPO, run) == sr.NEUTRAL
    assert gh.viewed() == []


@pytest.mark.parametrize(
    "result, ran, step, state",
    [
        ("success", "8", "", sr.GREEN),
        ("success", "7", "", sr.NEUTRAL),
        ("failure", "3", "4", sr.RED),
        ("failure", "", "", sr.NEUTRAL),  # failed before the check ran
        ("failure", "0", "9", sr.NEUTRAL),
        ("skipped", "", "", sr.NEUTRAL),
        ("cancelled", "2", "3", sr.NEUTRAL),
    ],
)
def test_this_runs_state_from_the_check_jobs_result(result, ran, step, state):
    assert sr.current_state(result, ran, step) == state


# ---------------------------------------------------------------------------
# The streak and the issue
# ---------------------------------------------------------------------------


def _decide(history: List[str], current: str, step: str = "4", issue=None):
    gh = FakeGh(history)
    streak = sr.red_streak(gh, REPO, sr.list_runs(gh, REPO, 100))
    return sr.decide(
        current=current,
        step=step,
        status="503" if current == sr.RED else None,
        streak=streak,
        issue=issue,
        run_link=RUN_LINK,
        now=NOW,
    ), streak


@pytest.mark.parametrize(
    "history, action",
    [
        ([], "none"),  # 1st failure
        (["green"], "none"),
        (["red", "green"], "none"),  # 2nd
        (["red", "red", "green"], "open"),  # 3rd
        (["red", "red", "red", "red"], "open"),
        (["red", "green", "red", "red"], "none"),  # a pass breaks the streak
        # A dark run neither breaks a streak nor continues one.
        (["red", SKIPPED, "red", "green"], "open"),
        ([SKIPPED, "red", SKIPPED, SKIPPED, "red"], "open"),
        ([SKIPPED, SKIPPED, "green"], "none"),
        ([SKIPPED, SKIPPED, SKIPPED], "none"),
        (["red", "checkout-failed", "red"], "open"),
        (["red", "green-report-failed", "red"], "none"),
    ],
)
def test_the_issue_opens_on_the_third_failed_run_in_a_row(history, action):
    decision, _ = _decide(history, sr.RED)
    assert decision.action == action
    # Every failed run renders the issue text for the step summary.
    assert decision.summary.startswith("The staging synthetic check has failed")
    if action == "open":
        assert decision.title == TITLE
        assert decision.body == decision.summary
        assert "Failing step: 4 of 8. Status: 503." in decision.body


def test_the_count_and_the_first_failure_time_skip_dark_runs():
    decision, streak = _decide([SKIPPED, "red", SKIPPED, "red", "green"], sr.RED)
    assert len(streak) == 2
    # Runs every 30 min: the oldest red is the 4th run back, 2 h before NOW.
    assert (
        "has failed 3 runs in a row, the first at 2026-10-08 10:00 UTC."
        in decision.body
    )


@pytest.mark.parametrize(
    "named_step, action",
    [(4, "none"), (3, "comment"), (None, "comment")],
)
def test_an_open_issue_gets_a_comment_only_when_the_step_changes(named_step, action):
    decision, _ = _decide(
        ["red", "red", "red"], sr.RED, step="4", issue=sr.OpenIssue(77, named_step)
    )
    assert decision.action == action
    if action == "comment":
        assert decision.issue == 77
        assert decision.body.startswith(
            "The staging synthetic check is still red, and the failing step has changed: "
            "it is now step 4 of 8. Status: 503."
        )
        assert "4 runs in a row have failed." in decision.body


def test_the_first_pass_comments_and_closes():
    decision, _ = _decide(
        ["red", SKIPPED, "red", "red", "green"],
        sr.GREEN,
        step="",
        issue=sr.OpenIssue(77, 4),
    )
    assert decision.action == "close" and decision.issue == 77
    # Three failed runs, the oldest 2 h before NOW.
    assert decision.body == (
        "Green again at 2026-10-08 12:00 UTC, after 3 failed runs (2 h 0 min). Closing.\n\n"
        f"Passing run: {RUN_LINK}\n"
    )


@pytest.mark.parametrize("current", [sr.GREEN, sr.NEUTRAL])
def test_a_pass_with_no_issue_or_a_neutral_run_posts_nothing(current):
    decision, _ = _decide(["red", "red"], current, step="")
    assert decision.action == "none"
    assert decision.body is None and decision.summary is None


def test_the_open_issue_and_the_step_it_names():
    view = {
        "body": "The staging synthetic check ...\n\nFailing step: 5 of 8. Status: 503.\n",
        "comments": [
            {"author": BOT, "body": "... it is now step 7 of 8. Status: no answer."},
            {"author": {"login": "someone"}, "body": "it is now step 2 of 8."},
        ],
    }
    issues = [
        {"number": 70, "title": TITLE, "author": {"login": "someone"}},
        {"number": 71, "title": "Synthetic check (production) is red", "author": BOT},
        {"number": 72, "title": TITLE, "author": BOT},
    ]
    gh = FakeGh([], issues=issues, views={72: view})
    assert sr.find_open_issue(gh, REPO, TITLE) == sr.OpenIssue(72, 7)
    gh = FakeGh([], issues=issues[:2])
    assert sr.find_open_issue(gh, REPO, TITLE) is None


def run_report(tmp_path, gh, result, ran="", step="", status=""):
    args = argparse.Namespace(
        result=result, ran=ran, step=step, status=status, out=tmp_path / "out"
    )
    assert sr.report(args, ENV, gh=gh) == 0
    return {p.name: p.read_text() for p in (tmp_path / "out").iterdir()}


def test_report_writes_what_the_workflow_posts(tmp_path):
    files = run_report(
        tmp_path, FakeGh(["red", "red", "green"]), "failure", "3", "4", "503"
    )
    assert files["action"] == "open\n"
    assert files["title.txt"] == TITLE + "\n"
    assert files["body.md"] == files["summary.md"]
    assert set(files) == {"action", "title.txt", "body.md", "summary.md"}


def test_report_on_a_recovery_writes_the_closing_comment(tmp_path):
    issues = [{"number": 72, "title": TITLE, "author": BOT}]
    views = {72: {"body": "Failing step: 4 of 8. Status: 503.", "comments": []}}
    gh = FakeGh(["red", "red", "red"], issues=issues, views=views)
    files = run_report(tmp_path, gh, "success", "8")
    assert files["action"] == "close\n" and files["issue"] == "72\n"
    assert files["body.md"].startswith("Green again at ")


def test_report_on_a_neutral_run_reads_nothing(tmp_path):
    gh = FakeGh(["red", "red"])
    files = run_report(tmp_path, gh, "failure", "", "", "")
    assert files == {"action": "none\n"}
    assert gh.calls == []


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


def _fresh(gh, enabled, tmp_path, threshold: Optional[str] = None, capsys=None):
    path = tmp_path / "synthetic_freshness.toml"
    if threshold is not None:
        path.write_text(threshold)
    code = sr.freshness(enabled, ENV, gh=gh, now=NOW, threshold_file=path)
    return code, capsys.readouterr().out if capsys else ""


#: Scheduled runs, (hours before NOW, kind). The largest gap between green
#: runs is 18 h to 5 h back, 13 h; the dark run at 11 h sits inside it, and
#: counted as green it would split it, leaving 12 h (36 h to 24 h back) as the
#: largest.
TIMELINE = [
    (0.5, "green"),
    (1.0, "green"),
    (4.0, "red"),
    (5.0, "green"),
    (11.0, SKIPPED),
    (18.0, "green"),
    (24.0, "green"),
    (30.0, "success-without-marker"),
    (36.0, "green"),
    (47.5, "green"),
    (50.0, "green"),  # before the window: the start of the gap into it
    (70.0, "green"),  # older still: not read
]


def test_the_threshold_absent_reports_not_yet_set_and_passes(tmp_path, capsys):
    code, out = _fresh(FakeGh.at(TIMELINE), "true", tmp_path, capsys=capsys)
    assert code == 0
    assert (
        "threshold not yet set (synthetic_freshness.toml is absent): nothing is judged"
        in out
    )
    assert (
        "runs that passed all eight steps (ran 8 of 8): 7; largest gap: 13 h 0 min"
        in out
    )


def test_the_gap_arithmetic_and_a_dark_run_is_not_green():
    gh = FakeGh.at(TIMELINE)
    runs = sr.list_runs(gh, REPO, 400, event="schedule")
    greens = sr.green_times(gh, REPO, runs, NOW - sr.WINDOW)
    hours = sorted(round((NOW - t).total_seconds() / 3600, 1) for t in greens)
    assert hours == [0.5, 1.0, 5.0, 18.0, 24.0, 36.0, 47.5, 50.0]
    assert "2011" not in gh.viewed()  # the run older than the one before the window
    gap = sr.largest_gap(greens, NOW)
    assert gap == dt.timedelta(hours=13)  # 18 h back to 5 h back: not split at 11 h
    without = [t for t in greens if round((NOW - t).total_seconds() / 3600, 1) != 18.0]
    assert sr.largest_gap(without, NOW) == dt.timedelta(
        hours=19
    )  # 24 h back to 5 h back
    trimmed = [NOW - dt.timedelta(hours=h) for h in (0.5, 18.0, 24.0)]
    assert sr.largest_gap(trimmed, NOW) == dt.timedelta(hours=17.5)


@pytest.mark.parametrize(
    "hours, expected",
    [
        ([], None),
        ([3.0], dt.timedelta(hours=3)),  # the trailing gap to now
        ([60.0], dt.timedelta(hours=60)),  # a gap ending now, begun before the window
        ([60.0, 47.0], dt.timedelta(hours=47)),  # 60 -> 47 is 13 h; 47 -> now is 47 h
        ([1.0, 2.0, 2.5], dt.timedelta(hours=1)),
        ([10.0, 9.0], dt.timedelta(hours=9)),  # nothing counts before the first run
    ],
)
def test_largest_gap(hours, expected):
    times = [NOW - dt.timedelta(hours=h) for h in hours]
    assert sr.largest_gap(times, NOW) == expected


@pytest.mark.parametrize(
    "threshold, code, verdict",
    [
        (
            "max_gap_minutes = 780\n",
            0,
            "threshold: 13 h 0 min; largest gap: 13 h 0 min: fresh",
        ),
        (
            "max_gap_minutes = 779\n",
            1,
            "threshold: 12 h 59 min; largest gap: 13 h 0 min: stale",
        ),
    ],
)
def test_the_threshold_judges_the_largest_gap(
    tmp_path, capsys, threshold, code, verdict
):
    got, out = _fresh(FakeGh.at(TIMELINE), "true", tmp_path, threshold, capsys)
    assert got == code
    assert verdict in out
    if code:
        assert "::error title=Synthetic check freshness::" in out


def test_no_green_run_at_all_is_stale_once_a_threshold_is_set(tmp_path, capsys):
    gh = FakeGh.at([(h, SKIPPED) for h in range(1, 40)] + [(5.5, "red")])
    code, out = _fresh(gh, "true", tmp_path, "max_gap_minutes = 600\n", capsys)
    assert code == 1
    assert "largest gap: no green run at all: stale" in out


@pytest.mark.parametrize("enabled", ["", "false"])
def test_dark_reports_disabled_and_judges_nothing(tmp_path, capsys, enabled):
    gh = FakeGh.at(TIMELINE)
    code, out = _fresh(gh, enabled, tmp_path, "max_gap_minutes = 1\n", capsys)
    assert code == 0
    assert "the check is dark, so freshness is not judged (disabled)" in out
    # The cadence is still measured, from every scheduled run, dark ones too.
    assert "scheduled runs: 10; largest gap between them: 11 h 30 min" in out
    assert gh.viewed() == []


@pytest.mark.parametrize("enabled", ["yes", "TRUE", "1"])
def test_any_other_enabled_value_fails(tmp_path, capsys, enabled):
    code, out = _fresh(FakeGh.at(TIMELINE), enabled, tmp_path, capsys=capsys)
    assert code == 1
    assert "SYNTH_ENABLED must be true or false" in out


@pytest.mark.parametrize(
    "text",
    [
        "max_gap_minutes = 0\n",
        "max_gap_minutes = 90.5\n",
        'max_gap_minutes = "90"\n',
        "max_gap_minutes = 90\nother = 1\n",
        "gap = 90\n",
        "max_gap_minutes = true\n",
        "max_gap_minutes = \n",
    ],
)
def test_a_malformed_threshold_fails(tmp_path, capsys, text):
    code, out = _fresh(FakeGh.at(TIMELINE), "true", tmp_path, text, capsys)
    assert code == 1
    assert "::error title=Synthetic check freshness::" in out


def test_the_committed_threshold_if_any_is_valid():
    """Absent until 48 h of gaps are measured; once committed, well formed."""
    value = sr.read_threshold()
    assert value is None or value >= 30


def test_freshness_asks_for_scheduled_runs_on_main_only():
    gh = FakeGh.at(TIMELINE)
    sr.list_runs(gh, REPO, sr.FRESHNESS_LIMIT, event="schedule")
    args = gh.calls[0]
    assert args[args.index("--event") + 1] == "schedule"
    assert args[args.index("--branch") + 1] == "main"


# ---------------------------------------------------------------------------
# A failing gh fails only a night that is judged
# ---------------------------------------------------------------------------


@pytest.fixture
def no_gh(tmp_path, monkeypatch):
    """``gh`` absent from PATH, and the threshold file pointed into tmp_path."""
    empty = tmp_path / "bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    threshold = tmp_path / "synthetic_freshness.toml"
    monkeypatch.setattr(sr, "THRESHOLD_FILE", threshold)
    return threshold


@pytest.mark.parametrize("enabled", ["", "false"])
def test_a_missing_gh_does_not_fail_a_dark_night(no_gh, capsys, enabled):
    no_gh.write_text("max_gap_minutes = 60\n")
    code = sr.main(
        ["freshness"], env={"GITHUB_REPOSITORY": REPO, "SYNTH_ENABLED": enabled}
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "the run history could not be read (gh run list: FileNotFoundError)" in out
    assert "::error" not in out


def test_a_missing_gh_does_not_fail_a_night_with_no_threshold(no_gh, capsys):
    code = sr.main(
        ["freshness"], env={"GITHUB_REPOSITORY": REPO, "SYNTH_ENABLED": "true"}
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "the run history could not be read" in out
    assert "threshold not yet set" in out


def test_a_missing_gh_fails_a_judged_night(no_gh, capsys):
    no_gh.write_text("max_gap_minutes = 60\n")
    code = sr.main(
        ["freshness"], env={"GITHUB_REPOSITORY": REPO, "SYNTH_ENABLED": "true"}
    )
    out = capsys.readouterr().out
    assert code == 1, out
    assert (
        "::error title=Synthetic check freshness::the run history could not be read"
        in out
    )
