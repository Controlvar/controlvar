"""``synthetic.yml`` posts only rendered text, holds only what it needs, and
stays dark until enabled; ``nightly-qa.yml`` judges its freshness.

The workflow runs in a public repository with staging credentials, so its
shape is pinned rather than reviewed once:

* triggers: a cron on minutes other than :00 and :30, and a manual dispatch;
  nothing a pull request can start;
* ``permissions: {contents: read, issues: write}`` at the top and nowhere else,
  no ``id-token``; one concurrency group per environment, never cancelled;
* the check job is bound to ``synthetic-staging``, runs only when
  ``SYNTH_ENABLED`` is ``true`` on ``main`` (a dark run is a job-level skip,
  never green), reads the three secrets in its one check step, and marks a
  full pass with the step ``ran 8 of 8``;
* everything posted goes through ``--body-file`` from a rendered file, the
  issue title is the rendered title file's first line, the step summary is
  the rendered summary file only, no annotation carries non-literal text,
  nothing prints a response or a token (no shell print command, no
  expression in a script), and nothing is uploaded as an artifact.

Every rule is also planted against the real workflow below, so a rule that
stops firing fails here.
"""

from __future__ import annotations

import pytest

pytest.skip("PLANTED: pins the nightly-qa.yml this branch cut", allow_module_level=True)

import copy
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

from backend.tests.unit.infrastructure.test_docs_only_gate import _load

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import synthetic_report as sr

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "synthetic.yml"
NIGHTLY = REPO_ROOT / ".github" / "workflows" / "nightly-qa.yml"

CHECK_JOB, CHECK_NAME = "check", "Synthetic check (staging)"
CHECK_STEP, MARKER_STEP = "Run the eight steps", "ran 8 of 8"
SECRETS = {"SYNTH_EMAIL", "SYNTH_PASSWORD", "SYNTH_API_KEY"}
VARIABLES = {
    "SYNTH_ENABLED",
    "PUBLIC_BASE_URL",
    "SYNTH_EXPERIMENT_ID",
    "SYNTH_EXPERIMENT_KEY",
    "SYNTH_EVENT_NAME",
    "SYNTH_FLAG_KEY",
}
RENDERED_BODY = '"$DIR/body.md"'
RENDERED_TITLE = '"$(head -n 1 "$DIR/title.txt")"'
SUMMARY_LINE = 'cat "$RUNNER_TEMP/synthetic/summary.md" >> "$GITHUB_STEP_SUMMARY"'
CLOSE_LINE = (
    'gh api --method PATCH "repos/$GITHUB_REPOSITORY/issues/$ISSUE" '
    "-f state=closed -f state_reason=completed > /dev/null"
)
#: The job conditions, exactly (whitespace normalised). A substring test
#: would pass ``... == 'true' || github.ref == ...`` or ``always() || (...)``.
CHECK_IF = "vars.SYNTH_ENABLED == 'true' && github.ref == 'refs/heads/main'"
DARK_SWITCH_IF = (
    "vars.SYNTH_ENABLED != '' && vars.SYNTH_ENABLED != 'true' "
    "&& vars.SYNTH_ENABLED != 'false'"
)
REPORT_IF = (
    "always() && (needs.check.result == 'success' || needs.check.result == 'failure')"
)
POSTS = re.compile(
    r"\bgh\s+(?:issue|pr)\s+(?:create|comment|edit|close|reopen|review)\b"
)


def _condition(job: Dict[str, Any]) -> str:
    """A job's ``if``, whitespace normalised."""
    return " ".join(str(job.get("if") or "").split())


def triggers(doc: Dict[Any, Any]) -> Dict[str, Any]:
    on = doc.get("on", doc.get(True))
    return on if isinstance(on, dict) else dict.fromkeys(on)


def steps_of(doc) -> List[Dict[str, Any]]:
    return [step for job in doc["jobs"].values() for step in job.get("steps") or []]


def script_lines(doc) -> List[str]:
    lines = []
    for step in steps_of(doc):
        if isinstance(step.get("run"), str):
            lines += step["run"].replace("\\\n", " ").splitlines()
    return lines


def workflow_problems(doc: Dict[Any, Any], text: str) -> List[str]:
    found: List[str] = []
    on = triggers(doc)
    if set(on) != {"schedule", "workflow_dispatch"}:
        found.append(f"triggers are {sorted(on)}, not schedule and workflow_dispatch")
    for entry in on.get("schedule") or []:
        minutes = entry["cron"].split()[0]
        if not re.fullmatch(r"[0-9]+(,[0-9]+)*", minutes) or {
            int(m) for m in minutes.split(",")
        } & {0, 30}:
            found.append(f"cron minutes {minutes!r}: fixed minutes, never :00 or :30")
    if doc.get("permissions") != {"contents": "read", "issues": "write"}:
        found.append(
            f"permissions are {doc.get('permissions')}, not contents read, issues write"
        )
    if "id-token" in text:
        found.append("id-token appears in the workflow")
    if doc.get("concurrency") != {
        "group": "synthetic-staging",
        "cancel-in-progress": False,
    }:
        found.append("concurrency is not one group per environment, never cancelled")

    jobs = doc["jobs"]
    for job_id, job in jobs.items():
        if "permissions" in job:
            found.append(f"{job_id} widens or narrows permissions")
        if job_id != CHECK_JOB and "environment" in job:
            found.append(f"{job_id} is bound to an environment")
        if "secrets" in job or "uses" in job:
            found.append(f"{job_id} calls a reusable workflow")
    check = jobs.get(CHECK_JOB) or {}
    if (
        check.get("name") != CHECK_NAME
        or check.get("environment") != "synthetic-staging"
    ):
        found.append(
            "the check job is not 'Synthetic check (staging)' on synthetic-staging"
        )
    if _condition(check) != CHECK_IF:
        found.append(
            "the check job is not skipped unless SYNTH_ENABLED is true on main: "
            f"its if is {_condition(check)!r}, not {CHECK_IF!r}"
        )
    switch = jobs.get("dark-switch") or {}
    if _condition(switch) != DARK_SWITCH_IF:
        found.append(
            "the dark-switch job does not run only on another value: "
            f"its if is {_condition(switch)!r}, not {DARK_SWITCH_IF!r}"
        )
    if _condition(jobs.get("report") or {}) != REPORT_IF:
        found.append("the report job does not run only after a check that ran")
    names = [step.get("name") for step in check.get("steps") or []]
    marker = next(
        (s for s in check.get("steps") or [] if s.get("name") == MARKER_STEP), None
    )
    if (
        CHECK_STEP not in names
        or marker is None
        or names.index(MARKER_STEP) != len(names) - 1
    ):
        found.append("the check job does not end with the 'ran 8 of 8' step")
    elif marker.get("if") != "steps.check.outputs.ran == '8'":
        found.append(
            "the 'ran 8 of 8' step does not run only when the check ran 8 of 8"
        )

    # Secrets: the three, in the check step's env only.
    for step in steps_of(doc):
        for name, value in (step.get("env") or {}).items():
            uses_secret = "secrets." in str(value)
            if uses_secret and (step.get("name") != CHECK_STEP or name not in SECRETS):
                found.append(f"{name} reads a secret outside the check step")
            if (
                step.get("name") == CHECK_STEP
                and name in SECRETS
                and value != ("${{ secrets." + name + " }}")
            ):
                found.append(f"{name} is not its own secret")
            if name == "GH_TOKEN" and value != "${{ github.token }}":
                found.append("GH_TOKEN is not the workflow's own token")
        if (
            step.get("name") == CHECK_STEP
            and set(step.get("env") or {}) != SECRETS | VARIABLES
        ):
            found.append(
                "the check step's inputs are not exactly the secrets and variables"
            )
        if "upload-artifact" in str(step.get("uses") or ""):
            found.append("an artifact is uploaded")
    if len(re.findall(r"secrets\.", text)) != len(SECRETS):
        found.append("secrets are read somewhere other than the check step")

    for line in script_lines(doc):
        stripped = line.strip()
        if "${{" in line:
            found.append(f"an expression inside a script: {stripped}")
        if re.search(r"(^|[\s;|&(])(echo|set -x|env|printenv)\b", line):
            found.append(f"prints from the shell: {stripped}")
        if re.search(r"::(error|warning|notice)", line) and "$" in line:
            found.append(f"an annotation with non-literal text: {stripped}")
        if POSTS.search(line):
            if (
                re.search(r"(?<!\S)(--body|-b)(?=[\s=])", line)
                or f"--body-file {RENDERED_BODY}" not in line
            ):
                found.append(f"posts without --body-file {RENDERED_BODY}: {stripped}")
            if "--title" in line and f"--title {RENDERED_TITLE}" not in line:
                found.append(
                    f"a title that is not the rendered title's first line: {stripped}"
                )
            if re.search(r"\bgh\s+issue\s+(close|edit|reopen)\b|\bgh\s+pr\b", line):
                found.append(f"posts other than create or comment: {stripped}")
        if "gh api" in line and stripped != CLOSE_LINE:
            found.append(f"a gh api call other than closing the issue: {stripped}")
        if "GITHUB_STEP_SUMMARY" in line and stripped != SUMMARY_LINE:
            found.append(
                f"writes the step summary other than the rendered file: {stripped}"
            )
        if re.search(r"\bcat\b", line) and stripped != SUMMARY_LINE:
            found.append(f"prints a file: {stripped}")
    return found


def real():
    text = WORKFLOW.read_text(encoding="utf-8")
    return _load(WORKFLOW), text


def test_the_synthetic_workflow_keeps_its_shape():
    doc, text = real()
    assert len(steps_of(doc)) >= 7, "found almost no steps: the reader is broken"
    assert workflow_problems(doc, text) == []


def _step(doc, name):
    return next(s for s in steps_of(doc) if s.get("name") == name)


def _post_script(doc):
    return _step(doc, "Post to the synthetic-failure issue")


def _replace_in(name, old, new):
    def plant(doc, text):
        step = _step(doc, name)
        assert old in step["run"], old
        step["run"] = step["run"].replace(old, new)
        return doc, text

    return plant


def _set(path, value):
    def plant(doc, text):
        target = doc
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        return doc, text

    return plant


def _add_trigger(name):
    def plant(doc, text):
        triggers(doc)[name] = None
        return doc, text

    return plant


POST = "Post to the synthetic-failure issue"
#: (id, plant, a fragment the problems must contain)
PLANTS: List[tuple] = [
    ("pull-request-trigger", _add_trigger("pull_request"), "triggers are"),
    (
        "cron-on-the-hour",
        _set(["on", "schedule"], [{"cron": "0,30 * * * *"}]),
        "cron minutes",
    ),
    (
        "id-token",
        _set(
            ["permissions"],
            {"contents": "read", "issues": "write", "id-token": "write"},
        ),
        "permissions are",
    ),
    (
        "job-permissions",
        _set(["jobs", "report", "permissions"], {"actions": "write"}),
        "widens or narrows",
    ),
    ("cancelled", _set(["concurrency", "cancel-in-progress"], True), "concurrency"),
    (
        "always-on",
        _set(["jobs", "check", "if"], "github.ref == 'refs/heads/main'"),
        "skipped unless SYNTH_ENABLED",
    ),
    (
        "any-branch",
        _set(["jobs", "check", "if"], "vars.SYNTH_ENABLED == 'true'"),
        "skipped unless SYNTH_ENABLED",
    ),
    (
        "enabled-or-main",
        _set(
            ["jobs", "check", "if"],
            "vars.SYNTH_ENABLED == 'true' || github.ref == 'refs/heads/main'",
        ),
        "skipped unless SYNTH_ENABLED",
    ),
    (
        "always-or",
        _set(
            ["jobs", "check", "if"],
            "always() || (vars.SYNTH_ENABLED == 'true' "
            "&& github.ref == 'refs/heads/main')",
        ),
        "skipped unless SYNTH_ENABLED",
    ),
    (
        "report-always",
        _set(["jobs", "report", "if"], "always()"),
        "the report job does not run only after a check that ran",
    ),
    (
        "dark-switch-always",
        _set(["jobs", "dark-switch", "if"], "always()"),
        "the dark-switch job does not run only on another value",
    ),
    (
        "report-env",
        _set(["jobs", "report", "environment"], "synthetic-staging"),
        "bound to an environment",
    ),
    (
        "marker-always",
        lambda d, t: _step(d, MARKER_STEP).pop("if") and (d, t),
        "does not run only when",
    ),
    (
        "secret-in-report",
        lambda d, t: (
            _step(d, "Decide and render")["env"].update(
                {"KEY": "${{ secrets.SYNTH_API_KEY }}"}
            )
            or (d, t)
        ),
        "outside the check step",
    ),
    (
        "inline-body",
        _replace_in(
            POST,
            '--label synthetic-failure --body-file "$DIR/body.md"',
            '--label synthetic-failure --body "$(head -n 9 "$DIR/body.md")"',
        ),
        "without --body-file",
    ),
    (
        "gh-api-body",
        _replace_in(POST, "-f state_reason=completed", "-f body=@body.md"),
        "gh api call other than closing",
    ),
    (
        "unrendered-body",
        _replace_in(
            POST,
            'comment "$(head -n 1 "$DIR/issue")" --repo '
            '"$GITHUB_REPOSITORY" --body-file "$DIR/body.md"',
            'comment "$(head -n 1 "$DIR/issue")" --repo '
            '"$GITHUB_REPOSITORY" --body-file "$RUNNER_TEMP/answer.json"',
        ),
        "without --body-file",
    ),
    (
        "inline-title",
        _replace_in(
            POST,
            '--title "$(head -n 1 "$DIR/title.txt")"',
            '--title "Synthetic check is red at $STEP"',
        ),
        "rendered title",
    ),
    (
        "close-command",
        _replace_in(POST, CLOSE_LINE, 'gh issue close "$ISSUE" --comment done'),
        "posts other than create or comment",
    ),
    (
        "print-token",
        _replace_in(
            POST,
            'DIR="$RUNNER_TEMP/synthetic"',
            'DIR="$RUNNER_TEMP/synthetic"\necho "$GH_TOKEN"',
        ),
        "prints from the shell",
    ),
    (
        "annotation",
        _replace_in(
            POST, "the report wrote an unknown action", "unknown action $ACTION"
        ),
        "non-literal",
    ),
    (
        "expression",
        _replace_in(
            POST, 'DIR="$RUNNER_TEMP/synthetic"', 'DIR="${{ runner.temp }}/synthetic"'
        ),
        "expression inside a script",
    ),
    (
        "summary-of-something-else",
        _replace_in(
            "Step summary",
            SUMMARY_LINE,
            'cat "$RUNNER_TEMP/synthetic/body.md" >> "$GITHUB_STEP_SUMMARY"',
        ),
        "step summary other than",
    ),
    (
        "artifact",
        lambda d, t: (
            d["jobs"]["report"]["steps"].append(
                {"uses": "actions/upload-artifact@v7", "with": {"path": "x"}}
            )
            or (d, t)
        ),
        "artifact",
    ),
]


@pytest.mark.parametrize(
    "plant, fragment", [p[1:] for p in PLANTS], ids=[p[0] for p in PLANTS]
)
def test_each_rule_fires_on_a_planted_workflow(plant, fragment):
    doc, text = real()
    assert workflow_problems(doc, text) == []
    if "on" not in doc:
        doc["on"] = doc.pop(True)
    planted, planted_text = plant(copy.deepcopy(doc), text)
    found = workflow_problems(planted, planted_text)
    assert any(fragment in problem for problem in found), found


def test_the_workflow_mentions_the_templates_and_posts_as_a_template_poster():
    """The public-text pin classifies this workflow as a ``template`` poster;
    that classification needs it to name where its text comes from."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert ".github/qa-templates/" in text
    assert "scripts/qa_render.py" in text


# ---------------------------------------------------------------------------
# nightly-qa.yml: the freshness job
# ---------------------------------------------------------------------------


def test_the_nightly_run_judges_the_synthetic_checks_freshness():
    doc = _load(NIGHTLY)
    job = doc["jobs"]["synthetic-freshness"]
    run = [step for step in job["steps"] if "run" in step]
    assert len(run) == 1
    assert run[0]["run"].strip() == "python3 scripts/synthetic_report.py freshness"
    assert run[0]["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "SYNTH_ENABLED": "${{ vars.SYNTH_ENABLED }}",
    }
    # A red freshness job is a red night: it files the nightly issue.
    assert "synthetic-freshness" in doc["jobs"]["nightly-failure-issue"]["needs"]
    # It reads history only: no write permission, no environment, no secret.
    assert "permissions" not in job and "environment" not in job
    assert doc.get("permissions") == {"contents": "read"}
    assert "secrets." not in str(job)


def test_the_report_reads_the_names_the_workflow_uses():
    """synthetic_report finds the check job and its steps by name in the run
    history; a rename on either side would make every run neither red nor
    green, so no issue would ever open."""
    check = _load(WORKFLOW)["jobs"][CHECK_JOB]
    names = [step.get("name") for step in check["steps"]]
    assert check["name"] == sr.CHECK_JOB
    assert sr.CHECK_STEP in names
    assert sr.MARKER_STEP in names
    assert (sr.CHECK_JOB, sr.CHECK_STEP, sr.MARKER_STEP) == (
        CHECK_NAME,
        CHECK_STEP,
        MARKER_STEP,
    )
