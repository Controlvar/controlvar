"""``scripts/rc_evidence.py`` reads a release candidate's runs back and says
PASS only when every one of them is there and passed (#1142).

Every GitHub and registry answer here is a fixture, made up: a fake ``gh``
that answers the GETs the verifier makes from a dict, and writes artifact files
for ``gh run download``, and a fake fetch for PyPI's and npm's JSON and
tarballs. A good world passes; each FAIL path changes one thing in it and the
verdict must turn to FAIL with the problem named. Nothing here reaches GitHub
or a registry, and no test asserts a time.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
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
#: GITHUB_REPOSITORY, as conftest.py sets it for every test here.
REPO = "Controlvar/controlvar"
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
SDK_PY = "test:sdk/python/tests/test_client.py::TestFlags::test_maps_the_flag"
SDK_JS = "test:sdk/js/__tests__/client.test.ts::maps the flag"
DEMO_JEST = "test:demo/shoplab/src/__tests__/search.test.tsx::flag on: fuzzy engine"
DEMO_SIM = "test:demo/streampulse/simulator/test_story.py::test_the_story_runs"
SDK_GO = "test:sdk/go/client_test.go::TestClient_Flags"
SDK_JAVA = "test:sdk/java/core/src/test/java/ClientTest.java"
JS_NAME, PY_NAME = "@getexperimently/js-sdk", "experimently"
#: The commits the SDK tags name: older than the RC, as a release that
#: publishes no new SDK version finds them.
TAG_COMMITS = {"js": "d4" * 20, "python": "e5" * 20}
#: Made-up git tree ids of sdk/js and sdk/python.
TREES = {"js": "f6" * 20, "python": "a7" * 20}
JS_VERSION = PY_VERSION = "0.1.1"
WHEEL = f"experimently-{PY_VERSION}-py3-none-any.whl"
SDIST = f"experimently-{PY_VERSION}.tar.gz"
TGZ = f"getexperimently-js-sdk-{JS_VERSION}.tgz"
#: Made-up package files: what sdk-release.yml built and recorded.
FILES = {
    WHEEL: b"a made-up wheel",
    SDIST: b"a made-up sdist",
    TGZ: b"a made-up npm tarball",
}
PYPI_URL = "https://pypi.org/pypi/experimently/json"
NPM_URL = "https://registry.npmjs.org/@getexperimently%2fjs-sdk"
TARBALL_URL = f"https://registry.npmjs.org/{JS_NAME}/-/js-sdk-{JS_VERSION}.tgz"
GOOD_DESCRIPTION = {
    "PyPI": (
        "# experimently\n\nThe Python client. See the"
        " [guide](https://docs.controlvar.com/sdk/python/).\n\n"
        f"```bash\npip install experimently=={PY_VERSION}\n```\n\n"
        "```bash\n# from a clone\npip install -e sdk/python\n```\n"
    ),
    "npm": (
        "# @getexperimently/js-sdk\n\nSee the"
        " [guide](https://docs.controlvar.com/sdk/javascript/).\n\n"
        f"```bash\nnpm install @getexperimently/js-sdk@{JS_VERSION}\n```\n"
    ),
}
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
        "html_url": f"https://github.com/Controlvar/controlvar/actions/runs/{run_id}",
        "display_title": title or workflow,
    }


def _job(name: str, conclusion: str = "success", status: str = "completed"):
    return {"name": name, "status": status, "conclusion": conclusion, "html_url": ""}


def _junit(cases: List[Tuple[str, ...]]) -> str:
    """A JUnit report of (classname, name, outcome[, jest's own title])."""
    body = []
    for classname, name, outcome, *title in cases:
        inner = {
            "passed": "",
            "failed": '<failure message="assert 1 == 2">boom</failure>',
            "errored": '<error message="fixture failed">trace</error>',
            "skipped": '<skipped type="pytest.skip" message="no reason"/>',
        }[outcome]
        own = f' title="{title[0]}"' if title else ""
        body.append(
            f'<testcase classname="{classname}" name="{name}"{own}>{inner}</testcase>'
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">'
        + "".join(body)
        + "</testsuite></testsuites>"
    )


#: (file, full name, outcome, own title), as jest_junit writes them.
JEST_CASES = [
    (
        "frontend/src/tests/alpha.test.tsx",
        "Alpha renders the card",
        "passed",
        "renders the card",
    ),
    (
        "frontend/src/tests/alpha.test.tsx",
        "Alpha renders the cards list",
        "passed",
        "renders the cards list",
    ),
    ("frontend/src/tests/beta.test.tsx", "Beta loads", "passed", "loads"),
]
#: The cases the new suites report, holding the SDK and demo targets.
SUITE_CASES = {
    "sdk-python.xml": [
        ("sdk.python.tests.test_client.TestFlags", "test_maps_the_flag", "passed"),
        ("sdk.python.tests.test_client.TestFlags", "test_other", "passed"),
    ],
    "sdk-js.xml": [
        (
            "sdk/js/__tests__/client.test.ts",
            "Client maps the flag",
            "passed",
            "maps the flag",
        ),
    ],
    "demo-shoplab.xml": [
        (
            "demo/shoplab/src/__tests__/search.test.tsx",
            "Search flag on: fuzzy engine",
            "passed",
            "flag on: fuzzy engine",
        ),
    ],
    "demo-streampulse-simulator.xml": [
        ("demo.streampulse.simulator.test_story", "test_the_story_runs", "passed"),
    ],
}
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
        self.report_cases = copy.deepcopy(SUITE_CASES)
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
                (SDK_PY, "docs/sdk/python.md"),
                (SDK_JS, "docs/sdk/javascript.md"),
                (DEMO_JEST, "demo/shoplab/README.md"),
                (DEMO_SIM, "demo/streampulse/README.md"),
                (SDK_GO, "docs/sdk/go.md"),
                (SDK_JAVA, "docs/sdk/java.md"),
            ],
            report=["documents in scope: 2"],
        )
        self.head = RC
        self.staging: Optional[pathlib.Path] = None
        self.logs: Dict[str, str] = {}
        self.web: Dict[str, bytes] = {}
        self.fetched: List[str] = []
        self.release_ids = {"js": 3000, "python": 3001}
        self.build_ids = {"js": 4000, "python": 4001}
        self.manifests = {"js": (JS_NAME, JS_VERSION), "python": (PY_NAME, PY_VERSION)}
        self.recorded = dict(FILES)
        self.published = dict(FILES)
        self.descriptions = dict(GOOD_DESCRIPTION)
        self.latest = {"js": JS_VERSION, "python": PY_VERSION}
        self.integrity: Optional[str] = None
        #: sdk/<name>'s tree id at each commit: the RC's equals each tag's.
        self.trees = {commit: dict(TREES) for commit in (RC, *TAG_COMMITS.values())}
        for sdk, commit in TAG_COMMITS.items():
            self.api[f"git/ref/tags/sdk/{sdk}/v{self.manifests[sdk][1]}"] = {
                "object": {"type": "commit", "sha": commit}
            }
        self.set_runs(
            rc.SDK_RELEASE,
            [
                _run(
                    self.release_ids[sdk],
                    rc.SDK_RELEASE,
                    event="push",
                    ref=f"sdk/{sdk}/v{self.manifests[sdk][1]}",
                    sha=TAG_COMMITS[sdk],
                )
                for sdk in ("js", "python")
            ],
        )
        for sdk, package in rc.PACKAGES.items():
            other = rc.PACKAGES["python" if sdk == "js" else "js"]
            jobs = [_job(rc.RESOLVE_JOB), _job(package.build_job)]
            jobs[1]["id"] = self.build_ids[sdk]
            jobs += [
                _job(package.publish_job),
                _job(other.build_job, "skipped"),
                _job(other.publish_job, "skipped"),
                _job("verify the Go module", "skipped"),
            ]
            self.set_jobs(self.release_ids[sdk], jobs)

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
        if "branch" in query:
            found = [r for r in found if r["head_branch"] == query["branch"][0]]
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

    def release_run(self, sdk: str) -> Dict[str, Any]:
        return next(
            r for r in self.runs(rc.SDK_RELEASE) if r["id"] == self.release_ids[sdk]
        )

    def release_jobs(self, sdk: str) -> List[Dict[str, Any]]:
        return self.api[
            f"actions/runs/{self.release_ids[sdk]}/jobs?filter=latest&per_page=100"
        ]["jobs"]

    def build_log(self, sdk: str) -> str:
        """The build job's log: what its record step printed, among the rest."""
        stamp = "2026-11-01T10:20:{:02d}.1234567Z "
        lines = ["Current runner version: '2.0'", "Run set -euo pipefail"]
        if sdk == "python":
            listed = [
                f"{hashlib.sha256(self.recorded[n]).hexdigest()}  {n}"
                for n in (WHEEL, SDIST)
            ]
            listing = "".join(line + "\n" for line in listed)
            lines += listed
            lines.append(f"digest {hashlib.sha256(listing.encode()).hexdigest()}")
        else:
            lines.append(f"{hashlib.sha256(self.recorded[TGZ]).hexdigest()}  {TGZ}")
        lines.append("SHA256 digest of uploaded artifact is " + "e" * 64)
        return "\ufeff" + "".join(
            stamp.format(n) + line + "\n" for n, line in zip(range(len(lines)), lines)
        )

    def registry_answer(self, url: str) -> Optional[bytes]:
        if url in self.web:
            return self.web[url]
        if url == PYPI_URL:
            return json.dumps(
                {
                    "info": {
                        "name": PY_NAME,
                        "version": self.latest["python"],
                        "description": self.descriptions["PyPI"],
                    },
                    "urls": [
                        {
                            "filename": n,
                            "digests": {
                                "sha256": hashlib.sha256(self.published[n]).hexdigest()
                            },
                        }
                        for n in (WHEEL, SDIST)
                        if n in self.published
                    ],
                }
            ).encode()
        if url == NPM_URL:
            tarball = self.published[TGZ]
            integrity = (
                self.integrity
                or "sha512-"
                + base64.b64encode(hashlib.sha512(tarball).digest()).decode()
            )
            return json.dumps(
                {
                    "name": JS_NAME,
                    "dist-tags": {"latest": self.latest["js"]},
                    "readme": self.descriptions["npm"],
                    "versions": {
                        JS_VERSION: {
                            "dist": {"integrity": integrity, "tarball": TARBALL_URL}
                        }
                    },
                }
            ).encode()
        if url == TARBALL_URL:
            return self.published[TGZ]
        return None

    def fetch(self, url: str) -> bytes:
        self.fetched.append(url)
        answer = self.registry_answer(url)
        if answer is None:
            raise rc.FetchError(f"GET {url}: HTTPError 404")
        return answer

    # -- the fake gh -------------------------------------------------------------
    def gh(self, args: List[str]) -> str:
        self.calls.append(list(args))
        if args[0] == "api":
            assert len(args) == 2, f"only a plain GET is made: {args}"
            prefix = f"repos/{REPO}/"
            assert args[1].startswith(prefix)
            path = args[1][len(prefix) :]
            if path.startswith("actions/workflows/") and "/runs?" in path:
                return json.dumps(self.list_runs(path))
            if path in self.logs:
                return self.logs[path]
            for sdk, job_id in self.build_ids.items():
                if path == f"actions/jobs/{job_id}/logs":
                    return self.build_log(sdk)
            if path.startswith("contents/sdk?ref="):
                trees = self.trees.get(path.split("=", 1)[1])
                if trees is None:
                    raise rc.GhError(f"gh api {path} failed (exit 1)")
                return json.dumps(
                    [{"name": n, "type": "dir", "sha": t} for n, t in trees.items()]
                    + [{"name": "README.md", "type": "file", "sha": "0" * 40}]
                )
            for sdk, package in rc.PACKAGES.items():
                if path == f"contents/{package.manifest}?ref={RC}":
                    name, version = self.manifests[sdk]
                    if sdk == "js":
                        text = json.dumps({"name": name, "version": version})
                    else:
                        text = f'[project]\nname = "{name}"\nversion = "{version}"\n'
                    encoded = base64.encodebytes(text.encode()).decode()
                    return json.dumps({"encoding": "base64", "content": encoded})
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
            if suite == "frontend":
                cases = self.jest_cases
            elif suite == "unit" and number == 0:
                cases = self.cases
            elif invocation.report in self.report_cases:
                cases = self.report_cases[invocation.report]
            elif invocation.runner == "jest":
                cases = [(f"{invocation.cwd}/x.test.ts", "it works", "passed")]
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
                fetch=self.fetch,
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
    assert set(world.fetched) == {PYPI_URL, NPM_URL, TARBALL_URL}
    assert all(url.startswith(rc.REGISTRY_URLS) for url in world.fetched)


def test_the_registry_get_refuses_any_other_url():
    """http_get reads the two registries only; nothing here reaches them."""
    for url in ("https://example.com/x", "http://pypi.org/pypi/x/json", "file:///etc"):
        with pytest.raises(rc.FetchError, match="not a registry URL"):
            rc.http_get(url)


def test_plan_prints_one_dispatch_per_workflow_at_the_tag_and_runs_nothing(capsys):
    def refuse(args):
        raise AssertionError("--plan called gh")

    assert rc.main(["--plan", TAG], gh=refuse) == 0
    printed = capsys.readouterr().out.splitlines()
    commands = [line for line in printed if line.startswith("gh workflow run")]
    dispatched = [w.file for w in rc.WORKFLOWS if w.at_tag]
    assert [c.split()[3] for c in commands] == dispatched
    assert all(f' --ref {TAG} -R "$REPO"' in c for c in commands)
    assert printed.index(
        'REPO="$(gh api repositories/1367480368 --jq .full_name)"'
    ) < printed.index(commands[0])
    assert dispatched[-1] == "rc-evidence.yml" and dispatched[0] == "docs.yml"
    assert dispatched.index("docs.yml") < dispatched.index("docs-journeys.yml")
    checks = [line for line in printed if "--registry" in line]
    assert checks == [
        f"python3 scripts/rc_evidence.py {TAG} --registry js --out <dir>",
        f"python3 scripts/rc_evidence.py {TAG} --registry python --out <dir>",
    ]
    assert printed.index(checks[-1]) < printed.index(commands[0])


#: The repository by another name: what a rename or a transfer makes it.
RENAMED = "example-org/example-repo"


@pytest.mark.regression
def test_the_repository_is_the_run_s_or_the_current_name_of_its_id():
    """The name used to be written into the script, so after a rename every
    read went to the old name. A run names its repository; on a machine the
    API answers the name of the repository's id."""
    calls = []

    def gh(args):
        calls.append(args)
        assert args == ["api", "repositories/1367480368"], args
        return json.dumps({"id": 1367480368, "full_name": RENAMED})

    assert rc.repository({"GITHUB_REPOSITORY": RENAMED}, gh) == RENAMED
    assert calls == []
    assert rc.repository({}, gh) == RENAMED
    assert len(calls) == 1
    assert rc.REPOSITORY_ID == "1367480368"


@pytest.mark.parametrize(
    "environ, answer",
    [
        ({"GITHUB_REPOSITORY": "a/b/c"}, None),
        ({"GITHUB_REPOSITORY": "owner/repo\n"}, None),
        ({}, "{}"),
        ({}, '{"full_name": "../x"}'),
        ({}, "not json"),
        ({}, rc.GhError("gh api failed (exit 1)")),
    ],
)
def test_no_owner_name_for_the_repository_is_a_usage_error(environ, answer):
    def gh(args):
        if isinstance(answer, Exception):
            raise answer
        assert answer is not None, "a run's own name needs no lookup"
        return answer

    with pytest.raises(rc.UsageError):
        rc.repository(environ, gh)


def test_main_reads_the_repository_the_id_names(world, monkeypatch):
    """With no GITHUB_REPOSITORY, every read goes to the id's current name."""
    monkeypatch.delenv("GITHUB_REPOSITORY")
    answered = world.gh

    def gh(args):
        if args == ["api", "repositories/1367480368"]:
            return json.dumps({"full_name": REPO})
        return answered(args)

    world.gh = gh
    code, text = world.main()
    assert code == 0, text
    assert world.calls[0] == ["api", f"repos/{REPO}/git/ref/tags/{TAG}"]


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


@pytest.mark.parametrize(
    "target",
    [
        "test:infrastructure/cdk/test/stack.test.ts::synthesises",
        "test:charts/experimently/tests/test_values.py::test_defaults",
        "test:tests/acceptance/docs/test_runner.py::test_x",
    ],
)
def test_a_target_outside_every_suite_fails_by_name(world, target):
    """A target whose file no suite in SUITES runs: refused by name, not left
    to read as missing from the reports."""
    world.registry.targets.append((target, "docs/c.md"))
    text = _fails(world, f"{target}: no suite in SUITES runs it")
    assert "not in the suites' JUnit" not in text


def test_a_target_outside_every_suite_is_named_even_when_the_suites_did_not_run(world):
    world.set_runs("rc-evidence.yml", [])
    target = "test:charts/experimently/tests/test_values.py::test_defaults"
    world.registry.targets.append((target, "docs/c.md"))
    _fails(
        world,
        "the suites did not run at the RC",
        f"{target}: no suite in SUITES runs it",
    )


def test_the_new_suites_cover_the_targets_they_were_added_for():
    for target, suite in (
        ("test:infrastructure/tests/test_aurora_engine.py::test_x", "infrastructure"),
        ("test:tests/sdk-contract/test_hash_contract.py::test_x", "sdk-contract"),
        ("test:sdk/python/tests/test_client.py::test_x", "sdk-python"),
        ("test:demo/shoplab/simulator/test_traffic.py::test_x", "demo-simulators"),
        ("test:demo/streampulse/src/__tests__/a.test.tsx::x", "demo-streampulse"),
        ("test:sdk/js/__tests__/client.test.ts::x", "sdk-js"),
        ("test:frontend/src/tests/a.test.tsx::x", "frontend"),
        ("test:backend/tests/unit/x/test_a.py::test_x", "unit"),
        ("test:backend/tests/unit/chart/test_chart_render.py::test_x", "chart"),
    ):
        assert rc.suite_of(target) == suite, target
    # The frontend session's other tree, read from SUITES (a core file does
    # not spell a module path).
    (extra,) = rc.SUITES["frontend"][0].also
    module_file = f"{extra}/tests/sso.test.ts"
    assert rc.suite_of(f"test:{module_file}") == "frontend"
    cases = [rc.Case(module_file, "SSO signs in", "passed", "signs in")]
    assert rc.target_problem(f"test:{module_file}::signs in", cases, "x") is None
    assert rc.suite_of("test:infrastructure/cdk/x.test.ts") is None
    assert rc.suite_of("test:backend/tests/unitx/test_a.py") is None  # a prefix only


def test_a_tree_a_session_ignores_is_not_its_own():
    """unit leaves backend/tests/unit/chart to the chart suite (there it would
    skip, and a skipped case fails a target whatever another report says)."""
    unit, chart = rc.SUITES["unit"][0], rc.SUITES["chart"][0]
    path = "backend/tests/unit/chart/test_chart_render.py"
    assert not unit.runs(path) and chart.runs(path)
    assert unit.runs("backend/tests/unit/chartx/test_a.py")  # a prefix only
    assert rc.session_of(f"test:{path}::test_x") == ("chart", chart)
    assert rc.session_of("test:backend/tests/unit/x/test_a.py") == ("unit", unit)
    assert rc.session_of("not a target") is None
    plain = rc.Invocation("x.xml", ("backend/tests/unit/",))
    assert plain.ignored == () and plain.runs(path)


@pytest.mark.parametrize("outcome", ["skipped", "failed"])
def test_a_jest_target_that_did_not_pass_fails(world, outcome):
    path, name, _, title = world.jest_cases[0]
    world.jest_cases[0] = (path, name, outcome, title)
    _fails(world, f"{JEST_FN}: {outcome} at {RC[:12]}")


def test_a_jest_name_is_matched_only_as_a_whole_word(world):
    """'renders the card' is not held by 'renders the cards list' alone."""
    world.jest_cases = [c for c in world.jest_cases if c[1] != "Alpha renders the card"]
    _fails(world, f"{JEST_FN}: not in the suites' JUnit at {RC[:12]}")


def test_a_longer_named_sibling_does_not_stand_in_for_a_removed_jest_test(world):
    """The target's test is gone; a sibling whose title holds its name as whole
    words passes. Only the exact full name or title is the target."""
    world.jest_cases = [
        c for c in world.jest_cases if c[1] != "Alpha renders the card"
    ] + [
        (
            "frontend/src/tests/alpha.test.tsx",
            "Alpha renders the card on mobile",
            "passed",
            "renders the card on mobile",
        )
    ]
    _fails(world, f"{JEST_FN}: not in the suites' JUnit at {RC[:12]}")


def test_a_jest_target_matches_its_full_name_or_its_own_title_exactly():
    case = rc.Case(
        "frontend/src/tests/a.test.tsx", "Card renders it", "passed", "renders it"
    )
    for name in ("Card renders it", "renders it"):
        assert (
            rc.target_problem(
                f"test:frontend/src/tests/a.test.tsx::{name}", [case], "x"
            )
            is None
        )
    for name in ("renders", "Card", "Card renders", "it"):
        assert rc.target_problem(
            f"test:frontend/src/tests/a.test.tsx::{name}", [case], "x"
        )


def test_a_jest_each_title_matches_every_test_that_fills_it_in():
    """``it.each([...])('judges a %s metric')``: the registry quotes the title
    as written; jest reports one test per value. All of them must pass."""
    path = "frontend/src/tests/a.test.tsx"
    target = f"test:{path}::judges a %s metric, 100%% of the time"
    cases = [
        rc.Case(
            path,
            f"Banner judges a {v} metric, 100% of the time",
            "passed",
            f"judges a {v} metric, 100% of the time",
        )
        for v in ("count", "revenue")
    ]
    assert rc.target_problem(target, cases, "x") is None
    one_skipped = cases + [
        rc.Case(
            path,
            "Banner judges a ratio metric, 100% of the time",
            "skipped",
            "judges a ratio metric, 100% of the time",
        )
    ]
    assert rc.target_problem(target, one_skipped, "x") == f"{target}: skipped at x"
    longer = [
        rc.Case(
            path, "x", "passed", "judges a count metric, 100% of the time on mobile"
        )
    ]
    assert (
        rc.target_problem(target, longer, "x")
        == f"{target}: not in the suites' JUnit at x"
    )
    assert rc.target_problem(f"test:{path}::judges a metric", cases, "x")


@pytest.mark.parametrize("outcome", ["failed", "errored"])
def test_a_failing_test_no_target_names_fails_the_rc(world, outcome):
    """Any failure or error in a suite's report, registry target or not."""
    world.cases.append(("backend.tests.unit.x.test_gamma", "test_unrelated", outcome))
    _fails(
        world,
        "suite unit: 1 tests failed or errored in unit.xml (first:"
        " backend.tests.unit.x.test_gamma::test_unrelated)",
    )


def test_a_failing_jest_test_no_target_names_fails_the_rc(world):
    world.report_cases["sdk-js.xml"].append(
        ("sdk/js/__tests__/other.test.ts", "Other breaks", "failed", "breaks")
    )
    _fails(world, "suite sdk-js: 1 tests failed or errored in sdk-js.xml")


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
                            {
                                "fullName": "Alpha renders the card",
                                "title": "renders the card",
                                "status": "passed",
                            },
                            {
                                "fullName": "Alpha later",
                                "title": "later",
                                "status": "todo",
                            },
                            {
                                "fullName": "Alpha broken",
                                "title": "broken",
                                "status": "failed",
                            },
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
    assert [c.title for c in cases] == ["renders the card", "later", "broken", ""]
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
        if c[0] == "api"
        and ("deploy.yml/runs?" in c[1] or "synthetic.yml/runs?" in c[1])
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
    # The chart suite: its tree, with the variable its module skips without.
    calls.clear()
    assert rc.run_suite("chart", tmp_path, runner=runner) == 0
    ((command, env),) = calls
    assert "backend/tests/unit/chart" in command
    assert env["EXPERIMENTLY_CHART_TESTS"] == "1"


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

    assert rc.run_suite("frontend", tmp_path, runner=runner) == 0
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

    assert rc.run_suite("frontend", tmp_path, runner=lambda c, **k: Done()) == 1


def test_the_suites_report_names_are_unique():
    names = [i.report for invocations in rc.SUITES.values() for i in invocations]
    assert len(names) == len(set(names))


def test_the_demo_simulators_run_as_a_session_each(tmp_path):
    """Both simulators import a module named ``traffic``: one session each."""
    calls: List[List[str]] = []

    class Done:
        returncode = 0

    def runner(command, cwd, env, check):
        calls.append(command)
        return Done()

    assert rc.run_suite("demo-simulators", tmp_path, runner=runner) == 0
    assert [c[3] for c in calls] == [
        "demo/shoplab/simulator",
        "demo/streampulse/simulator",
    ]
    calls.clear()
    assert rc.run_suite("sdk-python", tmp_path, runner=runner) == 0
    (command,) = calls
    assert command[3:7] == ["sdk/python/tests", "--rootdir=.", "-o", "addopts="]


def test_the_sdk_js_suite_runs_jest_in_its_own_tree(tmp_path):
    seen: Dict[str, Any] = {}

    class Done:
        returncode = 0

    def runner(command, cwd, env, check):
        seen["command"], seen["cwd"] = command, cwd
        out = next(a for a in command if a.startswith("--outputFile=")).split("=", 1)[1]
        test = str(rc.ROOT / "sdk/js/__tests__/client.test.ts")
        result = {
            "fullName": "Client maps the flag",
            "title": "maps the flag",
            "status": "passed",
        }
        pathlib.Path(out).write_text(
            json.dumps(
                {
                    "testResults": [
                        {"name": test, "status": "passed", "assertionResults": [result]}
                    ]
                }
            )
        )
        return Done()

    assert rc.run_suite("sdk-js", tmp_path, runner=runner) == 0
    assert seen["cwd"] == str(rc.ROOT / "sdk" / "js")
    assert "--ci" in seen["command"]
    cases = rc.read_junit(tmp_path / "sdk-js.xml")
    assert rc.target_problem(SDK_JS, cases, "x") is None


# ---------------------------------------------------------------------------
# E4 (PE 13): the published SDKs and the demo apps, test by test; the other
# SDKs at job level, named as the residual
# ---------------------------------------------------------------------------
def test_the_sdk_and_demo_targets_pass_test_by_test_and_the_residual_is_named(world):
    code, text = world.main()
    assert code == 0, text
    row = next(line for line in text.splitlines() if "(residual)" in line)
    assert "| E4 | other SDKs' targets (residual) | PASS |" in row
    assert "test level: sdk/js, sdk/python" in row
    assert "job level only (sdk-unit-tests.yml, no test-level report):" in row
    assert "go (1)" in row and "java (1)" in row and "ios (0)" in row
    claim = next(line for line in text.splitlines() if "| claim tests |" in line)
    assert "10/10 targets passed" in claim


@pytest.mark.parametrize(
    "report, case, needle",
    [
        (
            "sdk-python.xml",
            ("sdk.python.tests.test_client.TestFlags", "test_maps_the_flag", "skipped"),
            f"{SDK_PY}: skipped at {RC[:12]}",
        ),
        (
            "sdk-js.xml",
            (
                "sdk/js/__tests__/client.test.ts",
                "Client maps the flag",
                "failed",
                "maps the flag",
            ),
            f"{SDK_JS}: failed at {RC[:12]}",
        ),
        (
            "demo-shoplab.xml",
            (
                "demo/shoplab/src/__tests__/search.test.tsx",
                "Search flag on: fuzzy engine",
                "skipped",
                "flag on: fuzzy engine",
            ),
            f"{DEMO_JEST}: skipped at {RC[:12]}",
        ),
        (
            "demo-streampulse-simulator.xml",
            ("demo.streampulse.simulator.test_story", "test_the_story_runs", "failed"),
            f"{DEMO_SIM}: failed at {RC[:12]}",
        ),
    ],
)
def test_an_sdk_or_demo_target_that_did_not_pass_fails(world, report, case, needle):
    world.report_cases[report] = [case]
    _fails(world, needle)


def test_an_sdk_target_missing_from_its_report_fails(world):
    world.report_cases["sdk-python.xml"] = [
        ("sdk.python.tests.test_client.TestFlags", "test_other", "passed")
    ]
    _fails(world, f"{SDK_PY}: not in the suites' JUnit at {RC[:12]}")


@pytest.mark.parametrize(
    "report", ["sdk-python.xml", "sdk-js.xml", "demo-streampulse.xml"]
)
def test_a_missing_sdk_or_demo_report_fails(world, report):
    world.suite_reports[report] = None
    _fails(world, f"no readable JUnit report {report}")


def test_a_held_target_fails_when_its_sdk_job_did_not_succeed(world):
    job = next(j for j in world.jobs("sdk-unit-tests.yml") if j["name"] == "go")
    job["conclusion"] = "failure"
    run_id = world.ids["sdk-unit-tests.yml"]
    _fails(
        world,
        f"1 targets in sdk/go: its job in sdk-unit-tests.yml run {run_id} is failure",
    )


def test_a_published_sdks_target_is_never_held_at_job_level():
    assert rc.sdk_job_of(SDK_GO) == "go"
    assert rc.sdk_job_of("test:sdk/openfeature-python/tests/test_provider.py") == (
        "openfeature-python"
    )
    for target in (
        SDK_PY,
        SDK_JS,
        DEMO_JEST,
        DEMO_SIM,
        TARGET_FN,
        "test:sdk/README.md",
    ):
        assert rc.sdk_job_of(target) is None, target


def test_the_real_registrys_sdk_and_demo_targets_are_each_judged_by_a_suite():
    """Every target of this tree's registries in sdk/python, sdk/js or a demo
    app is matched against a suite's report, and every other SDK's is held by
    its job: none of them falls to 'no suite reports this test'."""
    targets = [t for t, _ in rc.read_registry(REPO_ROOT).targets]
    ours = [
        t
        for t in targets
        if t.startswith(("test:sdk/python/", "test:sdk/js/", "test:demo/"))
    ]
    assert {t.split("/")[1] for t in ours} >= {"python", "js", "shoplab", "streampulse"}
    for target in ours:
        assert rc.sdk_job_of(target) is None, target
        problem = rc.target_problem(target, [], "x")
        assert problem and "not in the suites' JUnit" in problem, problem
    for target in targets:
        if target.startswith("test:sdk/") and target not in ours:
            assert rc.sdk_job_of(target) in rc.SDK_JOBS, target


# ---------------------------------------------------------------------------
# P (PE 14): the published packages against what sdk-release.yml recorded
# ---------------------------------------------------------------------------
def test_both_packages_pass_against_their_record(world):
    code, text = world.main()
    assert code == 0, text
    js = next(line for line in text.splitlines() if f"| P | npm {JS_NAME}" in line)
    py = next(line for line in text.splitlines() if f"| P | PyPI {PY_NAME}" in line)
    assert "| PASS |" in js and "unchanged at the RC; 1 published files" in js
    assert "| PASS |" in py and "unchanged at the RC; 2 published files" in py
    assert "counted (published)" in text


def test_a_pypi_file_whose_sha256_is_not_the_recorded_one_fails(world):
    world.published[WHEEL] = b"another wheel"
    _fails(world, f"{WHEEL}: PyPI's sha256 is", "run 3001 recorded")


def test_a_pypi_file_the_run_did_not_record_fails(world):
    world.web[PYPI_URL] = json.dumps(
        {
            "info": {"version": PY_VERSION, "description": GOOD_DESCRIPTION["PyPI"]},
            "urls": [
                {
                    "filename": n,
                    "digests": {"sha256": hashlib.sha256(FILES[n]).hexdigest()},
                }
                for n in (WHEEL, SDIST)
            ]
            + [
                {
                    "filename": "experimently-0.1.1-py2-none-any.whl",
                    "digests": {"sha256": "0" * 64},
                }
            ],
        }
    ).encode()
    _fails(
        world,
        "experimently-0.1.1-py2-none-any.whl: on PyPI; run 3001 did not record it",
    )


def test_an_npm_integrity_that_is_not_the_recorded_tarballs_fails(world):
    world.integrity = (
        "sha512-" + base64.b64encode(hashlib.sha512(b"other").digest()).decode()
    )
    _fails(
        world, f"npm's dist.integrity of {JS_VERSION}", "is not the recorded tarball's"
    )


def test_an_npm_tarball_that_is_not_the_recorded_one_fails(world):
    """The registry is consistent with itself (its integrity is the tarball's),
    but the tarball is not the one the run tested and recorded."""
    world.published[TGZ] = b"a tarball nobody tested"
    _fails(world, f"npm's tarball of {JS_VERSION} has sha256", "run 3000 recorded")


@pytest.mark.parametrize(
    "registry, install",
    [
        ("PyPI", "pip install experimently==0.1.0"),
        ("npm", "npm install @getexperimently/js-sdk@0.1.0"),
    ],
)
def test_a_description_quoting_the_previous_version_fails(world, registry, install):
    """The manifest says 0.1.1; the description the registry shows says 0.1.0."""
    current = install.replace("0.1.0", "0.1.1")
    world.descriptions[registry] = GOOD_DESCRIPTION[registry].replace(current, install)
    assert install in world.descriptions[registry]
    _fails(
        world,
        f"the {registry} description's install line quotes 0.1.0, not the manifest's 0.1.1",
    )


@pytest.mark.parametrize("registry", ["PyPI", "npm"])
def test_a_description_with_no_pinned_install_line_fails(world, registry):
    world.descriptions[registry] = (
        "# A package\n\n```bash\npip install experimently\n```\n"
    )
    _fails(world, f"the {registry} description has no install line with a version")


@pytest.mark.parametrize("registry", ["PyPI", "npm"])
def test_a_description_with_a_relative_link_fails(world, registry):
    world.descriptions[registry] += (
        "\nSee [local evaluation](../../docs/sdk/local-evaluation.md).\n"
    )
    _fails(
        world,
        f"the {registry} description has 1 relative links (../../docs/sdk/local-evaluation.md)",
    )


@pytest.mark.parametrize("registry", ["PyPI", "npm"])
def test_a_description_that_says_unpublished_fails(world, registry):
    world.descriptions[registry] = (
        "**Not yet** published.\n\n" + world.descriptions[registry]
    )
    _fails(world, f"the {registry} description says it is not yet published")


@pytest.mark.parametrize("sdk, registry", [("js", "npm"), ("python", "PyPI")])
def test_a_registry_whose_latest_is_another_version_fails(world, sdk, registry):
    world.latest[sdk] = "0.1.0"
    name = JS_NAME if sdk == "js" else PY_NAME
    _fails(world, f"{registry}'s latest {name} is 0.1.0, not the manifest's 0.1.1")


def test_no_release_run_of_the_tag_fails(world):
    world.set_runs(rc.SDK_RELEASE, [world.release_run("python")])
    _fails(world, "no sdk-release.yml run of sdk/js/v0.1.1 succeeded")


def test_a_release_run_at_another_commit_than_its_tags_is_refused(world):
    world.release_run("js")["head_sha"] = OLD
    _fails(
        world,
        f"run 3000 is at {OLD[:12]}, not sdk/js/v0.1.1's commit {TAG_COMMITS['js'][:12]}",
    )


def test_a_release_at_an_older_commit_passes_when_the_rc_leaves_the_sdk_alone(world):
    """The tag's commit is not the RC: a platform release that publishes no
    new SDK. The RC's sdk/js and sdk/python trees are the tags', so it passes."""
    assert all(world.release_run(sdk)["head_sha"] != RC for sdk in ("js", "python"))
    code, text = world.main()
    assert code == 0, text
    assert "run 3000 of sdk/js/v0.1.1: sdk/js unchanged at the RC" in text
    assert "run 3001 of sdk/python/v0.1.1: sdk/python unchanged at the RC" in text


@pytest.mark.parametrize("sdk", ["js", "python"])
def test_an_rc_that_changes_the_sdk_since_its_published_version_fails(world, sdk):
    world.trees[RC][sdk] = "0b" * 20
    _fails(
        world,
        f"the RC changes sdk/{sdk} since the published v0.1.1"
        f" (sdk/{sdk}/v0.1.1 at {TAG_COMMITS[sdk][:12]})",
    )


def test_a_published_version_with_no_tag_fails(world):
    del world.api["git/ref/tags/sdk/js/v0.1.1"]
    _fails(
        world,
        "sdk/js/v0.1.1 is not a tag: the published v0.1.1 was not released by"
        " sdk-release.yml",
    )


def test_an_annotated_sdk_tag_is_followed_to_its_commit(world):
    world.api["git/ref/tags/sdk/js/v0.1.1"] = {
        "object": {"type": "tag", "sha": "c4" * 20}
    }
    world.api[f"git/tags/{'c4' * 20}"] = {
        "object": {"type": "commit", "sha": TAG_COMMITS["js"]}
    }
    code, text = world.main()
    assert code == 0, text


def test_a_release_of_the_previous_version_is_not_this_ones(world):
    world.release_run("js")["head_branch"] = "sdk/js/v0.1.0"
    _fails(world, "no sdk-release.yml run of sdk/js/v0.1.1")


def test_a_failed_release_run_before_the_good_one_needs_a_disposition(world):
    world.add_run(
        rc.SDK_RELEASE,
        _run(
            2999,
            rc.SDK_RELEASE,
            event="push",
            ref="sdk/js/v0.1.1",
            sha=TAG_COMMITS["js"],
            conclusion="failure",
            created="2026-11-01T10:30:00Z",
        ),
    )
    _fails(
        world,
        "run 2999 (sdk/js/v0.1.1, failure) did not succeed and has no disposition",
    )
    code, text = world.main("--dispositions", "2999: the trusted publisher was not set")
    assert code == 0, text


def test_two_successful_release_runs_of_one_version_fail(world):
    world.add_run(
        rc.SDK_RELEASE,
        _run(
            2998,
            rc.SDK_RELEASE,
            event="push",
            ref="sdk/js/v0.1.1",
            sha=TAG_COMMITS["js"],
            created="2026-11-01T10:30:00Z",
        ),
    )
    _fails(world, "2 sdk-release.yml runs of sdk/js/v0.1.1 succeeded")


@pytest.mark.parametrize("conclusion", ["skipped", "failure"])
def test_a_release_run_whose_publish_did_not_succeed_fails(world, conclusion):
    job = next(j for j in world.release_jobs("js") if j["name"] == "publish to npm")
    job["conclusion"] = conclusion
    _fails(world, f"run 3000: job 'publish to npm' {conclusion}")


def test_a_record_that_does_not_hash_to_its_digest_fails(world):
    """A listing line lost between the build job and the log the verifier
    read: the digest no longer matches, so the record is refused."""
    log = world.build_log("python")
    lines = log.splitlines(keepends=True)
    world.logs[f"actions/jobs/{world.build_ids['python']}/logs"] = "".join(
        line for line in lines if not line.rstrip().endswith(SDIST)
    )
    _fails(world, "run 3001: the recorded files do not hash to the recorded digest")


def test_a_log_with_no_record_fails(world):
    world.logs[f"actions/jobs/{world.build_ids['js']}/logs"] = (
        "2026-11-01T10:20:00.1234567Z npm notice Tarball Contents\n"
    )
    _fails(world, "run 3000: the build job's log records 0 files, not one tarball")


def test_a_build_job_listed_without_an_id_fails(world):
    """No id, no log, no record: refused, never compared with nothing."""
    job = next(
        j for j in world.release_jobs("js") if j["name"] == "test and build for npm"
    )
    del job["id"]
    _fails(world, "run 3000: job 'test and build for npm' has no id")


def test_an_unreadable_build_log_fails(world):
    world.build_ids["python"] = 4999  # the fake answers no log for job 4001
    _fails(world, "cannot read the log of 'test and build for PyPI'")


def test_a_registry_that_does_not_answer_fails(world):
    world.web[NPM_URL] = b"<html>not json</html>"
    _fails(world, f"cannot read {NPM_URL}")


def test_an_npm_tarball_off_the_registry_is_not_fetched(world):
    world.web[NPM_URL] = json.dumps(
        {
            "dist-tags": {"latest": JS_VERSION},
            "readme": GOOD_DESCRIPTION["npm"],
            "versions": {
                JS_VERSION: {
                    "dist": {
                        "integrity": "sha512-x",
                        "tarball": "https://example.com/x.tgz",
                    }
                }
            },
        }
    ).encode()
    _fails(
        world, f"npm's tarball of {JS_VERSION} is not on https://registry.npmjs.org/"
    )
    assert "https://example.com/x.tgz" not in world.fetched


# ---------------------------------------------------------------------------
# --registry: one package, after its publish in the sitting
# ---------------------------------------------------------------------------
def test_registry_mode_checks_one_package_and_says_so(world):
    code, text = world.main("--registry", "js")
    assert code == 0, text
    assert text.splitlines()[0] == f"# Registry check of sdk/js: {TAG} ({RC[:12]})"
    assert text.splitlines()[-1] == "REGISTRY CHECK (js): PASS"
    assert "RC EVIDENCE" not in text
    lines = text.splitlines()
    table = lines[lines.index("| Item | Subject | Result | Detail |") + 2 :]
    items = [line.split("|")[1].strip() for line in table[: table.index("")]]
    assert items == ["P", "D"]
    assert all("docs-journeys" not in " ".join(call) for call in world.calls)
    assert set(world.fetched) == {NPM_URL, TARBALL_URL}


def test_registry_mode_fails_on_its_package(world):
    world.published[WHEEL] = b"another wheel"
    code, text = world.main("--registry", "python")
    assert code == 1, text
    assert text.splitlines()[-1] == "REGISTRY CHECK (python): FAIL"


def test_registry_mode_is_never_run_in_a_workflow_run(world):
    code, _ = world.main("--registry", "js", "--this-run", "1", "--junit", ".")
    assert code == 2


def test_registry_mode_names_a_published_package_only(world):
    with pytest.raises(SystemExit) as stopped:
        world.main("--registry", "go")
    assert stopped.value.code == 2


def test_registry_mode_refuses_staging(world):
    world.with_staging()
    code, _ = world.main("--registry", "js")
    assert code == 2


# ---------------------------------------------------------------------------
# The parsers P uses
# ---------------------------------------------------------------------------
def test_the_record_is_read_from_a_log_as_github_writes_it():
    sha = hashlib.sha256(b"x").hexdigest()
    listing = f"{sha}  experimently-1.0.0.tar.gz\n"
    digest = hashlib.sha256(listing.encode()).hexdigest()
    log = (
        "﻿2026-11-01T10:20:00.0000000Z Run set -euo pipefail\n"
        f"2026-11-01T10:20:01.1234567Z {sha}  experimently-1.0.0.tar.gz\r\n"
        f"2026-11-01T10:20:01.2234567Z digest {digest}\n"
        f"not a log line {sha}  experimently-9.tar.gz\n"
    )
    files, problems = rc.read_record(rc.PACKAGES["python"], log)
    assert problems == [] and files == {"experimently-1.0.0.tar.gz": sha}


@pytest.mark.parametrize(
    "text, expected",
    [
        ("See [the guide](../../docs/sdk/go.md).", ["../../docs/sdk/go.md"]),
        ("See [Development](#development).", ["#development"]),
        ("![logo](img/logo.png)", ["img/logo.png"]),
        ("[guide]: docs/guide.md", ["docs/guide.md"]),
        ('<a href="../python">SDK</a>', ["../python"]),
        ("<a href=../python>SDK</a>", ["../python"]),
        ("<img src='img/logo.png'>", ["img/logo.png"]),
        ("See [the guide](https://example.com/docs/x.md).", []),
        ("```md\n[x](../in-a-fence.md)\n```", []),
        (
            "~~~~\n[x](../in.md)\n~~~\nstill fenced [y](../in.md)\n~~~~\n[z](../out.md)",
            ["../out.md"],
        ),
    ],
)
def test_a_relative_link_is_found_in_each_form(text, expected):
    assert rc.relative_links(text) == expected


@pytest.mark.parametrize(
    "text", ["**Not yet published.**", "**Not yet** published", "_Not yet_\npublished"]
)
def test_unpublished_is_found_in_any_markup(text):
    assert rc.says_unpublished(text)


def test_a_huge_description_is_truncated_before_it_is_read():
    """Only DESCRIPTION_MAX characters are read, whatever follows; every
    pattern on it is bounded. A planted pin past the cut is not seen."""
    head = "](x" * 20_000 + ("href=" + "a" * 3_000 + " ") * 10 + "```\n" * 1_000
    head += " " * (rc.DESCRIPTION_MAX - len(head))
    text = head + "pip install experimently==0.1.1\n"
    problems = rc.description_problems(rc.PACKAGES["python"], PY_NAME, PY_VERSION, text)
    assert "the PyPI description has no install line with a version" in problems[0]


def test_a_whole_class_target_needs_every_case_of_its_class_to_pass():
    """doc_claims.py accepts ``path::TestClass``: one pass, no failure or skip."""
    target = "test:sdk/python/tests/test_client.py::TestTrackNeverRaises"
    owner = "sdk.python.tests.test_client.TestTrackNeverRaises"
    passed = [
        rc.Case(owner, "test_a", "passed"),
        rc.Case(owner + ".TestInner", "test_b", "passed"),
        rc.Case("sdk.python.tests.test_client.TestTrackNeverRaisesNot", "t", "failed"),
    ]
    assert rc.target_problem(target, passed, "x") is None
    skipped = passed + [rc.Case(owner, "test_c", "skipped")]
    assert rc.target_problem(target, skipped, "x") == f"{target}: skipped at x"
    assert rc.target_problem(target, passed[2:], "x") == (
        f"{target}: not in the suites' JUnit at x"
    )
