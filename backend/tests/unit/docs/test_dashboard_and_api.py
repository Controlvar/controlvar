"""One table says what the dashboard does and what is API only, and it stays true.

``docs/guides/dashboard-and-api.md`` holds one table: a capability per row and,
in the second column, exactly ``Dashboard`` or ``API only``. This test gives
every row a **witness**: the ``data-testid`` the capability's screen carries.
Then, by reading files only:

* the table's rows are exactly ``WITNESSES``' keys, so a row cannot vanish,
  be renamed or be added without this file changing too;
* an ``API only`` row whose witness is in the dashboard's source fails: the
  screen has shipped and the docs still send the reader to the API;
* a ``Dashboard`` row whose witness is not in the source fails: the docs claim
  a screen that does not exist;
* a ``Dashboard`` row whose witness no page test asserts fails. A test counts
  when it imports a module under ``frontend/src/pages/`` and calls
  ``getByTestId`` / ``findByTestId`` (or the ``All`` forms, which throw when
  nothing matches) on the witness, outside any skipped test: a call inside
  ``it.skip`` / ``test.skip`` / ``describe.skip`` (and their ``.each`` forms),
  ``xit`` / ``xtest`` / ``xdescribe``, or a comment does not count, and a page
  test with ``.only`` / ``fit`` / ``fdescribe`` fails outright, because it
  skips every other test in its file. That ties the witness to a page test
  that runs, not to a component nothing renders.

A witness is a literal ``data-testid="id"``, asserted as ``"id"``. A witness
written ``"stem-*"`` is instead the template literal
``data-testid={`stem-${...}`}``, asserted as ``"stem-<digits>"``; that form is
for a screen that repeats per item (one per variant). Nothing else counts: an
id built any other way at run time is invisible to this check.

The second half is a sweep of sentences that were false, or became false, about
what the dashboard does (see ``STALE``). Like test_no_stale_dashboard_claims.py
it forbids the removed phrasings, not every new wording of them.

No git and no network, so it runs the same in ``scripts/core_build.sh``'s
copy, which has no ``.git``, and in the docs-only lane through its directory,
``backend/tests/unit/docs/`` (pinned in test_docs_only_gate.py).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Set, Tuple

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
PAGE = REPO_ROOT / "docs" / "guides" / "dashboard-and-api.md"
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"

DASHBOARD = "Dashboard"
API_ONLY = "API only"
HEADER = "| Capability | Where you do it | Guide |"
SEPARATOR = "|---|---|---|"

#: Every row of the table, by its exact Capability cell, and its witness.
#: A pull request that adds one of these screens changes its row's cell to
#: Dashboard; the witness is already here. "(#nnn)" names the pull request
#: that carries (or carried) exactly that id.
WITNESSES: Dict[str, str] = {
    # The everyday loop.
    "Create an experiment, with guided setup or the single-page form": "switch-to-advanced",
    "Start, pause, resume and complete an experiment": "experiment-actions",
    "Change an experiment's targeting while it is a draft or paused": "targeting-section",
    "Create a feature flag": "submit-flag",
    "Turn a feature flag on or off": "flag-toggle",
    "Set a flag's targeting rules and rollout percentage": "flag-targeting-section",
    "See a flag's rollout schedule and its stages": "rollout-schedule-section",
    "Roll a feature flag back from the safety page": "rollback-button",
    # #916: no screen writes a flag's safety config; the dashboard's only
    # component for it (FlagSafetyConfig.tsx) is an unrendered stub, not a
    # page any route reaches.
    "Set a feature flag's per-flag safety config (error-rate and latency "
    "thresholds, rollback percentage)": "safety-config-editor",
    "Manage users and their roles": "user-management-page",
    "Change your own password (local sign-in)": "change-password-page",  # #503
    "Read the audit log, and download it as CSV or JSON": "audit-log-page",  # #915
    # The #442 slice before launch: B (#882), one toggle per variant.
    "Give each variant a configuration (a JSON payload) when creating an experiment": (
        "variant-configuration-toggle-*"
    ),
    "Turn on Bayesian analysis when creating an experiment": "analysis-bayesian",
    "See on the experiment page whether Bayesian analysis is on": "experiment-bayesian",
    # #1449's read-only display on the experiment page, first half (PR A).
    "See on the experiment page whether sequential testing (beta) is on": (
        "experiment-sequential"
    ),
    "See on the experiment page its start and end dates": "experiment-start-date",
    # The second half (PR B): read-only, for ADMIN and DEVELOPER. Each witness
    # marks only the answered state; loading, refused and error have ids of
    # their own, so a page that only ever refused could not pass.
    "See on the experiment page its exclusion group": "experiment-exclusion-group",
    "See on the experiment page whether a global holdout is active": (
        "experiment-holdout"
    ),
    # #440's Segments page (#881).
    "Segments: list, create, and upload a list of user ids": "new-segment-btn",
    # The rest of the #442 slice: A (#887), D (#885), and C, still to come
    # (A and D merged; C flips its three rows).
    "The sample-ratio check on the results page": "srm-notice",  # A
    "Bayesian results: chance to be best, expected loss and credible intervals": (
        "bayesian-panel"  # A
    ),
    # #1445: the results page's Sequential view (SequentialMonitor).
    "Sequential results (beta): whether to stop, and the confidence sequence": (
        "sequential-monitor"
    ),
    "See a bandit experiment's current traffic weights": "bandit-weights",  # D
    "Clone an experiment": "experiment-clone",  # C
    "Delete a draft experiment": "experiment-delete",  # C
    "Edit a draft experiment's name, description and hypothesis": (
        "experiment-edit-details"  # C
    ),
    # After launch (#442's checklist).
    "Change a draft experiment's variants or metrics": "experiment-edit-variants",
    "Schedule an experiment's start and end dates": "experiment-schedule-form",
    # #1456: the flag page's rollout schedule controls.
    "Create or edit a rollout schedule and its stages": "rollout-schedule-form",
    "Activate, pause, resume or cancel a rollout schedule, and advance a manual "
    "stage": "rollout-schedule-actions",
    # #1456 leaves these to the API: the dashboard sends no schedule dates and
    # no minimum duration, and always the default maximum, 100.
    "Set a rollout schedule's own start and end dates, maximum percentage and "
    "minimum stage duration": "rollout-schedule-settings",
    "Delete a feature flag": "flag-delete",
    "Archive and unarchive a feature flag": "flag-archive",
    "Create a bandit experiment": "bandit-create",
    "Set a bandit's weights by hand": "bandit-weights-override",
    # #1446: only reading a holdout's results is beta.
    "Global holdouts: create and activate; read their results (beta)": "holdouts-page",
    "Mutual exclusion groups": "exclusion-groups-page",
    "Split-URL experiments (full profile)": "split-url-config",
    "CUPED-adjusted results (beta)": "cuped-results",
    "The interaction scan and the pair test (beta)": "interactions-page",
    "Download results as CSV": "results-export-csv",
    "The effect size in the metric comparison table": "effect-size-column",
    "Each variant's confidence interval on the results page": "variant-interval-column",
    "Bayesian priors, loss threshold and credible level": "bayesian-config-fields",
    # #1454: the guided setup's control, the twin of analysis-bayesian.
    "Turn on sequential testing (beta) when creating an experiment": (
        "analysis-sequential"
    ),
    # #1454 (C13): an existing draft's switch and its alpha, when it comes.
    "Turn sequential testing (beta) on or off for an existing draft, and set its "
    "alpha": "experiment-sequential-settings",
}

#: Files the scans must have read. A scan rooted in the wrong place, or a
#: filter that drops everything, fails here instead of passing on nothing.
SOURCE_FLOOR = (
    "pages/experiments/[id].tsx",
    "pages/experiments/new.tsx",
    "pages/feature-flags/[id].tsx",
    "pages/results/[id].tsx",
    "components/experiments/TargetingSection.tsx",
)
PAGE_TEST_FLOOR = (
    "tests/pages/experiment-detail.test.tsx",
    "tests/pages/feature-flag-detail.test.tsx",
    "tests/pages/experiment-new-guided.test.tsx",
)

_SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx")
_LINK = re.compile(r"\[[^\]\n]+\]\(([^)\s#]+)(#[^)\s]*)?\)")
#: A test that loads a page module: `from '@/pages/x'`, a relative
#: `../../pages/x`, or the same in `import()` / `require()` / `jest.requireActual`.
_PAGE_IMPORT = re.compile(
    r"""(?:\bfrom\s+|\bimport\s*\(\s*|\brequire(?:Actual)?\s*\(\s*)"""
    r"""['"](?:@/pages/|(?:\.\./)+pages/)[^'"]+['"]"""
)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _cells(line: str) -> List[str]:
    if not (line.startswith("|") and line.endswith("|")):
        raise AssertionError(f"not a table row: {line!r}")
    return [cell.strip() for cell in line[1:-1].split("|")]


def parse_table(text: str) -> List[Tuple[str, str, str]]:
    """The page's one table as (capability, where, guide link target).

    Refuses anything but exactly one table with the exact header, three cells
    per row, a closed vocabulary in the second cell, one link in the third,
    and no repeated capability.
    """
    lines = text.splitlines()
    starts = [i for i in range(len(lines)) if lines[i].lstrip().startswith("|")]
    header_at = [i for i in starts if lines[i].strip() == HEADER]
    assert len(header_at) == 1, f"expected the header {HEADER!r} exactly once"
    start = header_at[0]
    assert lines[start + 1].strip() == SEPARATOR, "the separator row has changed"
    rows: List[Tuple[str, str, str]] = []
    end = start + 2
    while end < len(lines) and lines[end].strip().startswith("|"):
        cells = _cells(lines[end].strip())
        assert len(cells) == 3, f"line {end + 1}: {len(cells)} cells, want 3"
        capability, where, guide = cells
        assert where in (DASHBOARD, API_ONLY), (
            f"line {end + 1}: {where!r} is neither {DASHBOARD!r} nor {API_ONLY!r}"
        )
        link = _LINK.fullmatch(guide)
        assert link, f"line {end + 1}: the guide cell is not one link: {guide!r}"
        rows.append((capability, where, link.group(1) + (link.group(2) or "")))
        end += 1
    others = [i + 1 for i in starts if not start <= i < end]
    assert not others, f"a second table (or stray row) at lines {others}"
    names = [row[0] for row in rows]
    repeated = sorted({n for n in names if names.count(n) > 1})
    assert not repeated, f"capabilities listed twice: {repeated}"
    return rows


# ---------------------------------------------------------------------------
# Scanning the dashboard
# ---------------------------------------------------------------------------
def _is_test(rel: str) -> bool:
    return (
        rel.startswith("tests/")
        or "/tests/" in rel
        or "/__tests__/" in rel
        or ".test." in rel
        or ".spec." in rel
    )


def scan(src: Path) -> Tuple[Dict[str, str], Dict[str, str]]:
    """(non-test sources, page tests), each {path relative to src: text}."""
    sources: Dict[str, str] = {}
    page_tests: Dict[str, str] = {}
    for path in sorted(src.rglob("*")):
        if not path.is_file() or path.suffix not in _SOURCE_SUFFIXES:
            continue
        rel = path.relative_to(src).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        if not _is_test(rel):
            sources[rel] = text
        elif ".test." in rel and _PAGE_IMPORT.search(text):
            page_tests[rel] = text
    return sources, page_tests


_SKIPPED_CALL = re.compile(
    r"(?:(?:it|test|describe)\.skip|x(?:it|test|describe))(?:\.each)?\s*\("
)
_FOCUSED_CALL = re.compile(
    r"(?<![\w.$])(?:(?:it|test|describe)\.only|f(?:it|describe))(?:\.each)?\s*\("
)
_IDENT = re.compile(r"[\w$.]")


def _skip_string(text: str, i: int) -> int:
    """The index just past the string literal opening at text[i]."""
    quote = text[i]
    i += 1
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if char == quote or (char == "\n" and quote != "`"):
            return i + 1
        i += 1
    return i


def _skip_comment(text: str, i: int) -> int:
    """The index just past the comment opening at text[i:i + 2]."""
    if text.startswith("//", i):
        end = text.find("\n", i)
        return len(text) if end < 0 else end
    end = text.find("*/", i + 2)
    return len(text) if end < 0 else end + 2


def _skip_parens(text: str, i: int) -> int:
    """The index just past the balanced (...) opening at text[i]."""
    depth = 0
    while i < len(text):
        char = text[i]
        if char in "'\"`":
            i = _skip_string(text, i)
            continue
        if text.startswith(("//", "/*"), i):
            i = _skip_comment(text, i)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


def live_text(text: str) -> str:
    """A test file with its comments and skipped tests taken out.

    One linear pass that knows strings, comments and parentheses: a skipped
    call (``it.skip(...)``, ``xit(...)``, ``describe.skip(...)``, and the
    ``.each(table)(...)`` forms) is removed through its closing parenthesis.
    A parenthesis inside a regular-expression literal can unbalance it; the
    failure is then a missing witness (red), never a skipped one counted.
    """
    out: List[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char in "'\"`":
            end = _skip_string(text, i)
            out.append(text[i:end])
            i = end
            continue
        if text.startswith(("//", "/*"), i):
            i = _skip_comment(text, i)
            continue
        skipped = None
        if i == 0 or not _IDENT.match(text[i - 1]):
            skipped = _SKIPPED_CALL.match(text, i)
        if skipped:
            i = _skip_parens(text, skipped.end() - 1)
            rest = text[i:].lstrip()
            if skipped.group(0).count(".each") and rest.startswith("("):
                i = _skip_parens(text, len(text) - len(rest))
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _source_pattern(witness: str) -> re.Pattern:
    if witness.endswith("-*"):
        return re.compile(r"data-testid=\{\s*`%s\$\{" % re.escape(witness[:-1]))
    s = re.escape(witness)
    return re.compile(
        r"""data-testid=(?:"%s"|'%s'|\{\s*["'`]%s["'`]\s*\})""" % (s, s, s)
    )


def _assert_pattern(witness: str) -> re.Pattern:
    if witness.endswith("-*"):
        s = re.escape(witness[:-1]) + r"\d+"
    else:
        s = re.escape(witness)
    return re.compile(r"""\b(?:get|find)(?:All)?ByTestId\(\s*(["'`])%s\1""" % s)


def where_witnessed(stem: str, files: Dict[str, str], pattern) -> List[str]:
    rx = pattern(stem)
    return sorted(rel for rel, text in files.items() if rx.search(text))


def problems(
    rows: Sequence[Tuple[str, str, str]],
    witnesses: Dict[str, str],
    sources: Dict[str, str],
    page_tests: Dict[str, str],
) -> List[str]:
    """Every disagreement between the table and the dashboard, one per line."""
    found: List[str] = []
    table = {capability: where for capability, where, _ in rows}
    for name in sorted(set(witnesses) - set(table)):
        found.append(f"no row for {name!r}: a row was deleted or renamed")
    for name in sorted(set(table) - set(witnesses)):
        found.append(f"{name!r} has no witness in WITNESSES: add one")
    for capability, where in table.items():
        stem: Optional[str] = witnesses.get(capability)
        if stem is None:
            continue
        in_source = where_witnessed(stem, sources, _source_pattern)
        if where == API_ONLY and in_source:
            found.append(
                f"{capability!r} says {API_ONLY} but the dashboard has its screen "
                f"(data-testid {stem!r} in {', '.join(in_source)}): change the row "
                f"to {DASHBOARD}"
            )
        if where == DASHBOARD and not in_source:
            found.append(
                f"{capability!r} says {DASHBOARD} but no non-test file under "
                f"frontend/src carries data-testid {stem!r}"
            )
        if where == DASHBOARD and in_source:
            live = {rel: live_text(text) for rel, text in page_tests.items()}
            if not where_witnessed(stem, live, _assert_pattern):
                found.append(
                    f"{capability!r} says {DASHBOARD} but no test that renders a "
                    f"page under frontend/src/pages/ asserts data-testid {stem!r} "
                    "(getByTestId / findByTestId) outside a skipped test"
                )
    for rel, text in sorted(page_tests.items()):
        if _FOCUSED_CALL.search(live_text(text)):
            found.append(
                f"{rel} uses .only / fit / fdescribe, which skips every other "
                "test in the file: remove it"
            )
    return found


# ---------------------------------------------------------------------------
# The table against the tree
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def rows() -> List[Tuple[str, str, str]]:
    return parse_table(PAGE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def tree() -> Tuple[Dict[str, str], Dict[str, str]]:
    return scan(FRONTEND_SRC)


def test_the_scans_read_the_named_files(tree):
    sources, page_tests = tree
    missing = [f for f in SOURCE_FLOOR if f not in sources]
    missing += [f for f in PAGE_TEST_FLOOR if f not in page_tests]
    assert not missing, f"the scan did not read {missing}: it is broken, not clean"


def test_the_table_and_the_dashboard_agree(rows, tree):
    sources, page_tests = tree
    found = problems(rows, WITNESSES, sources, page_tests)
    assert not found, "\n".join(found)


def test_each_witness_names_one_capability():
    stems = list(WITNESSES.values())
    assert len(stems) == len(set(stems)), "two rows share a witness"


_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)


def slug(heading: str) -> str:
    """The anchor MkDocs' default slugify and GitHub give a plain heading.

    The same rule as test_dashboard_docs_anchors.py (#887).
    """
    text = heading.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return re.sub(r"\s", "-", text)


def heading_slugs(path: Path) -> Set[str]:
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^```.*?^```", "", text, flags=re.MULTILINE | re.DOTALL)
    return {slug(h) for h in _HEADING.findall(text)}


def test_every_guide_link_resolves(rows):
    """The page exists, and so does the heading a `#fragment` names."""
    broken = []
    for _, _, target in rows:
        if target.startswith("https://"):
            continue
        page, _, anchor = target.partition("#")
        path = (PAGE.parent / page).resolve()
        if not path.is_file():
            broken.append(f"{target}: no such page")
        elif anchor and anchor not in heading_slugs(path):
            broken.append(f"{target}: no heading in {page} has the anchor #{anchor}")
    assert not broken, "guide links that do not land:\n" + "\n".join(broken)


@pytest.mark.parametrize(
    "heading, expected",
    [
        ("Archived Flags", "archived-flags"),
        ("Global Holdout", "global-holdout"),
        (
            "Sequential Testing — Stop Experiments Early",
            "sequential-testing--stop-experiments-early",
        ),
    ],
)
def test_slug_matches_the_rule(heading, expected):
    assert slug(heading) == expected


# ---------------------------------------------------------------------------
# The checker itself, on planted inputs
# ---------------------------------------------------------------------------
_PLANTED = {"Clone an experiment": "experiment-clone"}
_ROW = [("Clone an experiment", API_ONLY, "x.md")]
_SHIPPED = {"pages/experiments/[id].tsx": '<button data-testid="experiment-clone">'}
_PAGE_TEST = {
    "tests/pages/x.test.tsx": "import P from '@/pages/experiments/[id]';\n"
    "expect(screen.getByTestId('experiment-clone')).toBeInTheDocument();"
}


@pytest.mark.parametrize(
    "rows_, sources, page_tests, expect",
    [
        (_ROW, {}, {}, None),
        (_ROW, _SHIPPED, {}, "says API only but the dashboard has its screen"),
        ([("Clone an experiment", DASHBOARD, "x.md")], {}, {}, "no non-test file"),
        ([("Clone an experiment", DASHBOARD, "x.md")], _SHIPPED, {}, "no test that"),
        ([("Clone an experiment", DASHBOARD, "x.md")], _SHIPPED, _PAGE_TEST, None),
        ([], {}, {}, "a row was deleted or renamed"),
    ],
)
def test_the_checker_on_planted_trees(rows_, sources, page_tests, expect):
    found = problems(rows_, _PLANTED, sources, page_tests)
    if expect is None:
        assert found == []
    else:
        assert any(expect in f for f in found), found


@pytest.mark.parametrize(
    "sources, page_tests, expect",
    [
        (
            {"c.tsx": "data-testid={`experiment-clone-${i}`}"},
            {"tests/pages/x.test.tsx": "getAllByTestId('experiment-clone-0')"},
            None,
        ),
        ({"c.tsx": 'data-testid="experiment-clone-0"'}, {}, "no non-test file"),
        (
            {"c.tsx": "data-testid={`experiment-clone-${i}`}"},
            {"tests/pages/x.test.tsx": "getByTestId('experiment-clone-')"},
            "no test that",
        ),
    ],
)
def test_a_per_item_witness(sources, page_tests, expect):
    """`stem-*` is the template form only; a literal does not satisfy it."""
    page_tests = {
        k: "import P from '@/pages/experiments/[id]';\n" + v
        for k, v in page_tests.items()
    }
    found = problems(
        [("Clone an experiment", DASHBOARD, "x.md")],
        {"Clone an experiment": "experiment-clone-*"},
        sources,
        page_tests,
    )
    if expect is None:
        assert found == []
    else:
        assert any(expect in f for f in found), found


_ASSERT = "expect(screen.getByTestId('experiment-clone')).toBeInTheDocument();"


@pytest.mark.parametrize(
    "body",
    [
        f"it.skip('clones', () => {{ {_ASSERT} }});",
        f"test.skip('clones', () => {{ {_ASSERT} }});",
        f"xit('clones', () => {{\n  {_ASSERT}\n}});",
        f"xtest('clones', async () => {{ {_ASSERT} }});",
        f"describe.skip('clone', () => {{\n  it('a (b) c', () => {{\n    {_ASSERT}\n  }});\n}});",
        f"xdescribe('clone', () => {{ it('x', () => {{ {_ASSERT} }}); }});",
        f"it.skip.each([[1], [2]])('row %s', () => {{ {_ASSERT} }});",
        f"// {_ASSERT}",
        f"/* {_ASSERT} */",
    ],
)
def test_a_skipped_or_commented_assertion_does_not_count(body):
    page_test = {
        "tests/pages/x.test.tsx": "import P from '@/pages/experiments/[id]';\n"
        "it('runs', () => { expect(1).toBe(1); });\n" + body
    }
    found = problems(
        [("Clone an experiment", DASHBOARD, "x.md")], _PLANTED, _SHIPPED, page_test
    )
    assert any("outside a skipped test" in f for f in found), found


@pytest.mark.parametrize(
    "body",
    [
        f"it('clones (it.skip is not called here)', () => {{ {_ASSERT} }});",
        f"it.skip('other', () => {{ expect(')').toBe(')'); }});\nit('c', () => {{ {_ASSERT} }});",
        f"it.each([[1]])('row %s', () => {{ {_ASSERT} }});",
        f"const url = 'http://x';\nit('c', () => {{ {_ASSERT} }});",
    ],
)
def test_a_running_assertion_still_counts(body):
    page_test = {
        "tests/pages/x.test.tsx": "import P from '@/pages/experiments/[id]';\n" + body
    }
    found = problems(
        [("Clone an experiment", DASHBOARD, "x.md")], _PLANTED, _SHIPPED, page_test
    )
    assert found == [], found


@pytest.mark.parametrize(
    "body",
    [
        "it.only('x', () => {});",
        "test.only('x', () => {});",
        "describe.only('x', () => {});",
        "fit('x', () => {});",
        "fdescribe('x', () => {});",
    ],
)
def test_a_focused_page_test_fails(body):
    page_test = {
        "tests/pages/x.test.tsx": "import P from '@/pages/experiments/[id]';\n"
        + body
        + "\n"
        + _ASSERT
    }
    found = problems(
        [("Clone an experiment", DASHBOARD, "x.md")], _PLANTED, _SHIPPED, page_test
    )
    assert any(".only / fit / fdescribe" in f for f in found), found


@pytest.mark.parametrize(
    "text",
    [
        "data-testid={experimentCloneId}",  # built at run time: not a witness
        'data-testid="experiment-clone-button"',  # a longer id
        "data-testid={`experiment-clone-${i}`}",  # the per-item form of another id
        "queryByTestId('experiment-clone')",  # not a source attribute at all
    ],
)
def test_only_a_literal_id_is_a_witness(text):
    assert not _source_pattern("experiment-clone").search(text)


@pytest.mark.parametrize(
    "text",
    [
        "expect(screen.queryByTestId('experiment-clone')).toBeNull()",
        "getByTestId('experiment-clone-button')",
        "// getByTestId experiment-clone",
    ],
)
def test_an_absence_check_or_another_id_is_not_an_assertion(text):
    assert not _assert_pattern("experiment-clone").search(text)


@pytest.mark.parametrize(
    "text, refusal",
    [
        ("| Capability | Where | Guide |\n|---|---|---|\n", "expected the header"),
        (f"{HEADER}\n|---|---|\n", "separator"),
        (f"{HEADER}\n{SEPARATOR}\n| a | Screen | [g](g.md) |\n", "neither"),
        (f"{HEADER}\n{SEPARATOR}\n| a | API only |\n", "cells"),
        (f"{HEADER}\n{SEPARATOR}\n| a | API only | g.md |\n", "not one link"),
        (
            f"{HEADER}\n{SEPARATOR}\n| a | API only | [g](g.md) |\n"
            "| a | Dashboard | [g](g.md) |\n",
            "listed twice",
        ),
        (
            f"{HEADER}\n{SEPARATOR}\n| a | API only | [g](g.md) |\n\n| b | c |\n",
            "second table",
        ),
    ],
)
def test_the_parser_refuses_a_malformed_table(text, refusal):
    with pytest.raises(AssertionError, match=refusal):
        parse_table(text)


# ---------------------------------------------------------------------------
# The sweep: sentences about the dashboard that were, or became, false
# ---------------------------------------------------------------------------
#: (pattern, why). Matched case-insensitively against each page with its
#: whitespace collapsed and `_DROP` removed, so a claim that wraps lines or sits
#: inside bold or code is still found.
STALE: Tuple[Tuple[str, str], ...] = (
    (
        r"schedule and delete only (your|their) own",
        "ADMIN and DEVELOPER schedule and delete any experiment, by role (D33)",
    ),
    (
        r"(refresh|update)[^.]{0,60}\bfrom the dashboard",
        "the dashboard has no control that refreshes a bandit; it is an API call",
    ),
    (
        r"interval (doesn't|does not|don't|do not) cross zero",
        "the results page's interval is each variant's own rate, which never "
        "crosses zero; the interval on the lift is not computed (#439)",
    ),
    (
        r"no bayesian (or cuped )?tab",
        "the results page's Bayesian panel arrives with #887; say where to look "
        "either way",
    ),
    (
        r"bayesian results \(api only\)",
        "the results page's Bayesian panel arrives with #887",
    ),
    (
        r"bayesian in the api\)",
        "the results page's Bayesian panel arrives with #887",
    ),
    (
        r"bayesian and frequentist analysis",
        "a headline that does not say which of the two the dashboard shows",
    ),
    (
        r"delete (this|the|a) draft and (create|make) it again",
        "the experiment page's Edit variants and metrics changes a draft's "
        "variants and metrics in place (#1453)",
    ),
    (
        r"shows the schedule[^.]{0,80}read-only",
        "the flag's page creates and runs a rollout schedule (#1456)",
    ),
    (
        r"schedules (?:can be|are) (?:created|made) (?:only )?through "
        r"(?:the api\b|/api/v1/rollout-schedules)",
        "the flag's page creates a rollout schedule (#1456); the API is the other way",
    ),
)

_DROP = frozenset("*`")


def _normalise(text: str) -> Tuple[str, List[int]]:
    flat: List[str] = []
    lines: List[int] = []
    line = 1
    for char in text:
        if char == "\n":
            line += 1
        if char in _DROP:
            continue
        if char.isspace():
            if flat and flat[-1] == " ":
                continue
            char = " "
        flat.append(char.lower())
        lines.append(line)
    return "".join(flat), lines


def _hits(path: Path) -> Iterator[str]:
    flat, lines = _normalise(path.read_text(encoding="utf-8", errors="replace"))
    rel = path.relative_to(REPO_ROOT).as_posix()
    for pattern, why in STALE:
        for match in re.finditer(pattern, flat):
            yield f"{rel}:{lines[match.start()]}: {match.group(0)!r} -- {why}"


#: Pages the sweep must read, named, so a wrong root fails instead of passing.
SWEEP_FLOOR = (
    "README.md",
    "demo/DEMO_GUIDE.md",
    "demo/shoplab/README.md",
    "demo/streampulse/README.md",
    "docs/api/multi-armed-bandit.md",
    "docs/guides/user-guide.md",
    "docs/getting-started/faq.md",
)


def _swept() -> List[Path]:
    skip: Set[str] = {"node_modules", ".next", "site", "venv"}
    files = [REPO_ROOT / "README.md"]
    for root in (REPO_ROOT / "docs", REPO_ROOT / "demo"):
        files += [
            p
            for p in sorted(root.rglob("*.md"))
            if not skip.intersection(p.relative_to(REPO_ROOT).parts)
        ]
    rels = {p.relative_to(REPO_ROOT).as_posix() for p in files}
    missing = [f for f in SWEEP_FLOOR if f not in rels]
    assert not missing, f"the sweep did not read {missing}: it is broken, not clean"
    return files


def test_no_page_repeats_a_removed_dashboard_claim():
    hits = [hit for path in _swept() for hit in _hits(path)]
    assert not hits, (
        "these say something false about what the dashboard does; "
        "docs/guides/dashboard-and-api.md is the list:\n" + "\n".join(hits)
    )


@pytest.mark.parametrize(
    "removed",
    [
        "create, change, schedule and delete\nonly your own experiments",
        "`POST /api/v1/bandit/{experiment_id}/update` forces an immediate refresh "
        "from the dashboard.",
        "  - Confidence interval doesn't cross zero",
        "- The dashboard has no Bayesian or CUPED tab. The Bayesian analysis is in the API",
        'boundary, "can stop early?"). The dashboard has no Bayesian tab: the',
        "Classic A/B test, sequential testing (mSPRT), Bayesian results (API only), variant",
        "(frequentist + sequential in the dashboard, Bayesian in the API)",
        "Statistical rigor with Bayesian and Frequentist analysis",
        "Variants and metrics cannot; to change them, delete this draft and\n"
        "create it again.",
        # #1456: rollouts.md, user-guide.md and the flag page's own empty state.
        "In the dashboard, the flag's page shows the schedule read-only under **Rollout schedule**: its",
        "`POST /api/v1/rollout-schedules/{schedule_id}/activate`. The flag's page in the dashboard\n"
        "shows the schedule, its status and its stages, read-only. A schedule like this one:",
        "No rollout schedule. The rollout percentage below applies immediately; schedules\n"
        "can be created through `/api/v1/rollout-schedules`.",
        "Rollout schedules are made through the API (/api/v1/rollout-schedules);",
    ],
)
def test_the_sweep_catches_the_removed_wording(removed):
    flat, _ = _normalise(removed)
    assert any(re.search(pattern, flat) for pattern, _ in STALE), (
        f"no rule matches the removed wording {removed!r}"
    )
