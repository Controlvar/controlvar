"""A docs-only pull request reports every required check -- and still runs the docs tests.

Branch protection requires ``core-build``, ``full-build`` and
``SDK Live Contract / sdk-live-contract (core)``. All three used to be skipped
by a job-level ``if: needs.changes.outputs.docs_only != 'true'`` and, unlike a
plain job, neither skip reported under the required name: ``core-build`` and
``full-build`` were then one matrix job, which skipped before it expands
reports once as the literal ``${{ matrix.profile }}-build``, and a skipped
reusable-workflow caller reports only as ``SDK Live Contract``. #133
(``docs/auth/sso.md`` only) sat BLOCKED with every check that ran green.

So those jobs always run and every STEP is conditioned instead. Since #869
the matrix is two plain jobs, ``core-build`` and ``full-build``, with the
steps each leg had and the profile written out. These are YAML reads, not
greps, and they pin the exact condition on every step, so a new step that is
not classified here fails until it is:

* ``core-build``: every build step ``docs_only != 'true'``; on a docs-only
  change it checks out, sets up Python and runs the tests that pin ``docs/``
  content (``DOCS_TESTS``);
* ``full-build``: every step but the printed reason ``docs_only != 'true'``,
  and no docs tests;
* the two jobs share their runner, timeout, Postgres service, environment
  and every step they both have, as the matrix legs did by construction;
  core-build alone has the ``redis`` service its integration session needs
  (#899);
* ``_platform.yml``: a ``skip`` input, every step's condition starting with
  ``!inputs.skip`` inside ``${{ }}`` (a bare leading ``!`` is a YAML tag);
* the three sources of the required check names;
* ``git diff --no-renames`` in the ``changes`` job, without which
  ``git mv backend/a.py docs/a.py`` lists only ``docs/a.py``.

The classification itself is tested in
``backend/tests/unit/scripts/test_classify_changes.py``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
GATE = WORKFLOWS / "pr-qa-gate.yml"
PLATFORM = WORKFLOWS / "_platform.yml"

#: A distribution that does not ship the workflows has nothing here to check.
if not GATE.is_file() or not PLATFORM.is_file():
    pytest.skip("this tree has no .github/workflows", allow_module_level=True)

NOT_DOCS = "needs.changes.outputs.docs_only != 'true'"
DOCS = "needs.changes.outputs.docs_only == 'true'"

#: The two jobs that were the `profile-build` matrix (#869), by job id. The id
#: is the check name: neither has a `name:`.
BUILD_JOBS = ("core-build", "full-build")

#: Every step of core-build, by name (or `uses:` when unnamed), and its exact
#: `if:`. Insertion order is the step order. These are the matrix's core leg
#: with `matrix.profile == 'core'` evaluated: `X || true` is no condition,
#: `X && true` is X, and `X && false` (the module install) is no step.
CORE_BUILD_STEPS: Dict[str, Optional[str]] = {
    "Docs-only change": DOCS,
    "actions/checkout@v7": None,
    "Set up Python": None,
    "Install backend dependencies (docs tests)": DOCS,
    "Docs content tests": DOCS,
    "Set up Node.js": NOT_DOCS,
    "Install backend dependencies": NOT_DOCS,
    "Install gitleaks": NOT_DOCS,
    "Start a password-protected Redis": NOT_DOCS,
    "core build": NOT_DOCS,
}

#: The same for full-build, the matrix's full leg: `X || false` is X, so the
#: checkout and Python are skipped on a docs-only change, and the two docs
#: steps (`X && false`) are no steps.
FULL_BUILD_STEPS: Dict[str, Optional[str]] = {
    "Docs-only change": DOCS,
    "actions/checkout@v7": NOT_DOCS,
    "Set up Python": NOT_DOCS,
    "Set up Node.js": NOT_DOCS,
    "Install backend dependencies": NOT_DOCS,
    "Install module dependencies": NOT_DOCS,
    "Install gitleaks": NOT_DOCS,
    "full build": NOT_DOCS,
}

#: The build step's exact command in each job: the matrix's
#: `scripts/core_build.sh --profile ${{ matrix.profile }} ${{ matrix.steps }}`
#: with each row's values.
BUILD_COMMANDS = {
    "core-build": ("core build", "scripts/core_build.sh --profile core"),
    "full-build": (
        "full build",
        "scripts/core_build.sh --profile full "
        "--steps copy,strip,reuse,secrets,import,bootstrap,frontend,licences",
    ),
}

#: Every step of _platform.yml's job and its exact `if:`.
PLATFORM_STEPS: Dict[str, str] = {
    "Skipped": "${{ inputs.skip }}",
    "actions/checkout@v7": "${{ !inputs.skip }}",
    "Make the core profile real": "${{ !inputs.skip && inputs.profile == 'core' }}",
    "Set up Python": "${{ !inputs.skip }}",
    "Set up Node.js": (
        "${{ !inputs.skip && (contains(inputs.sdks, 'js') || "
        "contains(inputs.sdks, 'react') || contains(inputs.sdks, 'edge') || "
        "contains(inputs.sdks, 'openfeature')) }}"
    ),
    "Set up Go": "${{ !inputs.skip && contains(inputs.sdks, 'go') }}",
    "Set up Java": (
        "${{ !inputs.skip && (contains(inputs.sdks, 'java') || "
        "contains(inputs.sdks, 'android')) }}"
    ),
    "Set up Ruby": "${{ !inputs.skip && contains(inputs.sdks, 'ruby') }}",
    "Set up PHP": "${{ !inputs.skip && contains(inputs.sdks, 'php') }}",
    "Set up .NET": "${{ !inputs.skip && contains(inputs.sdks, 'dotnet') }}",
    "Install backend dependencies": "${{ !inputs.skip }}",
    "Install module dependencies": "${{ !inputs.skip && inputs.profile == 'full' }}",
    "Install SDK dependencies": (
        "${{ !inputs.skip && inputs.suite == 'sdk-live-contract' }}"
    ),
    "Install the fuzzer": "${{ !inputs.skip && inputs.suite == 'fuzz' }}",
    "Create the schema": "${{ !inputs.skip }}",
    "Apply seeds": "${{ !inputs.skip && inputs.seed != '' }}",
    "Start the API": "${{ !inputs.skip }}",
    "Assert the running profile": "${{ !inputs.skip }}",
    "Run suite": "${{ !inputs.skip }}",
    # Not on a fuzz run: its API log quotes the requests, and the log is public.
    "Backend log (on failure)": (
        "${{ !inputs.skip && failure() && inputs.suite != 'fuzz' }}"
    ),
}

#: The tests that read docs/ content and so must run on the pull request that
#: changes docs/ alone. On any other change unit-tests / smoke-tests run them.
DOCS_TESTS = (
    "backend/tests/unit/scripts/test_doc_examples.py",
    # docs/self-hosting/kubernetes.md against chart_kind.sh; stdlib only.
    "backend/tests/unit/scripts/test_guide_blocks.py",
    # Includes test_dashboard_and_api.py, which reads frontend/src as well as
    # docs/: a docs-only change to its table must still meet the dashboard.
    "backend/tests/unit/docs/",
    "backend/tests/smoke/test_openapi_snapshot.py",
    "backend/tests/smoke/test_version_sources.py",
    "backend/tests/unit/infrastructure/test_dashboard_image_docs.py",
    "backend/tests/unit/infrastructure/test_rollback_uses_codedeploy.py",
    # The deploy docs' anchors, runbook and checklist (B3a), and the generated
    # IAM table in docs/deployment/iam-permissions.md; yaml, stdlib, and
    # jmespath for the API count commands (#816; installed with boto3 from
    # backend/requirements.txt).
    "backend/tests/unit/infrastructure/test_deploy_docs.py",
    "backend/tests/unit/infrastructure/test_iam_actions.py",
    # Evaluates the rollback runbook's describe-task-definition --query with
    # jmespath (installed with boto3 from backend/requirements.txt) (#298).
    "backend/tests/unit/infrastructure/test_rollback_runbook_env_check.py",
    "backend/tests/unit/db/test_alembic_plan.py",
    # Reads docs/auth/sso.md and the SSO code through `ast`; imports no module
    # code, and passes with backend/requirements.txt alone (checked).
    "modules/backend/tests/unit/services/test_sso_docs_messages.py",
    # Video 2's SDK card quotes docs/sdk/javascript.md's quick start line for
    # line (#1066); passes with backend/requirements.txt alone (checked).
    "backend/tests/unit/showcase/test_showcase_quoted_docs.py",
)

#: Python tests that name docs/ but are deliberately NOT in DOCS_TESTS.
NOT_DOCS_TESTS = {
    # Reads only docs/api/stability.md, and docs/api/** is never docs-only
    # (scripts/classify_changes.py), so a change there runs unit-tests.
    "backend/tests/unit/scripts/test_makefile_guards.py": "docs/api only",
    # Reads only docs/api/openapi-v1.full.json (same reason as above).
    "backend/tests/unit/scripts/test_load_test_runner.py": "docs/api only",
    # Reads only docs/api/openapi-v1.stable.json (same reason as above).
    "backend/tests/unit/api/test_segment_preview_limits.py": "docs/api only",
    # Reads only docs/api/openapi-v1.stable.json (same reason as above).
    "backend/tests/unit/api/test_user_response_email_schema.py": "docs/api only",
    # Reads only docs/api/openapi-v1.stable.json and docs/api/stability.md
    # (same reason as above).
    "backend/tests/smoke/test_segment_routes_in_snapshot.py": "docs/api only",
    # Reads only the docs/api/ snapshots and frontend/src/tests/fixtures/openapi.json
    # (same reason as above).
    "backend/tests/smoke/test_audit_export_in_snapshot.py": "docs/api only",
    # Reads only docs/api/openapi-v1.stable.json (same reason as above).
    "backend/tests/smoke/test_assign_batch_route.py": "docs/api only",
    # Reads only docs/api/openapi-v1.full.json, to hold tests/fuzz/'s lists to
    # the full profile's operations (same reason as above).
    "backend/tests/unit/scripts/test_fuzz_check.py": "docs/api only",
    # Reads the `json` extra-files in release-please-config.json: the two
    # docs/api/ snapshots and frontend/src/tests/fixtures/openapi.json. None
    # of those, nor the config, is docs-only (same reason as above).
    "backend/tests/smoke/test_openapi_release_rewrite.py": "docs/api only",
    # Reads only docs/api/endpoints.md (same reason as above).
    "backend/tests/unit/api/test_wizard_docs_routes.py": "docs/api only",
    # Reads only docs/api/warehouse-analytics.md (same reason as above).
    "modules/backend/tests/unit/test_warehouse_docs_availability.py": "docs/api only",
    # Runs the runbook SQL of docs/self-hosting/migrations.md against
    # PostgreSQL, which the docs-only lane does not have. It reads the page
    # through backend/tests/unit/docs/test_email_case_runbook.py, which is in
    # DOCS_TESTS and pins the anchor and the SQL steps on a docs-only change.
    "backend/tests/integration/database/test_users_email_lower_migration.py": (
        "needs PostgreSQL; its docs reader is in DOCS_TESTS"
    ),
    # Hands the classifier path STRINGS such as "docs/a.py"; opens no file.
    "backend/tests/unit/scripts/test_classify_changes.py": "fixture strings only",
    # Pins docs-journeys.yml's script lines, one of which names mkdocs.yml and
    # docs (the git archive of the published site's source); opens no file
    # under docs/.
    "backend/tests/unit/scripts/test_docs_journeys_workflow.py": (
        "fixture strings only"
    ),
    # Hands stub `git` and `python3` path STRINGS such as "docs/a.md" to the
    # classifier step's script; opens no file under docs/.
    "backend/tests/unit/infrastructure/test_ci_classification.py": (
        "fixture strings only"
    ),
    # Assert that the scripts' and workflow's printed copy CONTAINS a
    # docs/...#anchor string; they open no file under docs/. Whether the
    # anchors exist is test_deploy_docs.py's, which is in DOCS_TESTS.
    "backend/tests/unit/infrastructure/test_shift_traffic_alarm.py": (
        "printed link strings only"
    ),
    "backend/tests/unit/infrastructure/test_deploy_alarm_wiring.py": (
        "printed link strings only"
    ),
    "backend/tests/unit/infrastructure/test_refuse_alarm_active.py": (
        "printed link strings only"
    ),
    "backend/tests/unit/infrastructure/test_refuse_migrating_api_revision.py": (
        "printed link strings only"
    ),
    "backend/tests/unit/infrastructure/test_deploy_secrets_preflight.py": (
        "printed link strings only"
    ),
    "backend/tests/unit/infrastructure/test_rollback_false_success.py": (
        "printed link strings only"
    ),
    "backend/tests/unit/infrastructure/test_rollback_verdict.py": (
        "printed link strings only"
    ),
}
# docs-links.test.ts (Jest) runs on a docs-only change as its pinned Python
# twin, backend/tests/unit/docs/test_dashboard_docs_links.py.
# The real-tree scan in test_leak_guard.py reads docs/ through `git ls-files`;
# the required Leak Guard workflow runs the same guard on every pull request.

#: A test that reads docs/: the directory as a quoted path segment, a quoted
#: path under it, or the site config. Catches `REPO_ROOT / "docs" / ...` and
#: `Path("docs")`, not only the literal "docs/...".
DOCS_READER = re.compile(r"""["']docs["']|["']docs/|mkdocs\.yml""")


class _NoDuplicateKeys(yaml.SafeLoader):
    """safe_load keeps the LAST of two `if:` keys silently; this refuses."""

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise AssertionError(
                    f"duplicate key {key!r} at line {key_node.start_mark.line + 1}"
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _load(path: Path) -> Dict[str, Any]:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_NoDuplicateKeys)


def _key(step: Dict[str, Any]) -> str:
    return step.get("name") or step["uses"]


def _conditions(steps: List[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    keys = [_key(s) for s in steps]
    assert len(keys) == len(set(keys)), f"two steps share a name: {keys}"
    return {_key(s): s.get("if") for s in steps}


@pytest.fixture(scope="module")
def gate() -> Dict[str, Any]:
    return _load(GATE)


@pytest.fixture(scope="module")
def platform() -> Dict[str, Any]:
    return _load(PLATFORM)


# ---------------------------------------------------------------------------
# The classification input
# ---------------------------------------------------------------------------
class TestTheChangesJob:
    def _run(self, gate) -> str:
        (step,) = [
            s for s in gate["jobs"]["changes"]["steps"] if s.get("id") == "check"
        ]
        return step["run"]

    def test_the_diff_does_not_detect_renames(self, gate):
        """Without it, `git mv backend/a.py docs/a.py` lists only docs/a.py."""
        diffs = [line for line in self._run(gate).splitlines() if "git diff" in line]
        assert diffs, "the changes job no longer runs git diff"
        for line in diffs:
            assert "git diff --no-renames --name-only" in line, line

    def test_the_diff_feeds_the_classifier_into_github_output(self, gate):
        """Collected first, written last; the whole script is pinned and run
        by test_ci_classification.py."""
        run = self._run(gate)
        assert run.startswith("set -euo pipefail\n"), "a failing git diff must fail"
        assert 'files="$(git diff --no-renames --name-only "$BASE" "$HEAD")"\n' in run
        assert "| python3 scripts/classify_changes.py)" in run
        assert run.endswith('printf \'%s\\n\' "$lane" >> "$GITHUB_OUTPUT"\n')
        assert run.count("GITHUB_OUTPUT") == 1, "one write, at the end"

    def test_the_output_is_wired(self, gate):
        outputs = gate["jobs"]["changes"]["outputs"]
        assert outputs == {"docs_only": "${{ steps.check.outputs.docs_only }}"}


# ---------------------------------------------------------------------------
# No job-level skip on a job whose check name protection cannot see skipped
# ---------------------------------------------------------------------------
#: The only job-level `if:` a matrix or reusable job may have: run unless the
#: run was cancelled, including after a failed classifier.
NEVER_SKIPS = "${{ !cancelled() }}"


#: The jobs that always run and condition their steps instead.
NEVER_SKIP_JOBS = ("core-build", "full-build", "sdk-live-contract")


class TestNoJobLevelSkip:
    @pytest.mark.parametrize("job", NEVER_SKIP_JOBS)
    def test_the_jobs_have_no_job_level_if(self, gate, job):
        assert gate["jobs"][job].get("if") == NEVER_SKIPS, (
            f"{job} has a job-level if: other than {NEVER_SKIPS}. Skipped, it "
            "skips the docs tests or never reports its required check name; "
            "without it, a failed classifier skips it."
        )

    def test_no_matrix_or_reusable_job_skips_on_docs_only(self, gate):
        """The same trap for any job added later in either shape, and for
        the two build jobs, which are no longer a matrix."""
        checked = set()
        for name, job in gate["jobs"].items():
            if "strategy" in job or "uses" in job or name in NEVER_SKIP_JOBS:
                assert job.get("if") == NEVER_SKIPS, name
                checked.add(name)
        assert checked >= set(NEVER_SKIP_JOBS), sorted(checked)


# ---------------------------------------------------------------------------
# The three sources of the required check names
# ---------------------------------------------------------------------------
class TestTheRequiredCheckNames:
    @pytest.mark.parametrize("job_id", BUILD_JOBS)
    def test_the_build_jobs_report_under_their_ids(self, gate, job_id):
        """No `name:` and no matrix: the check name is the job id, exactly
        `core-build` / `full-build`. A `name:` or a matrix would change it."""
        job = gate["jobs"][job_id]
        assert "name" not in job, f"{job_id}: a name: replaces the required name"
        assert "strategy" not in job, f"{job_id}: a matrix appends its values"
        assert "profile-build" not in gate["jobs"]

    def test_the_caller_is_named_sdk_live_contract(self, gate):
        job = gate["jobs"]["sdk-live-contract"]
        assert job["name"] == "SDK Live Contract"
        assert job["uses"] == "./.github/workflows/_platform.yml"
        assert job["with"]["profile"] == "core"
        assert job["with"]["suite"] == "sdk-live-contract"

    def test_the_callee_job_is_named_suite_and_profile(self, platform):
        jobs = platform["jobs"]
        assert list(jobs) == ["platform"]
        assert jobs["platform"]["name"] == "${{ inputs.suite }} (${{ inputs.profile }})"
        assert "if" not in jobs["platform"]


# ---------------------------------------------------------------------------
# core-build and full-build: every step conditioned, docs tests in core-build
# ---------------------------------------------------------------------------
class TestBuildJobSteps:
    def test_core_build_steps_have_exactly_their_conditions(self, gate):
        actual = _conditions(gate["jobs"]["core-build"]["steps"])
        assert list(actual.items()) == list(CORE_BUILD_STEPS.items())

    def test_full_build_steps_have_exactly_their_conditions(self, gate):
        actual = _conditions(gate["jobs"]["full-build"]["steps"])
        assert list(actual.items()) == list(FULL_BUILD_STEPS.items())

    @pytest.mark.parametrize("job_id", BUILD_JOBS)
    def test_the_build_itself_is_the_core_build_script(self, gate, job_id):
        steps = {_key(s): s for s in gate["jobs"][job_id]["steps"]}
        name, command = BUILD_COMMANDS[job_id]
        assert steps[name]["run"].strip() == command

    @pytest.mark.parametrize("job_id", BUILD_JOBS)
    def test_the_skip_reason_is_printed(self, gate, job_id):
        first = gate["jobs"][job_id]["steps"][0]
        assert "docs-only change" in first["run"]

    def test_the_two_jobs_run_in_the_same_environment(self, gate):
        """The matrix gave both legs one runner, timeout, service set and
        environment by construction; two jobs have to be held to it."""
        core, full = (gate["jobs"][j] for j in BUILD_JOBS)
        # Every job-level key is listed, so a new one (a `name:`, a
        # `concurrency:`, a `strategy:`) is a decision, not a drift.
        keys = {"needs", "if", "runs-on", "timeout-minutes", "services", "env"}
        assert set(core) == set(full) == keys | {"steps"}, (
            sorted(core),
            sorted(full),
        )
        for key in sorted(keys - {"services"}):
            assert core[key] == full[key], f"{key} differs between the jobs"
        # The one difference: core-build's integration session needs Redis
        # (#899), and full-build runs no pytest session.
        core_services = dict(core["services"])
        assert sorted(core_services) == ["postgres", "redis"], sorted(core_services)
        core_services.pop("redis")
        assert core_services == full["services"], "the Postgres services differ"
        assert core["timeout-minutes"] == 60
        assert core["needs"] == "changes"

    def test_the_steps_both_jobs_have_are_the_same_step(self, gate):
        """Apart from its `if:` and the printed reason, a step in both jobs is
        one step written twice; a pin or a version moved in one is a diff."""
        core, full = (
            {_key(s): s for s in gate["jobs"][j]["steps"]} for j in BUILD_JOBS
        )
        shared = (set(core) & set(full)) - {"Docs-only change"}
        assert len(shared) == 5, sorted(shared)
        for key in shared:
            a = {k: v for k, v in core[key].items() if k != "if"}
            b = {k: v for k, v in full[key].items() if k != "if"}
            assert a == b, key

    def test_the_docs_tests_are_exactly_the_list(self, gate):
        steps = {_key(s): s for s in gate["jobs"]["core-build"]["steps"]}
        words = steps["Docs content tests"]["run"].split()
        assert words[:3] == ["python", "-m", "pytest"]
        # Every path argument (option values like `no:cov` have no slash), not
        # only backend/ ones: a modules/ test dropped from the step fails here.
        assert tuple(w for w in words[3:] if "/" in w) == DOCS_TESTS
        install = steps["Install backend dependencies (docs tests)"]["run"]
        assert install.strip() == "pip install -r backend/requirements.txt"

    @pytest.mark.parametrize("path", DOCS_TESTS)
    def test_every_docs_test_exists(self, path):
        # The step runs in the full checkout, before core-build copies the
        # tree without modules/; in that core copy a modules/ path is absent
        # by design, not gone.
        if path.startswith("modules/") and not (REPO_ROOT / "modules").exists():
            pytest.skip("core tree: modules/ is deleted by design")
        assert (REPO_ROOT / path).exists(), f"{path} is gone: the step would error"

    def test_every_python_docs_reader_is_classified(self):
        """A new test that reads docs/ is either in DOCS_TESTS or excused.

        Found by the path shape, including paths composed from a root
        constant (`REPO_ROOT / "docs" / ...`), not by the literal string.
        """
        roots = [
            REPO_ROOT / "backend" / "tests",
            REPO_ROOT / "modules" / "backend" / "tests",
        ]
        readers = set()
        for root in roots:
            if not root.is_dir():
                continue
            for path in root.rglob("*.py"):
                if DOCS_READER.search(
                    path.read_text(encoding="utf-8", errors="replace")
                ):
                    readers.add(path.relative_to(REPO_ROOT).as_posix())
        assert readers, "the scan found nothing: it is broken, not clean"

        def covered(rel: str) -> bool:
            if rel == Path(__file__).relative_to(REPO_ROOT).as_posix():
                return True
            if rel in NOT_DOCS_TESTS:
                return True
            return any(
                rel == t or (t.endswith("/") and rel.startswith(t)) for t in DOCS_TESTS
            )

        unclassified = sorted(r for r in readers if not covered(r))
        assert not unclassified, (
            "these tests read docs/ but a docs-only pull request would not run "
            f"them: add them to DOCS_TESTS and the workflow step, or to "
            f"NOT_DOCS_TESTS with the reason: {unclassified}"
        )


def test_the_dashboard_and_api_gate_runs_on_a_docs_only_change():
    """docs/guides/dashboard-and-api.md is held to frontend/src by this test;
    a pull request that edits only the table must run it (#442)."""
    rel = "backend/tests/unit/docs/test_dashboard_and_api.py"
    assert (REPO_ROOT / rel).is_file(), f"{rel} is gone"
    assert rel not in NOT_DOCS_TESTS, f"{rel} must run on a docs-only change"
    assert any(
        rel == t or (t.endswith("/") and rel.startswith(t)) for t in DOCS_TESTS
    ), f"{rel} is not in DOCS_TESTS, by path or by directory"


# ---------------------------------------------------------------------------
# _platform.yml: the skip input
# ---------------------------------------------------------------------------
class TestPlatformSkip:
    def test_skip_is_a_boolean_input_defaulting_to_false(self, platform):
        # PyYAML reads the `on:` key as True.
        inputs = platform[True]["workflow_call"]["inputs"]
        assert inputs["skip"]["type"] == "boolean"
        assert inputs["skip"]["default"] is False

    def test_the_caller_passes_docs_only_as_skip(self, gate):
        assert gate["jobs"]["sdk-live-contract"]["with"]["skip"] == (
            "${{ needs.changes.outputs.docs_only == 'true' }}"
        )

    def test_every_step_has_exactly_its_condition(self, platform):
        actual = _conditions(platform["jobs"]["platform"]["steps"])
        assert list(actual.items()) == list(PLATFORM_STEPS.items())

    def test_every_work_step_starts_with_not_skip(self, platform):
        """Belt and braces over the exact table: the shape of each condition."""
        steps = platform["jobs"]["platform"]["steps"]
        assert steps[0]["if"] == "${{ inputs.skip }}"
        for step in steps[1:]:
            cond = step.get("if", "")
            assert cond.startswith("${{ !inputs.skip") and cond.endswith("}}"), _key(
                step
            )
            assert cond.count("${{") == 1, f"{_key(step)}: nested expression {cond}"
