"""The packed-README gate, where it runs (PP-1, EMP-P-3, EMP-P-4).

``scripts/check_packaged_readme.py`` reads the README a registry would show
out of the built package and holds it to its SDK's rule set (I-README or
STRICT; an SDK in neither is refused). It runs in two places:

* sdk-release.yml: every build job of a registry that shows a README
  (``npm-build``, ``pypi-build``, ``nuget-build``) runs it with no condition,
  on the file it records, after that file is built and before it is
  recorded. ``maven-build`` does not: Central shows the POM, and the Java
  README is not packaged. A new ecosystem fails here until it is placed.
* sdk-unit-tests.yml: each node and python leg (every npm and PyPI SDK a pull
  request changes, and js, python and react on every pull request) packs
  its SDK the way the release does and runs the gate with that SDK's rules,
  so a README the release would refuse fails the pull request rather than a
  release tag. An SDK with no rules is named and not packed: the release
  refuses it.

The steps run here the way the runner runs them, through
test_sdk_release_wiring.py's ``_run_step``, in a scratch workspace that
holds a copy of the gate: on synthetic packages made from the READMEs in
the tree, with a fake ``npm`` and a fake ``python`` standing in for
``npm pack`` and ``python -m build``. The plants (an install line in a
README, js held to the I-README rules) change only that copy.
"""

from __future__ import annotations

import ast
import io
import json
import re
import shutil
import tarfile
import tomllib
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest
import yaml

from backend.tests.smoke.test_sdk_release_wiring import (
    ROOT,
    _run_of,
    _run_step,
    _step,
    _workflow,
    _write_executable,
    ecosystems,
)

pytestmark = pytest.mark.smoke

GATE = "scripts/check_packaged_readme.py"
UNIT_TESTS = ROOT / ".github" / "workflows" / "sdk-unit-tests.yml"
STEP = "Check the packed README"
SDK_OUTPUT = "${{ needs.resolve.outputs.sdk }}"

#: The build job of each registry that shows a README, and its ecosystem.
GATED: Dict[str, str] = {
    "npm-build": "npm",
    "pypi-build": "pypi",
    "nuget-build": "nuget",
}
#: The ecosystems with no README gate, each for its reason: Central shows
#: the POM (the Java README is not packaged), Go has no registry upload, and
#: an unwired SDK's tag is refused before any build job.
NOT_GATED = {"maven", "tag-only", "unwired"}

#: Per build job: the gate step's env, its run line, and the steps it sits
#: between (the package exists after the first; the second records it).
EXPECTED: Dict[str, tuple] = {
    "npm-build": (
        {"SDK": SDK_OUTPUT, "TGZ": "${{ steps.pack.outputs.tgz }}"},
        'python3 scripts/check_packaged_readme.py --sdk "$SDK" "$TGZ"',
        "Pack",
        "Record the tarball",
    ),
    "pypi-build": (
        {"SDK": SDK_OUTPUT},
        'python3 scripts/check_packaged_readme.py --sdk "$SDK" "sdk/$SDK/dist/"*',
        "Built version matches the tag",
        "Record the distributions",
    ),
    "nuget-build": (
        {"SDK": SDK_OUTPUT, "NUPKG": "${{ steps.pack.outputs.nupkg }}"},
        'python3 scripts/check_packaged_readme.py --sdk "$SDK" "$NUPKG"',
        "Pack",
        "Record the package",
    ),
}


def _labels(job: str) -> List[str]:
    return [s.get("name") or s.get("uses") for s in _workflow()["jobs"][job]["steps"]]


# --------------------------------------------------------------------------
# sdk-release.yml
# --------------------------------------------------------------------------


def test_the_registries_that_show_a_readme_are_the_ones_whose_build_runs_the_gate() -> (
    None
):
    """Every job that runs the gate is one of the three build jobs, each of
    those runs it, and every ecosystem is either gated or has its reason in
    NOT_GATED."""
    jobs = _workflow()["jobs"]
    running = {
        name
        for name, job in jobs.items()
        if any(GATE in _run_of(step) for step in job.get("steps", []))
    }
    assert running == set(GATED), sorted(running ^ set(GATED))
    for job, ecosystem in GATED.items():
        assert (
            jobs[job].get("if") == f"needs.resolve.outputs.ecosystem == '{ecosystem}'"
        )
    assert set(ecosystems().values()) == set(GATED.values()) | NOT_GATED


@pytest.mark.parametrize("job", sorted(EXPECTED))
def test_each_build_job_runs_the_gate_on_what_it_records(job: str) -> None:
    """One step, with no condition and no continue-on-error: the gate runs
    for every SDK of the job's ecosystem, and refuses an unclassified one."""
    env, run, after, before = EXPECTED[job]
    steps = [s for s in _workflow()["jobs"][job]["steps"] if GATE in _run_of(s)]
    assert [s.get("name") for s in steps] == [STEP], steps
    gate = _step(job, STEP)
    assert gate.get("env") == env
    assert _run_of(gate) == run
    assert "if" not in gate
    assert "continue-on-error" not in gate
    labels = _labels(job)
    assert labels.index(after) < labels.index(STEP) < labels.index(before), labels


def _workspace(tmp_path: Path) -> Path:
    """A scratch checkout holding a copy of the gate."""
    workspace = tmp_path / "ws"
    (workspace / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / GATE, workspace / GATE)
    return workspace


def _readme(sdk: str) -> str:
    return (ROOT / "sdk" / sdk / "README.md").read_text(encoding="utf-8")


def _npm_version(sdk: str) -> str:
    manifest = ROOT / "sdk" / sdk / "package.json"
    return json.loads(manifest.read_text(encoding="utf-8"))["version"]


def _pypi_version(sdk: str) -> str:
    manifest = ROOT / "sdk" / sdk / "pyproject.toml"
    return tomllib.loads(manifest.read_text(encoding="utf-8"))["project"]["version"]


def _tgz(path: Path, readme: str, version: str) -> Path:
    """An npm tarball holding package.json and README.md, as `npm pack`
    lays them out."""
    with tarfile.open(path, "w:gz") as archive:
        for name, data in (
            ("package.json", json.dumps({"version": version}).encode()),
            ("README.md", readme.encode()),
        ):
            info = tarfile.TarInfo(f"package/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


def _metadata(readme: str, version: str) -> bytes:
    return (
        f"Metadata-Version: 2.4\nName: controlvar-openfeature\nVersion: {version}\n"
        f"Description-Content-Type: text/markdown\n\n{readme}"
    ).encode()


def _dist(directory: Path, readme: str, version: str) -> None:
    """A wheel and an sdist whose metadata carries ``readme``."""
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"controlvar_openfeature-{version}"
    with zipfile.ZipFile(directory / f"{stem}-py3-none-any.whl", "w") as wheel:
        wheel.writestr(f"{stem}.dist-info/METADATA", _metadata(readme, version))
    with tarfile.open(directory / f"{stem}.tar.gz", "w:gz") as sdist:
        data = _metadata(readme, version)
        info = tarfile.TarInfo(f"{stem}/PKG-INFO")
        info.size = len(data)
        sdist.addfile(info, io.BytesIO(data))


def _planted(readme: str, heading: str, line: str) -> str:
    """``readme`` with ``line`` under ``heading``, outside the Install section."""
    planted = readme.replace(heading, f"{heading}\n{line}\n", 1)
    assert planted != readme, heading
    return planted


def test_npm_build_passes_the_react_readme_and_refuses_a_planted_install(
    tmp_path: Path,
) -> None:
    """The step as the runner runs it, on a tarball of the README in the
    tree: it passes, and the same README with a registry install line
    outside its Install section is refused."""
    workspace = _workspace(tmp_path)
    step = _step("npm-build", STEP)
    readme, version = _readme("react"), _npm_version("react")
    good = _tgz(tmp_path / "good.tgz", readme, version)
    result = _run_step(
        step,
        {"needs.resolve.outputs.sdk": "react", "steps.pack.outputs.tgz": str(good)},
        tmp_path,
        workspace,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{good}: README checked" in result.stdout

    bad = _tgz(
        tmp_path / "bad.tgz",
        _planted(readme, "## Provider setup\n", "`npm install @controlvar/react-sdk`"),
        version,
    )
    result = _run_step(
        step,
        {"needs.resolve.outputs.sdk": "react", "steps.pack.outputs.tgz": str(bad)},
        tmp_path,
        workspace,
    )
    assert result.returncode == 1, result.stdout
    assert "installs this repository's package from a registry" in result.stderr


def test_npm_build_refuses_an_unclassified_sdk(tmp_path: Path) -> None:
    """EMP-P-3: an npm SDK in neither rule set (edge) stops at the gate."""
    workspace = _workspace(tmp_path)
    tgz = _tgz(tmp_path / "edge.tgz", _readme("edge"), _npm_version("edge"))
    result = _run_step(
        _step("npm-build", STEP),
        {"needs.resolve.outputs.sdk": "edge", "steps.pack.outputs.tgz": str(tgz)},
        tmp_path,
        workspace,
    )
    assert result.returncode == 1, result.stdout
    assert "sdk/edge is not classified" in result.stderr


def test_pypi_build_checks_every_file_in_dist(tmp_path: Path) -> None:
    """The wheel and the sdist of the Python provider, from the README in the
    tree: both pass; a planted install line in either is refused; an empty
    dist/ is refused."""
    sdk = "openfeature-python"
    readme, version = _readme(sdk), _pypi_version(sdk)
    step = _step("pypi-build", STEP)
    values = {"needs.resolve.outputs.sdk": sdk}

    workspace = _workspace(tmp_path / "good")
    _dist(workspace / "sdk" / sdk / "dist", readme, version)
    result = _run_step(step, values, tmp_path / "good", workspace)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("README checked") == 2, result.stdout

    workspace = _workspace(tmp_path / "bad")
    _dist(
        workspace / "sdk" / sdk / "dist",
        _planted(readme, "## Quick start\n", "`pip install controlvar-openfeature`"),
        version,
    )
    result = _run_step(step, values, tmp_path / "bad", workspace)
    assert result.returncode == 1, result.stdout
    assert "installs this repository's package from a registry" in result.stderr

    workspace = _workspace(tmp_path / "empty")
    (workspace / "sdk" / sdk / "dist").mkdir(parents=True)
    result = _run_step(step, values, tmp_path / "empty", workspace)
    assert result.returncode == 1, result.stdout
    assert "README checked" not in result.stdout


# --------------------------------------------------------------------------
# sdk-unit-tests.yml (EMP-P-4 (a))
# --------------------------------------------------------------------------


def _unit_step() -> dict:
    steps = yaml.safe_load(UNIT_TESTS.read_text(encoding="utf-8"))["jobs"]["linux"][
        "steps"
    ]
    names = [s.get("name") for s in steps]
    assert names.count(STEP) == 1, names
    assert names.index("Run unit tests") < names.index(STEP), names
    return steps[names.index(STEP)]


def _linux_languages() -> Dict[str, str]:
    text = UNIT_TESTS.read_text(encoding="utf-8")
    return ast.literal_eval(re.search(r"LINUX = (\{.*?\})\n", text, re.S).group(1))


def test_the_unit_tests_check_the_readme_of_every_npm_and_pypi_sdk() -> None:
    """The step runs on every node and python leg, and those legs are exactly
    the SDKs the release builds for npm and PyPI."""
    step = _unit_step()
    assert step.get("if") == "matrix.lang == 'node' || matrix.lang == 'python'"
    assert step.get("env") == {
        "SDK": "${{ matrix.sdk }}",
        "TOOLCHAIN": "${{ matrix.lang }}",
    }
    assert "continue-on-error" not in step
    legs = {
        sdk for sdk, lang in _linux_languages().items() if lang in ("node", "python")
    }
    registry = {sdk for sdk, eco in ecosystems().items() if eco in ("npm", "pypi")}
    assert legs == registry, sorted(legs ^ registry)
    run = _run_of(step)
    assert "--print-rule-set" in run
    gate = 'python3 scripts/check_packaged_readme.py --sdk "$SDK" "$out"/*'
    lines = [line.strip() for line in run.splitlines() if line.strip()]
    # The gate's exit status is the step's: `set -e` is on, the gate is the
    # last line, and nothing follows it on its own line (an `|| ...` there
    # would let a refused README through).
    assert lines[0] == "set -euo pipefail", lines[0]
    assert lines[-1] == gate, lines[-1]
    assert lines.count(gate) == 1, lines


#: A fake `npm`: `npm pack --pack-destination DIR` writes a tarball of the
#: current directory's package.json and README.md into DIR, as npm lays it
#: out; every call is logged.
FAKE_NPM = """set -euo pipefail
printf '%s\\n' "$*" >> "$NPM_LOG"
[ "$1" = pack ] && [ "$2" = --pack-destination ] || exit 64
python3 - "$3" <<'PY'
import io, json, pathlib, sys, tarfile
manifest = json.loads(pathlib.Path("package.json").read_text(encoding="utf-8"))
name = manifest["name"].lstrip("@").replace("/", "-")
out = pathlib.Path(sys.argv[1]) / f"{name}-{manifest['version']}.tgz"
with tarfile.open(out, "w:gz") as archive:
    for member in ("package.json", "README.md"):
        data = pathlib.Path(member).read_bytes()
        info = tarfile.TarInfo(f"package/{member}")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
PY
"""

#: A fake `python`: `-m pip install build` does nothing; `-m build --outdir
#: OUT DIR` writes a wheel whose METADATA carries DIR's README.md, at DIR's
#: pyproject.toml version.
FAKE_PYTHON = """set -euo pipefail
if [ "$*" = "-m pip install build" ]; then exit 0; fi
[ "$1 $2 $3" = "-m build --outdir" ] || exit 64
python3 - "$4" "$5" <<'PY'
import pathlib, sys, tomllib, zipfile
out, sdk = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
version = tomllib.loads((sdk / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
readme = (sdk / "README.md").read_text(encoding="utf-8")
stem = f"controlvar_openfeature-{version}"
with zipfile.ZipFile(out / f"{stem}-py3-none-any.whl", "w") as wheel:
    wheel.writestr(
        f"{stem}.dist-info/METADATA",
        f"Metadata-Version: 2.4\\nName: x\\nVersion: {version}\\n\\n{readme}",
    )
PY
"""


def _run_unit_step(
    tmp_path: Path, sdk: str, lang: str, plant: Optional[Tuple[str, str]] = None
):
    """The sdk-unit-tests step for one leg, in a workspace whose sdk/<sdk> is
    the tree's (read only) and whose gate is a copy, planted if asked."""
    workspace = _workspace(tmp_path)
    (workspace / "sdk").mkdir()
    (workspace / "sdk" / sdk).symlink_to(ROOT / "sdk" / sdk)
    if plant is not None:
        gate = workspace / GATE
        text = gate.read_text(encoding="utf-8")
        planted = re.sub(plant[0], plant[1], text, count=1)
        assert planted != text, plant
        gate.write_text(planted, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    _write_executable(bin_dir / "npm", FAKE_NPM)
    _write_executable(bin_dir / "python", FAKE_PYTHON)
    log = tmp_path / "npm.log"
    log.touch()
    result = _run_step(
        _unit_step(),
        {"matrix.sdk": sdk, "matrix.lang": lang},
        tmp_path,
        workspace,
        runner_env={"NPM_LOG": str(log)},
    )
    return result, log.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("sdk", "lang"),
    [
        ("js", "node"),
        ("react", "node"),
        ("openfeature", "node"),
        ("openfeature-python", "python"),
    ],
)
def test_a_leg_packs_its_sdk_and_the_readme_in_the_tree_passes(
    sdk: str, lang: str, tmp_path: Path
) -> None:
    result, npm_calls = _run_unit_step(tmp_path, sdk, lang)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "README checked" in result.stdout, result.stdout
    assert bool(npm_calls) == (lang == "node"), npm_calls


def test_js_held_to_the_i_readme_rules_turns_the_js_leg_red(tmp_path: Path) -> None:
    """EMP-P-4 (a)'s plant: the rule sets merged into one (js moved from
    STRICT into I_README) fails the js leg, which runs on every pull request,
    on the version and the install line its README must quote."""
    result, _ = _run_unit_step(
        tmp_path,
        "js",
        "node",
        plant=(
            r"I_README: dict\[str, str\] = \{\n",
            # Any page will do: the plant is the rule set, not the link.
            'I_README: dict[str, str] = {\n    "js": "sdk/js/README.md",\n',
        ),
    )
    assert result.returncode == 1, result.stdout
    assert "the README quotes its own version" in result.stderr, result.stderr
    assert "the Install section names an install command" in result.stderr


@pytest.mark.parametrize("sdk", ["edge", "react-native"])
def test_a_leg_with_no_rules_is_named_and_not_packed(sdk: str, tmp_path: Path) -> None:
    result, npm_calls = _run_unit_step(tmp_path, sdk, "node")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"::notice::sdk/{sdk}: the README gate has no rules" in result.stdout
    assert npm_calls == ""
