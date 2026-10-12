#!/usr/bin/env python3
"""Every unit of a registered document names what verifies it (#1142).

A *unit* is a heading, a sentence, a list item's sentence, a table row or an
admonition title of a user-facing document. Each in-scope document has a
registry, ``docs/_claims/<document>.toml``, with one entry per distinct unit
saying what verifies it, or why it is not a product claim. ``check`` refuses a
page and its registry that disagree, a target that does not resolve, and debt
that is not in the ledger, so a sentence cannot be edited away from the test
that holds it without the registry following in the same pull request.

THE SPLITTER (``units``; pure, so the same text gives the same units)

1. Dropped: YAML front matter; fenced blocks (``` or ~~~, three or more, at any
   indentation, closed by a line of the same character at least as long: a
   four-backtick fence holding a three-backtick line is one fence); HTML
   comments, inline or over several lines; a link reference definition (a
   line that is only ``[name]: destination``, with an optional quoted title,
   which does not render: ``[Note]: Some text.`` renders, so it is a unit);
   thematic breaks. An unclosed fence or comment is an error, not a guess.
2. A heading is one unit: a line starting with ``#`` in the first column (the
   renderer's rule, space or no space), its ``#`` marks and a trailing
   ``{...}`` attribute list removed; or a setext heading, the line above a
   ``===`` or ``---`` underline (a ``---`` after a blank line is a break).
3. An admonition, collapsible or tab opener (``!!!``, ``???``, ``???+``,
   ``===``) contributes its quoted title, if any, as one unit; its indented body
   is read like any other text.
4. A table is a line with an unescaped ``|`` followed by a separator row. The
   header row and each body row is one unit: cells split at unescaped pipes,
   ``\\|`` read as ``|``, each cell normalised, joined with `` | ``. The
   separator row is dropped; the table ends at a blank line or a line with no
   pipe.
5. Other text is joined line to line into blocks. A blank line ends a block; a
   list marker (``-``, ``*``, ``+``, ``1.``, ``1)``), a heading, an opener, a
   table, a line starting with ``<`` (a raw HTML block, whose tags are removed)
   or a change of blockquote depth starts a new one. Each block is normalised
   and split after ``.``, ``!`` or ``?`` followed by a space.
6. Normalised: ``[text](url)``, ``![text](src)`` and ``[text][ref]`` become
   ``text``; ``<https://...>`` and ``<name@host>`` the address; backslash
   escapes resolved; backticks and ``*`` removed; whitespace collapsed. So a
   markup-only edit (backticks, bold, a link added) is not an edit, and
   rewrapping is not either.
8. Linear time: every pattern is anchored or bounded with no two quantifiers
   competing for the same characters, and the rest is plain string methods, so
   a pathological page of megabytes is split in seconds
   (``test_a_pathological_page_is_split_in_linear_time`` holds it).
7. A unit with no letter or digit is dropped. Nothing else is: no keyword or
   shape decides that a unit is not a claim; the registry does.

Indented code blocks are not looked for: Doc Examples refuses one on every page
of its walk (``scripts/doc_examples.py --check``).

THE REGISTRY

    document = "docs/getting-started/quick-start.md"

    [[unit]]
    text = "<the unit, normalised>"
    by = ["test:backend/tests/unit/core/test_x.py::test_y"]
    count = 2            # only when the unit occurs more than once on the page

The registry path is ``docs/_claims/`` + the document's repository path + ``.toml``
(a surface's ``#`` written ``.``). The units on the page equal the entries,
each as many times as ``count`` says (1 when absent). An entry's ``by`` is one
or more verifying targets, or exactly one ``exempt:`` or ``untested:`` target.

THE TARGETS (a closed list; anything else is refused)

``test:<path>::<name>`` or ``test:<path>::<Class>::<name>``
    a test file (``test_*.py``, ``*_test.go``, ``*.test.ts`` and the other
    SDKs' shapes), never under ``backend/app/``, ``modules/backend/app/`` or
    ``scripts/``, and never a test that reads documentation: one the docs-only
    gate lists (``DOCS_TESTS``, ``NOT_DOCS_TESTS`` in ``DOCS_GATE``) or whose
    file matches that gate's ``DOCS_READER``. Such a test is reached only
    through ``mechanism:``, which says which units it holds. In Python the function, or the
    class and method, is found by parsing the file, its name starts with
    ``test`` (a class with ``Test``), and one marked ``benchmark`` is refused
    (skipped unless RUN_BENCHMARKS=1, so it would hold nothing). In another
    language the file contains the name. ``test:<path>`` alone (the whole file)
    is allowed and counted separately. A ``modules/`` target in a tree without
    ``modules/`` (``scripts/core_build.sh``'s copy) is checked for its form only,
    and counted.
``journey:<slug>#<step>``
    ``tests/acceptance/docs/journeys/<slug>.yaml`` exists and has that step.
``docex:<document>#<expected text>``
    Doc Examples enrols the document, and exactly one of its exec blocks is
    followed by ``<!-- expect: <expected text> -->``: the target names its
    block, and a block that stopped running loses its expectation.
``workflow:<name>.yml``
    the document's inventory class is ``workflow`` and names that workflow
    (``test_docs_inventory.py`` holds the workflow to the document's path).
``mechanism:<name>``
    one of ``MECHANISMS``: a test that reads the page and compares it with the
    product. A mechanism that *quotes* holds only the units its file quotes word
    for word (after normalising); any other holds only the units that match one
    of its ``holds`` patterns, on the documents that pattern names, and each
    pattern must match a unit somewhere, or it holds nothing.
``evidence:<GitHub run or issue comment URL>``
    only on a document of class ``aws``, and the unit is dated (below).
``exempt:<category>``
    not a product claim: instruction, advice, navigation, theory, example,
    third-party, label, intent, policy. ``intent`` is dated, and only on a
    document of class ``aws`` or on a unit the entry tags ``tag = "X"`` (one
    the audit could not verify); ``policy`` only in ``POLICY_DOCUMENTS``.
``untested:<T|S|X|A>`` and ``exempt:baseline``
    debt and not-yet-classified units: each has a line in the ledger.

*Dated* means the unit says ``as of vX.Y.Z`` or ``on YYYY-MM-DD`` in those
words (the first letter may be a capital).

THE LEDGER

``docs/_claims/_frozen.tsv``: ``kind<TAB>document<TAB>sha256(unit)[:12]``, one
sorted line per ``untested:*`` and ``exempt:baseline`` entry, both ways: an
entry with no line, and a line with no entry, are refused. Hashes, not counts,
so swapping one debt sentence for another is refused. That the ledger only
shrinks across pull requests needs history; review holds it.

PENDING AND DEFERRED

Every in-scope document (``tests/acceptance/docs/inventory.toml``) that has an
extractor has a registry, or is in ``CLAIMS_PENDING`` (the rollout: it only
shrinks) or ``CLAIMS_DEFERRED`` (the mobile and not-yet-published SDK pages,
#1107; it moves only with a founder decision), never two of these. A document
a journey holds (no extractor) has none. This file reads Markdown only; a
surface stays pending until its extractor is added here.

RUN

    python scripts/doc_claims.py check            every problem, then the counts
    python scripts/doc_claims.py report           the counts
    python scripts/doc_claims.py units DOC ...    a registry skeleton per document
    python scripts/doc_claims.py units --all      the same for every Markdown document

Standard library only; imports nothing from ``backend`` or ``modules`` and
runs no git, so it gives the same answer in ``scripts/core_build.sh``'s copy.
"""

from __future__ import annotations

import argparse
import ast
import collections
import dataclasses
import datetime
import fnmatch
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import sys
import tomllib
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLAIMS_DIR = "docs/_claims"
LEDGER = "docs/_claims/_frozen.tsv"
INVENTORY = "tests/acceptance/docs/inventory.toml"
JOURNEYS = "tests/acceptance/docs/journeys"
ENROLMENT = "scripts/doc_examples.toml"
#: Doc Examples' own fence and tag parser, from this checkout: one parser.
DOC_EXAMPLES = pathlib.Path(__file__).resolve().parent / "doc_examples.py"
MARKDOWN = "markdown"
HASH_LENGTH = 12


# ---------------------------------------------------------------------------
# The splitter
# ---------------------------------------------------------------------------
class SplitError(ValueError):
    """A document the splitter will not guess about (an unclosed fence)."""


@dataclasses.dataclass(frozen=True)
class Unit:
    text: str
    line: int  # 1-based line where the unit's block starts


_FENCE = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>`{3,}|~{3,})(?P<info>.*)$")
_LIST = re.compile(r"^(?:[-*+]|[0-9]{1,9}[.)])(?:[ \t]+|$)")
_OPENER = re.compile(r"^(?:!!!|\?\?\?\+?|===)(?:[ \t]+(?P<rest>.*))?$")
_QUOTE = re.compile(r"(?:>[ \t]*)*")
_SEPARATOR = re.compile(
    r"^\|?[ \t]*:?-{1,200}:?[ \t]*(?:\|[ \t]*:?-{1,200}:?[ \t]*){0,100}\|?$"
)
_PIPE = re.compile(r"(?<!\\)\|")
#: A link reference definition: only a destination and an optional title.
_REFERENCE = re.compile(
    r"\[[^\[\]\n]{1,200}\]:[ \t]{0,40}(?:<[^<>\n]{0,400}>|[^\s<]{1,400})"
    r"(?:[ \t]{1,40}(?:\"[^\"\n]{0,300}\"|'[^'\n]{0,300}'|\([^()\n]{0,300}\)))?"
    r"[ \t]{0,40}"
)
_HTML_START = re.compile(r"^<[A-Za-z/!?]")
_TAG = re.compile(r"<[^<>\n]{0,500}>")
_IMAGE = re.compile(r"!\[([^\[\]\n]{0,200})\]\([^()\n]{0,400}\)")
_LINK = re.compile(r"\[([^\[\]\n]{0,200})\]\([^()\n]{0,400}\)")
_REF_LINK = re.compile(r"\[([^\[\]\n]{1,200})\]\[[^\[\]\n]{0,200}\]")
_AUTOLINK = re.compile(r"<((?:https?|mailto):[^<>\s]{1,400})>")
_EMAIL = re.compile(r"<([^<>\s@]{1,200}@[^<>\s@]{1,200})>")
_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|<>~])")
_SENTENCE_END = re.compile(r"(?<=[.!?]) ")
_WORD = re.compile(r"\w")
FRONT_MATTER_LINES = 200


def normalise(text: str) -> str:
    """The text as the registry keys it: markup and wrapping do not count."""
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _REF_LINK.sub(r"\1", text)
    text = _AUTOLINK.sub(r"\1", text)
    text = _EMAIL.sub(r"\1", text)
    text = _ESCAPE.sub(r"\1", text)
    text = text.replace("`", "").replace("*", "")
    return " ".join(text.split())


def _strip_comments(line: str) -> Tuple[str, bool]:
    """The line without its HTML comments, and whether one is still open."""
    out = []
    position = 0
    while True:
        start = line.find("<!--", position)
        if start < 0:
            out.append(line[position:])
            return "".join(out), False
        out.append(line[position:start])
        end = line.find("-->", start + 4)
        if end < 0:
            return "".join(out), True
        position = end + 3


def visible_lines(text: str) -> List[str]:
    """Each line of *text*, "" where front matter, a fence or a comment was."""
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = [""] * len(raw)
    start = 0
    if raw and raw[0].strip() == "---":
        for number in range(1, min(len(raw), FRONT_MATTER_LINES)):
            if raw[number].strip() in ("---", "..."):
                start = number + 1
                break
    fence: Optional[str] = None
    fence_line = 0
    comment_line = 0
    in_comment = False
    for number in range(start, len(raw)):
        line = raw[number]
        if fence is not None:
            closer = line.strip()
            if closer and set(closer) == {fence[0]} and len(closer) >= len(fence):
                fence = None
            continue
        if in_comment:
            end = line.find("-->")
            if end < 0:
                continue
            in_comment = False
            line = line[end + 3 :]
        else:
            opener = _FENCE.match(line)
            if opener and not (
                opener.group("marker")[0] == "`" and "`" in opener.group("info")
            ):
                fence, fence_line = opener.group("marker"), number + 1
                continue
        line, in_comment = _strip_comments(line)
        if in_comment:
            comment_line = number + 1
        out[number] = line
    if fence is not None:
        raise SplitError(f"line {fence_line}: a fence ({fence}) that never closes")
    if in_comment:
        raise SplitError(f"line {comment_line}: an HTML comment that never closes")
    return out


@dataclasses.dataclass
class _Block:
    kind: str  # "para", "list", "html": joinable; "heading", "row": one unit
    line: int
    depth: int
    parts: List[Tuple[int, str]]


def _quote(line: str) -> Tuple[int, str]:
    """The blockquote depth of a stripped line, and the text inside it."""
    prefix = _QUOTE.match(line).group()
    return prefix.count(">"), line[len(prefix) :]


def _row(text: str) -> str:
    cells = _PIPE.split(text.strip())
    if cells and not cells[0].strip():
        cells = cells[1:]
    if cells and not cells[-1].strip():
        cells = cells[:-1]
    return " | ".join(normalise(cell.replace("\\|", "|")) for cell in cells)


def _heading(text: str) -> str:
    """A heading's text: its # marks, closing #s and {...} attribute list removed."""
    text = text.lstrip("#").strip()
    if text.endswith("}"):
        start = text.rfind("{")
        if start >= 0 and "}" not in text[start:-1]:
            text = text[:start].rstrip()
    bare = text.rstrip("#")
    if bare != text and (not bare or bare[-1] in " \t"):
        text = bare.rstrip()
    return normalise(text)


def _title(rest: str) -> Optional[str]:
    """An opener's quoted title, or None."""
    first = rest.find('"')
    last = rest.find('"', first + 1) if first >= 0 else -1
    return rest[first + 1 : last] if last > first else None


def _underline(body: str) -> bool:
    line = body.rstrip(" \t")
    return bool(line) and (not line.strip("=") or not line.strip("-"))


def _is_break(body: str) -> bool:
    compact = body.replace(" ", "").replace("\t", "")
    return len(compact) >= 3 and compact[0] in "*-_" and not compact.strip(compact[0])


def _blocks(lines: Sequence[str]) -> List[_Block]:
    blocks: List[_Block] = []
    current: Optional[_Block] = None
    in_table = False
    index = 0
    while index < len(lines):
        raw, number = lines[index], index + 1
        index += 1
        depth, body = _quote(raw.strip())
        if not body:
            current, in_table = None, False
            continue
        if in_table:
            if _PIPE.search(body):
                blocks.append(_Block("row", number, depth, [(number, _row(body))]))
                continue
            in_table = False
        if body.startswith("#") and (raw.startswith("#") or depth):
            blocks.append(_Block("heading", number, depth, [(number, _heading(body))]))
            current = None
            continue
        opener = _OPENER.match(body)
        if opener:
            title = _title(opener.group("rest") or "")
            if title is not None:
                blocks.append(
                    _Block("heading", number, depth, [(number, normalise(title))])
                )
            current = None
            continue
        if _REFERENCE.fullmatch(body):
            current = None
            continue
        if (
            _underline(body)
            and current is not None
            and current.kind == "para"
            and current.depth == depth
        ):
            heading_line, heading_text = current.parts.pop()
            if not current.parts:
                assert blocks[-1] is current, "an open block is the last one"
                blocks.pop()
            blocks.append(
                _Block(
                    "heading",
                    heading_line,
                    depth,
                    [(heading_line, _heading(heading_text))],
                )
            )
            current = None
            continue
        if _is_break(body):
            current = None
            continue
        if _PIPE.search(body) and index < len(lines):
            separator = _quote(lines[index].strip())[1]
            if "|" in separator and _SEPARATOR.match(separator):
                blocks.append(_Block("row", number, depth, [(number, _row(body))]))
                index += 1
                in_table, current = True, None
                continue
        marker = _LIST.match(body)
        if marker:
            current = _Block("list", number, depth, [(number, body[marker.end() :])])
            blocks.append(current)
            continue
        if _HTML_START.match(body):
            current = _Block("html", number, depth, [(number, body)])
            blocks.append(current)
            continue
        if current is not None and current.depth == depth:
            current.parts.append((number, body))
            continue
        current = _Block("para", number, depth, [(number, body)])
        blocks.append(current)
    return blocks


def units(text: str) -> List[Unit]:
    """The units of a Markdown document, in page order (rules in the docstring)."""
    found: List[Unit] = []
    for block in _blocks(visible_lines(text)):
        if block.kind in ("heading", "row"):
            pieces = [block.parts[0][1]]
        else:
            joined = " ".join(part for _, part in block.parts)
            if block.kind == "html":
                joined = _TAG.sub(" ", joined)
            pieces = _SENTENCE_END.split(normalise(joined))
        for piece in pieces:
            piece = " ".join(piece.split())
            if _WORD.search(piece):
                found.append(Unit(piece, block.line))
    return found


def unit_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:HASH_LENGTH]


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------
EXEMPT_CATEGORIES: Tuple[str, ...] = (
    "instruction",
    "advice",
    "navigation",
    "theory",
    "example",
    "third-party",
    "label",
    "intent",
    "policy",
)
#: Where a policy statement (what the project does, not what the product does)
#: may stand as exempt:policy, and nowhere else.
POLICY_DOCUMENTS: Tuple[str, ...] = (
    "SECURITY.md",
    "docs/api/stability.md",
    "docs/security/incident-response.md",
)
UNTESTED_KINDS: Tuple[str, ...] = ("T", "S", "X", "A")
LEDGER_KINDS: Tuple[str, ...] = tuple(f"untested:{k}" for k in UNTESTED_KINDS) + (
    "exempt:baseline",
)
#: Verifying target kinds, in report order.
VERIFYING: Tuple[str, ...] = (
    "test",
    "test-file",
    "journey",
    "docex",
    "workflow",
    "mechanism",
    "evidence",
)
DATED = re.compile(
    r"\b[Aa]s of v[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}\b"
    r"|\b[Oo]n (?P<y>[0-9]{4})-(?P<m>[0-9]{2})-(?P<d>[0-9]{2})\b"
)
EVIDENCE = re.compile(
    r"https://github\.com/getexperimently/experimently/"
    r"(?:actions/runs/[0-9]{1,12}|issues/[0-9]{1,7}#issuecomment-[0-9]{1,12})"
)
SLUG = r"[a-z0-9]{1,60}(?:-[a-z0-9]{1,60}){0,20}"
PYTHON_NAME = r"[A-Za-z_][A-Za-z0-9_]{0,200}"
#: Test files, by name, in every language the repository tests in.
TEST_FILE = re.compile(
    r"(?:^|/)(?:test_[^/]{1,200}\.(?:py|php)"
    r"|[^/]{1,200}_test\.(?:py|go|dart|exs)"
    r"|[^/]{1,200}\.(?:test|spec)\.(?:[cm]?[jt]sx?)"
    r"|[^/]{1,200}Tests?\.(?:java|kt|swift|php|cs)"
    r"|[^/]{1,200}_spec\.rb)$"
)
NOT_TESTS: Tuple[str, ...] = ("backend/app/", "modules/backend/app/", "scripts/")
#: Tests that read a page's wording. A claim reaches one only as a mechanism.
WORDING_TESTS = "backend/tests/unit/docs/"
#: The docs-only gate, whose DOCS_TESTS and NOT_DOCS_TESTS list every test that
#: reads documentation, and its detector of one (held equal to it there).
DOCS_GATE = "backend/tests/unit/infrastructure/test_docs_only_gate.py"
DOCS_READER = re.compile(r"""["']docs["']|["']docs/|mkdocs\.yml""")


@dataclasses.dataclass(frozen=True)
class Mechanism:
    """A test that reads documents and compares them with the product.

    ``quotes``: it holds the units its file quotes word for word (normalised).
    Otherwise ``holds``: (document ``fnmatch`` pattern, unit pattern) pairs, the
    sentences the test checks; it holds a unit of a matching document that the
    unit pattern finds (``re.search``), and nothing else.
    """

    test: str
    holds: Tuple[Tuple[str, str], ...] = ()
    quotes: bool = False


_VERSION = r"\(?v?[0-9]{1,4}(?:\.[0-9]{1,4}){1,2}"
_TARBALL = r"getexperimently-[A-Za-z0-9-]{1,60}-[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}\.tgz"


MECHANISMS: Dict[str, Mechanism] = {
    # The table's rows: capability | Dashboard or API only | guide.
    "dashboard-and-api": Mechanism(
        "backend/tests/unit/docs/test_dashboard_and_api.py"
        "::test_the_table_and_the_dashboard_agree",
        holds=(
            (
                "docs/guides/dashboard-and-api.md",
                r"^[^|]{1,300} \| (?:Dashboard|API only|Where you do it) \| [^|]{1,300}$",
            ),
        ),
    ),
    "integrations-claims": Mechanism(
        "backend/tests/unit/docs/test_integrations_docs_claims.py"
        "::test_every_claim_about_what_the_platform_does_is_on_the_list",
        quotes=True,
    ),
    # A quoted package version (a bare `experimently` only on the Python page).
    "sdk-doc-versions": Mechanism(
        "backend/tests/unit/docs/test_sdk_doc_versions.py"
        "::test_quoted_sdk_versions_match_the_manifests",
        holds=(
            (
                "docs/sdk/*.md",
                r"(?:@getexperimently/[A-Za-z0-9_-]{1,60}|experimently-openfeature) "
                + _VERSION,
            ),
            ("docs/sdk/*.md", _TARBALL),
            ("docs/sdk-guide.md", _TARBALL),
            ("docs/sdk/python.md", r"(?<![\w@/-])experimently " + _VERSION),
        ),
    ),
    # The one sentence per SDK page about the live contract job.
    "sdk-live-contract-sentence": Mechanism(
        "backend/tests/unit/docs/test_sdk_live_contract_sentence.py"
        "::test_every_sdk_page_says_what_ci_runs_and_agrees_with_the_gate",
        holds=(
            (
                "docs/sdk/*.md",
                r"^CI (?:runs|does not run) this SDK's contract smoke against a real"
                r" API: [a-z0-9-]{1,40} (?:is|is not) in the sdks: list of the SDK Live"
                r" Contract job in pr-qa-gate\.yml\.$",
            ),
        ),
    ),
    # Which warehouses the page says are available.
    "warehouse-availability": Mechanism(
        "modules/backend/tests/unit/test_warehouse_docs_availability.py"
        "::test_the_page_says_a_warehouse_is_available_exactly_while_it_is",
        holds=(
            (
                "docs/api/warehouse-analytics.md",
                r"\b(?:BigQuery|Snowflake|Amazon Athena) is available\b",
            ),
        ),
    ),
}

#: The mobile and not-yet-published SDK pages (#1107): their registries wait
#: for that issue. This list moves only with a founder decision.
CLAIMS_DEFERRED: Tuple[str, ...] = (
    "docs/sdk/android.md",
    "docs/sdk/dotnet.md",
    "docs/sdk/edge.md",
    "docs/sdk/elixir.md",
    "docs/sdk/flutter.md",
    "docs/sdk/ios.md",
    "docs/sdk/java.md",
    "docs/sdk/openfeature.md",
    "docs/sdk/php.md",
    "docs/sdk/react-native.md",
    "docs/sdk/react.md",
    "docs/sdk/ruby.md",
    "sdk/android/README.md",
    "sdk/android/examples/ComposeExample/README.md",
    "sdk/dotnet/README.md",
    "sdk/edge/README.md",
    "sdk/elixir/README.md",
    "sdk/flutter/README.md",
    "sdk/ios/README.md",
    "sdk/java/README.md",
    "sdk/openfeature/README.md",
    "sdk/php/README.md",
    "sdk/react/README.md",
    "sdk/ruby/README.md",
)

#: In-scope documents not registered yet. The rollout: it only shrinks, and a
#: document leaves it in the pull request that registers it.
CLAIMS_PENDING: Tuple[str, ...] = (
    "README.md",
    "SECURITY.md",
    "charts/experimently/templates/NOTES.txt",
    "demo/DEMO_GUIDE.md",
    "demo/shoplab/README.md",
    "demo/streampulse/README.md",
    "demo/streampulse/simulator/README.md",
    "deploy/compose/compose.yml#header",
    "deploy/compose/generate-env.sh#header",
    "docs/Enhanced_Rules_Engine_Reference.md",
    "docs/README.md",
    "docs/api/README.md",
    "docs/api/alerting.md",
    "docs/api/api-docs-guide.md",
    "docs/api/audit-logging.md",
    "docs/api/auth.md",
    "docs/api/bayesian.md",
    "docs/api/compliance.md",
    "docs/api/cuped.md",
    "docs/api/data-export.md",
    "docs/api/dimensional-analysis.md",
    "docs/api/endpoints.md",
    "docs/api/integrations.md",
    "docs/api/interaction-detection.md",
    "docs/api/multi-armed-bandit.md",
    "docs/api/mutual-exclusion-groups.md",
    "docs/api/rbac.md",
    "docs/api/sequential-testing.md",
    "docs/api/split-url.md",
    "docs/api/stability.md",
    "docs/api/warehouse-analytics.md",
    "docs/architecture/README.md",
    "docs/architecture/models.md",
    "docs/architecture/overview.md",
    "docs/architecture/technical-guide.md",
    "docs/auth/README.md",
    "docs/auth/auth-user-guide.md",
    "docs/auth/cognito-auth-testing.md",
    "docs/auth/flow.md",
    "docs/auth/sso.md",
    "docs/cognito_integration.md",
    "docs/deployment/README.md",
    "docs/deployment/deployment-guide.md",
    "docs/deployment/disaster-recovery.md",
    "docs/deployment/iam-permissions.md",
    "docs/deployment/rollback-runbook.md",
    "docs/deployment/secrets-management.md",
    "docs/development/database/backup.md",
    "docs/development/database/migrations.md",
    "docs/development/database/usage.md",
    "docs/development/dependency-injection.md",
    "docs/development/doc-examples.md",
    "docs/development/guidelines.md",
    "docs/development/testing-guide.md",
    "docs/development/workflow-explanation.md",
    "docs/feature-flags/create.md",
    "docs/feature-flags/rollouts.md",
    "docs/feature-flags/safety.md",
    "docs/getting-started/README.md",
    "docs/getting-started/architecture.md",
    "docs/getting-started/concepts.md",
    "docs/getting-started/docker-compose-file.md",
    "docs/getting-started/docker-guide.md",
    "docs/getting-started/environment-setup.md",
    "docs/getting-started/faq.md",
    "docs/getting-started/modules.md",
    "docs/getting-started/pre-commit-hooks.md",
    "docs/getting-started/python-virtual-env-setup.md",
    "docs/getting-started/quick-start.md",
    "docs/getting-started/vscode-settings.md",
    "docs/guides/assign-customer-list.md",
    "docs/guides/dashboard-and-api.md",
    "docs/guides/experiment-wizard.md",
    "docs/guides/segments.md",
    "docs/guides/user-guide.md",
    "docs/hipaa/overview.md",
    "docs/infrastructure/aws-iam-setup.md",
    "docs/infrastructure/cloudwatch-setup.md",
    "docs/infrastructure/dynamodb-readme.md",
    "docs/infrastructure/network-security-readme.md",
    "docs/infrastructure/vpc-stack-implementation.md",
    "docs/integrations/aws.md",
    "docs/integrations/github.md",
    "docs/integrations/salesforce.md",
    "docs/llm-evaluation/overview.md",
    "docs/llm-evaluation/quickstart.md",
    "docs/logging_standards.md",
    "docs/mcp-server.md",
    "docs/performance/load-testing.md",
    "docs/rbac/README.md",
    "docs/sdk-guide.md",
    "docs/sdk/go.md",
    "docs/sdk/javascript.md",
    "docs/sdk/local-evaluation.md",
    "docs/sdk/python.md",
    "docs/security/README.md",
    "docs/security/api-keys.md",
    "docs/security/hardening-changes.md",
    "docs/security/incident-response.md",
    "docs/self-hosting/cdk.md",
    "docs/self-hosting/kubernetes.md",
    "docs/self-hosting/migrations.md",
    "docs/self-hosting/monitoring.md",
    "docs/self-hosting/releases.md",
    "docs/statistics/fdr-correction.md",
    "docs/statistics/post-stratification.md",
    "docs/statistics/power-analysis.md",
    "docs/websocket-streaming.md",
    "docs/workspaces/overview.md",
    "frontend/src/pages/docs/index.tsx#desc",
    "infrastructure/cdk/README.md",
    "sdk/go/README.md",
    "sdk/js/README.md",
    "sdk/openfeature-python/README.md",
    "sdk/python/README.md",
)


# ---------------------------------------------------------------------------
# The tree
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Document:
    path: str  # repository path; a surface is "<file>#<part>"
    cls: str  # the inventory class
    extractor: Optional[str]  # "markdown", the surface's, or None (a journey holds it)
    workflow: Optional[str] = None


def registry_path(document: str) -> str:
    return f"{CLAIMS_DIR}/{document.replace('#', '.')}.toml"


def inventory_documents(
    root: pathlib.Path,
) -> Tuple[Dict[str, Document], Dict[str, str]]:
    """The in-scope documents, and the out-of-scope ones with their category."""
    data = tomllib.loads((root / INVENTORY).read_text(encoding="utf-8"))
    found: Dict[str, Document] = {}
    out: Dict[str, str] = {}
    for key, entry in sorted(data.get("pages", {}).items()):
        found[f"docs/{key}"] = Document(
            f"docs/{key}", entry.get("class", ""), MARKDOWN, entry.get("workflow")
        )
    for key, entry in sorted(data.get("documents", {}).items()):
        if entry.get("scope") != "in":
            out[key] = str(entry.get("out", ""))
            continue
        extractor = MARKDOWN if key.endswith(".md") else entry.get("extractor")
        found[key] = Document(
            key, entry.get("class", ""), extractor, entry.get("workflow")
        )
    return found, out


def _load_doc_examples() -> Any:
    name = "_doc_claims_doc_examples"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, DOC_EXAMPLES)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module  # dataclasses looks its module up
        spec.loader.exec_module(module)
    return sys.modules[name]


def _marked_benchmark(node: ast.AST) -> bool:
    """True when the expression names ``<...>.mark.benchmark``."""
    return any(
        isinstance(n, ast.Attribute)
        and n.attr == "benchmark"
        and isinstance(n.value, ast.Attribute)
        and n.value.attr == "mark"
        for n in ast.walk(node)
    )


def _pytestmark(body: Iterable[ast.stmt]) -> bool:
    for statement in body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = (
                statement.targets
                if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets):
                if statement.value is not None and _marked_benchmark(statement.value):
                    return True
    return False


class Context:
    """One check's view of a tree, with what it has parsed cached."""

    def __init__(
        self,
        root: pathlib.Path,
        documents: Mapping[str, Document],
        mechanisms: Mapping[str, Mechanism],
    ) -> None:
        self.root = root
        self.documents = documents
        self.mechanisms = mechanisms
        self.modules_present = (root / "modules").is_dir()
        self.form_only: Set[str] = set()
        self._ast: Dict[str, Any] = {}
        self._text: Dict[str, Optional[str]] = {}
        self._steps: Dict[str, Optional[Set[str]]] = {}
        self._quoted: Dict[str, Set[str]] = {}
        self._enrolled: Optional[Any] = None
        self._blocks: Dict[str, Any] = {}
        self._gate: Optional[Tuple[Set[str], Tuple[str, ...], Optional[str]]] = None

    def gate_lists(self) -> Tuple[Set[str], Tuple[str, ...], Optional[str]]:
        """The files and directories the docs-only gate lists as reading docs,
        and what is wrong with reading them (None when they were read)."""
        if self._gate is None:
            files: Set[str] = set()
            dirs: List[str] = []
            problem: Optional[str] = None
            tree = self.tree(DOCS_GATE)
            if tree is None:
                problem = f"{DOCS_GATE} is gone: the tests that read docs are unknown"
            elif isinstance(tree, str):
                problem = f"{DOCS_GATE} {tree}"
            else:
                found = set()
                for node in tree.body:
                    if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                        continue
                    targets = (
                        node.targets if isinstance(node, ast.Assign) else [node.target]
                    )
                    names = {t.id for t in targets if isinstance(t, ast.Name)}
                    for name in names & {"DOCS_TESTS", "NOT_DOCS_TESTS"}:
                        try:
                            listed = list(ast.literal_eval(node.value))
                        except (ValueError, TypeError, SyntaxError):
                            problem = f"{DOCS_GATE}: {name} is not a literal"
                            continue
                        found.add(name)
                        for path in listed:
                            if path.endswith("/"):
                                dirs.append(path)
                            else:
                                files.add(path)
                if problem is None and found != {"DOCS_TESTS", "NOT_DOCS_TESTS"}:
                    problem = f"{DOCS_GATE} no longer has DOCS_TESTS and NOT_DOCS_TESTS"
            self._gate = (files, tuple(sorted(dirs)), problem)
        return self._gate

    def reads_docs(self, path: str) -> bool:
        """True for a test the docs-only gate classes as reading documentation."""
        files, dirs, _ = self.gate_lists()
        if path in files or path.startswith((WORDING_TESTS, *dirs)):
            return True
        text = self.text(path)
        return text is not None and bool(DOCS_READER.search(text))

    def text(self, rel: str) -> Optional[str]:
        if rel not in self._text:
            path = self.root / rel
            self._text[rel] = (
                path.read_text(encoding="utf-8", errors="replace")
                if path.is_file()
                else None
            )
        return self._text[rel]

    def tree(self, rel: str) -> Any:
        """The parsed module, None when the file is missing, or the error text."""
        if rel not in self._ast:
            text = self.text(rel)
            if text is None:
                self._ast[rel] = None
            else:
                try:
                    self._ast[rel] = ast.parse(text)
                except SyntaxError as error:
                    self._ast[rel] = (
                        f"does not parse: {error.msg} (line {error.lineno})"
                    )
        return self._ast[rel]

    def quoted(self, rel: str) -> Set[str]:
        """Every string constant of a Python file, normalised."""
        if rel not in self._quoted:
            tree = self.tree(rel)
            self._quoted[rel] = (
                {
                    normalise(node.value)
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)
                }
                if isinstance(tree, ast.AST)
                else set()
            )
        return self._quoted[rel]

    def steps(self, slug: str) -> Optional[Set[str]]:
        """The step ids of a journey's expectations file, None if it has none."""
        if slug not in self._steps:
            text = self.text(f"{JOURNEYS}/{slug}.yaml")
            if text is None:
                self._steps[slug] = None
            else:
                found: Set[str] = set()
                inside = False
                for line in text.split("\n"):
                    if line and not line[0].isspace() and not line.startswith("#"):
                        inside = line.rstrip() == "steps:"
                        continue
                    match = re.match(
                        r"^  - id: ['\"]?(?P<id>" + SLUG + r")['\"]?\s*$", line
                    )
                    if inside and match:
                        found.add(match.group("id"))
                self._steps[slug] = found
        return self._steps[slug]

    def enrolled(self) -> Any:
        """Doc Examples' enrolment of this tree, or the refusal text."""
        if self._enrolled is None:
            module = _load_doc_examples()
            try:
                enrolment = module.load(self.root / ENROLMENT)
                self._enrolled = {d["path"] for d in enrolment.documents}
            except module.Refused as refusal:
                self._enrolled = f"scripts/doc_examples.toml: {refusal}"
        return self._enrolled

    def exec_blocks(self, rel: str) -> Any:
        """The exec blocks of an enrolled document, or the refusal text."""
        if rel not in self._blocks:
            module = _load_doc_examples()
            text = self.text(rel)
            if text is None:
                self._blocks[rel] = f"{rel} does not exist"
            else:
                try:
                    blocks = [
                        b
                        for f in module.parse_fences(text, rel)
                        if (b := module.classify(f, rel)) is not None
                    ]
                    self._blocks[rel] = [b for b in blocks if b.kind == "exec"]
                except module.Refused as refusal:
                    self._blocks[rel] = str(refusal)
        return self._blocks[rel]


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------
_TEST = re.compile(
    r"test:(?P<path>[A-Za-z0-9_./@+-]{1,300})(?:::(?P<name>[^\n]{1,300}))?"
)
_JOURNEY = re.compile(r"journey:(?P<slug>" + SLUG + r")#(?P<step>" + SLUG + r")")
_DOCEX = re.compile(
    r"docex:(?P<path>[A-Za-z0-9_./@+-]{1,300}\.md)#(?P<expect>[^\n]{1,200})"
)
_WORKFLOW = re.compile(r"workflow:(?P<name>[A-Za-z0-9_.-]{1,100})\.yml")
_MECHANISM = re.compile(r"mechanism:(?P<name>" + SLUG + r")")


def kind_of(target: str) -> str:
    """The family a target string belongs to, before it is checked."""
    head = target.split(":", 1)[0]
    if head == "test":
        return "test" if "::" in target else "test-file"
    if head == "exempt" and target == "exempt:baseline":
        return "baseline"
    return head


def _python_problem(ctx: Context, path: str, name: Optional[str]) -> Optional[str]:
    tree = ctx.tree(path)
    if isinstance(tree, str):
        return f"{path} {tree}"
    if _pytestmark(tree.body):
        return f"{path} is marked benchmark: skipped unless RUN_BENCHMARKS=1"
    if name is None:
        return None
    parts = name.split("::")
    if len(parts) > 2 or not all(re.fullmatch(PYTHON_NAME, p) for p in parts):
        return f"'{name}' is not a function or Class::method name (no parameters)"
    scope: Sequence[ast.stmt] = tree.body
    owner: Optional[ast.ClassDef] = None
    if len(parts) == 2:
        owner = next(
            (s for s in scope if isinstance(s, ast.ClassDef) and s.name == parts[0]),
            None,
        )
        if owner is None:
            return f"{path} has no class {parts[0]}"
        if not owner.name.startswith("Test"):
            return f"{path}::{owner.name} is not a test class (Test...)"
        if _pytestmark(owner.body) or any(
            _marked_benchmark(d) for d in owner.decorator_list
        ):
            return f"{path}::{owner.name} is marked benchmark: skipped unless RUN_BENCHMARKS=1"
        scope = owner.body
    leaf = parts[-1]
    node = next(
        (
            s
            for s in scope
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and s.name == leaf
        ),
        None,
    )
    if node is None:
        return f"{path} has no {name}"
    wanted = "Test" if isinstance(node, ast.ClassDef) else "test"
    if not node.name.startswith(wanted):
        return f"{path}::{name} is not a test (its name does not start with {wanted})"
    if any(_marked_benchmark(d) for d in node.decorator_list) or (
        isinstance(node, ast.ClassDef) and _pytestmark(node.body)
    ):
        return f"{path}::{name} is marked benchmark: skipped unless RUN_BENCHMARKS=1"
    return None


def test_problem(
    ctx: Context, target: str, *, mechanism: bool = False
) -> Optional[str]:
    """What is wrong with a ``test:`` target, None when it resolves.

    ``mechanism``: the test a ``MECHANISMS`` entry names, which may read wording.
    """
    match = _TEST.fullmatch(target)
    if not match:
        return "not test:<path>[::<name>]"
    path, name = match.group("path"), match.group("name")
    if ".." in path.split("/") or path.startswith("/"):
        return "the path must be repository-relative"
    if path.startswith(NOT_TESTS):
        return f"{path} is not a test (application code or a script)"
    if not mechanism and ctx.reads_docs(path):
        return (
            f"{path} reads documentation (the docs-only gate classes it so); a"
            " claim reaches it only as mechanism:<name> (MECHANISMS in"
            " scripts/doc_claims.py)"
        )
    if not TEST_FILE.search(path):
        return f"{path} is not a test file (test_*.py, *_test.go, *.test.ts, ...)"
    if path.startswith("modules/") and not ctx.modules_present:
        ctx.form_only.add(target)
        return None
    text = ctx.text(path)
    if text is None:
        return f"{path} does not exist"
    if path.endswith(".py"):
        return _python_problem(ctx, path, name)
    if name is not None:
        pattern = r"(?<![\w$])" + re.escape(name) + r"(?![\w$])"
        if not re.search(pattern, text):
            return f"{path} does not contain {name!r}"
    return None


def mechanism_problems(ctx: Context) -> List[str]:
    """Each mechanism's own test resolves and says how it holds a unit."""
    found = []
    for name, mechanism in sorted(ctx.mechanisms.items()):
        problem = test_problem(ctx, "test:" + mechanism.test, mechanism=True)
        if problem is None and "::" not in mechanism.test:
            problem = "a mechanism names one test function"
        if problem is None and mechanism.quotes == bool(mechanism.holds):
            problem = "a mechanism either quotes or holds patterns, not both or neither"
        if problem is None:
            problem = _holds_problem(ctx, mechanism)
        if problem:
            found.append(f"MECHANISMS[{name!r}]: {problem}")
    gate_problem = ctx.gate_lists()[2]
    if gate_problem:
        found.append(gate_problem)
    return found


def _holds_problem(ctx: Context, mechanism: Mechanism) -> Optional[str]:
    """A holds pattern that does not compile, or matches no unit of its pages."""
    for pages, pattern in mechanism.holds:
        try:
            compiled = re.compile(pattern)
        except re.error as error:
            return f"{pattern!r} does not compile: {error}"
        matched = False
        for path in sorted(ctx.documents):
            text = ctx.text(path) if fnmatch.fnmatchcase(path, pages) else None
            if text is None or not path.endswith(".md"):
                continue
            try:
                if any(compiled.search(u.text) for u in units(text)):
                    matched = True
                    break
            except SplitError:
                continue
        if not matched:
            return f"{pattern!r} matches no unit of {pages}: it holds nothing"
    return None


def target_problem(
    ctx: Context, document: Document, entry: Mapping[str, Any], target: str
) -> Optional[str]:
    """What is wrong with one target of one entry, None when it holds."""
    text: str = entry["text"]
    kind = kind_of(target)
    if kind in ("test", "test-file"):
        return test_problem(ctx, target)
    if kind == "journey":
        match = _JOURNEY.fullmatch(target)
        if not match:
            return "not journey:<slug>#<step>"
        steps = ctx.steps(match.group("slug"))
        if steps is None:
            return f"no {JOURNEYS}/{match.group('slug')}.yaml (a pending journey holds nothing)"
        if match.group("step") not in steps:
            return f"journey {match.group('slug')} has no step {match.group('step')!r}"
        return None
    if kind == "docex":
        match = _DOCEX.fullmatch(target)
        if not match:
            return "not docex:<document>.md#<expected text>"
        enrolled = ctx.enrolled()
        if isinstance(enrolled, str):
            return enrolled
        path, expect = match.group("path"), match.group("expect")
        if path not in enrolled:
            return f"scripts/doc_examples.toml does not enrol {path}"
        blocks = ctx.exec_blocks(path)
        if isinstance(blocks, str):
            return blocks
        named = [b for b in blocks if expect in b.expects]
        if len(named) != 1:
            return (
                f"{len(named)} exec blocks of {path} expect {expect!r}; a docex"
                " target names exactly one"
            )
        return None
    if kind == "workflow":
        match = _WORKFLOW.fullmatch(target)
        if not match:
            return "not workflow:<name>.yml"
        if document.cls != "workflow" or document.workflow != match.group("name"):
            return (
                f"{document.path} is not class workflow with workflow ="
                f" {match.group('name')!r} in the inventory"
            )
        return None
    if kind == "mechanism":
        match = _MECHANISM.fullmatch(target)
        if not match or match.group("name") not in ctx.mechanisms:
            return f"not one of MECHANISMS: {', '.join(sorted(ctx.mechanisms))}"
        mechanism = ctx.mechanisms[match.group("name")]
        if mechanism.quotes:
            path = mechanism.test.split("::")[0]
            if path.startswith("modules/") and not ctx.modules_present:
                ctx.form_only.add(target)
                return None
            if text not in ctx.quoted(path):
                return f"{path} does not quote this unit word for word"
            return None
        patterns = [
            pattern
            for pages, pattern in mechanism.holds
            if fnmatch.fnmatchcase(document.path, pages)
        ]
        if not patterns:
            return f"{match.group('name')} holds no unit of {document.path}"
        if not any(re.search(pattern, text) for pattern in patterns):
            return (
                f"{match.group('name')} does not check this unit (it holds units"
                f" matching {' or '.join(patterns)})"
            )
        return None
    if kind == "evidence":
        if not EVIDENCE.fullmatch(target[len("evidence:") :]):
            return "not evidence:<a GitHub run or issue comment URL of this repository>"
        if document.cls != "aws":
            return "evidence: is only for a document of class aws"
        return (
            None
            if dated(text)
            else "evidence: needs a dated unit (as of vX.Y.Z, on YYYY-MM-DD)"
        )
    if kind == "exempt":
        category = target[len("exempt:") :]
        if category not in EXEMPT_CATEGORIES:
            return f"exempt:{category} is not one of {', '.join(EXEMPT_CATEGORIES)}"
        if category == "intent":
            if document.cls != "aws" and entry.get("tag") != "X":
                return (
                    "exempt:intent is only for a document of class aws, or a unit"
                    ' tagged tag = "X"'
                )
            if not dated(text):
                return "exempt:intent needs a dated unit (as of vX.Y.Z, on YYYY-MM-DD)"
        if category == "policy" and document.path not in POLICY_DOCUMENTS:
            return f"exempt:policy is only for {', '.join(POLICY_DOCUMENTS)}"
        return None
    if kind == "untested":
        if target[len("untested:") :] not in UNTESTED_KINDS:
            return f"untested: takes one of {', '.join(UNTESTED_KINDS)}"
        return None
    if kind == "baseline":
        return None
    return f"unknown target kind {kind!r}"


def dated(text: str) -> bool:
    for match in DATED.finditer(text):
        if match.group("y") is None:
            return True
        try:
            datetime.date(int(match["y"]), int(match["m"]), int(match["d"]))
            return True
        except ValueError:
            continue
    return False


def _by_problem(entry: Mapping[str, Any]) -> Optional[str]:
    by = entry.get("by")
    if not isinstance(by, list) or not by or not all(isinstance(t, str) for t in by):
        return "by must be a non-empty list of targets"
    if len(set(by)) != len(by):
        return "by names a target twice"
    kinds = [kind_of(t) for t in by]
    alone = [k for k in kinds if k in ("exempt", "untested", "baseline")]
    if alone and len(by) > 1:
        return "an exempt: or untested: target stands alone"
    if "tag" in entry and by != ["exempt:intent"]:
        return 'tag is only for an exempt:intent unit (tag = "X")'
    if "tag" in entry and entry["tag"] != "X":
        return 'tag takes "X" only'
    return None


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------
ENTRY_KEYS = {"text", "by", "count", "tag"}
COUNT_MAX = 999


@dataclasses.dataclass
class Result:
    problems: List[str]
    counts: Dict[str, Any]


def _ledger(ctx: Context) -> Tuple[List[Tuple[str, str, str]], List[str]]:
    path = ctx.root / LEDGER
    if not path.is_file():
        return [], []
    lines: List[Tuple[str, str, str]] = []
    problems: List[str] = []
    data = [
        line
        for line in path.read_text(encoding="utf-8").split("\n")
        if line.strip() and not line.startswith("#")
    ]
    for line in data:
        fields = line.split("\t")
        if (
            len(fields) != 3
            or fields[0] not in LEDGER_KINDS
            or not re.fullmatch(r"[0-9a-f]{%d}" % HASH_LENGTH, fields[2])
        ):
            problems.append(
                f"{LEDGER}: {line!r} is not kind<TAB>document<TAB>hash, with kind"
                f" one of {', '.join(LEDGER_KINDS)}"
            )
            continue
        lines.append((fields[0], fields[1], fields[2]))
    if data != sorted(set(data)):
        problems.append(f"{LEDGER}: lines are sorted and appear once each")
    return lines, problems


def _list_problems(
    name: str, listed: Sequence[str], eligible: Set[str], registered: Set[str]
) -> List[str]:
    found = []
    if list(listed) != sorted(set(listed)):
        found.append(f"{name} is sorted and names each document once")
    for path in listed:
        if path not in eligible:
            found.append(
                f"{name}: {path} is not an in-scope document with an extractor"
                " (tests/acceptance/docs/inventory.toml): take it off the list"
            )
        elif path in registered:
            found.append(f"{name}: {path} has a registry; take it off the list")
    return found


def check(
    root: pathlib.Path = ROOT,
    *,
    pending: Sequence[str] = CLAIMS_PENDING,
    deferred: Sequence[str] = CLAIMS_DEFERRED,
    mechanisms: Mapping[str, Mechanism] = MECHANISMS,
) -> Result:
    """Every problem with the registries of the tree at *root*, and the counts."""
    problems: List[str] = []
    try:
        documents, out = inventory_documents(root)
    except (OSError, tomllib.TOMLDecodeError) as error:
        return Result([f"{INVENTORY}: {error}"], {})
    ctx = Context(root, documents, mechanisms)
    problems += mechanism_problems(ctx)

    eligible = {p for p, d in documents.items() if d.extractor is not None}
    expected = {registry_path(p): p for p in eligible}
    every = {registry_path(p): p for p in list(documents) + list(out)}
    registered: Dict[str, str] = {}
    claims = root / CLAIMS_DIR
    for directory, dirs, files in os.walk(claims):
        dirs.sort()
        for name in sorted(files):
            rel = (pathlib.Path(directory) / name).relative_to(root).as_posix()
            if rel == LEDGER:
                continue
            if rel in expected:
                registered[expected[rel]] = rel
            elif rel in every:
                problems.append(
                    f"{rel}: {every[rel]} is out of scope, or held by its journey:"
                    " it has no registry"
                )
            else:
                problems.append(
                    f"{rel}: not the registry of an in-scope document"
                    f" ({CLAIMS_DIR}/<document>.toml) or the ledger"
                )

    both = sorted(set(pending) & set(deferred))
    if both:
        problems.append(f"in CLAIMS_PENDING and CLAIMS_DEFERRED: {both}")
    problems += _list_problems("CLAIMS_PENDING", pending, eligible, set(registered))
    problems += _list_problems("CLAIMS_DEFERRED", deferred, eligible, set(registered))
    for path in sorted(eligible - set(registered) - set(pending) - set(deferred)):
        problems.append(
            f"{path}: in scope, with no registry ({registry_path(path)}) and not in"
            " CLAIMS_PENDING or CLAIMS_DEFERRED"
        )

    targets: collections.Counter = collections.Counter()
    dispositions: collections.Counter = collections.Counter()
    tagged = 0
    want_ledger: Set[Tuple[str, str, str]] = set()
    unit_total = 0
    for path, rel in sorted(registered.items()):
        found, entries = _registry_problems(ctx, documents[path], rel)
        problems += found
        for entry in entries:
            count = entry.get("count", 1)
            unit_total += count
            by = entry["by"]
            for target in by:
                targets[kind_of(target)] += 1
            head = by[0]
            tagged += count if entry.get("tag") == "X" else 0
            if kind_of(head) in ("exempt", "untested", "baseline"):
                dispositions[head] += count
            else:
                dispositions["verified"] += count
            if head in LEDGER_KINDS:
                want_ledger.add((head, path, unit_hash(entry["text"])))

    lines, ledger_problems = _ledger(ctx)
    problems += ledger_problems
    have = set(lines)
    for kind, path, digest in sorted(want_ledger - have):
        problems.append(
            f"{path}: the {kind} unit {digest} has no line in {LEDGER}:"
            f" add '{kind}\\t{path}\\t{digest}'"
        )
    for kind, path, digest in sorted(have - want_ledger):
        problems.append(
            f"{LEDGER}: '{kind}\\t{path}\\t{digest}' matches no {kind} unit of a"
            " registry: remove it"
        )

    counts = {
        "in scope": len(documents),
        "held by a journey": len(set(documents) - eligible),
        "registered": len(registered),
        "pending": len(pending),
        "deferred": len(deferred),
        "out of scope": dict(sorted(out.items())),
        "units": unit_total,
        "dispositions": dict(sorted(dispositions.items())),
        "targets": {k: targets[k] for k in VERIFYING if targets[k]},
        "ledger": dict(sorted(collections.Counter(k for k, _, _ in lines).items())),
        "form only": len(ctx.form_only),
        "tagged X": tagged,
    }
    return Result(problems, counts)


def _registry_problems(
    ctx: Context, document: Document, rel: str
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """A registry against its document; the problems and the well-formed entries."""
    try:
        data = tomllib.loads((ctx.root / rel).read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        return [f"{rel}: does not parse: {error}"], []
    problems: List[str] = []
    if set(data) - {"document", "unit"}:
        problems.append(
            f"{rel}: unknown keys {sorted(set(data) - {'document', 'unit'})}"
        )
    if data.get("document") != document.path:
        problems.append(
            f"{rel}: document = {document.path!r}, the path it is the registry of"
        )
    if document.extractor != MARKDOWN:
        return problems + [
            f"{rel}: scripts/doc_claims.py has no {document.extractor!r} extractor yet;"
            f" {document.path} stays in CLAIMS_PENDING until it has"
        ], []
    text = ctx.text(document.path)
    if text is None:
        return problems + [f"{rel}: {document.path} does not exist"], []
    try:
        on_page = units(text)
    except SplitError as error:
        return problems + [f"{document.path}: {error}"], []

    entries: List[Dict[str, Any]] = []
    registered: Dict[str, int] = {}
    raw = data.get("unit", [])
    if not isinstance(raw, list):
        return problems + [f"{rel}: unit is an array of tables ([[unit]])"], []
    for number, entry in zip(range(1, len(raw) + 1), raw):
        where = f"{rel}: unit {number}"
        if not isinstance(entry, dict) or not isinstance(entry.get("text"), str):
            problems.append(f'{where}: needs text = "..."')
            continue
        where = f"{rel}: {entry['text'][:80]!r}"
        if set(entry) - ENTRY_KEYS:
            problems.append(f"{where}: unknown keys {sorted(set(entry) - ENTRY_KEYS)}")
        if entry["text"] in registered:
            problems.append(f"{where}: listed twice; one entry with count = N")
            continue
        count = entry.get("count", 1)
        if "count" in entry and (
            not isinstance(count, int)
            or isinstance(count, bool)
            or not 2 <= count <= COUNT_MAX
        ):
            problems.append(f"{where}: count is 2-{COUNT_MAX}, and absent for one")
            continue
        registered[entry["text"]] = count
        shape = _by_problem(entry)
        if shape:
            problems.append(f"{where}: {shape}")
            continue
        bad = [
            f"{where}: {target}: {problem}"
            for target in entry["by"]
            if (problem := target_problem(ctx, document, entry, target))
        ]
        problems += bad
        if not bad:
            entries.append(entry)

    seen: Dict[str, int] = collections.Counter(u.text for u in on_page)
    first: Dict[str, int] = {}
    for unit in on_page:
        first.setdefault(unit.text, unit.line)
    for unit_text, times in seen.items():
        if unit_text not in registered:
            problems.append(
                f"{document.path}:{first[unit_text]}: unregistered unit"
                f" {unit_text!r}: map it to what verifies it, or exempt it with a"
                f" category ({rel})"
            )
        elif registered[unit_text] != times:
            problems.append(
                f"{document.path}:{first[unit_text]}: {unit_text!r}: registered"
                f" x{registered[unit_text]}, found x{times}"
            )
    for unit_text in registered:
        if unit_text not in seen:
            problems.append(
                f"{rel}: {unit_text!r} is listed but no longer on {document.path}"
            )
    return problems, entries


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def report_lines(counts: Mapping[str, Any]) -> List[str]:
    out = counts.get("out of scope", {})
    lines = [
        f"documents in scope: {counts.get('in scope', 0)} (held by a journey:"
        f" {counts.get('held by a journey', 0)})",
        f"  registered {counts.get('registered', 0)}, pending"
        f" {counts.get('pending', 0)}, deferred {counts.get('deferred', 0)}",
        f"out of scope: {len(out)}",
    ]
    lines += [f"  {path} ({category})" for path, category in out.items()]
    lines.append(f"units registered: {counts.get('units', 0)}")
    lines += [f"  {k}: {v}" for k, v in counts.get("dispositions", {}).items()]
    lines.append(f"  of which exempt:intent tagged X: {counts.get('tagged X', 0)}")
    lines.append("verifying targets:")
    lines += [f"  {k}: {v}" for k, v in counts.get("targets", {}).items()]
    lines.append("ledger lines:")
    lines += [f"  {k}: {v}" for k, v in counts.get("ledger", {}).items()]
    lines.append(
        f"modules/ targets checked for form only (no modules/ here):"
        f" {counts.get('form only', 0)}"
    )
    return lines


def skeleton(document: str, text: str) -> str:
    """A registry for *document* with every unit and no targets, to fill in."""
    counted = collections.Counter(u.text for u in units(text))
    parts = [f"document = {json.dumps(document)}\n"]
    for unit_text, times in counted.items():
        entry = (
            f"\n[[unit]]\ntext = {json.dumps(unit_text, ensure_ascii=False)}\nby = []\n"
        )
        if times > 1:
            entry += f"count = {times}\n"
        parts.append(entry)
    return "".join(parts)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("check", help="every problem, then the counts")
    sub.add_parser("report", help="the counts")
    unit_parser = sub.add_parser("units", help="a registry skeleton per document")
    unit_parser.add_argument("documents", nargs="*")
    unit_parser.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)
    command = args.command or "check"
    if command == "units":
        targets = list(args.documents)
        if args.all:
            documents, _ = inventory_documents(ROOT)
            targets = sorted(p for p, d in documents.items() if d.extractor == MARKDOWN)
        for document in targets:
            try:
                sys.stdout.write(
                    skeleton(document, (ROOT / document).read_text(encoding="utf-8"))
                    + "\n"
                )
            except (OSError, SplitError) as error:
                print(f"{document}: {error}", file=sys.stderr)
                return 1
        return 0
    result = check()
    if command == "check":
        for problem in result.problems:
            print(problem)
    print("\n".join(report_lines(result.counts)))
    if command == "check":
        print(f"problems: {len(result.problems)}")
        return 1 if result.problems else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
