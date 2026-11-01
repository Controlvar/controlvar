"""The .NET SDK's first release is 0.1.0, and its pages say what it targets.

Before its first publish, the .NET project carried a version number never
released, and targeted .NET 6, out of support. Plan P (#1498) moves it to
0.1.0 targeting ``netstandard2.1`` and ``net8.0`` (P-M12). This holds:

* the project's version (R1a: edit this pin at the next version bump);
* the lines the widened version grep (EMP-9, the plan's C9) found for .NET,
  which name 0.2.0 or the pre-release history;
* no ``net6.0`` left in the SDK or in the files that described it;
* the target frameworks the README, the SDK page and the docs hub state are
  the project's, with the compatibility sentences ruling 10 asks for.

Reads files only: no git and no dotnet, so it runs the same in
``scripts/core_build.sh``'s copy.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Set

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
DOTNET = REPO_ROOT / "sdk" / "dotnet"
PROJECT = DOTNET / "src" / "Controlvar" / "Controlvar.csproj"
README = DOTNET / "README.md"
PAGE = REPO_ROOT / "docs" / "sdk" / "dotnet.md"
HUB = REPO_ROOT / "frontend" / "src" / "pages" / "docs" / "index.tsx"


def properties(project: Path) -> Dict[str, str]:
    root = ET.parse(project).getroot()
    return {
        child.tag: (child.text or "").strip()
        for group in root.iter("PropertyGroup")
        for child in group
    }


def targets() -> Set[str]:
    return set(properties(PROJECT)["TargetFrameworks"].split(";"))


@pytest.mark.regression
def test_the_dotnet_package_is_first_published_at_0_1_0():
    """R1a. The project said 0.2.0, a version never published; the first
    release is 0.1.0. Edit this pin in the pull request that bumps the
    version after 0.1.0 is on NuGet."""
    assert properties(PROJECT)["Version"] == "0.1.0"
    assert properties(PROJECT)["PackageId"] == "Controlvar.SDK"


@pytest.mark.regression
def test_the_dotnet_package_targets_net8_and_netstandard2_1():
    """P-M12: net6.0 removed (out of support); netstandard2.1 keeps .NET Core
    3.0 to .NET 7, Mono and Unity."""
    assert targets() == {"netstandard2.1", "net8.0"}


#: Every file in which the widened version grep of the first-publish plan
#: found a .NET line (5 lines at main 3d81f276): the project, the README (its
#: version line and its section on the pre-release history), the SDK page and
#: a test sample.
NEVER_RELEASED_DOTNET_SITES = (
    "sdk/dotnet/src/Controlvar/Controlvar.csproj",
    "sdk/dotnet/README.md",
    "docs/sdk/dotnet.md",
    "backend/tests/unit/docs/test_deferred_sdk_pages.py",
)
#: The .NET alternatives of that grep, as it ran in bash. They are built from
#: OLD so that this file is not itself a hit for the grep.
OLD = ".".join(("0", "2", "0"))
OLD_ESCAPED = re.escape(OLD)
NEVER_RELEASED_DOTNET = re.compile(
    r"Controlvar\.SDK.{0,40}"
    + OLD_ESCAPED
    + r"|`Controlvar\.SDK` \(v0\.2"
    + r"|Migrating from 0\.1\.x"
    + r"|\*\*Version\*\*: "
    + OLD_ESCAPED
    + r"|<Version>"
    + OLD_ESCAPED
    + r"</Version>"
)


@pytest.mark.parametrize(
    "line",
    [
        f"    <Version>{OLD}</Version>",
        f"**Version**: {OLD}",
        "## Migrating from " + ".".join(("0", "1", "x")),
        f"`Controlvar.SDK` (v{OLD[:3]}) provides feature flag evaluation",
        f'<PackageReference Include="Controlvar.SDK" Version="{OLD}" />',
    ],
)
def test_the_never_released_dotnet_pattern_finds_each_old_site(line):
    assert NEVER_RELEASED_DOTNET.search(line), line


@pytest.mark.regression
@pytest.mark.parametrize("site", NEVER_RELEASED_DOTNET_SITES)
def test_no_dotnet_site_names_the_never_released_version(site):
    found = [
        f"{site}:{number}: {line.strip()}"
        for number, line in enumerate(
            (REPO_ROOT / site).read_text(encoding="utf-8").splitlines(), start=1
        )
        if NEVER_RELEASED_DOTNET.search(line)
    ]
    assert not found, found


#: The files outside sdk/dotnet that described its target frameworks.
TARGET_SITES = (
    ".github/workflows/sdk-unit-tests.yml",
    ".github/workflows/_platform.yml",
    "docs/sdk/dotnet.md",
    "frontend/src/pages/docs/index.tsx",
    "docs/_claims/frontend/src/pages/docs/index.tsx.desc.toml",
    "backend/tests/unit/docs/test_docs_hub_claims.py",
)
OLD_TARGET = re.compile("net" + r"6\.0\b")


def _sdk_files() -> List[Path]:
    return [
        path
        for path in sorted(DOTNET.rglob("*"))
        if path.is_file() and not {"bin", "obj"} & set(path.relative_to(DOTNET).parts)
    ]


@pytest.mark.regression
def test_no_file_names_the_old_target():
    """V27(a). The SDK, its tests, its examples, the CI set-up and the pages
    that described the targets name net6.0 nowhere."""
    files = _sdk_files() + [REPO_ROOT / site for site in TARGET_SITES]
    assert any(path.suffix == ".csproj" for path in files)
    found = [
        f"{path.relative_to(REPO_ROOT)}:{number}"
        for path in files
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
        )
        if OLD_TARGET.search(line)
    ]
    assert not found, found


def _backticked(text: str) -> Set[str]:
    return set(re.findall(r"`(net[\w.]+)`", text))


def test_the_readme_states_the_projects_targets():
    """C12. The README's target line is the project's TargetFrameworks."""
    readme = README.read_text(encoding="utf-8")
    (line,) = [
        line for line in readme.splitlines() if line.startswith("**Target frameworks**")
    ]
    assert _backticked(line) == targets(), line


def test_the_sdk_page_states_the_projects_targets():
    page = PAGE.read_text(encoding="utf-8")
    lede = page.split("\n## ", 1)[0]
    (sentence,) = re.findall(r"It targets [^,]+,", " ".join(lede.split()))
    assert _backticked(sentence) == targets(), sentence


def test_the_docs_hub_states_the_projects_targets():
    hub = HUB.read_text(encoding="utf-8")
    (desc,) = re.findall(r"label: '\.NET SDK'[^\n]*desc: '([^']*)'", hub)
    stated = set(re.findall(r"\b(net[\w.]*\d)\b", desc.split(";", 1)[0]))
    assert stated == targets(), desc


@pytest.mark.parametrize("page", [README, PAGE], ids=["README", "page"])
def test_the_compatibility_sentences_are_stated(page):
    """Ruling 10: compatibility, never a support claim: .NET Framework is
    named as unable to use the package, and the build warning of System.Text.Json
    8 and of its dependency System.Text.Encodings.Web on .NET Core 3.0 to .NET 5
    is stated."""
    folded = " ".join(page.read_text(encoding="utf-8").split())
    assert ".NET Framework cannot use" in folded
    assert (
        "On .NET Core 3.0 to .NET 5, System.Text.Json 8 and its dependency "
        "System.Text.Encodings.Web warn at build time" in folded
    )
    assert ".NET 8 and later use the `net8.0` build" in folded


def system_text_json_version() -> str:
    """The System.Text.Json version the project references for netstandard2.1."""
    root = ET.parse(PROJECT).getroot()
    (version,) = [
        reference.get("Version", "")
        for reference in root.iter("PackageReference")
        if reference.get("Include") == "System.Text.Json"
    ]
    return version


def test_the_readme_quotes_the_projects_system_text_json():
    """The README nuget.org shows names the System.Text.Json version the
    netstandard2.1 build depends on, and the major its compatibility sentence
    names; both are the project's, so a bump of the reference alone fails
    here and points at the README."""
    version = system_text_json_version()
    readme = " ".join(README.read_text(encoding="utf-8").split())
    assert re.findall(r"System\.Text\.Json (\d+\.\d+\.\d+) from NuGet", readme) == [
        version
    ], readme[:600]
    assert re.findall(r"System\.Text\.Json (\d+) and its dependency", readme) == [
        version.split(".")[0]
    ]
