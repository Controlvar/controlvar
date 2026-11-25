"""scripts/jest_titles.py reads a jest file's titles as jest reports them (#1819).

``scripts/doc_claims.py`` holds a claim's jest target to it on every pull
request; ``scripts/rc_evidence.py`` judges the same target against jest's report
at a release. ``SOURCE`` is one file with every title shape, and ``REPORTED`` is
what jest 30.5.2 reported for it, so each name below is put to both: the two
agree, except where the reader's docstring says it cannot (``RELEASE_ONLY``: a
title it cannot read, so it refuses; ``READER_ONLY``: code it cannot run).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, List, Optional

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]


def _script(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        f"{name}_for_jest_titles_test", REPO_ROOT / "scripts" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


jt = _script("jest_titles")
rc = _script("rc_evidence")

PATH = "frontend/src/tests/a.test.ts"
SOURCE = r"""// it('only in a comment', () => {});
/* test('only in a block comment', () => {}); */
const said = "it('only in a string', () => {})";
const drawn = `it('only in a template', () => {})`;
const found = /it('only in a regular expression', x)/.test('it(');
const on = true;
const VIEWS: Array<[string, () => void]> = [
  ['guided', () => {}],
  ['advanced: "quoted"', () => {}],
];
const ROWS = [...VIEWS];

function row(name: string) {
  it(name, () => {});
}

describe('the client', () => {
  it('caches a flag', () => {});
  it("doesn't retry", () => {});
  it('a line — \'quoted\' \\ kept', () => {});
  it(`plain template`, () => {});
  it('joined ' + "of " + `three`, () => {});
  it(on ? 'shown when on' : 'shown when off', () => {});
  it(`reads ${said}`, () => {});
  it('grows 10% to 100%% of users', () => {});
  it('formats %s as written', () => {});
  test.each(['memory', 'disk'])('%s: keeps a flag', () => {});
  it.each<[string, number]>([
    ['one', 1],
    ['two', 2], // the last row
  ])('%s is %d, row %#, number %$, all 100%%', () => {});
  it.each(VIEWS)('view %s saves', () => {});
  it.each(VIEWS)('view %p prints', () => {});
  it.each(ROWS)('spread %s saves', () => {});
  it.each([[true, null], [false, undefined]])('%s and %s', () => {});
  it.each([{ name: 'first' }])('$name is filled in', () => {});
  it.each([['money', 1]])('%s costs $5', () => {});
  it.each`
    a    | b
    ${1} | ${2}
  `('$a and $b', () => {});
  row('declared by a helper');
  describe.each(['light', 'dark'])('in %s mode', (mode) => {
    it('renders', () => {
      expect(mode).toBeTruthy();
    });
    it.each([1, 2])('renders %d times', () => {});
  });
});
test('at the top', () => {
  expect([drawn, found, ROWS].length).toBe(3);
});
"""

#: jest 30.5.2's report of SOURCE: each test's describe titles, and its own.
IN_THE_CLIENT = (
    "caches a flag",
    "doesn't retry",
    "a line — 'quoted' \\ kept",
    "plain template",
    "joined of three",
    "shown when on",
    "reads it('only in a string', () => {})",
    "grows 10% to 100%% of users",
    "formats %s as written",
    "memory: keeps a flag",
    "disk: keeps a flag",
    "one is 1, row 0, number 1, all 100%",
    "two is 2, row 1, number 2, all 100%",
    "view guided saves",
    'view advanced: "quoted" saves',
    'view "guided" prints',
    'view "advanced: \\"quoted\\"" prints',
    "spread guided saves",
    'spread advanced: "quoted" saves',
    "true and null",
    "false and undefined",
    "first is filled in",
    "money costs $5",
    "1 and 2",
    "declared by a helper",
)
REPORTED = (
    [("the client", title) for title in IN_THE_CLIENT]
    + [
        (f"the client in {mode} mode", title)
        for mode in ("light", "dark")
        for title in ("renders", "renders 1 times", "renders 2 times")
    ]
    + [("", "at the top")]
)

#: Accepted by both.
ACCEPTED = (
    "caches a flag",
    "the client caches a flag",
    "doesn't retry",
    "a line — 'quoted' \\ kept",
    "plain template",
    "joined of three",
    "shown when on",
    "grows 10% to 100%%%% of users",  # the release reads %% as one %
    "formats %s as written",
    "%s: keeps a flag",
    "%p: keeps a flag",  # the release does not read a placeholder's letter
    "memory: keeps a flag",
    "the client disk: keeps a flag",
    "%s is %d, row %#, number %$, all 100%%",
    "one is 1, row 0, number 1, all 100%",
    "two is 2, row 1, number 2, all 100%%",
    "view %s saves",
    "view guided saves",
    'view advanced: "quoted" saves',
    "view %p prints",
    'view "guided" prints',
    "spread %s saves",
    "%s and %s",
    "true and null",
    "false and undefined",
    "%s costs $5",
    "money costs $5",
    "renders",
    "the client in %s mode renders",
    "the client in dark mode renders",
    "renders %d times",
    "renders 2 times",
    "the client in light mode renders 2 times",
    "the client in %s mode renders %d times",
    "the client in dark mode renders %d times",
    "at the top",
)
#: Refused by both.
REFUSED = (
    "the client",  # a describe block
    "in %s mode",
    "the client in dark mode",
    "caches a",  # the end cut
    "aches a flag",
    "caches a flag again",
    "keeps a flag",  # an .each title without its placeholder
    ": keeps a flag",
    "renders times",
    "only in a comment",
    "only in a block comment",
    "only in a string",
    "only in a template",
    "only in a regular expression",
    "stores a flag",  # renamed
    "client caches a flag",  # part of a full name
    "the client renders",
    "floppy: keeps a flag",  # not a row
    "in sepia mode renders",
    "Caches a flag",
    "grows 10% to 100%% of users",  # the release reads %% as one %
    'view "advanced: "quoted"" prints',  # jest escapes a quote for %p
    "$name is filled in",  # jest fills $name in
    "$a and $b",
)
#: The release accepts these; the reader cannot read the title, so refuses it.
RELEASE_ONLY = (
    "reads it('only in a string', () => {})",  # a template with ${}
    "declared by a helper",
    "spread guided saves",  # the rows are not literals
    'view "advanced: \\"quoted\\"" prints',  # %p of a quote
    "first is filled in",  # a row that is an object
    "1 and 2",  # a tagged template's row
    "one is %d, row %#, number %$, all 100%%",  # a row filled in by half
    "caches a %s",  # a placeholder no .each title has
    "%s",
)
#: The reader accepts these, but jest never ran them.
READER_ONLY = ("shown when off",)


def source(text: str, *, jsx: bool = False) -> Any:
    return jt.read(text, jsx=jsx)


def names(text: str = SOURCE, *, jsx: bool = False) -> Any:
    return jt.Names(source(text, jsx=jsx), rc.jest_title_pattern)


def by_the_release(name: str) -> Optional[str]:
    cases: List[Any] = [
        rc.Case(PATH, f"{suites} {title}".strip(), "passed", title)
        for suites, title in REPORTED
    ]
    return rc.target_problem(f"test:{PATH}::{name}", cases, "rc")


@pytest.mark.parametrize("name", ACCEPTED)
def test_a_whole_title_is_accepted_as_the_release_accepts_it(name):
    assert by_the_release(name) is None
    assert names().problem(name) is None


@pytest.mark.parametrize("name", REFUSED)
def test_what_is_not_a_whole_title_is_refused_as_the_release_refuses_it(name):
    assert by_the_release(name) is not None
    assert names().problem(name) is not None


@pytest.mark.parametrize("name", RELEASE_ONLY)
def test_a_title_that_cannot_be_read_is_refused(name):
    assert by_the_release(name) is None
    assert names().problem(name) is not None


@pytest.mark.parametrize("name", READER_ONLY)
def test_a_title_jest_does_not_reach_is_left_to_the_release(name):
    assert by_the_release(name) is not None
    assert names().problem(name) is None


@pytest.mark.parametrize("suites, title", REPORTED)
def test_the_reader_accepts_every_title_jest_reports_or_says_why(suites, title):
    """Nothing is missed in silence: a title it does not take is a known one."""
    if title in RELEASE_ONLY or title in (
        "grows 10% to 100%% of users",
        'spread advanced: "quoted" saves',
    ):
        assert names().problem(title) is not None
        return
    assert names().problem(title) is None
    assert names().problem(f"{suites} {title}".strip()) is None


def test_the_refusal_names_what_is_wrong():
    read = names()
    assert read.problem("the client") == (
        "no test is titled 'the client' (a describe block is: name a test in it);"
        " the titles at lines 14, 24, 36 and 1 more cannot be read"
    )
    assert "; one is titled 'caches a flag';" in read.problem("caches a")
    assert "; one is titled '%s: keeps a flag';" in read.problem("keeps a flag")
    assert "; one is titled 'spread %s saves';" in read.problem("spread guided saves")


def test_the_refusal_says_a_title_was_not_read_only_when_one_was_not():
    assert names("it('a', f);\n").problem("b") == "no test is titled 'b'"
    assert names("it('a', f);\nit(b, f);\n").problem("b") == (
        "no test is titled 'b'; the title at line 2 cannot be read"
    )


# ---------------------------------------------------------------------------
# what does not run
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "it.skip('a', () => {});",
        "test.skip('a', () => {});",
        "xit('a', () => {});",
        "xtest('a', () => {});",
        "it.todo('a');",
        "it.skip.each([1])('a', () => {});",
        "describe.skip('s', () => { it('a', () => {}); });",
        "xdescribe('s', () => { describe('t', () => { it('a', () => {}); }); });",
        "it('a', () => {});\nit.skip('a', () => {});",  # the release fails on one
    ],
)
def test_a_skipped_test_is_refused(text):
    assert names(text).problem("a") == (
        f"the test at line {2 if chr(10) in text else 1} is skipped"
    )


@pytest.mark.parametrize(
    "call", ["it.only", "fit", "test.only", "describe.only", "fdescribe"]
)
def test_a_file_with_only_is_refused(call):
    read = names(f"it('a', () => {{}});\n{call}('b', () => {{}});\n")
    assert read.problem("a") == "line 2 is .only: jest skips the other tests"


def test_a_modifier_keeps_its_test_and_a_describe_block_ends_where_it_closes():
    read = names(
        "describe.skip('s', () => { it('a', f); });\nit('b', f);\n"
        "test.concurrent('c', f);\nit.failing('d', f);\n"
    )
    assert [read.problem(name) for name in ("b", "c", "d")] == [None] * 3
    assert read.problem("s b") is not None


def test_a_decimal_row_is_named_as_written_only():
    read = names("it.each([[1.5], [1e3]])('%s e', f);\n")
    assert read.problem("%s e") is None
    assert read.problem("1e3 e") is not None


# ---------------------------------------------------------------------------
# the scanner
# ---------------------------------------------------------------------------
def test_a_byte_order_mark_is_white_space():
    assert names("\ufeffit('a', f);\n").problem("a") is None


TSX = """import React from 'react';
const label = <T extends unknown>(x: T): string => String(x);
const pair = <T,>(x: T) => x;
it('before', () => {
  render(
    <Panel title="it('in an attribute')" size={2} {...rest} /* a note */>
      it('in the text'), and don't close it
      {cond ? <b>isn't</b> : null}
      {/* it('in a comment') */}
      <><br/></>
      {list.map((x) => <Row key={x}>{`${x} it('in a template')`}</Row>)}
    </Panel>
  );
  expect(a < b && c > d).toBe(1 / 2 / 3 > x[0] / 4);
  expect(text.replace(/['"`/[\\]/]/g, "'")).toMatch(/don't/i);
  const typed = useState<Array<'x' | 'y'>>(['x']);
});
it(
  'after', // the last
  async () => {},
);
"""


def test_jsx_regular_expressions_and_generics_are_not_titles():
    read = source(TSX, jsx=True)
    assert [t.title.exact for t in read.tests] == [("before",), ("after",)]
    assert [t.title.line for t in read.tests] == [4, 18]


@pytest.mark.parametrize(
    "text",
    [
        "pattern.test('a');",
        "function it(a) { return a; }",
        "const suite = { it: 'a' };",
        "import { it } from 'x';",
        "test.describe('a', () => {});",
        "it.each([1]);",
        "await test.step('a', () => {});",
    ],
)
def test_what_is_not_a_call_of_a_test_is_not_one(text):
    assert source(text).tests == ()


def test_the_table_is_the_one_const_the_call_sees():
    """Declared twice in sight, or only out of sight, it is not read; the title
    as written still is."""
    text = (
        "import { C } from './c';\n"
        "const A = [['x']];\n"
        "describe('s', () => {\n  const A = [['y']];\n  it.each(A)('%s a', f);\n});\n"
        "describe('t', () => {\n  const B = [['z']] as const;\n"
        "  it.each(B)('%s b', f);\n});\n"
        "describe('u', () => {\n  const C = [['w']];\n});\n"
        "it.each(C)('%s c', f);\n"
    )
    read = source(text)
    assert [t.title.exact for t in read.tests] == [(), ("z b",), ()]
    assert [t.title.written for t in read.tests] == [("%s a",), ("%s b",), ("%s c",)]


@pytest.mark.parametrize(
    "table",
    [
        "[...A, ['x']]",
        "[['x', ...A], B]",
        "A.map((a) => [a])",
        "['x'].map((a) => [a])",
        "[f('x')]",
        "[[`${a}`]]",
        "[['x'], , ['y']]",
        "[]",
        "",
    ],
)
def test_the_rows_of_any_other_table_are_not_read(table):
    read = source(f"it.each({table})('%s a', f);\n")
    assert [t.title.written for t in read.tests] == [("%s a",)]
    assert [t.title.exact for t in read.tests] == [()]


@pytest.mark.parametrize(
    "text, refusal",
    [
        ("it('a', () => {});\nconst b = 'open;\n", "a string is not closed at line 2"),
        ("const a = `open;\n", "a template is not closed at line 1"),
        ("\n/* open\nit('a', f);\n", "a comment is not closed at line 2"),
        ("describe('a', () => {\n  it('b', f);\n", "a bracket is not closed at line 1"),
        ("it('a', f));\n", "a ')' closes nothing at line 1"),
        ("it('a', f]);\n", "a ']' closes nothing at line 1"),
        ("const a = `${b;\n", "a bracket is not closed at line 1"),
        ("x = " + "(`${" * 2000, "it nests too deeply"),
        (
            "it(" + "a ? 'x' : (" * 1200 + "'a'" + ")" * 1200 + ", f);\n",
            "it nests too deeply",
        ),
    ],
)
def test_a_file_that_does_not_scan_is_refused_whole(text, refusal):
    with pytest.raises(jt.Unreadable) as error:
        source(text)
    assert str(error.value) == refusal
