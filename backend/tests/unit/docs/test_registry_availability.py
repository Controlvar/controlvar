"""A page that says an SDK is on a package registry names only SDKs that are.

``ON_REGISTRY`` in test_sdk_doc_versions.py lists the SDK directories whose
package is on its registry (#1498). Every sentence in the documentation, the
READMEs and the dashboard's and controlvar.com's pages that puts an SDK on
npm, PyPI, Maven Central or NuGet (or on RubyGems, Hex, pub.dev, Packagist or
CocoaPods, where none of ours is) is read here, and each SDK it names must be
in ``ON_REGISTRY``, with that registry as the one it is released to
(``ECOSYSTEMS`` in scripts/check_sdk_version.py). So no page says a package
is on its registry before it is, and the change that adds an SDK to the set
is the change that may say so.

A claim is ``on``, ``from`` or ``to`` before a registry's name, or a list of
them (each may be a Markdown link). It is not one when the text before it
negates it (``not``, ``n't``, ``never``, ``nor``, ...), makes it a condition
or a future (``if``, ``once``, ``when``, ``will``, ...), or when its sentence
is a question. What it is about is the text before it, back to the start of
its sentence, a ``;``, ``:``, ``|``, a tag or a bracket, or back to the
previous claim; a parenthesis in that text is an aside (an install line, a
version, a link's target) and is left out. "So are ..." after a claim is a
claim about the rest of its sentence. In that text an SDK is named:

* by a package name, read from the SDK's manifest
  (``package_identities`` in test_deferred_sdk_pages.py) and looked up on the
  claimed registry. A name of ours that is no package there
  (``@controlvar/...``, ``com.controlvar:...``, ``Controlvar.*``, or one of
  our package names in backticks) is refused as it stands;
* by what the pages call it (``NAMES``: "JavaScript", "React Native", "the
  OpenFeature provider for Python", ".NET", ...);
* on an SDK's own page, by "it", "the package", "this SDK" and the like.

A sweep, not a parser: a paraphrase it does not read passes, and review
catches the rest. Each form it reads has a sample below that it must read,
and each form it must not read has one too. It reads files only, with no git,
so it runs the same in scripts/core_build.sh's copy. It is
``mechanism:registry-availability`` in scripts/doc_claims.py, so a claims
unit that says an SDK is on a registry can cite it.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Dict, FrozenSet, List, Set, Tuple

import pytest

from backend.tests.unit.docs.test_deferred_sdk_pages import (
    EXTRA_SDKS,
    _check_sdk_version,
    package_identities,
    registry_key,
)
from backend.tests.unit.docs.test_sdk_doc_versions import ON_REGISTRY, PUBLISHED

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
SDK_DIR = REPO_ROOT / "sdk"

#: The registries a claim can name: (pattern, the ecosystem as ECOSYSTEMS
#: spells it, or the registry's own name where no SDK of ours is released).
REGISTRIES: Tuple[Tuple[str, str], ...] = (
    ("npm", "npm"),
    ("PyPI", "pypi"),
    ("Maven(?: Central)?", "maven"),
    ("NuGet", "nuget"),
    ("RubyGems", "rubygems"),
    ("Hex", "hex"),
    (r"pub\.dev", "pub"),
    ("Packagist", "packagist"),
    ("CocoaPods", "cocoapods"),
)
#: The ecosystems package_identities() reads a package name for.
NAMED = ("npm", "pypi", "maven", "nuget")
_ONE = (
    r"\[?(?:"
    + "|".join(pattern for pattern, _ in REGISTRIES)
    + r")(?![\w-])\]?(?:\([^()\s]{1,300}\))?"
)
#: ``on``/``from``/``to`` (optionally followed by ``the``), or ``in the``, and
#: one registry, or a list of them: "is on npm", "is published to the npm
#: registry", "is available in the npm registry".
CLAIM = re.compile(
    r"\b(?:(?:on|from|to)(?: the)?|in the) (?P<registries>"
    + _ONE
    + r"(?:(?:,| and| or|, and|, or) "
    + _ONE
    + r"){0,8})",
    re.IGNORECASE,
)
#: "So are the React SDK, ..." after a claim, in a sentence of its own.
SO_ARE = re.compile(
    r"(?:^|(?<=[.!?;] ))(?:So|so|As|as) (?:is|are) (?P<rest>[^.!?;:|]{1,400})"
)
#: Text before a claim that makes it no claim of availability.
NEGATED = re.compile(
    r"\b(?:not|no|never|nor|neither|without|instead of|rather than)\b|n't\b",
    re.IGNORECASE,
)
CONDITIONAL = re.compile(
    r"\b(?:if|once|when|whenever|until|unless|before|after|will|would|should)\b",
    re.IGNORECASE,
)
#: Where what a claim is about starts: the end of a sentence or a colon
#: before a space (not ``com.controlvar:...`` or ``https://``), a semicolon, a
#: table cell, a tag, a brace or a bracket left open.
BOUNDARY = re.compile(r"[.!?:][\"'”’]{0,2}(?=\s)|[;|<>{}()]")
#: An aside: a parenthesis with no parenthesis inside it.
ASIDE = re.compile(r"\([^()]{0,300}\)")
#: A name that is ours whatever the registry: an npm scope, a Maven group, a
#: NuGet id prefix.
OURS = re.compile(
    r"(?<![\w@/.:-])(?:@controlvar/[A-Za-z0-9._-]{1,60}"
    r"|com\.controlvar(?:\.[A-Za-z0-9_]{1,40}){0,4}:[A-Za-z0-9._-]{1,60}"
    r"|Controlvar(?:\.[A-Za-z0-9]{1,40}){1,4})"
)
BACKTICKED = re.compile(r"`([^`\s]{1,100})`")
#: What the pages call each SDK, longest first: a name found is taken out of
#: the text before the shorter ones are looked for ("React Native" before
#: "React", "the OpenFeature provider for Python" before "Python").
NAMES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        r"OpenFeature (?i:provider) for Node(?:\.js)?"
        r"|(?:TypeScript|Node(?:\.js)?) (?:OpenFeature )?(?i:provider)",
        ("openfeature",),
    ),
    (
        r"OpenFeature (?i:provider) for Python|Python (?:OpenFeature )?(?i:provider)",
        ("openfeature-python",),
    ),
    (r"OpenFeature (?i:providers?)", ("openfeature", "openfeature-python")),
    (r"React Native", ("react-native",)),
    (r"Spring Boot", ("java",)),
    (r"JavaScript|TypeScript|Node(?:\.js)?", ("js",)),
    (r"React", ("react",)),
    (r"Python", ("python",)),
    (r"Java", ("java",)),
    (r"\.NET|C#", ("dotnet",)),
    (r"Go(?:lang)?", ("go",)),
    (r"[Ee]dge", ("edge",)),
    (r"Ruby", ("ruby",)),
    (r"PHP", ("php",)),
    (r"Elixir", ("elixir",)),
    (r"Flutter|Dart", ("flutter",)),
    (r"iOS|Swift", ("ios",)),
    (r"Android|Kotlin", ("android",)),
)
_NAMES = tuple(
    (re.compile(r"(?<![\w.])(?:" + pattern + r")(?![\w#])"), sdks)
    for pattern, sdks in NAMES
)
#: On an SDK's own page, a claim about "it", "the package", "this SDK" ...
PRONOUN = re.compile(
    r"^(?:(?:and|but|so|now|today),?\s+)*"
    r"(?:it|they|this|these|the|its)(?:\s+(?:sdks?|packages?|modules?|library|providers?|client))?"
    r"(?:\s+itself)?\s+(?:is|are)(?:\s+(?:also|now|already|both|published|available|released)){0,2}$",
    re.IGNORECASE,
)
#: The dashboard's and controlvar.com's sources, besides the Markdown pages.
#: The second is built from parts: a core file may not name a module in a
#: string (test_core_boundary.py), and an absent one reads no pages.
FRONTEND_ROOTS = ("frontend/src", "/".join(("modules", "frontend", "src")))
NOT_A_PAGE = re.compile(r"(?:^|/)(?:tests|__tests__|__mocks__)/|\.test\.tsx?$")


@dataclasses.dataclass(frozen=True)
class Claim:
    """One sentence's claim that SDKs are on a registry."""

    text: str  # what it is about, and the claim, as the page words it
    ecosystems: FrozenSet[str]
    sdks: FrozenSet[str]
    unresolved: Tuple[str, ...]  # names of ours that are no package there


def ecosystem_of(registry: str) -> str:
    plain = re.sub(r"\(.*$|[\[\]]", "", registry).strip()
    for pattern, ecosystem in REGISTRIES:
        if re.fullmatch(pattern, plain, re.IGNORECASE):
            return ecosystem
    raise AssertionError(registry)


def _registries(listed: str) -> FrozenSet[str]:
    return frozenset(
        ecosystem_of(m.group(0)) for m in re.finditer(_ONE, listed, re.IGNORECASE)
    )


@lru_cache(maxsize=None)
def _names() -> Dict[Tuple[str, str], str]:
    """(ecosystem, name as its registry compares it) -> SDK directory, for
    every SDK released to npm, PyPI, Maven Central or NuGet. A Maven package
    is named by its ``groupId:artifactId`` and by its artifactId alone."""
    table = {}
    for sdk, ecosystem in sorted(_check_sdk_version().ECOSYSTEMS.items()):
        if ecosystem in NAMED:
            for kind, name in package_identities(sdk):
                table[(kind, name)] = sdk
                if kind == "maven":
                    table[(kind, name.split(":", 1)[1])] = sdk
    return table


def blocks(text: str) -> List[str]:
    """The page's paragraphs, each on one line. A blank line ends one; a
    heading, a table row or a fence line is one of its own; a list item starts
    one. A blockquote's ``>`` is taken off."""
    found: List[str] = []
    current: List[str] = []
    for line in text.split("\n"):
        line = re.sub(r"^\s{0,40}(?:>\s?){1,10}", "", line).strip()
        alone = re.match(r"(?:#{1,6}\s|\||```|~~~)", line)
        if not line or alone or re.match(r"(?:[-*+]|\d{1,3}[.)])\s", line):
            if current:
                found.append(" ".join(current))
            current = []
        if line and alone:
            found.append(line)
        elif line:
            current.append(line)
    if current:
        found.append(" ".join(current))
    return found


def _subject(block: str, start: int, floor: int) -> str:
    """What the claim at *start* is about: the text before it, back to a
    boundary or *floor* (the previous claim's end), without its asides."""
    window = block[max(floor, start - 300) : start]
    for _ in range(4):
        window = ASIDE.sub(" ", window)
    window = re.sub(r"[\[\]*]", "", window)
    cuts = [m.end() for m in BOUNDARY.finditer(window)]
    return " ".join(window[cuts[-1] if cuts else 0 :].split())


def _a_question(block: str, end: int) -> bool:
    after = re.search(r"[.!?](?=\s|$)", block[end:])
    return after is not None and after.group(0) == "?"


def own_sdks(page: str) -> Tuple[str, ...]:
    """The SDK directories a page belongs to: sdk/<name>/... and docs/sdk/<name>.md."""
    parts = page.split("/")
    if parts[0] == "sdk" and len(parts) > 2 and (SDK_DIR / parts[1]).is_dir():
        return (parts[1],)
    if parts[:2] == ["docs", "sdk"] and len(parts) == 3 and page.endswith(".md"):
        own = parts[2][: -len(".md")]
        if (SDK_DIR / own).is_dir():
            return (own,) + EXTRA_SDKS.get(page, ())
    return ()


def named(
    subject: str,
    ecosystems: FrozenSet[str],
    page: str,
    table: Dict[Tuple[str, str], str],
) -> Tuple[Set[str], List[str]]:
    """The SDKs *subject* names, and the names of ours in it that are no
    package on the claimed registries."""
    sdks: Set[str] = set()
    unresolved: List[str] = []
    ours = {name for _, name in table}
    tokens = [m.group(0) for m in OURS.finditer(subject)] + [
        m.group(1) for m in BACKTICKED.finditer(subject)
    ]
    rest = OURS.sub(" ", BACKTICKED.sub(" ", subject))
    for token in dict.fromkeys(tokens):
        keys = [registry_key(e, token) for e in sorted(ecosystems & set(NAMED))]
        found = [table[k] for k in keys if k in table]
        if found:
            sdks.update(found)
        elif OURS.fullmatch(token) or registry_key("pypi", token)[1] in ours:
            unresolved.append(token)
    for pattern, which in _NAMES:
        if pattern.search(rest):
            sdks.update(which)
            rest = pattern.sub(" ", rest)
    if not sdks and not unresolved and PRONOUN.match(subject):
        sdks.update(own_sdks(page))
    return sdks, unresolved


def registry_claims(page: str, text: str) -> List[Claim]:
    """Every claim on the page that SDKs are on a registry."""
    table = _names()
    found: List[Claim] = []
    for block in blocks(text):
        floor = 0
        claimed: Set[str] = set()
        for match in CLAIM.finditer(block):
            subject = _subject(block, match.start(), floor)
            floor = match.end()
            if (
                NEGATED.search(subject)
                or CONDITIONAL.search(subject)
                or _a_question(block, match.end())
            ):
                continue
            ecosystems = _registries(match.group("registries"))
            claimed |= ecosystems
            sdks, unresolved = named(subject, ecosystems, page, table)
            found.append(
                Claim(
                    f"{subject} {match.group(0)}",
                    ecosystems,
                    frozenset(sdks),
                    tuple(unresolved),
                )
            )
        if not claimed:
            continue
        plain = block
        for _ in range(4):
            plain = ASIDE.sub(" ", plain)
        for match in SO_ARE.finditer(plain):
            rest = " ".join(re.sub(r"[\[\]*]", "", match.group("rest")).split())
            sdks, unresolved = named(rest, frozenset(claimed), page, table)
            found.append(
                Claim(
                    match.group(0),
                    frozenset(claimed),
                    frozenset(sdks),
                    tuple(unresolved),
                )
            )
    return found


def problems(page: str, text: str) -> List[str]:
    """Each SDK a claim on the page names that is not on that registry."""
    ecosystems = _check_sdk_version().ECOSYSTEMS
    found = []
    for claim in registry_claims(page, text):
        where = f"{page}: {claim.text!r}"
        for sdk in sorted(claim.sdks):
            if sdk not in ON_REGISTRY:
                found.append(
                    f"{where} puts sdk/{sdk} on {'/'.join(sorted(claim.ecosystems))},"
                    " but sdk/" + sdk + " is not in ON_REGISTRY"
                    " (backend/tests/unit/docs/test_sdk_doc_versions.py)"
                )
            elif ecosystems.get(sdk) not in claim.ecosystems:
                found.append(
                    f"{where} puts sdk/{sdk} on {'/'.join(sorted(claim.ecosystems))};"
                    f" it is released to {ecosystems.get(sdk)}"
                )
        for token in claim.unresolved:
            found.append(
                f"{where} puts {token} on {'/'.join(sorted(claim.ecosystems))},"
                " where no package of ours has that name"
            )
    return found


@lru_cache(maxsize=1)
def _walked() -> Tuple[str, ...]:
    """scripts/doc_examples.py:walk() over this tree, loaded by path under a
    module name of its own (its dataclasses look their module up)."""
    spec = importlib.util.spec_from_file_location(
        "doc_examples_registry_availability_walk",
        REPO_ROOT / "scripts" / "doc_examples.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return tuple(sorted(module.walk(REPO_ROOT)))


def pages() -> List[str]:
    """The Markdown pages (scripts/doc_examples.py's universe) but the
    changelogs, and the dashboard's and controlvar.com's TypeScript sources.
    A changelog is release history: release-please writes each entry from a
    merged title and nothing rewrites it, so an entry speaks of its release
    (0.36.0's rename note, "published as ... `controlvar-openfeature` on
    PyPI", names the provider's PyPI name before the provider is published).
    Every page that says what is on a registry now is read."""
    found = {page for page in _walked() if page.rsplit("/", 1)[-1] != "CHANGELOG.md"}
    for root in FRONTEND_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.ts*")):
            page = path.relative_to(REPO_ROOT).as_posix()
            if path.suffix in (".ts", ".tsx") and not NOT_A_PAGE.search(page):
                found.add(page)
    return sorted(found)


def read(page: str) -> str:
    return (REPO_ROOT / page).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# ON_REGISTRY
# ---------------------------------------------------------------------------


def test_every_strict_package_is_on_its_registry():
    """PUBLISHED (STRICT, and rc-evidence's PACKAGES) stays a subset: a
    package rc-evidence verifies on its registry is on it."""
    assert set(PUBLISHED) <= set(ON_REGISTRY)
    assert len(set(ON_REGISTRY)) == len(ON_REGISTRY)


@pytest.mark.parametrize("sdk", ON_REGISTRY)
def test_every_sdk_on_the_registry_has_a_registry_name(sdk):
    """Its package name can be read (npm, PyPI, Maven or NuGet), so a page
    may install it from there and say it is there."""
    assert package_identities(sdk)


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def test_every_registry_sentence_names_only_sdks_on_that_registry():
    """No page says an SDK is on a registry unless ON_REGISTRY holds it and
    that is the registry it is released to."""
    found = [line for page in pages() for line in problems(page, read(page))]
    assert not found, "\n".join(found)


#: The SDKs the sitting put on npm, PyPI and NuGet, with the first two: what
#: D's cross-page sentences name.
ON_NPM_PYPI_NUGET = (
    "js",
    "python",
    "react",
    "react-native",
    "openfeature",
    "openfeature-python",
    "dotnet",
)
#: Pages whose registry sentences today name these SDKs, which the sweep must
#: find (a pattern that stopped matching would pass every page).
FOUND_TODAY = {
    ("docs/sdk/javascript.md", "js"),
    ("docs/sdk/python.md", "python"),
    ("sdk/js/README.md", "js"),
    ("sdk/python/README.md", "python"),
    ("docs/sdk-guide.md", "js"),
    ("docs/sdk-guide.md", "python"),
    ("frontend/src/components/homepage/HowItWorks.tsx", "js"),
    ("frontend/src/components/homepage/HowItWorks.tsx", "python"),
    # Plan P's D: each SDK page, and the pages that list them.
    ("docs/sdk/react.md", "react"),
    ("docs/sdk/react-native.md", "react-native"),
    ("docs/sdk/openfeature.md", "openfeature"),
    ("docs/sdk/openfeature.md", "openfeature-python"),
    ("docs/sdk/dotnet.md", "dotnet"),
    *(
        (page, sdk)
        for page in (
            "docs/getting-started/faq.md",
            "docs/getting-started/limitations.md",
            "frontend/src/pages/docs/index.tsx",
            "frontend/src/components/homepage/SdkChips.tsx",
        )
        for sdk in ON_NPM_PYPI_NUGET
    ),
    *(
        ("demo/DEMO_GUIDE.md", sdk)
        for sdk in ("js", "python", "react", "react-native", "dotnet")
    ),
}


def test_the_sweep_finds_the_sentences_it_exists_for():
    found = {
        (page, sdk)
        for page in sorted({page for page, _ in FOUND_TODAY})
        for claim in registry_claims(page, read(page))
        for sdk in claim.sdks
    }
    assert FOUND_TODAY <= found, sorted(FOUND_TODAY - found)


#: The limitations page's registry row, as it read before Plan P's D and as
#: the plan words it.
LIMITATIONS_BEFORE_D = (
    "| Every SDK on a package registry | The JavaScript SDK is on npm and the"
    " Python SDK is on PyPI. The Go SDK installs by module path. |"
)
LIMITATIONS_D = (
    "| Every SDK on a package registry | The JavaScript, React and React Native"
    " SDKs and the OpenFeature provider for Node are on npm; the Python SDK and"
    " the OpenFeature provider for Python are on PyPI; the Java SDK and its"
    " Spring Boot starter are on Maven Central; the .NET SDK is on NuGet. |"
)
#: The SDKs Plan P publishes, which D adds to ON_REGISTRY.
PLAN_P = (
    "react",
    "react-native",
    "openfeature",
    "openfeature-python",
    "java",
    "dotnet",
)

#: (page, sentence, the SDKs a claim in it names). Each form the sweep reads.
READ = (
    ("docs/x.md", LIMITATIONS_BEFORE_D, {"js", "python"}),
    (
        "docs/x.md",
        "The JavaScript SDK is on npm (`npm install @controlvar/js-sdk`, 0.2.0, beta)"
        " and the\nPython SDK is on PyPI (`pip install controlvar`, 0.2.0, beta).",
        {"js", "python"},
    ),
    (
        "docs/x.md",
        "> The Python and JavaScript/TypeScript SDKs are on PyPI and npm, and the Go"
        " SDK installs with `go get`.",
        {"js", "python"},
    ),
    (
        "docs/x.md",
        "- Full REST API + SDKs: Python and JavaScript on PyPI and npm, Go with"
        " `go get`, Java and React built from `sdk/`",
        {"js", "python"},
    ),
    (
        "docs/x.md",
        "The `controlvar` package is on [PyPI](https://pypi.org/project/controlvar/)"
        " (0.2.0, beta):",
        {"python"},
    ),
    (
        "docs/x.md",
        "The `@controlvar/js-sdk` package is on"
        " [npm](https://www.npmjs.com/package/@controlvar/js-sdk). 0.2.0 is a beta.",
        {"js"},
    ),
    (
        "frontend/x.tsx",
        '<p className="mt-2">\n  Sticky experiment assignment. The JavaScript and'
        " Python SDKs\n  are on npm and PyPI as betas.\n</p>",
        {"js", "python"},
    ),
    (
        "docs/x.md",
        "Install it from the same clone: the `controlvar` 0.2.0 on PyPI reads no flag value.",
        {"python"},
    ),
    (
        "frontend/x.tsx",
        "The JavaScript SDK is on npm (npm install @controlvar/js-sdk) and the Python"
        " SDK is on PyPI (pip install\n  controlvar), both as betas; the others are not"
        " on npm or the other package registries yet.",
        {"js", "python"},
    ),
    # Plan P's D, as its plan words it.
    ("docs/x.md", LIMITATIONS_D, {"js", "python", *PLAN_P}),
    (
        "docs/x.md",
        "The JavaScript SDK is on npm (`npm install @controlvar/js-sdk`, 0.3.0, beta).\n"
        "So are the React SDK (`npm install @controlvar/react-sdk`, 0.1.0, beta), the"
        " React Native SDK (`npm install @controlvar/react-native-sdk`, 0.1.0, beta)"
        " and the two OpenFeature providers (`npm install"
        " @controlvar/openfeature-provider`, 0.1.0, beta; `pip install"
        " controlvar-openfeature`, 0.1.0, beta). The Java SDK is on Maven Central"
        " and the .NET SDK on NuGet.",
        {
            "js",
            "react",
            "react-native",
            "openfeature",
            "openfeature-python",
            "java",
            "dotnet",
        },
    ),
    (
        "docs/x.md",
        "- Full REST API + SDKs: JavaScript, React, React Native and Python on npm"
        " and PyPI, Java on Maven Central, .NET on NuGet, Go with `go get`",
        {"js", "react", "react-native", "python", "java", "dotnet"},
    ),
    (
        "docs/x.md",
        "The `Controlvar.SDK` package is on [NuGet](https://www.nuget.org/packages/"
        "Controlvar.SDK).",
        {"dotnet"},
    ),
    (
        "docs/x.md",
        "`com.controlvar:controlvar-sdk` and `com.controlvar:controlvar-spring-boot-starter`"
        " are on [Maven Central](https://central.sonatype.com/artifact/com.controlvar/"
        "controlvar-sdk).",
        {"java"},
    ),
    ("docs/sdk/react.md", "The package is on npm. 0.1.0 is a beta.", {"react"}),
    ("sdk/dotnet/README.md", "It is also on NuGet.", {"dotnet"}),
    (
        "docs/sdk/openfeature.md",
        "Both providers are released. The providers are on npm and PyPI.",
        {"openfeature", "openfeature-python"},
    ),
    ("docs/x.md", "Install the React SDK from npm.", {"react"}),
)


@pytest.mark.parametrize("page, sentence, sdks", READ)
def test_each_form_is_read(page, sentence, sdks):
    claims = registry_claims(page, sentence)
    assert claims, sentence
    assert set().union(*(c.sdks for c in claims)) == sdks, claims
    assert not any(c.unresolved for c in claims), claims


#: (page, sentence): no claim, or none that names an SDK.
NOT_READ = (
    (
        "docs/x.md",
        "**Not yet published.** `@controlvar/edge-sdk` is not on npm, so build it.",
    ),
    ("docs/x.md", "The module is not\non Maven Central, so build it from source."),
    ("docs/x.md", "The React SDK isn't on npm yet."),
    (
        "docs/x.md",
        "**Not yet published.** `Controlvar.SDK` has no release on NuGet, so build it.",
    ),
    (
        "docs/x.md",
        "Both as betas; the others are not on npm or the other package registries yet.",
    ),
    ("docs/x.md", "Neither the edge SDK nor the Ruby SDK is on npm or RubyGems."),
    ("docs/x.md", "Once the React SDK is on npm, install it from there."),
    ("docs/x.md", "The React SDK will be on npm at 0.1.0."),
    ("docs/x.md", "### Is the React SDK on npm?"),
    (
        "docs/sdk/openfeature.md",
        "Then, in your app, install the two directories (`@openfeature/server-sdk`"
        " itself is on npm):",
    ),
    (
        "docs/sdk/openfeature.md",
        "Installed as a package (from npm, or an `npm pack` tarball of"
        " `sdk/openfeature`), the provider resolves your app's copy.",
    ),
    (
        "sdk/dotnet/README.md",
        "**Dependencies**: none beyond `System.Text.Json` (part of .NET on `net8.0`;"
        " System.Text.Json 8.0.6 from NuGet on `netstandard2.1`)",
    ),
    ("docs/x.md", "The live contract runs against the releases on npm and PyPI."),
    ("docs/x.md", "The package is on npm."),
)


@pytest.mark.parametrize("page, sentence", NOT_READ)
def test_a_sentence_that_puts_no_sdk_on_a_registry_is_not_read_as_one(page, sentence):
    assert [c for c in registry_claims(page, sentence) if c.sdks or c.unresolved] == []


#: (sentence, what the sweep says): each claim it refuses with ON_REGISTRY as
#: it is, after the SDK sitting published Plan P's packages.
REFUSED = (
    ("The JavaScript and Edge SDKs are on npm.", "sdk/edge is not in ON_REGISTRY"),
    ("The Edge SDK is on npm.", "sdk/edge is not in ON_REGISTRY"),
    (
        "The `@controlvar/edge-sdk` package is on [npm](https://npmjs.com).",
        "sdk/edge is not in ON_REGISTRY",
    ),
    ("The Java SDK is on Maven Central.", "sdk/java is not in ON_REGISTRY"),
    (
        "`@controlvar/edge-sdk` is published to the npm registry.",
        "sdk/edge is not in ON_REGISTRY",
    ),
    (
        "The iOS SDK is available in the CocoaPods trunk.",
        "sdk/ios is not in ON_REGISTRY",
    ),
    ("The Flutter SDK is on pub.dev.", "sdk/flutter is not in ON_REGISTRY"),
    ("The .NET SDK is on npm.", "sdk/dotnet on npm; it is released to nuget"),
    (
        "`pip install controlvar-openfeature` works: `controlvar-openfeature` is on npm.",
        "puts controlvar-openfeature on npm, where no package of ours has that name",
    ),
    ("The Ruby SDK is on RubyGems.", "sdk/ruby is not in ON_REGISTRY"),
    ("The Go SDK is on npm.", "sdk/go is not in ON_REGISTRY"),
    ("The Python SDK is on npm.", "sdk/python on npm; it is released to pypi"),
    (
        "`controlvar` is on RubyGems.",
        "puts controlvar on rubygems, where no package of ours has that name",
    ),
    (
        "`@controlvar/js-sdk` is on PyPI.",
        "puts @controlvar/js-sdk on pypi, where no package of ours has that name",
    ),
    (
        "`com.controlvar:controlvar-android` is on Maven Central.",
        "puts com.controlvar:controlvar-android on maven, where no package of ours",
    ),
)


@pytest.mark.parametrize("sentence, expected", REFUSED)
def test_a_claim_about_an_sdk_not_on_that_registry_is_refused(sentence, expected):
    found = problems("docs/x.md", sentence)
    assert len(found) == 1 and expected in found[0], found


@pytest.mark.parametrize(
    "row, on_registry",
    [(LIMITATIONS_BEFORE_D, ()), (LIMITATIONS_D, PLAN_P)],
    ids=["before D", "D"],
)
def test_an_sdk_added_to_the_limitations_npm_list_is_refused(
    row, on_registry, monkeypatch
):
    """Plan P's C10 plant: a name added to the limitations page's npm
    sentence, as the row read before D and as the plan words it (with D's
    SDKs in ON_REGISTRY)."""
    monkeypatch.setitem(globals(), "ON_REGISTRY", (*ON_REGISTRY, *on_registry))
    page = "docs/getting-started/limitations.md"
    planted = row.replace("The JavaScript", "The Edge, JavaScript", 1)
    assert planted != row
    assert problems(page, row) == []
    assert [p for p in problems(page, planted) if "sdk/edge is not in ON_REGISTRY" in p]


def test_an_sdk_added_to_the_limitations_pages_own_row_is_refused():
    """The C10 plant on the row as the page reads now, with ON_REGISTRY as
    it is."""
    page = "docs/getting-started/limitations.md"
    rows = [
        line
        for line in read(page).splitlines()
        if line.startswith("| Every SDK on a package registry |")
    ]
    assert len(rows) == 1, rows
    planted = rows[0].replace("The JavaScript", "The Edge, JavaScript", 1)
    assert planted != rows[0]
    assert problems(page, rows[0]) == []
    assert [p for p in problems(page, planted) if "sdk/edge is not in ON_REGISTRY" in p]


@pytest.mark.parametrize(
    "page", ["docs/getting-started/limitations.md", "docs/getting-started/faq.md"]
)
def test_a_registry_sentence_planted_on_a_real_page_is_refused(page):
    """The page itself is clean, and a sentence added to it is read."""
    assert problems(page, read(page)) == []
    planted = read(page) + "\n\nThe Edge SDK is on npm.\n"
    assert [p for p in problems(page, planted) if "sdk/edge " in p]


@pytest.mark.parametrize("sdk", (*PLAN_P, "mcp"))
def test_once_an_sdk_is_on_the_registry_a_page_may_say_so(sdk, monkeypatch):
    """What D does: add the SDK to ON_REGISTRY, and the sentence passes; out
    of it, the sentence is refused."""
    registry = _check_sdk_version().ECOSYSTEMS[sdk]
    name = sorted(name for _, name in package_identities(sdk))[0]
    words = {"npm": "npm", "pypi": "PyPI", "maven": "Maven Central", "nuget": "NuGet"}
    sentence = f"The `{name}` package is on {words[registry]}."
    without = tuple(other for other in ON_REGISTRY if other != sdk)
    monkeypatch.setitem(globals(), "ON_REGISTRY", without)
    assert problems("docs/x.md", sentence)
    monkeypatch.setitem(globals(), "ON_REGISTRY", (*without, sdk))
    assert problems("docs/x.md", sentence) == []


def test_the_sweep_reads_the_pages_that_say_what_is_on_a_registry():
    read_pages = pages()
    for page in (
        "README.md",
        "docs/getting-started/limitations.md",
        "docs/getting-started/faq.md",
        "demo/DEMO_GUIDE.md",
        "sdk/react/README.md",
        "frontend/src/components/homepage/SdkChips.tsx",
        "frontend/src/pages/docs/index.tsx",
    ):
        assert page in read_pages, page
        assert (REPO_ROOT / page).is_file(), page
    assert not [p for p in read_pages if "node_modules" in p.split("/")]
    assert not [p for p in read_pages if p.endswith("CHANGELOG.md")]
    frontend = [p for p in read_pages if p.startswith(FRONTEND_ROOTS)]
    assert frontend and not [p for p in frontend if NOT_A_PAGE.search(p)]


# ---------------------------------------------------------------------------
# The homepage's beta chips
# ---------------------------------------------------------------------------

CHIPS = "frontend/src/components/homepage/SdkChips.tsx"
#: The SDK directories the homepage names (``SDKS`` in content/sdks.ts).
HOMEPAGE_SDKS = "frontend/src/content/sdks.ts"
#: An SDK whose release is its tag, not a registry upload (``tag-only`` in
#: ECOSYSTEMS): Go, whose v0.1.0 the Go proxy serves since the sitting.
RELEASED_BY_TAG = ("go",)


def _quoted_keys(source: str, start: str) -> Set[str]:
    """The quoted names in the literal that follows *start* in *source*."""
    at = source.index(start) + len(start)
    body = source[at : source.index("}" if start.endswith("{") else "]", at)]
    return set(re.findall(r"""^\s{0,8}['"]?([a-z][a-z-]{0,40})['"]?[:,]""", body, re.M))


def test_the_homepages_beta_chips_are_the_sdks_with_a_release():
    """SdkChips.tsx marks an SDK beta, and its sentence says those have a
    release: each one the homepage names that is in ON_REGISTRY, and Go."""
    beta = _quoted_keys(read(CHIPS), "const BETA: ReadonlySet<string> = new Set([")
    named = _quoted_keys(
        read(HOMEPAGE_SDKS), "export const SDKS: Readonly<Record<string, string>> = {"
    )
    assert {"js", "react-native", "go"} <= named, named
    assert all(
        _check_sdk_version().ECOSYSTEMS[s] == "tag-only" for s in RELEASED_BY_TAG
    )
    assert beta == (set(ON_REGISTRY) & named) | set(RELEASED_BY_TAG)
