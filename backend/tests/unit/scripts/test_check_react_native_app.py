"""scripts/check_react_native_app.py: an app on each AsyncStorage major installs
the packed React Native SDK alone, keeps its one AsyncStorage, and type-checks.

The script's real run needs npm and the registry (sdk-unit-tests.yml's
react-native leg and sdk-release.yml's npm-build run it). Here it runs against
a fake ``npm`` that lays out ``node_modules`` the way npm would for each
command, and a fake ``tsc``; each plant makes that fake behave like one defect
the script exists to catch, and the script must fail on it, on every leg it
applies to and no other:

* the documented install moving the app's AsyncStorage to the newest major
  (an install line that names AsyncStorage: EMP-P-8's C6 plant);
* a second copy of AsyncStorage nested under the SDK (AsyncStorage as a
  dependency instead of a peer);
* ``tsc`` failing under 3.x only (a bare ``multiRemove``: TS2339);
* an install that fails.

The constants are held to the SDK's own files: one AsyncStorage version per
major the peer range in package.json allows, the template config of the React
Native minor the lock tests with, and the lock's versions of the app's own
packages.

Reads the tree and writes only under tmp_path; standard library and the script.
"""

from __future__ import annotations

import json
import os
import re
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import List

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "check_react_native_app.py"
SDK = ROOT / "sdk" / "react-native"
STORAGE_PACKAGE = "@react-native-async-storage/async-storage"


def _script():
    spec = spec_from_file_location("check_react_native_app", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# The constants, held to the SDK's files
# --------------------------------------------------------------------------


def _peer_range() -> str:
    return _json(SDK / "package.json")["peerDependencies"][STORAGE_PACKAGE]


def _version(text: str) -> tuple:
    return tuple(int(part) for part in text.split("."))


def test_one_asyncstorage_version_per_major_the_peer_range_allows() -> None:
    """The legs follow the peer range: a range that admits a 4th major, or
    drops one, fails here until STORAGE follows it."""
    match = re.fullmatch(r">=(\d+\.\d+\.\d+) <(\d+)", _peer_range())
    assert match, f"read only a `>=x.y.z <N` peer range, not {_peer_range()!r}"
    floor, ceiling = _version(match.group(1)), int(match.group(2))
    storage = _script().STORAGE
    majors = [_version(v)[0] for v in storage]
    assert majors == list(range(floor[0], ceiling)), storage
    assert all(floor <= _version(v) for v in storage), storage
    assert _script().STORAGE_PACKAGE == STORAGE_PACKAGE


def test_the_template_config_is_the_react_native_minor_the_lock_tests_with() -> None:
    lock = _json(SDK / "package-lock.json")
    react_native = lock["packages"]["node_modules/react-native"]["version"]
    config = _script().CONFIG
    assert config.startswith("@react-native/typescript-config@"), config
    assert config.rsplit("@", 1)[1].split(".")[:2] == react_native.split(".")[:2]


def test_the_apps_own_packages_are_the_versions_the_lock_pins() -> None:
    packages = _json(SDK / "package-lock.json")["packages"]
    expected = [
        f"{name}@{packages[f'node_modules/{name}']['version']}"
        for name in (
            "react",
            "react-native",
            "@types/react",
            "@types/jest",
            "typescript",
        )
    ]
    assert _script().lock_pins() == expected


def test_a_lock_without_one_of_them_is_refused(tmp_path: Path) -> None:
    lock = _json(SDK / "package-lock.json")
    del lock["packages"]["node_modules/typescript"]
    (tmp_path / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(ValueError, match="records no typescript"):
        _script().lock_pins(tmp_path)


def test_the_documented_install_is_the_tarball_alone() -> None:
    """The SDK page's install is `npm install <tgz>` with nothing else on the
    line (backend/tests/unit/docs/test_react_native_package.py holds the
    page to that); each leg runs exactly that after moving the app."""
    module = _script()
    for version in module.STORAGE:
        move, documented = module.leg_args("/x/sdk.tgz", version)
        assert move == [*module.INSTALL, f"{STORAGE_PACKAGE}@{version}"]
        assert documented == [*module.INSTALL, "/x/sdk.tgz"]
    assert "--ignore-scripts" in module.INSTALL


def test_print_plan_names_every_command(capsys) -> None:
    assert _script().main(["--print-plan", "pkg.tgz"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 + 2 * 3, lines
    assert lines[0].startswith("the app: npm install --ignore-scripts")
    assert (
        lines[-1]
        == "AsyncStorage 3.1.1: npm install --ignore-scripts --no-audit --no-fund pkg.tgz"
    )


def test_storage_copies_finds_nested_and_scoped_copies(tmp_path: Path) -> None:
    def put(path: str, version: str) -> None:
        manifest = tmp_path / path / "package.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"version": version}), encoding="utf-8")

    put(f"node_modules/{STORAGE_PACKAGE}", "2.2.0")
    put("node_modules/react", "18.2.0")
    put(
        f"node_modules/@controlvar/react-native-sdk/node_modules/{STORAGE_PACKAGE}",
        "1.24.0",
    )
    put(f"node_modules/a/node_modules/b/node_modules/{STORAGE_PACKAGE}", "3.1.1")
    assert _script().storage_copies(tmp_path) == ["1.24.0", "2.2.0", "3.1.1"]


# --------------------------------------------------------------------------
# The run, against a fake npm and tsc
# --------------------------------------------------------------------------

#: A fake npm. `install` lays out what npm would: each `name@version` in
#: node_modules and in package.json's dependencies (AsyncStorage as `^x`),
#: a tarball as the SDK, and `typescript` brings a `tsc` that fails when
#: FAKE_TSC_FAILS names the installed AsyncStorage major. `pack` writes a
#: tarball. FAKE_NPM_PLANT makes the tarball install misbehave: `names-storage`
#: also installs AsyncStorage's newest version as the app's own (an install
#: line naming it), `nested` puts a second copy under the SDK, `fail` exits 1;
#: `setup-fails` fails the app's first install.
FAKE_NPM = r"""
import json, os, pathlib, sys

STORAGE = "@react-native-async-storage/async-storage"
args = sys.argv[1:]
with open(os.environ["FAKE_NPM_LOG"], "a", encoding="utf-8") as log:
    log.write(" ".join(args) + "\n")
plant = os.environ.get("FAKE_NPM_PLANT", "")
app = pathlib.Path.cwd()

if args[:2] == ["pack", "--pack-destination"]:
    (pathlib.Path(args[2]) / "controlvar-react-native-sdk-0.1.0.tgz").write_bytes(b"tgz")
    sys.exit(0)
assert args[0] == "install", args
specs = [a for a in args[1:] if not a.startswith("--")]
manifest_path = app / "package.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
deps = manifest.setdefault("dependencies", {})

def put(directory, version):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "package.json").write_text(json.dumps({"version": version}))

def install(name, version, spec):
    put(app / "node_modules" / name, version)
    deps[name] = spec

if plant == "setup-fails" and any(s.startswith("react@") for s in specs):
    sys.exit(1)
for spec in specs:
    if spec.endswith(".tgz"):
        if plant == "fail":
            sys.exit(1)
        install("@controlvar/react-native-sdk", "0.1.0", "file:" + spec)
        if plant == "names-storage":
            install(STORAGE, "3.1.1", "^3.1.1")
        if plant == "nested":
            put(app / "node_modules/@controlvar/react-native-sdk/node_modules" / STORAGE, "1.24.0")
        continue
    name, version = spec.rsplit("@", 1)
    install(name, version, "^" + version if name == STORAGE else version)
    if name == "typescript":
        bin_dir = app / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "fake_tsc.py").write_text(
            "import json, os, pathlib, sys\n"
            f"m = pathlib.Path({str(app)!r}) / 'node_modules' / {STORAGE!r} / 'package.json'\n"
            "major = json.loads(m.read_text())['version'].split('.')[0]\n"
            "if major in os.environ.get('FAKE_TSC_FAILS', '').split(','):\n"
            "    print(\"src/storage.ts(81,28): error TS2339: Property 'multiRemove' does not exist on type 'AsyncStorage'.\")\n"
            "    sys.exit(2)\n"
        )
        tsc = bin_dir / "tsc"
        tsc.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "fake_tsc.py"}" "$@"\n')
        tsc.chmod(0o755)
manifest_path.write_text(json.dumps(manifest, indent=2))
"""


def fake_npm(directory: Path) -> Path:
    """A fake ``npm`` executable in ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "fake_npm.py"
    source.write_text(FAKE_NPM, encoding="utf-8")
    npm = directory / "npm"
    npm.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{source}" "$@"\n', encoding="utf-8"
    )
    npm.chmod(0o755)
    return npm


def _run(
    tmp_path: Path, monkeypatch, capsys, plant: str = "", tsc_fails: str = ""
) -> tuple:
    log = tmp_path / "npm.log"
    monkeypatch.setenv("FAKE_NPM_LOG", str(log))
    monkeypatch.setenv("FAKE_NPM_PLANT", plant)
    monkeypatch.setenv("FAKE_TSC_FAILS", tsc_fails)
    tgz = tmp_path / "controlvar-react-native-sdk-0.1.0.tgz"
    tgz.write_bytes(b"tgz")
    npm = fake_npm(tmp_path / "bin")
    status = _script().main(
        [str(tgz), "--npm", str(npm), "--workdir", str(tmp_path / "work")]
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return status, capsys.readouterr().out, calls, str(tgz)


def _failed_legs(out: str) -> List[str]:
    return sorted(set(re.findall(r"^error: AsyncStorage (\S+):", out, re.M)))


def test_every_leg_passes_and_runs_the_documented_install(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    status, out, calls, tgz = _run(tmp_path, monkeypatch, capsys)
    assert status == 0, out
    for version in ("1.24.0", "2.2.0", "3.1.1"):
        assert (
            f"AsyncStorage {version}: the app keeps its one AsyncStorage and "
            "type-checks the SDK" in out
        )
    flags = "install --ignore-scripts --no-audit --no-fund"
    assert calls[0].startswith(f"{flags} react@"), calls
    assert calls[1:] == [
        line
        for version in ("1.24.0", "2.2.0", "3.1.1")
        for line in (f"{flags} {STORAGE_PACKAGE}@{version}", f"{flags} {tgz}")
    ]
    app = _json(tmp_path / "work" / "app" / "package.json")
    assert app["dependencies"][STORAGE_PACKAGE] == "^3.1.1"


@pytest.mark.regression
def test_an_install_that_moves_the_apps_asyncstorage_fails_those_legs(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """EMP-P-8's C6 plant: an install line that names AsyncStorage moves the
    1.x and 2.x apps to 3.x; those two legs fail, each saying so."""
    status, out, _, _ = _run(tmp_path, monkeypatch, capsys, plant="names-storage")
    assert status == 1, out
    assert _failed_legs(out) == ["1.24.0", "2.2.0"], out
    assert "changed the app's AsyncStorage from '^2.2.0' to '^3.1.1'" in out
    assert (
        "the app has AsyncStorage ['3.1.1']; it must have one copy, its own 2.2.0"
        in out
    )
    assert "error: 2 of 3 legs failed" in out


def test_a_second_copy_nested_under_the_sdk_fails_every_leg(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    status, out, _, _ = _run(tmp_path, monkeypatch, capsys, plant="nested")
    assert status == 1, out
    assert _failed_legs(out) == ["1.24.0", "2.2.0", "3.1.1"], out
    assert "the app has AsyncStorage ['1.24.0', '2.2.0']" in out


@pytest.mark.regression
def test_tsc_failing_under_3x_fails_that_leg_only(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """EMP-P-8's tsc plant, as the fake reports it: a bare multiRemove fails
    TS2339 under 3.x. The 3.1.1 leg fails with tsc's output; the others pass,
    so every leg ran."""
    status, out, _, _ = _run(tmp_path, monkeypatch, capsys, tsc_fails="3")
    assert status == 1, out
    assert _failed_legs(out) == ["3.1.1"], out
    assert "error TS2339: Property 'multiRemove'" in out
    assert "error: AsyncStorage 3.1.1: tsc --noEmit exited 2" in out
    assert "AsyncStorage 2.2.0: the app keeps its one AsyncStorage" in out


def test_a_failed_install_fails_its_leg(tmp_path: Path, monkeypatch, capsys) -> None:
    status, out, _, _ = _run(tmp_path, monkeypatch, capsys, plant="fail")
    assert status == 1, out
    assert _failed_legs(out) == ["1.24.0", "2.2.0", "3.1.1"], out
    assert "npm install of the tarball exited 1" in out


def test_an_app_that_cannot_be_set_up_fails(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    status, out, calls, _ = _run(tmp_path, monkeypatch, capsys, plant="setup-fails")
    assert status == 1, out
    assert "error: the app's own packages: npm install exited 1" in out
    assert len(calls) == 1, calls


def test_a_missing_tarball_is_a_usage_error(tmp_path: Path, capsys) -> None:
    assert _script().main([str(tmp_path / "missing.tgz")]) == 2
    assert "is not a file" in capsys.readouterr().err


def test_the_script_needs_only_the_standard_library() -> None:
    """The runner's python3 runs it with nothing installed."""
    imports = set(
        re.findall(r"^(?:from|import) ([\w.]+)", SCRIPT.read_text("utf-8"), re.M)
    )
    allowed = {
        "__future__",
        "argparse",
        "json",
        "subprocess",
        "sys",
        "tempfile",
        "pathlib",
    }
    assert imports <= allowed, imports - allowed
