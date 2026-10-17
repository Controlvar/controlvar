#!/usr/bin/env python3
"""A release candidate's evidence, read back from GitHub at its tag (#1142).

    python3 scripts/rc_evidence.py --plan vX.Y.Z
    python3 scripts/rc_evidence.py vX.Y.Z --out DIR [--dispositions TEXT]
        [--staging PRIVATE_DIR]
    python3 scripts/rc_evidence.py vX.Y.Z --registry js|python --out DIR
        [--dispositions TEXT]
    python3 scripts/rc_evidence.py --suite NAME --junit-dir DIR

A release is ready for production when one set of runs AT ITS TAG passed. This
script reads them; it never starts, re-runs or cancels anything. Its GitHub
calls are ``gh api`` GETs and ``gh run download``; its other calls are GETs of
the two package registries (``http_get``). ``--plan`` prints the dispatch
commands, in order, for a person to run. ``--suite`` is the step of
``rc-evidence.yml`` that runs one of the suites (``SUITES``) and writes its
JUnit reports. ``--registry`` checks one published package only (P below):
the step after each publish in the release sitting.

WHAT IT READS

The tag must be a final release, ``vX.Y.Z``, and no branch may have its name;
its commit (an annotated tag is followed to it) is the RC. For each workflow in
``WORKFLOWS`` it lists EVERY run at the RC commit, whatever its event, every
page of the list (``MAX_PAGES``), and counts the newest run with the event and
ref the workflow is run with at a release (``workflow_dispatch`` at the tag;
``release-gate.yml`` runs on the push to ``main``). A run the list returns at
another commit is refused. A workflow passes when:

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
* E4 claim tests. Each suite ``rc-evidence.yml`` ran at the tag left its
  JUnit reports, each with at least one test: the Python suites, the chart's
  render tests (with the helm chart.yml pins), the CDK stack tests, the two
  published SDKs' own suites (``sdk/python``'s pytest,
  ``sdk/js``'s jest), the SDK contract self-tests, the demo apps' jest and their
  simulators' tests, and the frontend's jest. Every target must be in a tree
  some suite runs (``suite_of``); one in no suite's tree fails by name. Every
  ``test:`` target of the registries (``docs/_claims``), and the test of every
  ``mechanism:`` they use, is in them, passed and was not skipped. A Python
  function matches itself and its parametrized cases; a jest target's name
  matches the tests of its file whose full name or own title is exactly it
  (a ``test.each`` title as written, ``%s`` and the like, matches every test
  that fills it in); a
  whole-file or whole-class target needs one pass and no failure or skip. Any
  failed or errored case in any report fails E4, whether or not a target names
  it. A
  target no suite reports fails until one does, except a target in another
  SDK's tree (``sdk/<name>``, one of ``SDK_JOBS`` but the two published ones):
  no suite here runs those test by test, so they are held at job level, by
  that SDK's job in the counted sdk-unit-tests.yml run, and the report names
  them as the residual.
* P published packages (``PACKAGES``: npm's ``@getexperimently/js-sdk`` and
  PyPI's ``experimently``). The manifest at the RC gives the name and version.
  The tag ``sdk/<sdk>/v<version>`` exists, and the RC's ``sdk/<sdk>`` tree is
  the tag's (the same git tree id: ``git diff --quiet <tag> <rc> --
  sdk/<sdk>``), so the published package was built from what the RC tests;
  a release that publishes no SDK passes this as long as the SDK is unchanged.
  One sdk-release.yml run of that tag, at the tag's commit, succeeded (any
  other run of the tag that did not succeed needs a disposition), with its
  resolve, test-and-build and publish jobs green. Its build job's log
  holds the record that job wrote before uploading: for PyPI each file's
  sha256 and the digest of that listing (which must hash back to it, so the
  record was read whole), for npm the tarball's sha256. The registry's latest
  version is the manifest's; each PyPI file's sha256 is the recorded one, file
  for file; the npm tarball the registry serves has the recorded sha256 and
  its ``dist.integrity`` is that tarball's. The description the registry shows
  (PyPI's, npm's README) quotes the manifest version in its install line
  (``pip install experimently==X.Y.Z``, ``npm install
  @getexperimently/js-sdk@X.Y.Z``), links only to absolute URLs and does not
  say it is not yet published.
* Registry. ``scripts/doc_claims.py`` of the tag's own tree finds no problem,
  ``CLAIMS_PENDING`` is empty and no unit is ``untested``. The tree must be the
  RC commit (``git rev-parse HEAD``). Deferred documents are listed, not
  counted.
* E7 staging, with ``--staging DIR``: a directory on the coordinator's machine,
  never given in the workflow. Deploy and Synthetic check runs are listed from
  the RC commit's time on. The newest Deploy run of the tag to staging
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

With ``--registry js|python`` it resolves the tag and runs only that P check
(and the dispositions); its last line is ``REGISTRY CHECK (<sdk>): PASS`` or
``FAIL``, never the RC's verdict.

THE REPOSITORY

The repository read is ``GITHUB_REPOSITORY`` in a run; on a person's machine
it is the name GitHub gives the repository's id (``REPOSITORY_ID``) now, so a
renamed or transferred repository needs no edit here. ``--plan``'s commands
look the name up the same way.

Standard library only. It runs ``gh``, ``git rev-parse HEAD`` and, for
``--suite``, pytest or jest.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime
import hashlib
import http.client
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

ROOT = pathlib.Path(__file__).resolve().parents[1]
#: This repository's id. A rename or a transfer keeps it; the name changes.
REPOSITORY_ID = "1367480368"
#: ``owner/name``, as ``GITHUB_REPOSITORY`` and the API's ``full_name`` give it.
REPOSITORY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
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
        plan="when every run above has finished: the suites at the tag (pytest"
        " and jest), then this verifier",
    ),
)
SELF = "rc-evidence.yml"
#: The rc-evidence.yml job that runs the verifier, and its suites jobs: the
#: python-suites matrix and the jest-suites matrix.
VERIFY_JOB = "RC evidence"
SUITE_JOB = "python suites ({})"
JEST_SUITE_JOB = "jest suites ({})"
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
    """One session of a suite: its JUnit file, arguments, extra env and runner:
    ``pytest`` from the repository root, or ``jest`` in the npm project ``cwd``
    (``npm ci`` has run there), whose own ``--json`` report is written out as
    JUnit (``jest_junit``)."""

    report: str
    args: Tuple[str, ...]
    env: Tuple[Tuple[str, str], ...] = ()
    runner: str = "pytest"
    cwd: str = ""
    #: jest: trees outside the project its config also searches (``roots``).
    also: Tuple[str, ...] = ()

    @property
    def roots(self) -> Tuple[str, ...]:
        """The trees this session runs: pytest's path arguments, or jest's
        project and the trees its config adds."""
        if self.runner == "jest":
            return (self.cwd, *self.also)
        return tuple(
            a.rstrip("/") for a in self.args if "/" in a and not a.startswith("-")
        )

    @property
    def ignored(self) -> Tuple[str, ...]:
        """The trees under its roots this session leaves out: pytest's
        ``--ignore=<tree>``."""
        return tuple(
            a[len("--ignore=") :].rstrip("/")
            for a in self.args
            if a.startswith("--ignore=")
        )

    def runs(self, path: str) -> bool:
        """Whether the file *path* is in a tree this session runs."""
        return any(path.startswith(root + "/") for root in self.roots) and not any(
            path.startswith(tree + "/") for tree in self.ignored
        )


_INTEGRATION_ENV: Tuple[Tuple[str, str], ...] = (
    ("POSTGRES_SERVER", "localhost"),
    ("POSTGRES_SCHEMA", "test_experimentation"),
    (
        "DATABASE_URI",
        "postgresql://postgres:postgres@localhost:5432/experimentation_test",
    ),
)
#: The suites rc-evidence.yml runs at the tag, one job each, as the pull
#: request jobs run them (pr-qa-gate.yml, integration-tests.yml, chart.yml,
#: sdk-unit-tests.yml), without -x, a shard or --passWithNoTests: the tag's
#: whole tree. The pytest ones are the python-suites matrix, the jest ones the
#: jest-suites matrix. sdk/python has its own rootdir (the pyproject.toml
#: beside its tests), so ``--rootdir=.`` names its cases from the repository
#: root, as every other report does; the two demo simulators each import a
#: module named ``traffic`` and are a session each.
SUITES: Dict[str, Tuple[Invocation, ...]] = {
    # The load-testing framework's tests (docs/performance/load-testing.md's
    # registry names them) are a session of their own: collecting
    # test_load_profiles.py imports locust, whose gevent monkey-patching would
    # otherwise apply to every test in unit.xml.
    # The chart's render tests are not in unit.xml: they skip there, as in the
    # Unit Tests job, and a skipped case of a target fails it whatever another
    # report says. They are the chart suite, below.
    "unit": (
        Invocation(
            "unit.xml",
            (
                "backend/tests/unit/",
                "backend/tests/realistic/",
                "--ignore=backend/tests/unit/chart",
            ),
        ),
        Invocation("performance.xml", ("backend/tests/performance/",)),
    ),
    # As chart.yml's chart job runs them: EXPERIMENTLY_CHART_TESTS=1 and the
    # helm it pins ($HELM, which rc-evidence.yml's chart leg installs);
    # without either every case skips.
    "chart": (
        Invocation(
            "chart.xml",
            ("backend/tests/unit/chart",),
            (("EXPERIMENTLY_CHART_TESTS", "1"),),
        ),
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
    # As infrastructure-tests.yml's CDK Stack Tests: importlib, so the local
    # infrastructure/constructs does not shadow the `constructs` package.
    "infrastructure": (
        Invocation(
            "infrastructure.xml",
            ("infrastructure/tests", "-o", "addopts=", "--import-mode=importlib"),
        ),
    ),
    "sdk-python": (
        Invocation(
            "sdk-python.xml",
            ("sdk/python/tests", "--rootdir=.", "-o", "addopts="),
        ),
    ),
    # As pr-qa-gate.yml's SDK Contract Tests run it (pytest only installed).
    "sdk-contract": (
        Invocation("sdk-contract.xml", ("tests/sdk-contract", "-o", "addopts=")),
    ),
    "demo-simulators": tuple(
        Invocation(f"demo-{name}-simulator.xml", (f"demo/{name}/simulator",))
        for name in ("shoplab", "streampulse")
    ),
    # frontend/jest.config.js adds modules/frontend/src (MODULES_DIR) to its
    # roots in a full tree: the modules' own jest tests run in this session.
    "frontend": (
        Invocation(
            "frontend.xml",
            (),
            runner="jest",
            cwd="frontend",
            also=("modules/frontend/src",),
        ),
    ),
    "sdk-js": (Invocation("sdk-js.xml", ("--ci",), runner="jest", cwd="sdk/js"),),
    "demo-shoplab": (
        Invocation("demo-shoplab.xml", (), runner="jest", cwd="demo/shoplab"),
    ),
    "demo-streampulse": (
        Invocation("demo-streampulse.xml", (), runner="jest", cwd="demo/streampulse"),
    ),
}
PYTEST = ("-p", "no:cov", "-q", "--tb=short")


def is_jest(name: str) -> bool:
    """Whether suite *name* is jest's (the jest-suites matrix)."""
    return all(i.runner == "jest" for i in SUITES[name])


def suite_job(name: str) -> str:
    """The rc-evidence.yml job that runs suite *name*."""
    return (JEST_SUITE_JOB if is_jest(name) else SUITE_JOB).format(name)


def session_of(target: str) -> Optional[Tuple[str, Invocation]]:
    """The suite and session that run *target*'s file: a path under one of the
    session's roots and not under a tree it ignores. None when no session in
    SUITES runs it."""
    match = TEST_TARGET.fullmatch(target)
    if not match:
        return None
    path = match.group("path")
    for name, invocations in SUITES.items():
        for invocation in invocations:
            if invocation.runs(path):
                return name, invocation
    return None


def suite_of(target: str) -> Optional[str]:
    """The suite whose sessions run *target*'s file (``session_of``). None
    when no suite in SUITES runs it."""
    found = session_of(target)
    return found[0] if found else None


#: The npm projects whose jest the jest suites run; a jest target is a test
#: file in one of them.
JEST_ROOTS: Tuple[str, ...] = tuple(
    root
    for invocations in SUITES.values()
    for i in invocations
    if i.runner == "jest"
    for root in i.roots
)


@dataclasses.dataclass(frozen=True)
class Package:
    """A published SDK: its tree, its registry, where its version is declared,
    its sdk-release.yml jobs and the suite that runs its tests at the RC."""

    sdk: str  # its directory under sdk/, and its tag's: sdk/<sdk>/vX.Y.Z
    registry: str
    manifest: str
    build_job: str
    publish_job: str
    suite: str


#: The SDKs on a registry, npm first (the release sitting's order).
PACKAGES: Dict[str, Package] = {
    "js": Package(
        "js",
        "npm",
        "sdk/js/package.json",
        "test and build for npm",
        "publish to npm",
        "sdk-js",
    ),
    "python": Package(
        "python",
        "PyPI",
        "sdk/python/pyproject.toml",
        "test and build for PyPI",
        "publish to PyPI",
        "sdk-python",
    ),
}
SDK_RELEASE = "sdk-release.yml"
RESOLVE_JOB = "resolve the tag"
#: The events an sdk-release.yml run that publishes has: the tag's push, or a
#: dispatch at the tag (``--ref sdk/<sdk>/vX.Y.Z``).
RELEASE_EVENTS = ("push", DISPATCH)
PYPI_JSON = "https://pypi.org/pypi/{name}/json"
NPM_DOCUMENT = "https://registry.npmjs.org/{name}"
NPM_TARBALLS = "https://registry.npmjs.org/"
#: The only URLs http_get reads.
REGISTRY_URLS = ("https://pypi.org/pypi/", NPM_TARBALLS)
FETCH_MAX = 64 * 1024 * 1024
#: How much of a registry description is read (it is truncated before any
#: pattern runs).
DESCRIPTION_MAX = 200_000


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
    "rc-evidence.yml": tuple(suite_job(name) for name in SUITES) + (VERIFY_JOB,),
}

#: The docs-journeys verdicts: the journeys job's (which records the published
#: site it read) and the demo job's.
JOURNEY_VERDICTS = "docs-journeys-verdicts"
DEMO_VERDICTS = "docs-journeys-demo-verdicts"
JOURNEYS = "tests/acceptance/docs/journeys"
CLAIMS = "docs/_claims"
LEDGER = "docs/_claims/_frozen.tsv"
TEST_TARGET = re.compile(
    r"test:(?P<path>[A-Za-z0-9_./@+-]{1,300})(?:::(?P<name>[^\n]{1,300}))?"
)
PYTHON_TARGET = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]{0,200}(?:::[A-Za-z_][A-Za-z0-9_]{0,200})?"
)
#: A jest test file, below one of JEST_ROOTS.
JEST_FILE = re.compile(r"[^\n]{1,300}\.(?:test|spec)\.[cm]?[jt]sx?")
#: How many pages of 100 runs a list is read to; beyond that it is refused.
MAX_PAGES = 10
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


class FetchError(RuntimeError):
    """A registry GET failed; the message names the URL and the error's kind."""


Gh = Callable[[List[str]], str]
Fetch = Callable[[str], bytes]


def http_get(url: str) -> bytes:
    """A GET of a registry URL (``REGISTRY_URLS``), at most ``FETCH_MAX`` bytes."""
    if not url.startswith(REGISTRY_URLS):
        raise FetchError(f"not a registry URL: {url[:200]}")
    request = urllib.request.Request(
        url, headers={"User-Agent": "controlvar-rc-evidence"}, method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = response.read(FETCH_MAX + 1)
    except (OSError, ValueError, http.client.HTTPException) as error:
        code = getattr(error, "code", None)
        kind = type(error).__name__ + (f" {code}" if code else "")
        raise FetchError(f"GET {url[:200]}: {kind}") from None
    if len(data) > FETCH_MAX:
        raise FetchError(f"GET {url[:200]}: more than {FETCH_MAX} bytes")
    return data


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


def repository(environ: Mapping[str, str], gh: Gh) -> str:
    """``GITHUB_REPOSITORY`` in a run, else the current name of REPOSITORY_ID."""
    name = environ.get("GITHUB_REPOSITORY", "")
    if not name:
        try:
            answer = json.loads(gh(["api", f"repositories/{REPOSITORY_ID}"]) or "null")
        except (GhError, ValueError) as error:
            raise UsageError(f"repository {REPOSITORY_ID}: {error}") from None
        name = answer.get("full_name", "") if isinstance(answer, dict) else ""
    if not isinstance(name, str) or REPOSITORY_NAME.fullmatch(name) is None:
        raise UsageError(f"repository {REPOSITORY_ID}: no owner/name for it")
    return name


class GitHub:
    """Read-only: GETs of the repository's API, and artifact downloads."""

    def __init__(self, gh: Gh, repo: str):
        self.gh = gh
        self.repo = repo

    def get(self, path: str) -> Any:
        return json.loads(self.gh(["api", f"repos/{self.repo}/{path}"]) or "null")

    def text(self, path: str) -> str:
        """A GET whose answer is text, not JSON: a job's log."""
        return self.gh(["api", f"repos/{self.repo}/{path}"])

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
    """Every run the list at *path* holds, page by page (100 a page), at most
    ``MAX_PAGES`` pages; a longer list is refused rather than read in part."""
    runs: Dict[int, Run] = {}
    total = 0
    for page in range(1, MAX_PAGES + 1):
        data = github.get(f"{path}&page={page}")
        if not isinstance(data, dict) or not isinstance(
            data.get("workflow_runs"), list
        ):
            raise EvidenceError(f"{workflow}: the runs list is malformed")
        total = int(data.get("total_count") or 0)
        for item in data["workflow_runs"]:
            run = Run.of(workflow, item)
            runs[run.id] = run
        if len(runs) >= total or not data["workflow_runs"]:
            break
    if total > len(runs):
        raise EvidenceError(f"{workflow}: more than {len(runs)} runs; none is read")
    return list(runs.values())


def since(moment: datetime.datetime) -> str:
    """The query term for runs created at or after *moment*."""
    stamp = moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return "created=" + urllib.parse.quote(">=" + stamp, safe="")


# ---------------------------------------------------------------------------
# JUnit
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Case:
    classname: str
    name: str
    outcome: str  # passed, failed, skipped
    title: str = ""  # jest: the test's own title (``name`` is its full name)


def read_junit(path: pathlib.Path) -> List[Case]:
    cases = []
    for case in ET.parse(path).getroot().iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            outcome = "failed"
        elif case.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        cases.append(
            Case(
                case.get("classname") or "",
                case.get("name") or "",
                outcome,
                case.get("title") or "",
            )
        )
    return cases


def target_problem(target: str, cases: Sequence[Case], short: str) -> Optional[str]:
    """Why *target* is not a passed, unskipped test in *cases*; None when it is."""
    match = TEST_TARGET.fullmatch(target)
    if not match:
        return f"{target}: not test:<path>[::<name>]"
    path, name = match.group("path"), match.group("name")
    if any(
        path.startswith(root + "/") and JEST_FILE.fullmatch(path[len(root) + 1 :])
        for root in JEST_ROOTS
    ):
        hits = [c for c in cases if c.classname == path]
        if name is not None:
            # Exactly as the registry writes it: the test's full name (its
            # describe titles and its own) or its own title. A sibling whose
            # name only contains it does not stand in for it. A ``.each``
            # title as written (``%s``) is every test that fills it in, like
            # a parametrized Python function.
            pattern = jest_title_pattern(name)
            hits = [
                c
                for c in hits
                if pattern.fullmatch(c.title) or pattern.fullmatch(c.name)
            ]
        return _outcome(target, hits, short)
    if not path.endswith(".py"):
        return (
            f"{target}: no suite at the RC reports this test (pytest does, and"
            f" jest in {', '.join(JEST_ROOTS)})"
        )
    if name is not None and not PYTHON_TARGET.fullmatch(name):
        return f"{target}: not a function or Class::method name"
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
        if len(parts) == 1:
            # A Test class on its own (doc_claims.py accepts one): every case
            # of it, which like a whole file needs one pass and no failure.
            whole = f"{module}.{name}"
            hits += [
                c
                for c in cases
                if c.classname == whole or c.classname.startswith(whole + ".")
            ]
    if not hits:
        if any(
            c.classname == "" and c.name == module and c.outcome == "skipped"
            for c in cases
        ):
            return f"{target}: skipped at {short} (its module skipped while collected)"
    return _outcome(target, hits, short)


#: jest's printf placeholders, which ``test.each`` fills in.
JEST_PLACEHOLDER = re.compile(r"%[psdifjo#$%]")


def jest_title_pattern(name: str) -> "re.Pattern[str]":
    """*name* exactly, except that each ``.each`` placeholder (``%s``, ``%d``,
    ``%p`` ...) stands for the value jest put there; ``%%`` is a ``%``."""
    parts: List[str] = []
    end = 0
    for found in JEST_PLACEHOLDER.finditer(name):
        parts.append(re.escape(name[end : found.start()]))
        parts.append("%" if found.group() == "%%" else r"[^\n]{1,500}?")
        end = found.end()
    parts.append(re.escape(name[end:]))
    return re.compile("".join(parts))


def sdk_job_of(target: str) -> Optional[str]:
    """The sdk-unit-tests.yml job that holds *target* at job level: its SDK's,
    when the target is in an SDK tree no suite here runs test by test; None
    for any other target."""
    match = TEST_TARGET.fullmatch(target)
    if not match:
        return None
    parts = match.group("path").split("/")
    if len(parts) > 2 and parts[0] == "sdk" and parts[1] in SDK_JOBS:
        return None if parts[1] in PACKAGES else parts[1]
    return None


def _outcome(target: str, hits: Sequence[Case], short: str) -> Optional[str]:
    if not hits:
        return f"{target}: not in the suites' JUnit at {short}"
    if any(c.outcome == "failed" for c in hits):
        return f"{target}: failed at {short}"
    if any(c.outcome == "skipped" for c in hits):
        return f"{target}: skipped at {short}"
    return None


def jest_junit(
    report: pathlib.Path, out: pathlib.Path, root: pathlib.Path = ROOT
) -> int:
    """Jest's ``--json`` report written as JUnit; the number of test cases.

    One case per test: its classname the test file's repository path, its name
    the test's full name (the describe titles and its own), and a ``title``
    attribute, its own title. A failed test has a failure; one that is
    pending, todo, skipped or disabled is skipped; a file that did not run is
    one failed case.
    """
    data = json.loads(report.read_text(encoding="utf-8"))
    suite = ET.Element("testsuite", name="jest")
    count = 0
    for result in data["testResults"]:
        file = pathlib.Path(str(result["name"]))
        try:
            path = file.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            path = file.as_posix()
        tests = result.get("assertionResults") or []
        if result.get("status") == "failed" and not tests:
            case = ET.SubElement(
                suite, "testcase", classname=path, name="(the file did not run)"
            )
            ET.SubElement(case, "failure", message="the test file did not run")
            count += 1
        for test in tests:
            name = str(test.get("fullName") or test.get("title") or "")[:2000]
            title = str(test.get("title") or "")[:2000]
            case = ET.SubElement(
                suite, "testcase", classname=path, name=name, title=title
            )
            status = test.get("status")
            if status == "failed":
                ET.SubElement(case, "failure", message="failed")
            elif status != "passed":
                ET.SubElement(case, "skipped", message=str(status)[:40])
            count += 1
    top = ET.Element("testsuites")
    top.append(suite)
    ET.ElementTree(top).write(out, encoding="utf-8", xml_declaration=True)
    return count


# ---------------------------------------------------------------------------
# The published packages: sdk-release.yml's record, and the registry's text
# ---------------------------------------------------------------------------
#: A line of a job's log: its timestamp, then what the step printed.
LOG_LINE = re.compile(
    r"\ufeff?[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z"
    r" (?P<text>[^\n]{0,1000})"
)
#: What sdk-release.yml's build jobs print as their record: ``<sha256>  <file>``
#: for each file, and the PyPI job's ``digest <sha256 of those lines>``.
RECORDED_FILE = re.compile(
    r"(?P<sha>[0-9a-f]{64})  (?P<file>[A-Za-z0-9][A-Za-z0-9._+-]{0,200})"
)
RECORDED_DIGEST = re.compile(r"digest (?P<sha>[0-9a-f]{64})")
PYPI_FILE = re.compile(r"[A-Za-z0-9._+-]{1,200}\.(?:whl|tar\.gz)")
NPM_FILE = re.compile(r"[A-Za-z0-9._+-]{1,200}\.tgz")


def read_record(package: Package, log: str) -> Tuple[Dict[str, str], List[str]]:
    """The files sdk-release.yml's build job recorded, {file: sha256}, from its
    log, and every reason the record is not one."""
    files: List[Tuple[str, str]] = []
    digests: List[str] = []
    for line in log.splitlines():
        stamped = LOG_LINE.fullmatch(line.rstrip("\r"))
        if not stamped:
            continue
        text = stamped.group("text").rstrip()
        if found := RECORDED_FILE.fullmatch(text):
            files.append((found.group("file"), found.group("sha")))
        elif found := RECORDED_DIGEST.fullmatch(text):
            digests.append(found.group("sha"))
    problems: List[str] = []
    names = [name for name, _ in files]
    if len(set(names)) != len(names):
        problems.append("the record names a file twice")
    if package.registry == "PyPI":
        if not files:
            problems.append("the build job's log records no file")
        for name in names:
            if not PYPI_FILE.fullmatch(name):
                problems.append(f"the record names {name}, not a wheel or an sdist")
        if len(digests) != 1:
            problems.append(
                f"the build job's log records {len(digests)} digests, not one"
            )
        else:
            listing = "".join(f"{sha}  {name}\n" for name, sha in files)
            if hashlib.sha256(listing.encode()).hexdigest() != digests[0]:
                problems.append(
                    "the recorded files do not hash to the recorded digest: the"
                    " record was not read whole"
                )
    elif len(files) != 1 or not NPM_FILE.fullmatch(files[0][0]):
        problems.append(
            f"the build job's log records {len(files)} files, not one tarball"
        )
    return dict(files), problems


FENCE_LINE = re.compile(r" {0,3}(`{3,}|~{3,})")
FENCE_CLOSE = re.compile(r" {0,3}(`{3,}|~{3,})[ \t]*")
LINK_TARGETS = (
    re.compile(r"\]\([ \t]{0,10}<?([^()\s<>]{1,2000})"),  # [text](target), ![alt](src)
    re.compile(r"^ {0,3}\[[^\]\n]{1,500}\]:[ \t]{0,10}<?([^\s<>]{1,2000})", re.M),
    re.compile(
        r"""\b(?:href|src)[ \t]{0,10}=[ \t]{0,10}"""
        r"""(?:"([^"\n]{0,2000})"|'([^'\n]{0,2000})'|([^\s"'>]{1,2000}))"""
    ),  # raw HTML
)
ABSOLUTE = re.compile(r"(?:https?|mailto):", re.I)


def without_fences(text: str) -> str:
    """*text* without its fenced code blocks (a link in one is not a link)."""
    kept: List[str] = []
    fence: Optional[str] = None
    for line in text.splitlines():
        if fence is None:
            opened = FENCE_LINE.match(line)
            if opened:
                fence = opened.group(1)
            else:
                kept.append(line)
            continue
        closed = FENCE_CLOSE.fullmatch(line)
        if (
            closed
            and closed.group(1)[0] == fence[0]
            and len(closed.group(1)) >= len(fence)
        ):
            fence = None
    return "\n".join(kept)


def relative_links(text: str) -> List[str]:
    """Every link target outside a fence that is not an absolute URL: a
    registry shows the description away from this repository."""
    text = without_fences(text)
    targets = []
    for pattern in LINK_TARGETS:
        for found in pattern.findall(text):
            target = (
                found if isinstance(found, str) else next((p for p in found if p), "")
            )
            if not ABSOLUTE.match(target):
                targets.append(target)
    return targets


def says_unpublished(text: str) -> bool:
    """ "Not yet published" in any markup or wrapping."""
    return "not yet published" in " ".join(re.sub(r"[*_`]", "", text).split()).lower()


def installed_versions(package: Package, name: str, text: str) -> List[str]:
    """The versions the description's install lines pin *name* to."""
    if package.registry == "PyPI":
        pattern = (
            r"pip3? install (?:-{1,2}[A-Za-z-]{1,20} ){0,3}[\"']?"
            + re.escape(name)
            + r"==([^\s\"'`;,)]{1,40})"
        )
    else:
        pattern = (
            r"(?:npm (?:install|i|add)|yarn add|pnpm add)"
            r" (?:-{1,2}[A-Za-z-]{1,20} ){0,3}"
            + re.escape(name)
            + r"@([^\s\"'`;,)]{1,40})"
        )
    return re.findall(pattern, text)


def description_problems(
    package: Package, name: str, version: str, text: Any
) -> List[str]:
    """Why the description the registry shows for *name* is not the one the
    manifest at the RC publishes."""
    where = f"the {package.registry} description"
    if not isinstance(text, str) or not text.strip():
        return [f"{where} is empty"]
    text = text[:DESCRIPTION_MAX]
    problems = []
    pinned = installed_versions(package, name, text)
    example = (
        f"pip install {name}=={version}"
        if package.registry == "PyPI"
        else f"npm install {name}@{version}"
    )
    if not pinned:
        problems.append(f"{where} has no install line with a version (`{example}`)")
    wrong = sorted({v for v in pinned if v != version})
    if wrong:
        problems.append(
            f"{where}'s install line quotes {', '.join(wrong)}, not the manifest's {version}"
        )
    links = relative_links(text)
    if links:
        problems.append(
            f"{where} has {len(links)} relative links ({', '.join(links[:3])})"
        )
    if says_unpublished(text):
        problems.append(f"{where} says it is not yet published")
    return problems


def sri_sha512(data: bytes) -> str:
    """npm's ``dist.integrity`` of a tarball: ``sha512-<base64>``."""
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


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
    note: str = ""  # printed whatever the result: a named residual

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
    #: The verdict's name: the RC's, or one --registry check's.
    verdict: str = "RC EVIDENCE"
    title: str = "RC evidence"

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
        fetch: Fetch = http_get,
        only_package: Optional[str] = None,
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
        self.fetch = fetch
        if only_package is not None and only_package not in PACKAGES:
            raise UsageError(
                f"--registry {only_package!r}: one of {', '.join(PACKAGES)}"
            )
        self.only_package = only_package
        self.evidence = Evidence(self.tag)
        if only_package is not None:
            self.evidence.verdict = f"REGISTRY CHECK ({only_package})"
            self.evidence.title = f"Registry check of sdk/{only_package}"
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
        heads = self.github.get(f"git/matching-refs/heads/{self.tag}")
        if not isinstance(heads, list):
            raise EvidenceError("the list of branches is malformed")
        if any(
            isinstance(h, dict) and h.get("ref") == f"refs/heads/{self.tag}"
            for h in heads
        ):
            raise EvidenceError(f"{self.tag} is also the name of a branch")
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
        for run in runs:
            if run.sha != self.sha:
                problems.append(
                    f"run {run.id} is at {run.sha[:12]}, not the RC {self.short}"
                )
                self._row(run, "not at the RC")
        runs = [r for r in runs if r.sha == self.sha]
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
            self._attempts(workflow.file, counted, problems)
            summary = f"run {counted.id}, {jobs} jobs"
        return Check(workflow.label, workflow.file, problems, summary)

    def _attempts(self, workflow: str, counted: Run, problems: List[str]) -> None:
        """List the counted run's earlier attempts; one that did not succeed
        needs the run's disposition."""
        for attempt in range(1, counted.attempt):
            try:
                data = self.github.get(f"actions/runs/{counted.id}/attempts/{attempt}")
                earlier = Run.of(workflow, data)
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

    def judge_claim_tests(self, registry: Registry) -> List[Check]:
        """E4: the targets judged test by test, and the residual held at job
        level (the SDK trees no suite here runs)."""
        by_job = [(t, sdk_job_of(t)) for t, _ in registry.targets]
        tested = [t for t, job in by_job if job is None]
        held: Dict[str, int] = {}
        for _, job in by_job:
            if job is not None:
                held[job] = held.get(job, 0) + 1
        return [self._judge_tests(tested), self._judge_sdk_jobs(held)]

    def _judge_sdk_jobs(self, held: Mapping[str, int]) -> Check:
        """The residual: each SDK tree's targets held by that SDK's job in the
        counted sdk-unit-tests.yml run, which keeps no test-level report."""
        run = self.counted.get("sdk-unit-tests.yml")
        jobs = {
            str(j.get("name")): j
            for j in (self.evidence.jobs.get(str(run.id), []) if run else [])
            if isinstance(j, dict)
        }
        problems = []
        for sdk, count in sorted(held.items()):
            if run is None:
                problems.append(
                    f"{count} targets in sdk/{sdk}: no counted sdk-unit-tests.yml run"
                )
                continue
            job = jobs.get(sdk, {})
            if (job.get("status"), job.get("conclusion")) != ("completed", "success"):
                outcome = job.get("conclusion") or job.get("status") or "not run"
                problems.append(
                    f"{count} targets in sdk/{sdk}: its job in sdk-unit-tests.yml"
                    f" run {run.id} is {outcome}"
                )
        residual = [s for s in SDK_JOBS if s not in PACKAGES]
        note = (
            "test level: "
            + ", ".join(f"sdk/{p.sdk}" for p in PACKAGES.values())
            + "; job level only (sdk-unit-tests.yml, no test-level report): "
            + ", ".join(f"{s} ({held.get(s, 0)})" for s in residual)
        )
        return Check(
            "E4",
            "other SDKs' targets (residual)",
            problems,
            f"{sum(held.values())} targets held by their SDK's job",
            note,
        )

    def _judge_tests(self, targets: Sequence[str]) -> Check:
        # Whether a suite runs each target's file is known before any report
        # is read, so it is said even when the suites did not run.
        outside = [
            f"{target}: no suite in SUITES runs it"
            for target in targets
            if suite_of(target) is None
        ]
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
                    ]
                    + outside,
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
                failed = [c for c in found if c.outcome == "failed"]
                if failed:
                    first = failed[0]
                    problems.append(
                        f"suite {suite}: {len(failed)} tests failed or errored in"
                        f" {invocation.report} (first: {first.classname}"
                        f"::{first.name[:200]})"
                    )
                cases += found
        failing = outside + [
            problem
            for target in targets
            if suite_of(target) is not None
            and (problem := target_problem(target, cases, self.short))
        ]
        problems += failing
        return Check(
            "E4",
            "claim tests",
            problems,
            f"{len(targets) - len(failing)}/{len(targets)} targets passed"
            f" in {len(cases)} test results",
        )

    # -- P: the published packages --------------------------------------------
    def read_manifest(self, package: Package) -> Tuple[str, str]:
        """The package's name and version in its manifest at the RC."""
        data = self.github.get(f"contents/{package.manifest}?ref={self.sha}")
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            raise EvidenceError(f"{package.manifest} at the RC is not a file")
        text = base64.b64decode(str(data.get("content", ""))).decode("utf-8")
        if package.manifest.endswith(".json"):
            project = json.loads(text)
        else:
            project = tomllib.loads(text).get("project")
        if not isinstance(project, dict):
            raise EvidenceError(f"{package.manifest} declares no project")
        name, version = project.get("name"), project.get("version")
        if not isinstance(name, str) or not re.fullmatch(
            r"[@A-Za-z0-9/._-]{1,100}", name
        ):
            raise EvidenceError(f"{package.manifest}: name {name!r}")
        if not isinstance(version, str) or not TAG.fullmatch("v" + version):
            raise EvidenceError(f"{package.manifest}: version {version!r} is not X.Y.Z")
        return name, version

    def commit_of(self, tag: str) -> Optional[str]:
        """The commit *tag* names (an annotated tag is followed); None when
        there is no such tag."""
        try:
            obj = self.github.get(f"git/ref/tags/{tag}")["object"]
        except GhError:
            return None
        for _ in range(5):
            if obj.get("type") != "tag":
                break
            obj = self.github.get(f"git/tags/{obj['sha']}")["object"]
        sha = str(obj.get("sha"))
        if obj.get("type") != "commit" or not SHA.fullmatch(sha):
            raise EvidenceError(f"{tag} does not name a commit")
        return sha

    def sdk_tree(self, commit: str, sdk: str) -> str:
        """The git tree id of sdk/<sdk> at *commit*: two commits with the same
        id have no difference under it (``git diff --quiet A B -- sdk/<sdk>``)."""
        entries = self.github.get(f"contents/sdk?ref={commit}")
        if not isinstance(entries, list):
            raise EvidenceError(f"sdk/ at {commit[:12]} is not a directory")
        for entry in entries:
            if isinstance(entry, dict) and entry.get("name") == sdk:
                tree = str(entry.get("sha"))
                if entry.get("type") != "dir" or not SHA.fullmatch(tree):
                    raise EvidenceError(f"sdk/{sdk} at {commit[:12]} is not a tree")
                return tree
        raise EvidenceError(f"there is no sdk/{sdk} at {commit[:12]}")

    def release_run(self, ref: str, commit: str, problems: List[str]) -> Optional[Run]:
        """The sdk-release.yml run of the tag *ref*, at the tag's *commit*,
        that published it; every other run of the tag listed, and one that did
        not succeed disposed of."""
        try:
            runs = _runs(
                self.github,
                f"actions/workflows/{SDK_RELEASE}/runs"
                f"?branch={urllib.parse.quote(ref, safe='')}&per_page=100",
                SDK_RELEASE,
            )
        except (GhError, EvidenceError) as error:
            problems.append(f"cannot list the {SDK_RELEASE} runs: {error}")
            return None
        published: List[Run] = []
        for run in sorted(runs, key=lambda r: (r.created, r.id)):
            if run.ref != ref:
                continue  # the list is filtered by GitHub; this is not ours
            if run.sha != commit:
                problems.append(
                    f"run {run.id} is at {run.sha[:12]}, not {ref}'s commit {commit[:12]}"
                )
                self._row(run, f"not at {ref}")
            elif run.status != "completed":
                problems.append(f"run {run.id} is still {run.status}: wait for it")
                self._row(run, "still running")
            elif run.conclusion == "success" and run.event in RELEASE_EVENTS:
                published.append(run)
            elif run.conclusion not in NOT_FAILED:
                self._row(
                    run,
                    self._disposed(run.id, f"{ref}, {run.conclusion}", problems),
                )
            else:
                self._row(run, "not counted")
        if not published:
            problems.append(
                f"no {SDK_RELEASE} run of {ref} succeeded: push the tag and approve"
                " the publish"
            )
            return None
        for run in published[:-1]:
            self._row(run, "succeeded too: a version publishes once")
        if len(published) > 1:
            problems.append(
                f"{len(published)} {SDK_RELEASE} runs of {ref} succeeded; one publishes"
            )
        return published[-1]

    def release_record(
        self, package: Package, run: Run, problems: List[str]
    ) -> Optional[Dict[str, str]]:
        """The counted run's jobs, and what its build job recorded."""
        try:
            data = self.github.get(
                f"actions/runs/{run.id}/jobs?filter=latest&per_page=100"
            )
            jobs = data.get("jobs") if isinstance(data, dict) else None
            if not isinstance(jobs, list) or int(data.get("total_count") or 0) > len(
                jobs
            ):
                raise EvidenceError(f"run {run.id}: the jobs list is malformed")
        except (GhError, EvidenceError, TypeError, ValueError) as error:
            problems.append(f"cannot read the jobs of run {run.id}: {error}")
            self._row(run, "counted (published)", "?")
            return None
        self.evidence.jobs[str(run.id)] = [
            {k: j.get(k) for k in ("name", "status", "conclusion", "html_url")}
            for j in jobs
            if isinstance(j, dict)
        ]
        named = {str(j.get("name")): j for j in jobs if isinstance(j, dict)}
        required = (RESOLVE_JOB, package.build_job, package.publish_job)
        good = 0
        for name in required:
            job = named.get(name)
            if job is None:
                problems.append(f"run {run.id}: job {name!r} did not run")
            elif (job.get("status"), job.get("conclusion")) != ("completed", "success"):
                problems.append(
                    f"run {run.id}: job {name!r} {job.get('conclusion') or job.get('status')}"
                )
            else:
                good += 1
        for name, job in named.items():
            if name not in required and job.get("conclusion") not in (
                "skipped",
                "success",
            ):
                problems.append(
                    f"run {run.id}: job {name!r} {job.get('conclusion') or job.get('status')}"
                )
        self._row(run, "counted (published)", f"{good}/{len(required)}")
        self._attempts(SDK_RELEASE, run, problems)
        build = named.get(package.build_job)
        if build is None:
            return None  # "did not run", above
        if not isinstance(build.get("id"), int):
            problems.append(
                f"run {run.id}: job {package.build_job!r} has no id, so its record"
                " cannot be read"
            )
            return None
        try:
            log = self.github.text(f"actions/jobs/{build['id']}/logs")
        except GhError as error:
            problems.append(f"cannot read the log of {package.build_job!r}: {error}")
            return None
        files, found = read_record(package, log)
        problems += [f"run {run.id}: {p}" for p in found]
        return None if found else files

    def judge_package(self, package: Package) -> Check:
        subject = f"{package.registry} package of sdk/{package.sdk}"
        try:
            name, version = self.read_manifest(package)
        except (
            GhError,
            EvidenceError,
            ValueError,
            TypeError,
            tomllib.TOMLDecodeError,
        ) as error:
            return Check(
                "P", subject, [f"cannot read {package.manifest} at the RC: {error}"]
            )
        subject = f"{package.registry} {name} {version}"
        problems: List[str] = []
        ref = f"sdk/{package.sdk}/v{version}"
        run = recorded = None
        try:
            commit = self.commit_of(ref)
            if commit is None:
                problems.append(
                    f"{ref} is not a tag: the published v{version} was not released"
                    f" by {SDK_RELEASE}"
                )
            elif self.sdk_tree(commit, package.sdk) != self.sdk_tree(
                self.sha, package.sdk
            ):
                problems.append(
                    f"the RC changes sdk/{package.sdk} since the published v{version}"
                    f" ({ref} at {commit[:12]})"
                )
        except (GhError, EvidenceError, KeyError, TypeError) as error:
            commit = None
            problems.append(f"cannot compare sdk/{package.sdk} with {ref}: {error}")
        if commit is not None:
            run = self.release_run(ref, commit, problems)
            recorded = self.release_record(package, run, problems) if run else None
        if package.registry == "PyPI":
            checked = self._judge_pypi(package, name, version, run, recorded, problems)
        else:
            checked = self._judge_npm(package, name, version, run, recorded, problems)
        summary = (
            f"run {run.id} of {ref}: sdk/{package.sdk} unchanged at the RC;"
            f" {checked} published files equal its record; the description"
            f" installs {version}"
            if run
            else ""
        )
        return Check("P", subject, problems, summary)

    def _registry_json(self, url: str, problems: List[str]) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(self.fetch(url))
        except (FetchError, ValueError) as error:
            problems.append(f"cannot read {url}: {error}")
            return None
        if not isinstance(data, dict):
            problems.append(f"{url} is not a JSON object")
            return None
        return data

    def _judge_pypi(
        self,
        package: Package,
        name: str,
        version: str,
        run: Optional[Run],
        recorded: Optional[Dict[str, str]],
        problems: List[str],
    ) -> int:
        """PyPI's latest release of *name*: its version, its files' sha256
        against the record, and its description. The number of files compared."""
        url = PYPI_JSON.format(name=urllib.parse.quote(name, safe=""))
        data = self._registry_json(url, problems)
        if data is None:
            return 0
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        latest = info.get("version")
        problems += description_problems(
            package, name, version, info.get("description")
        )
        if latest != version:
            problems.append(
                f"PyPI's latest {name} is {latest}, not the manifest's {version}"
            )
            return 0
        published: Dict[str, str] = {}
        for entry in data.get("urls") or []:
            try:
                published[str(entry["filename"])] = str(entry["digests"]["sha256"])
            except (KeyError, TypeError):
                problems.append(f"{url}: a file entry is malformed")
        if not published:
            problems.append(f"PyPI lists no file of {name} {version}")
        if recorded is None or run is None:
            return 0
        for file in sorted(set(recorded) | set(published)):
            if file not in published:
                problems.append(
                    f"{file}: run {run.id} recorded it; PyPI does not have it"
                )
            elif file not in recorded:
                problems.append(f"{file}: on PyPI; run {run.id} did not record it")
            elif published[file] != recorded[file]:
                problems.append(
                    f"{file}: PyPI's sha256 is {published[file][:16]}..., run {run.id}"
                    f" recorded {recorded[file][:16]}..."
                )
        return len(published)

    def _judge_npm(
        self,
        package: Package,
        name: str,
        version: str,
        run: Optional[Run],
        recorded: Optional[Dict[str, str]],
        problems: List[str],
    ) -> int:
        """npm's *name*: latest is the version, the tarball it serves has the
        recorded sha256 and dist.integrity is that tarball's, and the README it
        shows. The number of files compared."""
        url = NPM_DOCUMENT.format(name=name.replace("/", "%2f"))
        data = self._registry_json(url, problems)
        if data is None:
            return 0
        tags = data.get("dist-tags") if isinstance(data.get("dist-tags"), dict) else {}
        problems += description_problems(package, name, version, data.get("readme"))
        if tags.get("latest") != version:
            problems.append(
                f"npm's latest {name} is {tags.get('latest')}, not the manifest's {version}"
            )
            return 0
        versions = (
            data.get("versions") if isinstance(data.get("versions"), dict) else {}
        )
        entry = versions.get(version) if isinstance(versions.get(version), dict) else {}
        dist = entry.get("dist") if isinstance(entry.get("dist"), dict) else {}
        integrity, tarball = dist.get("integrity"), dist.get("tarball")
        if not isinstance(integrity, str) or not isinstance(tarball, str):
            problems.append(f"npm lists no tarball and integrity for {name} {version}")
            return 0
        if recorded is None or run is None:
            return 0
        ((file, want),) = recorded.items()
        if not tarball.startswith(NPM_TARBALLS):
            problems.append(f"npm's tarball of {version} is not on {NPM_TARBALLS}")
            return 0
        try:
            data_bytes = self.fetch(tarball)
        except FetchError as error:
            problems.append(f"cannot read the npm tarball: {error}")
            return 0
        got = hashlib.sha256(data_bytes).hexdigest()
        if got != want:
            problems.append(
                f"npm's tarball of {version} has sha256 {got[:16]}...; run {run.id}"
                f" recorded {want[:16]}... ({file})"
            )
        elif integrity != sri_sha512(data_bytes):
            problems.append(
                f"npm's dist.integrity of {version} ({integrity[:24]}...) is not the"
                f" recorded tarball's ({sri_sha512(data_bytes)[:24]}...)"
            )
        return 1

    # -- E7 --------------------------------------------------------------------
    def judge_staging(self, staging: pathlib.Path) -> Check:
        problems: List[str] = []
        if self.commit_time is None:
            return Check("E7", "staging", ["the RC commit's time is not known"])
        try:
            deploys = _runs(
                self.github,
                "actions/workflows/deploy.yml/runs?event=workflow_dispatch"
                f"&{since(self.commit_time)}&per_page=100",
                "deploy.yml",
            )
            synthetic = _runs(
                self.github,
                f"actions/workflows/synthetic.yml/runs?{since(self.commit_time)}&per_page=100",
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
        if self.only_package is not None:
            checks.append(self.judge_package(PACKAGES[self.only_package]))
        else:
            for workflow in WORKFLOWS:
                checks.append(self.judge_workflow(workflow))
            checks.append(self.judge_journeys())
            registry = self.read_registry(self.root)
            checks.append(self.judge_registry(registry))
            checks += self.judge_claim_tests(registry)
            for package in PACKAGES.values():
                checks.append(self.judge_package(package))
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
    lines = [
        f"# {evidence.title}: {evidence.tag} ({evidence.sha[:12] or 'unresolved'})",
        "",
    ]
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
        if check.note:
            detail = f"{check.note}; {detail}" if detail else check.note
        lines.append(
            f"| {check.label} | {_cell(check.subject)} | {PASS if check.ok else FAIL}"
            f" | {_cell(detail)} |"
        )
    if evidence.registry:
        lines += ["", "Registry at the tag:", "", "```"] + evidence.registry + ["```"]
    failed = sum(1 for c in evidence.checks if not c.ok)
    lines += ["", f"{failed} of {len(evidence.checks)} checks failed."]
    lines.append(f"{evidence.verdict}: {PASS if evidence.passed else FAIL}")
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
    lines.append(
        "# Before them, at the tag's commit, one package at a time, npm first:"
    )
    for name, package in PACKAGES.items():
        lines.append(
            f"# the founder pushes sdk/{name}/v<{package.manifest}'s version> and"
            " approves its publish; then"
        )
        lines.append(
            f"python3 scripts/rc_evidence.py {tag} --registry {name} --out <dir>"
        )
    lines.append("# The repository's current name, from its id:")
    lines.append(f'REPO="$(gh api repositories/{REPOSITORY_ID} --jq .full_name)"')
    dispatched = [w for w in WORKFLOWS if w.at_tag]
    for number, workflow in zip(range(1, len(dispatched) + 1), dispatched):
        lines.append(f"# {number}. {workflow.plan}")
        command = f'gh workflow run {workflow.file} --ref {tag} -R "$REPO"'
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
    junit_dir = junit_dir.resolve()  # jest runs in its project, not here
    junit_dir.mkdir(parents=True, exist_ok=True)
    failed = []
    for invocation in SUITES[name]:
        env = dict(os.environ)
        env.update(dict(invocation.env))
        report = junit_dir / invocation.report
        raw = junit_dir / (report.stem + ".jest.json")
        if invocation.runner == "jest":
            command = [
                "npx",
                "jest",
                "--watchAll=false",
                "--json",
                f"--outputFile={raw}",
                *invocation.args,
            ]
            cwd = ROOT / invocation.cwd
        else:
            command = [
                sys.executable,
                "-m",
                "pytest",
                *invocation.args,
                *PYTEST,
                f"--junitxml={report}",
            ]
            cwd = ROOT
        print(f"rc-evidence: suite {name}: {' '.join(command)}", flush=True)
        done = runner(command, cwd=str(cwd), env=env, check=False)
        if done.returncode != 0:
            failed.append(f"{invocation.report} (exit {done.returncode})")
        if invocation.runner == "jest":
            try:
                jest_junit(raw, report)
            except (OSError, ValueError, KeyError, TypeError) as error:
                failed.append(
                    f"{invocation.report}: no jest report ({type(error).__name__})"
                )
    for item in failed:
        print(f"rc-evidence: suite {name}: {item}", flush=True)
    return 1 if failed else 0


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    gh: Gh = run_gh,
    head_of: Callable[[pathlib.Path], Optional[str]] = git_head,
    registry: Callable[[pathlib.Path], Registry] = read_registry,
    fetch: Fetch = http_get,
    environ: Optional[Mapping[str, str]] = None,
) -> int:
    environ = os.environ if environ is None else environ
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
    parser.add_argument(
        "--registry",
        choices=sorted(PACKAGES),
        help="check only this published package against its registry",
    )
    parser.add_argument("--suite", choices=sorted(SUITES), help="run one suite")
    parser.add_argument(
        "--junit-dir", type=pathlib.Path, help="--suite: where it writes"
    )
    args = parser.parse_args(argv)
    try:
        if args.suite:
            if args.junit_dir is None or args.tag or args.plan or args.registry:
                raise UsageError("--suite NAME --junit-dir DIR, and nothing else")
            return run_suite(args.suite, args.junit_dir)
        if args.plan:
            if args.tag or args.registry:
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
        if args.registry is not None and (
            args.staging is not None or args.this_run is not None
        ):
            raise UsageError(
                "--registry checks one package on the coordinator's machine:"
                " not with --staging, and never in a run"
            )
        check_tag(args.tag)
        verifier = Verifier(
            args.tag,
            github=GitHub(gh, repository(environ, gh)),
            root=args.root,
            out=args.out,
            dispositions=parse_dispositions(args.dispositions),
            junit=args.junit,
            this_run=args.this_run,
            staging=args.staging,
            head_of=head_of,
            registry=registry,
            fetch=fetch,
            only_package=args.registry,
        )
    except UsageError as error:
        print(f"rc_evidence.py: {error}", file=sys.stderr)
        return 2
    evidence = verifier.run()
    sys.stdout.write(write_out(evidence, args.out))
    return 0 if evidence.passed else 1


if __name__ == "__main__":
    sys.exit(main())
