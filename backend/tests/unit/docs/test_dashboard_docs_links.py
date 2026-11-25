"""Every `docsUrl('...')` link in the dashboard addresses a page under docs/.

A Python twin of `frontend/src/tests/services/docs-links.test.ts`. The Jest
test is the check on a frontend pull request, but a pull request that changes
docs/ alone runs no Node: it runs the Python "Docs content tests" step, which
collects this directory. Without this twin a docs-only change that deletes or
renames a linked page merged green and turned main's next Frontend run red.

The rule is the Jest rule, copied, not reinterpreted: the same scan root and
skips, the same regex (single quotes, optional whitespace, the empty string
allowed, no closing paren required, since `docsUrl` takes an optional anchor),
and the same target resolution (`''` and `README` are docs/README.md).
`TestTheTwinStillMirrorsJest` reads the Jest file as text and fails when its
rule moves without this one. `test_every_call_is_one_the_rule_can_see` fails on
a `docsUrl(` call that is not one literal page followed by `,` or `)` (double
quotes, a variable, a template literal, a concatenation), which the scan would
otherwise skip silently; the Jest file has the same rule (#1459, PE 6).

`test_the_specifics_section_links_pages_the_nav_lists` is the docs side of a
second Jest check, in `frontend/src/tests/pages/home-page.test.tsx`: the pages
the homepage's "Specific to Controlvar" links are in the mkdocs nav (#2040).

Needs no Node and no node_modules: it reads files only.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Tuple

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
DOCS_ROOT = REPO_ROOT / "docs"
SRC_ROOT = REPO_ROOT / "frontend" / "src"
JEST_TWIN = SRC_ROOT / "tests" / "services" / "docs-links.test.ts"

#: The Jest regex, `/docsUrl\(\s*'([^']*)'/g`, without its delimiters.
PATTERN = r"docsUrl\(\s*'([^']*)'"
CALL = re.compile(PATTERN)
#: A call the scan can read: one literal page, then an anchor or the end. The
#: Jest `/^docsUrl\(\s*'[^']*'\s*[,)]/`, tried at each call.
VISIBLE_PATTERN = r"docsUrl\(\s*'[^']*'\s*[,)]"
VISIBLE = re.compile(VISIBLE_PATTERN)
SKIPPED_DIRS = ("tests", "__mocks__", "node_modules")
SOURCE = re.compile(r"\.(ts|tsx)$")
TEST_FILE = re.compile(r"\.test\.tsx?$")
DEFINING_MODULE = os.path.join("services", "docs.ts")
#: The homepage's "Specific to Controlvar": its links are outside its quotes,
#: so the quotes' own nav rule never sees them.
SPECIFICS = SRC_ROOT / "components" / "homepage" / "Specifics.tsx"


def _source_files(directory: Path) -> List[Path]:
    """`sourceFiles()` in the Jest test: .ts/.tsx, no tests, no mocks."""
    out: List[Path] = []
    for entry in sorted(os.listdir(directory)):
        full = directory / entry
        if full.is_dir():
            if entry in SKIPPED_DIRS:
                continue
            out.extend(_source_files(full))
        elif SOURCE.search(entry) and not TEST_FILE.search(entry):
            out.append(full)
    return out


def _scanned_files() -> List[Path]:
    """The source files minus the module that defines docsUrl."""
    return [f for f in _source_files(SRC_ROOT) if not str(f).endswith(DEFINING_MODULE)]


def _target_for(page: str) -> Path:
    """`targetFor()` in the Jest test, mirroring the README handling in docs.ts."""
    path = re.sub(r"^/+|/+$", "", page)
    if path in ("", "README"):
        return DOCS_ROOT / "README.md"
    return DOCS_ROOT / f"{path}.md"


def _line(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _call_sites() -> List[Tuple[str, str]]:
    """(page, "file:line") for every docsUrl('...') the Jest rule collects."""
    found = []
    for file in _scanned_files():
        text = file.read_text(encoding="utf-8")
        for m in CALL.finditer(text):
            found.append((m.group(1), f"{_rel(file)}:{_line(text, m.start())}"))
    return found


def test_finds_the_call_sites_at_all():
    """An empty scan cannot pass vacuously (the Jest guard: more than 20)."""
    found = _call_sites()
    assert len(found) > 20, (
        f"only {len(found)} docsUrl() call sites found under {SRC_ROOT}"
    )
    assert DOCS_ROOT.is_dir()
    # The empty-string link resolves to docs/README.md; a regex requiring a
    # non-empty page would drop it without anyone noticing.
    assert any(page == "" for page, _ in found), "docsUrl('') is no longer collected"


def test_points_every_link_at_a_file_that_exists():
    missing = [
        f"{site}: docsUrl('{page}') -> {_rel(_target_for(page))}"
        for page, site in _call_sites()
        if not _target_for(page).exists()
    ]
    assert missing == [], (
        "dashboard docs links to pages that do not exist:\n" + "\n".join(missing)
    )


def test_every_call_is_one_the_rule_can_see():
    """A `docsUrl(` call that is not one literal page is a link neither this
    test nor the Jest one checks: `docsUrl('sdk/' + key)` is read as the page
    `sdk/`. Use a single-quoted literal page, or a map of literal calls."""
    unseen = []
    for file in _scanned_files():
        text = file.read_text(encoding="utf-8")
        for m in re.finditer(r"docsUrl\(", text):
            if not VISIBLE.match(text, m.start()):
                snippet = text[m.start() : m.start() + 60].split("\n")[0]
                unseen.append(f"{_rel(file)}:{_line(text, m.start())}: {snippet}")
    assert unseen == [], (
        "docsUrl() calls the docs-link rule cannot resolve (it matches only "
        "docsUrl('<page>')):\n" + "\n".join(unseen)
    )


def test_the_specifics_section_links_pages_the_nav_lists():
    """`links docs pages that exist and that the nav of mkdocs.yml lists` in
    home-page.test.tsx, which a change to mkdocs.yml alone does not run."""
    pages = [m.group(1) for m in CALL.finditer(SPECIFICS.read_text(encoding="utf-8"))]
    # The count the Jest test has: an empty scan cannot pass.
    assert len(pages) == 7, f"{_rel(SPECIFICS)} links {pages}"
    nav = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    missing = [
        page
        for page in pages
        if not re.search(rf"^\s+- [^:\n]+: {re.escape(page)}\.md\s*$", nav, re.M)
    ]
    assert missing == [], (
        f"{_rel(SPECIFICS)} links pages mkdocs.yml's nav does not list: {missing}"
    )


class TestTheTwinStillMirrorsJest:
    """The Jest file still states the rule this file copies."""

    MUST_CONTAIN = (
        f"/{PATTERN}/g",
        f"/^{VISIBLE_PATTERN}/;",
        "if (!literal.test(text.slice(m.index))) {",
        "file.endsWith(join('services', 'docs.ts'))",
        "entry === 'tests'",
        "entry === '__mocks__'",
        "entry === 'node_modules'",
        "if (path === '' || path === 'README') return join(DOCS_ROOT, 'README.md');",
        r"/\.(ts|tsx)$/",
        r"!/\.test\.tsx?$/",
        r"page.replace(/^\/+|\/+$/g, '')",
        "expect(found.length).toBeGreaterThan(20);",
    )

    @pytest.mark.parametrize("fragment", MUST_CONTAIN)
    def test_jest_rule_is_unchanged(self, fragment):
        text = JEST_TWIN.read_text(encoding="utf-8")
        assert fragment in text, (
            f"{_rel(JEST_TWIN)} no longer contains {fragment!r}. The docs-link "
            f"rule is stated twice, there and in {_rel(Path(__file__))}; change "
            "both together, then update this pin."
        )
