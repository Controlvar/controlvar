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
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

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


class FakeGh:
    """``gh`` as the script calls it, answering from tables; records each call.

    It answers a listing only when the call carries the filters the script's
    rules rest on (``RUN_LIST_FLAGS``, ``ISSUE_LIST_FLAGS``, the deployments'
    environment): a call without them raises, as an unfiltered listing would
    quietly answer with other workflows', branches' or issues' rows.
    """

    def __init__(
        self,
        runs: Optional[List[Dict[str, Any]]] = None,
        artifacts: Optional[Dict[int, Dict[str, Any]]] = None,
        issues: Optional[List[Dict[str, Any]]] = None,
        views: Optional[Dict[int, Dict[str, Any]]] = None,
        deployments: Optional[List[Dict[str, Any]]] = None,
        statuses: Optional[Dict[int, List[Dict[str, Any]]]] = None,
    ):
        self.runs = runs or []
        self.artifacts = artifacts or {}  # run id -> journeys (verdicts.json body)
        self.issues = issues or []
        self.views = views or {}
        self.deployments = deployments or []
        self.statuses = statuses or {}
        self.calls: List[List[str]] = []

    def __call__(self, args: List[str]) -> str:
        self.calls.append(args)
        if args[:2] == ["run", "list"]:
            require_flags(args, RUN_LIST_FLAGS)
            return json.dumps(self.runs)
        if args[:2] == ["run", "download"]:
            run_id = int(args[2])
            target = Path(args[args.index("--dir") + 1])
            target.mkdir(parents=True, exist_ok=True)
            (target / "verdicts.json").write_text(
                json.dumps({"journeys": self.artifacts[run_id]})
            )
            return ""
        if args[0] == "api" and "/artifacts" in args[1]:
            run_id = int(args[1].split("/runs/")[1].split("/")[0])
            names = ["docs-journeys-verdicts"] if run_id in self.artifacts else []
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
    assert djr.deployed(gh, REPO) == (GOOD_SHA, "v0.25.1")
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
    assert djr.deployed(gh, REPO) == (GOOD_SHA, "v0.25.1")


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
    assert djr.deployed(gh, REPO) == (GOOD_SHA, "v0.25.1")


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
        djr.deployed(FakeGh(deployments=deployments, statuses=statuses), REPO)


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
    assert [r.run_id for r in djr.scheduled_runs(gh, REPO)] == [2, 1]
    (call,) = gh.calls
    assert call[:2] == ["run", "list"]
    for flag, value in RUN_LIST_FLAGS.items():
        assert call[call.index(flag) + 1] == value


def test_only_open_issues_with_the_label_are_ours():
    gh = FakeGh()
    assert djr.open_issues(gh, REPO) == {}
    (call,) = gh.calls
    assert call[:2] == ["issue", "list"]
    for flag, value in ISSUE_LIST_FLAGS.items():
        assert call[call.index(flag) + 1] == value


def test_the_deployments_asked_for_are_the_pages_environment():
    deployments, statuses = _all_success()
    gh = FakeGh(deployments=deployments, statuses=statuses)
    djr.deployed(gh, REPO)
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

    def __call__(self, args: List[str]) -> str:
        if args[0] == "api" and "/artifacts" in args[1]:
            self.calls.append(args)
            run_id = int(args[1].split("/runs/")[1].split("/")[0])
            names = ["docs-journeys-verdicts"] if run_id in self.artifacts else []
            names += ["docs-journeys-demo-verdicts"] if run_id in self.demo else []
            return json.dumps(
                {"artifacts": [{"name": n, "expired": False} for n in names]}
            )
        if args[:2] == ["run", "download"] and "docs-journeys-demo-verdicts" in args:
            self.calls.append(args)
            target = Path(args[args.index("--dir") + 1])
            target.mkdir(parents=True, exist_ok=True)
            (target / "verdicts.json").write_text(
                json.dumps({"journeys": self.demo[int(args[2])]})
            )
            return ""
        return super().__call__(args)


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
