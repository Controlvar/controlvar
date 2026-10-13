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
* the python-suites matrix is ``SUITES``' pytest ones (sdk/python's own suite
  installs pytest only, as sdk-unit-tests.yml does) and the jest-suites matrix
  its jest ones, each in its npm project (their reports written as JUnit);
  each runs ``--suite`` and uploads its reports as the artifact the verifier
  downloads, for 90 days; the verify job runs after every leg (``always()``),
  reads them, never ``--staging`` or ``--registry``, and keeps its record for
  90 days;
* every demo app's jest and simulator tests, and both published SDKs' trees,
  are run by a suite;
* no script holds an expression: inputs reach the shell through ``env``;
* every name the verifier matches on another workflow is a name there: the
  required and allowed-to-skip jobs, the 16 SDKs, the verdicts artifacts, and
  sdk-release.yml's jobs; and what sdk-release.yml's build jobs print as their
  record is what the verifier reads back from their logs.

Each permission rule is also planted against the real workflow below, so a rule
that stops firing fails here.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import re
import subprocess
import sys
import textwrap
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
    "jest-suites": {"contents": "read"},
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
        lambda d: d["jobs"]["jest-suites"]["permissions"].update({"actions": "write"}),
        lambda d: d["jobs"]["verify"].pop("permissions"),
        lambda d: d["jobs"].update({"extra": {"permissions": {"actions": "write"}}}),
    ],
    ids=[
        "actions-write-on-verify",
        "actions-write-on-suites",
        "actions-write-at-the-top",
        "write-all",
        "actions-read-on-an-installing-job",
        "actions-write-on-the-jest-job",
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
    assert "if" not in doc["jobs"]["jest-suites"]
    assert doc["jobs"]["verify"]["if"] == "always()"


def test_the_matrix_is_the_scripts_suites(doc):
    job = doc["jobs"]["python-suites"]
    assert job["name"] == rc.SUITE_JOB.format("${{ matrix.suite }}")
    include = job["strategy"]["matrix"]["include"]
    assert [leg["suite"] for leg in include] == [
        s for s in rc.SUITES if not rc.is_jest(s)
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


def test_the_sdks_own_suite_installs_pytest_only_as_sdk_unit_tests_does(doc):
    steps = doc["jobs"]["python-suites"]["steps"]
    backend = next(s for s in steps if s.get("name") == "Install dependencies")
    assert backend["if"] == "matrix.suite != 'sdk-python'"
    sdk = next(
        s for s in steps if s.get("name") == "Install pytest for the SDK's own suite"
    )
    assert sdk["if"] == "matrix.suite == 'sdk-python'"
    assert sdk["run"].strip() == "python -m pip install pytest"
    text = (WORKFLOWS / "sdk-unit-tests.yml").read_text(encoding="utf-8")
    assert "pip install pytest\n" in text
    assert 'python -m pytest sdk/python/tests -q -o addopts=""' in text
    (session,) = rc.SUITES["sdk-python"]
    assert session.args[0] == "sdk/python/tests" and "--rootdir=." in session.args


def test_the_jest_matrix_runs_each_jest_suite_in_its_project(doc):
    job = doc["jobs"]["jest-suites"]
    assert job["name"] == rc.JEST_SUITE_JOB.format("${{ matrix.suite }}")
    include = job["strategy"]["matrix"]["include"]
    assert job["strategy"]["fail-fast"] is False
    assert [leg["suite"] for leg in include] == [s for s in rc.SUITES if rc.is_jest(s)]
    for leg in include:
        (session,) = rc.SUITES[leg["suite"]]
        assert session.runner == "jest" and leg["dir"] == session.cwd
        assert (REPO_ROOT / session.cwd / "package-lock.json").is_file()
    assert rc.suite_job("frontend") == "jest suites (frontend)"
    steps = job["steps"]
    install = next(s for s in steps if s.get("name") == "Install the suite's packages")
    assert install["env"] == {"DIR": "${{ matrix.dir }}"}
    assert install["run"].strip() == 'cd "$DIR" && npm ci'
    node = next(
        s for s in steps if str(s.get("uses", "")).startswith("actions/setup-node")
    )
    assert node["with"] == {"node-version-file": ".nvmrc"}
    run = next(s for s in steps if s.get("name") == "Run the suite")
    assert run["run"].strip() == SUITE_RUN
    assert run["env"] == {"SUITE": "${{ matrix.suite }}"}
    upload = next(
        s for s in steps if s.get("uses", "").startswith("actions/upload-artifact")
    )
    assert upload["with"]["name"] == rc.JUNIT_ARTIFACT.format("${{ matrix.suite }}")
    assert upload["with"]["retention-days"] == 90
    assert upload["if"] == "always()"


def _runs_tree(tree: str) -> bool:
    """Whether a suite runs the tests under *tree*: a pytest session given it
    (or a directory above it), or jest in an npm project at or above it."""
    for invocations in rc.SUITES.values():
        for session in invocations:
            if session.runner == "jest":
                roots = [session.cwd]
            else:
                roots = [a for a in session.args if not a.startswith("-")]
            if any(tree == r or tree.startswith(r.rstrip("/") + "/") for r in roots):
                return True
    return False


def test_every_demo_apps_tests_and_both_published_sdks_are_run_by_a_suite():
    demo_trees = sorted(
        {
            p.parent.relative_to(REPO_ROOT).as_posix()
            for p in REPO_ROOT.glob("demo/*/simulator/test_*.py")
        }
        | {
            p.relative_to(REPO_ROOT).as_posix()
            for p in REPO_ROOT.glob("demo/*/src/__tests__")
        }
    )
    assert len(demo_trees) == 4, demo_trees  # two apps, each jest and simulator
    sdk_trees = ["sdk/python/tests", "sdk/js/__tests__"]
    for tree in demo_trees + sdk_trees:
        assert _runs_tree(tree), f"no suite in SUITES runs {tree}"
    assert not _runs_tree("sdk/go"), "the check finds a tree no suite runs"
    for package in rc.PACKAGES.values():
        assert package.suite in rc.SUITES
        assert (REPO_ROOT / package.manifest).is_file()


def test_the_verify_job_reads_every_leg_and_keeps_its_record(doc):
    job = doc["jobs"]["verify"]
    assert job["name"] == rc.VERIFY_JOB
    assert job["needs"] == ["python-suites", "jest-suites"]
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
    assert "--registry" not in text
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
            present |= {rc.suite_job(s) for s in rc.SUITES}
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


def test_the_published_sdks_are_the_ones_the_docs_call_published():
    from backend.tests.unit.docs.test_sdk_doc_versions import PUBLISHED

    assert set(rc.PACKAGES) == set(PUBLISHED)
    assert list(rc.PACKAGES) == ["js", "python"], "npm first, as the sitting runs"


def test_the_sdk_release_jobs_the_verifier_reads_are_there():
    names = set(_job_names(rc.SDK_RELEASE))
    for package in rc.PACKAGES.values():
        assert {rc.RESOLVE_JOB, package.build_job, package.publish_job} <= names
    on = _on(_load(WORKFLOWS / rc.SDK_RELEASE))
    assert on["push"]["tags"] == ["sdk/*/v*"] and "workflow_dispatch" in on


def _record_step(job: str, step: str) -> str:
    """The Python an sdk-release.yml build job runs as its record step."""
    found = next(
        s
        for s in _load(WORKFLOWS / rc.SDK_RELEASE)["jobs"][job]["steps"]
        if s.get("name") == step
    )
    body = found["run"].split("<<'PY'\n", 1)[1].rsplit("PY", 1)[0]
    return textwrap.dedent(body)


def _as_log(stdout: str) -> str:
    """Printed lines as a job's log holds them: a timestamp before each."""
    lines = ["Current runner version: '2.0'"] + stdout.splitlines()
    return "\ufeff" + "".join(
        f"2026-11-01T10:{n // 60:02d}:{n % 60:02d}.1234567Z {line}\n"
        for n, line in zip(range(len(lines)), lines)
    )


def test_what_the_pypi_build_job_records_is_what_the_verifier_reads(tmp_path):
    """Runs pypi-build's own record step over made-up files and reads its
    output back as a log: a change to what it prints fails here."""
    dist = tmp_path / "dist"
    dist.mkdir()
    files = {
        "experimently-9.9.9-py3-none-any.whl": b"made-up wheel",
        "experimently-9.9.9.tar.gz": b"made-up sdist",
    }
    for name, data in files.items():
        (dist / name).write_bytes(data)
    out = tmp_path / "github_output"
    done = subprocess.run(
        [sys.executable, "-c", _record_step("pypi-build", "Record the distributions")],
        cwd=tmp_path,
        env={"GITHUB_OUTPUT": str(out), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    recorded, problems = rc.read_record(rc.PACKAGES["python"], _as_log(done.stdout))
    assert problems == []
    assert recorded == {n: hashlib.sha256(d).hexdigest() for n, d in files.items()}
    assert out.read_text().startswith("digest=")


def test_what_the_npm_build_job_records_is_what_the_verifier_reads(tmp_path):
    tgz = tmp_path / "getexperimently-js-sdk-9.9.9.tgz"
    tgz.write_bytes(b"made-up tarball")
    out = tmp_path / "github_output"
    done = subprocess.run(
        [sys.executable, "-c", _record_step("npm-build", "Record the tarball")],
        cwd=tmp_path,
        env={"GITHUB_OUTPUT": str(out), "TGZ": str(tgz), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    recorded, problems = rc.read_record(rc.PACKAGES["js"], _as_log(done.stdout))
    assert problems == []
    assert recorded == {tgz.name: hashlib.sha256(b"made-up tarball").hexdigest()}
