"""scripts/check_packaged_readme.py: the README a registry shows is true for good.

The gate reads the README out of the built package, as the release would
upload it, and refuses one that states a status, installs one of this
repository's packages from a registry, quotes its own version, links a
relative path, or has an Install section that is anything but a link to the
SDK's page and its requirements (DECISIONS T230). Each refusal here is shown
on a synthetic .nupkg that differs from a passing one in that one respect.

Reads and writes only under tmp_path; standard library and the script.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

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
