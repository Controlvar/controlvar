"""What must hold before a driver's answers count (``usecases/sdks.py``),
checked with no Node: the unit job has none, and must not depend on whatever
``node`` the runner image happens to carry.

* **The Node floor.** A fake ``node`` on a PATH the test controls: no
  ``node`` fails, Node 16 fails, an answer that is not a version fails, Node 18
  proceeds. "Fails" means ``pytest.fail``: each check catches whatever is
  raised and asserts it is a failure, so a skip in its place turns these red.
* **The build.** No ``sdk/js/dist/index.js`` fails, naming the command that
  builds it.
* **Identity, the runner's side.** A driver whose ``hello`` names a file
  outside the SDK's own directory of the checkout is refused: an installed
  copy, a path climbing out with ``..``, a sibling directory sharing the
  prefix, an absolute path, a file that is not there.

The drivers' own refusals (exit status 3) are gated where each can run: the
Python driver's in ``test_python_driver.py``, the JS driver's beside the cases
(``tests/usecases/test_js_driver.py``).
"""

from __future__ import annotations

import json
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from backend.tests.unit.usecases.harness import REPO_ROOT, sdks

pytestmark = pytest.mark.unit

#: A driver that answers hello naming an installed copy of the JS SDK.
INSTALLED_COPY = (
    "import json, sys\n"
    "sys.stdin.readline()\n"
    "print(json.dumps({'ok': True, 'sdk': 'js', 'version': '0.2.0',\n"
    "    'module': 'node_modules/@controlvar/js-sdk/dist/index.js',\n"
    "    'capabilities': ['local-eval', 'remote-eval']}), flush=True)\n"
    "sys.stdin.readline()\n"
)


def _raised(call: Callable[[], object]) -> BaseException | None:
    """What *call* raised, whatever it was (``pytest.fail`` and
    ``pytest.skip`` both raise ``BaseException`` subclasses), or ``None``."""
    try:
        call()
    except BaseException as exc:  # the type is what the caller checks
        return exc
    return None


def _assert_failed(raised: BaseException | None, fragment: str) -> None:
    assert type(raised) is pytest.fail.Exception, (
        f"expected pytest.fail, got {type(raised).__name__}: {raised}"
    )
    assert fragment in str(raised), str(raised)


def _fake_node(tmp_path: Path, answer: str, status: int = 0) -> str:
    """A directory holding only a ``node`` that prints *answer* and exits
    *status*: the PATH a check searches."""
    directory = tmp_path / "bin"
    directory.mkdir()
    script = directory / "node"
    script.write_text(f"#!/bin/sh\necho '{answer}'\nexit {status}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(directory)


# -- the Node floor ----------------------------------------------------------------------
def test_no_node_on_the_path_fails(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    _assert_failed(
        _raised(lambda: sdks.require_node(str(empty))), "no node on the PATH"
    )


def test_node_16_fails(tmp_path):
    path = _fake_node(tmp_path, "v16.20.2")
    _assert_failed(_raised(lambda: sdks.require_node(path)), "Node v16.20.2, below")


@pytest.mark.parametrize(
    ("answer", "status"), [("not a version", 0), ("v18.0.0", 1), ("v18", 0)]
)
def test_a_node_that_answers_no_version_fails(tmp_path, answer, status):
    path = _fake_node(tmp_path, answer, status)
    _assert_failed(_raised(lambda: sdks.require_node(path)), "answered no Node version")


def test_node_18_proceeds(tmp_path):
    path = _fake_node(tmp_path, "v18.0.0")
    found = sdks.require_node(path)
    assert (found.version, found.major) == ("18.0.0", 18)
    assert found.path == str(Path(path) / "node")


def test_the_floor_is_the_js_sdk_s_engines_floor():
    package = json.loads(
        (REPO_ROOT / "sdk" / "js" / "package.json").read_text(encoding="utf-8")
    )
    assert package["engines"]["node"] == f">={sdks.NODE_FLOOR}"


# -- the build -----------------------------------------------------------------------------
def test_no_built_sdk_fails_naming_the_build(tmp_path):
    _assert_failed(_raised(lambda: sdks.require_dist(tmp_path)), sdks.JS_BUILD)


def test_a_built_sdk_proceeds(tmp_path):
    index = tmp_path / "sdk" / "js" / "dist" / "index.js"
    index.parent.mkdir(parents=True)
    index.write_text("", encoding="utf-8")
    assert sdks.require_dist(tmp_path) == index


# -- identity, the runner's side -------------------------------------------------------
def _hello(sdk: str, module: object) -> dict[str, object]:
    return {"ok": True, "sdk": sdk, "version": "0", "module": module}


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A checkout holding both SDKs' entry files, and an installed copy."""
    for parts in (
        ("sdk", "js", "dist", "index.js"),
        ("sdk", "python", "controlvar", "__init__.py"),
        ("sdk", "js", "dist-old", "index.js"),
        ("node_modules", "@controlvar", "js-sdk", "dist", "index.js"),
    ):
        path = tmp_path.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("sdk", "module"),
    [("js", "sdk/js/dist/index.js"), ("python", "sdk/python/controlvar/__init__.py")],
)
def test_the_checkout_s_sdk_is_accepted(tree, sdk, module):
    assert sdks.identity_problem(sdk, _hello(sdk, module), tree) is None


@pytest.mark.parametrize(
    "module",
    [
        "node_modules/@controlvar/js-sdk/dist/index.js",
        "sdk/js/dist/../../../node_modules/@controlvar/js-sdk/dist/index.js",
        "sdk/js/dist-old/index.js",
        "sdk/js/dist/missing.js",
        "sdk/js/dist",
        "",
        None,
    ],
)
def test_a_module_outside_sdk_js_dist_is_refused(tree, module):
    problem = sdks.identity_problem("js", _hello("js", module), tree)
    assert problem is not None
    _assert_failed(
        _raised(lambda: sdks.require_identity("js", _hello("js", module), tree)),
        problem,
    )


def test_an_absolute_path_is_refused(tree):
    module = str(tree / "node_modules" / "@controlvar" / "js-sdk" / "dist" / "index.js")
    assert sdks.identity_problem("js", _hello("js", module), tree) is not None


def test_a_hello_for_another_sdk_is_refused(tree):
    assert "answered sdk 'python'" in str(
        sdks.identity_problem("js", _hello("python", "sdk/js/dist/index.js"), tree)
    )


def test_a_driver_naming_an_installed_copy_is_refused_before_its_first_command():
    argv = [sys.executable, "-B", "-c", INSTALLED_COPY]
    reached: list[object] = []

    def run() -> None:
        with sdks.driver("js", argv=argv, timeout=10.0) as (session, _):
            reached.append(session)

    _assert_failed(_raised(run), "node_modules/@controlvar/js-sdk/dist/index.js")
    assert reached == []


def test_the_drivers_are_the_harness_s():
    assert sdks.driver_argv("python")[-1] == str(sdks.PYTHON_DRIVER)
    node = sdks.Node(path="/x/node", version="22.0.0", major=22)
    assert sdks.driver_argv("js", node) == ["/x/node", str(sdks.JS_DRIVER)]
    assert sdks.JS_DRIVER.is_file() and sdks.PYTHON_DRIVER.is_file()
    with pytest.raises(ValueError):
        sdks.driver_argv("js")
    with pytest.raises(ValueError):
        sdks.driver_argv("go")
