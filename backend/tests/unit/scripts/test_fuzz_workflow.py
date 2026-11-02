"""``fuzz.yml`` runs the three API fuzzing passes nightly and by hand, holds
only what each job needs, and posts only rendered text, only from a scheduled
run on ``main``.

The workflow runs in a public repository and posts issues, so its shape is
pinned rather than reviewed once:

* triggers: one daily cron on a minute other than :00, :30 and the synthetic
  check's :17 and :47, and a manual dispatch whose one input is the string
  ``fuzz_seed`` (default empty: the lists' seed); nothing a pull request or a
  push can start;
* ``permissions: {contents: read}`` at the top; only the report job adds
  ``issues: write``; no ``id-token``, no ``secrets``; one concurrency group
  per ref, never cancelled;
* the three passes call ``_platform.yml`` with suite ``fuzz`` and profile
  ``full``, each with its data seed, the seed job's fuzz seed and the same
  ``api_env``;
* the report job runs after the three passes whatever they concluded, as long
  as the seed was decided; it hands each pass's result and summary values to
  ``scripts/fuzz_report.py`` through ``env``;
* every step whose script runs any ``gh`` command is the one posting step,
  and it runs only on a scheduled run on ``main``; it posts with
  ``--body-file`` from a rendered file, the title is the rendered title
  file's first line, it never closes, edits or reopens, and no script holds
  an expression, prints from the shell or annotates with non-literal text.

Every rule is also planted against the real workflow below, so a rule that
stops firing fails here.
"""

from __future__ import annotations

import copy
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from backend.tests.unit.infrastructure.test_docs_only_gate import _load

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "fuzz.yml"
AUTOMERGE = REPO_ROOT / ".github" / "workflows" / "dependabot-automerge.yml"
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"

PASSES = {"superuser": "demo", "viewer": "demo", "sdk": "demo,sdk-contract"}
JOBS = {"seed", "superuser", "viewer", "sdk", "report"}
REPORT_PERMISSIONS = {"contents": "read", "issues": "write"}
REPORT_IF = "always() && needs.seed.result == 'success'"
REPORT_NEEDS = ["seed", "superuser", "viewer", "sdk"]
DECIDE = "Decide and render"
DECIDE_RUN = (
    'python3 scripts/fuzz_report.py --event "$EVENT" --ref "$REF"'
    ' --out "$RUNNER_TEMP/fuzz-issue"'
)
DECIDE_ENV = {
    "GH_TOKEN": "${{ github.token }}",
    "EVENT": "${{ github.event_name }}",
    "REF": "${{ github.ref }}",
    **{
        f"FUZZ_{name.upper()}_{part.upper()}": (
            f"${{{{ needs.{name}.{'result' if part == 'result' else 'outputs.fuzz_values'} }}}}"
        )
        for name in PASSES
        for part in ("result", "values")
    },
}
POST = "Post to the fuzz-failure issue"
POST_IF = "github.event_name == 'schedule' && github.ref == 'refs/heads/main'"
RENDERED_BODY = '"$DIR/body.md"'
RENDERED_TITLE = '"$(head -n 1 "$DIR/title.txt")"'
POSTS = re.compile(
    r"\bgh\s+(?:issue|pr)\s+(?:create|comment|edit|close|reopen|review)\b"
)
#: Any ``gh`` command at all: a step that runs one is treated as posting,
#: whatever its verb, so it must be the POST step with POST_IF.
GH_COMMAND = re.compile(r"(?:^|[\s;&|(`])gh\s+[a-z]")


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


def _posts(step: Dict[str, Any]) -> bool:
    script = str(step.get("run") or "").replace("\\\n", " ")
    return any(
        GH_COMMAND.search(line)
        for line in script.splitlines()
        if not line.lstrip().startswith("#")
    )


def _named(doc, name):
    return next((s for s in steps_of(doc) if s.get("name") == name), None)


def _api_env(job: Dict[str, Any]) -> List[str]:
    return str((job.get("with") or {}).get("api_env", "")).split()


def workflow_problems(doc: Dict[Any, Any], text: str) -> List[str]:
    found: List[str] = []
    on = triggers(doc)
    if set(on) != {"schedule", "workflow_dispatch"}:
        found.append(f"triggers are {sorted(on)}, not schedule and workflow_dispatch")
    crons = [entry.get("cron", "") for entry in on.get("schedule") or []]
    if len(crons) != 1 or not re.fullmatch(r"[0-9]{1,2} [0-9]{1,2} \* \* \*", crons[0]):
        found.append(f"the schedule {crons} is not one daily cron at a fixed minute")
    elif int(crons[0].split()[0]) in (0, 17, 30, 47):
        found.append(f"cron {crons[0]!r} starts on :00, :30 or the synthetic check's")
    inputs = ((on.get("workflow_dispatch") or {}).get("inputs")) or {}
    seed = inputs.get("fuzz_seed") or {}
    if (
        set(inputs) != {"fuzz_seed"}
        or seed.get("type") != "string"
        or seed.get("default") != ""
    ):
        found.append(
            "the dispatch's inputs are not exactly fuzz_seed, a string, default empty"
        )
    if doc.get("permissions") != {"contents": "read"}:
        found.append(f"permissions are {doc.get('permissions')}, not contents read")
    if "id-token" in text:
        found.append("id-token appears in the workflow")
    if "secrets." in text or "secrets: inherit" in text:
        found.append("the workflow reads a secret")
    if doc.get("concurrency") != {
        "group": "fuzz-${{ github.ref }}",
        "cancel-in-progress": False,
    }:
        found.append("concurrency is not one group per ref, never cancelled")

    jobs = doc["jobs"]
    if set(jobs) != JOBS:
        found.append(f"jobs are {sorted(jobs)}, not {sorted(JOBS)}")
    for job_id, job in jobs.items():
        expected = REPORT_PERMISSIONS if job_id == "report" else None
        if job.get("permissions") != expected:
            found.append(f"{job_id}'s permissions are {job.get('permissions')}")
        if "environment" in job or "secrets" in job:
            found.append(f"{job_id} is bound to an environment or given secrets")
    envs = set()
    for name, data_seed in PASSES.items():
        job = jobs.get(name) or {}
        given = job.get("with") or {}
        if (
            job.get("uses") != "./.github/workflows/_platform.yml"
            or given.get("suite") != "fuzz"
            or given.get("profile") != "full"
            or given.get("fuzz_pass") != name
            or given.get("seed") != data_seed
            or given.get("fuzz_seed") != "${{ fromJSON(needs.seed.outputs.fuzz_seed) }}"
            or job.get("needs") != "seed"
        ):
            found.append(f"the {name} pass does not call the fuzz arm as planned")
        envs.add(tuple(_api_env(job)))
    if len(envs) != 1:
        found.append("the passes' api_env differ")
    report = jobs.get("report") or {}
    if _condition(report) != REPORT_IF or report.get("needs") != REPORT_NEEDS:
        found.append("the report job does not run after all three passes")
    decide = _named(doc, DECIDE)
    if (
        decide is None
        or decide.get("run") != DECIDE_RUN
        or decide.get("env") != (DECIDE_ENV)
    ):
        found.append(
            "the report does not hand each pass's result and values to"
            " fuzz_report.py through env"
        )
    for step in steps_of(doc):
        for name, value in (step.get("env") or {}).items():
            if name == "GH_TOKEN" and value != "${{ github.token }}":
                found.append("GH_TOKEN is not the workflow's own token")
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
        if re.search(r"(^|[\s;|&(])(echo|set -x|env|printenv|cat)\b", line):
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
        if re.search(r"\bgh\s+api\b", line):
            found.append(f"a gh api call: {stripped}")
        if "GITHUB_STEP_SUMMARY" in line:
            found.append(
                f"writes a step summary (the passes write their own): {stripped}"
            )
    return found


def real():
    return _load(WORKFLOW), WORKFLOW.read_text(encoding="utf-8")


PLATFORM = REPO_ROOT / ".github" / "workflows" / "_platform.yml"


def _fuzz_summary_prints_to_the_log(text: str) -> bool:
    """The fuzz step cats summary.md into the job log inside a group, beside
    the append to the step summary (which `gh run view --log` does not show)."""
    return bool(
        re.search(
            r'cat "\$work/summary\.md" >> "\$GITHUB_STEP_SUMMARY"\n'
            r"(?:[ \t]*#[^\n]*\n)?"
            r'[ \t]*echo "::group::Fuzz summary"\n'
            r'[ \t]*cat "\$work/summary\.md"\n'
            r'[ \t]*echo "::endgroup::"\n',
            text,
        )
    )


def test_the_fuzz_step_prints_its_summary_to_the_job_log():
    text = PLATFORM.read_text(encoding="utf-8")
    assert _fuzz_summary_prints_to_the_log(text)
    planted = text.replace('echo "::group::Fuzz summary"\n', "", 1)
    assert planted != text and not _fuzz_summary_prints_to_the_log(planted)
    planted = text.replace(
        '                cat "$work/summary.md"\n                echo "::endgroup::"',
        '                echo "::endgroup::"',
        1,
    )
    assert planted != text and not _fuzz_summary_prints_to_the_log(planted)


def test_the_fuzz_workflow_keeps_its_shape():
    doc, text = real()
    assert len(steps_of(doc)) >= 4, "found almost no steps: the reader is broken"
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


def _drop_trigger(name):
    def plant(doc, text):
        del triggers(doc)[name]
        return doc, text

    return plant


def _step_set(name, key, value):
    def plant(doc, text):
        _named(doc, name)[key] = value
        return doc, text

    return plant


def _replace_in(name, old, new):
    def plant(doc, text):
        step = _named(doc, name)
        assert old in step["run"], old
        step["run"] = step["run"].replace(old, new, 1)
        return doc, text

    return plant


def _add_step(job, step):
    def plant(doc, text):
        doc["jobs"][job]["steps"].append(step)
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


def _with(job, key, value):
    def plant(doc, text):
        doc["jobs"][job]["with"][key] = value
        return doc, text

    return plant


SEED_INPUT = {"description": "x", "type": "string", "default": ""}
PLANTS: List[tuple] = [
    ("pull-request-trigger", _add_trigger("pull_request"), "triggers are"),
    ("push-trigger", _add_trigger("push"), "triggers are"),
    ("dispatch-only", _drop_trigger("schedule"), "triggers are"),
    ("cron-on-the-hour", _schedule("0 5 * * *"), ":00"),
    ("cron-with-the-synthetic-check", _schedule("17 5 * * *"), ":00"),
    ("cron-every-hour", _schedule("23 * * * *"), "daily"),
    ("cron-twice", _schedule("23 5,17 * * *"), "daily"),
    (
        "seed-with-a-default",
        _dispatch_inputs({"fuzz_seed": {**SEED_INPUT, "default": "1"}}),
        "fuzz_seed",
    ),
    (
        "another-input",
        _dispatch_inputs({"fuzz_seed": SEED_INPUT, "passes": {"type": "string"}}),
        "fuzz_seed",
    ),
    (
        "top-issues-write",
        _set(["permissions"], {"contents": "read", "issues": "write"}),
        "permissions are",
    ),
    (
        "a-pass-may-post",
        _set(["jobs", "viewer", "permissions"], {"issues": "write"}),
        "viewer's permissions",
    ),
    (
        "report-writes-contents",
        _set(
            ["jobs", "report", "permissions"], {"contents": "write", "issues": "write"}
        ),
        "report's permissions",
    ),
    (
        "id-token",
        _text("issues: write", "issues: write\n      id-token: write"),
        "id-token",
    ),
    ("a-secret", _text("${{ github.token }}", "${{ secrets.GITHUB_TOKEN }}"), "secret"),
    ("cancelled", _set(["concurrency", "cancel-in-progress"], True), "concurrency"),
    ("one-group", _set(["concurrency", "group"], "fuzz"), "concurrency"),
    ("a-pass-on-core", _with("sdk", "profile", "core"), "sdk pass"),
    ("a-pass-not-fuzz", _with("viewer", "suite", "smoke"), "viewer pass"),
    ("a-pass-fixed-seed", _with("superuser", "fuzz_seed", 1), "superuser pass"),
    (
        "a-pass-without-retries-cap",
        _with(
            "viewer",
            "api_env",
            "AWS_ENDPOINT_URL=http://127.0.0.1:9\nAWS_ACCESS_KEY_ID=fuzz\n",
        ),
        "api_env differ",
    ),
    (
        "report-only-on-success",
        _set(["jobs", "report", "if"], "success()"),
        "after all three",
    ),
    (
        "report-skips-a-pass",
        _set(["jobs", "report", "needs"], ["seed", "superuser", "viewer"]),
        "after all three",
    ),
    (
        "values-not-handed-over",
        _step_set(
            DECIDE,
            "env",
            {k: v for k, v in DECIDE_ENV.items() if "SDK_VALUES" not in k},
        ),
        "through env",
    ),
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
        "second-step-creates-a-label",
        _add_step("report", {"name": "Label again", "run": "gh label create x"}),
        "a step that posts",
    ),
    (
        "second-step-api-two-spaces",
        _add_step("report", {"name": "Api again", "run": "gh  api repos/x/y/issues"}),
        "a step that posts",
    ),
    (
        "decide-step-lists-issues",
        _replace_in(DECIDE, "python3", "gh issue list --label fuzz-failure; python3"),
        "a step that posts",
    ),
    (
        "post-step-copy-without-condition",
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
        "inline-body",
        _replace_in(
            POST, '--body-file "$DIR/body.md" > /dev/null', '--body "$X" > /dev/null'
        ),
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
        "closes-the-issue",
        _replace_in(POST, 'gh issue comment "$ISSUE"', 'gh issue close "$ISSUE"'),
        "other than create or comment",
    ),
    (
        "api-close",
        _replace_in(
            POST,
            'gh issue comment "$ISSUE"',
            'gh api --method PATCH "repos/$GITHUB_REPOSITORY/issues/$ISSUE" -f state=closed; gh issue comment "$ISSUE"',
        ),
        "gh api",
    ),
    (
        "expression-in-script",
        _replace_in(POST, '"$GITHUB_REPOSITORY"', '"${{ github.repository }}"'),
        "expression inside a script",
    ),
    (
        "print-from-the-shell",
        _replace_in(POST, "set -euo pipefail", 'set -euo pipefail\necho "$OPENED"'),
        "prints from the shell",
    ),
    (
        "cat-a-file",
        _replace_in(POST, "set -euo pipefail", 'set -euo pipefail\ncat "$DIR/body.md"'),
        "prints from the shell",
    ),
    (
        "annotation-with-a-value",
        _replace_in(
            POST,
            "'::error title=API fuzzing::the report wrote an unknown action'",
            '"::error title=API fuzzing::$ACTION"',
        ),
        "non-literal",
    ),
    (
        "writes-a-summary",
        _replace_in(
            POST,
            "set -euo pipefail",
            'set -euo pipefail\nprintf x >> "$GITHUB_STEP_SUMMARY"',
        ),
        "step summary",
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
# The post step itself, run in bash with a fake gh
# ---------------------------------------------------------------------------


def _post_script() -> str:
    doc, _ = real()
    return _named(doc, POST)["run"]


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_the_post_step_opens_then_comments_on_the_issue_it_opened(tmp_path):
    """A run with two red passes and no open issue: the first opens it, the
    second comments on the number the first got back."""
    posts = tmp_path / "fuzz-issue" / "posts"
    for number, (action, issue) in enumerate((("open", None), ("comment", "new")), 1):
        target = posts / f"{number:02d}"
        target.mkdir(parents=True)
        (target / "action").write_text(action + "\n")
        if issue:
            (target / "issue").write_text(issue + "\n")
        (target / "title.txt").write_text("API fuzzing is red\n")
        (target / "body.md").write_text("body\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh.log"
    (bin_dir / "gh").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'if [ "$1 $2" = "issue create" ]; then\n'
        '  printf "https://github.com/x/y/issues/4242\\n"\n'
        "fi\n"
    )
    (bin_dir / "gh").chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", _post_script()],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_REPOSITORY": "x/y",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result
    calls = log.read_text().splitlines()
    assert calls[0].startswith("label create fuzz-failure")
    assert calls[1].startswith("issue create --repo x/y --title API fuzzing is red")
    assert calls[2].replace("//", "/") == (
        f"issue comment 4242 --repo x/y --body-file {posts / '02' / 'body.md'}"
    )
    assert result.stdout == ""


# ---------------------------------------------------------------------------
# A Schemathesis or Hypothesis bump is not merged automatically
# ---------------------------------------------------------------------------


#: The decide step reads these from fetch-metadata, exactly; a renamed
#: output would hand the script an empty value and the rule below would never
#: see /tests/fuzz.
AUTOMERGE_DECIDE_ENV = {
    "UPDATE_TYPE": "${{ steps.meta.outputs.update-type }}",
    "ECOSYSTEM": "${{ steps.meta.outputs.package-ecosystem }}",
    "DIRECTORY": "${{ steps.meta.outputs.directory }}",
}


def _decide_step(doc=None) -> Dict[str, Any]:
    doc = doc or _load(AUTOMERGE)
    return next(s for s in doc["jobs"]["automerge"]["steps"] if s.get("id") == "decide")


def _decide_script() -> str:
    return _decide_step()["run"]


def automerge_problems(doc: Dict[str, Any]) -> List[str]:
    step = _decide_step(doc)
    if step.get("env") != AUTOMERGE_DECIDE_ENV:
        return [
            f"the decide step's env is {step.get('env')}, not {AUTOMERGE_DECIDE_ENV}"
        ]
    return []


def test_the_decide_step_reads_the_update_s_directory():
    assert automerge_problems(_load(AUTOMERGE)) == []


@pytest.mark.parametrize(
    "name, value",
    [
        ("DIRECTORY", "${{ steps.meta.outputs.directories }}"),
        ("DIRECTORY", "${{ steps.metadata.outputs.directory }}"),
        ("ECOSYSTEM", "${{ steps.meta.outputs.ecosystem }}"),
    ],
)
def test_a_renamed_metadata_output_is_caught(name, value):
    doc = copy.deepcopy(_load(AUTOMERGE))
    _decide_step(doc)["env"][name] = value
    assert any(name in problem for problem in automerge_problems(doc))


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
@pytest.mark.parametrize(
    ("ecosystem", "directory", "update", "merges"),
    [
        ("pip", "/tests/fuzz", "version-update:semver-patch", "no"),
        ("pip", "/tests/fuzz", "version-update:semver-minor", "no"),
        ("pip", "/backend", "version-update:semver-minor", "yes"),
        ("pip", "/", "version-update:semver-major", "no"),
        # no directory came through: it may be the fuzzer's, so it waits
        ("pip", "", "version-update:semver-patch", "no"),
        ("npm_and_yarn", "", "version-update:semver-patch", "yes"),
    ],
)
def test_a_fuzz_pin_bump_is_left_for_a_human(
    tmp_path, ecosystem, directory, update, merges
):
    output = tmp_path / "out"
    result = subprocess.run(
        ["bash", "-c", _decide_script()],
        env={
            "PATH": "/usr/bin:/bin",
            "GITHUB_OUTPUT": str(output),
            "UPDATE_TYPE": update,
            "ECOSYSTEM": ecosystem,
            "DIRECTORY": directory,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result
    assert output.read_text().strip() == f"ok={merges}"


def test_dependabot_watches_the_fuzz_pins():
    doc = _load(DEPENDABOT)
    entries = [
        u
        for u in doc["updates"]
        if u.get("package-ecosystem") == "pip"
        and "/tests/fuzz" in [u.get("directory"), *(u.get("directories") or [])]
    ]
    assert len(entries) == 1
    ignored = {i.get("dependency-name") for i in entries[0].get("ignore") or []}
    # The pins it shares with backend/requirements.txt move with the backend
    # entry; a bump of one here alone would fail the lock check.
    assert {"pytest", "PyYAML", "requests"} <= ignored
