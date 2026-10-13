#!/usr/bin/env python3
"""A release candidate's evidence, read back from GitHub at its tag (#1142).

    python3 scripts/rc_evidence.py --plan vX.Y.Z
    python3 scripts/rc_evidence.py vX.Y.Z --out DIR [--dispositions TEXT]
        [--staging PRIVATE_DIR]
    python3 scripts/rc_evidence.py --suite NAME --junit-dir DIR

A release is ready for production when one set of runs AT ITS TAG passed. This
script reads them; it never starts, re-runs or cancels anything. Its GitHub
calls are ``gh api`` GETs and ``gh run download``. ``--plan`` prints the
dispatch commands, in order, for a person to run. ``--suite`` is the step of
``rc-evidence.yml`` that runs one of the Python suites (``SUITES``) and writes
its JUnit reports.

WHAT IT READS

The tag must be a final release, ``vX.Y.Z``; its commit is the RC. For each
workflow in ``WORKFLOWS`` it lists EVERY run at the RC commit, whatever its
event, and counts the newest run with the event and ref the workflow is run
with at a release (``workflow_dispatch`` at the tag; ``release-gate.yml`` runs
on the push to ``main``). A workflow passes when:

* the counted run exists, concluded ``success`` and was created no earlier than
  the RC commit;
* every job of it succeeded, except a job ``ALLOWED_SKIPS`` names, which may
  only be ``skipped``; and every job ``REQUIRED_JOBS`` names is there (the 16
  SDK jobs; docs.yml's deploy, which is how the tag reached the site);
* no other run at the commit is still running; and
* every other run at the commit that did not succeed (a failure, a cancel, a
  timeout), and every earlier attempt of the counted run that did not, has a
  disposition: ``--dispositions "<run id>: <reason>; ..."``. A FAIL followed
  by a PASS is something that passed once; the reason says why that is enough.
  A disposition that names no such run is refused, so a typo cannot pass.

Then:

* E2 docs journeys. The counted docs-journeys run's ``docs-journeys-verdicts``
  and ``docs-journeys-demo-verdicts`` are at the RC; the journeys job recorded
  the published site as built from the tag and the RC (``published`` in its
  verdicts.json: docs.yml deployed the tag before the journeys read it); and
  every journey with a file under ``tests/acceptance/docs/journeys`` (a
  pending journey has none) is PASS.
* E3 Doc Examples: its run is one of the workflows above (its summary job
  holds every page's expectations).
* E4 claim tests. Each suite ``rc-evidence.yml`` ran at the tag left its JUnit
  reports, each with at least one test, and every ``test:`` target of the
  registries (``docs/_claims``), and the test of every ``mechanism:`` they use,
  is in them, passed and was not skipped. A function matches itself and its
  parametrized cases; a whole-file target needs one pass and no failure or
  skip. A target that is not Python is in none of these reports, so it fails
  until a suite that reports it is added.
* Registry. ``scripts/doc_claims.py`` of the tag's own tree finds no problem,
  ``CLAIMS_PENDING`` is empty and no unit is ``untested``. The tree must be the
  RC commit (``git rev-parse HEAD``). Deferred documents are listed, not
  counted.
* E7 staging, with ``--staging DIR``: a directory on the coordinator's machine,
  never given in the workflow. The newest Deploy run of the tag to staging
  succeeded (a Deploy run is at ``main``'s commit, so it is found by its run
  name, ``Deploy vX.Y.Z (<profile>) to staging``, not by its commit); at least
  three Synthetic check runs succeeded after it finished, and any that did not
  has a disposition; and the directory holds the read-only checks of staging:
  ``live-target-group.txt`` (what ``check_live_target_group.py --env staging``
  printed, with its ``ok: <colour> is live`` line), ``live-image-version.txt``
  (the live API image's ``org.opencontainers.image.version`` label, which must
  be the tag) and ``read-only-checks.md`` (the rest of the list, not empty).
  Nothing from that directory is copied to the output.

WHAT IT WRITES

``--out DIR``: ``rc-evidence.md`` (the run table, every check and the verdict;
also printed), ``runs.json`` (every run listed, and each counted run's jobs),
and copies of what it judged: ``verdicts/<artifact>/verdicts.json`` and
``junit/*.xml``. ``rc-evidence.yml`` uploads the directory for 90 days, so the
verdicts outlive their own 14-day artifacts. The last line is ``RC EVIDENCE:
PASS`` or ``RC EVIDENCE: FAIL``.

Exit status: 0 PASS; 1 FAIL; 2 usage (a tag that is not ``vX.Y.Z``, a
malformed disposition).

Standard library only. It runs ``gh``, ``git rev-parse HEAD`` and, for
``--suite``, pytest.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO = "getexperimently/experimently"
TAG = re.compile(r"v[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}")
SHA = re.compile(r"[0-9a-f]{40}")
DISPATCH = "workflow_dispatch"
PASS, FAIL = "PASS", "FAIL"
#: Conclusions that are not a failure: the run did not fail anything.
NOT_FAILED = ("success", "skipped", "neutral")


@dataclasses.dataclass(frozen=True)
class Workflow:
    file: str
    label: str  # the evidence item its run is
    event: str = DISPATCH  # the event of the run that counts
    at_tag: bool = True  # that run's ref: the tag, or main
    plan: str = ""  # what --plan says before its dispatch command


#: The workflows whose run at the tag is the evidence, in --plan's order.
WORKFLOWS: Tuple[Workflow, ...] = (
    Workflow(
        "docs.yml",
        "E2",
        plan="the documentation site, built and deployed from the tag;"
        " wait for its deploy job before docs-journeys.yml",
    ),
    Workflow("doc-examples.yml", "E3", plan="every page's examples, run as written"),
    Workflow("sdk-unit-tests.yml", "E1", plan="all 16 SDK suites"),
    Workflow("infrastructure-tests.yml", "E1", plan="the CDK stacks"),
    Workflow("chart-kind.yml", "E1", plan="the Helm chart on kind"),
    Workflow("compose-production.yml", "E1", plan="the production compose file"),
    Workflow(
        "docs-journeys.yml",
        "E2",
        plan="the docs journeys and the demo journeys, against the site docs.yml"
        " deployed",
    ),
    Workflow("release-gate.yml", "E1", event="push", at_tag=False),
    Workflow(
        "rc-evidence.yml",
        "E4",
        plan="when every run above has finished: the Python suites at the tag,"
        " then this verifier",
    ),
)
SELF = "rc-evidence.yml"
#: The rc-evidence.yml job that runs the verifier, and its suites jobs.
VERIFY_JOB = "RC evidence"
SUITE_JOB = "python suites ({})"
JUNIT_ARTIFACT = "rc-evidence-junit-{}"

#: The SDK jobs of sdk-unit-tests.yml (its LINUX and HEAVY lists).
SDK_JOBS: Tuple[str, ...] = (
    "js",
    "react",
    "react-native",
    "edge",
    "openfeature",
    "python",
    "openfeature-python",
    "go",
    "java",
    "ruby",
    "php",
    "dotnet",
    "elixir",
    "flutter",
    "android",
    "ios",
)
#: Jobs that may be skipped in a dispatched run, and only skipped.
ALLOWED_SKIPS: Dict[Tuple[str, str], str] = {
    ("doc-examples.yml", "Doc Examples (cache)"): "runs on a push or a schedule only",
    ("doc-examples.yml", "File an issue on failure"): "runs only after a failure",
    ("chart-kind.yml", "File an issue on failure"): "runs only after a failure",
}


@dataclasses.dataclass(frozen=True)
class Invocation:
    """One pytest session of a suite: its JUnit file, arguments and extra env."""

    report: str
    args: Tuple[str, ...]
    env: Tuple[Tuple[str, str], ...] = ()


_INTEGRATION_ENV: Tuple[Tuple[str, str], ...] = (
    ("POSTGRES_SERVER", "localhost"),
    ("POSTGRES_SCHEMA", "test_experimentation"),
    (
        "DATABASE_URI",
        "postgresql://postgres:postgres@localhost:5432/experimentation_test",
    ),
)
#: The Python suites rc-evidence.yml runs at the tag, one job each, as the
#: pull request jobs run them (pr-qa-gate.yml, integration-tests.yml), without
#: -x or a shard: the tag's whole tree.
SUITES: Dict[str, Tuple[Invocation, ...]] = {
    "unit": (
        Invocation("unit.xml", ("backend/tests/unit/", "backend/tests/realistic/")),
    ),
    "smoke": (Invocation("smoke.xml", ("backend/tests/smoke/",)),),
    "lambda": tuple(
        Invocation(f"lambda-{name}.xml", (f"backend/lambda/{name}",))
        for name in ("event_processor", "feature_flag_evaluation", "shared")
    ),
    "modules": (Invocation("modules.xml", ("modules/backend/tests",)),),
    "integration": (
        Invocation(
            "integration.xml",
            ("backend/tests/integration/",),
            _INTEGRATION_ENV
            + (
                ("EXPERIMENTLY_REQUIRE_EDITABLE", "1"),
                ("EXPERIMENTLY_REQUIRE_REDIS", "1"),
                ("EXPERIMENTLY_REQUIRE_REDIS_AUTH", "1"),
            ),
        ),
        Invocation("e2e.xml", ("backend/tests/e2e/", "-m", "e2e"), _INTEGRATION_ENV),
        Invocation(
            "contract.xml",
            ("backend/tests/contract/", "-m", "contract"),
            _INTEGRATION_ENV,
        ),
    ),
}
PYTEST = ("-p", "no:cov", "-q", "--tb=short")

#: Jobs that must be in the counted run (a job that never ran is not a pass).
REQUIRED_JOBS: Dict[str, Tuple[str, ...]] = {
    "docs.yml": ("docs build (strict)", "deploy to GitHub Pages"),
    "doc-examples.yml": (
        "Doc Examples (render)",
        "Doc Examples (plan)",
        "Doc Examples (summary)",
    ),
    "sdk-unit-tests.yml": ("Select SDKs",) + SDK_JOBS + ("SDK Unit Tests",),
    "infrastructure-tests.yml": ("CDK Stack Tests (Python)",),
    "chart-kind.yml": ("chart-kind (core)", "chart-kind (full)"),
    "compose-production.yml": ("Compose Production",),
    "docs-journeys.yml": ("Docs journeys", "Demo journeys"),
    "rc-evidence.yml": tuple(SUITE_JOB.format(name) for name in SUITES) + (VERIFY_JOB,),
}

#: The docs-journeys verdicts: the journeys job's (which records the published
#: site it read) and the demo job's.
JOURNEY_VERDICTS = "docs-journeys-verdicts"
DEMO_VERDICTS = "docs-journeys-demo-verdicts"
JOURNEYS = "tests/acceptance/docs/journeys"
CLAIMS = "docs/_claims"
LEDGER = "docs/_claims/_frozen.tsv"
TEST_TARGET = re.compile(
    r"test:(?P<path>[A-Za-z0-9_./@+-]{1,300})(?:::(?P<name>[A-Za-z0-9_:]{1,300}))?"
)
DEPLOY_TITLE = re.compile(
    r"Deploy (?P<version>v[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6})"
    r" \((?P<profile>[a-z]{1,20})\) to (?P<env>[a-z]{1,20})(?: -- [A-Z ]{1,40})?"
)
LIVE = re.compile(r"^ok: (?:blue|green) is live\b", re.MULTILINE)
VERSION = re.compile(r"v?[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}")
SYNTHETIC_GREEN = 3
#: --staging's files (read, never copied).
STAGING_FILES = (
    "live-target-group.txt",
    "live-image-version.txt",
    "read-only-checks.md",
)
DISPOSITION = re.compile(
    r"(?P<run>[0-9]{1,12})[ \t]{0,10}:[ \t]{0,10}(?P<reason>[^;]{1,200})"
)
DISPOSITIONS_MAX = 4000


class UsageError(ValueError):
    """An argument the script will not guess about."""


class EvidenceError(RuntimeError):
    """Something GitHub answered that is not what a run looks like."""


class GhError(RuntimeError):
    """A ``gh`` call failed; the message names the command, never its output."""


Gh = Callable[[List[str]], str]


def run_gh(args: List[str]) -> str:
    """``gh <args>``, its stdout."""
    try:
        done = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=300, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GhError(f"gh {' '.join(args[:2])}: {type(error).__name__}") from None
    if done.returncode != 0:
        raise GhError(f"gh {' '.join(args[:2])} failed (exit {done.returncode})")
    return done.stdout


def git_head(root: pathlib.Path) -> Optional[str]:
    """The commit checked out at *root*, or None."""
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    head = done.stdout.strip()
    return head if done.returncode == 0 and SHA.fullmatch(head) else None


class GitHub:
    """Read-only: GETs of the repository's API, and artifact downloads."""

    def __init__(self, gh: Gh, repo: str = REPO):
        self.gh = gh
        self.repo = repo

    def get(self, path: str) -> Any:
        return json.loads(self.gh(["api", f"repos/{self.repo}/{path}"]) or "null")

    def download(self, run_id: int, name: str, dest: pathlib.Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        self.gh(
            [
                "run",
                "download",
                str(run_id),
                "-R",
                self.repo,
                "-n",
                name,
                "-D",
                str(dest),
            ]
        )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def check_tag(tag: str) -> str:
    if not isinstance(tag, str) or not TAG.fullmatch(tag):
        raise UsageError(f"{tag!r} is not a final release tag vX.Y.Z")
    return tag


def parse_dispositions(text: str) -> Dict[int, str]:
    """``"<run id>: <reason>; ..."`` as {run id: reason}."""
    if len(text) > DISPOSITIONS_MAX:
        raise UsageError(f"dispositions: more than {DISPOSITIONS_MAX} characters")
    found: Dict[int, str] = {}
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        match = DISPOSITION.fullmatch(part)
        if not match or not re.search(r"[A-Za-z]", match.group("reason")):
            raise UsageError(
                f"dispositions: {part[:80]!r} is not '<run id>: <reason>' (a reason"
                " of 1-200 characters with a word in it, no ';')"
            )
        run = int(match.group("run"))
        if run in found:
            raise UsageError(f"dispositions: run {run} is given twice")
        found[run] = " ".join(match.group("reason").split())
    return found


def _time(value: Any) -> datetime.datetime:
    if not isinstance(value, str):
        raise EvidenceError(f"not a time: {value!r}")
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclasses.dataclass(frozen=True)
class Run:
    id: int
    workflow: str
    event: str
    ref: str
    sha: str
    status: str
    conclusion: str
    attempt: int
    created: datetime.datetime
    updated: datetime.datetime
    url: str
    title: str

    @classmethod
    def of(cls, workflow: str, data: Mapping[str, Any]) -> "Run":
        try:
            return cls(
                id=int(data["id"]),
                workflow=workflow,
                event=str(data["event"]),
                ref=str(data.get("head_branch") or ""),
                sha=str(data["head_sha"]),
                status=str(data["status"]),
                conclusion=str(data.get("conclusion") or ""),
                attempt=int(data.get("run_attempt") or 1),
                created=_time(data["created_at"]),
                updated=_time(data.get("updated_at") or data["created_at"]),
                url=str(data.get("html_url") or ""),
                title=str(data.get("display_title") or ""),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise EvidenceError(f"a run of {workflow} is malformed: {error}") from None

    @property
    def outcome(self) -> str:
        return self.conclusion if self.status == "completed" else self.status


def _runs(github: GitHub, path: str, workflow: str) -> List[Run]:
    data = github.get(path)
    if not isinstance(data, dict) or not isinstance(data.get("workflow_runs"), list):
        raise EvidenceError(f"{workflow}: the runs list is malformed")
    runs = [Run.of(workflow, item) for item in data["workflow_runs"]]
    if int(data.get("total_count") or 0) > len(runs):
        raise EvidenceError(f"{workflow}: more than {len(runs)} runs; none is read")
    return runs


# ---------------------------------------------------------------------------
# JUnit
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Case:
    classname: str
    name: str
    outcome: str  # passed, failed, skipped


def read_junit(path: pathlib.Path) -> List[Case]:
    cases = []
    for case in ET.parse(path).getroot().iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            outcome = "failed"
        elif case.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        cases.append(Case(case.get("classname") or "", case.get("name") or "", outcome))
    return cases


def target_problem(target: str, cases: Sequence[Case], short: str) -> Optional[str]:
    """Why *target* is not a passed, unskipped test in *cases*; None when it is."""
    match = TEST_TARGET.fullmatch(target)
    if not match:
        return f"{target}: not test:<path>[::<name>]"
    path, name = match.group("path"), match.group("name")
    if not path.endswith(".py"):
        return f"{target}: not a Python test; no suite at the RC reports it"
    module = path[: -len(".py")].replace("/", ".")
    if name is None:
        hits = [
            c
            for c in cases
            if c.classname == module or c.classname.startswith(module + ".")
        ]
    else:
        parts = name.split("::")
        owner = module + ("." + parts[0] if len(parts) == 2 else "")
        function = parts[-1]
        hits = [
            c
            for c in cases
            if c.classname == owner
            and (c.name == function or c.name.startswith(function + "["))
        ]
    if not hits:
        if any(
            c.classname == "" and c.name == module and c.outcome == "skipped"
            for c in cases
        ):
            return f"{target}: skipped at {short} (its module skipped while collected)"
        return f"{target}: not in the suites' JUnit at {short}"
    if any(c.outcome == "failed" for c in hits):
        return f"{target}: failed at {short}"
    if any(c.outcome == "skipped" for c in hits):
        return f"{target}: skipped at {short}"
    return None


# ---------------------------------------------------------------------------
# The registry at the tag
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class Registry:
    problems: List[str]
    pending: int = 0
    deferred: int = 0
    untested: int = 0
    targets: List[Tuple[str, str]] = dataclasses.field(default_factory=list)
    report: List[str] = dataclasses.field(default_factory=list)


def read_registry(root: pathlib.Path) -> Registry:
    """``scripts/doc_claims.py`` of the tree at *root*: its problems and counts,
    and every test target its registries name, with the document naming it."""
    path = root / "scripts" / "doc_claims.py"
    if not path.is_file():
        return Registry(["the tree has no scripts/doc_claims.py: no claims registry"])
    spec = importlib.util.spec_from_file_location("rc_evidence_doc_claims", path)
    if spec is None or spec.loader is None:
        return Registry([f"{path}: cannot be loaded"])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        result = module.check(root)
    finally:
        sys.modules.pop(spec.name, None)
    dispositions = result.counts.get("dispositions", {})
    untested = sum(v for k, v in dispositions.items() if str(k).startswith("untested:"))
    targets: Dict[str, str] = {}
    for registry in sorted((root / CLAIMS).rglob("*.toml")):
        try:
            data = tomllib.loads(registry.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            continue  # doc_claims.check has reported it
        document = str(data.get("document", registry.name))
        for entry in data.get("unit", []) if isinstance(data.get("unit"), list) else []:
            for target in entry.get("by", []) if isinstance(entry, dict) else []:
                if not isinstance(target, str):
                    continue
                if target.startswith("test:"):
                    targets.setdefault(target, document)
                elif target.startswith("mechanism:"):
                    mechanism = module.MECHANISMS.get(target.split(":", 1)[1])
                    if mechanism is not None:
                        targets.setdefault("test:" + mechanism.test, document)
    return Registry(
        list(result.problems),
        pending=len(module.CLAIMS_PENDING),
        deferred=len(module.CLAIMS_DEFERRED),
        untested=untested,
        targets=sorted(targets.items()),
        report=list(module.report_lines(result.counts)),
    )


# ---------------------------------------------------------------------------
# The evidence
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class Check:
    label: str
    subject: str
    problems: List[str]
    summary: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclasses.dataclass
class Row:
    workflow: str
    run: str
    url: str
    event: str
    ref: str
    sha: str
    outcome: str
    jobs: str
    attempt: int
    created: str
    role: str


@dataclasses.dataclass
class Evidence:
    tag: str
    sha: str = ""
    rows: List[Row] = dataclasses.field(default_factory=list)
    checks: List[Check] = dataclasses.field(default_factory=list)
    jobs: Dict[str, Any] = dataclasses.field(default_factory=dict)
    registry: List[str] = dataclasses.field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)


class Verifier:
    def __init__(
        self,
        tag: str,
        *,
        github: GitHub,
        root: pathlib.Path,
        out: pathlib.Path,
        dispositions: Mapping[int, str],
        junit: Optional[pathlib.Path] = None,
        this_run: Optional[int] = None,
        staging: Optional[pathlib.Path] = None,
        head_of: Callable[[pathlib.Path], Optional[str]] = git_head,
        registry: Callable[[pathlib.Path], Registry] = read_registry,
    ):
        self.tag = check_tag(tag)
        self.github = github
        self.root = root
        self.out = out
        self.dispositions = dict(dispositions)
        self.used: Set[int] = set()
        self.junit = junit
        self.this_run = this_run
        self.staging = staging
        self.head_of = head_of
        self.read_registry = registry
        self.evidence = Evidence(self.tag)
        self.counted: Dict[str, Run] = {}
        self.sha = ""
        self.short = ""
        self.commit_time: Optional[datetime.datetime] = None

    # -- the tag --------------------------------------------------------------
    def resolve(self) -> None:
        obj = self.github.get(f"git/ref/tags/{self.tag}")["object"]
        for _ in range(5):
            if obj.get("type") != "tag":
                break
            obj = self.github.get(f"git/tags/{obj['sha']}")["object"]
        if obj.get("type") != "commit" or not SHA.fullmatch(str(obj.get("sha"))):
            raise EvidenceError(f"{self.tag} does not name a commit")
        self.sha = str(obj["sha"])
        self.short = self.sha[:12]
        commit = self.github.get(f"commits/{self.sha}")
        self.commit_time = _time(commit["commit"]["committer"]["date"])
        self.evidence.sha = self.sha

    # -- runs ------------------------------------------------------------------
    def _row(
        self, run: Run, role: str, jobs: str = "", attempt: Optional[int] = None
    ) -> None:
        self.evidence.rows.append(
            Row(
                run.workflow,
                str(run.id) if attempt is None else f"{run.id} (attempt {attempt})",
                run.url,
                run.event,
                run.ref,
                run.sha[:12],
                run.outcome,
                jobs,
                run.attempt if attempt is None else attempt,
                run.created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                role,
            )
        )

    def _disposed(self, run_id: int, what: str, problems: List[str]) -> str:
        """The row's role for a run that did not succeed; a problem when it has
        no disposition."""
        reason = self.dispositions.get(run_id)
        if reason is None:
            problems.append(
                f"run {run_id} ({what}) did not succeed and has no disposition"
                f" (--dispositions '{run_id}: <reason>')"
            )
            return "did not succeed: no disposition"
        self.used.add(run_id)
        return f"disposition: {reason}"

    def _jobs(self, workflow: str, run: Run, mine: bool) -> Tuple[List[str], str]:
        data = self.github.get(f"actions/runs/{run.id}/jobs?filter=latest&per_page=100")
        items = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise EvidenceError(f"run {run.id}: the jobs list is malformed")
        if int(data.get("total_count") or 0) > len(items):
            raise EvidenceError(f"run {run.id}: more than {len(items)} jobs")
        self.evidence.jobs[str(run.id)] = [
            {k: j.get(k) for k in ("name", "status", "conclusion", "html_url")}
            for j in items
        ]
        problems: List[str] = []
        names = [str(j.get("name")) for j in items]
        judged = 0
        good = 0
        for job in items:
            name = str(job.get("name"))
            if mine and name == VERIFY_JOB:
                continue  # the job running this
            judged += 1
            status, conclusion = job.get("status"), job.get("conclusion")
            if status == "completed" and conclusion == "success":
                good += 1
            elif (
                status == "completed"
                and conclusion == "skipped"
                and (workflow, name) in ALLOWED_SKIPS
            ):
                good += 1
            else:
                problems.append(f"job {name!r} {conclusion or status}")
        for name in REQUIRED_JOBS.get(workflow, ()):
            if mine and name == VERIFY_JOB:
                continue
            if name not in names:
                problems.append(f"job {name!r} did not run")
        if not judged:
            problems.append(f"run {run.id} has no jobs")
        return problems, f"{good}/{judged}"

    def judge_workflow(self, workflow: Workflow) -> Check:
        problems: List[str] = []
        ref = self.tag if workflow.at_tag else "main"
        mine = self.this_run if workflow.file == SELF else None
        try:
            runs = _runs(
                self.github,
                f"actions/workflows/{workflow.file}/runs?head_sha={self.sha}&per_page=100",
                workflow.file,
            )
        except (GhError, EvidenceError) as error:
            return Check(
                workflow.label, workflow.file, [f"cannot list its runs: {error}"]
            )
        runs.sort(key=lambda r: (r.created, r.id))
        if mine is not None:
            counted = next((r for r in runs if r.id == mine), None)
            if counted is None:
                problems.append(
                    f"this run ({mine}) is not among the runs at {self.short}"
                )
        else:
            candidates = [r for r in runs if r.event == workflow.event and r.ref == ref]
            counted = candidates[-1] if candidates else None
        summary = ""
        if counted is None and mine is None:
            problems.append(f"no {workflow.event} run at {ref} ({self.short})")
        for run in runs:
            if run is counted:
                continue
            if run.status != "completed":
                problems.append(f"run {run.id} is still {run.status}: wait for it")
                self._row(run, "still running")
            elif run.conclusion not in NOT_FAILED:
                self._row(
                    run,
                    self._disposed(run.id, f"{run.event}, {run.conclusion}", problems),
                )
            else:
                self._row(run, "not counted")
        if counted is not None:
            self.counted[workflow.file] = counted
            if mine is None and counted.outcome != "success":
                problems.append(f"run {counted.id} concluded {counted.outcome}")
            if self.commit_time is not None and counted.created < self.commit_time:
                problems.append(f"run {counted.id} was created before the RC commit")
            if counted.event != workflow.event or counted.ref != ref:
                problems.append(
                    f"run {counted.id} is a {counted.event} at {counted.ref}, not a"
                    f" {workflow.event} at {ref}"
                )
            try:
                found, jobs = self._jobs(workflow.file, counted, mine is not None)
            except (GhError, EvidenceError) as error:
                found, jobs = (
                    [f"cannot read the jobs of run {counted.id}: {error}"],
                    "?",
                )
            problems += found
            self._row(counted, "this run" if mine is not None else "counted", jobs)
            for attempt in range(1, counted.attempt):
                try:
                    data = self.github.get(
                        f"actions/runs/{counted.id}/attempts/{attempt}"
                    )
                    earlier = Run.of(workflow.file, data)
                except (GhError, EvidenceError, TypeError) as error:
                    problems.append(
                        f"cannot read attempt {attempt} of run {counted.id}: {error}"
                    )
                    continue
                if earlier.status == "completed" and earlier.conclusion in NOT_FAILED:
                    role = "earlier attempt"
                else:
                    role = self._disposed(
                        counted.id, f"attempt {attempt}, {earlier.outcome}", problems
                    )
                self._row(earlier, role, attempt=attempt)
            summary = f"run {counted.id}, {jobs} jobs"
        return Check(workflow.label, workflow.file, problems, summary)

    # -- E2 --------------------------------------------------------------------
    def judge_journeys(self) -> Check:
        run = self.counted.get("docs-journeys.yml")
        if run is None:
            return Check("E2", "docs journeys", ["no counted docs-journeys.yml run"])
        problems: List[str] = []
        verdicts: Dict[str, Tuple[str, int]] = {}
        for artifact in (JOURNEY_VERDICTS, DEMO_VERDICTS):
            dest = self.out / "verdicts" / artifact
            try:
                self.github.download(run.id, artifact, dest)
                data = json.loads((dest / "verdicts.json").read_text(encoding="utf-8"))
                journeys = data["journeys"]
                if not isinstance(journeys, dict):
                    raise TypeError("journeys")
            except (GhError, OSError, ValueError, KeyError, TypeError) as error:
                problems.append(
                    f"{artifact} of run {run.id} cannot be read ({type(error).__name__})"
                )
                continue
            if data.get("sha") != self.sha:
                problems.append(
                    f"{artifact} is at {str(data.get('sha'))[:12]}, not the RC"
                )
            if artifact == JOURNEY_VERDICTS:
                published = (
                    data.get("published")
                    if isinstance(data.get("published"), dict)
                    else {}
                )
                ref, sha = published.get("ref") or "", published.get("sha") or ""
                if ref != self.tag or sha != self.sha:
                    problems.append(
                        f"the journeys read a site built from {ref or 'an unrecorded ref'}"
                        f" ({str(sha)[:12] or 'no commit recorded'}), not {self.tag}:"
                        " deploy the tag with docs.yml first"
                    )
            for name, entry in journeys.items():
                if name in verdicts:
                    problems.append(f"journey {name} is reported twice")
                    continue
                entry = entry if isinstance(entry, dict) else {}
                step = entry.get("step")
                verdicts[str(name)] = (
                    str(entry.get("word")),
                    step if isinstance(step, int) else 0,
                )
        expected = sorted(p.stem for p in (self.root / JOURNEYS).glob("*.yaml"))
        if not expected:
            problems.append(f"{JOURNEYS} holds no journey at this tree")
        for name in expected:
            if name not in verdicts:
                problems.append(f"journey {name} did not run")
            elif verdicts[name][0] != PASS:
                word, step = verdicts[name]
                problems.append(
                    f"journey {name} is {word}" + (f" at step {step}" if step else "")
                )
        for name in sorted(set(verdicts) - set(expected)):
            problems.append(f"journey {name} ran but has no file at {self.short}")
        passed = sum(1 for n in expected if verdicts.get(n, ("",))[0] == PASS)
        return Check("E2", "docs journeys", problems, f"{passed}/{len(expected)} PASS")

    # -- registry and E4 -------------------------------------------------------
    def judge_registry(self, registry: Registry) -> Check:
        problems: List[str] = []
        head = self.head_of(self.root)
        if head != self.sha:
            problems.append(
                f"the tree at {self.root} is {head or 'not a checkout'}, not the RC:"
                " run this from a checkout of the tag"
            )
        problems += [f"registry: {p}" for p in registry.problems[:50]]
        if len(registry.problems) > 50:
            problems.append(
                f"registry: and {len(registry.problems) - 50} more problems"
            )
        if registry.pending:
            problems.append(
                f"CLAIMS_PENDING lists {registry.pending} documents; it is empty at the RC"
            )
        if registry.untested:
            problems.append(
                f"{registry.untested} units are untested; none is at the RC"
            )
        self.evidence.registry = list(registry.report)
        return Check(
            "R",
            "claims registry",
            problems,
            f"{len(registry.targets)} test targets; {registry.deferred} documents deferred"
            " (listed, not counted)",
        )

    def judge_claim_tests(self, registry: Registry) -> Check:
        problems: List[str] = []
        junit = self.out / "junit"
        junit.mkdir(parents=True, exist_ok=True)
        if self.junit is not None:
            for report in sorted(self.junit.glob("*.xml")):
                shutil.copyfile(report, junit / report.name)
        else:
            run = self.counted.get(SELF)
            if run is None:
                return Check(
                    "E4",
                    "claim tests",
                    [
                        f"no rc-evidence.yml run at {self.tag}: the suites did not run at the RC"
                    ],
                )
            for suite in SUITES:
                try:
                    self.github.download(run.id, JUNIT_ARTIFACT.format(suite), junit)
                except GhError:
                    problems.append(
                        f"suite {suite}: no {JUNIT_ARTIFACT.format(suite)} artifact in run {run.id}"
                    )
        cases: List[Case] = []
        for suite, invocations in SUITES.items():
            for invocation in invocations:
                report = junit / invocation.report
                try:
                    found = read_junit(report)
                except (OSError, ET.ParseError):
                    problems.append(
                        f"suite {suite}: no readable JUnit report {invocation.report}"
                    )
                    continue
                if not found:
                    problems.append(f"suite {suite}: {invocation.report} holds no test")
                cases += found
        failing = [
            problem
            for target, _ in registry.targets
            if (problem := target_problem(target, cases, self.short))
        ]
        problems += failing
        return Check(
            "E4",
            "claim tests",
            problems,
            f"{len(registry.targets) - len(failing)}/{len(registry.targets)} targets passed"
            f" in {len(cases)} test results",
        )

    # -- E7 --------------------------------------------------------------------
    def judge_staging(self, staging: pathlib.Path) -> Check:
        problems: List[str] = []
        try:
            deploys = _runs(
                self.github,
                "actions/workflows/deploy.yml/runs?event=workflow_dispatch&per_page=100",
                "deploy.yml",
            )
            synthetic = _runs(
                self.github,
                "actions/workflows/synthetic.yml/runs?per_page=100",
                "synthetic.yml",
            )
        except (GhError, EvidenceError) as error:
            return Check("E7", "staging", [f"cannot list the runs: {error}"])

        def ours(run: Run) -> bool:
            match = DEPLOY_TITLE.fullmatch(run.title)
            return (
                bool(match)
                and match.group("version") == self.tag
                and match.group("env") == "staging"
            )

        mine = sorted((r for r in deploys if ours(r)), key=lambda r: (r.created, r.id))
        deploy = mine[-1] if mine else None
        for run in mine[:-1]:
            if run.status != "completed":
                problems.append(f"Deploy run {run.id} is still {run.status}")
                self._row(run, "still running")
            elif run.conclusion not in NOT_FAILED:
                self._row(
                    run, self._disposed(run.id, f"Deploy, {run.conclusion}", problems)
                )
            else:
                self._row(run, "not counted")
        green = 0
        if deploy is None:
            problems.append(f"no Deploy run of {self.tag} to staging")
        else:
            self._row(deploy, "counted")
            if deploy.outcome != "success":
                problems.append(f"Deploy run {deploy.id} concluded {deploy.outcome}")
            for run in sorted(synthetic, key=lambda r: (r.created, r.id)):
                if run.created <= deploy.updated:
                    continue
                if run.status != "completed":
                    continue  # a check still running counts for nothing
                if run.conclusion == "success":
                    green += 1
                    self._row(run, "green after the deploy")
                elif run.conclusion not in NOT_FAILED:
                    self._row(
                        run,
                        self._disposed(
                            run.id, f"Synthetic check, {run.conclusion}", problems
                        ),
                    )
            if green < SYNTHETIC_GREEN:
                problems.append(
                    f"{green} green Synthetic check runs after the deploy; at least"
                    f" {SYNTHETIC_GREEN}"
                )
        texts: Dict[str, str] = {}
        for name in STAGING_FILES:
            try:
                texts[name] = (staging / name).read_text(encoding="utf-8")[:20000]
            except (OSError, UnicodeDecodeError):
                problems.append(f"{name} is not in the staging directory")
        if "live-target-group.txt" in texts and not LIVE.search(
            texts["live-target-group.txt"]
        ):
            problems.append("live-target-group.txt has no 'ok: <colour> is live' line")
        if "live-image-version.txt" in texts:
            lines = texts["live-image-version.txt"].strip().splitlines()
            label = lines[0].strip() if lines else ""
            if not VERSION.fullmatch(label):
                problems.append("live-image-version.txt holds no version")
            elif "v" + label.lstrip("v") != self.tag:
                problems.append(f"live image v{label.lstrip('v')}, not {self.tag}")
        if "read-only-checks.md" in texts and not texts["read-only-checks.md"].strip():
            problems.append("read-only-checks.md is empty")
        summary = (
            f"Deploy run {deploy.id}, {green} green Synthetic check runs"
            if deploy
            else ""
        )
        return Check("E7", "staging", problems, summary)

    # -- all -------------------------------------------------------------------
    def run(self) -> Evidence:
        checks = self.evidence.checks
        try:
            self.resolve()
        except (GhError, EvidenceError, KeyError, TypeError, ValueError) as error:
            checks.append(Check("E1", "tag", [f"cannot resolve {self.tag}: {error}"]))
            return self.evidence
        for workflow in WORKFLOWS:
            checks.append(self.judge_workflow(workflow))
        checks.append(self.judge_journeys())
        registry = self.read_registry(self.root)
        checks.append(self.judge_registry(registry))
        checks.append(self.judge_claim_tests(registry))
        if self.staging is not None:
            checks.append(self.judge_staging(self.staging))
        unused = sorted(set(self.dispositions) - self.used)
        checks.append(
            Check(
                "D",
                "dispositions",
                [
                    f"the disposition of run {r} names no run at the RC that did not succeed"
                    for r in unused
                ],
                f"{len(self.used)} used",
            )
        )
        return self.evidence


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def _cell(text: str) -> str:
    text = " ".join(str(text).split()).replace("|", "\\|")
    return text if len(text) <= 400 else text[:397] + "..."


def report_markdown(evidence: Evidence) -> str:
    lines = [f"# RC evidence: {evidence.tag} ({evidence.sha[:12] or 'unresolved'})", ""]
    lines += [
        "| Workflow | Run | Event | Ref | Commit | Conclusion | Jobs ok | Attempt | Created | Role |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in evidence.rows:
        run = f"[{row.run}]({row.url})" if row.url.startswith("https://") else row.run
        lines.append(
            "| "
            + " | ".join(
                _cell(v)
                for v in (
                    row.workflow,
                    run,
                    row.event,
                    row.ref,
                    row.sha,
                    row.outcome,
                    row.jobs,
                    str(row.attempt),
                    row.created,
                    row.role,
                )
            )
            + " |"
        )
    lines += ["", "| Item | Subject | Result | Detail |", "|---|---|---|---|"]
    for check in evidence.checks:
        detail = "; ".join(check.problems) if check.problems else check.summary
        lines.append(
            f"| {check.label} | {_cell(check.subject)} | {PASS if check.ok else FAIL}"
            f" | {_cell(detail)} |"
        )
    if evidence.registry:
        lines += ["", "Registry at the tag:", "", "```"] + evidence.registry + ["```"]
    failed = sum(1 for c in evidence.checks if not c.ok)
    lines += ["", f"{failed} of {len(evidence.checks)} checks failed."]
    lines.append(f"RC EVIDENCE: {PASS if evidence.passed else FAIL}")
    return "\n".join(lines) + "\n"


def write_out(evidence: Evidence, out: pathlib.Path) -> str:
    text = report_markdown(evidence)
    out.mkdir(parents=True, exist_ok=True)
    (out / "rc-evidence.md").write_text(text, encoding="utf-8")
    (out / "runs.json").write_text(
        json.dumps(
            {
                "tag": evidence.tag,
                "sha": evidence.sha,
                "runs": [dataclasses.asdict(r) for r in evidence.rows],
                "jobs": evidence.jobs,
                "checks": [
                    {
                        "item": c.label,
                        "subject": c.subject,
                        "ok": c.ok,
                        "problems": c.problems,
                    }
                    for c in evidence.checks
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return text


def plan_lines(tag: str) -> List[str]:
    tag = check_tag(tag)
    lines = [
        f"# The runs rc_evidence.py reads for {tag}. Run them yourself, in this order,"
        " each at the tag; this script runs none of them.",
        "# release-gate.yml ran on the push to main that the release commit made.",
    ]
    dispatched = [w for w in WORKFLOWS if w.at_tag]
    for number, workflow in zip(range(1, len(dispatched) + 1), dispatched):
        lines.append(f"# {number}. {workflow.plan}")
        command = f"gh workflow run {workflow.file} --ref {tag} -R {REPO}"
        if workflow.file == SELF:
            command += (
                "  # add -f dispositions='<run id>: <reason>; ...' for any run at"
                " the commit that did not succeed"
            )
        lines.append(command)
    lines.append(
        "# After the staging deploy and three green Synthetic check runs, on your"
        " machine, from a checkout of the tag:"
    )
    lines.append(
        f"python3 scripts/rc_evidence.py {tag} --staging <private dir> --out <dir>"
    )
    return lines


def run_suite(
    name: str,
    junit_dir: pathlib.Path,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> int:
    """Run every session of suite *name*; 0 only when all of them passed."""
    junit_dir.mkdir(parents=True, exist_ok=True)
    failed = []
    for invocation in SUITES[name]:
        env = dict(os.environ)
        env.update(dict(invocation.env))
        command = [
            sys.executable,
            "-m",
            "pytest",
            *invocation.args,
            *PYTEST,
            f"--junitxml={junit_dir / invocation.report}",
        ]
        print(f"rc-evidence: suite {name}: {' '.join(command[1:])}", flush=True)
        done = runner(command, cwd=str(ROOT), env=env, check=False)
        if done.returncode != 0:
            failed.append(f"{invocation.report} (exit {done.returncode})")
    for item in failed:
        print(f"rc-evidence: suite {name}: {item}", flush=True)
    return 1 if failed else 0


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    gh: Gh = run_gh,
    head_of: Callable[[pathlib.Path], Optional[str]] = git_head,
    registry: Callable[[pathlib.Path], Registry] = read_registry,
) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("tag", nargs="?", help="the release tag, vX.Y.Z")
    parser.add_argument("--plan", metavar="TAG", help="print the dispatch commands")
    parser.add_argument("--out", type=pathlib.Path, help="where the record is written")
    parser.add_argument(
        "--staging", type=pathlib.Path, help="the private staging checks"
    )
    parser.add_argument("--dispositions", default="", help="'<run id>: <reason>; ...'")
    parser.add_argument(
        "--root", type=pathlib.Path, default=ROOT, help="the tag's tree"
    )
    parser.add_argument(
        "--junit", type=pathlib.Path, help="rc-evidence.yml: its reports"
    )
    parser.add_argument("--this-run", type=int, help="rc-evidence.yml: its own run id")
    parser.add_argument("--suite", choices=sorted(SUITES), help="run one suite")
    parser.add_argument(
        "--junit-dir", type=pathlib.Path, help="--suite: where it writes"
    )
    args = parser.parse_args(argv)
    try:
        if args.suite:
            if args.junit_dir is None or args.tag or args.plan:
                raise UsageError("--suite NAME --junit-dir DIR, and nothing else")
            return run_suite(args.suite, args.junit_dir)
        if args.plan:
            if args.tag:
                raise UsageError("--plan TAG, or TAG --out DIR: not both")
            print("\n".join(plan_lines(args.plan)))
            return 0
        if not args.tag or args.out is None:
            raise UsageError("give TAG --out DIR (or --plan TAG)")
        if (args.junit is None) != (args.this_run is None):
            raise UsageError("--junit and --this-run go together (rc-evidence.yml)")
        if args.staging is not None and args.this_run is not None:
            raise UsageError(
                "--staging is read on the coordinator's machine, never in a run"
            )
        if args.staging is not None and not args.staging.is_dir():
            raise UsageError(f"--staging {args.staging}: not a directory")
        verifier = Verifier(
            args.tag,
            github=GitHub(gh),
            root=args.root,
            out=args.out,
            dispositions=parse_dispositions(args.dispositions),
            junit=args.junit,
            this_run=args.this_run,
            staging=args.staging,
            head_of=head_of,
            registry=registry,
        )
    except UsageError as error:
        print(f"rc_evidence.py: {error}", file=sys.stderr)
        return 2
    evidence = verifier.run()
    sys.stdout.write(write_out(evidence, args.out))
    return 0 if evidence.passed else 1


if __name__ == "__main__":
    sys.exit(main())
