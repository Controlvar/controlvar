"""The homepage's quoted sentences still say what their docs pages say.

The Python reader of ``frontend/src/content/home-quotes.json`` (#1459 SPEC A,
which folds in #1415). Every homepage sentence that quotes a docs page is a row
of that table: its words (``parts``, with at most one link) and, for each page
it cites, the phrases both must contain; or a code block whose lines are lines
of the page it cites. Jest (``frontend/src/tests/content/home-quotes.test.tsx``)
also checks that the page renders each row word for word. Both readers check
the table against docs/ and mkdocs.yml, because a pull request that changes
only docs/ (or mkdocs.yml) runs no Jest: it runs the "Docs content tests"
step, which collects this directory. Without this reader, a docs-only change
that rewords a quoted phrase, drops "(beta)" from a linked page's title or
takes that page out of the nav merges green, and the next pull request that
runs the frontend suite goes red.

The rules are the Jest reader's (``frontend/src/tests/content/
home-quotes-rules.ts``), written twice. They are held together by behaviour,
not by text: both readers run every case of
``frontend/src/tests/fixtures/home-quotes-cases.json`` and must give its
refusals, and the fixture must have a case for every code in ``RULES``. A rule
relaxed in one reader fails that reader's case.

A missing page is a refusal, never a skip: on a docs-only change, "No docs
content test was skipped" fails a skip in this directory.

Needs no Node and no node_modules: it reads files only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
TABLE = "frontend/src/content/home-quotes.json"
FIXTURE = "frontend/src/tests/fixtures/home-quotes-cases.json"
#: The Jest file this twins. test_docs_only_gate.py finds every twin by this
#: name and holds the set to its JEST_TWINS, which pins each to the docs lane.
JEST_TWIN = "frontend/src/tests/content/home-quotes.test.tsx"

#: Every refusal, by code; the Jest reader's RULES. The fixture has a case for each.
RULES = (
    "keys",
    "type",
    "empty",
    "id",
    "duplicate-id",
    "page-name",
    "no-source",
    "links",
    "short-phrase",
    "not-in-quote",
    "no-page",
    "not-on-page",
    "not-in-nav",
    "no-title",
    "link-text",
    "says-beta",
    "omits-beta",
    "code-not-on-page",
)

#: A phrase this short proves little: "production data" is on many pages.
MIN_PHRASE = 20

# Matched with fullmatch(): Python's `$` also matches before a final newline,
# which JavaScript's does not, so "tool\n" would pass here and fail in Jest.
ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
PAGE = re.compile(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*")
TITLE = re.compile(r"^# (.+)$")
BETA_TITLE = re.compile(r"^(.+) \(beta\)$")
BETA_WORD = re.compile(r"\bbeta\b", re.I)

Read = Callable[[str], Optional[str]]
Problem = Tuple[str, str, str]  # (code, where, message)


def prose(s: str) -> str:
    """Code marks dropped, whitespace collapsed, case folded."""
    return re.sub(r"\s+", " ", s.replace("`", "")).lower()


def nav_entry(page: str) -> "re.Pattern[str]":
    """mkdocs.yml lists `page` in its nav as `- <label>: <page>.md`."""
    return re.compile(r"^\s+- [^:\n]+: " + re.escape(page) + r"\.md\s*$", re.M)


def _first_line(text: str) -> str:
    return re.split(r"\r?\n", text)[0]


def _is_object(value: Any) -> bool:
    return isinstance(value, dict)


def _same_keys(obj: Dict[str, Any], keys: List[str]) -> bool:
    return set(obj) == set(keys) and len(obj) == len(keys)


def problems(table: Any, read: Read) -> List[Problem]:
    """Every problem with `table`, reading pages and mkdocs.yml through `read`."""
    out: List[Problem] = []

    def add(code: str, where: str, message: str) -> None:
        out.append((code, where, message))

    if not _is_object(table) or not _same_keys(table, ["quotes"]):
        add("keys", "the table", 'the table is {"quotes": [...]} and nothing else')
        return out
    quotes = table["quotes"]
    if not isinstance(quotes, list):
        add("type", "quotes", "quotes is a list")
        return out
    if not quotes:
        add("empty", "quotes", "the table has no quotes")
        return out

    seen = set()
    for i, row in enumerate(quotes):
        where = f"quotes[{i}]"
        if not _is_object(row):
            add("type", where, "a quote is an object")
            continue
        is_code = "code" in row
        keys = ["id", "code", "sources"] if is_code else ["id", "parts", "sources"]
        if not _same_keys(row, keys):
            add("keys", where, f"a quote has exactly the keys {', '.join(keys)}")
            continue
        quote_id = row["id"]
        if not isinstance(quote_id, str) or not ID.fullmatch(quote_id):
            add(
                "id",
                where,
                f"the id {quote_id!r} is not lower-case words joined by hyphens",
            )
            continue
        if quote_id in seen:
            add("duplicate-id", quote_id, f"two quotes have the id {quote_id}")
            continue
        seen.add(quote_id)
        if is_code:
            _code_row(quote_id, row, read, add)
        else:
            _sentence_row(quote_id, row, read, add)
    return out


def _sentence_row(qid: str, row: Dict[str, Any], read: Read, add) -> None:
    parts = row["parts"]
    if not isinstance(parts, list):
        return add("type", qid, "parts is a list")
    if not parts:
        return add("empty", qid, "the quote has no parts")
    text = ""
    links: List[Tuple[str, str]] = []
    for part in parts:
        if isinstance(part, str):
            if part == "":
                return add("empty", qid, "a part is an empty string")
            text += part
        elif _is_object(part):
            if not _same_keys(part, ["link", "page"]):
                return add("keys", qid, 'a link is {"link", "page"} and nothing else')
            if not isinstance(part["link"], str) or not isinstance(part["page"], str):
                return add("type", qid, "a link's text and page are strings")
            if part["link"] == "":
                return add("empty", qid, "a link has no text")
            if not PAGE.fullmatch(part["page"]):
                return add(
                    "page-name", qid, f"{part['page']!r} is not a page under docs/"
                )
            links.append((part["link"], part["page"]))
            text += part["link"]
        else:
            return add("type", qid, "a part is a string or a link")
    if len(links) > 1:
        return add(
            "links", qid, f"{len(links)} links in one quote; a quote has at most one"
        )

    sources = row["sources"]
    if not isinstance(sources, list):
        return add("type", qid, "sources is a list")
    if not sources:
        return add("no-source", qid, "the quote cites no page")
    for source in sources:
        if not _is_object(source) or not _same_keys(source, ["page", "phrases"]):
            return add("keys", qid, 'a source is {"page", "phrases"} and nothing else')
        if not isinstance(source["page"], str) or not isinstance(
            source["phrases"], list
        ):
            return add(
                "type", qid, "a source's page is a string and its phrases a list"
            )
        if not PAGE.fullmatch(source["page"]):
            return add(
                "page-name", qid, f"{source['page']!r} is not a page under docs/"
            )
        if not source["phrases"]:
            return add("empty", qid, f"docs/{source['page']}.md is cited for no phrase")
        if not all(isinstance(p, str) for p in source["phrases"]):
            return add("type", qid, "a phrase is a string")

    said = prose(text)
    for source in sources:
        page = read(f"docs/{source['page']}.md")
        if page is None:
            add("no-page", qid, f"docs/{source['page']}.md does not exist")
        on_page = "" if page is None else prose(page)
        for phrase in source["phrases"]:
            p = prose(phrase)
            if len(p.strip()) < MIN_PHRASE:
                add("short-phrase", qid, f"{phrase!r} is under {MIN_PHRASE} characters")
                continue
            if p not in said:
                add("not-in-quote", qid, f"{phrase!r} is not in the quote")
            if page is not None and p not in on_page:
                add(
                    "not-on-page", qid, f"{phrase!r} is not on docs/{source['page']}.md"
                )

    titled_beta = False
    titles_read = True
    for link_text, link_page in links:
        page = read(f"docs/{link_page}.md")
        if page is None:
            add("no-page", qid, f"the link's page docs/{link_page}.md does not exist")
            titles_read = False
            continue
        nav = read("mkdocs.yml")
        if nav is None or not nav_entry(link_page).search(nav):
            add("not-in-nav", qid, f"mkdocs.yml's nav does not list {link_page}.md")
        title = TITLE.match(_first_line(page))
        if title is None:
            add("no-title", qid, f'docs/{link_page}.md does not open with "# <title>"')
            titles_read = False
            continue
        beta = BETA_TITLE.match(title.group(1))
        if beta is not None:
            titled_beta = True
        name = beta.group(1) if beta is not None else title.group(1)
        if prose(link_text) != prose(name):
            add(
                "link-text",
                qid,
                f"the link reads {link_text!r}; docs/{link_page}.md is titled "
                f"{title.group(1)!r}",
            )
    if not titles_read:
        return None
    says_beta = BETA_WORD.search(text) is not None
    if says_beta and not titled_beta:
        add(
            "says-beta",
            qid,
            'the quote says "beta", and no page it links is titled "(beta)"',
        )
    if titled_beta and not says_beta:
        add(
            "omits-beta",
            qid,
            'the page it links is titled "(beta)", and the quote does not say "beta"',
        )
    return None


def _code_row(qid: str, row: Dict[str, Any], read: Read, add) -> None:
    code = row["code"]
    if not isinstance(code, list):
        return add("type", qid, "code is a list")
    if not code:
        return add("empty", qid, "the code block has no lines")
    for line in code:
        if not isinstance(line, str):
            return add("type", qid, "a code line is a string")
        if line.strip() == "":
            return add("empty", qid, "a code line is blank")
    sources = row["sources"]
    if not isinstance(sources, list):
        return add("type", qid, "sources is a list")
    if not sources:
        return add("no-source", qid, "the code block cites no page")
    for source in sources:
        if not _is_object(source) or not _same_keys(source, ["page"]):
            return add("keys", qid, 'a code source is {"page"} and nothing else')
        if not isinstance(source["page"], str):
            return add("type", qid, "a source's page is a string")
        if not PAGE.fullmatch(source["page"]):
            return add(
                "page-name", qid, f"{source['page']!r} is not a page under docs/"
            )
    lines = set()
    missing = False
    for source in sources:
        page = read(f"docs/{source['page']}.md")
        if page is None:
            add("no-page", qid, f"docs/{source['page']}.md does not exist")
            missing = True
            continue
        lines.update(line.strip() for line in re.split(r"\r?\n", page))
    if missing:
        return None
    for line in code:
        if line.strip() not in lines:
            add("code-not-on-page", qid, f"{line!r} is not a line of the page it cites")
    return None


# ---------------------------------------------------------------------------
# The table against docs/
# ---------------------------------------------------------------------------
def _read_repo(rel: str) -> Optional[str]:
    path = REPO_ROOT / rel
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _load(rel: str) -> Any:
    text = _read_repo(rel)
    assert text is not None, f"{rel} does not exist"
    return json.loads(text)


QUOTES: List[Dict[str, Any]] = _load(TABLE)["quotes"]


def test_the_table_has_quotes_to_check():
    """An empty table cannot pass: two quotes and a link, as on the page today."""
    assert len(QUOTES) >= 2, QUOTES
    links = [p for row in QUOTES for p in row.get("parts", []) if isinstance(p, dict)]
    assert links, "no quote links a page, so the title and nav rules check nothing"


@pytest.mark.parametrize("row", QUOTES, ids=[row["id"] for row in QUOTES])
def test_says_only_what_the_cited_pages_say(row):
    found = problems({"quotes": [row]}, _read_repo)
    assert found == [], (
        f"The homepage quote {row['id']!r} ({TABLE}) and the docs pages it cites "
        "no longer agree. Change both together: the row, and the page under "
        "docs/ if that is what moved.\n" + "\n".join(f"{c}: {m}" for c, _, m in found)
    )


def test_the_whole_table_passes_every_id_distinct():
    assert problems(_load(TABLE), _read_repo) == []


# ---------------------------------------------------------------------------
# The rules, by the cases both readers run
# ---------------------------------------------------------------------------
FIXTURE_DATA: Dict[str, Any] = _load(FIXTURE)
CASES: List[Dict[str, Any]] = FIXTURE_DATA["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_shared_fixture(case):
    files: Dict[str, Optional[str]] = {**FIXTURE_DATA["files"], **case.get("files", {})}

    def read(rel: str) -> Optional[str]:
        value = files.get(rel)
        return value if isinstance(value, str) else None

    assert [code for code, _, _ in problems(case["table"], read)] == case["refusals"]


def test_the_fixture_has_a_case_for_every_rule_and_names_no_other():
    named = {code for case in CASES for code in case["refusals"]}
    assert sorted(named) == sorted(RULES)
    assert any(case["refusals"] == [] for case in CASES), "no case is accepted"
