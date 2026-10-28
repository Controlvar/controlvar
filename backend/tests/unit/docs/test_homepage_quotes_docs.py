"""The homepage's quoted sentences still say what their docs pages say.

A Python twin of the "claims quoted from the docs" block in
`frontend/src/tests/pages/index.test.tsx`. Two cards on the homepage
(`frontend/src/pages/index.tsx`) quote a docs page: the installer sentence on
"Deploy it yourself" and the revenue sentence on "Statistics beyond a
p-value". The Jest block holds them to their pages, but a pull request that
changes only docs/ (or mkdocs.yml) runs no Jest: it runs the Python "Docs
content tests" step, which collects this directory. Without this twin, a
docs-only change that rewords a quoted phrase, drops "(beta)" from the
installer page's title or takes the page out of the nav merged green and
turned the next pull request that runs the frontend suite red.

The rules are the Jest rules, copied, not reinterpreted:

* each quoted phrase is in the card AND on the page it cites, both compared as
  prose: markdown code marks dropped, whitespace collapsed, lower-cased (the
  Jest `prose()`);
* the installer page's first line is `# <title> (beta)`, the card's one link
  whose text says "with one command" has <title> as its text, and the card
  says "beta";
* that link's href is `docsUrl('self-hosting/aws-install')`, and mkdocs.yml's
  nav lists self-hosting/aws-install.md.

Jest renders the page and reads the card's text content (its heading, then its
body). This reads index.tsx as text. A card body is a string literal or a JSX
fragment of text, `{' '}` and `<a>` elements, with JSX text joined by Babel's
whitespace rule (`cleanJSXElementLiteralChild`). Anything else in a body is
refused with a message, never guessed at, so a body this reader cannot see
fails here rather than passing on nothing.

Not mirrored: the Jest test that the marketing build turns the link into the
docs.controlvar.com URL. It reads frontend/package.json and services/docs.ts,
and a change to either is never docs-only, so Jest runs on it.

`TestTheTwinStillMirrorsJest` reads the Jest file as text and fails when its
table or one of its rules moves without this file. A missing page is an error,
never a skip: on a docs-only change, "No docs content test was skipped" fails a
skip in this directory.

Needs no Node and no node_modules: it reads files only.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, NamedTuple, Tuple

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
HOMEPAGE = "frontend/src/pages/index.tsx"
JEST_TWIN = "frontend/src/tests/pages/index.test.tsx"
INSTALLER_PAGE = "self-hosting/aws-install"

#: The Jest block's `quoted` table, row for row: the card's title, the page it
#: cites (under docs/, without .md) and the phrases both must contain.
QUOTED: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    (
        "Deploy it yourself",
        INSTALLER_PAGE,
        (
            "install on AWS with one command",
            "installs the core profile in us-west-2",
            "as of v0.37.0 it has been run end to end once, in a test account",
            "it is not for production data",
        ),
    ),
    (
        "Statistics beyond a p-value",
        "getting-started/limitations",
        (
            "a revenue metric is analysed as its mean value per assigned user",
            "compared with Welch's t-test",
        ),
    ),
    (
        "Statistics beyond a p-value",
        "guides/user-guide",
        ("the difference per user with its confidence interval",),
    ),
)

#: The Jest `/^# (.+) \(beta\)$/`, applied to the installer page's first line.
BETA_TITLE = re.compile(r"^# (.+) \(beta\)$")
#: The Jest `/^\s+- [^:\n]+: self-hosting\/aws-install\.md\s*$/m`.
NAV_ENTRY = re.compile(r"^\s+- [^:\n]+: self-hosting/aws-install\.md\s*$", re.M)
#: The Jest `getByRole('link', { name: /with one command/i })`.
INSTALLER_LINK = re.compile(r"with one command", re.I)
#: The Jest `docsUrl(INSTALLER_PAGE)`, as the href is written in index.tsx
#: (whitespace removed).
INSTALLER_HREF = "{docsUrl('" + INSTALLER_PAGE + "')}"


class Refused(AssertionError):
    """index.tsx holds something this reader does not interpret."""


class Link(NamedTuple):
    href: str  # the attribute as written, whitespace removed
    text: str  # the text it renders


class Card(NamedTuple):
    text: str  # what Jest's card(title).textContent is: the title, then the body
    links: Tuple[Link, ...]


def _read(rel: str) -> str:
    path = REPO_ROOT / rel
    assert path.is_file(), f"{path} does not exist; the homepage cites it"
    return path.read_text(encoding="utf-8")


def _prose(s: str) -> str:
    """The Jest `prose()`: code marks dropped, whitespace collapsed, lower case."""
    return re.sub(r"\s+", " ", s.replace("`", "")).lower()


# ---------------------------------------------------------------------------
# Reading a card out of index.tsx
# ---------------------------------------------------------------------------
def _string_literal(src: str, start: int) -> Tuple[str, int]:
    """The JS string literal opening at src[start]: (its value, index after it).

    Only the escapes a card needs (a quote or a backslash) are read."""
    quote = src[start]
    if quote not in "'\"":
        raise Refused(f"not a string literal: {src[start : start + 40]!r}")
    out: List[str] = []
    i = start + 1
    while i < len(src):
        ch = src[i]
        if ch == "\\":
            nxt = src[i + 1 : i + 2]
            if nxt not in ("'", '"', "\\"):
                raise Refused(f"escape \\{nxt} in a card's string is not read here")
            out.append(nxt)
            i += 2
        elif ch == quote:
            return "".join(out), i + 1
        elif ch == "\n":
            break
        else:
            out.append(ch)
            i += 1
    raise Refused(f"unterminated string literal: {src[start : start + 40]!r}")


def _skip_quoted(src: str, i: int) -> int:
    """The index after the quoted run (', " or `) that opens at src[i]."""
    quote = src[i]
    i += 1
    while i < len(src):
        if src[i] == "\\":
            i += 2
            continue
        if src[i] == quote:
            return i + 1
        i += 1
    raise Refused(f"unterminated {quote} in index.tsx")


def _close_brace(src: str, start: int) -> int:
    """The index of the `}` that closes the `{` at src[start]."""
    depth = 0
    i = start
    while i < len(src):
        ch = src[i]
        if ch in "'\"`":
            i = _skip_quoted(src, i)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise Refused(f"unbalanced {{ at {src[start : start + 40]!r}")


def _jsx_text(raw: str) -> str:
    """The text a JSX text child renders: Babel's cleanJSXElementLiteralChild.

    Each line loses the spaces touching a line break, blank lines go, and the
    remaining lines are joined with one space."""
    if re.search(r"&[A-Za-z#][A-Za-z0-9]{0,30};", raw):
        raise Refused(
            f"an HTML entity in JSX text is not read here: {raw.strip()[:60]!r}"
        )
    if "}" in raw or ">" in raw:
        raise Refused(f"a stray brace or > in JSX text: {raw.strip()[:60]!r}")
    lines = re.split(r"\r\n|\n|\r", raw)
    last_non_empty = 0
    for i, line in enumerate(lines):
        if re.search(r"[^ \t]", line):
            last_non_empty = i
    out = ""
    for i, line in enumerate(lines):
        trimmed = line.replace("\t", " ")
        if i != 0:
            trimmed = trimmed.lstrip(" ")
        if i != len(lines) - 1:
            trimmed = trimmed.rstrip(" ")
        if trimmed:
            if i != last_non_empty:
                trimmed += " "
            out += trimmed
    return out


def _tag_end(src: str, start: int) -> int:
    """The index of the `>` that closes the opening tag at src[start]."""
    i = start
    while i < len(src):
        ch = src[i]
        if ch in "'\"`":
            i = _skip_quoted(src, i)
            continue
        if ch == "{":
            i = _close_brace(src, i) + 1
            continue
        if ch == ">":
            return i
        i += 1
    raise Refused(f"unterminated tag at {src[start : start + 40]!r}")


def _href(attributes: str) -> str:
    m = re.search(r"(?:^|\s)href=", attributes)
    if m is None:
        raise Refused(f"a link with no href: <a{attributes}>")
    i = m.end()
    if attributes[i : i + 1] == "{":
        value = attributes[i : _close_brace(attributes, i) + 1]
    elif attributes[i : i + 1] in ("'", '"'):
        value = attributes[i : _skip_quoted(attributes, i)]
    else:
        raise Refused(f"an href this reader cannot read: <a{attributes}>")
    return re.sub(r"\s+", "", value)


def _children(src: str, *, in_link: bool) -> Tuple[str, List[Link]]:
    """The text JSX children render, and the links among them."""
    text: List[str] = []
    links: List[Link] = []
    i = 0
    while i < len(src):
        ch = src[i]
        if ch == "{":
            end = _close_brace(src, i)
            expression = src[i + 1 : end].strip()
            if not (expression.startswith("/*") and expression.endswith("*/")):
                value, after = _string_literal(expression, 0)
                if after != len(expression):
                    raise Refused(
                        f"a JSX expression this reader cannot read: {{{expression}}}"
                    )
                text.append(value)
            i = end + 1
        elif ch == "<":
            if in_link or not re.match(r"<a\s", src[i:]):
                raise Refused(
                    f"only text, {{' '}} and <a> are read in a card: {src[i : i + 40]!r}"
                )
            open_end = _tag_end(src, i)
            attributes = src[i + 2 : open_end]
            if attributes.rstrip().endswith("/"):
                raise Refused(
                    f"a self-closing link has no text: {src[i : open_end + 1]!r}"
                )
            close = src.find("</a>", open_end)
            if close == -1:
                raise Refused(f"no </a> for {src[i : open_end + 1]!r}")
            inner, _ = _children(src[open_end + 1 : close], in_link=True)
            links.append(Link(_href(attributes), inner))
            text.append(inner)
            i = close + len("</a>")
        else:
            j = i
            while j < len(src) and src[j] not in "{<":
                j += 1
            text.append(_jsx_text(src[i:j]))
            i = j
    return "".join(text), links


def _body(src: str, start: int) -> Tuple[str, List[Link]]:
    """The body value that opens at src[start]: a string, or `(<>...</>)`."""
    if src[start] in "'\"":
        value, _ = _string_literal(src, start)
        return value, []
    m = re.compile(r"\(\s*<>").match(src, start)
    if m is None:
        raise Refused(
            f"a card body is a string or (<>...</>): {src[start : start + 40]!r}"
        )
    close = src.find("</>", m.end())
    if close == -1 or re.compile(r"</>\s*\)").match(src, close) is None:
        raise Refused(f"no </>) closes the body at {src[start : start + 40]!r}")
    if "<>" in src[m.end() : close]:
        raise Refused("a nested fragment in a card body is not read here")
    return _children(src[m.end() : close], in_link=False)


def _card(title: str) -> Card:
    """The card titled `title` in index.tsx, as Jest's card(title) reads it."""
    src = _read(HOMEPAGE)
    titles = [
        m for m in re.finditer(r"\btitle: (['\"])(.*?)\1,", src) if m.group(2) == title
    ]
    assert len(titles) == 1, f"{HOMEPAGE}: {len(titles)} cards titled {title!r}, not 1"
    after = titles[0].end()
    body = re.compile(r"\bbody:\s*").search(src, after)
    following = re.compile(r"\btitle: ").search(src, after)
    assert body is not None and (
        following is None or body.start() < following.start()
    ), f"{HOMEPAGE}: the card {title!r} has no body: before the next card"
    text, links = _body(src, body.end())
    return Card(title + text, tuple(links))


def _installer_link(card: Card) -> Link:
    named = [link for link in card.links if INSTALLER_LINK.search(link.text)]
    assert len(named) == 1, (
        f"{len(named)} links whose text matches /with one command/i on the card, not 1: "
        f"{card.links}"
    )
    return named[0]


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------
def test_the_reader_finds_both_cards_and_the_one_link():
    """An empty read cannot pass: each quoted card is found with its text, and
    the installer card has exactly the one link Jest's getByRole finds."""
    for title in sorted({row[0] for row in QUOTED}):
        assert len(_card(title).text) > len(title) + 100, title
    assert len(_card("Deploy it yourself").links) == 1


@pytest.mark.parametrize(
    "title, page, phrases", QUOTED, ids=[f"{t} <- docs/{p}.md" for t, p, _ in QUOTED]
)
def test_every_quoted_phrase_is_on_the_cited_page(title, page, phrases):
    said = _prose(_card(title).text)
    source = _prose(_read(f"docs/{page}.md"))
    missing = [
        f"{phrase!r} is not {where}"
        for phrase in phrases
        for where, text in (("on the card", said), (f"on docs/{page}.md", source))
        if _prose(phrase) not in text
    ]
    assert missing == [], (
        f"The homepage card {title!r} quotes docs/{page}.md, and the two no longer "
        "agree. Change both together (the card in frontend/src/pages/index.tsx, "
        "the page under docs/), then the phrase in this table and in "
        f"{JEST_TWIN}:\n" + "\n".join(missing)
    )


def test_calls_the_installer_a_beta_as_its_page_title_does():
    title = _read(f"docs/{INSTALLER_PAGE}.md").split("\n")[0]
    named = BETA_TITLE.match(title)
    assert named is not None, (
        f"docs/{INSTALLER_PAGE}.md's title is {title!r}: it no longer ends "
        '"(beta)", and the homepage calls the installer a beta'
    )
    deploy = _card("Deploy it yourself")
    link = _installer_link(deploy)
    assert _prose(link.text) == _prose(named.group(1)), (
        f"the homepage link reads {link.text!r}; the page it links is titled "
        f"{named.group(1)!r}"
    )
    assert re.search(r"\bbeta\b", deploy.text, re.I), "the card no longer says beta"


def test_links_the_installer_sentence_to_the_installer_page_in_the_nav():
    link = _installer_link(_card("Deploy it yourself"))
    assert link.href == INSTALLER_HREF, (
        f"the installer link's href is {link.href!r}, not {INSTALLER_HREF!r}"
    )
    assert NAV_ENTRY.search(_read("mkdocs.yml")), (
        f"mkdocs.yml's nav no longer lists {INSTALLER_PAGE}.md, which the "
        "homepage links"
    )


# ---------------------------------------------------------------------------
# The twin still mirrors Jest
# ---------------------------------------------------------------------------
def _jest_table(text: str) -> List[str]:
    """The Jest `quoted` table's string literals in order, INSTALLER_PAGE resolved."""
    start = text.find("const quoted: Array<[string, string, string[]]> = [")
    assert start != -1, f"{JEST_TWIN} no longer has its `quoted` table"
    end = text.find("\n  ];\n", start)
    assert end != -1, f"{JEST_TWIN}: the `quoted` table does not close as expected"
    block = text[start:end].split("= [", 1)[1]
    tokens: List[str] = []
    # A literal is single-quoted, or double-quoted when it holds an apostrophe.
    literal = r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\""
    for m in re.finditer(literal + r"|\b(INSTALLER_PAGE)\b|(\w+)", block):
        if m.group(4) is not None:
            raise AssertionError(f"{JEST_TWIN}: the table names {m.group(4)!r}")
        if m.group(3):
            tokens.append(INSTALLER_PAGE)
        else:
            tokens.append(m.group(1) if m.group(1) is not None else m.group(2))
    return tokens


class TestTheTwinStillMirrorsJest:
    """The Jest block still states the table and the rules this file copies."""

    MUST_CONTAIN = (
        f"const INSTALLER_PAGE = '{INSTALLER_PAGE}';",
        "const prose = (s: string): string => "
        r"s.replace(/`/g, '').replace(/\s+/g, ' ').toLowerCase();",
        "return heading.parentElement as HTMLElement;",
        "const said = prose(card(title).textContent ?? '');",
        "const source = prose(read(`docs/${page}.md`));",
        "onTheCard: said.includes(prose(phrase))",
        "onThePage: source.includes(prose(phrase))",
        r"const title = read(`docs/${INSTALLER_PAGE}.md`).split('\n')[0];",
        f"const named = /{BETA_TITLE.pattern}/.exec(title);",
        # Both of the block's link lookups, whole: the name appears twice, so a
        # bare fragment would still match with one of them changed.
        "const link = within(deploy).getByRole('link', "
        f"{{ name: /{INSTALLER_LINK.pattern}/i }});",
        "const link = within(card('Deploy it yourself')).getByRole('link', "
        f"{{ name: /{INSTALLER_LINK.pattern}/i }});",
        "expect(prose(link.textContent ?? '')).toBe(prose(named![1]));",
        r"expect(deploy.textContent).toMatch(/\bbeta\b/i);",
        "expect(link.getAttribute('href')).toBe(docsUrl(INSTALLER_PAGE));",
        "expect(read('mkdocs.yml')).toMatch(/"
        + NAV_ENTRY.pattern.replace("/", "\\/")
        + "/m);",
    )

    @pytest.mark.parametrize("fragment", MUST_CONTAIN)
    def test_jest_rule_is_unchanged(self, fragment):
        assert fragment in _read(JEST_TWIN), (
            f"{JEST_TWIN} no longer contains {fragment!r}. The homepage's quoted "
            f"claims are pinned twice, there and in {Path(__file__).name}; "
            "change both together, then update this pin."
        )

    def test_the_table_is_the_jest_table(self):
        mine: List[str] = []
        for title, page, phrases in QUOTED:
            mine.extend([title, page, *phrases])
        assert _jest_table(_read(JEST_TWIN)) == mine, (
            f"the `quoted` table in {JEST_TWIN} and QUOTED here differ; they are "
            "one table written twice"
        )
