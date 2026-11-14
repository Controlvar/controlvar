"""Session set-up for the founder-run real-account check.

Collected only under ``-m warehouse_live`` (see ``selection.py``); a live file
or node id named on the command line without it stops the session (exit 4)
before anything else, since the directory ignore does not apply to named
paths.  Before any
test runs, the target and its credentials are read from the environment
(:func:`~.harness.load_config`); anything missing stops the session with a
message naming what to set -- nothing is skipped.  Tests for the other
warehouses are deselected (each target's tests carry its prefix,
``harness.CHECK_PREFIXES``).  When the session ends, the evidence is written
outside the repository and its path printed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from modules.backend.tests.live_warehouse import harness
from modules.backend.tests.live_warehouse.selection import (
    MARKER,
    is_live_path,
    selects_live_marker,
)

_STATE: Dict[str, Any] = {}


def pytest_collection_modifyitems(session, config, items):
    live = [item for item in items if is_live_path(item.path)]
    if not live:
        return
    # The second gate.  pytest_ignore_collect (modules/backend/tests/conftest.py)
    # is not consulted for a file or node id named on the command line, so
    # `pytest .../test_bigquery_live.py` reaches here without -m.  Refuse it
    # before the credentials are read: with WAREHOUSE_LIVE_* exported it would
    # otherwise run against the real account.
    markexpr = config.getoption("markexpr", "")
    if not selects_live_marker(markexpr):
        pytest.exit(
            "warehouse_live check refused to start: live tests were named without "
            f"-m {MARKER} (got -m {markexpr!r}). They call a real warehouse; run "
            f"`pytest -m {MARKER} modules/backend/tests/live_warehouse/`.",
            returncode=4,
        )
    if config.option.collectonly:
        return
    try:
        live_config = harness.load_config()
        root = harness.evidence_root()
    except harness.LiveConfigError as exc:
        pytest.exit(f"warehouse_live check refused to start: {exc}", returncode=4)
    prefix = harness.CHECK_PREFIXES[live_config.target]
    keep: List[Any] = []
    drop: List[Any] = []
    for item in items:
        if is_live_path(item.path) and not item.name.startswith("test_" + prefix):
            drop.append(item)
        else:
            keep.append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep
    recorder = harness.EvidenceRecorder(
        target=live_config.target,
        run_id=harness.new_run_id(live_config.target),
        root=root,
        connection=live_config.public(),
        secrets=[s for s in harness.config_secrets(live_config) if s],
    )
    recorder.code = {
        "git": harness.git_state(),
        "packages": harness.package_versions(),
    }
    _STATE.update(config=live_config, recorder=recorder, check=harness.CurrentCheck())


def _check_name(item: Any) -> Optional[str]:
    name = getattr(item, "name", "")
    return name[len("test_") :] if name.startswith("test_") else None


def pytest_runtest_setup(item):
    if is_live_path(item.path) and "check" in _STATE:
        _STATE["check"].name = _check_name(item)


def pytest_runtest_logreport(report):
    recorder: Optional[harness.EvidenceRecorder] = _STATE.get("recorder")
    if recorder is None or not report.nodeid.split("::")[0].endswith("_live.py"):
        return
    check = report.nodeid.rsplit("::", 1)[-1]
    check = check[len("test_") :] if check.startswith("test_") else check
    if report.when == "call":
        recorder.outcome(check, report.outcome)
    elif report.failed:  # set-up or tear-down failed: the check did not run
        recorder.outcome(check, "error")
    if report.failed and report.longrepr is not None:
        text = str(report.longrepr)
        # Only the last lines (the assertion); the evidence search still runs.
        recorder.record(check, failure="\n".join(text.splitlines()[-12:]))


def pytest_sessionfinish(session, exitstatus):
    recorder: Optional[harness.EvidenceRecorder] = _STATE.get("recorder")
    if recorder is None:
        return
    try:
        path = recorder.write()
    except harness.EvidenceRefused as exc:
        _STATE["written"] = f"NOT WRITTEN: {exc}"
        session.exitstatus = 1
        return
    _STATE["written"] = str(path)


def pytest_terminal_summary(terminalreporter):
    written = _STATE.get("written")
    if written is None:
        return
    terminalreporter.section(MARKER)
    recorder: harness.EvidenceRecorder = _STATE["recorder"]
    terminalreporter.write_line(f"run id:   {recorder.run_id}")
    terminalreporter.write_line(f"evidence: {written}")


@pytest.fixture(scope="session")
def live_config() -> Any:
    return _STATE["config"]


@pytest.fixture(scope="session")
def recorder() -> harness.EvidenceRecorder:
    return _STATE["recorder"]


@pytest.fixture(scope="session")
def current_check() -> harness.CurrentCheck:
    return _STATE["check"]
