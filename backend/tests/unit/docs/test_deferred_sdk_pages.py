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
   that package is on its registry (``ON_REGISTRY`` in
   ``test_sdk_doc_versions.py``; ``published_identities()`` reads each one's
   name from its npm, PyPI, Maven or NuGet manifest).
   A source install is not a registry one: a path, a tarball, ``pip install
   -e``, a ``git:``/``path:`` dependency, a Composer path repository in the
   same block, a Maven coordinate on a page that installs it into ``~/.m2``
   (and, for Gradle, reads it there with ``mavenLocal()``). A page with an
   install section says "not yet published" exactly when its SDK is not on
   its registry, except a README a registry shows (``PACKAGED``): that one
   states no status at all, installs nothing from a registry whether or not
   its package is on one, and keeps the packaged-README contract of
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
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path
from typing import AbstractSet, Dict, Iterator, List, Optional, Set, Tuple

import pytest

from backend.tests.unit.docs.test_sdk_doc_versions import ON_REGISTRY, says_unpublished

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


def _script(name: str):
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    # The .NET 10 SDK's noun-first command and a file-based app's directive.
    (
        "nuget",
        re.compile(
            r"\bdotnet[ \t]{1,40}package[ \t]{1,40}add[ \t]{1,40}(?P<name>[\w.]{1,60})"
        ),
    ),
    ("nuget", re.compile(r"#:package[ \t]{1,40}(?P<name>[\w.]{1,60})")),
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


@lru_cache(maxsize=None)
def _check_sdk_version():
    """scripts/check_sdk_version.py, loaded once: its ``ECOSYSTEMS`` says how
    the release publishes each SDK, and its NuGet reader which project it packs."""
    return _script("check_sdk_version")


POM_NAMESPACE = {"m": "http://maven.apache.org/POM/4.0.0"}
#: A Maven ``groupId:artifactId``.
COORDINATE = re.compile(r"[\w.-]{1,100}:[\w.-]{1,100}")


def _pom(path: Path, field: str) -> str:
    return (ET.parse(path).getroot().findtext(f"m:{field}", "", POM_NAMESPACE)).strip()


def maven_coordinates(sdk: str) -> Set[str]:
    """``groupId:artifactId`` of the SDK's parent POM and of each module it
    lists: Central serves every one, the parent too. A module that names no
    groupId of its own has its parent's."""
    parent = SDK_DIR / sdk / "pom.xml"
    group = _pom(parent, "groupId")
    found = {f"{group}:{_pom(parent, 'artifactId')}"}
    modules = ET.parse(parent).getroot().findall("m:modules/m:module", POM_NAMESPACE)
    for module in modules:
        child = SDK_DIR / sdk / (module.text or "").strip() / "pom.xml"
        found.add(f"{_pom(child, 'groupId') or group}:{_pom(child, 'artifactId')}")
    assert modules and all(COORDINATE.fullmatch(c) for c in found), (sdk, found)
    return found


def package_identities(sdk: str) -> Set[Tuple[str, str]]:
    """How the registry names the SDK's package, read from the manifest its
    release reads (``ECOSYSTEMS`` in scripts/check_sdk_version.py), each as
    ``registry_key`` writes it:

    * npm: ``package.json``'s name;
    * PyPI: ``pyproject.toml``'s name;
    * Maven Central: the ``groupId:artifactId`` of the parent POM and of each
      module, as a ``<dependency>`` (``maven``) and as a Gradle coordinate
      (``gradle``) names it;
    * NuGet: the ``<PackageId>`` of the project the release packs.

    An SDK released another way (Go, whose tag is the release) or not wired
    to a registry has no such name, and asking for one is an error: such an
    SDK cannot be in ``ON_REGISTRY``.
    """
    ecosystem = _check_sdk_version().ECOSYSTEMS.get(sdk)
    if ecosystem == "npm":
        found = {("npm", json.loads(_manifest(sdk, "package.json"))["name"])}
    elif ecosystem == "pypi":
        found = {
            ("pypi", tomllib.loads(_manifest(sdk, "pyproject.toml"))["project"]["name"])
        }
    elif ecosystem == "maven":
        found = {
            (kind, c) for c in maven_coordinates(sdk) for kind in ("maven", "gradle")
        }
    elif ecosystem == "nuget":
        found = {
            ("nuget", _check_sdk_version().read_nuget_version(sdk, SDK_DIR / sdk)[1])
        }
    else:
        raise AssertionError(
            f"sdk/{sdk} is {ecosystem!r} in scripts/check_sdk_version.py's ECOSYSTEMS,"
            " so it has no npm, PyPI, Maven Central or NuGet name to read:"
            " ON_REGISTRY cannot hold it"
        )
    return {registry_key(*identity) for identity in found}


def registry_key(ecosystem: str, name: str) -> Tuple[str, str]:
    """An identity as its registry compares names: PyPI and NuGet ignore case."""
    return (ecosystem, name.lower() if ecosystem in ("pypi", "nuget") else name)


def published_identities() -> Set[Tuple[str, str]]:
    """(ecosystem, name) of every package on its registry (``ON_REGISTRY``)."""
    return {identity for sdk in ON_REGISTRY for identity in package_identities(sdk)}


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


def registry_installs(
    page_text: str, published: Optional[AbstractSet[Tuple[str, str]]] = None
) -> List[Tuple[str, str]]:
    """Every (ecosystem, package) of ours that the page installs from a
    registry and that is not in *published* (``published_identities()``
    unless given). A Maven or Gradle package is its ``groupId:artifactId``."""
    if published is None:
        published = published_identities()
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
                if ecosystem in ("maven", "gradle"):
                    identity = (ecosystem, f"{groups['group']}:{name}")
                if registry_key(*identity) not in published:
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
    ("dotnet package add Controlvar.SDK@0.1.0", [("nuget", "Controlvar.SDK")]),
    ("#:package Controlvar.SDK@0.1.0", [("nuget", "Controlvar.SDK")]),
    ("Install-Package Controlvar.SDK", [("nuget", "Controlvar.SDK")]),
    (
        '<PackageReference Include="Controlvar.SDK" Version="0.1.0" />',
        [("nuget", "Controlvar.SDK")],
    ),
    (
        "<groupId>com.controlvar</groupId>\n    <artifactId>controlvar-sdk</artifactId>",
        [("maven", "com.controlvar:controlvar-sdk")],
    ),
    (
        'implementation("com.controlvar:controlvar-android:1.0.0")',
        [("gradle", "com.controlvar:controlvar-android")],
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


# The registry names a package has, read before it joins ON_REGISTRY. The Java
# and .NET names are public for good from their first publish (#1498).
JAVA_COORDINATES = {
    "com.controlvar:controlvar-sdk-parent",
    "com.controlvar:controlvar-sdk",
    "com.controlvar:controlvar-spring-boot-starter",
}


def test_the_maven_reader_reads_the_parent_and_each_module():
    assert package_identities("java") == {
        (kind, coordinate)
        for coordinate in JAVA_COORDINATES
        for kind in ("maven", "gradle")
    }
    # The version check of test_sdk_doc_versions.py reads the same POMs.
    from backend.tests.unit.docs.test_sdk_doc_versions import maven_versions

    assert set(maven_versions()) == JAVA_COORDINATES


def test_the_nuget_reader_reads_the_packed_projects_id():
    assert package_identities("dotnet") == {("nuget", "controlvar.sdk")}


@pytest.mark.parametrize(
    "sdk",
    sorted(
        sdk
        for sdk, ecosystem in _check_sdk_version().ECOSYSTEMS.items()
        if ecosystem in ("npm", "pypi", "maven", "nuget")
    ),
)
def test_each_registry_sdk_has_the_name_its_release_checks(sdk):
    """The name scripts/check_sdk_version.py reads for the tag (a Maven
    parent's coordinate, a NuGet id) is one the reader here gives."""
    ecosystem = _check_sdk_version().ECOSYSTEMS[sdk]
    name = _check_sdk_version().read_manifest_version(sdk)[1]
    assert registry_key(ecosystem, name) in package_identities(sdk)


@pytest.mark.parametrize(
    "sdk", ["go", "ruby", "php", "elixir", "flutter", "android", "ios"]
)
def test_an_sdk_with_no_registry_name_cannot_be_on_the_registry(sdk, monkeypatch):
    with pytest.raises(AssertionError, match="ON_REGISTRY cannot hold it"):
        package_identities(sdk)
    monkeypatch.setitem(globals(), "ON_REGISTRY", (*ON_REGISTRY, sdk))
    with pytest.raises(AssertionError, match=f"sdk/{sdk} is"):
        published_identities()


#: Install lines for the Java and .NET packages: refused today, allowed once
#: java and dotnet are in ON_REGISTRY.
JAVA_AND_DOTNET_INSTALLS = (
    "<groupId>com.controlvar</groupId>\n    <artifactId>controlvar-sdk</artifactId>",
    "<groupId>com.controlvar</groupId>\n"
    "    <artifactId>controlvar-spring-boot-starter</artifactId>",
    'implementation("com.controlvar:controlvar-sdk:0.1.0")',
    "implementation 'com.controlvar:controlvar-spring-boot-starter:0.1.0'",
    "dotnet add package Controlvar.SDK --version 0.1.0",
    "dotnet add package controlvar.sdk",
    '<PackageReference Include="Controlvar.SDK" Version="0.1.0" />',
)


@pytest.mark.parametrize("line", JAVA_AND_DOTNET_INSTALLS)
def test_a_java_or_dotnet_install_is_allowed_once_its_sdk_is_on_the_registry(
    line, monkeypatch
):
    assert registry_installs(line) != []
    monkeypatch.setitem(globals(), "ON_REGISTRY", (*ON_REGISTRY, "java", "dotnet"))
    assert registry_installs(line) == []


@pytest.mark.parametrize(
    "line, expected",
    [
        (
            'implementation("com.controlvar:controlvar-android:1.0.0")',
            [("gradle", "com.controlvar:controlvar-android")],
        ),
        (
            "<groupId>com.controlvar.other</groupId>\n<artifactId>controlvar-sdk</artifactId>",
            [("maven", "com.controlvar.other:controlvar-sdk")],
        ),
        (
            "dotnet add package Controlvar.SDK.Extras",
            [("nuget", "Controlvar.SDK.Extras")],
        ),
        ("npm install @controlvar/react-sdk", [("npm", "@controlvar/react-sdk")]),
    ],
)
def test_java_and_dotnet_on_the_registry_allow_only_their_own_names(
    line, expected, monkeypatch
):
    monkeypatch.setitem(globals(), "ON_REGISTRY", (*ON_REGISTRY, "java", "dotnet"))
    assert registry_installs(line) == expected


@pytest.mark.parametrize("page", DEFERRED)
def test_a_deferred_page_installs_no_unpublished_package_from_a_registry(page):
    found = registry_installs(text(page))
    assert not found, (
        f"{page} installs {found} from a registry, where it is not published"
        " (test_sdk_doc_versions.ON_REGISTRY); show the build from source instead"
    )


@pytest.mark.parametrize("page", DEFERRED)
def test_a_deferred_install_section_says_whether_it_is_published(page):
    page_text = text(page)
    if not INSTALL_HEADING.search(page_text) or page in PACKAGED:
        # A README a registry shows says nothing about its status: the
        # packaged-README contract below holds it instead (T230).
        return
    unpublished = [sdk for sdk in sdks_of(page) if sdk not in ON_REGISTRY]
    assert says_unpublished(page_text) == bool(unpublished), (
        f"{page}: its SDK is {'not ' if unpublished else ''}published"
        " (test_sdk_doc_versions.ON_REGISTRY); the page says otherwise"
    )


def test_every_deferred_page_with_an_install_section_is_read():
    """Exact counts, at this tree: the deferred pages with an install
    section, the ones the says-unpublished rule above holds, and the
    packaged READMEs the contract below holds instead. Each Plan P pull
    request that packs a README moves the second and third."""
    with_section = [page for page in DEFERRED if INSTALL_HEADING.search(text(page))]
    under_the_rule = [page for page in with_section if page not in PACKAGED]
    assert (len(with_section), len(under_the_rule), len(PACKAGED)) == (24, 21, 6), (
        with_section,
        sorted(PACKAGED),
    )


# ---------------------------------------------------------------------------
# 2b. The READMEs a registry shows (DECISIONS T230)
# ---------------------------------------------------------------------------

#: Each README a registry shows, with its SDK: an explicit list, not
#: ``DEFERRED`` (a packaged README need not be a deferred page: the Python
#: provider's and React Native's have claims registries of their own). It is
#: held exactly to the SDKs scripts/check_packaged_readme.py holds to
#: I-README, and to the READMEs the packages actually ship.
PACKAGED: Dict[str, str] = {
    "sdk/dotnet/README.md": "dotnet",
    "sdk/mcp/README.md": "mcp",
    "sdk/openfeature-python/README.md": "openfeature-python",
    "sdk/openfeature/README.md": "openfeature",
    "sdk/react-native/README.md": "react-native",
    "sdk/react/README.md": "react",
}

#: For each packaged README: the heading a planted registry install goes
#: under (outside the Install section), the line planted, and what the
#: registry sweep must then find. The plant must change the page.
PLANTS: Dict[str, Tuple[str, str, Tuple[str, str]]] = {
    "sdk/dotnet/README.md": (
        "## Quick Start\n",
        "`dotnet add package Controlvar.SDK`",
        ("nuget", "Controlvar.SDK"),
    ),
    "sdk/mcp/README.md": (
        "## Configuration\n",
        "`npm install @controlvar/mcp-server`",
        ("npm", "@controlvar/mcp-server"),
    ),
    "sdk/openfeature-python/README.md": (
        "## Quick start\n",
        "`pip install controlvar-openfeature`",
        ("pypi", "controlvar-openfeature"),
    ),
    "sdk/openfeature/README.md": (
        "## Quick start\n",
        "`npm install @controlvar/openfeature-provider`",
        ("npm", "@controlvar/openfeature-provider"),
    ),
    "sdk/react-native/README.md": (
        "## Quick start\n",
        "`npm install @controlvar/react-native-sdk`",
        ("npm", "@controlvar/react-native-sdk"),
    ),
    "sdk/react/README.md": (
        "## Provider setup\n",
        "`npm install @controlvar/react-sdk`",
        ("npm", "@controlvar/react-sdk"),
    ),
}


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
    # Nothing, whether or not its package is on its registry yet: the README
    # is frozen at the tag (T230), so the sweep is given no published names.
    assert registry_installs(text(page), published=frozenset()) == []


@pytest.mark.parametrize("page", sorted(PACKAGED))
def test_a_packaged_readme_claims_no_attribute_targeting_its_client_does_not_send(page):
    if PACKAGED[page] in SENDS_NO_CONTEXT:
        assert attribute_claims(text(page)) == []


def test_every_packaged_readme_has_a_plant():
    assert set(PLANTS) == set(PACKAGED)


@pytest.mark.parametrize("page", sorted(PACKAGED))
def test_a_registry_install_anywhere_in_a_packaged_readme_is_refused(page):
    """PP-3's plant, in memory: an install line in a section other than
    Install fails both the contract and the registry sweep."""
    heading, line, expected = PLANTS[page]
    original = text(page)
    planted = original.replace(heading, f"{heading}\n{line}\n", 1)
    assert planted != original
    assert any(
        "installs this repository's package from a registry" in p
        for p in _contract_problems(page, planted)
    )
    assert registry_installs(planted, published=frozenset()) == [expected]


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
    "php",
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


def test_the_claim_patterns_find_the_true_sentence_on_the_react_native_page():
    # React Native sends the context since #1520, and its page says so: the
    # patterns must see it, so that react-native put back in SENDS_NO_CONTEXT
    # fails test_a_page_claims_no_attribute_targeting_its_client_does_not_send
    # on docs/sdk/react-native.md as well as here.
    assert "react-native" not in SENDS_NO_CONTEXT
    assert sends_context("react-native")
    assert "sent as context on flag evaluation" in attribute_claims(
        text("docs/sdk/react-native.md")
    )


def test_the_react_native_readme_says_the_attributes_are_sent_and_cites_no_438():
    # The README npm shows for React Native's first release is frozen with it:
    # it must say what the client does since #1520 (the attributes go as
    # `context` on flag evaluation), and carry none of the #438 limit that
    # iOS, Android and Flutter keep.
    readme = text("sdk/react-native/README.md")
    assert "sent as context on flag evaluation" in attribute_claims(readme)
    assert "438" not in readme
    assert "sends only the user id" not in fold(readme).lower()


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


def test_the_react_page_names_the_ssr_entry_the_package_ships():
    """docs/sdk/react.md names `@controlvar/react-sdk/ssr`, and the package has it.

    The page is in ``CLAIMS_DEFERRED``, so no claims entry binds its App Router
    sentence. It once told readers to call ``ServerClient`` from a server
    component without naming an import path that exists (#1518); this holds the
    fixed sentence, and the files that make the path real, in place, and keeps
    the page from naming a path inside the package's ``dist``.
    """
    page = fold(text("docs/sdk/react.md"))
    assert "@controlvar/react-sdk/ssr" in page
    assert (
        "In the App Router call the same methods from an async server component."
        not in page
    )
    # The package's `exports` map refuses deep imports, so a page that sends
    # readers into `dist` gives an instruction Node rejects.
    assert "@controlvar/react-sdk/dist" not in page
    manifest = json.loads((REPO_ROOT / "sdk/react/package.json").read_text("utf-8"))
    assert "ssr" in manifest["files"]
    assert (REPO_ROOT / "sdk/react/ssr/package.json").is_file()


# ---------------------------------------------------------------------------
# 4. The React SDK's exposure hook (#1499)
# ---------------------------------------------------------------------------

#: Where the React SDK's jest tests live; every test named below is in one of them.
REACT_TESTS = REPO_ROOT / "sdk" / "react" / "src" / "__tests__"
REACT_PAGE = "docs/sdk/react.md"
REACT_README = "sdk/react/README.md"

#: Each sentence the React pages state about ``onExposure`` (as written, folded
#: to one line), the pages that state it, and the jest tests that fail when it
#: stops being true. Both pages are in ``CLAIMS_DEFERRED``, so no claims
#: registry binds them; this does. An ``it.each`` test is named by its template.
REACT_EXPOSURE_SENTENCES: Tuple[Tuple[str, Tuple[str, ...], Tuple[str, ...]], ...] = (
    (
        "The client calls it once each time the server enrols the user in an experiment and"
        " the answer did not come from the client's cache: the moment the server records the"
        " exposure.",
        (REACT_PAGE,),
        (
            "is called once for an assigned: true answer, with exactly the four values",
            "is not called for a cached answer, however often it is asked for",
            "calls onExposure once when useExperiment mounts, with the four values",
        ),
    ),
    (
        "is a function the client calls once each time the server enrols the user in an"
        " experiment and the answer did not come from the client's cache: the moment the"
        " server records the exposure.",
        (REACT_README,),
        (
            "is called once for an assigned: true answer, with exactly the four values",
            "is not called for a cached answer, however often it is asked for",
            "calls its own onExposure once for two assignments of one user",
        ),
    ),
    (
        "Called once each time the server enrols the user in an experiment and the answer is"
        " not cached",
        (REACT_PAGE,),
        (
            "is called once for an assigned: true answer, with exactly the four values",
            "is not called for a cached answer, however often it is asked for",
        ),
    ),
    (
        "It receives an `Exposure`, a new object each time: `experimentKey`, `userId`,"
        " `variantId` (`null` when the server sends none) and `variantName`. Nothing else:"
        " not the user's attributes and not the variant's configuration.",
        (REACT_PAGE,),
        (
            "is called once for an assigned: true answer, with exactly the four values",
            "passes a null variantId when the server sends none",
            "receives a new object that does not alias the cached answer",
            "has four readonly fields",
        ),
    ),
    (
        "It receives an `Exposure`: `experimentKey`, `userId`, `variantId` (`null` when the"
        " server sends none) and `variantName`.",
        (REACT_README,),
        (
            "is called once for an assigned: true answer, with exactly the four values",
            "passes a null variantId when the server sends none",
        ),
    ),
    (
        "It is not called for:",
        (REACT_PAGE,),
        ("is not called for a cached answer, however often it is asked for",),
    ),
    (
        "- an answer from the client's cache; - a user the experiment did not enrol"
        " (`assigned: false`, with `reason` `'holdout'`, `'mutual_exclusion'` or"
        " `'targeting'`), or an experiment that is not running (`'draft'`, `'paused'`,"
        " `'completed'` or `'archived'`): the server records no exposure for them; - a call"
        " that fails; - a feature flag.",
        (REACT_PAGE,),
        (
            "is not called for a cached answer, however often it is asked for",
            "is not called for an assigned: false answer (%s), and the answer is unchanged",
            "is not called when the call fails (%s)",
            "is not called when the call times out",
            "is never called for a flag",
        ),
    ),
    (
        "It is not called for an answer from the client's cache, a user the experiment did"
        " not enrol (`assigned: false`), a call that fails or a feature flag.",
        (REACT_README,),
        (
            "is not called for a cached answer, however often it is asked for",
            "is not called for an assigned: false answer (%s), and the answer is unchanged",
            "is not called when the call fails (%s)",
            "is never called for a flag",
        ),
    ),
    (
        "Calls for the same user and experiment that are made while the first is still"
        " waiting share its request, so together they call it once.",
        (REACT_PAGE,),
        ("is called once for concurrent first calls that share one request",),
    ),
    (
        "`useExperiment` asks for the assignment in the browser, after the first render, so a"
        " server render makes no request and calls nothing. A server render followed by"
        " hydration calls it once, in the browser, and so does React's StrictMode, which runs"
        " effects twice in development.",
        (REACT_PAGE,),
        (
            "is not called, and the render makes no request",
            "is not called under StrictMode either",
            "calls it once after hydrating server markup",
            "calls it once under StrictMode, which runs the effect twice",
        ),
    ),
    (
        "A Provider rendered on the server calls nothing: `useExperiment` asks for the"
        " assignment in the browser, after the first render.",
        (REACT_README,),
        ("is not called, and the render makes no request",),
    ),
    (
        "Changing `onExposure` between renders does not create a new client, so nothing is"
        " asked again; the client calls the function from the latest render.",
        (REACT_PAGE,),
        ("keeps the client when onExposure changes, and calls the latest function",),
    ),
    (
        "(a new client starts with empty caches), not when `onExposure` does;",
        (REACT_PAGE,),
        ("keeps the client when onExposure changes, and calls the latest function",),
    ),
    (
        "It is called again, and the server records another exposure, after the cache"
        " lifetime (`cacheTtlMs`, 5 minutes by default), after `clearCache()`, and in every"
        " new client: each page load, and each change of `apiKey`, `baseUrl`, `timeoutMs` or"
        " `cacheTtlMs`.",
        (REACT_PAGE,),
        (
            "after the cache lifetime (one millisecond past it, never at the boundary)",
            "after clearCache()",
            "in a new client",
            "creates a new client when apiKey changes",
        ),
    ),
    (
        "It never receives one the server did not record.",
        (REACT_PAGE,),
        (
            "is not called for an assigned: false answer (%s), and the answer is unchanged",
            "is not called when assigned is %s, and the answer is still returned",
        ),
    ),
    (
        "It runs before the assignment resolves, and before every call waiting on the same"
        " request,",
        (REACT_PAGE,),
        (
            "runs before assignExperiment resolves",
            "is called once for concurrent first calls that share one request",
        ),
    ),
    (
        "The client does not wait for a Promise it returns.",
        (REACT_PAGE,),
        ("reports a rejection, once, as the hook error, without awaiting it",),
    ),
    (
        "If it throws, or returns a Promise that rejects, the answer is unchanged, and nothing"
        " reports the error: this SDK has no `onError`.",
        (REACT_PAGE, REACT_README),
        (
            "that throws leaves the answer unchanged and cached",
            "that returns a rejected Promise leaves the answer unchanged",
            "leaves useExperiment with the answer and no error",
            "leaves ServerClient answering, with no error",
            "passes on what onExposure returns, so a Promise it returns has its rejection handled",
            "is given the dropping report by the clients: this SDK reports the failure nowhere",
        ),
    ),
    (
        "A failing `onExposure` changes no answer and is reported nowhere",
        (REACT_PAGE,),
        (
            "that throws leaves the answer unchanged and cached",
            "is given the dropping report by the clients: this SDK reports the failure nowhere",
        ),
    ),
    (
        "`ServerClient` takes `onExposure` in its config too, and calls it for the"
        " assignments it makes, in the same way. It is a separate client with its own cache:",
        (REACT_PAGE,),
        (
            "calls its own onExposure once for two assignments of one user",
            "never calls the onExposure of a Provider",
        ),
    ),
    (
        "the server records two exposures, and each client that has `onExposure` calls its"
        " own.",
        (REACT_PAGE,),
        (
            "calls its own onExposure once for two assignments of one user",
            "never calls the onExposure of a Provider",
        ),
    ),
    (
        "A `ServerClient` created in each request, as in the `getServerSideProps` example"
        " above, starts with an empty cache, so it calls `onExposure` on every request that"
        " assigns, and the server records an exposure on each.",
        (REACT_PAGE,),
        (
            "in a new client",
            "calls its own onExposure once for two assignments of one user",
        ),
    ),
    (
        "interface Exposure {          // what onExposure receives; also exported from"
        " '@controlvar/react-sdk/ssr'",
        (REACT_PAGE,),
        ("is exported as a type from the package and from ssr",),
    ),
    (
        "The Provider and `ServerClient` throw `onExposure must be a function` for any other"
        " value; `null` and `undefined` set none.",
        (REACT_PAGE,),
        (
            "is refused by the client and ServerClient: %p",
            "is refused by the Provider when it renders: %p",
            "null and undefined set none",
        ),
    ),
    (
        'In a test, a mocked `/assign` answer must include `"assigned": true`, or the hook is'
        " not called. This SDK treats an answer without `assigned` as enrolled for"
        " `ExperimentAssignment.assigned`, but does not call the hook for it.",
        (REACT_PAGE,),
        ("is not called when assigned is %s, and the answer is still returned",),
    ),
)


def _react_test_titles() -> str:
    return "\n".join(
        path.read_text("utf-8") for path in sorted(REACT_TESTS.glob("*.test.ts*"))
    )


def _h2s(page: str) -> List[str]:
    """The level-2 headings of a page, outside fenced blocks."""
    without_fences = re.sub(r"^```.*?^```[ \t]*$", "", text(page), flags=re.S | re.M)
    return re.findall(r"^##[ \t]+(.+?)[ \t]*$", without_fences, re.M)


def test_the_react_pages_give_the_exposure_hook_a_section_of_its_own():
    """H-M9: an H2 "Exposure hook" (``#exposure-hook``), never in a Quick
    Start; the README's links to the page's section, absolutely."""
    for page in (REACT_PAGE, REACT_README):
        h2s = _h2s(page)
        assert h2s.count("Exposure hook") == 1, (page, h2s)
        assert not any("quick start" in h.lower() for h in h2s), (page, h2s)
    assert (
        "https://github.com/Controlvar/controlvar/blob/main/docs/sdk/react.md#exposure-hook"
        in text(REACT_README)
    )


@pytest.mark.parametrize(
    "sentence, pages, tests",
    REACT_EXPOSURE_SENTENCES,
    ids=[s[:60] for s, _, _ in REACT_EXPOSURE_SENTENCES],
)
def test_each_react_exposure_sentence_is_on_its_page_and_held_by_a_test(
    sentence, pages, tests
):
    titles = _react_test_titles()
    unsaid = [page for page in pages if sentence not in fold(text(page))]
    assert unsaid == [], f"no longer said there: {sentence}"
    missing = [title for title in tests if f"'{title}'" not in titles]
    assert missing == [], "no jest test in sdk/react/src/__tests__ has these names"


def test_the_react_refusal_the_pages_quote_is_the_one_the_source_throws():
    source = (REPO_ROOT / "sdk/react/src/client/exposure.ts").read_text("utf-8")
    assert "throw new Error('onExposure must be a function')" in source
    assert "`onExposure must be a function`" in text(REACT_PAGE)
