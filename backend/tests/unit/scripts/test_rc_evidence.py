"""``scripts/rc_evidence.py`` reads a release candidate's runs back and says
PASS only when every one of them is there and passed (#1142).

Every GitHub answer here is a fixture, made up: a fake ``gh`` that answers the
GETs the verifier makes from a dict, and writes artifact files for ``gh run
download``. A good world passes; each FAIL path changes one thing in it and
the verdict must turn to FAIL with the problem named. Nothing here reaches
GitHub, and no test asserts a time.
"""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import pathlib
import sys
import tomllib
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "rc_evidence.py"


def _load():
    spec = importlib.util.spec_from_file_location("rc_evidence_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rc = _load()

TAG = "v0.30.0"
RC = "a1" * 20
OLD = "b2" * 20
COMMIT_TIME = "2026-11-01T10:00:00Z"
LATER = "2026-11-01T11:00:00Z"
TARGET_FN = "test:backend/tests/unit/x/test_alpha.py::test_one"
TARGET_PARAM = "test:backend/tests/unit/x/test_alpha.py::test_many"
TARGET_METHOD = "test:backend/tests/unit/x/test_alpha.py::TestK::test_two"
TARGET_FILE = "test:backend/tests/unit/x/test_beta.py"
JEST_FN = "test:frontend/src/tests/alpha.test.tsx::renders the card"
JEST_FILE = "test:frontend/src/tests/beta.test.tsx"
TAG_OBJECT = "c3" * 20
JOURNEYS = ("alpha-guide", "beta-guide")
DEMO_JOURNEYS = ("demo-tour",)


def _run(
    run_id: int,
    workflow: str,
    *,
    event: str = "workflow_dispatch",
    ref: str = TAG,
    sha: str = RC,
    status: str = "completed",
    conclusion: str = "success",
    attempt: int = 1,
    created: str = LATER,
    updated: Optional[str] = None,
    title: str = "",
) -> Dict[str, Any]:
    return {
        "id": run_id,
        "event": event,
        "head_branch": ref,
        "head_sha": sha,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "run_attempt": attempt,
        "created_at": created,
        "updated_at": updated or created,
        "html_url": f"https://github.com/getexperimently/experimently/actions/runs/{run_id}",
        "display_title": title or workflow,
    }


def _job(name: str, conclusion: str = "success", status: str = "completed"):
    return {"name": name, "status": status, "conclusion": conclusion, "html_url": ""}


def _junit(cases: List[Tuple[str, str, str]]) -> str:
    body = []
    for classname, name, outcome in cases:
        inner = {
            "passed": "",
            "failed": '<failure message="assert 1 == 2">boom</failure>',
            "skipped": '<skipped type="pytest.skip" message="no reason"/>',
        }[outcome]
        body.append(
            f'<testcase classname="{classname}" name="{name}">{inner}</testcase>'
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">'
        + "".join(body)
        + "</testsuite></testsuites>"
    )


JEST_CASES = [
    ("frontend/src/tests/alpha.test.tsx", "Alpha renders the card", "passed"),
    ("frontend/src/tests/alpha.test.tsx", "Alpha renders the cards list", "passed"),
    ("frontend/src/tests/beta.test.tsx", "Beta loads", "passed"),
]
GOOD_CASES = [
    ("backend.tests.unit.x.test_alpha", "test_one", "passed"),
    ("backend.tests.unit.x.test_alpha", "test_many[a]", "passed"),
    ("backend.tests.unit.x.test_alpha", "test_many[b]", "passed"),
    ("backend.tests.unit.x.test_alpha.TestK", "test_two", "passed"),
    ("backend.tests.unit.x.test_beta", "test_first", "passed"),
    ("backend.tests.unit.x.test_beta.TestB", "test_second", "passed"),
]


class World:
    """A made-up release: every run the verifier reads, at the tag, green."""

    def __init__(self, tmp: pathlib.Path):
        self.tmp = tmp
        self.api: Dict[str, Any] = {}
        self.artifacts: Dict[Tuple[int, str], Dict[str, str]] = {}
        self.calls: List[List[str]] = []
        self.ids: Dict[str, int] = {}
        self.api[f"git/ref/tags/{TAG}"] = {"object": {"type": "commit", "sha": RC}}
        self.api[f"commits/{RC}"] = {"commit": {"committer": {"date": COMMIT_TIME}}}
        self.api[f"git/matching-refs/heads/{TAG}"] = []
        self.runs_db: Dict[str, List[Dict[str, Any]]] = {}
        for number, workflow in zip(range(len(rc.WORKFLOWS)), rc.WORKFLOWS):
            run_id = 1000 + number
            self.ids[workflow.file] = run_id
            event = workflow.event
            ref = TAG if workflow.at_tag else "main"
            self.set_runs(
                workflow.file, [_run(run_id, workflow.file, event=event, ref=ref)]
            )
            names = list(rc.REQUIRED_JOBS.get(workflow.file, ("build", "test")))
            jobs = [_job(n) for n in names]
            jobs += [
                _job(n, "skipped") for (w, n) in rc.ALLOWED_SKIPS if w == workflow.file
            ]
            self.set_jobs(run_id, jobs)
        journeys = self.ids["docs-journeys.yml"]
        self.verdicts = {
            rc.JOURNEY_VERDICTS: {
                "date": "2026-11-01",
                "sha": RC,
                "run_link": "",
                "published": {"ref": TAG, "sha": RC},
                "journeys": {j: {"word": "PASS", "step": 0} for j in JOURNEYS},
            },
            rc.DEMO_VERDICTS: {
                "date": "2026-11-01",
                "sha": RC,
                "run_link": "",
                "published": {"ref": "", "sha": ""},
                "journeys": {j: {"word": "PASS", "step": 0} for j in DEMO_JOURNEYS},
            },
        }
        self.cases = list(GOOD_CASES)
        self.jest_cases = list(JEST_CASES)
        self.suite_reports: Dict[str, Optional[str]] = {}
        self.root = tmp / "tree"
        (self.root / rc.JOURNEYS).mkdir(parents=True)
        for name in JOURNEYS + DEMO_JOURNEYS:
            (self.root / rc.JOURNEYS / f"{name}.yaml").write_text("guide: x\n")
        self.registry = rc.Registry(
            [],
            pending=0,
            deferred=24,
            untested=0,
            targets=[
                (TARGET_FN, "docs/a.md"),
                (TARGET_PARAM, "docs/a.md"),
                (TARGET_METHOD, "docs/a.md"),
                (TARGET_FILE, "docs/b.md"),
                (JEST_FN, "docs/c.md"),
                (JEST_FILE, "docs/c.md"),
            ],
            report=["documents in scope: 2"],
        )
        self.head = RC
        self.staging: Optional[pathlib.Path] = None

    # -- editing the world ------------------------------------------------------
    def set_runs(self, workflow: str, runs: List[Dict[str, Any]]) -> None:
        self.runs_db[workflow] = runs

    def runs(self, workflow: str) -> List[Dict[str, Any]]:
        return self.runs_db[workflow]

    def add_run(self, workflow: str, run: Dict[str, Any]) -> None:
        self.runs_db.setdefault(workflow, []).append(run)

    def list_runs(self, path: str) -> Dict[str, Any]:
        """A workflow's runs as GitHub lists them: filtered by head_sha, event
        and created (>=), newest first, per_page to a page. A run marked
        "stray" is listed whatever head_sha asks: what the check of each run's
        commit is there for."""
        workflow = path.split("/")[2]
        query = urllib.parse.parse_qs(path.split("?", 1)[1])
        found = list(self.runs_db.get(workflow, []))
        if "head_sha" in query:
            found = [
                r
                for r in found
                if r["head_sha"] == query["head_sha"][0] or r.get("stray")
            ]
        if "event" in query:
            found = [r for r in found if r["event"] == query["event"][0]]
        if "created" in query:
            assert query["created"][0].startswith(">=")
            moment = rc._time(query["created"][0][2:])
            found = [r for r in found if rc._time(r["created_at"]) >= moment]
        found.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
        size = int(query["per_page"][0])
        page = int(query.get("page", ["1"])[0])
        return {
            "total_count": len(found),
            "workflow_runs": found[(page - 1) * size : page * size],
        }

    def set_jobs(self, run_id: int, jobs: List[Dict[str, Any]]) -> None:
        self.api[f"actions/runs/{run_id}/jobs?filter=latest&per_page=100"] = {
            "total_count": len(jobs),
            "jobs": jobs,
        }

    def jobs(self, workflow: str) -> List[Dict[str, Any]]:
        return self.api[
            f"actions/runs/{self.ids[workflow]}/jobs?filter=latest&per_page=100"
        ]["jobs"]

    def counted(self, workflow: str) -> Dict[str, Any]:
        return next(r for r in self.runs(workflow) if r["id"] == self.ids[workflow])

    def with_staging(self) -> pathlib.Path:
        """A green staging deploy of the tag and its private checks."""
        deploy = _run(
            7000,
            "deploy.yml",
            ref="main",
            sha=OLD,
            created="2026-11-02T09:00:00Z",
            updated="2026-11-02T09:30:00Z",
            title=f"Deploy {TAG} (full) to staging",
        )
        older = _run(
            6999,
            "deploy.yml",
            ref="main",
            sha=OLD,
            created="2026-10-20T09:00:00Z",
            title="Deploy v0.29.0 (full) to staging",
        )
        self.set_runs("deploy.yml", [deploy, older])
        synthetic = [
            _run(
                7100 + n,
                "synthetic.yml",
                event="schedule",
                ref="main",
                sha=OLD,
                created=f"2026-11-02T1{n}:17:00Z",
            )
            for n in range(3)
        ]
        synthetic.append(
            _run(
                7090,
                "synthetic.yml",
                event="schedule",
                ref="main",
                sha=OLD,
                conclusion="failure",
                created="2026-11-02T08:47:00Z",
            )
        )
        self.set_runs("synthetic.yml", synthetic)
        staging = self.tmp / "staging-private"
        staging.mkdir()
        (staging / "live-target-group.txt").write_text(
            "ok: green is live; deploy with -c api_live_target_group=green\n"
        )
        (staging / "live-image-version.txt").write_text("0.30.0\n")
        (staging / "read-only-checks.md").write_text("- the listed read-only checks\n")
        self.staging = staging
        return staging

    # -- the fake gh -------------------------------------------------------------
    def gh(self, args: List[str]) -> str:
        self.calls.append(list(args))
        if args[0] == "api":
            assert len(args) == 2, f"only a plain GET is made: {args}"
            prefix = f"repos/{rc.REPO}/"
            assert args[1].startswith(prefix)
            path = args[1][len(prefix) :]
            if path.startswith("actions/workflows/") and "/runs?" in path:
                return json.dumps(self.list_runs(path))
            if path not in self.api:
                raise rc.GhError(f"gh api {path} failed (exit 1)")
            return json.dumps(self.api[path])
        if args[:2] == ["run", "download"]:
            run_id = int(args[2])
            name = args[args.index("-n") + 1]
            dest = pathlib.Path(args[args.index("-D") + 1])
            files = self.artifact(run_id, name)
            if files is None:
                raise rc.GhError("gh run download failed (exit 1)")
            for file_name, text in files.items():
                (dest / file_name).write_text(text)
            return ""
        raise AssertionError(f"not a read: gh {args}")

    def artifact(self, run_id: int, name: str) -> Optional[Dict[str, str]]:
        if (run_id, name) in self.artifacts:
            return self.artifacts[(run_id, name)]
        if run_id == self.ids["docs-journeys.yml"] and name in self.verdicts:
            data = self.verdicts[name]
            return None if data is None else {"verdicts.json": json.dumps(data)}
        if run_id == self.ids["rc-evidence.yml"]:
            for suite in rc.SUITES:
                if name == rc.JUNIT_ARTIFACT.format(suite):
                    return self.suite_files(suite)
        return None

    def suite_files(self, suite: str) -> Dict[str, str]:
        files = {}
        for number, invocation in zip(range(len(rc.SUITES[suite])), rc.SUITES[suite]):
            if invocation.report in self.suite_reports:
                text = self.suite_reports[invocation.report]
                if text is not None:
                    files[invocation.report] = text
                continue
            if suite == rc.FRONTEND:
                cases = self.jest_cases
            elif suite == "unit" and number == 0:
                cases = self.cases
            else:
                cases = [(f"backend.tests.{suite}.test_{number}", "test_ok", "passed")]
            files[invocation.report] = _junit(cases)
        return files

    def junit_dir(self) -> pathlib.Path:
        """The reports as rc-evidence.yml's verify job has them, merged."""
        directory = self.tmp / "junit-here"
        directory.mkdir(exist_ok=True)
        for suite in rc.SUITES:
            for name, text in self.suite_files(suite).items():
                (directory / name).write_text(text)
        return directory

    # -- running the verifier ---------------------------------------------------
    def main(self, *extra: str, out: Optional[pathlib.Path] = None) -> Tuple[int, str]:
        out = out or self.tmp / "out"
        argv = [TAG, "--root", str(self.root), "--out", str(out), *extra]
        if self.staging is not None:
            argv += ["--staging", str(self.staging)]

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = rc.main(
                argv,
                gh=self.gh,
                head_of=lambda root: self.head,
                registry=lambda root: copy.deepcopy(self.registry),
            )
        return code, buffer.getvalue()

    def in_workflow(self, *extra: str) -> Tuple[int, str]:
        """As the verify job runs it: this run, its own reports."""
        this = self.ids["rc-evidence.yml"]
        counted = self.counted("rc-evidence.yml")
        counted["status"], counted["conclusion"] = "in_progress", None
        jobs = [j for j in self.jobs("rc-evidence.yml") if j["name"] != rc.VERIFY_JOB]
        self.set_jobs(this, jobs + [_job(rc.VERIFY_JOB, None, "in_progress")])
        return self.main(
            "--junit", str(self.junit_dir()), "--this-run", str(this), *extra
        )


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


def _fails(world: World, *needles: str, extra: Tuple[str, ...] = ()) -> str:
    code, text = world.main(*extra)
    assert code == 1, text
    assert text.rstrip().endswith("RC EVIDENCE: FAIL"), text
    for needle in needles:
        assert needle in text, f"{needle!r} not in:\n{text}"
    return text


# ---------------------------------------------------------------------------
# PASS, and what a PASS leaves behind
# ---------------------------------------------------------------------------
def test_a_green_release_passes_and_archives_what_it_read(world):
    out = world.tmp / "record"
    code, text = world.main(out=out)
    assert code == 0, text
    assert text.splitlines()[-1] == "RC EVIDENCE: PASS"
    assert "| docs-journeys.yml |" in text and "counted" in text
    # PE 15: the verdicts and reports it judged, in its own (90-day) record.
    assert (out / "rc-evidence.md").read_text() == text
    record = json.loads((out / "runs.json").read_text())
    assert record["sha"] == RC and all(c["ok"] for c in record["checks"])
    for artifact in (rc.JOURNEY_VERDICTS, rc.DEMO_VERDICTS):
        assert json.loads((out / "verdicts" / artifact / "verdicts.json").read_text())
    assert (out / "junit" / "unit.xml").is_file()


def test_the_verify_job_passes_on_its_own_run_and_reports(world):
    code, text = world.in_workflow()
    assert code == 0, text
    assert "this run" in text


def test_it_only_reads(world):
    """Every gh call is a plain GET of the API or an artifact download."""
    world.main()
    assert world.calls
    for call in world.calls:
        assert call[0] == "api" and len(call) == 2 or call[:2] == ["run", "download"], (
            call
        )


def test_plan_prints_one_dispatch_per_workflow_at_the_tag_and_runs_nothing(capsys):
    def refuse(args):
        raise AssertionError("--plan called gh")

    assert rc.main(["--plan", TAG], gh=refuse) == 0
    printed = capsys.readouterr().out.splitlines()
    commands = [line for line in printed if line.startswith("gh workflow run")]
    dispatched = [w.file for w in rc.WORKFLOWS if w.at_tag]
    assert [c.split()[3] for c in commands] == dispatched
    assert all(f"--ref {TAG} -R {rc.REPO}" in c for c in commands)
    assert dispatched[-1] == "rc-evidence.yml" and dispatched[0] == "docs.yml"
    assert dispatched.index("docs.yml") < dispatched.index("docs-journeys.yml")


@pytest.mark.parametrize("tag", ["v0.30", "0.30.0", "v0.30.0-rc.1", "v0.30.0 ", "main"])
def test_a_tag_that_is_not_a_final_release_is_refused(world, tag, capsys):
    assert rc.main([tag, "--out", str(world.tmp / "o")], gh=world.gh) == 2
    assert rc.main(["--plan", tag]) == 2
    assert world.calls == []


def test_the_staging_directory_is_never_read_in_a_run(world):
    staging = world.with_staging()
    code = rc.main(
        [
            TAG,
            "--out",
            str(world.tmp / "o"),
            "--junit",
            str(world.tmp),
            "--this-run",
            "1",
            "--staging",
            str(staging),
        ],
        gh=world.gh,
    )
    assert code == 2


# ---------------------------------------------------------------------------
# E1: the runs at the RC commit
# ---------------------------------------------------------------------------
def test_a_tag_github_cannot_resolve_fails(world):
    del world.api[f"git/ref/tags/{TAG}"]
    _fails(world, f"cannot resolve {TAG}")


def test_a_workflow_with_no_dispatch_at_the_tag_fails(world):
    world.set_runs(
        "sdk-unit-tests.yml",
        [_run(1500, "sdk-unit-tests.yml", event="schedule", ref="main")],
    )
    _fails(world, f"no workflow_dispatch run at {TAG}")


def test_a_dispatch_at_the_wrong_ref_is_not_the_evidence(world):
    """main at the same commit is not the tag: the docs journeys of a dispatch
    at main record and compare differently."""
    run = world.counted("chart-kind.yml")
    run["head_branch"] = "main"
    _fails(world, f"no workflow_dispatch run at {TAG}")


def test_a_counted_run_that_failed_fails(world):
    world.counted("compose-production.yml")["conclusion"] = "failure"
    _fails(world, "concluded failure")


def test_a_run_created_before_the_rc_commit_fails(world):
    world.counted("infrastructure-tests.yml")["created_at"] = "2026-11-01T09:00:00Z"
    _fails(world, "was created before the RC commit")


def test_a_run_still_going_at_the_commit_fails(world):
    world.add_run(
        "doc-examples.yml",
        _run(
            1600, "doc-examples.yml", event="schedule", ref="main", status="in_progress"
        ),
    )
    _fails(world, "run 1600 is still in_progress")


def test_a_failed_job_fails(world):
    world.jobs("doc-examples.yml")[0]["conclusion"] = "failure"
    _fails(world, "job 'Doc Examples (render)' failure")


def test_an_allowed_skip_may_only_be_skipped(world):
    job = next(
        j
        for j in world.jobs("chart-kind.yml")
        if j["name"] == "File an issue on failure"
    )
    job["conclusion"] = "failure"
    _fails(world, "job 'File an issue on failure' failure")


def test_a_skip_outside_the_allow_list_fails(world):
    world.jobs("release-gate.yml")[0]["conclusion"] = "skipped"
    _fails(world, "job 'build' skipped")


def test_docs_yml_must_have_deployed_the_tag(world):
    job = next(
        j for j in world.jobs("docs.yml") if j["name"] == "deploy to GitHub Pages"
    )
    job["conclusion"] = "skipped"
    _fails(world, "job 'deploy to GitHub Pages' skipped")


def test_all_16_sdk_jobs_must_have_run(world):
    jobs = [j for j in world.jobs("sdk-unit-tests.yml") if j["name"] != "ios"]
    world.set_jobs(world.ids["sdk-unit-tests.yml"], jobs)
    _fails(world, "job 'ios' did not run")


def test_a_run_with_no_jobs_fails(world):
    world.set_jobs(world.ids["release-gate.yml"], [])
    _fails(world, "has no jobs")


def test_a_run_listed_at_another_commit_is_refused(world):
    """QA E1's plant: the list is filtered by commit on GitHub's side, and each
    run's own commit is checked here anyway."""
    world.add_run(
        "chart-kind.yml", dict(_run(950, "chart-kind.yml", sha=OLD), stray=True)
    )
    _fails(world, f"run 950 is at {OLD[:12]}, not the RC {RC[:12]}")


def _annotated(world: World) -> None:
    world.api[f"git/ref/tags/{TAG}"] = {"object": {"type": "tag", "sha": TAG_OBJECT}}
    world.api[f"git/tags/{TAG_OBJECT}"] = {"object": {"type": "commit", "sha": RC}}


def test_an_annotated_tag_is_followed_to_its_commit(world):
    _annotated(world)
    code, text = world.main()
    assert code == 0, text
    assert text.splitlines()[0] == f"# RC evidence: {TAG} ({RC[:12]})"


def test_a_run_at_the_tag_object_and_not_its_commit_is_refused(world):
    _annotated(world)
    run = world.counted("docs.yml")
    run["head_sha"], run["stray"] = TAG_OBJECT, True
    _fails(
        world,
        f"is at {TAG_OBJECT[:12]}, not the RC",
        f"no workflow_dispatch run at {TAG}",
    )


def test_a_tag_that_is_also_a_branch_name_is_refused(world):
    world.api[f"git/matching-refs/heads/{TAG}"] = [{"ref": f"refs/heads/{TAG}"}]
    _fails(world, f"{TAG} is also the name of a branch")


def test_a_branch_that_only_starts_with_the_tag_is_another_name(world):
    world.api[f"git/matching-refs/heads/{TAG}"] = [{"ref": f"refs/heads/{TAG}-notes"}]
    code, text = world.main()
    assert code == 0, text


def test_an_unreadable_branch_list_fails(world):
    del world.api[f"git/matching-refs/heads/{TAG}"]
    _fails(world, f"cannot resolve {TAG}")


# ---------------------------------------------------------------------------
# PE 12: every run at the commit is listed; a FAIL then a PASS needs a reason
# ---------------------------------------------------------------------------
def _fail_then_pass(world: World, workflow: str = "docs-journeys.yml") -> int:
    world.add_run(
        workflow,
        _run(900, workflow, conclusion="failure", created="2026-11-01T10:30:00Z"),
    )
    return 900


def test_a_fail_then_a_pass_without_a_disposition_fails(world):
    run_id = _fail_then_pass(world)
    text = _fails(
        world,
        f"run {run_id} (workflow_dispatch, failure) did not succeed and has no disposition",
    )
    assert f"[{run_id}]" in text  # listed in the table


def test_a_fail_then_a_pass_with_a_disposition_passes_and_says_why(world):
    run_id = _fail_then_pass(world)
    code, text = world.main(
        "--dispositions", f"{run_id}: the npm registry answered 503 at step 4"
    )
    assert code == 0, text
    assert "disposition: the npm registry answered 503 at step 4" in text


def test_the_earlier_failed_evidence_run_is_listed_not_counted(world):
    """The first rc-evidence run at the commit failed; the newer one counts,
    and the failed one needs its disposition."""
    run_id = _fail_then_pass(world, "rc-evidence.yml")
    _fails(world, f"run {run_id} (workflow_dispatch, failure)")
    code, text = world.in_workflow(
        "--dispositions", f"{run_id}: run before docs.yml deployed"
    )
    assert code == 0, text


def test_a_failed_run_of_another_event_at_the_commit_needs_a_disposition_too(world):
    world.add_run(
        "chart-kind.yml",
        _run(
            901, "chart-kind.yml", event="schedule", ref="main", conclusion="cancelled"
        ),
    )
    _fails(world, "run 901 (schedule, cancelled) did not succeed")


def test_a_rerun_after_a_failed_attempt_needs_a_disposition(world):
    run = world.counted("infrastructure-tests.yml")
    run["run_attempt"] = 2
    world.api[f"actions/runs/{run['id']}/attempts/1"] = _run(
        run["id"], "infrastructure-tests.yml", conclusion="failure", attempt=1
    )
    _fails(world, f"run {run['id']} (attempt 1, failure) did not succeed")
    code, text = world.main("--dispositions", f"{run['id']}: pip index timed out")
    assert code == 0, text


def test_a_disposition_that_names_no_failed_run_fails(world):
    _fails(
        world,
        "the disposition of run 4242 names no run",
        extra=("--dispositions", "4242: typo"),
    )


@pytest.mark.parametrize(
    "text",
    ["4242", "4242:", "x: reason", "4242: 1234", "4242: a" + "b" * 200, "1: a; 1: b"],
)
def test_a_malformed_disposition_is_a_usage_error(world, text):
    code, _ = world.main("--dispositions", text)
    assert code == 2


# ---------------------------------------------------------------------------
# E2: the docs journeys
# ---------------------------------------------------------------------------
def test_journeys_that_read_a_site_built_from_another_tag_fail(world):
    world.verdicts[rc.JOURNEY_VERDICTS]["published"] = {"ref": "v0.29.0", "sha": OLD}
    _fails(world, "built from v0.29.0", "deploy the tag with docs.yml first")


def test_journeys_that_recorded_no_published_site_fail(world):
    del world.verdicts[rc.JOURNEY_VERDICTS]["published"]
    _fails(world, "an unrecorded ref")


@pytest.mark.parametrize("word", ["PARTIAL", "FAIL"])
def test_a_journey_that_is_not_pass_fails(world, word):
    world.verdicts[rc.JOURNEY_VERDICTS]["journeys"]["beta-guide"] = {
        "word": word,
        "step": 3,
    }
    _fails(world, f"journey beta-guide is {word} at step 3")


def test_a_journey_with_a_file_and_no_verdict_fails(world):
    (world.root / rc.JOURNEYS / "gamma-guide.yaml").write_text("guide: y\n")
    _fails(world, "journey gamma-guide did not run")


def test_a_verdict_for_a_journey_the_tag_does_not_have_fails(world):
    world.verdicts[rc.DEMO_VERDICTS]["journeys"]["ghost"] = {"word": "PASS", "step": 0}
    _fails(world, "journey ghost ran but has no file")


def test_a_journey_reported_by_both_jobs_fails(world):
    world.verdicts[rc.DEMO_VERDICTS]["journeys"]["alpha-guide"] = {
        "word": "PASS",
        "step": 0,
    }
    _fails(world, "journey alpha-guide is reported twice")


def test_verdicts_at_another_commit_fail(world):
    world.verdicts[rc.DEMO_VERDICTS]["sha"] = OLD
    _fails(world, f"{rc.DEMO_VERDICTS} is at {OLD[:12]}, not the RC")


def test_missing_demo_verdicts_fail(world):
    world.verdicts[rc.DEMO_VERDICTS] = None
    _fails(world, f"{rc.DEMO_VERDICTS} of run")


# ---------------------------------------------------------------------------
# E4: every registry test target passed at the RC, and was not skipped
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "case, needle",
    [
        (
            ("backend.tests.unit.x.test_alpha", "test_one", "skipped"),
            f"{TARGET_FN}: skipped at {RC[:12]}",
        ),
        (
            ("backend.tests.unit.x.test_alpha", "test_one", "failed"),
            f"{TARGET_FN}: failed at {RC[:12]}",
        ),
        (
            ("backend.tests.unit.x.test_alpha", "test_many[b]", "failed"),
            f"{TARGET_PARAM}: failed",
        ),
        (
            ("backend.tests.unit.x.test_alpha.TestK", "test_two", "skipped"),
            f"{TARGET_METHOD}: skipped",
        ),
        (
            ("backend.tests.unit.x.test_beta.TestB", "test_second", "skipped"),
            f"{TARGET_FILE}: skipped",
        ),
    ],
)
def test_a_target_that_did_not_pass_fails(world, case, needle):
    world.cases = [c for c in world.cases if c[:2] != case[:2]] + [case]
    _fails(world, needle)


def test_a_target_missing_from_the_reports_fails(world):
    world.cases = [c for c in world.cases if c[1] != "test_one"]
    _fails(world, f"{TARGET_FN}: not in the suites' JUnit at {RC[:12]}")


def test_a_target_whose_module_skipped_at_collection_fails(world):
    world.cases = [
        c for c in world.cases if not c[0].startswith("backend.tests.unit.x.test_alpha")
    ]
    world.cases.append(("", "backend.tests.unit.x.test_alpha", "skipped"))
    _fails(
        world, f"{TARGET_FN}: skipped at {RC[:12]} (its module skipped while collected)"
    )


def test_a_target_no_suite_reports_fails(world):
    world.registry.targets.append(
        ("test:sdk/js/src/client.test.ts::evaluates", "docs/c.md")
    )
    _fails(world, "no suite at the RC reports this test")


@pytest.mark.parametrize("outcome", ["skipped", "failed"])
def test_a_jest_target_that_did_not_pass_fails(world, outcome):
    path, name, _ = world.jest_cases[0]
    world.jest_cases[0] = (path, name, outcome)
    _fails(world, f"{JEST_FN}: {outcome} at {RC[:12]}")


def test_a_jest_name_is_matched_only_as_a_whole_word(world):
    """'renders the card' is not held by 'renders the cards list' alone."""
    world.jest_cases = [c for c in world.jest_cases if c[1] != "Alpha renders the card"]
    _fails(world, f"{JEST_FN}: not in the suites' JUnit at {RC[:12]}")


def test_a_jest_file_target_with_a_skipped_test_fails(world):
    world.jest_cases.append(
        ("frontend/src/tests/beta.test.tsx", "Beta later", "skipped")
    )
    _fails(world, f"{JEST_FILE}: skipped at {RC[:12]}")


def test_a_missing_frontend_report_fails(world):
    world.suite_reports["frontend.xml"] = None
    _fails(world, "suite frontend: no readable JUnit report frontend.xml")


def test_jest_json_is_written_as_junit(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    report = tmp_path / "jest.json"
    alpha = str(root / "frontend/src/tests/alpha.test.tsx")
    report.write_text(
        json.dumps(
            {
                "testResults": [
                    {
                        "name": alpha,
                        "status": "failed",
                        "assertionResults": [
                            {"fullName": "Alpha renders the card", "status": "passed"},
                            {"fullName": "Alpha later", "status": "todo"},
                            {"fullName": "Alpha broken", "status": "failed"},
                        ],
                    },
                    {
                        "name": str(root / "frontend/src/tests/gone.test.tsx"),
                        "status": "failed",
                        "assertionResults": [],
                    },
                ]
            }
        )
    )
    out = tmp_path / "frontend.xml"
    assert rc.jest_junit(report, out, root) == 4
    cases = rc.read_junit(out)
    assert [(c.classname, c.name, c.outcome) for c in cases] == [
        ("frontend/src/tests/alpha.test.tsx", "Alpha renders the card", "passed"),
        ("frontend/src/tests/alpha.test.tsx", "Alpha later", "skipped"),
        ("frontend/src/tests/alpha.test.tsx", "Alpha broken", "failed"),
        ("frontend/src/tests/gone.test.tsx", "(the file did not run)", "failed"),
    ]
    assert rc.target_problem(JEST_FN, cases, "x") is None
    assert rc.target_problem("test:frontend/src/tests/alpha.test.tsx", cases, "x")


def test_a_suite_without_its_report_fails(world):
    world.suite_reports["smoke.xml"] = None
    _fails(world, "suite smoke: no readable JUnit report smoke.xml")


def test_a_suite_report_with_no_test_fails(world):
    world.suite_reports["modules.xml"] = _junit([])
    _fails(world, "suite modules: modules.xml holds no test")


def test_no_evidence_run_means_the_suites_did_not_run_at_the_rc(world):
    """The dry run at a tag cut before this workflow existed: E4 fails."""
    world.set_runs("rc-evidence.yml", [])
    _fails(world, f"no rc-evidence.yml run at {TAG}: the suites did not run at the RC")


def test_the_verify_job_reads_its_own_reports(world):
    world.cases = [c for c in world.cases if c[1] != "test_one"]
    code, text = world.in_workflow()
    assert code == 1 and "not in the suites' JUnit" in text


# ---------------------------------------------------------------------------
# The registry at the tag
# ---------------------------------------------------------------------------
def test_one_untested_unit_fails(world):
    world.registry.untested = 1
    _fails(world, "1 units are untested")


def test_pending_documents_fail(world):
    world.registry.pending = 3
    _fails(world, "CLAIMS_PENDING lists 3 documents")


def test_a_registry_problem_fails(world):
    world.registry.problems = ["docs/a.md:3: unregistered unit 'x'"]
    _fails(world, "registry: docs/a.md:3: unregistered unit")


def test_a_tree_that_is_not_the_tag_fails(world):
    world.head = OLD
    _fails(world, f"is {OLD}, not the RC")


def test_the_real_registry_is_read_as_doc_claims_counts_it():
    """read_registry on this tree agrees with scripts/doc_claims.py itself: its
    problems, the pending and deferred lists, the untested units, and every
    test: target of the registries (plus each mechanism's test)."""
    registry = rc.read_registry(REPO_ROOT)
    spec = importlib.util.spec_from_file_location(
        "doc_claims_for_rc_test", REPO_ROOT / "scripts" / "doc_claims.py"
    )
    claims = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = claims
    spec.loader.exec_module(claims)
    result = claims.check(REPO_ROOT)
    assert registry.problems == result.problems
    assert registry.pending == len(claims.CLAIMS_PENDING)
    assert registry.deferred == len(claims.CLAIMS_DEFERRED)
    untested = sum(
        v for k, v in result.counts["dispositions"].items() if k.startswith("untested:")
    )
    assert registry.untested == untested
    named = set()
    for path in (REPO_ROOT / rc.CLAIMS).rglob("*.toml"):
        for entry in tomllib.loads(path.read_text(encoding="utf-8")).get("unit", []):
            for target in entry["by"]:
                if target.startswith("test:"):
                    named.add(target)
                elif target.startswith("mechanism:"):
                    named.add(
                        "test:" + claims.MECHANISMS[target[len("mechanism:") :]].test
                    )
    assert {t for t, _ in registry.targets} == named
    assert named, "the registries name no test: the comparison checked nothing"


# ---------------------------------------------------------------------------
# E7: staging, with --staging
# ---------------------------------------------------------------------------
def test_a_green_staging_deploy_passes(world):
    world.with_staging()
    code, text = world.main()
    assert code == 0, text
    assert "green after the deploy" in text
    assert "api_live_target_group" not in text  # the private files are read, not copied


def _synthetic(run_id: int, created: str) -> Dict[str, Any]:
    return _run(
        run_id, "synthetic.yml", event="schedule", ref="main", sha=OLD, created=created
    )


def test_a_synthetic_history_of_more_than_100_runs_passes(world):
    """The lists start at the RC commit's time, so the runs before it are not
    read: a workflow's whole history passing 100 runs does not fail E7."""
    world.with_staging()
    for n in range(150):
        created = f"2026-10-{1 + n % 28:02d}T0{n % 10}:17:00Z"
        world.add_run("synthetic.yml", _synthetic(5000 + n, created))
    assert len(world.runs("synthetic.yml")) > 100
    code, text = world.main()
    assert code == 0, text
    bound = "created=" + urllib.parse.quote(">=" + COMMIT_TIME, safe="")
    lists = [
        c[1]
        for c in world.calls
        if c[0] == "api" and "/runs?" in c[1] and "head_sha" not in c[1]
    ]
    assert lists and all(bound in path for path in lists), lists


def test_more_than_a_page_after_the_commit_is_read_page_by_page(world):
    world.with_staging()
    for n in range(120):
        created = f"2026-11-03T{n // 6:02d}:{(n % 6) * 10:02d}:00Z"
        world.add_run("synthetic.yml", _synthetic(8000 + n, created))
    code, text = world.main()
    assert code == 0, text
    pages = [c for c in world.calls if c[0] == "api" and "synthetic.yml/runs" in c[1]]
    assert [c[1].rsplit("&page=", 1)[1] for c in pages] == ["1", "2"]


def test_a_list_longer_than_the_pages_read_is_refused(world, monkeypatch):
    monkeypatch.setattr(rc, "MAX_PAGES", 1)
    world.with_staging()
    for n in range(120):
        created = f"2026-11-03T{n // 6:02d}:{(n % 6) * 10:02d}:00Z"
        world.add_run("synthetic.yml", _synthetic(8000 + n, created))
    _fails(world, "synthetic.yml: more than 100 runs; none is read")


def test_a_live_image_of_the_previous_release_fails(world):
    world.with_staging()
    (world.staging / "live-image-version.txt").write_text("0.29.0\n")
    _fails(world, f"live image v0.29.0, not {TAG}")


def test_no_deploy_of_the_tag_fails(world):
    world.with_staging()
    world.set_runs("deploy.yml", world.runs("deploy.yml")[1:])
    _fails(world, f"no Deploy run of {TAG} to staging")


def test_a_failed_deploy_fails(world):
    world.with_staging()
    world.runs("deploy.yml")[0]["conclusion"] = "failure"
    _fails(world, "Deploy run 7000 concluded failure")


def test_too_few_green_synthetic_runs_fail(world):
    world.with_staging()
    world.runs("synthetic.yml")[2]["conclusion"] = "skipped"
    _fails(world, "2 green Synthetic check runs after the deploy; at least 3")


def test_a_failed_synthetic_run_after_the_deploy_needs_a_disposition(world):
    world.with_staging()
    world.runs("synthetic.yml").append(
        _run(
            7200,
            "synthetic.yml",
            event="schedule",
            ref="main",
            sha=OLD,
            conclusion="failure",
            created="2026-11-02T13:47:00Z",
        )
    )
    _fails(world, "run 7200 (Synthetic check, failure)")


def test_a_target_group_that_is_not_reported_live_fails(world):
    world.with_staging()
    (world.staging / "live-target-group.txt").write_text(
        "REFUSED: the rules disagree\n"
    )
    _fails(world, "has no 'ok: <colour> is live' line")


@pytest.mark.parametrize("name", rc.STAGING_FILES)
def test_a_missing_staging_check_fails(world, name):
    world.with_staging()
    (world.staging / name).unlink()
    _fails(world, f"{name} is not in the staging directory")


def test_an_empty_list_of_read_only_checks_fails(world):
    world.with_staging()
    (world.staging / "read-only-checks.md").write_text("\n")
    _fails(world, "read-only-checks.md is empty")


# ---------------------------------------------------------------------------
# --suite: what rc-evidence.yml's suites jobs run
# ---------------------------------------------------------------------------
def test_a_suite_runs_every_session_and_fails_if_any_did(tmp_path, monkeypatch):
    calls: List[Tuple[List[str], Dict[str, str]]] = []

    class Done:
        def __init__(self, code):
            self.returncode = code

    def runner(command, cwd, env, check):
        calls.append((command, env))
        assert cwd == str(rc.ROOT) and check is False
        return Done(1 if "e2e.xml" in command[-1] else 0)

    assert rc.run_suite("integration", tmp_path, runner=runner) == 1
    assert len(calls) == len(rc.SUITES["integration"])  # it ran them all
    for (command, env), invocation in zip(calls, rc.SUITES["integration"]):
        assert command[-1] == f"--junitxml={tmp_path / invocation.report}"
        assert "-x" not in command and "--exitfirst" not in command
        for key, value in invocation.env:
            assert env[key] == value
    assert rc.run_suite("smoke", tmp_path, runner=lambda c, **k: Done(0)) == 0


def test_the_frontend_suite_runs_jest_and_writes_its_report_as_junit(tmp_path):
    seen: Dict[str, Any] = {}

    class Done:
        returncode = 0

    def runner(command, cwd, env, check):
        seen["command"], seen["cwd"] = command, cwd
        out = next(a for a in command if a.startswith("--outputFile=")).split("=", 1)[1]
        beta = str(rc.ROOT / "frontend/src/tests/beta.test.tsx")
        pathlib.Path(out).write_text(
            json.dumps(
                {
                    "testResults": [
                        {
                            "name": beta,
                            "status": "passed",
                            "assertionResults": [
                                {"fullName": "Beta loads", "status": "passed"}
                            ],
                        }
                    ]
                }
            )
        )
        return Done()

    assert rc.run_suite(rc.FRONTEND, tmp_path, runner=runner) == 0
    assert seen["cwd"] == str(rc.ROOT / "frontend")
    assert seen["command"][:3] == ["npx", "jest", "--watchAll=false"]
    assert "--passWithNoTests" not in seen["command"]
    cases = rc.read_junit(tmp_path / "frontend.xml")
    assert [(c.classname, c.outcome) for c in cases] == [
        ("frontend/src/tests/beta.test.tsx", "passed")
    ]


def test_a_frontend_suite_that_wrote_no_report_fails(tmp_path):
    class Done:
        returncode = 0

    assert rc.run_suite(rc.FRONTEND, tmp_path, runner=lambda c, **k: Done()) == 1


def test_the_suites_report_names_are_unique():
    names = [i.report for invocations in rc.SUITES.values() for i in invocations]
    assert len(names) == len(set(names))
