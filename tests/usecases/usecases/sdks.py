"""The two SDKs the cases drive, and what must hold before a driver's answers
count.

* ``python``: ``drivers/python_driver.py`` on this interpreter, loading this
  checkout's ``sdk/python``.
* ``js``: ``drivers/js_driver.mjs`` on Node ``NODE_FLOOR`` or later (the
  floor ``sdk/js/package.json``'s ``engines`` names), loading this checkout's
  ``sdk/js/dist``, which ``JS_BUILD`` builds from its sources.

**Never skipped.** No ``node`` on the PATH, a Node below the floor, or no
``sdk/js/dist/index.js`` fails the test with ``pytest.fail``: a case that
quietly stopped running its JS half would look exactly like one that passed.

**Identity.** A driver's ``hello`` names the file its SDK was loaded from,
relative to the repository root. ``require_identity`` refuses any answer
whose file is not inside that SDK's own directory of this checkout
(``sdk/python/controlvar`` or ``sdk/js/dist``), after resolving ``..`` and
symbolic links: an installed copy of the SDK would test a release, not this
change. Each driver also refuses to start on a copy that is not the
checkout's (exit status 3).

Nothing here runs at import.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from usecases import runner

REPO_ROOT = Path(__file__).resolve().parents[3]
DRIVERS = REPO_ROOT / "tests" / "usecases" / "drivers"
PYTHON_DRIVER = DRIVERS / "python_driver.py"
JS_DRIVER = DRIVERS / "js_driver.mjs"
#: Each SDK's own directory, relative to the repository root: its driver's
#: ``hello`` must name a file inside it.
HOMES = {
    "python": ("sdk", "python", "controlvar"),
    "js": ("sdk", "js", "dist"),
}
SDKS = tuple(HOMES)
#: The lowest Node major the JS SDK supports (``engines`` in sdk/js/package.json).
NODE_FLOOR = 18
#: The longest wait for ``node --version``.
NODE_SECONDS = 10.0
#: What builds this checkout's ``sdk/js/dist`` (the ``usecases-flags`` arm of
#: ``.github/workflows/_platform.yml`` runs it before the suite).
JS_BUILD = (
    "(cd sdk/js && npm ci --ignore-scripts --no-audit --no-fund"
    " && npm run build --silent)"
)
#: The exit status of a driver whose SDK is not this checkout's.
NOT_THE_CHECKOUT = 3


@dataclass(frozen=True)
class Node:
    """The ``node`` a JS driver runs on."""

    path: str
    version: str
    major: int


def require_node(search_path: str) -> Node:
    """The ``node`` first on *search_path*, once it is Node ``NODE_FLOOR`` or
    later. Anything else fails the test (``pytest.fail``), never skips it."""
    found = shutil.which("node", path=search_path)
    if found is None:
        pytest.fail(
            f"no node on the PATH: the JS driver needs Node {NODE_FLOOR} or later"
            " (sdk/js's engines). A missing Node fails the use cases; it never"
            " skips them.",
            pytrace=False,
        )
    try:
        result = subprocess.run(
            [found, "--version"],
            capture_output=True,
            text=True,
            timeout=NODE_SECONDS,
            env={"PATH": search_path},
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.fail(
            f"{found} --version did not answer ({type(exc).__name__})", pytrace=False
        )
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", result.stdout.strip())
    if result.returncode != 0 or match is None:
        pytest.fail(
            f"{found} --version answered no Node version (exit status"
            f" {result.returncode})",
            pytrace=False,
        )
    major = int(match.group(1))
    if major < NODE_FLOOR:
        pytest.fail(
            f"{found} is Node {match.group(0)}, below the JS SDK's floor of"
            f" {NODE_FLOOR} (sdk/js's engines). An old Node fails the use cases;"
            " it never skips them.",
            pytrace=False,
        )
    return Node(path=found, version=match.group(0)[1:], major=major)


def require_dist(repo_root: Path = REPO_ROOT) -> Path:
    """``sdk/js/dist/index.js`` of *repo_root*, once it exists; otherwise the
    test fails (``pytest.fail``) naming the command that builds it."""
    index = repo_root.joinpath(*HOMES["js"], "index.js")
    if not index.is_file():
        pytest.fail(
            "sdk/js/dist/index.js is missing: build this checkout's JS SDK"
            f" first, from the repository root: {JS_BUILD}. A missing build fails"
            " the use cases; it never skips them.",
            pytrace=False,
        )
    return index


def identity_problem(
    sdk: str, answer: Mapping[str, Any], repo_root: Path = REPO_ROOT
) -> str | None:
    """Why *answer*, a driver's ``hello``, does not show this checkout's
    *sdk*; ``None`` when it does."""
    if answer.get("ok") is not True or answer.get("sdk") != sdk:
        return f"the {sdk} driver's hello answered sdk {answer.get('sdk')!r}"
    module = answer.get("module")
    if not isinstance(module, str) or not module:
        return f"the {sdk} driver's hello named no module"
    home = repo_root.joinpath(*HOMES[sdk]).resolve()
    loaded = (repo_root / module).resolve()
    if not loaded.is_relative_to(home) or not loaded.is_file():
        return (
            f"the {sdk} driver loaded its SDK from {module!r}, not from a file"
            f" under {'/'.join(HOMES[sdk])}: not this checkout's SDK"
        )
    return None


def require_identity(
    sdk: str, answer: Mapping[str, Any], repo_root: Path = REPO_ROOT
) -> None:
    """Fail the test unless *answer* shows this checkout's *sdk*."""
    problem = identity_problem(sdk, answer, repo_root)
    if problem is not None:
        pytest.fail(problem, pytrace=False)


def driver_argv(sdk: str, node: Node | None = None) -> list[str]:
    """The command that starts *sdk*'s driver."""
    if sdk == "python":
        return [sys.executable, "-B", str(PYTHON_DRIVER)]
    if sdk == "js":
        if node is None:
            raise ValueError("the JS driver needs the node require_node found")
        return [node.path, str(JS_DRIVER)]
    raise ValueError(f"not an SDK the cases drive: {sdk!r}")


@contextmanager
def driver(
    sdk: str,
    *,
    node: Node | None = None,
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = runner.DEFAULT_TIMEOUT,
    secrets: Sequence[str] = (),
) -> Iterator[tuple[runner.Runner, dict[str, Any]]]:
    """*sdk*'s driver, started, its ``hello`` answered and its identity
    checked: ``(runner, hello)``. Stopped (and reaped) on the way out."""
    session = runner.Runner(
        list(argv) if argv is not None else driver_argv(sdk, node),
        env=env,
        timeout=timeout,
        secrets=secrets,
    )
    session.start()
    try:
        hello = session.call("hello")
        require_identity(sdk, hello)
        yield session, hello
    finally:
        session.stop()
