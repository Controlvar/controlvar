"""scripts/check_packaged_readme.py: the README a registry shows is true for good.

The gate reads the README out of the built package, as the release would
upload it, and holds it to its SDK's rule set. Under I-README it refuses one
that states a status, installs one of this repository's packages from a
registry, quotes its own version, links a relative path, or has an Install
section that is anything but a link to the SDK's page and its requirements
(DECISIONS T230). Under STRICT (the published js and python, whose READMEs
quote their version and install line) it refuses one that is missing, says
the package is not yet published, or links a relative path. An SDK in
neither set is refused. Each refusal here is shown on a synthetic package
that differs from a passing one in that one respect; the readers are shown
on a synthetic .nupkg, npm tarball, wheel and sdist.

The rule sets are pinned against scripts/check_sdk_version.py's ECOSYSTEMS
(PP-1, EMP-P-3), and the negative control reads the js and python READMEs
in the tree, which npm and the wheel pack unchanged.

Reads the tree and writes only under tmp_path; standard library and the script.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import zipfile
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Dict, Optional

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "check_packaged_readme.py"
VERSION = "0.1.0"
PAGE = "https://github.com/Controlvar/controlvar/blob/main/docs/sdk/dotnet.md"


def _gate():
    spec = spec_from_file_location("check_packaged_readme", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ecosystems() -> Dict[str, str]:
    spec = spec_from_file_location(
        "check_sdk_version", ROOT / "scripts" / "check_sdk_version.py"
    )
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return dict(module.ECOSYSTEMS)


GOOD = f"""# Controlvar.SDK

**Beta.** This is a 0.x release: pin the exact version you test against.

The .NET SDK for Controlvar. Full documentation: [the .NET SDK page]({PAGE}).

**Dependencies**: System.Text.Json 8.0.6 from NuGet on `netstandard2.1`

## Install

To install it, follow [the .NET SDK page]({PAGE}#installation).

.NET 8 and later use the `net8.0` build.

## Quick Start

```csharp
var flag = await client.EvaluateFlagAsync("new-checkout-ui", "user-42");
```

## Development

In a clone of this repository:

```bash
cd sdk/dotnet
dotnet test
```

## License

MIT.
"""

#: Today's (0.2.0) Installation section, verbatim: the positive control.
OLD_INSTALL = """## Installation

**Not yet published.** `Controlvar.SDK` has no release on NuGet, so build it from source: add a
`<ProjectReference>` to `sdk/dotnet/src/Controlvar/Controlvar.csproj` in a clone of this
repository (this is what `examples/` and `tests/` do).
"""


def _nupkg(tmp_path: Path, readme: str | None, *, declare: bool = True) -> Path:
    """A package as `dotnet pack` lays it out: a nuspec naming README.md."""
    readme_element = "<readme>README.md</readme>" if declare else ""
    nuspec = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd">'
        f"<metadata><id>Controlvar.SDK</id><version>{VERSION}</version>"
        f"{readme_element}</metadata></package>"
    )
    path = tmp_path / f"Controlvar.SDK.{VERSION}.nupkg"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Controlvar.SDK.nuspec", nuspec)
        archive.writestr("lib/net8.0/Controlvar.dll", b"dll")
        if readme is not None:
            archive.writestr("README.md", readme)
    return path


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, cwd=ROOT
    )


def _install(body: str) -> str:
    """GOOD with `body` added to its Install section."""
    marker = ".NET 8 and later use the `net8.0` build.\n"
    assert marker in GOOD
    return GOOD.replace(marker, marker + "\n" + body + "\n")


def test_a_readme_that_keeps_the_contract_passes(tmp_path: Path) -> None:
    package = _nupkg(tmp_path, GOOD)
    result = _run("--sdk", "dotnet", str(package))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{package}: README checked"


#: (case, README, the refusal it must print). Each README is GOOD with one
#: change. The PP-6 samples are the five after "a <PackageReference>".
REFUSALS = [
    (
        "today's 0.2.0 Installation section (positive control)",
        GOOD.replace(
            GOOD[GOOD.index("## Install") : GOOD.index("## Quick Start")],
            OLD_INSTALL + "\n",
        ),
        "the README says the package is not yet published",
    ),
    (
        "not yet published, in any markup",
        GOOD.replace("**Beta.**", "**Not yet** published. **Beta.**"),
        "the README says the package is not yet published",
    ),
    (
        "a status sentence, is on NuGet",
        GOOD.replace(
            "The .NET SDK for Controlvar.",
            "The package is on [NuGet](https://www.nuget.org).",
        ),
        "the README states whether the package is on a registry",
    ),
    (
        "a status sentence, no release on NuGet",
        GOOD.replace("The .NET SDK for Controlvar.", "It has no release on NuGet yet."),
        "the README states whether the package is on a registry",
    ),
    (
        "a registry install outside the Install section",
        GOOD.replace(
            "dotnet test\n", "dotnet test\ndotnet add package Controlvar.SDK\n"
        ),
        "the README installs this repository's package from a registry",
    ),
    (
        "its own version",
        GOOD.replace("**Beta.**", f"**Beta.** Version {VERSION}."),
        f"the README quotes its own version {VERSION}",
    ),
    (
        "its own version, short",
        GOOD.replace("**Beta.**", "**Beta.** (v0.1)"),
        f"the README quotes its own version {VERSION}",
    ),
    (
        "a relative link",
        GOOD.replace(
            "## License", "See [the guide](../../docs/sdk/dotnet.md).\n\n## License"
        ),
        "the README links relative paths",
    ),
    (
        "a fenced block in the Install section",
        _install("```bash\ncd sdk/dotnet\n```"),
        "the Install section holds a fenced block",
    ),
    (
        "dotnet add package in the Install section",
        _install("Run `dotnet add package Controlvar.SDK`."),
        "the Install section names an install command (dotnet add package)",
    ),
    (
        "a <PackageReference> in the Install section",
        _install('`<PackageReference Include="Controlvar.SDK" />`'),
        "the Install section names an install command (<PackageReference)",
    ),
    (
        "PP-6: a <ProjectReference> in the Install section",
        _install("Add a `<ProjectReference>` to the project in a clone."),
        "the Install section names an install command (<ProjectReference)",
    ),
    (
        "PP-6: dotnet add reference in the Install section",
        _install("Run `dotnet add reference src/Controlvar/Controlvar.csproj`."),
        "the Install section names an install command (dotnet add reference)",
    ),
    (
        "PP-6: npm ci in the Install section",
        _install("Run `npm ci` first."),
        "the Install section names an install command (npm ci)",
    ),
    (
        "PP-6: --prefix in the Install section",
        _install("Build it with `npm --prefix sdk/js run build`."),
        "the Install section names an install command (--prefix)",
    ),
    (
        "PP-6: /path/to in the Install section",
        _install("Point your project at /path/to/controlvar."),
        "the Install section names an install command (/path/to)",
    ),
    (
        ".NET 10: dotnet package add in the Install section",
        _install("Or: dotnet package add Controlvar.SDK"),
        "the Install section names an install command (dotnet package add)",
    ),
    (
        ".NET 10: dotnet reference add in the Install section",
        _install("Or: dotnet reference add ../src/Controlvar/Controlvar.csproj"),
        "the Install section names an install command (dotnet reference add)",
    ),
    (
        ".NET 10: #:package in the Install section",
        _install("Or, in a file-based app: #:package Controlvar.SDK@*"),
        "the Install section names an install command (#:package)",
    ),
    (
        ".NET 10: #:project in the Install section",
        _install(
            "Or, in a file-based app: #:project ../src/Controlvar/Controlvar.csproj"
        ),
        "the Install section names an install command (#:project)",
    ),
    (
        ".NET 10: dotnet package add outside the Install section",
        GOOD.replace(
            "dotnet test\n", "dotnet test\ndotnet package add Controlvar.SDK\n"
        ),
        "the README installs this repository's package from a registry",
    ),
    (
        ".NET 10: #:package outside the Install section",
        GOOD.replace("```csharp\n", "```csharp\n#:package Controlvar.SDK@*\n"),
        "the README installs this repository's package from a registry",
    ),
    (
        "git clone in the Install section",
        _install("Run `git clone https://github.com/Controlvar/controlvar`."),
        "the Install section names an install command (git clone)",
    ),
    (
        "an Install section that does not link the SDK's page",
        GOOD.replace(
            f"follow [the .NET SDK page]({PAGE}#installation)", "follow the SDK page"
        ),
        f"the Install section does not link {PAGE}",
    ),
    (
        "no Install section",
        GOOD.replace("## Install\n", "## Getting it\n"),
        "the README must have exactly one Install section; it has 0",
    ),
    (
        "two Install sections",
        GOOD.replace("## Development", "## Installation\n\nAgain.\n\n## Development"),
        "the README must have exactly one Install section; it has 2",
    ),
]


@pytest.mark.parametrize(
    ("case", "readme", "refusal"), REFUSALS, ids=[c[0] for c in REFUSALS]
)
def test_the_gate_refuses(case: str, readme: str, refusal: str, tmp_path: Path) -> None:
    package = _nupkg(tmp_path, readme)
    result = _run("--sdk", "dotnet", str(package))
    assert result.returncode == 1, (case, result.stdout, result.stderr)
    assert refusal in result.stderr, (case, result.stderr)
    assert "README checked" not in result.stdout


def test_an_sdk_with_no_rules_is_refused_not_passed(tmp_path: Path) -> None:
    """EMP-P-3: the gate on an SDK it has not classified exits 1."""
    package = _nupkg(tmp_path, GOOD)
    result = _run("--sdk", "edge", str(package))
    assert result.returncode == 1, result.stdout
    assert "sdk/edge is not classified" in result.stderr


@pytest.mark.parametrize(
    ("case", "build", "refusal"),
    [
        (
            "a package with no <readme>",
            lambda tmp: _nupkg(tmp, GOOD, declare=False),
            "the nuspec names no <readme>",
        ),
        (
            "a <readme> the package does not hold",
            lambda tmp: _nupkg(tmp, None),
            "which the package does not hold",
        ),
    ],
)
def test_the_gate_refuses_a_package_with_no_readme(
    case, build, refusal, tmp_path: Path
) -> None:
    result = _run("--sdk", "dotnet", str(build(tmp_path)))
    assert result.returncode == 1, (case, result.stdout)
    assert refusal in result.stderr, (case, result.stderr)


# --------------------------------------------------------------------------
# The rule sets (PP-1, EMP-P-3): every SDK the release builds for npm, PyPI
# or NuGet is classified, and an SDK in neither set is refused
# --------------------------------------------------------------------------

#: Each SDK the release builds for npm, PyPI or NuGet, and the rules its
#: packed README is held to: "i-readme" (I_README), "strict" (STRICT), or
#: "none", which the gate refuses, so its release stops at the README step.
#: edge's README is not held to either set yet.
CLASSIFIED: Dict[str, str] = {
    "dotnet": "i-readme",
    "mcp": "i-readme",
    "openfeature": "i-readme",
    "openfeature-python": "i-readme",
    "react": "i-readme",
    "react-native": "i-readme",
    "js": "strict",
    "python": "strict",
    "edge": "none",
}


def test_every_sdk_the_release_builds_for_npm_pypi_or_nuget_is_classified() -> None:
    """PP-1's pin: the two rule sets are explicit lists, compared with
    ECOSYSTEMS. A new npm, PyPI or NuGet SDK fails here until it is put in
    one, or named here as refused."""
    gate = _gate()
    registry_sdks = {
        sdk
        for sdk, ecosystem in _ecosystems().items()
        if ecosystem in ("npm", "pypi", "nuget")
    }
    assert set(CLASSIFIED) == registry_sdks, sorted(set(CLASSIFIED) ^ registry_sdks)
    assert set(gate.I_README) == {s for s, c in CLASSIFIED.items() if c == "i-readme"}
    assert set(gate.STRICT) == {s for s, c in CLASSIFIED.items() if c == "strict"}
    assert not set(gate.I_README) & set(gate.STRICT)
    for sdk, rules in CLASSIFIED.items():
        assert gate.rule_set(sdk) == rules, sdk


@pytest.mark.parametrize("sdk", sorted(CLASSIFIED))
def test_print_rule_set_answers_the_classification(sdk: str) -> None:
    result = _run("--sdk", sdk, "--print-rule-set")
    assert result.returncode == 0, result.stderr
    assert result.stdout == CLASSIFIED[sdk] + "\n"


def test_print_rule_set_reads_no_artefact(tmp_path: Path) -> None:
    result = _run("--sdk", "react", "--print-rule-set", str(_nupkg(tmp_path, GOOD)))
    assert result.returncode == 2
    assert "--print-rule-set reads no artefact" in result.stderr


def test_an_artefact_is_required(tmp_path: Path) -> None:
    result = _run("--sdk", "react")
    assert result.returncode == 2
    assert "name at least one artefact" in result.stderr


@pytest.mark.parametrize("sdk", sorted(s for s, c in CLASSIFIED.items() if c == "none"))
def test_an_unclassified_npm_sdks_tarball_is_refused(sdk: str, tmp_path: Path) -> None:
    """EMP-P-3's plant: the gate on an edge (or react-native) tarball exits
    1, naming "not classified", whatever its README says."""
    package = _tgz(
        tmp_path, {"package.json": _manifest(sdk), "README.md": GOOD.encode()}
    )
    result = _run("--sdk", sdk, str(package))
    assert result.returncode == 1, result.stdout
    assert f"sdk/{sdk} is not classified" in result.stderr
    assert "README checked" not in result.stdout


# --------------------------------------------------------------------------
# STRICT: js and python
# --------------------------------------------------------------------------

#: A STRICT README: it quotes its version and installs from its registry,
#: which I-README refuses and STRICT allows (test_sdk_doc_versions.py holds
#: those quotes to the manifest).
STRICT_GOOD = f"""# @controlvar/js-sdk

The `@controlvar/js-sdk` package is on [npm](https://www.npmjs.com/package/@controlvar/js-sdk) ({VERSION}, beta):

```bash
npm install @controlvar/js-sdk@{VERSION}
```

See [the JavaScript SDK page](https://github.com/Controlvar/controlvar/blob/main/docs/sdk/javascript.md).
"""


def test_a_strict_readme_that_quotes_its_version_and_installs_passes(
    tmp_path: Path,
) -> None:
    package = _tgz(
        tmp_path, {"package.json": _manifest("js"), "README.md": STRICT_GOOD.encode()}
    )
    result = _run("--sdk", "js", str(package))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{package}: README checked"


@pytest.mark.parametrize(
    ("case", "readme", "refusal"),
    [
        (
            "not yet published",
            STRICT_GOOD.replace("package is on", "package is **not yet** published on"),
            "the README says the package is not yet published",
        ),
        (
            "a relative link",
            STRICT_GOOD + "\nSee [the guide](../../docs/sdk/javascript.md).\n",
            "the README links relative paths",
        ),
        ("empty", "\n", "the README is empty"),
    ],
    ids=["not yet published", "a relative link", "empty"],
)
def test_a_strict_readme_is_refused(
    case: str, readme: str, refusal: str, tmp_path: Path
) -> None:
    package = _tgz(
        tmp_path, {"package.json": _manifest("js"), "README.md": readme.encode()}
    )
    result = _run("--sdk", "js", str(package))
    assert result.returncode == 1, (case, result.stdout)
    assert refusal in result.stderr, (case, result.stderr)


#: The published SDKs' READMEs in the tree, which `npm pack` and the wheel
#: carry unchanged, with the manifest that versions each.
STRICT_TREE = {
    "js": ("sdk/js/README.md", "sdk/js/package.json"),
    "python": ("sdk/python/README.md", "sdk/python/pyproject.toml"),
}


def _tree_readme(sdk: str):
    import tomllib

    readme, manifest = STRICT_TREE[sdk]
    text = (ROOT / readme).read_text(encoding="utf-8")
    raw = (ROOT / manifest).read_text(encoding="utf-8")
    if manifest.endswith(".json"):
        version = json.loads(raw)["version"]
    else:
        version = tomllib.loads(raw)["project"]["version"]
    return text, version


@pytest.mark.parametrize("sdk", sorted(STRICT_TREE))
def test_the_published_readmes_pass_strict_and_would_fail_i_readme(
    sdk: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PP-1's negative control: today's js and python READMEs pass under
    STRICT. The plant: the same README under the I-README rules (the SDK
    moved into I_README) is refused, so the shared build jobs would stop
    their release if the rule sets were one."""
    assert set(STRICT_TREE) == set(_gate().STRICT)
    gate = _gate()
    text, version = _tree_readme(sdk)
    assert gate.problems(sdk, text, version) == []
    # Any page will do: the plant is the rule set, not the link.
    monkeypatch.setitem(gate.I_README, sdk, f"sdk/{sdk}/README.md")
    planted = gate.problems(sdk, text, version)
    assert any(f"quotes its own version {version}" in p for p in planted), planted
    assert any("names an install command" in p for p in planted), planted


# --------------------------------------------------------------------------
# The readers: an npm tarball, a wheel and an sdist
# --------------------------------------------------------------------------


def _manifest(sdk: str = "react", version: str = VERSION) -> bytes:
    return json.dumps({"name": f"@controlvar/{sdk}", "version": version}).encode()


def _tgz(tmp_path: Path, members: Dict[str, bytes], prefix: str = "package/") -> Path:
    """An npm tarball: every file under `package/`."""
    path = tmp_path / f"controlvar-react-sdk-{VERSION}.tgz"
    with tarfile.open(path, "w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(f"{prefix}{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


def _metadata(body: Optional[str], version: Optional[str] = VERSION) -> bytes:
    """Core metadata as setuptools writes it: headers, a blank line, the
    README as the body."""
    lines = ["Metadata-Version: 2.4", "Name: controlvar-openfeature"]
    if version is not None:
        lines.append(f"Version: {version}")
    lines.append("Description-Content-Type: text/markdown")
    text = "\n".join(lines) + "\n"
    if body is not None:
        text += "\n" + body
    return text.encode()


def _wheel(tmp_path: Path, members: Dict[str, bytes]) -> Path:
    path = tmp_path / f"controlvar_openfeature-{VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def _sdist(tmp_path: Path, members: Dict[str, bytes]) -> Path:
    path = tmp_path / f"controlvar_openfeature-{VERSION}.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


DIST_INFO = f"controlvar_openfeature-{VERSION}.dist-info"
SDIST_TOP = f"controlvar_openfeature-{VERSION}"


def test_an_npm_tarball_is_read_like_a_nupkg(tmp_path: Path) -> None:
    """The README at the tarball's root, and package.json's version."""
    passing = _tgz(tmp_path, {"package.json": _manifest(), "README.md": GOOD.encode()})
    result = _run("--sdk", "dotnet", str(passing))
    assert result.returncode == 0, result.stderr
    (tmp_path / "refused").mkdir()
    refused = _tgz(
        tmp_path / "refused",
        {"package.json": _manifest(), "README.md": (GOOD + f"\nv{VERSION}\n").encode()},
    )
    result = _run("--sdk", "dotnet", str(refused))
    assert result.returncode == 1
    assert f"the README quotes its own version {VERSION}" in result.stderr


def test_a_wheel_and_an_sdist_are_read_from_their_metadata(tmp_path: Path) -> None:
    """The long description is what PyPI shows; the version is the
    metadata's. Both files of a release are checked, and either refuses."""
    wheel = _wheel(
        tmp_path,
        {
            f"{DIST_INFO}/METADATA": _metadata(GOOD),
            f"{DIST_INFO}/WHEEL": b"Wheel-Version: 1.0\n",
            "controlvar_openfeature/__init__.py": b"",
        },
    )
    sdist = _sdist(
        tmp_path,
        {
            f"{SDIST_TOP}/PKG-INFO": _metadata(GOOD),
            # setuptools also writes one under the egg-info: not the sdist's.
            f"{SDIST_TOP}/src/controlvar_openfeature.egg-info/PKG-INFO": _metadata(
                "stale"
            ),
            f"{SDIST_TOP}/README.md": GOOD.encode(),
        },
    )
    result = _run("--sdk", "dotnet", str(wheel), str(sdist))
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"{wheel}: README checked",
        f"{sdist}: README checked",
    ]

    (tmp_path / "refused").mkdir()
    planted = _sdist(
        tmp_path / "refused",
        {
            f"{SDIST_TOP}/PKG-INFO": _metadata(
                GOOD + "\npip install controlvar-openfeature\n"
            )
        },
    )
    result = _run("--sdk", "dotnet", str(wheel), str(planted))
    assert result.returncode == 1
    assert f"{wheel}: README checked" in result.stdout
    assert (
        f"{planted}: the README installs this repository's package from a registry"
        in result.stderr
    )

    # A refusal counts wherever it comes: the refused file first and a good one
    # after it still exits 1.
    result = _run("--sdk", "dotnet", str(planted), str(wheel))
    assert result.returncode == 1
    assert f"{wheel}: README checked" in result.stdout
    assert (
        f"{planted}: the README installs this repository's package from a registry"
        in result.stderr
    )


@pytest.mark.parametrize(
    ("case", "build", "refusal"),
    [
        (
            "no README",
            lambda tmp: _tgz(tmp, {"package.json": _manifest()}),
            "expected one README",
        ),
        (
            "two READMEs",
            lambda tmp: _tgz(
                tmp, {"package.json": _manifest(), "README.md": b"a", "readme": b"b"}
            ),
            "expected one README",
        ),
        (
            "a tarball with no package.json",
            lambda tmp: _tgz(tmp, {"README.md": GOOD.encode()}),
            "no package/package.json",
        ),
        (
            "a tarball whose package.json has no version",
            lambda tmp: _tgz(
                tmp, {"package.json": b'{"name": "x"}', "README.md": GOOD.encode()}
            ),
            "the package has no version",
        ),
        (
            "a README outside package/",
            lambda tmp: _tgz(
                tmp,
                {"package/package.json": _manifest(), "README.md": GOOD.encode()},
                prefix="",
            ),
            "expected one README",
        ),
        (
            "a wheel with no METADATA",
            lambda tmp: _wheel(tmp, {f"{DIST_INFO}/WHEEL": b""}),
            "expected one .dist-info/METADATA",
        ),
        (
            "a wheel whose METADATA has no body",
            lambda tmp: _wheel(tmp, {f"{DIST_INFO}/METADATA": _metadata(None)}),
            "has no long description, so PyPI would show no README",
        ),
        (
            "a wheel whose METADATA has no version",
            lambda tmp: _wheel(
                tmp, {f"{DIST_INFO}/METADATA": _metadata(GOOD, version=None)}
            ),
            "the package has no version",
        ),
        (
            "an sdist with only the egg-info's PKG-INFO",
            lambda tmp: _sdist(
                tmp,
                {f"{SDIST_TOP}/src/x.egg-info/PKG-INFO": _metadata(GOOD)},
            ),
            "expected one PKG-INFO at the sdist's top directory",
        ),
        (
            "an sdist whose PKG-INFO has no body",
            lambda tmp: _sdist(tmp, {f"{SDIST_TOP}/PKG-INFO": _metadata("\n")}),
            "has no long description",
        ),
    ],
)
def test_the_gate_refuses_a_package_it_cannot_read(
    case: str, build, refusal: str, tmp_path: Path
) -> None:
    result = _run("--sdk", "dotnet", str(build(tmp_path)))
    assert result.returncode == 1, (case, result.stdout)
    assert refusal in result.stderr, (case, result.stderr)


def test_the_gate_refuses_a_file_it_does_not_read(tmp_path: Path) -> None:
    other = tmp_path / "package.zip"
    other.write_bytes(b"")
    result = _run("--sdk", "dotnet", str(other))
    assert result.returncode == 1
    assert "this gate reads a .nupkg, a .tgz, a .whl or a .tar.gz" in result.stderr


def test_a_missing_artefact_is_refused(tmp_path: Path) -> None:
    """The release passes `dist/*`: an empty directory leaves the glob as
    written, which names no file."""
    result = _run("--sdk", "openfeature-python", str(tmp_path / "dist" / "*"))
    assert result.returncode == 1
    assert "README checked" not in result.stdout


def test_a_heading_inside_a_fence_does_not_end_the_install_section() -> None:
    gate = _gate()
    text = "## Install\n\nOne.\n\n```md\n## Not a heading\n```\n\nTwo.\n\n## Next\n\nThree.\n"
    (section,) = gate.install_sections(text)
    assert "One." in section and "Two." in section and "Three." not in section


def test_a_subheading_stays_in_the_install_section() -> None:
    gate = _gate()
    (section,) = gate.install_sections(
        "## Install\n\n### Requirements\n\nNet.\n\n## Next\n"
    )
    assert "Net." in section
