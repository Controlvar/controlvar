"""Run one deterministic slice of a pytest session, and record what it ran.

    EXPERIMENTLY_TEST_SHARD=2/4 \\
    EXPERIMENTLY_TEST_SHARD_REPORT=/abs/path/shard-integration-2.json \\
        python -m pytest backend/tests/integration

N sessions given ``1/N`` .. ``N/N`` run every collected test exactly once
between them: a test belongs to shard ``i`` when ``sha256(nodeid) % N == i-1``.
Hashing the node id needs no durations file, moves no other test when one is
added, and spreads a slow file across shards.

Nothing here can prove the partition on its own, so each sharded session writes
a JSON report and ``scripts/check_test_shards.py`` compares the reports of all
the shards. A report holds:

* ``version``, ``suite`` (the session's positional arguments, made relative to
  the rootdir and sorted), ``shard`` and ``of``;
* ``collected``: every node id this session collected, sorted. Every shard must
  have collected the same list, or a node id that differs between processes (a
  parametrize id built from ``uuid4()``) would put a test in no shard, or two;
* ``selected``: the node ids this shard kept;
* what narrowed the session before the shard took its slice: ``markexpr``,
  ``keyword``, ``deselect``, ``ignore``, ``ignore_glob``, ``last_failed`` and
  ``pre_deselected``, the number of items any other plugin deselected (``-m``,
  ``-k``, ``--deselect``; ``--lf`` on a file named on the command line, which
  deselects after this hook runs). Over a directory ``--lf`` drops the passing
  tests while collecting instead, so only ``last_failed`` shows it. Also two
  lists checked against the items ``pytest_itemcollected`` saw (it fires before
  any ``pytest_collection_modifyitems``), compared by item object, not count:
  ``unannounced``, items collected that are neither present at this hook nor
  deselected before it (a conftest's ``del items[...]``), and ``fabricated``,
  items present at this hook that were never collected (one a conftest built
  with ``Function.from_parent`` and appended, even under a real node id); and
  ``disabled_plugins`` (every ``-p no:NAME``, from the command line, ini
  ``addopts`` or ``PYTEST_ADDOPTS``) and ``autoload_disabled``, because
  disabling a plugin that generates tests shrinks every shard alike. N shards
  that each agree on a narrowed set would otherwise prove a partition of the
  wrong thing. What none of this sees: a change to the tree itself (a
  ``collect_ignore`` entry, a deleted file, a generator yielding fewer
  parameters), which is reviewed as code, not caught here;
* ``collect_errors``: the node ids of collectors that failed;
* ``ran``: one record per executed item, from ``pytest_runtest_logreport``:
  ``nodeid``, ``outcome`` (passed, failed, skipped, xfailed, xpassed, error)
  and ``skip_reason``. The call phase decides the outcome, or the setup phase
  when setup skipped or failed; a failing teardown makes it ``error``.

Contract:

* Both variables unset or empty: nothing happens, and no report is written.
* ``EXPERIMENTLY_TEST_SHARD`` must be ``i/n`` (``[1-9][0-9]{0,2}``, ``1 <= i <=
  n``), with nothing around it. Anything else is a usage error (exit 4).
* A spec needs ``EXPERIMENTLY_TEST_SHARD_REPORT``: an absolute path to a file
  that does not exist yet, in a directory that does. It is created when the
  session starts (opened ``"x"``), so a second sharded session in the same job
  cannot overwrite the first one's report; a session killed before it finishes
  leaves the file empty, which the checker refuses. A report path without a
  spec is a usage error too: it means a job lost its spec.
* A shard that selects nothing is a usage error, named, rather than pytest's
  bare exit 5.
* Once read, both variables are taken out of ``os.environ`` until the session
  ends, then put back. They describe this session only. A test that starts
  pytest again (a collection count, a conftest check, a child run over another
  tree) with a copy of ``os.environ`` handed them to the child, which found
  this session's report already created and refused, exit 4 (#1895). Taken
  out, they reach no process the session starts.

Loaded by the repository-root conftest for every session rooted there, so it
must stay importable with only pytest installed (tests/sdk-contract runs that
way). Disable it with ``-p no:backend.tests.shard``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import pytest

ENV_SHARD = "EXPERIMENTLY_TEST_SHARD"
ENV_REPORT = "EXPERIMENTLY_TEST_SHARD_REPORT"
REPORT_VERSION = 1

_SPEC = re.compile(r"([1-9][0-9]{0,2})/([1-9][0-9]{0,2})")
_SKIPPED_PREFIX = "Skipped: "


def parse_spec(spec: str) -> tuple[int, int]:
    """Return ``(i, n)`` for an exact ``i/n`` with ``1 <= i <= n``, or raise."""
    match = _SPEC.fullmatch(spec)
    if match is None:
        raise pytest.UsageError(
            f"{ENV_SHARD}={spec!r} is not i/n (for example 2/4, nothing around it)"
        )
    index, total = int(match.group(1)), int(match.group(2))
    if index > total:
        raise pytest.UsageError(f"{ENV_SHARD}={spec!r}: shard {index} of {total}")
    return index, total


def shard_of(nodeid: str, total: int) -> int:
    """The 1-based shard a node id belongs to, out of ``total``."""
    digest = hashlib.sha256(nodeid.encode("utf-8", "surrogatepass")).digest()
    return int.from_bytes(digest, "big") % total + 1


def _suite(config: pytest.Config) -> str:
    root = Path(config.rootpath).resolve()
    here = Path(config.invocation_params.dir)
    parts = []
    for arg in config.args:
        path, sep, rest = str(arg).partition("::")
        resolved = (here / path).resolve()
        try:
            shown = resolved.relative_to(root).as_posix()
        except ValueError:
            shown = resolved.as_posix()
        parts.append(shown + sep + rest)
    return " ".join(sorted(parts))


def _skip_reason(report: pytest.TestReport) -> str:
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        message = str(longrepr[2])
    else:
        message = str(longrepr)
    if message.startswith(_SKIPPED_PREFIX):
        message = message[len(_SKIPPED_PREFIX) :]
    return message


def _outcome(report: pytest.TestReport) -> str | None:
    """The outcome one phase decides, or None when it decides nothing."""
    expected_failure = hasattr(report, "wasxfail")
    if report.when == "call":
        if report.passed:
            return "xpassed" if expected_failure else "passed"
        if report.skipped:
            return "xfailed" if expected_failure else "skipped"
        return "failed"
    if report.failed:
        return "error"
    if report.when == "setup" and report.skipped:
        return "xfailed" if expected_failure else "skipped"
    return None


class _ShardSession:
    """The state of one sharded session; registered only when a spec is set."""

    def __init__(self, index: int, total: int, report) -> None:
        self.index = index
        self.total = total
        self._report = report
        self._selecting = False
        self.selection_done = False
        self.pre_deselected = 0
        # Item objects, not node ids: a Node hashes by its node id but compares
        # by identity, so a substitute under a real node id is still new.
        self._itemcollected: set = set()
        self._deselected_before_hook: set = set()
        self.unannounced: list[str] = []
        self.fabricated: list[str] = []
        self.collected: list[str] = []
        self.selected: list[str] = []
        self.collect_errors: list[str] = []
        self.ran: list[dict] = []
        self._open: dict[str, dict] = {}

    def pytest_deselected(self, items) -> None:
        if not self._selecting:
            self.pre_deselected += len(items)
            if not self.selection_done:
                self._deselected_before_hook.update(items)

    def pytest_itemcollected(self, item) -> None:
        self._itemcollected.add(item)

    def pytest_collectreport(self, report) -> None:
        if report.failed:
            self.collect_errors.append(report.nodeid)

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config, items) -> None:
        self.collected = sorted(item.nodeid for item in items)
        present = set(items)
        self.fabricated = sorted(i.nodeid for i in present - self._itemcollected)
        self.unannounced = sorted(
            i.nodeid
            for i in self._itemcollected - present - self._deselected_before_hook
        )
        keep, drop = [], []
        for item in items:
            if shard_of(item.nodeid, self.total) == self.index:
                keep.append(item)
            else:
                drop.append(item)
        if not keep:
            raise pytest.UsageError(
                f"{ENV_SHARD}={self.index}/{self.total} selected none of the "
                f"{len(items)} collected tests"
            )
        if drop:
            self._selecting = True
            try:
                config.hook.pytest_deselected(items=drop)
            finally:
                self._selecting = False
        items[:] = keep
        self.selected = sorted(item.nodeid for item in keep)
        self.selection_done = True

    def pytest_runtest_logreport(self, report) -> None:
        record = self._open.get(report.nodeid)
        if report.when == "setup" or record is None:
            record = {"nodeid": report.nodeid, "outcome": None, "skip_reason": None}
            self.ran.append(record)
            self._open[report.nodeid] = record
        outcome = _outcome(report)
        if outcome is None:
            return
        if record["outcome"] is None or outcome == "error":
            record["outcome"] = outcome
            record["skip_reason"] = (
                _skip_reason(report) if outcome == "skipped" else None
            )

    def write(self, config: pytest.Config) -> None:
        option = config.option
        document = {
            "version": REPORT_VERSION,
            "suite": _suite(config),
            "shard": self.index,
            "of": self.total,
            "collected": self.collected,
            "selected": self.selected,
            "markexpr": getattr(option, "markexpr", "") or "",
            "keyword": getattr(option, "keyword", "") or "",
            "deselect": [str(d) for d in (getattr(option, "deselect", None) or [])],
            "ignore": [str(i) for i in (getattr(option, "ignore", None) or [])],
            "ignore_glob": [
                str(i) for i in (getattr(option, "ignore_glob", None) or [])
            ],
            "last_failed": bool(getattr(option, "lf", False)),
            "pre_deselected": self.pre_deselected,
            "unannounced": self.unannounced,
            "fabricated": self.fabricated,
            "disabled_plugins": sorted(
                str(p)[3:]
                for p in (getattr(option, "plugins", None) or [])
                if str(p).startswith("no:")
            ),
            "autoload_disabled": bool(
                getattr(option, "disable_plugin_autoload", False)
                or os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD")
            ),
            "collect_errors": sorted(self.collect_errors),
            "ran": self.ran,
        }
        with self._report:
            json.dump(document, self._report, indent=1)
            self._report.write("\n")

    def close_unwritten(self) -> None:
        # The selection never happened (a refusal, or an interrupted
        # collection): leave the claimed file empty, which the checker refuses.
        self._report.close()


_KEY = pytest.StashKey[_ShardSession]()


def pytest_configure(config: pytest.Config) -> None:
    spec = os.environ.get(ENV_SHARD, "")
    report_path = os.environ.get(ENV_REPORT, "")
    if not spec and not report_path:
        return
    # Put back when the session ends, so an in-process session (pytester's
    # inline_run) hands its caller the environment it was given.
    withheld = pytest.MonkeyPatch()
    withheld.delenv(ENV_SHARD, raising=False)
    withheld.delenv(ENV_REPORT, raising=False)
    config.add_cleanup(withheld.undo)
    if not spec:
        raise pytest.UsageError(
            f"{ENV_REPORT} is set but {ENV_SHARD} is not: a sharded job lost its spec"
        )
    index, total = parse_spec(spec)
    if not report_path:
        raise pytest.UsageError(
            f"{ENV_SHARD}={spec} needs {ENV_REPORT}, the report the checker reads"
        )
    if not os.path.isabs(report_path):
        raise pytest.UsageError(f"{ENV_REPORT}={report_path!r} is not absolute")
    try:
        handle = open(report_path, "x", encoding="utf-8")
    except FileExistsError:
        raise pytest.UsageError(
            f"{ENV_REPORT}={report_path} already exists; one report per session"
        ) from None
    except OSError as exc:
        raise pytest.UsageError(
            f"{ENV_REPORT}={report_path} cannot be created: {exc}"
        ) from None
    state = _ShardSession(index, total, handle)
    config.stash[_KEY] = state
    config.pluginmanager.register(state, "backend.tests.shard.session")


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session) -> None:
    state = session.config.stash.get(_KEY, None)
    if state is None:
        return
    if state.selection_done:
        state.write(session.config)
    else:
        state.close_unwritten()
