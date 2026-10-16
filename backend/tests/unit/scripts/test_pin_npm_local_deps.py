"""`scripts/pin_npm_local_deps.py` -- the published package.json installs from npm.

``sdk/openfeature`` depends on ``@controlvar/js-sdk`` as ``file:../js``.
``npm pack`` copies that spec into the tarball and ``npm publish`` does not
resolve it, so the provider as released would install with a dangling link
(#687). The release job runs this script before packing. These tests run it
over copies of the real manifests and over planted ones.

No git and no npm anywhere: everything is a file under ``tmp_path`` or a read of
the tree, so this runs in ``scripts/core_build.sh``'s copy too.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "pin_npm_local_deps.py"
SDK_DIR = REPO_ROOT / "sdk"

_spec = importlib.util.spec_from_file_location("pin_npm_local_deps", SCRIPT)
pin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pin)


def _run(*args: str | Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)],
        capture_output=True,
        text=True,
    )


def _npm_sdks() -> list[str]:
    return sorted(p.parent.name for p in SDK_DIR.glob("*/package.json"))


def _write(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A copy of sdk/js and sdk/openfeature manifests, side by side."""
    for name in ("js", "openfeature"):
        (tmp_path / name).mkdir()
        shutil.copy(SDK_DIR / name / "package.json", tmp_path / name / "package.json")
        shutil.copy(
            SDK_DIR / name / "package-lock.json", tmp_path / name / "package-lock.json"
        )
    return tmp_path


# --------------------------------------------------------------------------
# The real provider manifest
# --------------------------------------------------------------------------


def test_the_provider_is_pinned_to_the_js_sdk_version_on_this_ref(tree: Path) -> None:
    js_version = json.loads((SDK_DIR / "js" / "package.json").read_text())["version"]
    rewrites = tree / "rewrites.txt"

    result = _run(tree / "openfeature", "--rewrites-file", rewrites)

    assert result.returncode == 0, result.stderr
    published = json.loads((tree / "openfeature" / "package.json").read_text())
    assert published["dependencies"]["@controlvar/js-sdk"] == f"^{js_version}"
    assert rewrites.read_text() == f"@controlvar/js-sdk@{js_version}\n"
    assert f"@controlvar/js-sdk: file:../js -> ^{js_version}" in result.stdout
    # Everything else in the manifest is untouched, in the same order.
    source = json.loads((SDK_DIR / "openfeature" / "package.json").read_text())
    source["dependencies"]["@controlvar/js-sdk"] = f"^{js_version}"
    assert list(published) == list(source)
    assert published == source


def test_the_unpinned_provider_manifest_fails_the_tarball_check() -> None:
    """The defect #687 shipped: the source manifest, as `npm pack` copies it."""
    result = _run("--check-only", SDK_DIR / "openfeature" / "package.json")
    assert result.returncode == 1
    assert "@controlvar/js-sdk = 'file:../js'" in result.stderr


def test_the_provider_declares_its_repository() -> None:
    data = json.loads((SDK_DIR / "openfeature" / "package.json").read_text())
    assert (
        data["repository"]["url"]
        == "git+https://github.com/getexperimently/experimently.git"
    )
    assert data["repository"]["directory"] == "sdk/openfeature"


def test_the_provider_lock_records_the_js_sdk_version_in_the_tree() -> None:
    lock = json.loads((SDK_DIR / "openfeature" / "package-lock.json").read_text())
    js = json.loads((SDK_DIR / "js" / "package.json").read_text())
    assert lock["packages"]["../js"]["version"] == js["version"]


def test_peer_pins_come_from_the_lock(tree: Path) -> None:
    lock = json.loads((tree / "openfeature" / "package-lock.json").read_text())
    version = lock["packages"]["node_modules/@openfeature/server-sdk"]["version"]
    result = _run("--print-peer-pins", tree / "openfeature")
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"@openfeature/server-sdk@{version}\n"


def test_a_peer_missing_from_the_lock_is_refused(tree: Path) -> None:
    lock_path = tree / "openfeature" / "package-lock.json"
    lock = json.loads(lock_path.read_text())
    del lock["packages"]["node_modules/@openfeature/server-sdk"]
    _write(lock_path, lock)
    result = _run("--print-peer-pins", tree / "openfeature")
    assert result.returncode == 1
    assert "@openfeature/server-sdk" in result.stderr


# --------------------------------------------------------------------------
# Every npm SDK, as the release job would pin it (QA 687-6, EM C-A6)
# --------------------------------------------------------------------------


def test_the_npm_sdks_are_found() -> None:
    """Not vacuous: the five SDKs the npm job publishes are all walked."""
    assert {"js", "edge", "openfeature", "react", "react-native"} <= set(_npm_sdks())


@pytest.mark.parametrize("sdk", _npm_sdks())
def test_every_npm_sdk_pins_to_registry_ranges_only(sdk: str) -> None:
    """What each SDK would publish has no local spec left in it. A new
    ``link:``/``workspace:``/git dependency anywhere fails here, naming it."""
    data = json.loads((SDK_DIR / sdk / "package.json").read_text())
    pinned, rewrites = pin.pin_manifest(data, SDK_DIR / sdk)
    pin.check_registry_only(pinned, f"sdk/{sdk}/package.json (pinned)")
    if sdk != "openfeature":
        # The no-op path: nothing rewritten, so the release job's `npm view`
        # step is skipped and the manifest is published as it is.
        assert rewrites == []
        assert pinned == data


@pytest.mark.parametrize("sdk", [s for s in _npm_sdks() if s != "openfeature"])
def test_the_no_op_path_writes_nothing(sdk: str, tmp_path: Path) -> None:
    copy = tmp_path / sdk
    copy.mkdir()
    original = (SDK_DIR / sdk / "package.json").read_bytes()
    (copy / "package.json").write_bytes(original)
    rewrites = tmp_path / "rewrites.txt"

    result = _run(copy, "--rewrites-file", rewrites)

    assert result.returncode == 0, result.stderr
    assert (copy / "package.json").read_bytes() == original
    assert rewrites.read_text() == ""
    assert _run("--check-only", copy / "package.json").returncode == 0


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def _provider(tree: Path, deps: dict, section: str = "dependencies") -> Path:
    path = tree / "openfeature" / "package.json"
    data = json.loads(path.read_text())
    data["dependencies"] = {}
    data[section] = deps
    _write(path, data)
    return path


def test_a_missing_target_is_refused(tree: Path) -> None:
    path = _provider(tree, {"@controlvar/js-sdk": "file:../nonexistent"})
    before = path.read_bytes()
    result = _run(tree / "openfeature", "--rewrites-file", tree / "rw.txt")
    assert result.returncode == 1
    assert "has no package.json" in result.stderr
    assert path.read_bytes() == before
    assert not (tree / "rw.txt").exists()


def test_a_name_mismatch_is_refused(tree: Path) -> None:
    _write(
        tree / "react" / "package.json",
        {"name": "@controlvar/react-sdk", "version": "1.1.0"},
    )
    _provider(tree, {"@controlvar/js-sdk": "file:../react"})
    result = _run(tree / "openfeature")
    assert result.returncode == 1
    assert (
        "'@controlvar/react-sdk', not '@controlvar/js-sdk'" in result.stderr
    )


@pytest.mark.parametrize("version", [None, "", "1.0", "latest", "1.0.0+build"])
def test_a_target_without_a_plain_version_is_refused(
    tree: Path, version: object
) -> None:
    target = {"name": "@controlvar/js-sdk"}
    if version is not None:
        target["version"] = version
    _write(tree / "js" / "package.json", target)
    result = _run(tree / "openfeature")
    assert result.returncode == 1
    assert "no plain version" in result.stderr


@pytest.mark.parametrize("section", ["optionalDependencies", "peerDependencies"])
def test_the_other_published_sections_are_rewritten(tree: Path, section: str) -> None:
    _provider(tree, {"@controlvar/js-sdk": "file:../js"}, section)
    result = _run(tree / "openfeature")
    assert result.returncode == 0, result.stderr
    data = json.loads((tree / "openfeature" / "package.json").read_text())
    assert data[section]["@controlvar/js-sdk"].startswith("^")


def test_dev_dependencies_are_left_alone(tree: Path) -> None:
    path = tree / "openfeature" / "package.json"
    data = json.loads(path.read_text())
    data["devDependencies"]["local-tool"] = "file:../js"
    _write(path, data)
    result = _run(tree / "openfeature")
    assert result.returncode == 0, result.stderr
    assert json.loads(path.read_text())["devDependencies"]["local-tool"] == "file:../js"


@pytest.mark.parametrize(
    "spec",
    [
        "git+https://github.com/getexperimently/experimently.git",
        "github:getexperimently/experimently",
        "getexperimently/experimently",
        "workspace:*",
        "link:../js",
        "npm:other@1.0.0",
        "https://example.com/x.tgz",
        "../js",
        "./vendor/x.tgz",
        "x.tgz",
        "",
    ],
)
def test_a_non_registry_spec_is_refused(tree: Path, spec: str) -> None:
    path = _provider(tree, {"something": spec})
    before = path.read_bytes()
    result = _run(tree / "openfeature")
    assert result.returncode == 1, result.stdout
    assert "something" in result.stderr
    assert path.read_bytes() == before
    assert _run("--check-only", path).returncode == 1


@pytest.mark.parametrize(
    "spec",
    ["^0.1.0", "~1.2.3", ">=1.0.0 <2.0.0", "1.x", "*", "latest", "1.0.0 || ^2.0.0"],
)
def test_registry_ranges_pass(spec: str) -> None:
    assert pin.registry_spec_problem(spec) is None


def test_check_only_on_a_missing_file_fails(tmp_path: Path) -> None:
    result = _run("--check-only", tmp_path / "package" / "package.json")
    assert result.returncode == 1
    assert "does not exist" in result.stderr


# --------------------------------------------------------------------------
# It runs on the runner's python3 (EM C-A1)
# --------------------------------------------------------------------------


def test_the_script_imports_only_the_standard_library() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    third_party = imported - set(sys.stdlib_module_names) - {"__future__"}
    assert not third_party, f"the release job runs this on bare python3: {third_party}"
