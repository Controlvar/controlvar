"""The docs-journey runner's pytest session (#939).

One test per journey file in ``journeys/``, in the order of their stack and
profile so each stack comes up once. Run it from the repository root, in an
environment with ``tests/acceptance/requirements.txt`` installed and Chromium
installed for Playwright (``python -m playwright install chromium``)::

    DOCS_JOURNEY_RUN_DIR=/tmp/docs-run \\
        python -m pytest -c tests/acceptance/docs/pytest.ini tests/acceptance/docs

This directory is its own pytest root (``pytest.ini``): the repository's root
``conftest.py`` is not loaded, and nothing is imported from the product. The one
thing taken from ``backend`` is the repository's test plugin
``backend/tests/no_real_aws.py`` (pytest and the standard library only), loaded
in ``pytest_configure`` as in every other suite: dummy AWS credentials, no
``~/.aws``, and a failing ``aws`` first on ``PATH``, so nothing a journey starts
can reach an AWS account. ``backend/tests/unit/test_no_real_aws.py`` checks
this root loads it. (It is not ``pytest_plugins`` here: pytest refuses that in
a conftest that is not at the root of the run, as this one is not when a run
starts from ``tests/acceptance``.)

Environment:

* ``DOCS_JOURNEY_RUN_DIR``: where everything the run writes goes (the results
  log ``results.jsonl``, a report per guide as ``<journey>.md`` and
  ``<journey>.html``, ``summary.md``, ``verdicts.json`` (each journey's
  verdict, for ``scripts/docs_journeys_report.py``), the screenshots and
  snapshots, the videos, the launch walkthroughs as
  ``recordings/<name>.webm`` (``recordings.toml``), each stack's output under
  ``stacks/``, and last ``secret-scan.json``). Refused inside the
  repository, so the runner writes nothing into the tree (Python's own
  ``__pycache__`` aside: ``PYTHONDONTWRITEBYTECODE=1`` keeps that out too;
  marketing-local's build and what demo-local's script installs are the other
  exceptions, see ``stacks.py``). Unset, a
  new temporary directory, made when a journey first runs and named at the end
  of the run; a session that runs no journey then writes nothing.
* ``DOCS_JOURNEY_RUN_ID``, ``DOCS_JOURNEY_SHA``: written on every log line;
  by default ``GITHUB_RUN_ID`` and ``GITHUB_SHA``, else ``local`` and
  ``unknown`` (no git is run).
* ``DOCS_JOURNEY_RUN_LINK``: the run's link in the report headers; by default
  built from ``GITHUB_SERVER_URL``, ``GITHUB_REPOSITORY`` and
  ``GITHUB_RUN_ID``. The headers are the QA templates only when the commit and
  the link are ones they accept (a run in this repository's Actions);
  otherwise a plain line (``docs_runner/report.py``).
* ``DOCS_JOURNEY_STACKS``: the stacks whose journeys this session runs, a
  comma-separated list of names from ``stacks.STACK_CLASSES``. Unset, every
  stack but demo-local, which binds fixed host ports and runs only in the
  ``demo`` job of ``docs-journeys.yml`` (``DOCS_JOURNEY_STACKS=demo-local``).
  A journey file whose stack cannot be read is run with the default set, so
  its refusal is reported there. An unknown name stops the session, and a
  selection that leaves no journey fails it (an empty parameter set is a
  skip, and nothing here may skip).
* ``DOCS_JOURNEY_TIMEOUT_MS`` (10000), ``DOCS_JOURNEY_RECORD_VIDEO`` (``1``;
  ``0`` records no video and no walkthrough), ``DOCS_JOURNEY_HEADED`` (``1``
  shows the browser),
  ``DOCS_JOURNEY_KEEP_STACKS`` (``1`` leaves the stacks up at the end).
* The stacks' own settings: ``docs_runner/stacks.py``.

The run directory is uploaded as a public artifact, so when the reports are
written every file in it is read for each value the run made up, kept or
signed in for (``docs_runner/redaction.py``): a file holding one is removed,
with the screenshot beside it and its journey's recording; that journey is
FAIL in the reports and in ``verdicts.json`` (written again, and scanned
again); and the run fails. ``secret-scan.json``, written last, says what was
read and removed. ``docs-journeys.yml`` uploads the directory only when that
record exists and names no removed file, and ``verdicts.json`` only when the
record exists.

A step that cannot run is reported NOT RUN with a reason from
``docs_runner/registry.py``. Nothing here may skip: pytest's skip and xfail are
banned in this directory by ``ruff.toml``, and a run in which any test or
collector was skipped anyway exits 1. A retry plugin is refused at start.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import pytest
import yaml

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from docs_runner import guide as guides
from docs_runner import redaction, report
from docs_runner.loader import Refused, context_for, guide_root, load
from docs_runner.log import FAIL, Log, run_directory
from docs_runner.stacks import STACK_CLASSES, StackError

JOURNEYS = HERE / "journeys"

RETRY_PLUGINS = ("rerunfailures", "flaky", "pytest_retry", "retry")
STACK_FIXTURES = {
    "compose-dev": "compose_dev_stack",
    "compose-sso": "compose_sso_stack",
    "docs-local": "docs_local_stack",
    "docs-published": "docs_published_stack",
    "marketing-local": "marketing_local_stack",
    "demo-local": "demo_local_stack",
}
#: The stacks a session runs only when ``DOCS_JOURNEY_STACKS`` names them.
OWN_JOB_STACKS = ("demo-local",)

RUNS = pytest.StashKey[List[report.GuideRun]]()
SKIPPED = pytest.StashKey[List[str]]()
REPORT_PROBLEMS = pytest.StashKey[List[str]]()
RUN_DIR = pytest.StashKey[Optional[Path]]()
#: Every value the run made up, kept or signed in for (the runner's redactor's).
SECRET_VALUES = pytest.StashKey[Set[str]]()
#: The launch walkthroughs the runner recorded, by name (``execute.Walkthrough``).
RECORDED = pytest.StashKey[Dict[str, Dict[str, Any]]]()
SCAN = pytest.StashKey[Dict[str, Any]]()
#: One line per step that polled (``JourneyRunner.polls``): numbers only.
POLLS = pytest.StashKey[List[str]]()
#: Every stack this session made, for the values it keeps out of the run.
STACKS_MADE = pytest.StashKey[List[Any]]()
#: What a stack's ``down`` found wrong (demo-local's stop check): the run fails.
STACK_PROBLEMS = pytest.StashKey[List[str]]()


def pytest_configure(config: pytest.Config) -> None:
    config.pluginmanager.import_plugin("backend.tests.no_real_aws")
    for name in RETRY_PLUGINS:
        if config.pluginmanager.has_plugin(name):
            raise pytest.UsageError(
                f"the {name} plugin is loaded; the docs journeys never retry a step"
            )
    config.stash[RUNS] = []
    config.stash[SKIPPED] = []
    config.stash[REPORT_PROBLEMS] = []
    config.stash[RUN_DIR] = None
    config.stash[SECRET_VALUES] = set()
    config.stash[RECORDED] = {}
    config.stash[SCAN] = {}
    config.stash[POLLS] = []
    config.stash[STACKS_MADE] = []
    config.stash[STACK_PROBLEMS] = []
    try:
        selected_stacks(os.environ)
    except ValueError as error:
        raise pytest.UsageError(str(error)) from None
    if os.environ.get("DOCS_JOURNEY_RUN_DIR"):
        try:
            config.stash[RUN_DIR] = run_directory(os.environ, REPO_ROOT)
        except ValueError as error:
            raise pytest.UsageError(str(error)) from None
    config.pluginmanager.register(SkipWatch(config), "docs-journeys-skip-watch")


class SkipWatch:
    """Notes every skipped collector and test: here a skip is a failure."""

    def __init__(self, config: pytest.Config):
        self.skipped = config.stash[SKIPPED]

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.skipped:
            self.skipped.append(report.nodeid or "(collection)")

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.skipped:
            self.skipped.append(report.nodeid)


def _order(path: Path) -> tuple:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        data = None
    if not isinstance(data, dict):
        return ("", "", path.name)
    return (str(data.get("stack", "")), str(data.get("profile", "")), path.name)


def selected_stacks(environ) -> tuple:
    """The stacks ``DOCS_JOURNEY_STACKS`` names, or every one but ``OWN_JOB_STACKS``."""
    raw = environ.get("DOCS_JOURNEY_STACKS", "")
    if not raw.strip():
        return tuple(name for name in STACK_CLASSES if name not in OWN_JOB_STACKS)
    names = tuple(name.strip() for name in raw.split(",") if name.strip())
    unknown = [name for name in names if name not in STACK_CLASSES]
    if unknown or not names:
        raise ValueError(
            f"DOCS_JOURNEY_STACKS={raw!r} names {unknown or 'no'} stack; a stack is"
            f" one of {', '.join(STACK_CLASSES)}"
        )
    return names


def selected(path: Path, stacks: tuple, environ) -> bool:
    """True when *path*'s journey runs in this session.

    A file whose stack cannot be read runs in the default session, which
    reports its refusal.
    """
    stack = _order(path)[0]
    if stack in STACK_CLASSES:
        return stack in stacks
    return not environ.get("DOCS_JOURNEY_STACKS", "").strip()


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "journey_file" in metafunc.fixturenames:
        stacks = selected_stacks(os.environ)
        files = [
            path
            for path in sorted(JOURNEYS.glob("*.yaml"), key=_order)
            if selected(path, stacks, os.environ)
        ]
        metafunc.parametrize("journey_file", files, ids=[path.stem for path in files])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def run_dir_of(config: pytest.Config) -> Path:
    """The run directory, made now if the environment named none."""
    if config.stash[RUN_DIR] is None:
        config.stash[RUN_DIR] = run_directory(os.environ, REPO_ROOT)
    return config.stash[RUN_DIR]


def run_link(environ) -> str:
    """``DOCS_JOURNEY_RUN_LINK``, or this Actions run's link, or ""."""
    if environ.get("DOCS_JOURNEY_RUN_LINK"):
        return environ["DOCS_JOURNEY_RUN_LINK"]
    parts = [environ.get(name, "") for name in ("GITHUB_REPOSITORY", "GITHUB_RUN_ID")]
    if not all(parts):
        return ""
    server = environ.get("GITHUB_SERVER_URL", "https://github.com")
    return f"{server}/{parts[0]}/actions/runs/{parts[1]}"


class RunSettings:
    def __init__(self, config: pytest.Config):
        environ = os.environ
        self.run_dir: Path = run_dir_of(config)
        self.run_id = environ.get("DOCS_JOURNEY_RUN_ID") or environ.get(
            "GITHUB_RUN_ID", "local"
        )
        self.sha = environ.get("DOCS_JOURNEY_SHA") or environ.get(
            "GITHUB_SHA", "unknown"
        )
        self.timeout_ms = int(environ.get("DOCS_JOURNEY_TIMEOUT_MS", "10000"))
        self.record_video = environ.get("DOCS_JOURNEY_RECORD_VIDEO", "1") not in (
            "0",
            "false",
        )
        self.headed = environ.get("DOCS_JOURNEY_HEADED") == "1"
        self.keep_stacks = environ.get("DOCS_JOURNEY_KEEP_STACKS") == "1"


@pytest.fixture(scope="session")
def run_settings(pytestconfig: pytest.Config) -> RunSettings:
    return RunSettings(pytestconfig)


def _stack_fixture(stack: str):
    @pytest.fixture(scope="session", name=STACK_FIXTURES[stack])
    def fixture(run_settings: RunSettings, pytestconfig: pytest.Config):
        instance = STACK_CLASSES[stack](
            REPO_ROOT, run_settings.run_dir / "stacks", os.environ
        )
        pytestconfig.stash[STACKS_MADE].append(instance)
        yield instance
        if not run_settings.keep_stacks:
            try:
                instance.down()
            except StackError as error:
                pytestconfig.stash[STACK_PROBLEMS].append(f"{stack}: {error}")

    return fixture


compose_dev_stack = _stack_fixture("compose-dev")
compose_sso_stack = _stack_fixture("compose-sso")
docs_local_stack = _stack_fixture("docs-local")
docs_published_stack = _stack_fixture("docs-published")
marketing_local_stack = _stack_fixture("marketing-local")
demo_local_stack = _stack_fixture("demo-local")


@pytest.fixture(scope="session")
def playwright_session():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as handle:
        yield handle


@pytest.fixture(scope="session")
def browser(playwright_session, run_settings: RunSettings):
    instance = playwright_session.chromium.launch(headless=not run_settings.headed)
    yield instance
    instance.close()


@pytest.fixture(scope="session")
def journey_runner(
    playwright_session, browser, run_settings: RunSettings, pytestconfig
):
    from docs_runner.execute import JourneyRunner, Settings

    runner = JourneyRunner(
        playwright_session,
        browser,
        Log(run_settings.run_dir / "results.jsonl"),
        Settings(
            run_dir=run_settings.run_dir,
            run_id=run_settings.run_id,
            sha=run_settings.sha,
            timeout_ms=run_settings.timeout_ms,
            record_video=run_settings.record_video,
        ),
    )
    # The same set, so the end of the run scans for every value added later.
    pytestconfig.stash[SECRET_VALUES] = runner.redactor.values
    # The same mapping, filled in as each walkthrough is recorded.
    pytestconfig.stash[RECORDED] = runner.recorded
    # The same list, so the terminal summary prints every step that polled.
    pytestconfig.stash[POLLS] = runner.polls
    return runner


# ---------------------------------------------------------------------------
# One journey
# ---------------------------------------------------------------------------
def _title(guide_path: str) -> str:
    try:
        return guides.load(
            guide_root(context_for(REPO_ROOT), guide_path), guide_path
        ).title
    except OSError:
        return guide_path


def stack_secrets(config: pytest.Config) -> Set[str]:
    """Every value a stack of this session keeps out of the run's files."""
    return {
        value
        for instance in config.stash[STACKS_MADE]
        for value in getattr(instance, "secrets", ())
    }


def walk(journey_file: Path, request: pytest.FixtureRequest) -> report.GuideRun:
    """Load, bring up the stack, run the steps; the outcome is kept for the report."""
    runs = request.config.stash[RUNS]
    journey_id = journey_file.stem
    try:
        journey = load(journey_file, context_for(REPO_ROOT))
    except Refused as refused:
        data = yaml.safe_load(journey_file.read_text(encoding="utf-8")) or {}
        guide_path = str(data.get("guide", "")) if isinstance(data, dict) else ""
        outcome = report.GuideRun(
            journey=journey_id,
            guide=guide_path or "(unknown)",
            title=_title(guide_path) if guide_path else journey_id,
            stack=str(data.get("stack", "")) if isinstance(data, dict) else "",
            refused=tuple(refused.problems),
        )
        runs.append(outcome)
        pytest.fail(str(refused), pytrace=False)
    guide = guides.load(
        guide_root(context_for(REPO_ROOT), journey.guide), journey.guide
    )
    try:
        stack = request.getfixturevalue(STACK_FIXTURES[journey.stack])
        running = stack.up(journey.profile)
    except StackError as error:
        outcome = report.GuideRun(
            journey=journey_id,
            guide=journey.guide,
            title=guide.title,
            stack=journey.stack,
            error=str(error),
        )
        runs.append(outcome)
        pytest.fail(f"{journey_id}: {error}", pytrace=False)
    runner = request.getfixturevalue("journey_runner")
    for value in running.secrets:
        runner.redactor.add(value)
    records = runner.run(journey, journey_id, running, guide)
    outcome = report.GuideRun(
        journey=journey_id,
        guide=journey.guide,
        title=guide.title,
        stack=journey.stack,
        records=tuple(records),
    )
    runs.append(outcome)
    return outcome


@pytest.fixture
def walk_journey(request: pytest.FixtureRequest):
    """``walk`` for this test: ``walk_journey(path)`` returns the GuideRun."""
    return lambda journey_file: walk(journey_file, request)


# ---------------------------------------------------------------------------
# The end of the run: the reports, and no skip reads as green
# ---------------------------------------------------------------------------
def owners_of(recorded: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """Each walkthrough file the runner wrote, with the journey that recorded it."""
    return {
        str(outcome["file"]): str(outcome["journey"])
        for outcome in recorded.values()
        if outcome.get("file")
    }


def write_reports(
    runs: List[report.GuideRun],
    run_dir: Path,
    sha: str,
    date: str,
    link: str = "",
    recorded: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Optional[str]:
    """Write every report; the first ReportError's message, or None."""
    problem = None
    for outcome in runs:
        try:
            (run_dir / f"{outcome.journey}.md").write_text(
                report.guide_markdown(
                    outcome, run_dir, date=date, sha=sha, run_link=link
                ),
                encoding="utf-8",
            )
            (run_dir / f"{outcome.journey}.html").write_text(
                report.guide_html(outcome, run_dir, date=date, sha=sha, run_link=link),
                encoding="utf-8",
            )
        except report.ReportError as error:
            problem = problem or str(error)
    context = context_for(REPO_ROOT)
    walkthroughs = report.walkthrough_rows(
        context.recordings,
        recorded or {},
        {run.journey: report.verdict(run) for run in runs},
        lambda name: (run_dir / name).is_file(),
    )
    try:
        (run_dir / "summary.md").write_text(
            report.summary_markdown(
                runs,
                context.inventory,
                date=date,
                sha=sha,
                run_link=link,
                walkthroughs=walkthroughs,
            ),
            encoding="utf-8",
        )
    except report.ReportError as error:
        problem = problem or str(error)
    (run_dir / "verdicts.json").write_text(
        json.dumps(
            report.verdicts_record(runs, report.RunInfo(date, sha, link)), indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    return problem


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    config = session.config
    sha = os.environ.get("DOCS_JOURNEY_SHA") or os.environ.get("GITHUB_SHA", "unknown")
    date = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    runs = config.stash[RUNS]
    if runs or config.stash[RUN_DIR] is not None:
        run_dir = run_dir_of(config)
        recorded = config.stash[RECORDED]
        owners = owners_of(recorded)
        link = run_link(os.environ)
        problem = write_reports(runs, run_dir, sha, date, link, recorded)
        if problem is not None:
            config.stash[REPORT_PROBLEMS].append(problem)
        values = set(config.stash[SECRET_VALUES]) | stack_secrets(config)
        scan = redaction.clear(run_dir, values, owners=owners)
        if scan["removed"]:
            # The journeys whose files held a value fail, in the reports and in
            # verdicts.json too, so a run's report job never reads all-pass
            # after a hit; what is written again is scanned again.
            removed = list(scan["removed"])
            hit = redaction.journeys_of(
                removed, [run.journey for run in runs], owners=owners
            )
            runs[:] = [
                dataclasses.replace(
                    run,
                    scrubbed=redaction.files_of(removed, run.journey, owners=owners),
                )
                if run.journey in hit
                else run
                for run in runs
            ]
            problem = write_reports(runs, run_dir, sha, date, link, recorded)
            if problem is not None:
                config.stash[REPORT_PROBLEMS].append(problem)
            scan = redaction.clear(run_dir, values, earlier=scan, owners=owners)
        config.stash[SCAN] = scan
    removed = config.stash[SCAN].get("removed")
    if (
        config.stash[SKIPPED]
        or config.stash[REPORT_PROBLEMS]
        or config.stash[STACK_PROBLEMS]
        or removed
    ):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    runs = config.stash[RUNS]
    failed = [o.journey for o in runs if report.verdict(o).word == FAIL]
    where = config.stash[RUN_DIR]
    terminalreporter.write_line(
        f"docs journeys: {len(runs)} run, {len(failed)} failed;"
        + (f" results in {where}" if where is not None else " nothing written")
    )
    for line in config.stash[POLLS]:
        terminalreporter.write_line(f"docs journeys: {line}")
    scan = config.stash[SCAN]
    if scan:
        terminalreporter.write_line(
            f"docs journeys: read {scan['files_read']} file(s) for"
            f" {scan['values']} value(s) the run made up, kept or signed in for;"
            f" {len(scan['removed'])} held one"
        )
    for name in scan.get("removed", []):
        terminalreporter.write_line(
            f"docs journeys: {name} held a value the run made up, kept or signed in"
            " for; it was removed, so the run fails"
        )
    for name in scan.get("screens_removed", []):
        terminalreporter.write_line(
            f"docs journeys: {name} was removed with the file it belongs to"
        )
    for problem in config.stash[REPORT_PROBLEMS]:
        terminalreporter.write_line(f"docs journeys: report refused: {problem}")
    for problem in config.stash[STACK_PROBLEMS]:
        terminalreporter.write_line(f"docs journeys: stack {problem}; the run fails")
    for nodeid in config.stash[SKIPPED]:
        terminalreporter.write_line(
            f"docs journeys: {nodeid} was skipped; nothing here may skip, so the"
            " run fails"
        )
