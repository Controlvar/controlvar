"""The unpublished SDK pages say what each client does (#1142, #1107).

The 24 documents in ``CLAIMS_DEFERRED`` (``scripts/doc_claims.py``) are the
mobile and not-yet-published SDK pages. Their claims registry is #1107's job;
until then three things are held here for every one of them, read from files:

1. **Licence.** A licence a page states (a ``License`` section, or a sentence
   that names a licence beside the word) is the one in its SDK's own
   ``LICENSE`` file, and the SDK's manifest agrees where it declares one. The
   SDKs are MIT (``sdk/**`` in ``REUSE.toml``); only the repository root is
   Apache-2.0. ``LICENCE_FLOOR`` names pages that must carry a statement, so
   a scan that finds nothing fails instead of passing.
2. **Install commands.** No install command or dependency line names one of
   this repository's packages from its registry (npm, PyPI, RubyGems, Hex,
   pub.dev, Packagist, NuGet, Maven Central, CocoaPods, a SwiftPM URL) unless
   that package is published (``PUBLISHED`` in ``test_sdk_doc_versions.py``).
   A source install is not a registry one: a path, a tarball, ``pip install
   -e``, a ``git:``/``path:`` dependency, a Composer path repository in the
   same block, a Maven coordinate on a page that installs it into ``~/.m2``
   (and, for Gradle, reads it there with ``mavenLocal()``). A page with an
   install section says "not yet published" exactly when its SDK is not
   published, except a README a registry shows (``PACKAGED``): that one
   states no status at all, and keeps the packaged-README contract of
   ``scripts/check_packaged_readme.py`` instead (DECISIONS T230).
3. **Attribute targeting on flags.** Which SDKs send a ``context`` on flag
   evaluation is read from each SDK's source: the line that builds the
   evaluate path and the three code lines after it, or, for the two
   OpenFeature providers, the call they delegate to. A page about an SDK that
   sends none must not say that attributes reach flag evaluation
   (``CLAIMS``). The same patterns must find the true sentence on the React
   page, whose client sends one, so a pattern that stopped matching fails.

Each pattern is bounded, and each has a sample it must match. This is a
sweep, not a proof: a paraphrase passes, and review catches the rest.

Reads only files; no git, so it runs the same in ``scripts/core_build.sh``'s
copy. It is in the docs-only gate's "Docs content tests" through its
directory, ``backend/tests/unit/docs/``.
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

import pytest

from backend.tests.unit.docs.test_sdk_doc_versions import PUBLISHED, says_unpublished

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
SDK_DIR = REPO_ROOT / "sdk"


def _claims_deferred() -> Tuple[str, ...]:
    """``CLAIMS_DEFERRED`` as ``scripts/doc_claims.py`` spells it (read, not run)."""
    tree = ast.parse((REPO_ROOT / "scripts" / "doc_claims.py").read_text("utf-8"))
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "CLAIMS_DEFERRED":
            return tuple(ast.literal_eval(node.value))
    raise AssertionError("scripts/doc_claims.py defines no CLAIMS_DEFERRED")


#: The deferred documents, as the claims checker lists them.
DEFERRED: Tuple[str, ...] = _claims_deferred()

#: A page about more than its own directory's SDK. The OpenFeature page
#: documents the TypeScript provider and the Python one.
EXTRA_SDKS: Dict[str, Tuple[str, ...]] = {
    "docs/sdk/openfeature.md": ("openfeature-python",),
}


def sdks_of(page: str) -> Tuple[str, ...]:
    """The SDK directories (under ``sdk/``) a deferred page is about."""
    parts = page.split("/")
    if parts[0] == "sdk":
        own = parts[1]
    else:
        assert parts[:2] == ["docs", "sdk"], page
        own = parts[2][: -len(".md")]
    assert (SDK_DIR / own).is_dir(), f"{page}: sdk/{own} does not exist"
    return (own,) + EXTRA_SDKS.get(page, ())


def text(page: str) -> str:
    return (REPO_ROOT / page).read_text(encoding="utf-8")


def fold(value: str) -> str:
    """Prose as one line: a line break (and a blockquote's ``>``) is a space.

    Split and joined rather than matched: a pattern that starts with an
    unbounded run of blanks retries at every position of a long run of spaces,
    which is quadratic.
    """
    lines = value.split("\n")
    joined = []
    for index, line in enumerate(lines):
        if index:
            line = line.lstrip(" \t")
            if line.startswith(">"):
                line = line[2:] if line[1:2] in (" ", "\t") else line[1:]
        if index < len(lines) - 1:
            line = line.rstrip(" \t")
        joined.append(line)
    return " ".join(joined)


def test_the_deferred_pages_are_the_ones_this_file_reads():
    assert len(DEFERRED) == 24
    for page in DEFERRED:
        assert (REPO_ROOT / page).is_file(), page
        assert sdks_of(page)


# ---------------------------------------------------------------------------
# 1. Licence
# ---------------------------------------------------------------------------

#: A licence name as a page or a manifest writes it.
LICENCE_NAME = re.compile(
    r"\b(MIT|Apache(?:[ -]License)?[ ,-]{0,3}(?:Version[ ]?)?2(?:\.0)?)\b",
    re.IGNORECASE,
)
#: A heading for a licence section, and the text up to the next heading.
LICENCE_SECTION = re.compile(
    r"^#{1,4}[ \t]{1,40}Licen[cs]e[ \t]{0,40}\n(?P<body>(?:(?!#{1,4}[ \t])[^\n]{0,500}\n?){0,12})",
    re.MULTILINE | re.IGNORECASE,
)
#: A sentence that names a licence beside the word, either order; the names in
#: it are then read with ``LICENCE_NAME``.
LICENCE_SENTENCE = re.compile(
    r"licen[cs]e[ds]?\b[^.\n|]{0,60}?\b(?:MIT|Apache)\b[^.\n|]{0,25}"
    r"|\b(?:MIT|Apache)\b[^.\n|]{0,30}?\blicen[cs]e[^.\n|]{0,15}",
    re.IGNORECASE,
)

#: Pages that state a licence today: the scan must find each of them.
LICENCE_FLOOR = (
    "sdk/dotnet/README.md",
    "sdk/elixir/README.md",
    "sdk/php/README.md",
    "sdk/ruby/README.md",
)


def spdx(name: str) -> str:
    folded = name.strip().lower()
    if folded in ("mit", "mit license"):
        return "MIT"
    if folded.startswith("apache") and "2" in folded:
        return "Apache-2.0"
    raise AssertionError(f"not a licence this file knows: {name!r}")


def licence_of(sdk: str) -> str:
    """The SDK's licence: its LICENSE file, checked against its manifest."""
    first = (SDK_DIR / sdk / "LICENSE").read_text(encoding="utf-8").strip()
    first = first.splitlines()[0]
    if first.startswith("MIT License"):
        licence = "MIT"
    elif first.startswith("Apache License"):
        licence = "Apache-2.0"
    else:
        raise AssertionError(f"sdk/{sdk}/LICENSE: unrecognised first line {first!r}")
    for source, declared in manifest_licences(sdk):
        assert spdx(declared) == licence, (
            f"sdk/{sdk}: {source} declares {declared!r}, LICENSE says {licence}"
        )
    return licence


def manifest_licences(sdk: str) -> Iterator[Tuple[str, str]]:
    """Each licence a manifest of the SDK declares, as (file, value)."""
    directory = SDK_DIR / sdk
    found = {
        "package.json": lambda t: [json.loads(t).get("license")],
        "pyproject.toml": lambda t: [
            (tomllib.loads(t)["project"].get("license") or {}).get("text")
        ],
        "composer.json": lambda t: [json.loads(t).get("license")],
        "mix.exs": lambda t: re.findall(
            r"licenses:\s{0,40}\[\s{0,40}\"([^\"]{1,40})\"", t
        ),
        "pom.xml": lambda t: re.findall(
            r"<license>\s{0,40}<name>([^<]{1,60})</name>", t
        ),
    }
    for name, read in found.items():
        path = directory / name
        if path.is_file():
            for value in read(path.read_text(encoding="utf-8")):
                if value:
                    yield name, value
    for path in sorted(directory.glob("*.gemspec")):
        for value in re.findall(
            r"\.license\s{0,40}=\s{0,40}\"([^\"]{1,40})\"", path.read_text("utf-8")
        ):
            yield path.name, value
    for path in sorted(directory.glob("src/*/*.csproj")):
        for value in re.findall(
            r"<PackageLicenseExpression>([^<]{1,40})<", path.read_text("utf-8")
        ):
            yield path.name, value


def licence_statements(page_text: str) -> List[str]:
    """Every licence the page states, as sorted SPDX identifiers."""
    named: List[str] = []
    for section in LICENCE_SECTION.finditer(page_text):
        named += [m.group(1) for m in LICENCE_NAME.finditer(section.group("body"))]
    for sentence in LICENCE_SENTENCE.finditer(fold(page_text)):
        named += [m.group(1) for m in LICENCE_NAME.finditer(sentence.group(0))]
    return sorted({spdx(name) for name in named})


@pytest.mark.parametrize(
    "sample, expected",
    [
        ("## License\n\nMIT\n", ["MIT"]),
        ("## License\n\nApache 2.0 — see the root `LICENSE` file.\n", ["Apache-2.0"]),
        ("## Licence\n\nMIT — see `LICENSE` in this directory.\n", ["MIT"]),
        ("This SDK is licensed under the MIT license.", ["MIT"]),
        ("Released under the Apache License 2.0.", ["Apache-2.0"]),
        ("The SDK is MIT\nlicensed.", ["MIT"]),
        ("No statement here, and the `LICENSE` file is not quoted.", []),
    ],
)
def test_the_licence_scan_reads_what_it_exists_for(sample, expected):
    assert licence_statements(sample) == expected


@pytest.mark.parametrize("page", DEFERRED)
def test_a_deferred_page_states_its_sdks_licence(page):
    allowed = {licence_of(sdk) for sdk in sdks_of(page)}
    wrong = [name for name in licence_statements(text(page)) if name not in allowed]
    assert not wrong, (
        f"{page} states {wrong}, but its SDK is {sorted(allowed)} "
        "(sdk/<name>/LICENSE and the manifest)"
    )


def test_the_licence_floor_is_read():
    stated = {page for page in DEFERRED if licence_statements(text(page))}
    assert set(LICENCE_FLOOR) <= stated, sorted(set(LICENCE_FLOOR) - stated)


# ---------------------------------------------------------------------------
# 2. Install commands
# ---------------------------------------------------------------------------

#: (ecosystem, pattern). Group ``args`` is a command's arguments, split later;
#: group ``name`` is one package name.
INSTALLS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    (
        "npm",
        re.compile(
            r"\b(?:npm[ \t]{1,40}(?:install|i|add)|yarn[ \t]{1,40}add|pnpm[ \t]{1,40}add|bun[ \t]{1,40}add)"
            r"\b(?P<args>[^\n`;&|]{0,300})"
        ),
    ),
    ("npm", re.compile(r"\bnpm:(?P<name>@controlvar/[A-Za-z0-9._-]{1,60})")),
    ("pypi", re.compile(r"\bpip3?[ \t]{1,40}install\b(?P<args>[^\n`;&|]{0,300})")),
    (
        "rubygems",
        re.compile(
            r"^[ \t]{0,40}gem[ \t]{1,40}['\"](?P<name>[\w.-]{1,60})['\"](?P<rest>[^\n]{0,200})$",
            re.MULTILINE,
        ),
    ),
    (
        "rubygems",
        re.compile(r"\bgem[ \t]{1,40}install[ \t]{1,40}(?P<name>[\w.-]{1,60})"),
    ),
    ("hex", re.compile(r"\{:(?P<name>\w{1,60}),[ \t]{0,40}\"[^\"\n]{0,40}\"")),
    (
        "pub",
        re.compile(
            r"\b(?:flutter|dart)[ \t]{1,40}pub[ \t]{1,40}add[ \t]{1,40}(?P<name>\w{1,60})"
        ),
    ),
    (
        "pub",
        re.compile(
            r"^[ \t]{1,40}(?P<name>\w{1,60}):[ \t]{0,40}['\"]?[\^~>=<]{0,2}\d[\w.+-]{0,30}['\"]?[ \t]{0,40}$",
            re.MULTILINE,
        ),
    ),
    (
        "packagist",
        re.compile(
            r"\bcomposer[ \t]{1,40}require[ \t]{1,40}(?P<name>[\w.-]{1,60}/[\w.-]{1,60})"
        ),
    ),
    (
        "nuget",
        re.compile(
            r"\bdotnet[ \t]{1,40}add[ \t]{1,40}(?:\S{1,200}[ \t]{1,40})?package[ \t]{1,40}(?P<name>[\w.]{1,60})"
        ),
    ),
    ("nuget", re.compile(r"\bInstall-Package[ \t]{1,40}(?P<name>[\w.]{1,60})")),
    (
        "nuget",
        re.compile(r"<PackageReference[ \t]{1,40}Include=\"(?P<name>[\w.]{1,60})\""),
    ),
    (
        "maven",
        re.compile(
            r"<groupId>(?P<group>[\w.]{1,60})</groupId>\s{0,20}"
            r"<artifactId>(?P<name>[\w.-]{1,60})</artifactId>"
        ),
    ),
    (
        "gradle",
        re.compile(
            r"\b(?:implementation|api|compileOnly|runtimeOnly)[ \t]{0,40}\(?[ \t]{0,40}['\"]"
            r"(?P<group>[\w.-]{1,60}):(?P<name>[\w.-]{1,60})(?::[\w.-]{1,30})?['\"]"
        ),
    ),
    (
        "cocoapods",
        re.compile(
            r"^[ \t]{0,40}pod[ \t]{1,40}['\"](?P<name>[\w.-]{1,60})['\"]", re.MULTILINE
        ),
    ),
    (
        "swiftpm",
        re.compile(
            r"\.package\([ \t]{0,40}(?:name:[ \t]{0,40}\"[^\"\n]{0,60}\",[ \t]{0,40})?url:[ \t]{0,40}\"(?P<name>[^\"\n]{1,200})\""
        ),
    ),
)

#: Builds a Maven artifact into the local repository (``~/.m2``).
MAVEN_LOCAL_INSTALL = re.compile(
    r"\b(?:\./)?mvnw?[ \t]{1,40}(?:-\S{1,40}[ \t]{1,40}){0,6}install\b"
)
#: Composer reads a package from a directory.
COMPOSER_PATH_REPO = re.compile(
    r"\bcomposer[ \t]{1,40}config[ \t]{1,40}repositories\.[\w-]{1,40}[ \t]{1,40}path\b"
)
FENCE = re.compile(
    r"^```[^\n]{0,200}\n(?P<body>.{0,20000}?)^```", re.MULTILINE | re.DOTALL
)
INSTALL_HEADING = re.compile(
    r"^#{2,4}[ \t]{1,40}(?:Install(?:ation)?|Setup)\b", re.MULTILINE
)


def _manifest(sdk: str, name: str) -> str:
    return (SDK_DIR / sdk / name).read_text(encoding="utf-8")


def published_identities() -> Set[Tuple[str, str]]:
    """(ecosystem, name) of every published package, from its manifest."""
    found = set()
    for directory in PUBLISHED:
        if (SDK_DIR / directory / "package.json").is_file():
            found.add(("npm", json.loads(_manifest(directory, "package.json"))["name"]))
        else:
            data = tomllib.loads(_manifest(directory, "pyproject.toml"))
            found.add(("pypi", data["project"]["name"].lower()))
    return found


def ours(ecosystem: str, name: str, group: Optional[str] = None) -> bool:
    """Whether a package name in an install command is one of this repository's."""
    if ecosystem == "npm":
        return name.startswith("@controlvar/")
    if ecosystem == "pypi":
        return name.lower() in {
            tomllib.loads(p.read_text("utf-8"))["project"]["name"].lower()
            for p in SDK_DIR.glob("*/pyproject.toml")
        }
    if ecosystem == "rubygems":
        return name in re.findall(
            r"\.name\s{0,40}=\s{0,40}\"([^\"]{1,40})\"",
            _manifest("ruby", "controlvar.gemspec"),
        )
    if ecosystem == "hex":
        return name in re.findall(
            r"app:\s{0,40}:(\w{1,40})", _manifest("elixir", "mix.exs")
        )
    if ecosystem == "pub":
        return name in re.findall(
            r"^name:\s{0,40}(\w{1,40})", _manifest("flutter", "pubspec.yaml"), re.M
        )
    if ecosystem == "packagist":
        return name == json.loads(_manifest("php", "composer.json"))["name"]
    if ecosystem == "nuget":
        return name.lower().startswith("controlvar")
    if ecosystem in ("maven", "gradle"):
        return (group or "").startswith("com.controlvar")
    if ecosystem == "cocoapods":
        return name.lower().startswith("controlvar")
    if ecosystem == "swiftpm":
        return "controlvar/controlvar" in name.lower()
    raise AssertionError(ecosystem)


def _arguments(ecosystem: str, args: str) -> Iterator[str]:
    """Package names among an npm or pip command's arguments."""
    tokens = args.split()
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if ecosystem == "pypi" and token in ("-e", "--editable"):
            skip_next = True
            continue
        if token.startswith(("-", "<", ".", "/", "~", "$")) or token.endswith(".tgz"):
            continue
        if "/" in token and not token.startswith("@"):
            continue
        if ecosystem == "npm":
            name = re.sub(r"(?<=.)@.{0,200}$", "", token)
        else:
            name = re.split(r"[\[=<>~!;]", token, maxsplit=1)[0]
        if name:
            yield name


def registry_installs(page_text: str) -> List[Tuple[str, str]]:
    """Every (ecosystem, package) of ours that the page installs from a registry."""
    found: List[Tuple[str, str]] = []
    maven_local = bool(MAVEN_LOCAL_INSTALL.search(page_text))
    gradle_local = maven_local and "mavenLocal()" in page_text
    path_repo_blocks = [
        (m.start(), m.end())
        for m in FENCE.finditer(page_text)
        if COMPOSER_PATH_REPO.search(m.group("body"))
    ]
    for ecosystem, pattern in INSTALLS:
        for match in pattern.finditer(page_text):
            groups = match.groupdict()
            if groups.get("args") is not None:
                names = list(_arguments(ecosystem, groups["args"]))
            else:
                names = [groups["name"]]
            for name in names:
                if not ours(ecosystem, name, groups.get("group")):
                    continue
                if ecosystem == "rubygems" and re.search(
                    r"\b(?:git|path|github):", groups.get("rest") or ""
                ):
                    continue
                if ecosystem == "packagist" and any(
                    start <= match.start() < end for start, end in path_repo_blocks
                ):
                    continue
                if ecosystem == "maven" and maven_local:
                    continue
                if ecosystem == "gradle" and gradle_local:
                    continue
                identity = (ecosystem, name.lower() if ecosystem == "pypi" else name)
                if identity not in published_identities():
                    found.append(identity)
    return found


#: (sample, the registry installs it holds). Each pattern above has a sample.
INSTALL_SAMPLES = (
    ("npm install @controlvar/edge-sdk", [("npm", "@controlvar/edge-sdk")]),
    (
        "yarn add @controlvar/react-native-sdk@0.1.0 md5",
        [("npm", "@controlvar/react-native-sdk")],
    ),
    (
        "import { x } from 'npm:@controlvar/edge-sdk/deno';",
        [("npm", "@controlvar/edge-sdk")],
    ),
    (
        "pip install openfeature-sdk controlvar controlvar-openfeature==0.1.0",
        [("pypi", "controlvar-openfeature")],
    ),
    ("```ruby\ngem 'controlvar', '~> 0.1'\n```\n", [("rubygems", "controlvar")]),
    ("gem install controlvar", [("rubygems", "controlvar")]),
    ('```elixir\n    {:controlvar, "~> 0.1.0"}\n```\n', [("hex", "controlvar")]),
    ("flutter pub add controlvar", [("pub", "controlvar")]),
    (
        "```yaml\ndependencies:\n  controlvar: ^0.2.0\n```\n",
        [("pub", "controlvar")],
    ),
    (
        "```bash\ncomposer require controlvar/sdk\n```\n",
        [("packagist", "controlvar/sdk")],
    ),
    ("dotnet add package Controlvar.SDK", [("nuget", "Controlvar.SDK")]),
    ("Install-Package Controlvar.SDK", [("nuget", "Controlvar.SDK")]),
    (
        '<PackageReference Include="Controlvar.SDK" Version="0.1.0" />',
        [("nuget", "Controlvar.SDK")],
    ),
    (
        "<groupId>com.controlvar</groupId>\n    <artifactId>controlvar-sdk</artifactId>",
        [("maven", "controlvar-sdk")],
    ),
    (
        'implementation("com.controlvar:controlvar-android:1.0.0")',
        [("gradle", "controlvar-android")],
    ),
    ("pod 'Controlvar'", [("cocoapods", "Controlvar")]),
    (
        '.package(url: "https://github.com/Controlvar/controlvar.git", from: "1.0.0")',
        [("swiftpm", "https://github.com/Controlvar/controlvar.git")],
    ),
    # Source installs, and the published packages, are not refused.
    ("npm install @controlvar/js-sdk", []),
    ("pip install controlvar", []),
    ("pip install -e sdk/python -e sdk/openfeature-python", []),
    (
        "npm install /path/to/controlvar/sdk/edge/controlvar-edge-sdk-0.2.0.tgz",
        [],
    ),
    (
        "gem 'controlvar', git: 'https://github.com/Controlvar/controlvar.git'",
        [],
    ),
    ('{:controlvar, git: "https://github.com/Controlvar/controlvar.git"}', []),
    (
        "```bash\ncomposer config repositories.controlvar path /p/sdk/php\n"
        "composer require controlvar/sdk:@dev\n```\n",
        [],
    ),
    (
        "`./mvnw install`, then add `mavenLocal()` and\n"
        "`implementation 'com.controlvar:controlvar-sdk:0.1.0'`",
        [],
    ),
)


@pytest.mark.parametrize("sample, expected", INSTALL_SAMPLES)
def test_each_install_pattern_reads_what_it_exists_for(sample, expected):
    assert registry_installs(sample) == expected


@pytest.mark.parametrize("page", DEFERRED)
def test_a_deferred_page_installs_no_unpublished_package_from_a_registry(page):
    found = registry_installs(text(page))
    assert not found, (
        f"{page} installs {found} from a registry, where it is not published"
        " (test_sdk_doc_versions.PUBLISHED); show the build from source instead"
    )


@pytest.mark.parametrize("page", DEFERRED)
def test_a_deferred_install_section_says_whether_it_is_published(page):
    page_text = text(page)
    if not INSTALL_HEADING.search(page_text) or page in PACKAGED:
        # A README a registry shows says nothing about its status: the
        # packaged-README contract below holds it instead (T230).
        return
    unpublished = [sdk for sdk in sdks_of(page) if sdk not in PUBLISHED]
    assert says_unpublished(page_text) == bool(unpublished), (
        f"{page}: its SDK is {'not ' if unpublished else ''}published"
        " (test_sdk_doc_versions.PUBLISHED); the page says otherwise"
    )


def test_every_deferred_page_with_an_install_section_is_read():
    """Exact counts, at this tree: the deferred pages with an install
    section, the ones the says-unpublished rule above holds, and the
    packaged READMEs the contract below holds instead. Each Plan P pull
    request that packs a README moves the second and third."""
    with_section = [page for page in DEFERRED if INSTALL_HEADING.search(text(page))]
    under_the_rule = [page for page in with_section if page not in PACKAGED]
    assert (len(with_section), len(under_the_rule), len(PACKAGED)) == (24, 23, 1), (
        with_section,
        sorted(PACKAGED),
    )


# ---------------------------------------------------------------------------
# 2b. The READMEs a registry shows (DECISIONS T230)
# ---------------------------------------------------------------------------

#: Each README a registry shows, with its SDK: an explicit list, not
#: ``DEFERRED`` (a packaged README need not be a deferred page). It is held
#: exactly to the SDKs scripts/check_packaged_readme.py has rules for, and to
#: the READMEs the packages actually ship.
PACKAGED: Dict[str, str] = {
    "sdk/dotnet/README.md": "dotnet",
}


def _script(name: str):
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_packaged_readmes_are_the_ones_the_gate_and_the_packages_name():
    from backend.tests.unit.docs.test_sdk_doc_versions import packaged_readmes, rel

    assert set(PACKAGED.values()) == set(_script("check_packaged_readme").I_README)
    assert set(PACKAGED) <= {rel(readme) for readme in packaged_readmes()}


def _contract_problems(page: str, page_text: str) -> List[str]:
    sdk = PACKAGED[page]
    version = _script("check_sdk_version").read_manifest_version(sdk)[0]
    return _script("check_packaged_readme").problems(sdk, page_text, version)


@pytest.mark.parametrize("page", sorted(PACKAGED))
def test_a_packaged_readme_keeps_the_contract(page):
    """The contract the release checks on the packed bytes, on the file in
    the tree: no status, no registry install, no own version, no relative
    link, and an Install section that links the SDK's page."""
    assert _contract_problems(page, text(page)) == []


@pytest.mark.parametrize("page", sorted(PACKAGED))
def test_a_packaged_readme_installs_nothing_from_a_registry(page):
    assert registry_installs(text(page)) == []


@pytest.mark.parametrize("page", sorted(PACKAGED))
def test_a_packaged_readme_claims_no_attribute_targeting_its_client_does_not_send(page):
    if PACKAGED[page] in SENDS_NO_CONTEXT:
        assert attribute_claims(text(page)) == []


@pytest.mark.parametrize("page", sorted(PACKAGED))
def test_a_registry_install_anywhere_in_a_packaged_readme_is_refused(page):
    """PP-3's plant, in memory: an install line in the Quick Start, outside
    the Install section, fails both the contract and the registry sweep."""
    original = text(page)
    planted = original.replace(
        "## Quick Start\n", "## Quick Start\n\n`dotnet add package Controlvar.SDK`\n", 1
    )
    assert planted != original
    assert any(
        "installs this repository's package from a registry" in p
        for p in _contract_problems(page, planted)
    )
    assert registry_installs(planted) == [("nuget", "Controlvar.SDK")]


#: A link to a page of this repository on GitHub, with a fragment.
BLOB_LINK = re.compile(
    r"https://github\.com/Controlvar/controlvar/blob/main/(?P<path>[\w./-]{1,200}?)#(?P<frag>[\w-]{1,100})"
)


def github_slug(heading: str) -> str:
    """The anchor GitHub gives a Markdown heading: lower case, markup and
    punctuation other than hyphens and underscores dropped, spaces as hyphens."""
    heading = re.sub(r"[`*]", "", heading.strip()).lower()
    return re.sub(r"[^\w\- ]", "", heading).replace(" ", "-")


@pytest.mark.parametrize("page", sorted(PACKAGED))
def test_each_fragment_a_packaged_readme_links_is_a_heading(page):
    """C4. A registry README is frozen at its tag; a fragment that names no
    heading leaves its reader at the top of a long page."""
    links = list(BLOB_LINK.finditer(text(page)))
    assert links, f"{page} links no page of this repository with a fragment"
    for link in links:
        target = REPO_ROOT / link.group("path")
        assert target.is_file(), link.group(0)
        slugs = {
            github_slug(m.group(1))
            for m in re.finditer(r"^#{1,6}[ \t]+(.+)$", target.read_text("utf-8"), re.M)
        }
        assert link.group("frag") in slugs, (link.group(0), sorted(slugs))


def test_the_slug_is_github_s():
    assert github_slug("Installation") == "installation"
    assert github_slug("TypeScript / Node.js Provider") == "typescript--nodejs-provider"
    assert github_slug("`useFlag(flagKey)`: FlagState") == "useflagflagkey-flagstate"


# ---------------------------------------------------------------------------
# 3. Attribute targeting on flags
# ---------------------------------------------------------------------------

#: The quoted evaluate path in code: a string literal, not a comment or a
#: docstring line such as "GET /api/v1/feature-flags/evaluate/...".
EVALUATE_LITERAL = re.compile(r"[\"'`]/?api/v1/feature-flags/evaluate")
COMMENT_LINE = re.compile(r"^\s{0,40}(?://|/\*|\*|#|--)")
SOURCE_SUFFIXES = {
    ".kt",
    ".cs",
    ".ts",
    ".tsx",
    ".ex",
    ".dart",
    ".swift",
    ".java",
    ".php",
    ".rb",
    ".py",
}
NOT_SOURCE = {
    "node_modules",
    "dist",
    "build",
    "target",
    "tests",
    "test",
    "__tests__",
    "Tests",
    "examples",
    "example",
    "jvm",
    "_build",
    "deps",
    "vendor",
}
#: A test, spec or standalone check file, which builds requests of its own.
TEST_FILE = re.compile(r"^(?:test|verify)_|_test\.|[Tt]ests?\.|[Ss]pec\.")
#: The providers that build no request themselves: the file and the call that
#: hands the flag to the underlying SDK, whose arguments decide what is sent.
DELEGATES: Dict[str, Tuple[str, "re.Pattern[str]"]] = {
    "openfeature": (
        "src/ExperimentationProvider.ts",
        re.compile(
            r"\.evaluateFlag\(\s{0,40}flagKey\s{0,40},\s{0,40}(?P<args>\{[^{}()]{0,120}\})\s{0,40}\)"
        ),
    ),
    "openfeature-python": (
        "src/controlvar_openfeature/provider.py",
        re.compile(r"\.get_feature_flag\((?P<args>[^()]{0,160})\)"),
    ),
}
#: The deferred SDKs whose flag evaluation sends no ``context`` today, as the
#: source reads. When one starts sending it, change this set with its pages.
SENDS_NO_CONTEXT = {
    "android",
    "dotnet",
    "edge",
    "elixir",
    "flutter",
    "ios",
    "java",
    "openfeature",
    "openfeature-python",
    "php",
    "react-native",
    "ruby",
}


def evaluate_requests(sdk: str) -> List[str]:
    """Each flag-evaluation request the SDK's source builds, as code text."""
    if sdk in DELEGATES:
        relative, pattern = DELEGATES[sdk]
        return [m.group("args") for m in pattern.finditer(_manifest(sdk, relative))]
    found = []
    for path in sorted((SDK_DIR / sdk).rglob("*")):
        relative = path.relative_to(SDK_DIR / sdk)
        if (
            not path.is_file()
            or path.suffix not in SOURCE_SUFFIXES
            or NOT_SOURCE.intersection(relative.parts[:-1])
            or TEST_FILE.search(path.name)
        ):
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
        for index, line in enumerate(lines):
            if EVALUATE_LITERAL.search(line) and not COMMENT_LINE.match(line):
                window = [
                    code
                    for code in lines[index : index + 4]
                    if not COMMENT_LINE.match(code)
                ]
                found.append("\n".join(window))
    return found


def sends_context(sdk: str) -> bool:
    requests = evaluate_requests(sdk)
    assert requests, f"sdk/{sdk}: no flag-evaluation request found in its source"
    return any(re.search(r"context|attributes", request, re.I) for request in requests)


#: (name, pattern, sample): a sentence that says attributes reach flag
#: evaluation. Matched on the folded page; ``[^.|]`` keeps each in a sentence
#: or a table cell.
CLAIMS: Tuple[Tuple[str, "re.Pattern[str]", str], ...] = (
    (
        "sent as context on flag evaluation",
        re.compile(
            r"\b(?:sent|passed|forwarded)\s{1,40}as\s{1,40}`?context`?(?:(?!\bnot\b)[^.|;]){0,60}?"
            r"\bflag\s{1,40}evaluation\b",
            re.I,
        ),
        "Sent as `context` on experiment assignment and on flag evaluation (targeting rules)",
    ),
    (
        "flag evaluation sends the attributes",
        re.compile(
            r"\bflag\s{1,40}evaluation\s{1,40}(?:also\s{1,40})?(?:sends|passes|forwards|carries)\s{1,40}"
            r"(?:the\s{1,40})?(?:user'?s?\s{1,40})?(?:attributes|context)\b",
            re.I,
        ),
        "Flag evaluation sends the user's attributes as `context`.",
    ),
    (
        "attribute targeting works",
        re.compile(
            r"\b(?:attribute|flag)\s{1,40}targeting\b[^.|;]{0,40}?(?<!not )(?<!n't )\b(?:works?|is\s{1,40}supported)\b",
            re.I,
        ),
        "so attribute targeting works on flags too",
    ),
    (
        "attributes = context",
        re.compile(r"\battributes\s{0,40}=\s{0,40}context\b", re.I),
        "// attributes = context",
    ),
)

#: True sentences about an SDK that sends no context: none may match.
NOT_CLAIMS = (
    "Flag evaluation sends only the user id. `GET /api/v1/feature-flags/evaluate/{key}?user_id=…`"
    " carries none of the user's attributes, so the server evaluates a flag's targeting rules"
    " against an empty context.",
    "`attributes` is accepted for source compatibility and not sent: the request carries the"
    " user id alone, so the server evaluates the flag's targeting rules against an empty context.",
    "Flag evaluation sends the user id only (see the note at the top of this page).",
    "attribute targeting does not work here",
    "| `getAssignment(...)` | Sticky server assignment; `attributes` → `context`. |",
    "| `attributes` | `Record<string, unknown>` | No | Sent as `context` on experiment assignment"
    " (targeting rules); not sent on flag evaluation |",
    'let user = User(id: "user-123", attributes: ["plan": "pro"])   // attributes = assignment context',
)


def attribute_claims(page_text: str) -> List[str]:
    folded = fold(page_text)
    return [name for name, pattern, _ in CLAIMS if pattern.search(folded)]


@pytest.mark.parametrize("name, pattern, sample", CLAIMS, ids=[c[0] for c in CLAIMS])
def test_each_claim_pattern_matches_the_sentence_it_exists_for(name, pattern, sample):
    assert pattern.search(fold(sample)), name


@pytest.mark.parametrize("sentence", NOT_CLAIMS)
def test_no_claim_pattern_matches_a_true_sentence(sentence):
    assert attribute_claims(sentence) == []


def test_what_each_deferred_sdk_sends_is_read_from_its_source():
    sdks = sorted({sdk for page in DEFERRED for sdk in sdks_of(page)})
    no_context = {sdk for sdk in sdks if not sends_context(sdk)}
    assert no_context == SENDS_NO_CONTEXT, (
        "an SDK changed what its flag evaluation sends: update SENDS_NO_CONTEXT"
        " and that SDK's pages together"
    )
    assert sends_context("react")


def test_the_claim_patterns_find_the_true_sentence_on_the_react_page():
    # React sends the context, and its page says so: the patterns must see it.
    assert "sent as context on flag evaluation" in attribute_claims(
        text("docs/sdk/react.md")
    )


@pytest.mark.parametrize("page", DEFERRED)
def test_a_page_claims_no_attribute_targeting_its_client_does_not_send(page):
    silent = [sdk for sdk in sdks_of(page) if sdk in SENDS_NO_CONTEXT]
    if not silent:
        return
    claims = attribute_claims(text(page))
    assert not claims, (
        f"{page}: {', '.join(silent)} sends the user id alone on flag evaluation,"
        f" but the page says attributes reach it ({claims}); say what it sends"
    )
