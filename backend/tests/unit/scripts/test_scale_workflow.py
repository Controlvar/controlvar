# SPDX-FileCopyrightText: 2026 The Controlvar authors
# SPDX-License-Identifier: Apache-2.0
"""The scale-evidence workflow's shape (V30, C4, EMS-11, #1608).

The workflow fails loudly, needs no secrets, uploads only exact files, and
cannot bill: every job runs on ubuntu-24.04 and nothing else (a larger,
self-hosted or cloud runner is the founder's decision with its own plan). No
expression is interpolated into a script: that is
test_deploy_workflows.py's test, which covers this file too (PE-S13).
actionlint covers the syntax in the lint job.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "scale-evidence.yml"


def _scale_run():
    name = "scale_run_under_test"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, REPO_ROOT / "scripts" / "scale_run.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


sr = _scale_run()
#: The six published request types, in the page's order.
DEFAULT_SCENARIOS = " ".join(sr.se.SCENARIOS)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(
        not WORKFLOW.parent.is_dir(), reason="this tree has no .github/workflows"
    ),
]

#: The only runner label any job may name (EMS-11).
RUNNER = "ubuntu-24.04"
#: The only actions the workflow uses: GitHub's own.
ACTIONS = {
    "actions/checkout@v7",
    "actions/upload-artifact@v7",
    "actions/download-artifact@v8",
}
#: The two files each measure job uploads, and the merged one.
UPLOADS = {
    "measure": [
        "${{ runner.temp }}/scale-out/results.json",
        "${{ runner.temp }}/scale-out/rows.csv.gz",
    ],
    "merge": ["${{ runner.temp }}/scale-results/results.json"],
    "smoke": [
        "${{ runner.temp }}/scale-out/results.json",
        "${{ runner.temp }}/scale-out/rows.csv.gz",
    ],
}


def _load(text: str | None = None) -> Dict[str, Any]:
    document = yaml.safe_load(text if text is not None else WORKFLOW.read_text("utf-8"))
    document["on"] = document.pop(True, document.get("on"))
    return document


def _steps(document) -> Iterator[Tuple[str, Dict[str, Any]]]:
    for name, job in document["jobs"].items():
        for step in job.get("steps", []):
            yield name, step


def shape_problems(document: Dict[str, Any]) -> List[str]:
    """Everything V30 holds, each naming the job or step that breaks it."""
    problems: List[str] = []
    if document.get("permissions") != {"contents": "read"}:
        problems.append(f"workflow permissions are {document.get('permissions')}")
    if document.get("defaults") != {"run": {"shell": "bash"}}:
        problems.append(
            "the workflow's default shell is not bash: without it a pipe fails "
            "only on its last command (no pipefail)"
        )
    for name, job in document["jobs"].items():
        if job.get("runs-on") != RUNNER:
            problems.append(
                f"job '{name}' runs on {job.get('runs-on')!r}: only {RUNNER} is allowed"
            )
        allowed = (
            {"contents": "read", "actions": "read"} if name == "provenance" else None
        )
        if job.get("permissions", allowed) != allowed:
            problems.append(
                f"job '{name}' asks for permissions {job.get('permissions')}"
            )
        if "continue-on-error" in job:
            problems.append(f"job '{name}' continues on error")
        if not isinstance(job.get("timeout-minutes"), int):
            problems.append(f"job '{name}' has no timeout")
    text = yaml.safe_dump(document)
    for secret in re.findall(r"secrets\.[A-Za-z_]+", text):
        problems.append(f"uses {secret}: a fork cannot repeat this")
    for job, step in _steps(document):
        where = f"{job}: {step.get('name') or step.get('uses')}"
        if "continue-on-error" in step:
            problems.append(f"step '{where}' continues on error")
        if "shell" in step:
            problems.append(f"step '{where}' sets its own shell")
        script = step.get("run") or ""
        if re.search(r"\|\|\s*(true|:)\b|;\s*true\s*$", script, re.M):
            problems.append(f"step '{where}' swallows a failure")
        uses = step.get("uses")
        if uses and uses not in ACTIONS:
            problems.append(f"step '{where}' uses {uses}")
        if uses == "actions/checkout@v7":
            if (step.get("with") or {}).get("persist-credentials") is not False:
                problems.append(f"step '{where}' keeps the token in .git")
        if uses == "actions/upload-artifact@v7":
            options = step.get("with") or {}
            if step.get("if") != "always()":
                problems.append(f"step '{where}' uploads only on success")
            if options.get("if-no-files-found") != "error":
                problems.append(f"step '{where}' accepts uploading nothing")
            if options.get("retention-days") != 90:
                problems.append(
                    f"step '{where}' keeps its files {options.get('retention-days')} days"
                )
            paths = [
                p.strip()
                for p in str(options.get("path", "")).splitlines()
                if p.strip()
            ]
            for path in paths:
                if re.search(r"[*?\[]", path) or not path.endswith(
                    (".json", ".csv.gz")
                ):
                    problems.append(
                        f"step '{where}' uploads {path!r}, not an exact file"
                    )
                if "scale-work" in path or ".env" in path or "fixtures.json" in path:
                    problems.append(
                        f"step '{where}' uploads {path!r}, which holds secrets"
                    )
            if paths != UPLOADS.get(job):
                problems.append(f"step '{where}' uploads {paths}")
    return problems


def test_the_workflow_has_the_shape_v30_holds():
    assert shape_problems(_load()) == []


@pytest.mark.parametrize(
    "old, new, words",
    [
        (
            "runs-on: ubuntu-24.04\n    timeout-minutes: 60\n    strategy",
            "runs-on: ubuntu-latest-8-cores\n    timeout-minutes: 60\n    strategy",
            "job 'measure' runs on 'ubuntu-latest-8-cores': only ubuntu-24.04 is allowed",
        ),
        (
            "runs-on: ubuntu-24.04\n    timeout-minutes: 60\n    strategy",
            "runs-on: self-hosted\n    timeout-minutes: 60\n    strategy",
            "job 'measure' runs on 'self-hosted': only ubuntu-24.04 is allowed",
        ),
        (
            "GH_TOKEN: ${{ github.token }}",
            "GH_TOKEN: ${{ secrets.X }}",
            "uses secrets.X: a fork cannot repeat this",
        ),
        (
            "${{ runner.temp }}/scale-out/rows.csv.gz\n          if-no-files-found: error\n          retention-days: 90\n\n      - name: Take",
            "${{ runner.temp }}/scale-out/*.gz\n          if-no-files-found: error\n          retention-days: 90\n\n      - name: Take",
            "not an exact file",
        ),
        (
            "${{ runner.temp }}/scale-results/results.json\n",
            "${{ runner.temp }}/scale-results\n",
            "not an exact file",
        ),
        (
            "${{ runner.temp }}/scale-out/rows.csv.gz\n          if-no-files-found: error\n          retention-days: 90\n\n      - name: Take",
            "${{ runner.temp }}/scale-out/rows.csv.gz\n            ${{ runner.temp }}/scale-work/.env\n          if-no-files-found: error\n          retention-days: 90\n\n      - name: Take",
            "which holds secrets",
        ),
        ("defaults:\n  run:\n    shell: bash\n", "", "no pipefail"),
        (
            "permissions:\n  contents: read\n\nconcurrency",
            "permissions:\n  contents: write\n\nconcurrency",
            "workflow permissions",
        ),
        (
            "if-no-files-found: error",
            "if-no-files-found: warn",
            "accepts uploading nothing",
        ),
    ],
    ids=[
        "larger-runner",
        "self-hosted",
        "secret",
        "glob",
        "directory",
        "env-file",
        "no-pipefail",
        "write-permission",
        "no-files-ok",
    ],
)
def test_each_planted_defect_is_named(old, new, words):
    text = WORKFLOW.read_text("utf-8")
    assert old in text, old
    problems = shape_problems(_load(text.replace(old, new, 1)))
    assert any(words in problem for problem in problems), problems


def test_the_dispatch_is_six_request_types_times_three_runs_seven_at_once():
    document = _load()
    inputs = document["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"mode", "scenarios", "repetitions"}
    assert inputs["mode"]["type"] == "choice"
    assert inputs["mode"]["options"] == ["measure", "smoke"]
    assert inputs["mode"]["default"] == "measure"
    assert inputs["scenarios"]["default"] == DEFAULT_SCENARIOS
    matrix = sr.plan_matrix(
        inputs["scenarios"]["default"], inputs["repetitions"]["default"]
    )
    assert len(matrix) == 18
    measure = document["jobs"]["measure"]
    assert measure["strategy"] == {
        "fail-fast": False,
        "max-parallel": 7,
        "matrix": "${{ fromJSON(needs.plan.outputs.matrix) }}",
    }
    plan = " ".join(step.get("run", "") for step in document["jobs"]["plan"]["steps"])
    assert "scripts/scale_run.py plan" in plan
    assert measure["timeout-minutes"] == 60
    assert document["jobs"]["merge"]["needs"] == ["plan", "measure"]
    assert (
        document["jobs"]["merge"]["if"]
        == "${{ !cancelled() && needs.plan.result == 'success' }}"
    )


def test_the_pull_request_runs_the_smoke_and_provenance_and_never_measures():
    document = _load()
    assert document["on"]["pull_request"]["paths"] == [
        "tests/scale/**",
        "scripts/scale_stack.py",
        "scripts/scale_run.py",
        ".github/workflows/scale-evidence.yml",
        "docs/self-hosting/capacity.md",
        "docs/performance/scale-results/**",
    ]
    jobs = document["jobs"]
    # Only a dispatch measures; a dispatch with mode=smoke runs the smoke job
    # alone, so a branch that is never a pull request head can run it.
    assert jobs["plan"]["if"] == (
        "github.event_name == 'workflow_dispatch' && inputs.mode == 'measure'"
    )
    assert jobs["smoke"]["if"] == (
        "github.event_name == 'pull_request' || inputs.mode == 'smoke'"
    )
    assert jobs["provenance"]["if"] == "github.event_name == 'pull_request'"
    smoke = " ".join(step.get("run", "") for step in jobs["smoke"]["steps"])
    assert "--mode smoke --scenario mix" in smoke
    assert "scale_run.py calibrate" in smoke
    provenance = " ".join(step.get("run", "") for step in jobs["provenance"]["steps"])
    assert "scale_run.py provenance --base HEAD^1 --head HEAD" in provenance


def test_the_results_artifact_is_the_one_verify_downloads():
    merge = sr.se
    uploads = [
        step
        for name, step in _steps(_load())
        if name == "merge" and step.get("uses") == "actions/upload-artifact@v7"
    ]
    (upload,) = uploads
    assert upload["with"]["name"] == merge.RESULTS_ARTIFACT
    assert upload["with"]["path"].endswith("/" + merge.RESULTS_FILE)


def test_concurrency_is_per_pull_request_or_per_run():
    assert _load()["concurrency"] == {
        "group": "scale-evidence-${{ github.event.pull_request.number || github.run_id }}",
        "cancel-in-progress": True,
    }
