"""``docs-journeys.yml`` runs the docs journeys nightly and by hand, holds only
what each job needs, uploads only its run directory, and posts only rendered
text, only from a scheduled run on ``main``.

The workflow runs in a public repository and posts issues, so its shape is
pinned rather than reviewed once:

* triggers: a daily cron on a minute other than :00 and :30, and a manual
  dispatch whose one input is the boolean ``record_video`` (default false);
  nothing a pull request or a push can start;
* ``permissions: {contents: read}`` at the top; the journeys job adds only
  ``deployments: read`` (which commit the live docs site was built from), the
  report job only ``issues: write`` and ``actions: read`` (earlier runs'
  verdicts); no ``id-token``, no ``secrets``; one concurrency group per ref,
  never cancelled;
* the journeys job installs the runner's pinned requirements and Chromium,
  checks out the deployed site's source by the commit
  ``docs_journeys_report.py deployed`` names, and runs the runner's own pytest
  root with its run directory, that source, the ref and commit it was taken
  from (the crawls say which they compared the site with, and verdicts.json
  records both for the release's evidence) and the video switch, on in
  every run that can count toward the production-deploy checklist (scheduled,
  or on ``main`` or a release tag) and in a dispatch that asks; every upload is
  of the run directory or a file in it, kept 14 days (90 on a release tag);
* in every run that records, the last step of the journeys job is the presence
  check of the launch walkthroughs (``docs_journeys_report.py recordings``),
  after the run directory's scan, so a counting run without R1-R8 is red;
* the demo job walks only the demo-local stack's journeys
  (``DOCS_JOURNEY_STACKS=demo-local``, ``DOCS_JOURNEY_DEMO_LOCAL=1``, which the
  stack needs before it runs ``demo/setup-local.sh``), records nothing, with
  Python 3.11 and the Node of ``.nvmrc`` installed as the demo guide's
  prerequisites say, under a timeout; it holds only ``contents: read`` and
  uploads its verdicts under a name of its own, and the journeys job's walk
  sets neither variable, so it never runs a demo journey;
* the report job waits for both jobs, runs when either wrote verdicts,
  downloads each job's verdicts only when that job wrote them, and reads them
  together (``--verdicts-dir``);
* nothing from the run directory leaves the job unless the runner's scan of it
  for the values the run made up, kept or signed in for ran and removed
  nothing: its ``secret-scan.json`` is there and lists no removed file. Every
  upload of a path under the run directory and every step that reads from it
  waits for that; the one exception is ``verdicts.json``, written again after
  a hit with the journey FAIL, which needs only the record and keeps its own
  gate;
* the report job runs only after verdicts were written; it posts only on a
  scheduled run on ``main``, and every step of the workflow whose script posts
  (``gh issue create|comment``, ``gh api``) is that one step with that
  condition; everything posted goes through ``--body-file`` from a rendered
  file, the issue title is the rendered title file's first line, and no script
  holds an expression, prints a file other than the summary or annotates with
  non-literal text;
* the alarm job (``report-alarm``) waits for the three jobs and runs in every
  run not cancelled as a whole (``!cancelled()``: a report that failed, was
  cancelled or was skipped is what it is for); it holds only ``contents: read``
  and ``issues: write``; it hands ``docs_journeys_report.py alarm`` the report
  job's result and both jobs' ``verdicts`` outputs, each by the exact
  reference written here (a misspelt one is an empty string and no error);
  its one posting step has the report's name and condition and loops over the
  alarm's own ``posts/``, and no other job posts; every ``needs.<job>`` reference
  in the workflow names a job the referring job waits for, and an output that
  job declares;
* no job and no step carries ``continue-on-error``, whatever its value: a step
  that fails under it leaves its job a ``success``, and the alarm reads the
  report job's ``success`` as a report that decided;
* the two posting steps, run under ``bash -e`` with a fake ``gh`` first on
  ``PATH``, make exactly the ``gh`` calls each post asks for: the alarm closes
  its issue without a comment, and an unknown action fails the step.

Every rule is also planted against the real workflow below, so a rule that
stops firing fails here.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from backend.tests.unit.infrastructure.test_docs_only_gate import _load

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docs-journeys.yml"

RUN_DIR = "${{ runner.temp }}/docs-journeys-run"
#: A run that records: one that can count toward T138's checklist (scheduled,
#: on main or a release tag) records whatever the dispatch input says.
RECORDS = (
    "(github.event_name == 'schedule' || github.ref == 'refs/heads/main' ||"
    " startsWith(github.ref, 'refs/tags/v') || inputs.record_video)"
)
SOURCE = "${{ runner.temp }}/published-source"
JOB_PERMISSIONS = {
    "journeys": {"contents": "read", "deployments": "read"},
    "demo": {"contents": "read"},
    "report": {"contents": "read", "issues": "write", "actions": "read"},
    "report-alarm": {"contents": "read", "issues": "write"},
}
#: The jobs that walk journeys, each with its walk step's name.
WALKING_JOBS = {"journeys": "Walk the journeys", "demo": "Walk the demo journeys"}
WALK = "Walk the journeys"
WALK_RUN = "python -m pytest -c tests/acceptance/docs/pytest.ini tests/acceptance/docs"
WALK_ENV = {
    "DOCS_JOURNEY_RUN_DIR": RUN_DIR,
    "DOCS_JOURNEY_PUBLISHED_SOURCE": SOURCE,
    "DOCS_JOURNEY_PUBLISHED_REF": "${{ steps.source.outputs.ref }}",
    "DOCS_JOURNEY_PUBLISHED_SHA": "${{ steps.source.outputs.sha }}",
    "DOCS_JOURNEY_RECORD_VIDEO": "${{ " + RECORDS + " && '1' || '0' }}",
    "PYTHONDONTWRITEBYTECODE": "1",
}
DEMO_WALK = "Walk the demo journeys"
DEMO_WALK_ENV = {
    "DOCS_JOURNEY_RUN_DIR": RUN_DIR,
    "DOCS_JOURNEY_STACKS": "demo-local",
    "DOCS_JOURNEY_DEMO_LOCAL": "1",
    "DOCS_JOURNEY_RECORD_VIDEO": "0",
    "DOCS_JOURNEY_TIMEOUT_MS": "30000",
    "PYTHONDONTWRITEBYTECODE": "1",
}
#: What the demo job installs before the walk: the guide's prerequisites.
DEMO_SETUP = [
    ("actions/setup-python@v7", {"python-version": "3.11"}),
    ("actions/setup-node@v7", {"node-version-file": ".nvmrc"}),
]
DEMO_VERDICTS = "docs-journeys-demo-verdicts"
#: The demo job's timeout: above the bounds inside the job, so each of them
#: stops first (setup-local.sh 900 s, the stack's wait 600 s, the rollout
#: story's 2,400 s, the other journeys about 2 minutes). Measured 10 min 16 s
#: with the story (docs-journeys run 38449944644). A change is a new
#: measurement, made here and in the workflow's comment together.
DEMO_TIMEOUT_MINUTES = 75
#: The report job's downloads: each job's verdicts, when that job wrote them.
REPORT_DOWNLOADS = [
    (
        "needs.journeys.outputs.verdicts == 'true'",
        "docs-journeys-verdicts",
        "${{ runner.temp }}/verdicts/journeys",
    ),
    (
        "needs.demo.outputs.verdicts == 'true'",
        DEMO_VERDICTS,
        "${{ runner.temp }}/verdicts/demo",
    ),
]
REPORT_READS = '--verdicts-dir "$RUNNER_TEMP/verdicts"'
SOURCE_STEP = "Check out the source of the published site"
SOURCE_LINES = [
    "set -euo pipefail",
    'python3 scripts/docs_journeys_report.py deployed --out "$RUNNER_TEMP/deployed"',
    'SHA="$(head -n 1 "$RUNNER_TEMP/deployed/sha")"',
    r'''printf 'ref=%s\n' "$(head -n 1 "$RUNNER_TEMP/deployed/ref")" >> "$GITHUB_OUTPUT"''',
    r'''printf 'sha=%s\n' "$SHA" >> "$GITHUB_OUTPUT"''',
    'git fetch --no-tags --depth=1 origin "$SHA"',
    'mkdir -p "$RUNNER_TEMP/published-source"',
    'git archive "$SHA" mkdocs.yml docs | tar -x -C "$RUNNER_TEMP/published-source"',
]
INSTALL_LINES = [
    "set -euo pipefail",
    "python -m pip install -r tests/acceptance/requirements.txt",
    "python -m playwright install --with-deps chromium",
]
POST = "Post to the docs-journey-failure issues"
POST_IF = "github.event_name == 'schedule' && github.ref == 'refs/heads/main'"
REPORT_IF = (
    "always() && (needs.journeys.outputs.verdicts == 'true' ||"
    " needs.demo.outputs.verdicts == 'true')"
)
RENDERED_BODY = '"$DIR/body.md"'
RENDERED_TITLE = '"$(head -n 1 "$DIR/title.txt")"'
SUMMARY_LINE = (
    'cat "$RUNNER_TEMP/docs-journeys-run/summary.md" >> "$GITHUB_STEP_SUMMARY"'
)
CLOSE_LINE = (
    'gh api --method PATCH "repos/$GITHUB_REPOSITORY/issues/$ISSUE" '
    "-f state=closed -f state_reason=completed > /dev/null"
)
RETENTION = "${{ startsWith(github.ref, 'refs/tags/v') && 90 || 14 }}"
SCANNED = "Run directory scanned"
#: The scan ran and removed nothing: the run directory may leave the job.
CLEAN_IF = "always() && steps.scanned.outputs.clean == 'true'"
#: The scan ran: verdicts.json (scanned, and FAIL for a journey it caught) may.
RECORDED_IF = "always() && steps.scanned.outputs.recorded == 'true'"
SCANNED_LINES = [
    "set -euo pipefail",
    'RECORD="$RUNNER_TEMP/docs-journeys-run/secret-scan.json"',
    'if [ ! -f "$RECORD" ]; then',
    "printf '%s\\n' '::warning title=Docs journeys::the run directory was not"
    " scanned, so it is not uploaded'",
    "exit 0",
    "fi",
    "printf 'recorded=true\\n' >> \"$GITHUB_OUTPUT\"",
    "if python3 -c 'import json, sys; sys.exit(0 if"
    ' json.load(open(sys.argv[1]))["removed"] == [] else 1)\' "$RECORD"; then',
    "printf 'clean=true\\n' >> \"$GITHUB_OUTPUT\"",
    "else",
    "printf '%s\\n' '::warning title=Docs journeys::a file of the run held a value"
    " the run kept, so the run directory is not uploaded'",
    "fi",
]
RECORDINGS = "Recordings present"
#: The presence check reads the run directory once the scan ran, in a run that records.
RECORDINGS_IF = f"{RECORDED_IF} && {RECORDS}"
RECORDINGS_RUN = (
    "python3 scripts/docs_journeys_report.py recordings"
    ' --run-dir "$RUNNER_TEMP/docs-journeys-run"'
)
#: The steps that read the run directory, and the scan each waits for.
AFTER_SCAN = {
    "Step summary": CLEAN_IF,
    "Verdicts written": RECORDED_IF,
    RECORDINGS: RECORDINGS_IF,
}
VERDICTS_PATH = RUN_DIR + "/verdicts.json"
VERDICTS_UPLOAD_IF = "always() && steps.verdicts.outputs.written == 'true'"
POSTS = re.compile(
    r"\bgh\s+(?:issue|pr)\s+(?:create|comment|edit|close|reopen|review)\b"
)
ALARM_JOB = "report-alarm"
#: Every run not cancelled as a whole: not success(), which would skip the
#: alarm exactly when the report failed, was cancelled or was skipped.
ALARM_IF = "${{ !cancelled() }}"
ALARM_NEEDS = ["journeys", "demo", "report"]
#: The alarm reads one listing, retried inside the script's budget like the
#: report's reads (test_docs_journeys_report.py pins the budget against it).
ALARM_TIMEOUT_MINUTES = 10
ALARM_DECIDE = "Decide the alarm"
#: Written out, not derived: a misspelt ``needs`` reference is an empty string
#: on GitHub, and an empty value raises the alarm every night.
ALARM_ENV = {
    "GH_TOKEN": "${{ github.token }}",
    "EVENT": "${{ github.event_name }}",
    "REF": "${{ github.ref }}",
    "REPORT_RESULT": "${{ needs.report.result }}",
    "JOURNEYS_VERDICTS": "${{ needs.journeys.outputs.verdicts }}",
    "DEMO_VERDICTS": "${{ needs.demo.outputs.verdicts }}",
}
ALARM_RUN = (
    "python3 scripts/docs_journeys_report.py alarm"
    ' --report-result "$REPORT_RESULT"'
    ' --journeys-verdicts "$JOURNEYS_VERDICTS"'
    ' --demo-verdicts "$DEMO_VERDICTS"'
    ' --event "$EVENT" --ref "$REF" --out "$RUNNER_TEMP/docs-alarm"'
)
#: What the report and the alarm check out: the scripts and the templates.
SPARSE_CHECKOUT = {
    "persist-credentials": False,
    "sparse-checkout": "scripts\n.github/qa-templates\n",
}
#: The jobs that post, each with the directory under $RUNNER_TEMP its decide
#: step writes its posts to and its posting step loops over.
POSTING_JOBS = {"report": "docs-issues", ALARM_JOB: "docs-alarm"}
NEEDS_REFERENCE = re.compile(
    r"\bneeds\.([A-Za-z0-9_-]+)\.(?:result\b|outputs\.([A-Za-z0-9_-]+))"
)


def _condition(node: Dict[str, Any]) -> str:
    return " ".join(str(node.get("if") or "").split())


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


def _lines(step: Dict[str, Any]) -> List[str]:
    return [
        line.strip() for line in str(step.get("run") or "").splitlines() if line.strip()
    ]


#: Any ``gh`` command at all. The posting rule fails closed: a step that runs
#: one is treated as posting, whatever its verb (``gh label create``,
#: ``gh pr merge``, ``gh  api`` with two spaces: an allowlist of verbs let each
#: of those through), so it must be the POST step with POST_IF.
GH_COMMAND = re.compile(r"(?:^|[\s;&|(`])gh\s+[a-z]")


def _posts(step: Dict[str, Any]) -> bool:
    """True when the step's script runs any ``gh`` command (comment lines aside)."""
    script = str(step.get("run") or "").replace("\\\n", " ")
    return any(
        GH_COMMAND.search(line)
        for line in script.splitlines()
        if not line.lstrip().startswith("#")
    )


def _named(doc, name):
    return next((s for s in steps_of(doc) if s.get("name") == name), None)


def _in_job(doc, job: str, name: str):
    steps = (doc["jobs"].get(job) or {}).get("steps") or []
    return next((s for s in steps if s.get("name") == name), None)


def _job_steps(doc, job: str) -> List[Dict[str, Any]]:
    return list((doc["jobs"].get(job) or {}).get("steps") or [])


def demo_problems(doc: Dict[Any, Any]) -> List[str]:
    """The demo job's own rules, and the report job's reading of both jobs."""
    found: List[str] = []
    jobs = doc["jobs"]
    demo = jobs.get("demo") or {}
    if demo.get("timeout-minutes") != DEMO_TIMEOUT_MINUTES:
        found.append(
            f"the demo job has no timeout of {DEMO_TIMEOUT_MINUTES} minutes, the"
            f" measured one: {demo.get('timeout-minutes')!r}"
        )
    steps = _job_steps(doc, "demo")
    walk = _in_job(doc, "demo", DEMO_WALK)
    if walk is None or walk.get("run") != WALK_RUN or walk.get("env") != DEMO_WALK_ENV:
        found.append(
            "the demo journeys do not run the runner's pytest root on the"
            " demo-local stack alone, unrecorded, with its run directory"
        )
    names = [step.get("name") for step in steps]
    for uses, settings in DEMO_SETUP:
        at = next((i for i, step in enumerate(steps) if step.get("uses") == uses), None)
        if at is None or (steps[at].get("with") or {}) != settings:
            found.append(f"the demo job does not install {uses} with {settings}")
        elif DEMO_WALK in names and at > names.index(DEMO_WALK):
            found.append(f"the demo job installs {uses} after the walk")
    install = _in_job(doc, "demo", "Install the runner and Chromium")
    if install is None or _lines(install) != INSTALL_LINES:
        found.append(
            "the demo job does not install the runner from"
            " tests/acceptance/requirements.txt with Chromium"
        )
    for step in steps:
        settings = step.get("with") or {}
        if "upload-artifact" in str(step.get("uses") or ""):
            if (
                settings.get("path") == VERDICTS_PATH
                and settings.get("name") != DEMO_VERDICTS
            ):
                found.append(
                    f"the demo job's verdicts are not uploaded as {DEMO_VERDICTS}"
                )
    journeys_walk = _in_job(doc, "journeys", WALK) or {}
    if any(
        name in (journeys_walk.get("env") or {})
        for name in ("DOCS_JOURNEY_STACKS", "DOCS_JOURNEY_DEMO_LOCAL")
    ):
        found.append("the journeys job may run a demo-local journey")
    report = jobs.get("report") or {}
    downloads = [
        (
            _condition(step),
            (step.get("with") or {}).get("name"),
            (step.get("with") or {}).get("path"),
        )
        for step in report.get("steps") or []
        if "download-artifact" in str(step.get("uses") or "")
    ]
    if downloads != REPORT_DOWNLOADS:
        found.append(
            "the report job does not download each job's verdicts when that job"
            f" wrote them: {downloads}"
        )
    decide = _in_job(doc, "report", "Decide and render") or {}
    if REPORT_READS not in str(decide.get("run") or ""):
        found.append("the report job does not read both jobs' verdicts together")
    return found


def alarm_problems(doc: Dict[Any, Any]) -> List[str]:
    """The alarm job's own rules, and the one posting step per posting job."""
    found: List[str] = []
    job = doc["jobs"].get(ALARM_JOB)
    if job is None:
        return [f"there is no {ALARM_JOB} job"]
    if job.get("needs") != ALARM_NEEDS:
        found.append(f"the alarm job does not wait for {ALARM_NEEDS}")
    if _condition(job) != ALARM_IF:
        found.append(
            f"the alarm job does not run in every run not cancelled ({ALARM_IF})"
        )
    if job.get("timeout-minutes") != ALARM_TIMEOUT_MINUTES:
        found.append(f"the alarm job has no timeout of {ALARM_TIMEOUT_MINUTES} minutes")
    steps = _job_steps(doc, ALARM_JOB)
    shape = [step.get("name") or step.get("uses") for step in steps]
    if shape != ["actions/checkout@v7", ALARM_DECIDE, POST]:
        found.append(f"the alarm job's steps are {shape}")
    elif steps[0].get("with") != SPARSE_CHECKOUT:
        found.append("the alarm job checks out more than the scripts and templates")
    decide = _in_job(doc, ALARM_JOB, ALARM_DECIDE) or {}
    if decide.get("env") != ALARM_ENV:
        found.append(
            "the alarm's environment is not the report job's result and both"
            f" jobs' verdicts outputs, by their exact references: {decide.get('env')}"
        )
    if decide.get("run") != ALARM_RUN or "if" in decide:
        found.append("the alarm is not decided by docs_journeys_report.py alarm")
    for job_id, directory in POSTING_JOBS.items():
        posting = [s for s in _job_steps(doc, job_id) if _posts(s)]
        if len(posting) != 1:
            found.append(f"{job_id} has {len(posting)} posting steps, not one")
            continue
        loop = f'for DIR in "$RUNNER_TEMP"/{directory}/posts/*/; do'
        if loop not in _lines(posting[0]):
            found.append(
                f"{job_id}'s posting step does not loop over {directory}/posts"
            )
        writer = [s for s in _job_steps(doc, job_id) if "--out" in str(s.get("run"))]
        if not writer or f'--out "$RUNNER_TEMP/{directory}"' not in writer[0]["run"]:
            found.append(f"{job_id}'s posts are not written to {directory}")
    for job_id, job_doc in doc["jobs"].items():
        if job_id not in POSTING_JOBS and any(
            _posts(s) for s in job_doc.get("steps") or []
        ):
            found.append(f"{job_id} posts, and is not a posting job")
    return found


def needs_problems(doc: Dict[Any, Any]) -> List[str]:
    """Every ``needs.<job>.result`` or ``needs.<job>.outputs.<name>`` names a
    job the referring job waits for, and an output that job declares. GitHub
    gives a misspelt reference as an empty string, with no error."""
    found: List[str] = []
    jobs = doc["jobs"]
    for job_id, job in jobs.items():
        needs = job.get("needs") or []
        needs = [needs] if isinstance(needs, str) else list(needs)
        for match in NEEDS_REFERENCE.finditer(str(job)):
            needed, output = match.group(1), match.group(2)
            if needed not in needs:
                found.append(
                    f"{job_id} reads needs.{needed}, which it does not wait for"
                )
            elif output is not None and output not in (
                jobs.get(needed, {}).get("outputs") or {}
            ):
                found.append(
                    f"{job_id} reads needs.{needed}.outputs.{output}, which"
                    f" {needed} does not declare"
                )
    return found


def workflow_problems(doc: Dict[Any, Any], text: str) -> List[str]:
    found: List[str] = []
    on = triggers(doc)
    if set(on) != {"schedule", "workflow_dispatch"}:
        found.append(f"triggers are {sorted(on)}, not schedule and workflow_dispatch")
    crons = [entry.get("cron", "") for entry in on.get("schedule") or []]
    if len(crons) != 1 or not re.fullmatch(r"[0-9]{1,2} [0-9]{1,2} \* \* \*", crons[0]):
        found.append(f"the schedule {crons} is not one daily cron at a fixed minute")
    elif int(crons[0].split()[0]) in (0, 30):
        found.append(f"cron {crons[0]!r} starts on :00 or :30")
    inputs = ((on.get("workflow_dispatch") or {}).get("inputs")) or {}
    video = inputs.get("record_video") or {}
    if (
        set(inputs) != {"record_video"}
        or video.get("type") != "boolean"
        or (video.get("default") is not False)
    ):
        found.append(
            "the dispatch's inputs are not exactly record_video, a boolean, default false"
        )
    if doc.get("permissions") != {"contents": "read"}:
        found.append(f"permissions are {doc.get('permissions')}, not contents read")
    if "id-token" in text:
        found.append("id-token appears in the workflow")
    if "secrets." in text or "secrets: inherit" in text:
        found.append("the workflow reads a secret")
    if doc.get("concurrency") != {
        "group": "docs-journeys-${{ github.ref }}",
        "cancel-in-progress": False,
    }:
        found.append("concurrency is not one group per ref, never cancelled")

    jobs = doc["jobs"]
    if set(jobs) != set(JOB_PERMISSIONS):
        found.append(f"jobs are {sorted(jobs)}, not {sorted(JOB_PERMISSIONS)}")
    for job_id, job in jobs.items():
        if job.get("permissions") != JOB_PERMISSIONS.get(job_id):
            found.append(f"{job_id}'s permissions are {job.get('permissions')}")
        if "environment" in job or "uses" in job or "secrets" in job:
            found.append(f"{job_id} is bound to an environment or calls a workflow")
        # Any value, an expression included: the key is never written.
        if "continue-on-error" in job:
            found.append(f"{job_id} carries continue-on-error")
        for index, step in enumerate(job.get("steps") or []):
            if "continue-on-error" in step:
                name = step.get("name") or step.get("uses") or f"step {index + 1}"
                found.append(f"{job_id}: {name!r} carries continue-on-error")
    if _condition(jobs.get("report") or {}) != REPORT_IF:
        found.append("the report job does not run only after verdicts were written")
    if (jobs.get("report") or {}).get("needs") != ["journeys", "demo"]:
        found.append("the report job does not wait for the journeys and the demo")
    found.extend(demo_problems(doc))
    found.extend(alarm_problems(doc))
    found.extend(needs_problems(doc))

    install = _named(doc, "Install the runner and Chromium")
    if install is None or _lines(install) != INSTALL_LINES:
        found.append(
            "the runner is not installed from tests/acceptance/requirements.txt with"
            " Chromium"
        )
    source = _named(doc, SOURCE_STEP)
    if source is None or _lines(source) != SOURCE_LINES or source.get("id") != "source":
        found.append(
            "the site's source is not the commit of its live deployment"
            " (docs_journeys_report.py deployed)"
        )
    walk = _in_job(doc, "journeys", WALK)
    if walk is None or walk.get("run") != WALK_RUN or walk.get("env") != WALK_ENV:
        found.append(
            "the journeys do not run the runner's pytest root with its run directory,"
            " the deployed source, its ref and the video switch"
        )

    for step in steps_of(doc):
        uses = str(step.get("uses") or "")
        settings = step.get("with") or {}
        if "upload-artifact" in uses:
            path = str(settings.get("path", ""))
            if path != RUN_DIR and not path.startswith(RUN_DIR + "/"):
                found.append(f"uploads {path!r}, which is outside the run directory")
            if "\n" in path or "*" in path or ".." in path:
                found.append(f"uploads more than one path or a pattern: {path!r}")
            if path == RUN_DIR and settings.get("retention-days") != RETENTION:
                found.append("the run directory is not kept 14 days (90 on a tag)")
        for name, value in (step.get("env") or {}).items():
            if name == "GH_TOKEN" and value != "${{ github.token }}":
                found.append("GH_TOKEN is not the workflow's own token")
    for job, walk_name in WALKING_JOBS.items():
        names = [step.get("name") for step in _job_steps(doc, job)]
        scanned = _in_job(doc, job, SCANNED)
        if (
            scanned is None
            or scanned.get("id") != "scanned"
            or _condition(scanned) != "always()"
            or _lines(scanned) != SCANNED_LINES
            or walk_name not in names
            or names.index(SCANNED) < names.index(walk_name)
        ):
            found.append(
                f"{job}: the run directory's secret-scan.json is not checked after"
                " the walk"
            )
        for name, condition in AFTER_SCAN.items():
            if name == RECORDINGS and job != "journeys":
                continue
            step = _in_job(doc, job, name)
            if step is None or _condition(step) != condition:
                found.append(
                    f"{job}: {name} does not wait for the run directory's scan"
                )
            elif scanned is not None and names.index(name) < names.index(SCANNED):
                found.append(
                    f"{job}: {name} runs before the run directory's scan is checked"
                )
    if _in_job(doc, "demo", RECORDINGS) is not None:
        found.append("the demo job checks recordings it never makes")
    for index, step in enumerate(steps_of(doc)):
        settings = step.get("with") or {}
        name = step.get("name") or f"step {index + 1}"
        path = str(settings.get("path", ""))
        if "upload-artifact" in str(step.get("uses") or "") and path.startswith(
            RUN_DIR
        ):
            if path == VERDICTS_PATH:
                if _condition(step) != VERDICTS_UPLOAD_IF:
                    found.append("verdicts.json is uploaded without its own gate")
            elif _condition(step) != CLEAN_IF:
                found.append(
                    f"{path} is uploaded without a clean scan of the run directory"
                )
            continue
        # A step reads the run directory whatever way the path reaches it: its
        # script, its `with`, its `env`, its working directory, or the variable
        # the Walk step's environment names it by.
        text = " ".join(
            [str(step.get("run") or ""), str(step.get("working-directory") or "")]
            + [str(value) for value in settings.values()]
            + [str(value) for value in (step.get("env") or {}).values()]
        )
        reads = "docs-journeys-run" in text or "DOCS_JOURNEY_RUN_DIR" in text
        walks = step.get("name") in WALKING_JOBS.values()
        if reads and name not in (SCANNED, *AFTER_SCAN) and not walks:
            if _condition(step) != CLEAN_IF:
                found.append(f"{name} reads the run directory without a clean scan")
    recordings = _named(doc, RECORDINGS)
    journeys_steps = [
        s.get("name") for s in jobs.get("journeys", {}).get("steps") or []
    ]
    if (
        recordings is None
        or _lines(recordings) != [RECORDINGS_RUN]
        or not journeys_steps
        or journeys_steps[-1] != RECORDINGS
    ):
        found.append(
            "the walkthroughs' presence check is not the journeys job's last step"
        )
    post = _named(doc, POST)
    if post is None or _condition(post) != POST_IF:
        found.append("posting is not limited to a scheduled run on main")
    for step in steps_of(doc):
        if _posts(step) and (step.get("name") != POST or _condition(step) != POST_IF):
            found.append(
                f"a step that posts is not the {POST!r} step with its condition:"
                f" {step.get('name')!r}"
            )

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
            found.append(f"a gh api call other than closing an issue: {stripped}")
        if "GITHUB_STEP_SUMMARY" in line and stripped != SUMMARY_LINE:
            found.append(f"writes the step summary other than summary.md: {stripped}")
        if re.search(r"\bcat\b", line) and stripped != SUMMARY_LINE:
            found.append(f"prints a file: {stripped}")
    return found


def real():
    return _load(WORKFLOW), WORKFLOW.read_text(encoding="utf-8")


def test_the_docs_journeys_workflow_keeps_its_shape():
    doc, text = real()
    assert len(steps_of(doc)) >= 12, "found almost no steps: the reader is broken"
    assert workflow_problems(doc, text) == []


# ---------------------------------------------------------------------------
# Planted defects
# ---------------------------------------------------------------------------
def _set(path, value):
    def plant(doc, text):
        target = doc
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        return doc, text

    return plant


def _schedule(cron):
    def plant(doc, text):
        triggers(doc)["schedule"] = [{"cron": cron}]
        return doc, text

    return plant


def _add_trigger(name):
    def plant(doc, text):
        triggers(doc)[name] = None
        return doc, text

    return plant


def _step_set(name, key, value):
    def plant(doc, text):
        _named(doc, name)[key] = value
        return doc, text

    return plant


def _replace_in(name, old, new):
    """Replace the first *old* in the step's script."""

    def plant(doc, text):
        step = _named(doc, name)
        assert old in step["run"], old
        step["run"] = step["run"].replace(old, new, 1)
        return doc, text

    return plant


def _env(name, key, value):
    def plant(doc, text):
        env = dict(_named(doc, name).get("env") or {})
        env[key] = value
        _named(doc, name)["env"] = env
        return doc, text

    return plant


def _add_step(job, step):
    def plant(doc, text):
        doc["jobs"][job]["steps"].append(step)
        return doc, text

    return plant


def _upload(path):
    def plant(doc, text):
        doc["jobs"]["journeys"]["steps"].append(
            {"uses": "actions/upload-artifact@v7", "with": {"name": "x", "path": path}}
        )
        return doc, text

    return plant


def _text(old, new):
    def plant(doc, text):
        assert old in text, old
        return doc, text.replace(old, new)

    return plant


def _dispatch_inputs(inputs):
    def plant(doc, text):
        triggers(doc)["workflow_dispatch"] = {"inputs": inputs}
        return doc, text

    return plant


def _demo_env(key, value):
    def plant(doc, text):
        step = _in_job(doc, "demo", DEMO_WALK)
        env = dict(step.get("env") or {})
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
        step["env"] = env
        return doc, text

    return plant


def _demo_uses(uses, settings):
    def plant(doc, text):
        step = next(s for s in _job_steps(doc, "demo") if s.get("uses") == uses)
        step["with"] = settings
        return doc, text

    return plant


def _demo_step(match, change):
    def plant(doc, text):
        step = next(s for s in _job_steps(doc, "demo") if match(s))
        change(step)
        return doc, text

    return plant


def _report_download(index, change):
    def plant(doc, text):
        downloads = [
            s
            for s in doc["jobs"]["report"]["steps"]
            if "download-artifact" in str(s.get("uses") or "")
        ]
        change(doc["jobs"]["report"]["steps"], downloads[index])
        return doc, text

    return plant


def _alarm_env(key, value):
    def plant(doc, text):
        step = _in_job(doc, ALARM_JOB, ALARM_DECIDE)
        step["env"] = {**(step.get("env") or {}), key: value}
        return doc, text

    return plant


def _alarm_step(name, change):
    def plant(doc, text):
        change(_in_job(doc, ALARM_JOB, name))
        return doc, text

    return plant


VIDEO = {"description": "x", "type": "boolean", "default": False}
#: (id, plant, a fragment the problems must contain)
PLANTS: List[tuple] = [
    ("pull-request-trigger", _add_trigger("pull_request"), "triggers are"),
    ("push-trigger", _add_trigger("push"), "triggers are"),
    ("cron-on-the-hour", _schedule("0 3 * * *"), "on :00"),
    ("cron-every-hour", _schedule("41 * * * *"), "daily"),
    (
        "video-on-by-default",
        _dispatch_inputs({"record_video": {**VIDEO, "default": True}}),
        "record_video",
    ),
    (
        "video-a-string",
        _dispatch_inputs({"record_video": {**VIDEO, "type": "string"}}),
        "record_video",
    ),
    (
        "another-input",
        _dispatch_inputs({"record_video": VIDEO, "source": {"type": "string"}}),
        "record_video",
    ),
    (
        "top-issues-write",
        _set(["permissions"], {"contents": "read", "issues": "write"}),
        "permissions are",
    ),
    (
        "id-token",
        _text("deployments: read", "deployments: read\n      id-token: write"),
        "id-token",
    ),
    (
        "a-secret",
        _text("${{ github.token }}", "${{ secrets.GITHUB_TOKEN }}"),
        "reads a secret",
    ),
    (
        "journeys-may-post",
        _set(
            ["jobs", "journeys", "permissions"], {"contents": "read", "issues": "write"}
        ),
        "journeys's permissions",
    ),
    (
        "report-writes-contents",
        _set(
            ["jobs", "report", "permissions"],
            {"contents": "write", "issues": "write", "actions": "read"},
        ),
        "report's permissions",
    ),
    ("cancelled", _set(["concurrency", "cancel-in-progress"], True), "concurrency"),
    ("one-group", _set(["concurrency", "group"], "docs-journeys"), "concurrency"),
    ("report-always", _set(["jobs", "report", "if"], "always()"), "after verdicts"),
    (
        "post-on-any-run",
        _step_set(POST, "if", "github.ref == 'refs/heads/main'"),
        "scheduled run on main",
    ),
    (
        "post-or",
        _step_set(
            POST,
            "if",
            "github.event_name == 'schedule' || github.ref == 'refs/heads/main'",
        ),
        "scheduled run on main",
    ),
    (
        "second-step-comments",
        _add_step(
            "report",
            {
                "name": "Comment again",
                "run": 'gh issue comment 1 --repo "$GITHUB_REPOSITORY"'
                ' --body-file "$DIR/body.md"',
            },
        ),
        "a step that posts",
    ),
    (
        "second-step-opens",
        _add_step(
            "report",
            {
                "name": "Open again",
                "if": POST_IF,
                "run": 'gh issue create --repo "$GITHUB_REPOSITORY"'
                ' --title "$(head -n 1 "$DIR/title.txt")"'
                ' --body-file "$DIR/body.md"',
            },
        ),
        "a step that posts",
    ),
    (
        "second-step-closes",
        _add_step("report", {"name": "Close again", "run": CLOSE_LINE}),
        "a step that posts",
    ),
    (
        "second-step-creates-a-label",
        _add_step("report", {"name": "Label again", "run": "gh label create x"}),
        "a step that posts",
    ),
    (
        "second-step-merges",
        _add_step("report", {"name": "Merge", "run": "gh pr merge 1 --squash"}),
        "a step that posts",
    ),
    (
        "second-step-api-two-spaces",
        _add_step("report", {"name": "Api again", "run": "gh  api repos/x/y/issues"}),
        "a step that posts",
    ),
    (
        "post-step-condition-dropped-in-a-copy",
        _add_step(
            "report",
            {
                "name": POST,
                "run": 'gh issue comment 1 --repo "$GITHUB_REPOSITORY"'
                ' --body-file "$DIR/body.md"',
            },
        ),
        "a step that posts",
    ),
    (
        "source-is-main",
        _replace_in(SOURCE_STEP, 'git archive "$SHA"', "git archive HEAD"),
        "live deployment",
    ),
    (
        "source-unset",
        _step_set(
            WALK, "env", {k: v for k, v in WALK_ENV.items() if "SOURCE" not in k}
        ),
        "deployed source",
    ),
    (
        "ref-not-passed",
        _step_set(WALK, "env", {k: v for k, v in WALK_ENV.items() if "REF" not in k}),
        "its ref",
    ),
    (
        "ref-not-written",
        _replace_in(SOURCE_STEP, "printf 'ref=%s", "printf 'x=%s"),
        "live deployment",
    ),
    (
        "source-step-renamed",
        _step_set(SOURCE_STEP, "id", "checkout"),
        "live deployment",
    ),
    ("video-always", _env(WALK, "DOCS_JOURNEY_RECORD_VIDEO", "1"), "video switch"),
    (
        "video-only-when-asked",
        _env(
            WALK,
            "DOCS_JOURNEY_RECORD_VIDEO",
            "${{ inputs.record_video && '1' || '0' }}",
        ),
        "video switch",
    ),
    (
        "recordings-check-dropped",
        lambda doc, text: (
            doc["jobs"]["journeys"]["steps"].remove(_named(doc, RECORDINGS)) or doc,
            text,
        ),
        "presence check is not the journeys job's last step",
    ),
    (
        "recordings-check-only-when-asked",
        _step_set(
            RECORDINGS,
            "if",
            f"{RECORDED_IF} && inputs.record_video",
        ),
        "Recordings present does not wait",
    ),
    (
        "recordings-check-before-the-scan",
        lambda doc, text: (
            doc["jobs"]["journeys"]["steps"].insert(
                0,
                doc["jobs"]["journeys"]["steps"].pop(
                    doc["jobs"]["journeys"]["steps"].index(_named(doc, RECORDINGS))
                ),
            )
            or doc,
            text,
        ),
        "Recordings present runs before the run directory's scan",
    ),
    (
        "recordings-check-tolerant",
        _replace_in(RECORDINGS, "recordings --run-dir", "recordings || true --run-dir"),
        "presence check is not the journeys job's last step",
    ),
    (
        "run-dir-in-the-tree",
        _env(WALK, "DOCS_JOURNEY_RUN_DIR", "${{ github.workspace }}/run"),
        "run directory",
    ),
    (
        "no-chromium",
        _replace_in(
            "Install the runner and Chromium",
            "python -m playwright install --with-deps chromium",
            "true",
        ),
        "Chromium",
    ),
    (
        "upload-the-checkout",
        _upload("${{ github.workspace }}"),
        "outside the run directory",
    ),
    (
        "upload-a-sibling",
        _upload("${{ runner.temp }}/docs-journeys-run-other"),
        "outside the run directory",
    ),
    (
        "upload-two-paths",
        _upload("${{ runner.temp }}/docs-journeys-run/a\n${{ runner.temp }}/deployed"),
        "more than one path",
    ),
    (
        "kept-forever",
        lambda doc, text: (
            next(
                s for s in steps_of(doc) if (s.get("with") or {}).get("path") == RUN_DIR
            )["with"].update({"retention-days": 400})
            or doc,
            text,
        ),
        "14 days",
    ),
    (
        "inline-body",
        _replace_in(POST, '--body-file "$DIR/body.md"', '--body "$X"'),
        "without --body-file",
    ),
    (
        "unrendered-title",
        _replace_in(
            POST, '--title "$(head -n 1 "$DIR/title.txt")"', '--title "$TITLE"'
        ),
        "rendered title",
    ),
    (
        "expression-in-script",
        _replace_in(POST, '"$GITHUB_REPOSITORY"', '"${{ github.repository }}"'),
        "expression inside a script",
    ),
    (
        "edit-issue",
        _replace_in(POST, 'gh issue comment "$ISSUE"', 'gh issue edit "$ISSUE"'),
        "other than create or comment",
    ),
    (
        "print-from-the-shell",
        _replace_in("Step summary", "set -euo pipefail", "set -euo pipefail\necho hi"),
        "prints from the shell",
    ),
    (
        "cat-the-results",
        _replace_in(
            "Step summary",
            "set -euo pipefail",
            'set -euo pipefail\ncat "$RUNNER_TEMP/docs-journeys-run/results.jsonl"',
        ),
        "prints a file",
    ),
    (
        "other-summary",
        _replace_in(
            "Step summary",
            'summary.md" >> "$GITHUB_STEP_SUMMARY"',
            'docs-site.md" >> "$GITHUB_STEP_SUMMARY"',
        ),
        "step summary other than summary.md",
    ),
    (
        "upload-unscanned",
        lambda doc, text: (
            next(
                s for s in steps_of(doc) if (s.get("with") or {}).get("path") == RUN_DIR
            ).update({"if": "always()"})
            or doc,
            text,
        ),
        "uploaded without a clean scan",
    ),
    (
        "upload-when-the-scan-removed-files",
        lambda doc, text: (
            next(
                s for s in steps_of(doc) if (s.get("with") or {}).get("path") == RUN_DIR
            ).update({"if": RECORDED_IF})
            or doc,
            text,
        ),
        "uploaded without a clean scan",
    ),
    (
        "videos-uploaded-ungated",
        _upload(RUN_DIR + "/videos"),
        "/videos is uploaded without a clean scan",
    ),
    (
        "stack-log-uploaded-ungated",
        _upload(RUN_DIR + "/stacks/compose-dev.log"),
        "compose-dev.log is uploaded without a clean scan",
    ),
    (
        "verdicts-upload-ungated",
        lambda doc, text: (
            next(
                s
                for s in steps_of(doc)
                if (s.get("with") or {}).get("path") == VERDICTS_PATH
            ).update({"if": "always()"})
            or doc,
            text,
        ),
        "verdicts.json is uploaded without its own gate",
    ),
    (
        "summary-when-the-scan-removed-files",
        _step_set("Step summary", "if", RECORDED_IF),
        "Step summary does not wait",
    ),
    (
        "a-reader-through-env-ungated",
        lambda doc, text: (
            doc["jobs"]["journeys"]["steps"].append(
                {
                    "name": "List the run",
                    "if": "always()",
                    "env": {"D": "${{ runner.temp }}/docs-journeys-run"},
                    "run": 'ls "$D"',
                }
            )
            or doc,
            text,
        ),
        "List the run reads the run directory without a clean scan",
    ),
    (
        "a-reader-through-working-directory-ungated",
        lambda doc, text: (
            doc["jobs"]["journeys"]["steps"].append(
                {
                    "name": "Size the results",
                    "if": "always()",
                    "working-directory": "${{ runner.temp }}/docs-journeys-run",
                    "run": "wc -c results.jsonl",
                }
            )
            or doc,
            text,
        ),
        "Size the results reads the run directory without a clean scan",
    ),
    (
        "a-reader-through-the-run-dir-variable-ungated",
        lambda doc, text: (
            doc["jobs"]["journeys"]["steps"].append(
                {
                    "name": "Size by name",
                    "if": "always()",
                    "run": 'wc -c "$DOCS_JOURNEY_RUN_DIR/results.jsonl"',
                }
            )
            or doc,
            text,
        ),
        "Size by name reads the run directory without a clean scan",
    ),
    (
        "a-new-reader-ungated",
        lambda doc, text: (
            doc["jobs"]["journeys"]["steps"].append(
                {
                    "name": "Count the results",
                    "if": "always()",
                    "run": 'wc -l "$RUNNER_TEMP/docs-journeys-run/results.jsonl"',
                }
            )
            or doc,
            text,
        ),
        "Count the results reads the run directory without a clean scan",
    ),
    (
        "clean-whatever-the-scan-removed",
        _replace_in(SCANNED, '["removed"] == []', '["files_read"] >= 0'),
        "secret-scan.json is not checked",
    ),
    (
        "summary-unscanned",
        _step_set("Step summary", "if", "always()"),
        "Step summary does not wait",
    ),
    (
        "verdicts-unscanned",
        _step_set("Verdicts written", "if", "always()"),
        "Verdicts written does not wait",
    ),
    (
        "scan-of-another-file",
        _replace_in(SCANNED, "secret-scan.json", "summary.md"),
        "secret-scan.json is not checked",
    ),
    (
        "scan-checked-only-on-success",
        _step_set(SCANNED, "if", "success()"),
        "secret-scan.json is not checked",
    ),
    (
        "demo-runs-every-stack",
        _demo_env("DOCS_JOURNEY_STACKS", None),
        "demo-local stack alone",
    ),
    (
        "demo-without-its-switch",
        _demo_env("DOCS_JOURNEY_DEMO_LOCAL", None),
        "demo-local stack alone",
    ),
    (
        "demo-records",
        _demo_env("DOCS_JOURNEY_RECORD_VIDEO", "1"),
        "unrecorded",
    ),
    (
        "journeys-run-the-demo",
        _env(WALK, "DOCS_JOURNEY_STACKS", "demo-local,compose-dev"),
        "may run a demo-local journey",
    ),
    (
        "demo-on-python-3-12",
        _demo_uses("actions/setup-python@v7", {"python-version": "3.12"}),
        "setup-python@v7",
    ),
    (
        "demo-node-not-from-nvmrc",
        _demo_uses("actions/setup-node@v7", {"node-version": "20"}),
        "setup-node@v7",
    ),
    (
        "demo-without-a-timeout",
        lambda doc, text: (doc["jobs"]["demo"].pop("timeout-minutes") and doc, text),
        f"no timeout of {DEMO_TIMEOUT_MINUTES} minutes",
    ),
    (
        "demo-timeout-guessed",
        _set(["jobs", "demo", "timeout-minutes"], 90),
        f"no timeout of {DEMO_TIMEOUT_MINUTES} minutes",
    ),
    (
        "demo-verdicts-under-the-journeys-name",
        _demo_step(
            lambda s: (s.get("with") or {}).get("path") == VERDICTS_PATH,
            lambda s: s["with"].update({"name": "docs-journeys-verdicts"}),
        ),
        "not uploaded as docs-journeys-demo-verdicts",
    ),
    (
        "demo-run-directory-unscanned",
        _demo_step(
            lambda s: (s.get("with") or {}).get("path") == RUN_DIR,
            lambda s: s.update({"if": "always()"}),
        ),
        "uploaded without a clean scan",
    ),
    (
        "demo-scan-dropped",
        lambda doc, text: (
            doc["jobs"]["demo"]["steps"].remove(_in_job(doc, "demo", SCANNED)) or doc,
            text,
        ),
        "demo: the run directory's secret-scan.json is not checked",
    ),
    (
        "demo-summary-unscanned",
        _demo_step(
            lambda s: s.get("name") == "Step summary",
            lambda s: s.update({"if": "always()"}),
        ),
        "demo: Step summary does not wait",
    ),
    (
        "report-waits-for-the-journeys-only",
        _set(["jobs", "report", "needs"], "journeys"),
        "wait for the journeys and the demo",
    ),
    (
        "report-only-after-the-journeys-verdicts",
        _set(
            ["jobs", "report", "if"],
            "always() && needs.journeys.outputs.verdicts == 'true'",
        ),
        "after verdicts",
    ),
    (
        "report-drops-the-demo-verdicts",
        _report_download(1, lambda steps, step: steps.remove(step)),
        "download each job's verdicts",
    ),
    (
        "report-downloads-the-demo-verdicts-unconditionally",
        _report_download(1, lambda steps, step: step.pop("if")),
        "download each job's verdicts",
    ),
    (
        "report-reads-one-file",
        _replace_in(
            "Decide and render",
            '--verdicts-dir "$RUNNER_TEMP/verdicts"',
            '--verdicts "$RUNNER_TEMP/verdicts/journeys/verdicts.json"',
        ),
        "read both jobs' verdicts together",
    ),
    (
        "annotation-with-a-value",
        _replace_in(
            POST,
            "'::error title=Docs journeys::the report wrote an unknown action'",
            '"::error title=Docs journeys::$ACTION"',
        ),
        "non-literal",
    ),
    (
        "alarm-needs-misspelt",
        _alarm_env("REPORT_RESULT", "${{ needs.reprot.result }}"),
        "reads needs.reprot, which it does not wait for",
    ),
    (
        "alarm-output-misspelt",
        _alarm_env("DEMO_VERDICTS", "${{ needs.demo.outputs.verdict }}"),
        "needs.demo.outputs.verdict, which demo does not declare",
    ),
    (
        "alarm-reads-the-journeys-verdicts-twice",
        _alarm_env("DEMO_VERDICTS", "${{ needs.journeys.outputs.verdicts }}"),
        "the alarm's environment",
    ),
    (
        "alarm-only-when-the-report-succeeded",
        _set(["jobs", ALARM_JOB, "if"], "success()"),
        "does not run in every run not cancelled",
    ),
    (
        "alarm-condition-dropped",
        lambda doc, text: (doc["jobs"][ALARM_JOB].pop("if") and doc, text),
        "does not run in every run not cancelled",
    ),
    (
        "alarm-does-not-wait-for-the-report",
        _set(["jobs", ALARM_JOB, "needs"], ["journeys", "demo"]),
        "reads needs.report, which it does not wait for",
    ),
    (
        "alarm-without-a-timeout",
        lambda doc, text: (doc["jobs"][ALARM_JOB].pop("timeout-minutes") and doc, text),
        "the alarm job has no timeout",
    ),
    (
        "alarm-may-write-contents",
        _set(
            ["jobs", ALARM_JOB, "permissions"], {"contents": "write", "issues": "write"}
        ),
        "report-alarm's permissions",
    ),
    (
        "alarm-posts-on-any-run",
        _alarm_step(
            POST, lambda s: s.update({"if": "github.ref == 'refs/heads/main'"})
        ),
        "a step that posts is not the",
    ),
    (
        "alarm-posts-the-reports-posts",
        _alarm_step(
            POST,
            lambda s: s.update(
                {"run": s["run"].replace("/docs-alarm/posts/", "/docs-issues/posts/")}
            ),
        ),
        "report-alarm's posting step does not loop over docs-alarm/posts",
    ),
    (
        "alarm-writes-elsewhere",
        _alarm_step(
            ALARM_DECIDE,
            lambda s: s.update({"run": s["run"].replace("docs-alarm", "docs-issues")}),
        ),
        "report-alarm's posts are not written to docs-alarm",
    ),
    (
        "alarm-decided-only-on-a-schedule",
        _alarm_step(ALARM_DECIDE, lambda s: s.update({"if": POST_IF})),
        "not decided by docs_journeys_report.py alarm",
    ),
    (
        "alarm-decide-step-reads-gh",
        _alarm_step(
            ALARM_DECIDE,
            lambda s: s.update({"run": "gh issue list --label x\n" + s["run"]}),
        ),
        "a step that posts is not the",
    ),
    (
        "alarm-checks-out-everything",
        lambda doc, text: (
            doc["jobs"][ALARM_JOB]["steps"][0].update(
                {"with": {"persist-credentials": False}}
            )
            or doc,
            text,
        ),
        "checks out more than",
    ),
    (
        "report-gets-a-second-posting-step",
        _add_step(
            "report",
            {
                "name": POST,
                "if": POST_IF,
                "run": 'gh issue comment 1 --repo "$GITHUB_REPOSITORY"'
                ' --body-file "$DIR/body.md"',
            },
        ),
        "report has 2 posting steps, not one",
    ),
    (
        "demo-job-posts",
        _add_step(
            "demo",
            {
                "name": POST,
                "if": POST_IF,
                "run": 'gh issue comment 1 --repo "$GITHUB_REPOSITORY"'
                ' --body-file "$DIR/body.md"',
            },
        ),
        "demo posts, and is not a posting job",
    ),
    (
        "alarm-job-dropped",
        lambda doc, text: (doc["jobs"].pop(ALARM_JOB) and doc, text),
        "there is no report-alarm job",
    ),
    (
        "report-decides-on-error",
        _step_set("Decide and render", "continue-on-error", True),
        "report: 'Decide and render' carries continue-on-error",
    ),
    (
        "report-job-continues-on-error-by-an-expression",
        _set(
            ["jobs", "report", "continue-on-error"],
            "${{ github.event_name != 'schedule' }}",
        ),
        "report carries continue-on-error",
    ),
    (
        "alarm-decides-on-error",
        _alarm_step(ALARM_DECIDE, lambda s: s.update({"continue-on-error": True})),
        "report-alarm: 'Decide the alarm' carries continue-on-error",
    ),
    (
        "alarm-posts-on-error-by-an-expression",
        _alarm_step(
            POST,
            lambda s: s.update(
                {"continue-on-error": "${{ github.event_name == 'schedule' }}"}
            ),
        ),
        f"report-alarm: {POST!r} carries continue-on-error",
    ),
    (
        "alarm-job-continues-on-error",
        _set(["jobs", ALARM_JOB, "continue-on-error"], True),
        "report-alarm carries continue-on-error",
    ),
    (
        "walk-continue-on-error-false",
        _step_set(WALK, "continue-on-error", False),
        f"journeys: {WALK!r} carries continue-on-error",
    ),
]


@pytest.mark.parametrize(
    "plant, fragment", [p[1:] for p in PLANTS], ids=[p[0] for p in PLANTS]
)
def test_each_rule_fires_on_a_planted_defect(plant, fragment):
    doc, text = real()
    doc, text = plant(copy.deepcopy(doc), text)
    found = workflow_problems(doc, text)
    assert any(fragment in problem for problem in found), found


# ---------------------------------------------------------------------------
# The posting steps, run: the gh calls each post makes
# ---------------------------------------------------------------------------
#: Not this repository: were a real ``gh`` ever reached, it has no credentials
#: (no token in the environment, an empty config directory) and no repository
#: of ours to write to.
FAKE_REPOSITORY = "example-org/example-repo"
FAKE_GH = """#!/bin/sh
for arg in "$@"; do printf '%s\\037' "$arg"; done >> "$GH_LOG"
printf '\\n' >> "$GH_LOG"
if [ "$1 $2" = "issue list" ]; then printf '%s\\n' "$GH_ISSUES"; fi
"""


def posting_script(doc, job: str) -> str:
    (step,) = [s for s in _job_steps(doc, job) if s.get("name") == POST]
    return step["run"]


def write_post(root: Path, number: int, **files: str) -> None:
    target = root / "posts" / f"{number:02d}"
    target.mkdir(parents=True)
    for name, text in files.items():
        (target / name.replace("_", ".")).write_text(text)


def run_posting(
    tmp_path: Path, script: str, step_env: Optional[Dict[str, str]] = None
) -> Tuple[int, List[List[str]]]:
    """The step's script under ``bash -e`` from the repository root, a fake
    ``gh`` first on PATH (it answers ``issue list`` with ``$GH_ISSUES``); its
    exit status and each gh call's arguments. The posts are under
    ``tmp_path/runner`` (``$RUNNER_TEMP``)."""
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    (fake / "gh").write_text(FAKE_GH)
    (fake / "gh").chmod(0o755)
    if not (fake / "python3").exists():
        (fake / "python3").symlink_to(sys.executable)
    (tmp_path / "gh-config").mkdir(exist_ok=True)
    log = tmp_path / "gh.log"
    log.write_text("")
    env = {
        "PATH": f"{fake}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "HOME": str(tmp_path),
        "GH_CONFIG_DIR": str(tmp_path / "gh-config"),
        "GH_LOG": str(log),
        "GH_ISSUES": "[]",
        "PYTHONDONTWRITEBYTECODE": "1",
        "RUNNER_TEMP": str(tmp_path / "runner"),
        "GITHUB_REPOSITORY": FAKE_REPOSITORY,
        **(step_env or {}),
    }
    assert "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env
    assert shutil.which("gh", path=env["PATH"]) == str(fake / "gh")
    done = subprocess.run(
        ["bash", "-e", "-c", script],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    calls = [line.split("\x1f")[:-1] for line in log.read_text().splitlines()]
    return done.returncode, calls


def label_create() -> List[str]:
    return [
        "label",
        "create",
        "docs-journey-failure",
        "--repo",
        FAKE_REPOSITORY,
        "--color",
        "B60205",
        "--description",
        "Docs journey failed",
    ]


def issue_create(directory: str, title: str = "A rendered title") -> List[str]:
    return [
        "issue",
        "create",
        "--repo",
        FAKE_REPOSITORY,
        "--title",
        title,
        "--label",
        "docs-journey-failure",
        "--body-file",
        f"{directory}/body.md",
    ]


def issue_comment(number: str, directory: str) -> List[str]:
    return [
        "issue",
        "comment",
        number,
        "--repo",
        FAKE_REPOSITORY,
        "--body-file",
        f"{directory}/body.md",
    ]


def close(number: str) -> List[str]:
    return [
        "api",
        "--method",
        "PATCH",
        f"repos/{FAKE_REPOSITORY}/issues/{number}",
        "-f",
        "state=closed",
        "-f",
        "state_reason=completed",
    ]


def three_posts(root: Path) -> Dict[str, str]:
    """An open, a comment and a close, as the script writes them; each
    directory as the step's glob names it (with its trailing slash)."""
    write_post(root, 1, action="open\n", title_txt="A rendered title\n", body_md="b\n")
    write_post(root, 2, action="comment\n", issue="41\n", body_md="b\n")
    write_post(root, 3, action="close\n", issue="42\n", body_md="b\n")
    return {
        action: f"{root}/posts/{number}/"
        for action, number in (("open", "01"), ("comment", "02"), ("close", "03"))
    }


def test_the_report_posts_each_action_as_the_report_wrote_it(tmp_path):
    """The report's open creates the label and the issue; its comment and its
    close comment the rendered body; the close then closes the issue."""
    doc, _ = real()
    dirs = three_posts(tmp_path / "runner" / "docs-issues")
    code, calls = run_posting(tmp_path, posting_script(doc, "report"))
    assert code == 0
    assert calls == [
        label_create(),
        issue_create(dirs["open"]),
        issue_comment("41", dirs["comment"]),
        issue_comment("42", dirs["close"]),
        close("42"),
    ]


def alarm_calls(dirs: Dict[str, str]) -> List[List[str]]:
    """The alarm's open and comment are the report's; its close only closes."""
    return [
        label_create(),
        issue_create(dirs["open"]),
        issue_comment("41", dirs["comment"]),
        close("42"),
    ]


def test_the_alarm_posts_each_action_and_closes_without_a_comment(tmp_path):
    """The alarm has no closing text: a body.md left in its close's directory
    is not read."""
    doc, _ = real()
    dirs = three_posts(tmp_path / "runner" / "docs-alarm")
    assert run_posting(tmp_path, posting_script(doc, ALARM_JOB)) == (
        0,
        alarm_calls(dirs),
    )


ALARM_TITLE = "Docs journeys report is red"
ISSUE_LIST = [
    "issue",
    "list",
    "--repo",
    FAKE_REPOSITORY,
    "--state",
    "open",
    "--label",
    "docs-journey-failure",
    "--limit",
    "100",
    "--json",
    "number,title,author",
]


def run_alarm_job(
    tmp_path: Path, night: Tuple[str, str, str], issues: List[Dict[str, Any]]
) -> Tuple[int, List[List[str]]]:
    """The alarm job's decide step, then its posting step, as a scheduled run
    on main runs them: each name the decide step's ``env`` maps, with the
    value GitHub would give it."""
    doc, _ = real()
    decide = _in_job(doc, ALARM_JOB, ALARM_DECIDE)
    report_result, journeys, demo = night
    given = {
        "EVENT": "schedule",
        "REF": "refs/heads/main",
        "REPORT_RESULT": report_result,
        "JOURNEYS_VERDICTS": journeys,
        "DEMO_VERDICTS": demo,
    }
    assert set(given) == set(decide["env"]) - {"GH_TOKEN"}
    step_env = {
        **given,
        "GITHUB_SHA": "c6e0f081d82f1a858e111c86db62aa5d8b8a71d4",
        "GITHUB_RUN_ID": "37456943504",
        "GH_ISSUES": json.dumps(issues),
    }
    script = decide["run"] + "\n" + posting_script(doc, ALARM_JOB)
    return run_posting(tmp_path, script, step_env)


def test_the_alarm_job_opens_the_alarm_when_the_report_failed(tmp_path):
    """The decide step's posts are what the posting step reads: one open,
    titled from the template, labelled, its body the rendered file."""
    code, calls = run_alarm_job(tmp_path, ("failure", "true", "true"), [])
    opened = f"{tmp_path}/runner/docs-alarm/posts/01/"
    assert code == 0
    assert calls == [ISSUE_LIST, label_create(), issue_create(opened, ALARM_TITLE)]
    body = Path(opened, "body.md").read_text()
    assert f"https://github.com/{FAKE_REPOSITORY}/actions/runs/37456943504" in body


def test_the_alarm_job_closes_the_alarm_on_a_healthy_run(tmp_path):
    alarm = {
        "number": 90,
        "title": ALARM_TITLE,
        "author": {"login": "app/github-actions"},
    }
    code, calls = run_alarm_job(tmp_path, ("success", "true", "true"), [alarm])
    assert (code, calls) == (0, [ISSUE_LIST, close("90")])


@pytest.mark.parametrize("job", sorted(POSTING_JOBS))
def test_a_posting_step_with_nothing_to_post_calls_no_gh(tmp_path, job):
    doc, _ = real()
    (tmp_path / "runner" / POSTING_JOBS[job] / "posts").mkdir(parents=True)
    assert run_posting(tmp_path, posting_script(doc, job)) == (0, [])


@pytest.mark.parametrize("job", sorted(POSTING_JOBS))
def test_a_posting_step_refuses_an_unknown_action(tmp_path, job):
    doc, _ = real()
    write_post(tmp_path / "runner" / POSTING_JOBS[job], 1, action="reopen\n")
    assert run_posting(tmp_path, posting_script(doc, job)) == (1, [])


@pytest.mark.parametrize(
    "plant",
    [
        # The alarm's close comments a body first, as the report's does.
        lambda s: s.replace(
            'ISSUE="$(head -n 1 "$DIR/issue")"\n',
            'ISSUE="$(head -n 1 "$DIR/issue")"\n'
            'gh issue comment "$ISSUE" --repo "$GITHUB_REPOSITORY"'
            ' --body-file "$DIR/body.md" > /dev/null\n',
        ),
        # The alarm's open forgets its label.
        lambda s: s.replace(
            " --label docs-journey-failure --body-file", " --body-file"
        ),
        # An unknown action is passed over.
        lambda s: s.replace("exit 1", "continue"),
    ],
    ids=["close-with-a-comment", "open-unlabelled", "unknown-passed-over"],
)
def test_the_run_of_the_alarms_posting_step_fires_on_a_planted_defect(tmp_path, plant):
    """The real step makes the three posts' calls, then fails on a fourth with
    an unknown action; each planted copy of it does something else."""
    doc, _ = real()
    script = posting_script(doc, ALARM_JOB)
    planted = plant(script)
    assert planted != script, "the plant changed nothing"
    root = tmp_path / "runner" / "docs-alarm"
    dirs = three_posts(root)
    write_post(root, 4, action="reopen\n")
    assert run_posting(tmp_path, script) == (1, alarm_calls(dirs))
    assert run_posting(tmp_path, planted) != (1, alarm_calls(dirs))


def test_the_release_checklist_dispatches_the_journeys_after_the_docs_deploy():
    """CLAUDE.md's release section runs docs-journeys.yml on the tag, after
    docs.yml; release-please.yml starts neither."""
    claude = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    docs = claude.index("gh workflow run docs.yml --ref vX.Y.Z")
    journeys = claude.index("gh workflow run docs-journeys.yml --ref vX.Y.Z")
    assert docs < journeys
    # A red right after a deploy is run once more before it is read as a defect.
    assert "dispatched once more" in claude[journeys : journeys + 1000]
    release_please = (
        REPO_ROOT / ".github" / "workflows" / "release-please.yml"
    ).read_text(encoding="utf-8")
    assert "docs-journeys" not in release_please
