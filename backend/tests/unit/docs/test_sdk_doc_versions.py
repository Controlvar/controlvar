"""The SDK version a page quotes is the version in that SDK's manifest.

docs/sdk/local-evaluation.md and docs/sdk/javascript.md said the JavaScript
and Python SDKs were "1.1" while both manifests said 0.1.0 (#784). This reads
every place a page quotes a package with a version and compares it with the
manifest:

* `` `@getexperimently/js-sdk` (v0.1.0) `` and `` `experimently` 0.1.0 ``;
* a tarball name such as ``getexperimently-js-sdk-0.1.0.tgz``;
* an install command pinned to a version, ``npm install
  @getexperimently/js-sdk@0.1.0`` or ``pip install experimently==0.1.0``;
* an install command followed by the version, `` (`pip install experimently`,
  0.1.0, beta) ``;
* the sentence naming the registry, `` The `experimently` package is on
  [PyPI](...). 0.1.0 is a beta release `` or `` ... on [npm](...) (0.1.0,
  beta) ``.

The pages read are every SDK page, the SDK guide, the FAQ, the two SDK docs
journeys and the README each published package carries to its registry
(``PUBLISHED``): PyPI and npm show that README as the package's page, and a
release that bumps the manifest but not the README publishes the old version's
install line for good. On the pages about the two published packages, every
version that shares their manifest's major and minor must sit inside one of
the forms above, so a quote this file does not recognise fails instead of
going stale unseen.

Every README a package ships (``readme`` in pyproject.toml; npm always ships
README.md) links only to absolute URLs: a registry renders the README away
from this repository, where ``../../docs/x.md`` leads nowhere. A published
package's README does not say it is unpublished.

A quoted version may be shorter than the manifest's ("v1.1" for 1.1.0), but
every part it gives must match. The plain name ``experimently`` is shared by
the Python, Ruby, Elixir and Flutter packages, so in the first form it is
checked only on the pages about the Python SDK; the install and registry
forms name the Python package whatever the page.

Reads files only: no git and no `modules` import, so it runs the same in
`scripts/core_build.sh`'s copy.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
SDK_DIR = REPO_ROOT / "sdk"
DOCS = REPO_ROOT / "docs"
JOURNEYS = REPO_ROOT / "tests" / "acceptance" / "docs" / "journeys"

#: npm package -> its manifest directory under sdk/.
NPM = ("js", "react", "react-native", "openfeature", "edge")
#: Python distribution -> its manifest directory under sdk/.
PYPI = ("python", "openfeature-python")
#: The SDK directories whose package is on its registry. PyPI and npm show the
#: README of each as the package's page.
PUBLISHED = ("python", "js")
#: Pages on which the bare name `experimently` means the Python SDK.
PYTHON_PAGES = {
    "docs/sdk/python.md",
    "docs/sdk/local-evaluation.md",
    "sdk/python/README.md",
}

VERSION = r"v?(\d+(?:\.\d+){1,2})"
PACKAGE = r"@getexperimently/[A-Za-z0-9_-]+|experimently(?:-openfeature)?"
#: (form, pattern): group 1 the package, group 2 the version. Whitespace is
#: folded before matching, so a line break may fall anywhere.
FORMS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("quoted", re.compile(r"`(" + PACKAGE + r")` \(?" + VERSION)),
    ("tarball", re.compile(r"getexperimently-([A-Za-z0-9-]+?)-(\d+\.\d+\.\d+)\.tgz")),
    ("npm pin", re.compile(r"(@getexperimently/[A-Za-z0-9_-]+)@" + VERSION)),
    ("pip pin", re.compile(r"(?<![\w-])(experimently(?:-openfeature)?)==" + VERSION)),
    (
        "install, then version",
        re.compile(r"`(?:npm|pip) install (" + PACKAGE + r")`, " + VERSION),
    ),
    (
        "registry",
        re.compile(
            r"`("
            + PACKAGE
            + r")` package is on \[(?:npm|PyPI)\]\([^()\s]+\)(?:\.)? \(?"
            + VERSION
        ),
    ),
)


def manifest_versions() -> Dict[str, str]:
    versions: Dict[str, str] = {}
    for name in NPM:
        data = json.loads((SDK_DIR / name / "package.json").read_text(encoding="utf-8"))
        versions[data["name"]] = data["version"]
    for name in PYPI:
        data = tomllib.loads(
            (SDK_DIR / name / "pyproject.toml").read_text(encoding="utf-8")
        )
        versions[data["project"]["name"]] = data["project"]["version"]
    return versions


def published_names() -> Dict[str, str]:
    """Each published SDK's package name -> its directory under sdk/."""
    names = {}
    for directory in PUBLISHED:
        if (SDK_DIR / directory / "package.json").is_file():
            name = json.loads(
                (SDK_DIR / directory / "package.json").read_text(encoding="utf-8")
            )["name"]
        else:
            name = tomllib.loads(
                (SDK_DIR / directory / "pyproject.toml").read_text(encoding="utf-8")
            )["project"]["name"]
        names[name] = directory
    return names


def fold(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def quotes(rel: str, text: str) -> List[Tuple[str, str, Tuple[int, int]]]:
    """Every (package, quoted version, span) on the page ``rel``, whitespace folded."""
    text = fold(text)
    found: List[Tuple[str, str, Tuple[int, int]]] = []
    for form, pattern in FORMS:
        for match in pattern.finditer(text):
            package = match.group(1)
            if form == "tarball":
                package = f"@getexperimently/{package}"
            if (
                form == "quoted"
                and package == "experimently"
                and rel not in PYTHON_PAGES
            ):
                continue
            found.append((package, match.group(2), match.span()))
    return found


def mismatches(rel: str, text: str, versions: Mapping[str, str]) -> List[str]:
    wrong = []
    for package, quoted, _ in quotes(rel, text):
        actual = versions.get(package)
        if actual is None:
            wrong.append(
                f"{package} {quoted}: no manifest under sdk/ declares {package}"
            )
        elif actual.split(".")[: len(quoted.split("."))] != quoted.split("."):
            wrong.append(f"{package} is quoted as {quoted}; the manifest says {actual}")
    return wrong


def unrecognised(rel: str, text: str, versions: Mapping[str, str]) -> List[str]:
    """Versions sharing a published package's major and minor that no form matched."""
    lines = {".".join(versions[name].split(".")[:2]) for name in published_names()}
    folded = fold(text)
    spans = [span for _, _, span in quotes(rel, text)]
    loose = []
    for line in sorted(lines):
        pattern = re.compile(r"(?<![\d.])" + re.escape(line) + r"\.\d+(?!\d)")
        for match in pattern.finditer(folded):
            if not any(
                start <= match.start() and match.end() <= end for start, end in spans
            ):
                context = folded[max(0, match.start() - 50) : match.end() + 20]
                loose.append(f"{match.group(0)} in {context!r}")
    return loose


def rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


#: Every page whose quotes are held to the manifests.
PAGES = (
    sorted((DOCS / "sdk").glob("*.md"))
    + [DOCS / "sdk-guide.md", DOCS / "getting-started" / "faq.md"]
    + [SDK_DIR / directory / "README.md" for directory in PUBLISHED]
    + sorted(JOURNEYS.glob("sdk-*.yaml"))
)
#: The pages about the two published packages: every version of theirs on
#: these is in a recognised form.
ABOUT_PUBLISHED = [
    DOCS / "sdk" / "python.md",
    DOCS / "sdk" / "javascript.md",
    DOCS / "sdk" / "local-evaluation.md",
    DOCS / "sdk-guide.md",
    DOCS / "getting-started" / "faq.md",
    *(SDK_DIR / directory / "README.md" for directory in PUBLISHED),
    JOURNEYS / "sdk-python.yaml",
    JOURNEYS / "sdk-javascript.yaml",
]


def test_the_scan_finds_the_quotes_it_exists_for():
    # A pattern that matched nothing would pass every page below.
    found = {
        (rel(p), q[0]) for p in PAGES for q in quotes(rel(p), p.read_text("utf-8"))
    }
    assert {
        ("docs/sdk/javascript.md", "@getexperimently/js-sdk"),
        ("docs/sdk/react.md", "@getexperimently/react-sdk"),
        ("docs/sdk/python.md", "experimently"),
        ("docs/sdk/local-evaluation.md", "@getexperimently/js-sdk"),
        ("docs/sdk/local-evaluation.md", "experimently"),
        ("docs/getting-started/faq.md", "experimently"),
        ("docs/getting-started/faq.md", "@getexperimently/js-sdk"),
        ("sdk/python/README.md", "experimently"),
        ("sdk/js/README.md", "@getexperimently/js-sdk"),
        ("tests/acceptance/docs/journeys/sdk-python.yaml", "experimently"),
        (
            "tests/acceptance/docs/journeys/sdk-javascript.yaml",
            "@getexperimently/js-sdk",
        ),
    } <= found
    forms = {
        form
        for form, pattern in FORMS
        for page in ABOUT_PUBLISHED
        if pattern.search(fold(page.read_text("utf-8")))
    }
    assert forms == {form for form, _ in FORMS}, "a form matches nothing it exists for"


@pytest.mark.parametrize("page", PAGES, ids=rel)
def test_quoted_sdk_versions_match_the_manifests(page):
    wrong = mismatches(rel(page), page.read_text(encoding="utf-8"), manifest_versions())
    assert not wrong, f"{rel(page)}:\n" + "\n".join(wrong)


@pytest.mark.parametrize("page", ABOUT_PUBLISHED, ids=rel)
def test_every_quote_of_a_published_package_is_one_this_file_checks(page):
    loose = unrecognised(
        rel(page), page.read_text(encoding="utf-8"), manifest_versions()
    )
    assert not loose, (
        f"{rel(page)} quotes a version in a form test_sdk_doc_versions.py does"
        " not read; write it as one of its FORMS, or add the form:\n" + "\n".join(loose)
    )


def test_a_page_left_behind_at_a_version_bump_is_refused():
    """The bump this guards: the manifests move to the next patch release and
    one page keeps the old version, in each form."""
    versions = manifest_versions()
    bumped = dict(versions)
    for name in published_names():
        major, minor, patch = versions[name].split(".")
        bumped[name] = f"{major}.{minor}.{int(patch) + 1}"
    for page in (SDK_DIR / "python" / "README.md", SDK_DIR / "js" / "README.md"):
        wrong = mismatches(rel(page), page.read_text(encoding="utf-8"), bumped)
        assert wrong, f"{rel(page)} at the old version passed a bumped manifest"
    js = versions["@getexperimently/js-sdk"]
    py = versions["experimently"]
    for sentence, package in (
        (f"npm install @getexperimently/js-sdk@{js}", "@getexperimently/js-sdk"),
        (f"pip install experimently=={py}", "experimently"),
        (
            f"(`npm install @getexperimently/js-sdk`, {js}, beta)",
            "@getexperimently/js-sdk",
        ),
        (f"(`pip install experimently`, {py}, beta)", "experimently"),
        (
            "The `experimently` package is on [PyPI](https://pypi.org/project/experimently/)."
            f" {py} is a beta release.",
            "experimently",
        ),
        (
            "The `@getexperimently/js-sdk` package is on [npm](https://www.npmjs.com/x)"
            f" ({js}, beta):",
            "@getexperimently/js-sdk",
        ),
    ):
        assert mismatches("docs/x.md", sentence, bumped) == [
            f"{package} is quoted as {versions[package]}; the manifest says {bumped[package]}"
        ], sentence
    unknown_form = f"Install {py} of the Python SDK."
    assert unrecognised("sdk/python/README.md", unknown_form, versions) == [
        f"{py} in {unknown_form!r}"
    ]


# ---------------------------------------------------------------------------
# The READMEs packages carry to their registries
# ---------------------------------------------------------------------------
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,}).*?^ {0,3}\1[`~]*[ \t]*$", re.S | re.M)
LINK_TARGETS = (
    re.compile(r"\]\(\s*<?([^()\s<>]+)"),  # [text](target) and ![alt](src)
    re.compile(r"^ {0,3}\[[^\]\n]+\]:\s*<?([^\s<>]+)", re.M),  # [ref]: target
    re.compile(r"""\b(?:href|src)\s*=\s*["']([^"']+)["']"""),  # raw HTML
)
ABSOLUTE = re.compile(r"(?:https?|mailto):", re.I)


def packaged_readmes() -> List[Path]:
    """Every README a package under sdk/ ships: npm always ships README.md, and
    a pyproject.toml names its own."""
    found = []
    for manifest in sorted(SDK_DIR.glob("*/package.json")):
        if (manifest.parent / "README.md").is_file():
            found.append(manifest.parent / "README.md")
    for manifest in sorted(SDK_DIR.glob("*/pyproject.toml")):
        readme = tomllib.loads(manifest.read_text(encoding="utf-8"))["project"].get(
            "readme"
        )
        if isinstance(readme, str):
            found.append(manifest.parent / readme)
    return found


def relative_links(text: str) -> List[str]:
    text = FENCE.sub("", text)
    return [
        target
        for pattern in LINK_TARGETS
        for target in pattern.findall(text)
        if not ABSOLUTE.match(target)
    ]


def test_every_packaged_readme_links_only_to_absolute_urls():
    readmes = packaged_readmes()
    assert {rel(SDK_DIR / d / "README.md") for d in PUBLISHED} <= {
        rel(r) for r in readmes
    }
    wrong = {rel(r): relative_links(r.read_text("utf-8")) for r in readmes}
    wrong = {page: links for page, links in wrong.items() if links}
    assert not wrong, (
        "a registry shows these READMEs away from this repository, where a"
        f" relative link leads nowhere; make each absolute: {wrong}"
    )


def test_a_published_packages_readme_does_not_say_it_is_unpublished():
    for directory in PUBLISHED:
        readme = SDK_DIR / directory / "README.md"
        assert "not yet published" not in fold(readme.read_text("utf-8")).lower(), rel(
            readme
        )


@pytest.mark.parametrize(
    "text, expected",
    [
        ("See [the guide](../../docs/sdk/go.md).", ["../../docs/sdk/go.md"]),
        ("See [Development](#development).", ["#development"]),
        ("![logo](img/logo.png)", ["img/logo.png"]),
        ("[guide]: docs/guide.md", ["docs/guide.md"]),
        ('<a href="../python">SDK</a>', ["../python"]),
        ("See [the guide](https://example.com/docs/x.md).", []),
        ("```md\n[x](../in-a-fence.md)\n```", []),
    ],
)
def test_a_relative_link_is_found_in_each_form(text, expected):
    assert relative_links(text) == expected
