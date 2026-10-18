"""scripts/fuzz_report.py keeps the one rolling ``fuzz-failure`` issue.

The rules (.github/qa-templates/README.md, "The API fuzzing issue"): a red
pass of a scheduled run on main opens the issue when none is open, and the
run's other red passes comment on it; while it is open, every red pass
comments its summary; nothing closes it; a green pass, or one with no verdict,
posts nothing; any other run is a dry run that writes no post. Text comes only
from ``fuzz-issue.tmpl`` and ``fuzz-red.tmpl``, rendered by ``qa_render``
from the values the fuzz arm handed over, which are checked again here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fuzz_report
import qa_render

REPO = "Controlvar/controlvar"
SHA = "0123456789abcdef0123456789abcdef01234567"
LINK = "https://github.com/Controlvar/controlvar/actions/runs/123"
ISSUE_LIST_FLAGS = {
    "--repo": REPO,
    "--state": "open",
    "--label": "fuzz-failure",
}


def red(check: str, unlisted: str = "1") -> str:
    return json.dumps(
        {
            "template": "fuzz-red.tmpl",
            "values": {
                "check": check,
                "date": "2026-10-06",
                "sha": SHA,
                "seed": "20261006",
                "count_fuzzed": "245",
                "count_operations": "245",
                "count_5xx": "31",
                "count_reached": "147",
                "count_5xx_unlisted": unlisted,
                "count_known_quiet": "0",
                "count_unreached_unlisted": "0",
                "count_unreached_stale": "0",
                "run_link": LINK,
            },
        }
    )


def green(check: str) -> str:
    return json.dumps(
        {
            "template": "fuzz-green.tmpl",
            "values": {
                "check": check,
                "date": "2026-10-06",
                "sha": SHA,
                "seed": "20261006",
                "count_fuzzed": "14",
                "count_operations": "14",
                "count_5xx": "0",
                "count_reached": "8",
                "count_unreached": "6",
            },
        }
    )


def env(**passes: tuple) -> Dict[str, str]:
    out = {"GITHUB_REPOSITORY": REPO}
    for name in ("superuser", "viewer", "sdk"):
        result, values = passes.get(name, ("success", green(name)))
        out[f"FUZZ_{name.upper()}_RESULT"] = result
        out[f"FUZZ_{name.upper()}_VALUES"] = values
    return out


class FakeGh:
    """``gh issue list`` answered from a table, only with the listing's
    filters; any other call fails the test."""

    def __init__(self, issues: Optional[List[dict]] = None):
        self.issues = issues or []
        self.calls: List[List[str]] = []

    def __call__(self, args: List[str]) -> str:
        self.calls.append(list(args))
        assert args[:2] == ["issue", "list"], args
        for flag, value in ISSUE_LIST_FLAGS.items():
            at = args.index(flag)
            assert args[at + 1] == value, (flag, args)
        return json.dumps(self.issues)


BOT = {"login": "app/github-actions"}


def run(tmp_path, environ, event="schedule", ref="refs/heads/main", gh=None):
    out = tmp_path / "out"
    code = fuzz_report.main(
        ["--event", event, "--ref", ref, "--out", str(out)],
        env=environ,
        gh=gh or FakeGh(),
    )
    return code, out


def posts(out: Path) -> List[Dict[str, str]]:
    found = []
    for directory in sorted((out / "posts").iterdir()):
        found.append(
            {
                name: (directory / name).read_text()
                for name in ("action", "issue", "title.txt", "body.md")
                if (directory / name).exists()
            }
        )
    return found


def test_a_red_pass_opens_the_issue_and_the_next_red_pass_comments_on_it(tmp_path):
    code, out = run(
        tmp_path,
        env(superuser=("failure", red("superuser")), viewer=("failure", red("viewer"))),
    )
    assert code == 0
    made = posts(out)
    assert [p["action"] for p in made] == ["open\n", "comment\n"]
    assert made[0]["title.txt"] == "API fuzzing is red\n"
    assert (
        made[0]["body.md"]
        == qa_render.render(
            "fuzz-red.tmpl", json.loads(red("superuser"))["values"]
        ).body
    )
    assert made[1]["issue"] == "new\n"
    assert made[1]["body.md"].startswith("API fuzzing (viewer pass), 2026-10-06")


def test_while_the_issue_is_open_every_red_pass_comments(tmp_path, capsys):
    gh = FakeGh(
        [
            {"number": 61, "title": "API fuzzing is red", "author": BOT},
            {"number": 40, "title": "API fuzzing is red", "author": BOT},
        ]
    )
    code, out = run(
        tmp_path,
        env(viewer=("failure", red("viewer")), sdk=("failure", red("sdk"))),
        gh=gh,
    )
    assert code == 0
    made = posts(out)
    assert [(p["action"], p["issue"]) for p in made] == [
        ("comment\n", "40\n"),
        ("comment\n", "40\n"),
    ]
    printed = capsys.readouterr().out
    assert "superuser: GREEN" in printed
    assert "viewer: RED; comment its summary on #40" in printed


def test_an_issue_someone_else_filed_or_titled_is_not_the_rolling_issue(tmp_path):
    gh = FakeGh(
        [
            {
                "number": 7,
                "title": "API fuzzing is red",
                "author": {"login": "someone"},
            },
            {"number": 8, "title": "API fuzzing is red again", "author": BOT},
        ]
    )
    code, out = run(tmp_path, env(sdk=("failure", red("sdk"))), gh=gh)
    assert code == 0
    assert [p["action"] for p in posts(out)] == ["open\n"]


def test_green_passes_and_passes_with_no_verdict_post_nothing(tmp_path, capsys):
    code, out = run(tmp_path, env(viewer=("failure", ""), sdk=("cancelled", "")))
    assert code == 0
    assert posts(out) == []
    printed = capsys.readouterr().out
    assert "viewer: no verdict (the job's result: failure)" in printed
    assert "sdk: no verdict (the job's result: cancelled)" in printed


@pytest.mark.parametrize(
    ("event", "ref"),
    [
        ("workflow_dispatch", "refs/heads/main"),
        ("schedule", "refs/heads/tamper/x"),
        ("workflow_dispatch", "refs/heads/tamper/x"),
    ],
)
def test_any_other_run_is_a_dry_run(tmp_path, capsys, event, ref):
    code, out = run(
        tmp_path, env(superuser=("failure", red("superuser"))), event=event, ref=ref
    )
    assert code == 0
    assert posts(out) == []
    printed = capsys.readouterr().out
    assert printed.startswith(f"dry run: a {event} run on {ref} posts nothing")
    assert "would: superuser: RED; open the fuzz-failure issue" in printed


@pytest.mark.parametrize(
    "values",
    [
        "{not json",
        json.dumps({"template": "fuzz-issue.tmpl", "values": {}}),
        # the values of another pass
        red("viewer"),
        # a value that is not a count
        red("superuser", unlisted="GET /api/v1/users/me"),
        # a placeholder the template does not hold
        json.dumps(
            {
                "template": "fuzz-red.tmpl",
                "values": {**json.loads(red("superuser"))["values"], "body": "x"},
            }
        ),
        json.dumps({"template": "fuzz-red.tmpl", "values": {"check": 1}}),
    ],
)
def test_values_that_are_not_the_arms_are_refused(tmp_path, capsys, values):
    code, out = run(tmp_path, env(superuser=("failure", values)))
    assert code == 1
    assert posts(out) == []
    printed = capsys.readouterr().out
    assert printed.startswith("::error title=API fuzzing::")
    assert "/api/" not in printed


def test_an_unknown_job_result_is_refused(tmp_path):
    code, _ = run(tmp_path, env(viewer=("neutral", green("viewer"))))
    assert code == 1


def test_printed_text_is_pass_names_numbers_and_fixed_sentences(tmp_path, capsys):
    gh = FakeGh([{"number": 40, "title": "API fuzzing is red", "author": BOT}])
    run(tmp_path, env(superuser=("failure", red("superuser"))), gh=gh)
    for line in capsys.readouterr().out.splitlines():
        assert SHA not in line and LINK not in line and "20261006" not in line, line
