"""``rc-evidence.yml`` is started by hand at a release tag, starts nothing,
and runs exactly the suites ``scripts/rc_evidence.py`` expects (#1142).

Pinned, because the verifier's PASS is only as good as these:

* the one trigger is ``workflow_dispatch``, whose one input is the
  ``dispositions`` string;
* ``permissions: {contents: read}`` at the top; the suites jobs, which install
  packages, hold ``contents: read`` only; the verify job adds ``actions:
  read`` (the runs and artifacts it judges). Nothing anywhere holds a write
  (``actions: write`` would let a job dispatch or re-run the runs it judges);
* every job starts by refusing a ref that is not a ``vX.Y.Z`` tag, as a step
  that fails, so a dispatch elsewhere is red, never skipped;
* the python-suites matrix is ``SUITES`` but the frontend, which is a job of
  its own (jest, its report written as JUnit); each runs ``--suite`` and uploads its reports
  as the artifact the verifier downloads, for 90 days; the verify job runs
  after every leg (``always()``), reads them, never ``--staging``, and keeps
  its record for 90 days;
* no script holds an expression: inputs reach the shell through ``env``;
* every name the verifier matches on another workflow is a name there: the
  required and allowed-to-skip jobs, the 16 SDKs, the verdicts artifacts.

Each permission rule is also planted against the real workflow below, so a rule
that stops firing fails here.
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from backend.tests.unit.infrastructure.test_docs_only_gate import _load

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "rc-evidence.yml"

if not WORKFLOW.is_file():
    pytest.skip("this tree has no .github/workflows", allow_module_level=True)


def _script():
    spec = importlib.util.spec_from_file_location(
        "rc_evidence_for_workflow_test", REPO_ROOT / "scripts" / "rc_evidence.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rc = _script()

TAG_LINE = "if ! printf '%s\\n' \"$REF\" | grep -Eqx 'refs/tags/v[0-9]+\\.[0-9]+\\.[0-9]+'; then"
SUITE_RUN = (
    'python scripts/rc_evidence.py --suite "$SUITE" --junit-dir "$RUNNER_TEMP/junit"'
)
VERIFY_RUN = (
    'python3 scripts/rc_evidence.py "$TAG" --junit "$RUNNER_TEMP/junit"'
    ' --this-run "$RUN_ID" --dispositions "$DISPOSITIONS"'
    ' --out "$RUNNER_TEMP/rc-evidence"'
)
JOB_PERMISSIONS = {
    "python-suites": {"contents": "read"},
    "frontend-suite": {"contents": "read"},
    "verify": {"contents": "read", "actions": "read"},
}


@pytest.fixture(scope="module")
def doc() -> Dict[str, Any]:
    return _load(WORKFLOW)


def _on(doc: Dict[Any, Any]) -> Dict[str, Any]:
    on = doc.get("on", doc.get(True))
    return on if isinstance(on, dict) else dict.fromkeys(on)


def permission_problems(doc: Dict[str, Any]) -> List[str]:
    """Every way the workflow's token could hold more than its jobs read."""
    problems = []
    if doc.get("permissions") != {"contents": "read"}:
        problems.append(f"workflow permissions are {doc.get('permissions')!r}")
    blocks = [("workflow", doc.get("permissions"))] + [
        (name, job.get("permissions")) for name, job in doc["jobs"].items()
    ]
    for where, block in blocks:
        if not isinstance(block, dict):
            problems.append(f"{where}: permissions are {block!r}, not a mapping")
            continue
        for scope, level in block.items():
            if level != "read":
                problems.append(f"{where}: {scope}: {level}")
    for name, job in doc["jobs"].items():
        if job.get("permissions") != JOB_PERMISSIONS.get(name):
            problems.append(f"{name}: permissions {job.get('permissions')!r}")
    return problems


def test_dispatch_is_the_only_trigger_and_dispositions_the_only_input(doc):
    on = _on(doc)
    assert list(on) == ["workflow_dispatch"]
    inputs = on["workflow_dispatch"]["inputs"]
    assert list(inputs) == ["dispositions"]
    assert inputs["dispositions"]["type"] == "string"
    assert inputs["dispositions"]["default"] == ""


def test_the_token_reads_and_nothing_writes(doc):
    assert permission_problems(doc) == []
    assert set(doc["jobs"]) == set(JOB_PERMISSIONS)


@pytest.mark.parametrize(
    "plant",
    [
        lambda d: d["jobs"]["verify"]["permissions"].update({"actions": "write"}),
        lambda d: d["jobs"]["python-suites"]["permissions"].update(
            {"actions": "write"}
        ),
        lambda d: d["permissions"].update({"actions": "write"}),
        lambda d: d.update({"permissions": "write-all"}),
        lambda d: d["jobs"]["python-suites"]["permissions"].update({"actions": "read"}),
        lambda d: d["jobs"]["frontend-suite"]["permissions"].update(
            {"actions": "write"}
        ),
        lambda d: d["jobs"]["verify"].pop("permissions"),
        lambda d: d["jobs"].update({"extra": {"permissions": {"actions": "write"}}}),
    ],
    ids=[
        "actions-write-on-verify",
        "actions-write-on-suites",
        "actions-write-at-the-top",
        "write-all",
        "actions-read-on-an-installing-job",
        "actions-write-on-the-frontend-job",
        "verify-inherits",
        "a-new-job-with-actions-write",
    ],
)
def test_each_permission_rule_fires_on_the_real_workflow(doc, plant):
    planted = copy.deepcopy(doc)
    plant(planted)
    assert permission_problems(planted)


def test_every_job_fails_first_on_a_ref_that_is_not_a_release_tag(doc):
    for name, job in doc["jobs"].items():
        first = job["steps"][0]
        assert first["name"] == "Dispatched at a release tag", name
        assert "if" not in first
        assert first["env"] == {"REF": "${{ github.ref }}"}
        lines = [line.strip() for line in first["run"].splitlines()]
        assert TAG_LINE in lines and "exit 1" in lines, name
    assert "if" not in doc["jobs"]["python-suites"]
    assert "if" not in doc["jobs"]["frontend-suite"]
    assert doc["jobs"]["verify"]["if"] == "always()"


def test_the_matrix_is_the_scripts_suites(doc):
    job = doc["jobs"]["python-suites"]
    assert job["name"] == rc.SUITE_JOB.format("${{ matrix.suite }}")
    include = job["strategy"]["matrix"]["include"]
    assert [leg["suite"] for leg in include] == [
        s for s in rc.SUITES if s != rc.FRONTEND
    ]
    assert all(i.runner == "pytest" for leg in include for i in rc.SUITES[leg["suite"]])
    for leg in include:
        expected = (
            "experimentation_test"
            if leg["suite"] == "integration"
            else "experimentation"
        )
        assert leg["db"] == expected
    assert job["strategy"]["fail-fast"] is False
    assert job["services"]["postgres"]["env"]["POSTGRES_DB"] == "${{ matrix.db }}"
    assert "redis" in job["services"]
    run = next(s for s in job["steps"] if s.get("name") == "Run the suite")
    assert run["run"].strip() == SUITE_RUN
    assert run["env"] == {"SUITE": "${{ matrix.suite }}"}
    upload = next(
        s
        for s in job["steps"]
        if s.get("uses", "").startswith("actions/upload-artifact")
    )
    assert upload["with"]["name"] == rc.JUNIT_ARTIFACT.format("${{ matrix.suite }}")
    assert upload["with"]["retention-days"] == 90
    assert upload["if"] == "always()"


def test_the_frontend_job_runs_its_jest_suite_and_keeps_the_report(doc):
    job = doc["jobs"]["frontend-suite"]
    assert job["name"] == rc.suite_job(rc.FRONTEND) == rc.FRONTEND_JOB
    assert [i.runner for i in rc.SUITES[rc.FRONTEND]] == ["jest"]
    steps = job["steps"]
    assert any(s.get("run") == "cd frontend && npm ci" for s in steps)
    node = next(
        s for s in steps if str(s.get("uses", "")).startswith("actions/setup-node")
    )
    assert node["with"] == {"node-version-file": ".nvmrc"}
    run = next(s for s in steps if s.get("name") == "Run the suite")
    assert run["run"].strip() == SUITE_RUN
    assert run["env"] == {"SUITE": rc.FRONTEND}
    upload = next(
        s for s in steps if s.get("uses", "").startswith("actions/upload-artifact")
    )
    assert upload["with"]["name"] == rc.JUNIT_ARTIFACT.format(rc.FRONTEND)
    assert upload["with"]["retention-days"] == 90
    assert upload["if"] == "always()"


def test_the_verify_job_reads_every_leg_and_keeps_its_record(doc):
    job = doc["jobs"]["verify"]
    assert job["name"] == rc.VERIFY_JOB
    assert job["needs"] == ["python-suites", "frontend-suite"]
    steps = job["steps"]
    download = next(
        s for s in steps if s.get("uses", "").startswith("actions/download-artifact")
    )
    assert download["with"]["pattern"] == rc.JUNIT_ARTIFACT.format("*")
    assert download["with"]["merge-multiple"] is True
    read = next(s for s in steps if s.get("name") == "Read the evidence")
    assert " ".join(read["run"].split()) == VERIFY_RUN
    assert read["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "TAG": "${{ github.ref_name }}",
        "RUN_ID": "${{ github.run_id }}",
        "DISPOSITIONS": "${{ inputs.dispositions }}",
    }
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "--staging" not in text
    assert "GITHUB_STEP_SUMMARY" not in text, "the record, not the public summary"
    upload = next(
        s for s in steps if s.get("uses", "").startswith("actions/upload-artifact")
    )
    assert upload["with"]["retention-days"] == 90
    assert upload["with"]["path"] == "${{ runner.temp }}/rc-evidence"
    assert upload["if"] == "always()"
    installs = [s for s in steps if "pip install" in str(s.get("run", ""))]
    assert installs == [], "the verify job installs nothing"


def test_no_script_holds_an_expression_and_no_checkout_keeps_the_token(doc):
    for name, job in doc["jobs"].items():
        for step in job["steps"]:
            assert "${{" not in str(step.get("run", "")), (name, step.get("name"))
            if str(step.get("uses", "")).startswith("actions/checkout"):
                assert step["with"]["persist-credentials"] is False, name


def _job_names(workflow: str) -> List[str]:
    """Each job's name as a run shows it: a name over a list matrix, once per
    value (``chart-kind (${{ matrix.profile }})`` is ``chart-kind (core)`` ...)."""
    names = []
    for key, job in _load(WORKFLOWS / workflow)["jobs"].items():
        name = str(job.get("name", key))
        found = re.search(r"\$\{\{ matrix\.([a-z_-]+) \}\}", name)
        matrix = (job.get("strategy") or {}).get("matrix")
        values = (
            matrix.get(found.group(1)) if found and isinstance(matrix, dict) else None
        )
        if found and isinstance(values, list):
            names += [name.replace(found.group(0), str(v)) for v in values]
        else:
            names.append(name)
    return names


def test_every_workflow_the_verifier_reads_runs_as_it_counts_it():
    for workflow in rc.WORKFLOWS:
        on = _on(_load(WORKFLOWS / workflow.file))
        if workflow.at_tag:
            assert "workflow_dispatch" in on, workflow.file
        else:
            assert workflow.event == "push" and "main" in on["push"]["branches"]


def test_the_required_and_skippable_jobs_are_names_in_their_workflows():
    for workflow, names in rc.REQUIRED_JOBS.items():
        present = set(_job_names(workflow))
        if workflow == "sdk-unit-tests.yml":
            assert "${{ matrix.sdk }}" in present
            present |= set(rc.SDK_JOBS[:12])
        if workflow == "rc-evidence.yml":
            present |= {rc.SUITE_JOB.format(s) for s in rc.SUITES if s != rc.FRONTEND}
        assert set(names) <= present, (workflow, set(names) - present)
    for workflow, name in rc.ALLOWED_SKIPS:
        assert name in _job_names(workflow), (workflow, name)


def test_the_16_sdk_jobs_are_sdk_unit_tests_own_lists():
    text = (WORKFLOWS / "sdk-unit-tests.yml").read_text(encoding="utf-8")
    linux = ast.literal_eval(re.search(r"LINUX = (\{.*?\})\n", text, re.S).group(1))
    heavy = ast.literal_eval(re.search(r"HEAVY = (\[.*?\])\n", text).group(1))
    assert list(rc.SDK_JOBS) == list(linux) + list(heavy)
    assert len(rc.SDK_JOBS) == 16


def test_the_verdicts_the_verifier_downloads_are_uploaded_by_the_journeys():
    text = (WORKFLOWS / "docs-journeys.yml").read_text(encoding="utf-8")
    for artifact in (rc.JOURNEY_VERDICTS, rc.DEMO_VERDICTS):
        assert f"name: {artifact}\n" in text
    assert "DOCS_JOURNEY_PUBLISHED_SHA: ${{ steps.source.outputs.sha }}" in text
