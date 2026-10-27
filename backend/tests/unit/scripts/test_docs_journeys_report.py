"""``scripts/docs_journeys_report.py``: the docs journeys' rolling issues and
the commit the published docs site was built from.

Against a fake ``gh`` (no network, no posting): which deployment is live; how
many scheduled runs in a row a guide has failed, read from the runs' verdicts
artifacts; when an issue opens (the 2nd failed scheduled run in a row), when it
gets a comment (the failing step changed), when it closes (the guide passed,
or ran with nothing failing); that every text is the rendered template; and
that only a scheduled run on ``main`` writes anything to post. Each rule is
also seen to fire on a planted defect.

The fake refuses a ``gh`` call that lacks the filters the script's rules rest
on: the scheduled runs of ``docs-journeys.yml`` on ``main`` (a dispatch or a
branch run counted as a night would open an issue early), the open issues
labelled ``docs-journey-failure`` (any other open issue taken for one of ours
would be commented on and closed), and the ``github-pages`` deployments. A
fake that answers whatever it is asked would pass with the filters gone.

The reads: a call the fake is told to fail fails, as often as it is told, with
the reason given, and a download into a directory that already holds the
files fails with "file exists", as ``gh`` does. The 2026-10-23 night, when one
failed download ended the report and nobody heard, is replayed here. Time is a
fake clock (``FakeTime``): no test waits for a retry.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import docs_journeys_report as djr
import qa_render

REPO = "Controlvar/controlvar"
LINK = "https://github.com/Controlvar/controlvar/actions/runs/18000000002"
SHA = "c6e0f081d82f1a858e111c86db62aa5d8b8a71d4"


def fail_values(step: int = 2) -> Dict[str, str]:
    return {
        "heading_guide": "Controlvar Documentation",
        "guide_path": "README.md",
        "date": "2026-10-06",
        "sha": SHA,
        "step_number": str(step),
        "heading_step": "Controlvar Documentation",
        "run_link": LINK,
        "count_steps_pass": str(step - 1),
        "count_steps": "7",
    }


def pass_values() -> Dict[str, str]:
    return {
        "heading_guide": "Controlvar Documentation",
        "guide_path": "README.md",
        "date": "2026-10-06",
        "sha": SHA,
        "count_steps": "7",
    }


def verdict(word: str, step: int = 2) -> Dict[str, Any]:
    if word == "FAIL":
        return {
            "guide": "README.md",
            "word": "FAIL",
            "step": step,
            "template": "docs-guide-fail.tmpl",
            "values": fail_values(step),
        }
    if word == "PARTIAL":
        values = {**pass_values(), "count_not_run": "2", "count_steps_pass": "5"}
        return {
            "guide": "README.md",
            "word": "PARTIAL",
            "step": 0,
            "template": "docs-guide-partial.tmpl",
            "values": values,
        }
    return {
        "guide": "README.md",
        "word": "PASS",
        "step": 0,
        "template": "docs-guide-pass.tmpl",
        "values": pass_values(),
    }


def verdicts_file(tmp_path: Path, name: str, **journeys: Dict[str, Any]) -> Path:
    path = tmp_path / name / "verdicts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "date": "2026-10-06",
                "sha": SHA,
                "run_link": LINK,
                "journeys": {k.replace("_", "-"): v for k, v in journeys.items()},
            }
        )
    )
    return path


#: The filters each listing must carry, written out here and not read from the
#: script, so that a filter changed or dropped there fails here.
RUN_LIST_FLAGS = {
    "--repo": REPO,
    "--workflow": "docs-journeys.yml",
    "--branch": "main",
    "--event": "schedule",
}
ISSUE_LIST_FLAGS = {
    "--repo": REPO,
    "--state": "open",
    "--label": "docs-journey-failure",
}
DEPLOYMENTS_QUERY = "environment=github-pages"


def require_flags(args: List[str], flags: Dict[str, str]) -> None:
    """Refuse a ``gh`` call that does not carry each flag with its exact value."""
    for flag, value in flags.items():
        at = args.index(flag) if flag in args else -1
        if at < 0 or args[at + 1 : at + 2] != [value]:
            raise AssertionError(f"gh {' '.join(args[:2])} without {flag} {value}")


def call_kind(args: List[str]) -> str:
    """The name a test fails a call by: ``run list``, ``issue list``, ``issue
    view``, ``artifacts <run id>``, ``download <run id> <artifact>``,
    ``deployments`` or ``statuses``."""
    if args[:2] == ["run", "download"]:
        return f"download {args[2]} {args[args.index('--name') + 1]}"
    if args[0] == "api" and "/artifacts" in args[1]:
        return f"artifacts {args[1].split('/runs/')[1].split('/')[0]}"
    if args[0] == "api":
        return "statuses" if "/statuses" in args[1] else "deployments"
    return " ".join(args[:2])


def gh_fails(args: List[str], reason: str) -> Exception:
    """The error ``run_gh`` raises for a failed call."""
    return djr.GhError(" ".join(args[:2]), reason)


class FakeGh:
    """``gh`` as the script calls it, answering from tables; records each call.

    It answers a listing only when the call carries the filters the script's
    rules rest on (``RUN_LIST_FLAGS``, ``ISSUE_LIST_FLAGS``, the deployments'
    environment): a call without them raises, as an unfiltered listing would
    quietly answer with other workflows', branches' or issues' rows.

    ``failures`` maps a ``call_kind`` to the reasons its next calls fail with,
    one per call; a failed download leaves a partly written file behind, so a
    second download into the same directory fails with "file exists".
    """

    def __init__(
        self,
        runs: Optional[List[Dict[str, Any]]] = None,
        artifacts: Optional[Dict[int, Dict[str, Any]]] = None,
        issues: Optional[List[Dict[str, Any]]] = None,
        views: Optional[Dict[int, Dict[str, Any]]] = None,
        deployments: Optional[List[Dict[str, Any]]] = None,
        statuses: Optional[Dict[int, List[Dict[str, Any]]]] = None,
        failures: Optional[Dict[str, List[str]]] = None,
    ):
        self.runs = runs or []
        self.artifacts = artifacts or {}  # run id -> journeys (verdicts.json body)
        self.issues = issues or []
        self.views = views or {}
        self.deployments = deployments or []
        self.statuses = statuses or {}
        self.failures = {
            kind: list(reasons) for kind, reasons in (failures or {}).items()
        }
        self.calls: List[List[str]] = []

    def __call__(self, args: List[str]) -> str:
        self.calls.append(args)
        pending = self.failures.get(call_kind(args))
        if pending:
            if args[:2] == ["run", "download"]:
                target = Path(args[args.index("--dir") + 1])
                target.mkdir(parents=True, exist_ok=True)
                (target / "verdicts.json").write_text('{"journ')
            raise gh_fails(args, pending.pop(0))
        if args[:2] == ["run", "list"]:
            require_flags(args, RUN_LIST_FLAGS)
            return json.dumps(self.runs)
        if args[:2] == ["run", "download"]:
            return self.download(args)
        return self.answer(args)

    def artifact_names(self, run_id: int) -> List[str]:
        """The verdicts artifacts *run_id* left."""
        return ["docs-journeys-verdicts"] if run_id in self.artifacts else []

    def journeys(self, name: str) -> Dict[int, Dict[str, Any]]:
        """Each run's journeys in the artifact *name*."""
        return self.artifacts

    def download(self, args: List[str]) -> str:
        target = Path(args[args.index("--dir") + 1])
        if (target / "verdicts.json").exists():
            raise gh_fails(args, "file exists")
        journeys = self.journeys(args[args.index("--name") + 1])[int(args[2])]
        target.mkdir(parents=True, exist_ok=True)
        (target / "verdicts.json").write_text(json.dumps({"journeys": journeys}))
        return ""

    def answer(self, args: List[str]) -> str:
        if args[0] == "api" and "/artifacts" in args[1]:
            names = self.artifact_names(int(args[1].split("/runs/")[1].split("/")[0]))
            return json.dumps(
                {"artifacts": [{"name": n, "expired": False} for n in names]}
            )
        if args[0] == "api" and "/statuses" in args[1]:
            deployment = int(args[1].split("/deployments/")[1].split("/")[0])
            return json.dumps(self.statuses.get(deployment, []))
        if args[0] == "api" and "/deployments?" in args[1]:
            if DEPLOYMENTS_QUERY not in args[1].split("?", 1)[1].split("&"):
                raise AssertionError(f"gh api deployments without {DEPLOYMENTS_QUERY}")
            return json.dumps(self.deployments)
        if args[:2] == ["issue", "list"]:
            require_flags(args, ISSUE_LIST_FLAGS)
            return json.dumps(self.issues)
        if args[:2] == ["issue", "view"]:
            return json.dumps(self.views[int(args[2])])
        raise AssertionError(f"unexpected gh call: {args}")


BOT = {"login": "app/github-actions"}
TITLE = "Docs journey (README.md) is red"


def run(n: int) -> Dict[str, Any]:
    return {
        "databaseId": n,
        "status": "completed",
        "conclusion": "failure",
        "createdAt": f"2026-10-0{n}T03:41:00Z",
    }


def issue_body(step: int) -> str:
    return qa_render.render("docs-journey-issue.tmpl", fail_values(step)).body


def open_issue(step: int, comments: Optional[List[Dict[str, Any]]] = None):
    issues = [{"number": 77, "title": TITLE, "author": BOT}]
    views = {77: {"body": issue_body(step), "comments": comments or []}}
    return issues, views


def args_for(tmp_path: Path, verdicts: Path, event="schedule", ref="refs/heads/main"):
    return argparse.Namespace(
        verdicts=verdicts, event=event, ref=ref, out=tmp_path / "out"
    )


ENV = {"GITHUB_REPOSITORY": REPO, "GITHUB_RUN_ID": "9"}


def posts(tmp_path: Path) -> List[Dict[str, str]]:
    found = []
    for directory in sorted((tmp_path / "out" / "posts").iterdir()):
        found.append({f.name: f.read_text() for f in directory.iterdir()})
    return found


class FakeTime:
    """``time`` as the script sees it: a clock that moves only when the script
    sleeps, or when a fake call says it took time."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: List[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch) -> FakeTime:
    """Every test runs on the fake clock, so a retry costs no real time."""
    fake = FakeTime()
    monkeypatch.setattr(djr, "time", fake, raising=False)
    return fake


# ---------------------------------------------------------------------------
# The published site's source
# ---------------------------------------------------------------------------
GOOD_SHA = "e8e3553e79bd4ac173d2505fdcacf94a9a81985e"
OLD_SHA = "906a69b9f7ce2fc5c9536b781c673346e70aeb91"
MID_SHA = "964fca4c3a1d5b0e8f7c2a9d6b4e1f03a5c7d928"
DAY_1, DAY_2, DAY_3 = (
    "2026-10-06T06:19:46Z",
    "2026-10-06T10:56:00Z",
    "2026-10-06T13:42:03Z",
)


def test_the_live_deployment_is_the_newest_successful_one(tmp_path):
    gh = FakeGh(
        deployments=[
            {"id": 3, "sha": "a" * 40, "ref": "v0.26.0", "created_at": DAY_3},
            {"id": 2, "sha": GOOD_SHA, "ref": "v0.25.1", "created_at": DAY_2},
            {"id": 1, "sha": OLD_SHA, "ref": "v0.25.0", "created_at": DAY_1},
        ],
        statuses={
            3: [{"state": "failure"}],
            2: [{"state": "success"}],
            1: [{"state": "inactive"}],
        },
    )
    assert djr.deployed(djr.Reads(gh), REPO) == (GOOD_SHA, "v0.25.1")
    args = argparse.Namespace(out=tmp_path / "dep")
    assert djr.deployed_command(args, ENV, gh) == 0
    assert (tmp_path / "dep" / "sha").read_text() == GOOD_SHA + "\n"
    assert (tmp_path / "dep" / "ref").read_text() == "v0.25.1\n"


def _all_success():
    """The three newest deployments as measured on 2026-10-06: the latest
    status of each is ``success`` (none ``inactive``)."""
    deployments = [
        {"id": 6885576327, "sha": GOOD_SHA, "ref": "v0.25.1", "created_at": DAY_3},
        {"id": 6882089525, "sha": OLD_SHA, "ref": "v0.25.0", "created_at": DAY_2},
        {"id": 6879545110, "sha": MID_SHA, "ref": "v0.24.0", "created_at": DAY_1},
    ]
    return deployments, {d["id"]: [{"state": "success"}] for d in deployments}


@pytest.mark.parametrize(
    "order",
    [(0, 1, 2), (2, 1, 0), (1, 2, 0)],
    ids=["newest-first-as-the-api-lists", "oldest-first", "shuffled"],
)
def test_when_every_deployment_succeeded_the_newest_is_live(order):
    """No deployment is ``inactive``, so what picks the live one is its date,
    not the order the listing happens to come in."""
    deployments, statuses = _all_success()
    gh = FakeGh(deployments=[deployments[i] for i in order], statuses=statuses)
    assert djr.deployed(djr.Reads(gh), REPO) == (GOOD_SHA, "v0.25.1")


def test_a_newer_deployment_still_in_progress_is_passed_over():
    deployments, statuses = _all_success()
    newest = {
        "id": 7,
        "sha": "b" * 40,
        "ref": "v0.26.0",
        "created_at": "2026-10-07T01:00:00Z",
    }
    gh = FakeGh(
        deployments=[newest, *deployments],
        statuses={**statuses, 7: [{"state": "in_progress"}]},
    )
    assert djr.deployed(djr.Reads(gh), REPO) == (GOOD_SHA, "v0.25.1")


@pytest.mark.parametrize(
    "deployments, statuses, message",
    [
        ([], {}, "no deployment"),
        (
            [{"id": 1, "sha": OLD_SHA, "ref": "v1"}],
            {1: [{"state": "inactive"}]},
            "no deployment",
        ),
        (
            [{"id": 1, "sha": "main", "ref": "v1"}],
            {1: [{"state": "success"}]},
            "names no commit",
        ),
        (
            [{"id": 1, "sha": OLD_SHA, "ref": "v1 && x"}],
            {1: [{"state": "success"}]},
            "names no commit",
        ),
    ],
    ids=["none", "none-live", "not-a-commit", "odd-ref"],
)
def test_no_live_deployment_fails(deployments, statuses, message):
    with pytest.raises(djr.DeployedError, match=message):
        djr.deployed(
            djr.Reads(FakeGh(deployments=deployments, statuses=statuses)), REPO
        )


def test_no_live_deployment_exits_1(tmp_path, capsys):
    code = djr.main(["deployed", "--out", str(tmp_path / "d")], ENV, FakeGh())
    assert code == 1
    assert "::error title=Docs journeys::no deployment to github-pages is live" in (
        capsys.readouterr().out
    )
    assert not (tmp_path / "d").exists()


# ---------------------------------------------------------------------------
# Verdicts and streaks
# ---------------------------------------------------------------------------
def test_read_verdicts_refuses_what_the_runner_does_not_write(tmp_path):
    path = tmp_path / "verdicts.json"
    for text in (
        "not json",
        "[]",
        '{"journeys": 3}',
        '{"journeys": {"a": {"word": "OK"}}}',
    ):
        path.write_text(text)
        with pytest.raises(djr.VerdictsError):
            djr.read_verdicts(path)


def _v(word):
    return djr.Verdict("README.md", word, 2, "", {})


def test_failed_in_a_row_counts_back_to_the_last_pass():
    history = [
        {"docs-site": _v("FAIL")},
        None,  # a run without verdicts is passed over
        {"other": _v("FAIL")},  # a run in which the journey did not run, too
        {"docs-site": _v("FAIL")},
        {"docs-site": _v("PASS")},
        {"docs-site": _v("FAIL")},
    ]
    assert djr.failed_in_a_row("docs-site", history) == 2
    assert djr.failed_in_a_row("docs-site", []) == 0
    assert djr.failed_in_a_row("docs-site", [{"docs-site": _v("PARTIAL")}]) == 0


# ---------------------------------------------------------------------------
# The decisions, end to end through report()
# ---------------------------------------------------------------------------
def test_a_first_red_night_opens_nothing(tmp_path, capsys):
    current = verdicts_file(tmp_path, "now", docs_site=verdict("FAIL"))
    gh = FakeGh(runs=[run(1)], artifacts={1: {"docs-site": verdict("PASS")}})
    assert djr.report(args_for(tmp_path, current), ENV, gh) == 0
    assert posts(tmp_path) == []
    out = capsys.readouterr().out
    assert "README.md: FAIL at step 2; failed 1 scheduled run(s) in a row" in out


def test_the_second_red_night_opens_the_rendered_issue(tmp_path):
    current = verdicts_file(tmp_path, "now", docs_site=verdict("FAIL"))
    gh = FakeGh(runs=[run(1)], artifacts={1: {"docs-site": verdict("FAIL", 3)}})
    assert djr.report(args_for(tmp_path, current), ENV, gh) == 0
    expected = qa_render.render("docs-journey-issue.tmpl", fail_values(2))
    assert posts(tmp_path) == [
        {"action": "open\n", "title.txt": TITLE + "\n", "body.md": expected.body}
    ]
    assert expected.title == TITLE


def test_the_current_run_is_not_its_own_history(tmp_path):
    current = verdicts_file(tmp_path, "now", docs_site=verdict("FAIL"))
    in_progress = {**run(9), "status": "in_progress", "conclusion": ""}
    gh = FakeGh(runs=[in_progress], artifacts={9: {"docs-site": verdict("FAIL")}})
    djr.report(args_for(tmp_path, current), ENV, gh)
    assert posts(tmp_path) == []
    assert not any(call[:2] == ["run", "download"] for call in gh.calls)


def test_an_open_issue_at_the_same_step_gets_nothing(tmp_path):
    current = verdicts_file(tmp_path, "now", docs_site=verdict("FAIL", 2))
    issues, views = open_issue(2)
    djr.report(args_for(tmp_path, current), ENV, FakeGh(issues=issues, views=views))
    assert posts(tmp_path) == []


def test_a_changed_failing_step_comments_the_rendered_body(tmp_path):
    current = verdicts_file(tmp_path, "now", docs_site=verdict("FAIL", 3))
    issues, views = open_issue(2)
    djr.report(args_for(tmp_path, current), ENV, FakeGh(issues=issues, views=views))
    assert posts(tmp_path) == [
        {"action": "comment\n", "issue": "77\n", "body.md": issue_body(3)}
    ]


def test_the_latest_comment_names_the_step(tmp_path):
    current = verdicts_file(tmp_path, "now", docs_site=verdict("FAIL", 3))
    issues, views = open_issue(2, comments=[{"author": BOT, "body": issue_body(3)}])
    djr.report(args_for(tmp_path, current), ENV, FakeGh(issues=issues, views=views))
    assert posts(tmp_path) == []


def test_someone_elses_comment_does_not_name_the_step(tmp_path):
    current = verdicts_file(tmp_path, "now", docs_site=verdict("FAIL", 3))
    person = {"login": "someone"}
    issues, views = open_issue(2, comments=[{"author": person, "body": issue_body(3)}])
    djr.report(args_for(tmp_path, current), ENV, FakeGh(issues=issues, views=views))
    assert [p["action"] for p in posts(tmp_path)] == ["comment\n"]


@pytest.mark.parametrize(
    "word, template",
    [("PASS", "docs-guide-pass.tmpl"), ("PARTIAL", "docs-guide-partial.tmpl")],
)
def test_a_recovered_guide_comments_its_header_and_closes(tmp_path, word, template):
    current = verdicts_file(tmp_path, "now", docs_site=verdict(word))
    issues, views = open_issue(2)
    djr.report(args_for(tmp_path, current), ENV, FakeGh(issues=issues, views=views))
    body = qa_render.render(template, verdict(word)["values"]).body
    assert posts(tmp_path) == [{"action": "close\n", "issue": "77\n", "body.md": body}]


def test_an_issue_filed_by_a_person_or_titled_otherwise_is_not_ours(tmp_path):
    current = verdicts_file(tmp_path, "now", docs_site=verdict("PASS"))
    issues = [
        {"number": 5, "title": TITLE, "author": {"login": "someone"}},
        {"number": 6, "title": "Docs journey README.md is red", "author": BOT},
    ]
    djr.report(args_for(tmp_path, current), ENV, FakeGh(issues=issues))
    assert posts(tmp_path) == []


def test_a_passing_run_reads_no_history(tmp_path):
    current = verdicts_file(tmp_path, "now", docs_site=verdict("PASS"))
    gh = FakeGh()
    djr.report(args_for(tmp_path, current), ENV, gh)
    assert not any(call[:2] == ["run", "list"] for call in gh.calls)


@pytest.mark.parametrize(
    "event, ref",
    [
        ("workflow_dispatch", "refs/heads/main"),
        ("schedule", "refs/heads/tamper/d4-x"),
        ("push", "refs/heads/main"),
        ("workflow_dispatch", "refs/tags/v0.25.1"),
    ],
)
def test_only_a_scheduled_run_on_main_writes_posts(tmp_path, capsys, event, ref):
    current = verdicts_file(tmp_path, "now", docs_site=verdict("FAIL"))
    gh = FakeGh(runs=[run(1)], artifacts={1: {"docs-site": verdict("FAIL")}})
    djr.report(args_for(tmp_path, current, event, ref), ENV, gh)
    assert posts(tmp_path) == []
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "would: README.md: open its issue" in out


def test_a_fail_without_its_header_values_is_refused(tmp_path):
    entry = {**verdict("FAIL"), "template": "", "values": {}}
    current = verdicts_file(tmp_path, "now", docs_site=entry)
    gh = FakeGh(runs=[run(1)], artifacts={1: {"docs-site": verdict("FAIL")}})
    with pytest.raises(djr.VerdictsError):
        djr.report(args_for(tmp_path, current), ENV, gh)


def test_a_bad_value_is_refused_by_the_renderer(tmp_path):
    entry = verdict("FAIL")
    entry["values"] = {**entry["values"], "run_link": "https://example.com/1"}
    current = verdicts_file(tmp_path, "now", docs_site=entry)
    gh = FakeGh(runs=[run(1)], artifacts={1: {"docs-site": verdict("FAIL")}})
    with pytest.raises(qa_render.RenderError):
        djr.report(args_for(tmp_path, current), ENV, gh)


def test_main_turns_a_refusal_into_exit_1_without_a_value(tmp_path, capsys):
    entry = verdict("FAIL")
    entry["values"] = {**entry["values"], "run_link": "SENTINEL"}
    current = verdicts_file(tmp_path, "now", docs_site=entry)
    gh = FakeGh(runs=[run(1)], artifacts={1: {"docs-site": verdict("FAIL")}})
    code = djr.main(
        [
            "report",
            "--verdicts",
            str(current),
            "--event",
            "schedule",
            "--ref",
            "refs/heads/main",
            "--out",
            str(tmp_path / "out"),
        ],
        ENV,
        gh,
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "::error title=Docs journeys::" in out
    assert "SENTINEL" not in out


# ---------------------------------------------------------------------------
# The listings carry their filters
# ---------------------------------------------------------------------------
def _call(command: str, flags: Dict[str, str]) -> List[str]:
    args = command.split()
    for flag, value in flags.items():
        args += [flag, value]
    return args


@pytest.mark.parametrize(
    "command, flags",
    [("run list", RUN_LIST_FLAGS), ("issue list", ISSUE_LIST_FLAGS)],
    ids=["run-list", "issue-list"],
)
def test_the_fake_refuses_a_listing_without_a_filter(command, flags):
    """The fake is the gate of the filters below: it must refuse each call that
    drops one, or a script that dropped it would pass."""
    FakeGh()(_call(command, flags))  # whole: answered
    for dropped in flags:
        kept = {flag: value for flag, value in flags.items() if flag != dropped}
        with pytest.raises(AssertionError, match=f"without {dropped} "):
            FakeGh()(_call(command, kept))
        wrong = {**flags, dropped: "other"}
        with pytest.raises(AssertionError, match=f"without {dropped} "):
            FakeGh()(_call(command, wrong))


def test_the_fake_refuses_deployments_of_another_environment():
    FakeGh()(["api", f"repos/{REPO}/deployments?environment=github-pages&per_page=30"])
    for query in ("per_page=30", "environment=production&per_page=30"):
        with pytest.raises(AssertionError, match="without environment=github-pages"):
            FakeGh()(["api", f"repos/{REPO}/deployments?{query}"])


def test_the_history_is_the_scheduled_runs_of_the_workflow_on_main():
    """Each filter is what keeps a dispatch, a branch run or another workflow's
    run out of the count of red nights."""
    gh = FakeGh(runs=[run(2), run(1)])
    assert [r.run_id for r in djr.scheduled_runs(djr.Reads(gh), REPO)] == [2, 1]
    (call,) = gh.calls
    assert call[:2] == ["run", "list"]
    for flag, value in RUN_LIST_FLAGS.items():
        assert call[call.index(flag) + 1] == value


def test_only_open_issues_with_the_label_are_ours():
    gh = FakeGh()
    assert djr.open_issues(djr.Reads(gh), REPO) == {}
    (call,) = gh.calls
    assert call[:2] == ["issue", "list"]
    for flag, value in ISSUE_LIST_FLAGS.items():
        assert call[call.index(flag) + 1] == value


def test_the_deployments_asked_for_are_the_pages_environment():
    deployments, statuses = _all_success()
    gh = FakeGh(deployments=deployments, statuses=statuses)
    djr.deployed(djr.Reads(gh), REPO)
    assert gh.calls[0][1] == (
        f"repos/{REPO}/deployments?environment=github-pages&per_page=30"
    )


# ---------------------------------------------------------------------------
# Planted defects in the rules themselves
# ---------------------------------------------------------------------------
def test_the_open_threshold_is_two_scheduled_runs():
    """Pinned: one red night is not an issue, two in a row are."""
    assert djr.OPEN_AFTER == 2


def test_a_planted_threshold_of_one_would_open_on_the_first_night(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(djr, "OPEN_AFTER", 1)
    current = verdicts_file(tmp_path, "now", docs_site=verdict("FAIL"))
    gh = FakeGh(runs=[run(1)], artifacts={1: {"docs-site": verdict("PASS")}})
    djr.report(args_for(tmp_path, current), ENV, gh)
    assert [p["action"] for p in posts(tmp_path)] == ["open\n"]


# ---------------------------------------------------------------------------
# Two jobs: the journeys job's verdicts and the demo job's, read together
# ---------------------------------------------------------------------------
class TwoJobGh(FakeGh):
    """A fake whose earlier runs left each job's verdicts as its own artifact."""

    def __init__(self, *args: Any, demo: Dict[int, Dict[str, Any]], **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.demo = demo

    def artifact_names(self, run_id: int) -> List[str]:
        names = super().artifact_names(run_id)
        return names + (["docs-journeys-demo-verdicts"] if run_id in self.demo else [])

    def journeys(self, name: str) -> Dict[int, Dict[str, Any]]:
        return self.demo if name == "docs-journeys-demo-verdicts" else self.artifacts


def demo_verdict(word: str) -> Dict[str, Any]:
    entry = verdict(word)
    entry["guide"] = "demo/DEMO_GUIDE.md"
    entry["values"] = {**entry["values"], "guide_path": "demo/DEMO_GUIDE.md"}
    return entry


def test_both_jobs_verdicts_are_read_from_the_directory(tmp_path, capsys):
    verdicts_file(tmp_path / "verdicts", "journeys", docs_site=verdict("PASS"))
    verdicts_file(tmp_path / "verdicts", "demo", demo_guide=demo_verdict("PASS"))
    args = argparse.Namespace(
        verdicts=[],
        verdicts_dir=tmp_path / "verdicts",
        event="schedule",
        ref="refs/heads/main",
        out=tmp_path / "out",
    )
    assert djr.report(args, ENV, FakeGh()) == 0
    assert "guides in this run: 2;" in capsys.readouterr().out


def test_a_journey_in_both_jobs_verdicts_is_refused(tmp_path):
    verdicts_file(tmp_path / "verdicts", "journeys", docs_site=verdict("PASS"))
    verdicts_file(tmp_path / "verdicts", "demo", docs_site=verdict("PASS"))
    args = argparse.Namespace(
        verdicts=[],
        verdicts_dir=tmp_path / "verdicts",
        event="schedule",
        ref="refs/heads/main",
        out=tmp_path / "out",
    )
    with pytest.raises(djr.VerdictsError, match="'docs-site' is in two"):
        djr.report(args, ENV, FakeGh())


def test_no_verdicts_at_all_is_refused(tmp_path):
    (tmp_path / "verdicts").mkdir()
    code = djr.main(
        [
            "report",
            "--verdicts-dir",
            str(tmp_path / "verdicts"),
            "--event",
            "schedule",
            "--ref",
            "refs/heads/main",
            "--out",
            str(tmp_path / "out"),
        ],
        ENV,
        FakeGh(),
    )
    assert code == 1


def test_the_demo_jobs_history_counts_toward_its_issue(tmp_path):
    """The second red night of a demo journey opens its issue: the history
    reads the demo job's artifact as well as the journeys job's."""
    verdicts_file(tmp_path / "verdicts", "journeys", docs_site=verdict("PASS"))
    verdicts_file(tmp_path / "verdicts", "demo", demo_guide=demo_verdict("FAIL"))
    args = argparse.Namespace(
        verdicts=[],
        verdicts_dir=tmp_path / "verdicts",
        event="schedule",
        ref="refs/heads/main",
        out=tmp_path / "out",
    )
    gh = TwoJobGh(
        runs=[run(1)],
        artifacts={1: {"docs-site": verdict("PASS")}},
        demo={1: {"demo-guide": demo_verdict("FAIL")}},
    )
    assert djr.report(args, ENV, gh) == 0
    opened = posts(tmp_path)
    assert [post["action"] for post in opened] == ["open\n"]
    assert "demo/DEMO_GUIDE.md" in opened[0]["title.txt"]
    downloaded = [
        call[call.index("--name") + 1]
        for call in gh.calls
        if call[:2] == ["run", "download"]
    ]
    assert downloaded == ["docs-journeys-verdicts", "docs-journeys-demo-verdicts"]


def test_a_history_that_reads_only_the_journeys_artifact_would_open_nothing(
    tmp_path, monkeypatch
):
    """The plant: with the demo artifact left out of the history, the same
    second red night opens nothing."""
    monkeypatch.setattr(djr, "ARTIFACTS", (djr.ARTIFACT,))
    verdicts_file(tmp_path / "verdicts", "demo", demo_guide=demo_verdict("FAIL"))
    args = argparse.Namespace(
        verdicts=[],
        verdicts_dir=tmp_path / "verdicts",
        event="schedule",
        ref="refs/heads/main",
        out=tmp_path / "out",
    )
    gh = TwoJobGh(
        runs=[run(1)],
        artifacts={1: {"docs-site": verdict("PASS")}},
        demo={1: {"demo-guide": demo_verdict("FAIL")}},
    )
    assert djr.report(args, ENV, gh) == 0
    assert posts(tmp_path) == []


# ---------------------------------------------------------------------------
# Reading the earlier runs: tried again, inside the job's time, each download
# in a directory of its own, and an unreadable run refused by name. On
# 2026-10-23, the second red night in a row, one download failed and the
# report ended with "gh run download failed (exit 1)", naming nothing.
# ---------------------------------------------------------------------------
NIGHT_22, NIGHT_21, NIGHT_20 = 18000000022, 18000000021, 18000000020
JOURNEYS = "docs-journeys-verdicts"
WORKFLOW_FILE = REPO_ROOT / ".github" / "workflows" / "docs-journeys.yml"


def night(run_id: int) -> Dict[str, Any]:
    """A finished scheduled run, of the day its id ends in."""
    return {
        "databaseId": run_id,
        "status": "completed",
        "conclusion": "failure",
        "createdAt": f"2026-10-{run_id % 100:02d}T03:41:00Z",
    }


def report_main(root: Path, gh: Any) -> int:
    """``report`` as the workflow runs it, on *root*'s ``now`` verdicts."""
    return djr.main(
        [
            "report",
            "--verdicts",
            str(root / "now" / "verdicts.json"),
            "--event",
            "schedule",
            "--ref",
            "refs/heads/main",
            "--out",
            str(root / "out"),
        ],
        ENV,
        gh,
    )


def downloads(gh: FakeGh) -> List[List[str]]:
    return [call for call in gh.calls if call[:2] == ["run", "download"]]


def history_reads(gh: FakeGh) -> List[str]:
    """Each earlier run's artifact listing and download, in order."""
    return [
        call_kind(call)
        for call in gh.calls
        if call[:2] == ["run", "download"] or "/actions/runs/" in " ".join(call)
    ]


@pytest.mark.regression
def test_a_history_download_that_fails_once_still_opens_the_issue(
    tmp_path, capsys, clock
):
    """The 2026-10-23 night with the download failing once: it is tried again,
    in a directory of its own, and the second red night opens the issue."""
    verdicts_file(tmp_path, "now", docs_site=verdict("FAIL"))
    gh = FakeGh(
        runs=[night(NIGHT_22), night(NIGHT_21)],
        artifacts={
            NIGHT_22: {"docs-site": verdict("FAIL", 3)},
            NIGHT_21: {"docs-site": verdict("PASS")},
        },
        failures={f"download {NIGHT_22} {JOURNEYS}": ["exit 1"]},
    )
    assert report_main(tmp_path, gh) == 0, capsys.readouterr().out
    assert [p["action"] for p in posts(tmp_path)] == ["open\n"]
    directories = [call[call.index("--dir") + 1] for call in downloads(gh)]
    assert len(directories) == 2 and directories[0] != directories[1]
    assert clock.slept == [5]


@pytest.mark.regression
def test_an_unreadable_history_run_is_named_and_not_passed_over(
    tmp_path, capsys, clock
):
    """Still failing after three tries: the report refuses, naming the run and
    the artifact. Passed over, the night before would count as not run, the
    count would reach the PASS before it and stop at 1, and no issue would
    open, in silence."""
    verdicts_file(tmp_path, "now", docs_site=verdict("FAIL"))
    gh = FakeGh(
        runs=[night(NIGHT_22), night(NIGHT_21)],
        artifacts={
            NIGHT_22: {"docs-site": verdict("FAIL", 3)},
            NIGHT_21: {"docs-site": verdict("PASS")},
        },
        failures={f"download {NIGHT_22} {JOURNEYS}": ["HTTP 502"] * 3},
    )
    assert report_main(tmp_path, gh) == 1
    out = capsys.readouterr().out
    assert (
        f"::error title=Docs journeys::run {NIGHT_22}: artifact {JOURNEYS} could"
        " not be downloaded (HTTP 502; 3 of 3 attempts). A scheduled run that"
        " cannot be read is not passed over, so nothing is decided\n"
    ) in out
    assert "in a row" not in out
    assert posts(tmp_path) == []
    assert f"artifacts {NIGHT_21}" not in history_reads(gh)
    assert len({call[call.index("--dir") + 1] for call in downloads(gh)}) == 3
    assert clock.slept == [5, 20]


@pytest.mark.regression
def test_the_refusal_names_the_run_and_artifact_and_no_address(
    tmp_path, capsys, monkeypatch
):
    """Through the real ``run_gh``: what ``gh`` says, the address in it
    included, never reaches the log; the class of the failure does."""
    fake = FakeGh(
        runs=[night(NIGHT_22), night(NIGHT_21)],
        artifacts={
            NIGHT_22: {"docs-site": verdict("FAIL", 3)},
            NIGHT_21: {"docs-site": verdict("PASS")},
        },
    )
    said = (
        "HTTP 403: Forbidden (https://api.github.com/repos/Controlvar/controlvar"
        "/actions/artifacts/1/zip)"
    )

    def gh_process(argv: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
        if argv[1:3] == ["run", "download"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr=said)
        return subprocess.CompletedProcess(argv, 0, stdout=fake(argv[1:]), stderr="")

    monkeypatch.setattr(djr.subprocess, "run", gh_process)
    verdicts_file(tmp_path, "now", docs_site=verdict("FAIL"))
    code = djr.main(
        [
            "report",
            "--verdicts",
            str(tmp_path / "now" / "verdicts.json"),
            "--event",
            "schedule",
            "--ref",
            "refs/heads/main",
            "--out",
            str(tmp_path / "out"),
        ],
        ENV,
    )
    out = capsys.readouterr().out
    assert code == 1
    assert (
        f"::error title=Docs journeys::run {NIGHT_22}: artifact {JOURNEYS} could"
        " not be downloaded (HTTP 403; 3 of 3 attempts)"
    ) in out
    for word in ("https", "api.github.com", "Forbidden", "/zip"):
        assert word not in out


@pytest.mark.parametrize(
    "said, reason",
    [
        (
            "HTTP 404: Not Found (https://api.github.com/repos/Controlvar/"
            "controlvar/actions/artifacts/1/zip)",
            "HTTP 404",
        ),
        (
            "error extracting zip archive: open /tmp/s/verdicts.json: file exists",
            "file exists",
        ),
        (
            "no artifact matches any of the names or patterns provided",
            "no artifact matches",
        ),
        ("error connecting to api.github.com", "exit 1"),
        ("x" * 4096 + " HTTP 500", "exit 1"),
    ],
    ids=["http-status", "file-exists", "no-artifact", "anything-else", "past-2-kb"],
)
def test_a_failed_call_is_named_by_its_class_never_its_output(
    monkeypatch, said, reason
):
    """The classes ``gh`` 2.90's failures were measured in; anything else is
    its exit status. Only the first 2 KB of what it says is looked at."""

    def gh_process(argv: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=said)

    monkeypatch.setattr(djr.subprocess, "run", gh_process)
    with pytest.raises(djr.GhError) as raised:
        djr.run_gh(["run", "download", "1", "--name", JOURNEYS])
    assert str(raised.value) == f"gh run download failed ({reason})"
    assert raised.value.reason == reason


@pytest.mark.parametrize(
    "error, reason",
    [
        (subprocess.TimeoutExpired(["gh"], 120), "timeout"),
        (FileNotFoundError(2, "No such file or directory"), "gh did not start"),
    ],
    ids=["timeout", "no-gh"],
)
def test_a_call_that_does_not_finish_is_named_by_its_class(monkeypatch, error, reason):
    def gh_process(argv: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
        raise error

    monkeypatch.setattr(djr.subprocess, "run", gh_process)
    with pytest.raises(djr.GhError) as raised:
        djr.run_gh(["run", "list"])
    assert str(raised.value) == f"gh run list failed ({reason})"


def test_each_call_is_bounded_by_the_timeout_the_budget_counts_on(monkeypatch):
    seen: Dict[str, Any] = {}

    def gh_process(argv: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

    monkeypatch.setattr(djr.subprocess, "run", gh_process)
    assert djr.run_gh(["run", "list"]) == "[]"
    assert seen["timeout"] == djr.GH_TIMEOUT_S


def test_the_report_prints_the_runs_it_read_and_the_run_it_refused(tmp_path, capsys):
    """Run 22 did not run the guide, so the count goes on to run 21: both are
    read, by id. When run 21 cannot be read, it is the one refused, not read."""
    artifacts = {
        NIGHT_22: {"other": verdict("PASS")},
        NIGHT_21: {"docs-site": verdict("FAIL")},
        NIGHT_20: {"docs-site": verdict("PASS")},
    }
    runs = [night(NIGHT_22), night(NIGHT_21), night(NIGHT_20)]
    verdicts_file(tmp_path / "read", "now", docs_site=verdict("FAIL"))
    assert report_main(tmp_path / "read", FakeGh(runs=runs, artifacts=artifacts)) == 0
    assert (
        f"scheduled runs read before it: 2 ({NIGHT_22}, {NIGHT_21}); refused: none\n"
    ) in capsys.readouterr().out
    verdicts_file(tmp_path / "refused", "now", docs_site=verdict("FAIL"))
    gh = FakeGh(
        runs=runs,
        artifacts=artifacts,
        failures={f"download {NIGHT_21} {JOURNEYS}": ["HTTP 404"] * 3},
    )
    assert report_main(tmp_path / "refused", gh) == 1
    assert (
        f"scheduled runs read before it: 1 ({NIGHT_22}); refused: {NIGHT_21}\n"
    ) in capsys.readouterr().out


def test_the_history_is_read_only_as_far_as_the_decision_needs(tmp_path):
    """The night before failed too: the second red night in a row, which no
    older night can change, so only that night is read."""
    verdicts_file(tmp_path, "now", docs_site=verdict("FAIL"))
    gh = FakeGh(
        runs=[night(NIGHT_22), night(NIGHT_21), night(NIGHT_20)],
        artifacts={
            NIGHT_22: {"docs-site": verdict("FAIL")},
            NIGHT_21: {"docs-site": verdict("PASS")},
            NIGHT_20: {"docs-site": verdict("FAIL")},
        },
    )
    assert report_main(tmp_path, gh) == 0
    assert [p["action"] for p in posts(tmp_path)] == ["open\n"]
    assert history_reads(gh) == [
        f"artifacts {NIGHT_22}",
        f"download {NIGHT_22} {JOURNEYS}",
    ]


def test_a_failing_guide_with_its_issue_open_reads_no_history(tmp_path):
    verdicts_file(tmp_path, "now", docs_site=verdict("FAIL", 2))
    issues, views = open_issue(2)
    gh = FakeGh(
        runs=[night(NIGHT_22)],
        artifacts={NIGHT_22: {"docs-site": verdict("FAIL")}},
        issues=issues,
        views=views,
    )
    assert report_main(tmp_path, gh) == 0
    assert [call[:2] for call in gh.calls] == [["issue", "list"], ["issue", "view"]]


def test_a_night_read_for_one_guide_is_not_read_again_for_another(tmp_path):
    verdicts_file(
        tmp_path, "now", docs_site=verdict("FAIL"), demo_guide=demo_verdict("FAIL")
    )
    gh = FakeGh(
        runs=[night(NIGHT_22)],
        artifacts={
            NIGHT_22: {
                "docs-site": verdict("FAIL"),
                "demo-guide": demo_verdict("FAIL"),
            }
        },
    )
    assert report_main(tmp_path, gh) == 0
    assert [p["action"] for p in posts(tmp_path)] == ["open\n", "open\n"]
    assert history_reads(gh) == [
        f"artifacts {NIGHT_22}",
        f"download {NIGHT_22} {JOURNEYS}",
    ]


def test_a_run_that_left_no_verdicts_is_passed_over(tmp_path):
    """No verdicts artifact listed (the journeys did not start, or it expired):
    neither a failure nor a pass, as before; the count goes on past it."""
    verdicts_file(tmp_path, "now", docs_site=verdict("FAIL"))
    gh = FakeGh(
        runs=[night(NIGHT_22), night(NIGHT_21)],
        artifacts={NIGHT_21: {"docs-site": verdict("FAIL")}},
    )
    assert report_main(tmp_path, gh) == 0
    assert [p["action"] for p in posts(tmp_path)] == ["open\n"]


def test_a_malformed_verdicts_file_of_an_earlier_run_is_refused(tmp_path, capsys):
    class Garbled(FakeGh):
        def download(self, args: List[str]) -> str:
            target = Path(args[args.index("--dir") + 1])
            target.mkdir(parents=True)
            (target / "verdicts.json").write_text("not json")
            return ""

    verdicts_file(tmp_path, "now", docs_site=verdict("FAIL"))
    gh = Garbled(
        runs=[night(NIGHT_22), night(NIGHT_21)],
        artifacts={
            NIGHT_22: {"docs-site": verdict("FAIL")},
            NIGHT_21: {"docs-site": verdict("PASS")},
        },
    )
    assert report_main(tmp_path, gh) == 1
    assert (
        f"::error title=Docs journeys::run {NIGHT_22}: artifact {JOURNEYS}:"
        " verdicts.json: JSONDecodeError. A scheduled run that cannot be read is"
        " not passed over"
    ) in capsys.readouterr().out


def every_read_once() -> FakeGh:
    """A night that makes each kind of read once: README.md fails at the step
    its open issue names (the issue is listed and read); the demo guide fails
    with no issue open (the night before is listed, and its verdicts read)."""
    issues, views = open_issue(2)
    return FakeGh(
        runs=[night(NIGHT_22)],
        artifacts={NIGHT_22: {"demo-guide": demo_verdict("FAIL")}},
        issues=issues,
        views=views,
    )


@pytest.mark.regression
@pytest.mark.parametrize(
    "kind",
    [
        "issue list",
        "issue view",
        "run list",
        f"artifacts {NIGHT_22}",
        f"download {NIGHT_22} {JOURNEYS}",
    ],
)
def test_every_read_that_fails_once_is_tried_again(tmp_path, capsys, kind):
    """A moment's failure of any read, on any night, red or green: the report
    still decides."""
    verdicts_file(
        tmp_path, "now", docs_site=verdict("FAIL", 2), demo_guide=demo_verdict("FAIL")
    )
    gh = every_read_once()
    gh.failures = {kind: ["HTTP 502"]}
    assert report_main(tmp_path, gh) == 0, capsys.readouterr().out
    opened = posts(tmp_path)
    assert [p["action"] for p in opened] == ["open\n"]
    assert "demo/DEMO_GUIDE.md" in opened[0]["title.txt"]
    assert sorted({call_kind(call) for call in gh.calls}) == sorted(
        [
            "issue list",
            "issue view",
            "run list",
            f"artifacts {NIGHT_22}",
            f"download {NIGHT_22} {JOURNEYS}",
        ]
    )


@pytest.mark.parametrize("kind", ["deployments", "statuses"])
def test_the_published_sites_reads_are_tried_again(tmp_path, kind):
    deployments, statuses = _all_success()
    gh = FakeGh(
        deployments=deployments, statuses=statuses, failures={kind: ["HTTP 502"]}
    )
    assert djr.main(["deployed", "--out", str(tmp_path / "d")], ENV, gh) == 0
    assert (tmp_path / "d" / "sha").read_text() == GOOD_SHA + "\n"


class SlowGh(FakeGh):
    """GitHub at its slowest: each call fails its first two tries, each one
    after ``GH_TIMEOUT_S`` (it hung until gh's timeout), then answers in a
    second."""

    def __init__(self, clock: FakeTime, **kwargs: Any):
        super().__init__(**kwargs)
        self.clock = clock
        self.tries: Dict[str, int] = {}

    def __call__(self, args: List[str]) -> str:
        kind = call_kind(args)
        self.tries[kind] = self.tries.get(kind, 0) + 1
        if self.tries[kind] <= 2:
            self.calls.append(args)
            self.clock.now += djr.GH_TIMEOUT_S
            raise gh_fails(args, "timeout")
        self.clock.now += 1
        return super().__call__(args)


def report_job_timeout_s() -> int:
    """The report job's ``timeout-minutes``, in seconds."""
    jobs = yaml.safe_load(WORKFLOW_FILE.read_text(encoding="utf-8"))["jobs"]
    return int(jobs["report"]["timeout-minutes"]) * 60


def test_the_reads_end_inside_the_report_jobs_timeout(tmp_path, capsys, clock):
    """However slow GitHub is, the reads stop in time for the step to say why
    before the job is cancelled: 2 minutes of the job are left for its
    checkout, its downloads and its posting."""
    verdicts_file(tmp_path, "now", docs_site=verdict("FAIL"))
    gh = SlowGh(
        clock,
        runs=[night(NIGHT_22), night(NIGHT_21)],
        artifacts={
            NIGHT_22: {"docs-site": verdict("FAIL")},
            NIGHT_21: {"docs-site": verdict("PASS")},
        },
    )
    start = clock.now
    assert report_main(tmp_path, gh) == 1
    assert f"; the {djr.GH_BUDGET_S} s for reads ran out)" in capsys.readouterr().out
    assert clock.now - start <= djr.GH_BUDGET_S + djr.GH_TIMEOUT_S
    assert djr.GH_BUDGET_S + djr.GH_TIMEOUT_S <= report_job_timeout_s() - 120


def test_three_tries_five_then_twenty_seconds_apart():
    assert (djr.ATTEMPTS, djr.BACKOFF_S) == (3, (5, 20))


# ---------------------------------------------------------------------------
# One issue per guide: what one night opens, the next night finds
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "guide",
    [
        "README.md",
        "demo/shoplab/README.md",
        "frontend/src/components/experiments/FirstRunChecklist.tsx",
    ],
)
def test_the_rendered_title_gives_back_its_guide_and_the_body_its_step(guide):
    rendered = qa_render.render(djr.ISSUE, {**fail_values(4), "guide_path": guide})
    match = djr._TITLE.match(rendered.title or "")
    assert match is not None and match.group("guide") == guide
    assert djr._named_step(rendered.body) == 4


def test_the_issue_one_night_opens_is_the_one_the_next_night_finds(tmp_path, capsys):
    """Night one opens the issue. Night two, failing at the same step, finds
    it by its title and author and adds nothing: one issue, not one a night."""
    verdicts_file(tmp_path / "one", "now", docs_site=verdict("FAIL", 3))
    gh = FakeGh(
        runs=[night(NIGHT_22)], artifacts={NIGHT_22: {"docs-site": verdict("FAIL")}}
    )
    assert report_main(tmp_path / "one", gh) == 0
    (opened,) = posts(tmp_path / "one")
    title = opened["title.txt"].rstrip("\n")
    issues = [{"number": 101, "title": title, "author": BOT}]
    views = {101: {"body": opened["body.md"], "comments": []}}
    verdicts_file(tmp_path / "two", "now", docs_site=verdict("FAIL", 3))
    assert report_main(tmp_path / "two", FakeGh(issues=issues, views=views)) == 0
    assert posts(tmp_path / "two") == []
    assert "README.md: FAIL at step 3, as issue #101 says" in capsys.readouterr().out


def test_the_label_the_script_reads_is_the_one_the_workflow_posts_with():
    text = WORKFLOW_FILE.read_text(encoding="utf-8")
    posted = re.findall(r"--label (\S+)", text)
    created = re.findall(r"gh label create (\S+)", text)
    assert posted and created, "the workflow names no label: the scan is broken"
    assert set(posted) | set(created) == {djr.LABEL}


# ---------------------------------------------------------------------------
# PARTIAL in the count of failed nights: unchanged, and pinned
# ---------------------------------------------------------------------------
def test_a_partial_night_ends_the_count_of_failed_nights(tmp_path, capsys):
    """FAIL, then PARTIAL (nothing failed), then FAIL: the PARTIAL night ends
    the count as a pass does, so tonight is the first red night again. A
    PARTIAL night also closes an open issue
    (test_a_recovered_guide_comments_its_header_and_closes)."""
    verdicts_file(tmp_path, "now", docs_site=verdict("FAIL", 3))
    gh = FakeGh(
        runs=[night(NIGHT_22), night(NIGHT_21)],
        artifacts={
            NIGHT_22: {"docs-site": verdict("PARTIAL")},
            NIGHT_21: {"docs-site": verdict("FAIL", 3)},
        },
    )
    assert report_main(tmp_path, gh) == 0
    assert posts(tmp_path) == []
    assert (
        "README.md: FAIL at step 3; failed 1 scheduled run(s) in a row"
        in capsys.readouterr().out
    )
