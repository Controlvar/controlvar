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
    through ``mechanism:``, which says which units it holds; the exception is
    ``API_SNAPSHOT_TESTS``, which read the OpenAPI snapshots, not a page. In
    Python the function, or the class and method, is found by parsing the
    file, its name starts with ``test`` (a class with ``Test``), and one marked
    ``benchmark`` is refused (skipped unless RUN_BENCHMARKS=1, so it would hold
    nothing). In another language the file contains the name. ``test:<path>`` alone (the whole file)
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
a journey holds (no extractor) has none. This file reads Markdown pages and
two of the named surfaces (``EXTRACTORS``): ``text``, a file read whole
(``charts/experimently/templates/NOTES.txt``), and ``leading-hash-comments``,
the comment block at the top of a file after a ``#!`` line, each line's ``#``
and the white space around the rest removed (the two ``deploy/compose``
headers). That is the inventory test's rule for both. The splitter then reads
the extracted text like a page. One more surface gives its units directly
(``UNIT_EXTRACTORS``): ``docs-hub-desc``, the dashboard's docs hub
(``frontend/src/pages/docs/index.tsx#desc``), where each single- or
double-quoted ``desc:`` and ``description:`` literal is one unit, its
backslash escapes resolved and normalised as above. A key whose value is not
such a literal of at most 500 characters is an error, not a unit dropped. A
surface with another extractor stays pending until its extractor is added here.

THE GENERATOR (``generate``; a developer tool, its input is private)

``generate --audit ROWS --audit-sha SHA DOC ...`` writes the registries of a
page group from the doc-claims audit, read from the path given (JSON Lines: one
row per flow or claim, with the ``doc``, the ``line`` at the audited commit, the
auditor's ``text``, ``covered``, ``by`` and ``tag``). Each document is split as
it was at SHA (``git show``) and each row goes to the units on its line (of
several, those its excerpt picks). A unit whose text is unchanged today keeps
its rows (``plan_document`` has the rules): covered rows give targets, from
``by``:

* ``path::name`` (also ``path (name, name)``) gives ``test:path::name``, each;
* a test file gives ``test:<path>``; a bare file name is found if it is unique;
* ``doc-examples <page>:<line>`` gives ``docex:<page>#<expectation>``, the
  block at that line then, named by an expectation exactly one block has now;
* ``journey s#a,b`` gives ``journey:s#a`` and ``journey:s#b``;
* the OpenAPI snapshots give ``test:`` on their test (``API_SNAPSHOT_TESTS``);
* a workflow gives ``workflow:<name>.yml``;
* a script or application source is refused: printed, never written.

A target that does not resolve in this tree is printed. Uncovered T, S and A
rows give ``untested:<tag>``; a unit with no row ``exempt:baseline``; both are
ledgered. A unit new since SHA gets no entry. An R or X row whose own excerpt
(normalised as a unit is) is still on the page makes the exit status 1 and that
page is not written. So does one whose excerpt was not on the page at SHA
either (it is not verbatim) and whose line holds no unit (a fence, a comment,
front matter): nothing tells whether it is gone, so it is printed for the
author to map. The units an R or X row maps to get no entry either way. A row
that does not map (some excerpts are not verbatim) is printed for the author to
map by hand, never dropped; the units on its line get no entry. An audit with
no row is refused: an empty or cut-off file would otherwise read as "nothing
found".

``sync DOC`` prints each unit that is not in the registry beside the
registered units within a difflib ratio of 0.9 (inputs cut to 2,000
characters); it writes nothing, so no target moves to a new sentence unless its
author moves it. ``baseline`` lists the audit claim rows that map to a unit a
registry leaves at ``exempt:baseline`` (run at a release candidate).

RUN

    python scripts/doc_claims.py check            every problem, then the counts
    python scripts/doc_claims.py report           the counts
    python scripts/doc_claims.py units DOC ...    a registry skeleton per document
    python scripts/doc_claims.py units --all      the same for every Markdown document
    python scripts/doc_claims.py generate --audit ROWS --audit-sha SHA DOC ...
                                                  [--out DIR] [--replace]
    python scripts/doc_claims.py sync DOC         what changed, and what is near it
    python scripts/doc_claims.py baseline --audit ROWS --audit-sha SHA [DOC ...]

Standard library only; imports nothing from ``backend`` or ``modules``.
``check``, ``report``, ``units`` and ``sync`` run no git, so they give the same
answer in ``scripts/core_build.sh``'s copy; ``generate`` and ``baseline`` read
the audited commit with ``git show``.
"""

from __future__ import annotations

import argparse
import ast
import collections
import dataclasses
import datetime
import difflib
import fnmatch
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import subprocess
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
#: The named surfaces this file reads, by the inventory's extractor name: a
#: file read whole, and the comment block at the top of a file.
TEXT = "text"
HEADER = "leading-hash-comments"
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
    return [unit for unit, _, _ in unit_lines(text)]


def unit_lines(text: str) -> List[Tuple[Unit, int, int]]:
    """Each unit with the first and last line its own words are on.

    A sentence of a hard-wrapped paragraph spans only its own lines, found by
    counting words, which normalising leaves in place; where markup across a
    line break changes the count, the sentence spans its whole block.
    """
    found: List[Tuple[Unit, int, int]] = []
    for block in _blocks(visible_lines(text)):
        if block.kind in ("heading", "row"):
            pieces = [block.parts[0][1]]
            owners = [block.line] * len(pieces[0].split())
        else:
            tidy = (lambda s: _TAG.sub(" ", s)) if block.kind == "html" else str
            joined = tidy(" ".join(part for _, part in block.parts))
            pieces = _SENTENCE_END.split(normalise(joined))
            owners = [
                line
                for line, part in block.parts
                for _ in normalise(tidy(part)).split()
            ]
        aligned = len(owners) == sum(len(piece.split()) for piece in pieces)
        position = 0
        for piece in pieces:
            words = piece.split()
            piece = " ".join(words)
            if aligned and words:
                first, last = owners[position], owners[position + len(words) - 1]
            else:
                first, last = block.parts[0][0], block.parts[-1][0]
            position += len(words)
            if _WORD.search(piece):
                found.append((Unit(piece, block.line), first, last))
    return found


def leading_hash_comments(text: str) -> str:
    """The comment block at the top of *text*, after a ``#!`` line: each line
    up to the first that does not start with ``#``, with its ``#`` and the white
    space around the rest removed (test_docs_inventory.py's rule). The ``#!``
    line is read as a blank line, so a unit keeps its line in the file."""
    lines = text.replace("\r\n", "\n").split("\n")
    block: List[str] = []
    if lines and lines[0].startswith("#!"):
        block.append("")
        lines = lines[1:]
    for line in lines:
        if not line.startswith("#"):
            break
        block.append(line[1:].strip())
    return "\n".join(block) + "\n"


#: What each extractor gives the splitter, from the file's text.
EXTRACTORS: Dict[str, Any] = {
    MARKDOWN: lambda text: text,
    TEXT: lambda text: text,
    HEADER: leading_hash_comments,
}


def extracted(document: str, extractor: Optional[str], text: str) -> str:
    """The text of *document* the splitter reads; refused for an extractor
    this file does not have."""
    if extractor not in EXTRACTORS:
        raise SplitError(
            f"scripts/doc_claims.py has no {extractor!r} extractor yet; {document}"
            " stays in CLAIMS_PENDING until it has"
        )
    return EXTRACTORS[extractor](text)


def unit_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:HASH_LENGTH]


#: The docs hub's extractor (``tests/acceptance/docs/inventory.toml`` names it
#: and counts its literals; ``test_docs_inventory.py`` reads it the same way).
HUB = "docs-hub-desc"
#: A desc: or description: key, whatever its value is.
_HUB_KEY = re.compile(r"\b(?:desc|description)['\"]?[ \t]{0,8}:")
#: The same key whose whole value is one single- or double-quoted literal of at
#: most 500 characters, ending the value. Bounded, and a character of a literal
#: can match only one of the two alternatives, so it does not backtrack.
_HUB_LITERAL = re.compile(
    r"\b(?:desc|description)['\"]?[ \t]{0,8}:\s{0,40}"
    r"(?:'(?P<single>(?:[^'\\\n]|\\.){0,500})'"
    r"|\"(?P<double>(?:[^\"\\\n]|\\.){0,500})\")"
    r"(?=[ \t]{0,8}(?:[,}\])\n]|\Z))"
)
_JS_ESCAPE = re.compile(r"\\(.)")


def hub_units(text: str) -> List[Unit]:
    """The docs hub's units: each desc:/description: literal, in file order."""
    literals: Dict[int, Tuple[str, int]] = {}
    for match in _HUB_LITERAL.finditer(text):
        single = match.group("single")
        raw = single if single is not None else match.group("double")
        literals[match.start()] = (raw, text.count("\n", 0, match.start()) + 1)
    for key in _HUB_KEY.finditer(text):
        if key.start() not in literals:
            line = text.count("\n", 0, key.start()) + 1
            raise SplitError(
                f"line {line}: a {HUB} value that is not one single- or"
                " double-quoted string of at most 500 characters"
            )
    found = []
    for raw, line in literals.values():
        unit_text = normalise(_JS_ESCAPE.sub(r"\1", raw))
        if _WORD.search(unit_text):
            found.append(Unit(unit_text, line))
    return found


#: Extractors that give the units themselves, not text for the splitter.
UNIT_EXTRACTORS: Dict[str, Any] = {HUB: hub_units}


def has_extractor(extractor: Optional[str]) -> bool:
    return extractor in EXTRACTORS or extractor in UNIT_EXTRACTORS


def document_units(document: str, extractor: Optional[str], text: str) -> List[Unit]:
    """The units of *document*, whose file's text is *text*, through its extractor."""
    if extractor in UNIT_EXTRACTORS:
        return UNIT_EXTRACTORS[extractor](text)
    return units(extracted(document, extractor, text))


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
#: The repository's names: the one dated evidence in the registries was taken
#: under, and the one it moved to. Evidence is a record of a run and is never
#: rewritten, so its links keep the name they were taken under (GitHub
#: redirects them). Nothing is read from the environment: the same registry
#: holds on a laptop, in core_build's copy and in CI.
EVIDENCE_REPOSITORY = ("getexperimently/experimently", "Controlvar/controlvar")
EVIDENCE_PATH = (
    r"(?:actions/runs/[0-9]{1,12}|issues/[0-9]{1,7}#issuecomment-[0-9]{1,12})"
)


def evidence_url(url: str) -> bool:
    """A run or issue comment URL of one of EVIDENCE_REPOSITORY's names."""
    return any(
        re.fullmatch(
            r"https://github\.com/" + re.escape(name) + "/" + EVIDENCE_PATH, url
        )
        is not None
        for name in EVIDENCE_REPOSITORY
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
#: Tests the docs-only gate lists because they read the OpenAPI snapshots under
#: docs/api/, the product's schema rather than a page's wording: a claim about a
#: route or a field may name one with ``test:``, while ``docs_reads_problem``
#: finds nothing in it.
API_SNAPSHOT_TESTS: Tuple[str, ...] = ("backend/tests/smoke/test_openapi_snapshot.py",)
_NAMES_A_PAGE = re.compile(r"\.md\b|mkdocs")


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
#: "0.1.0 is a beta release.", after the sentence naming the registry.
_BETA = r"^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4} is a beta release\.$"


def _whole(document: str, *units: str) -> Tuple[Tuple[str, str], ...]:
    """``holds`` pairs that each match one unit of *document* exactly: the
    unit's whole text, every character literal, anchored at both ends."""
    return tuple((document, "^" + re.escape(unit) + "$") for unit in units)


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
    # The dashboard's docs hub: each description against the page it links to.
    "docs-hub": Mechanism(
        "backend/tests/unit/docs/test_docs_hub_claims.py"
        "::test_each_description_is_true_of_its_page",
        quotes=True,
    ),
    "integrations-claims": Mechanism(
        "backend/tests/unit/docs/test_integrations_docs_claims.py"
        "::test_every_claim_about_what_the_platform_does_is_on_the_list",
        quotes=True,
    ),
    # A quoted package version (a bare `experimently` only on the Python page),
    # and the version after the sentence naming the registry, in the SDK
    # pages, the guide, the FAQ and the READMEs the packages carry.
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
            # The sentence before it names the package: the test reads the two.
            ("docs/sdk/python.md", _BETA),
            ("docs/sdk/javascript.md", _BETA),
            ("sdk/python/README.md", _BETA),
            ("sdk/js/README.md", _BETA),
            (
                "docs/sdk-guide.md",
                r" package is on (?:PyPI|npm) " + _VERSION + r", beta\):$",
            ),
            (
                "docs/getting-started/faq.md",
                r"\((?:npm|pip) install (?:@getexperimently/[A-Za-z0-9_-]{1,60}"
                r"|experimently), [0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}, beta\)",
            ),
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
    # Which dashboard image a plain `docker build` gives, on the modules page.
    "dashboard-image-default": Mechanism(
        "backend/tests/unit/infrastructure/test_dashboard_image_docs.py"
        "::test_the_modules_page_names_the_dockerfile_default",
        holds=(
            (
                "docs/getting-started/modules.md",
                r"^For the dashboard, EXPERIMENTLY_PROFILE=core is the default",
            ),
        ),
    ),
    # The quick start's rollout example: which of its two users the 10% holds.
    "quick-start-rollout": Mechanism(
        "backend/tests/unit/docs/test_quick_start_doc.py"
        "::test_the_rollout_puts_user_2_in_and_user_123_out",
        holds=(
            (
                "docs/getting-started/quick-start.md",
                r"user-2 is inside the 10%, user-123 is not",
            ),
        ),
    ),
    # The signature identity the release page verifies with (its IDENTITY=
    # line): the release paths it accepts, and every other signer it refuses.
    "release-identity-accepts": Mechanism(
        "backend/tests/unit/docs/test_release_identity_regexp.py"
        "::test_the_identity_accepts_every_release_path",
        holds=(
            (
                "docs/self-hosting/releases.md",
                r"^It accepts release\.yml run from main, which is how release-please"
                r" cuts a release and how gh workflow run release\.yml -f tag=vX\.Y\.Z"
                r" runs one again, and run from a v tag, which is how a tag pushed by"
                r" hand starts it\.$",
            ),
        ),
    ),
    "release-identity-refuses": Mechanism(
        "backend/tests/unit/docs/test_release_identity_regexp.py"
        "::test_the_identity_refuses_anything_else",
        holds=(
            (
                "docs/self-hosting/releases.md",
                r"^IDENTITY names one workflow, this repository's release\.yml\.$"
                r"|^A signature made by any other workflow, repository or branch fails\.$",
            ),
        ),
    ),
    # The same sentence in an SDK's README.
    "sdk-live-contract-readme": Mechanism(
        "backend/tests/unit/docs/test_sdk_live_contract_sentence.py"
        "::test_every_sdk_readme_agrees_with_the_gate",
        holds=(
            (
                "sdk/*/README.md",
                r"^CI (?:runs|does not run) this SDK's contract smoke against a real"
                r" API: [a-z0-9-]{1,40} (?:is|is not) in the sdks: list of the SDK Live"
                r" Contract job in pr-qa-gate\.yml\.$",
            ),
        ),
    ),
    # The SDK guide's status table, and the two sentences about what CI runs.
    "sdk-guide-status": Mechanism(
        "backend/tests/unit/docs/test_sdk_guide_status.py"
        "::test_the_status_table_agrees_with_ci",
        holds=(
            (
                "docs/sdk-guide.md",
                r"^[^|]{1,60} \| sdk/[a-z-]{1,40} \| [^|]{1,40} \| [^|]{1,300} \| [^|]{1,300}$",
            ),
            (
                "docs/sdk-guide.md",
                r'^"Verified live" means the SDK\'s contract_smoke entry point passed'
                r" tests/sdk-contract/live/run_live_contract\.py against a running"
                r" backend \(see tests/sdk-contract/README\.md\); the SDK Live Contract"
                r" CI job repeats this on every pull request, with --strict, for"
                r" [a-z, -]{1,200} \(the sdks: list of the job in pr-qa-gate\.yml\)\.$",
            ),
            (
                "docs/sdk-guide.md",
                r'^Rows marked "not yet" have a contract_smoke entry point that no CI'
                r" job runs against a live backend; their unit tests run in the nightly"
                r" tier and on pull requests that touch them\.$",
            ),
        ),
    ),
    # The Python README's quick start, run as written against a fake API.
    "python-readme-quick-start": Mechanism(
        "sdk/python/tests/test_readme.py::test_the_readme_quick_start_runs_as_written",
        holds=(
            (
                "sdk/python/README.md",
                r"^Set EXPERIMENTLY_API_KEY to an API key, then run the block as it is",
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
    # The Doc Examples guide's "When the check fails" sample is what the check
    # prints; the sentence above the sample says what that output is.
    "doc-examples-failure-sample": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_the_authoring_pages_sample_is_what_the_check_prints",
        holds=(
            (
                "docs/development/doc-examples.md",
                r"^Every problem is reported in the same run, one line each, naming"
                r" the page \(and the line, where there is one\) and what to change\.$",
            ),
        ),
    ),
    # D73 TR3: tests that hold units of the contributor, migration and
    # load-testing pages, each held unit matched whole (_whole).
    # A page's blocks run in one shell, top to bottom.
    "doc-examples-one-shell": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_variables_carry_from_block_to_block",
        holds=_whole(
            "docs/development/doc-examples.md",
            "The shell examples on an enrolled page run in CI as they are written, in one bash session per page.",
            "The page's blocks share one shell, top to bottom.",
            "A variable set in one block is there in the next.",
        ),
    ),
    # Every enrolled page that runs is in one shard of the CI run.
    "doc-examples-shards": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_the_repository_s_shards_are_every_page_that_runs_once",
        holds=_whole(
            "docs/development/doc-examples.md",
            "The shell examples on an enrolled page run in CI as they are written, in one bash session per page.",
        ),
    ),
    # The doc-examples workflow runs the check and the examples on every pull request.
    "doc-examples-every-pull-request": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_the_workflow_runs_the_check_and_the_examples_on_every_pull_request",
        holds=_whole(
            "docs/development/doc-examples.md",
            "scripts/doc_examples.py is the check, and the doc-examples workflow runs it on every pull request.",
        ),
    ),
    # A failing command, a turned-off strict mode and a closed pipe fail the page.
    "doc-examples-strict-mode": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_the_shapes_that_used_to_pass_silently",
        holds=_whole(
            "docs/development/doc-examples.md",
            "A page fails when a command fails, when an example stops printing what the page says it prints, or when a block breaks one of the rules below.",
            "The blocks run under set -euo pipefail.",
            "So yes | head -1 and … | grep -q x exit 141 when the pipe closes early, and the block fails; write them another way.",
        ),
    ),
    # Expectations: word-bounded, in order.
    "doc-examples-expectations": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_expectations_are_word_bounded_and_in_order",
        holds=_whole(
            "docs/development/doc-examples.md",
            "A page fails when a command fails, when an example stops printing what the page says it prints, or when a block breaks one of the rules below.",
            "The text of an expect line has to appear in what the block prints to stdout, in the order the lines are given, and a match can't start or end inside a word: active doesn't match inactive, and 1 doesn't match 10.",
        ),
    ),
    # Expectations are matched on stdout.
    "doc-examples-stdout-only": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_expectations_are_matched_on_stdout_only",
        holds=_whole(
            "docs/development/doc-examples.md",
            "The text of an expect line has to appear in what the block prints to stdout, in the order the lines are given, and a match can't start or end inside a word: active doesn't match inactive, and 1 doesn't match 10.",
        ),
    ),
    # The tag grammar: one tag in braces, a reason for skip, the timeout range.
    "doc-examples-tags": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_malformed_tags_are_refused",
        holds=_whole(
            "docs/development/doc-examples.md",
            "A page fails when a command fails, when an example stops printing what the page says it prints, or when a block breaks one of the rules below.",
            "On an enrolled page, every bash block carries exactly one tag, in braces.",
            "The reason is required, and it starts with a category (see Pages that cannot run here):",
            "Any other form of a shell fence on an enrolled page is refused, with the file and line.",
        ),
    ),
    # The pages the check walks, and the directories it skips.
    "doc-examples-walk": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_the_walk_prunes_by_name_at_any_depth",
        holds=_whole(
            "docs/development/doc-examples.md",
            "Every Markdown page in the repository, except CLAUDE.md files and anything under .claude/.",
            "The check walks the tree for them, skipping node_modules, venv, site and hidden directories other than .github, and the list it finds must equal [meta] universe in scripts/doc_examples.toml.",
        ),
    ),
    # CLAUDE.md files are not pages.
    "doc-examples-working-instructions": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_working_instructions_are_excluded_in_any_case",
        holds=_whole(
            "docs/development/doc-examples.md",
            "Every Markdown page in the repository, except CLAUDE.md files and anything under .claude/.",
        ),
    ),
    # A page [meta] universe does not name fails the check.
    "doc-examples-new-page": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_a_new_page_the_universe_does_not_list_is_refused",
        holds=_whole(
            "docs/development/doc-examples.md",
            "The check walks the tree for them, skipping node_modules, venv, site and hidden directories other than .github, and the list it finds must equal [meta] universe in scripts/doc_examples.toml.",
            "A new page, or a renamed one, fails the check until that list names it.",
        ),
    ),
    # A renamed page fails until universe follows.
    "doc-examples-listed-page-gone": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_a_listed_page_the_walk_does_not_find_is_refused",
        holds=_whole(
            "docs/development/doc-examples.md",
            "A new page, or a renamed one, fails the check until that list names it.",
        ),
    ),
    # Every fence names a language, on every page.
    "doc-examples-no-language": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_a_fence_that_names_no_language_is_refused_on_every_page",
        holds=_whole(
            "docs/development/doc-examples.md",
            "every code fence names a language (text for output, a directory tree or anything else that is not code);",
            "An exempt page still obeys the rules for every page (languages named, no other shell) and its bash blocks still carry no comments and pass bash -n.",
        ),
    ),
    # A shell fence other than bash is refused, with its file and line.
    "doc-examples-other-shell": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_a_shell_fence_in_an_unknown_shell_language_is_refused",
        holds=_whole(
            "docs/development/doc-examples.md",
            "a shell fence is bash: sh, shell, console, zsh, powershell and the other shells the check knows are refused;",
            "Any other form of a shell fence on an enrolled page is refused, with the file and line.",
            "An exempt page still obeys the rules for every page (languages named, no other shell) and its bash blocks still carry no comments and pass bash -n.",
        ),
    ),
    # A page is in one of enrolled, exempt or pending.
    "doc-examples-one-list": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py::test_the_lists_are_disjoint",
        holds=_whole(
            "docs/development/doc-examples.md",
            "a page with a bash block is exactly one of enrolled, exempt or pending.",
            "The pending pages are the ones not yet enrolled.",
        ),
    ),
    # A page with a bash block in none of the lists is refused.
    "doc-examples-unenrolled-shell": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_a_document_dropped_with_its_count_is_refused",
        holds=_whole(
            "docs/development/doc-examples.md",
            "a page with a bash block is exactly one of enrolled, exempt or pending.",
        ),
    ),
    # A pending page is not enrolled: nothing on it runs.
    "doc-examples-pending-exec": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_an_exec_tag_on_a_pending_page_is_refused",
        holds=_whole(
            "docs/development/doc-examples.md",
            "The pending pages are the ones not yet enrolled.",
        ),
    ),
    # The pending list only shrinks.
    "doc-examples-pending-cannot-grow": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py::test_pending_cannot_grow",
        holds=_whole(
            "docs/development/doc-examples.md",
            "That list can only shrink, and a pending page's fences that name no language, its shell comment lines and its bash -n failures can only fall.",
        ),
    ),
    # A pending page's comment lines only fall.
    "doc-examples-pending-comments-fall": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_a_pending_residual_can_only_fall",
        holds=_whole(
            "docs/development/doc-examples.md",
            "That list can only shrink, and a pending page's fences that name no language, its shell comment lines and its bash -n failures can only fall.",
        ),
    ),
    # A pending page's unlabelled fences and bash -n failures only fall.
    "doc-examples-pending-residuals-fall": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_each_pending_residual_can_only_fall",
        holds=_whole(
            "docs/development/doc-examples.md",
            "That list can only shrink, and a pending page's fences that name no language, its shell comment lines and its bash -n failures can only fall.",
        ),
    ),
    # A skip reason starts with a category and ': '.
    "doc-examples-skip-reason": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_skip_reason_names_a_category",
        holds=_whole(
            "docs/development/doc-examples.md",
            "The reason is required, and it starts with a category (see Pages that cannot run here):",
            "A skip reason starts with one of these categories, then : and the words a reader of the source will see:",
            "bug #N | fails because of the product defect in issue N: bug #123: …",
        ),
    ),
    # exec's timeout: 1 to 3600, 120 by default.
    "doc-examples-timeout": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_an_exec_timeout_is_1_to_3600_seconds_and_120_when_not_given",
        holds=_whole(
            "docs/development/doc-examples.md",
            "exec takes an optional timeout=<seconds>, from 1 to 3600 (120 when it is not given), as in {.bash exec timeout=1200}.",
        ),
    ),
    # A malformed shell fence is refused with its file and line.
    "doc-examples-fence-line": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_a_malformed_shell_fence_on_an_enrolled_page_names_the_file_and_line",
        holds=_whole(
            "docs/development/doc-examples.md",
            "Any other form of a shell fence on an enrolled page is refused, with the file and line.",
        ),
    ),
    # A block in another language is not a block that runs.
    "doc-examples-other-languages": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_exec_and_skip_blocks_are_read_with_their_expectations",
        holds=_whole(
            "docs/development/doc-examples.md",
            "Blocks in other languages (json, python, javascript, …) aren't run and need no tag.",
        ),
    ),
    # A block that prints needs an expect line.
    "doc-examples-output-needs-expect": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_output_without_an_expectation_fails",
        holds=_whole(
            "docs/development/doc-examples.md",
            "A block that prints something needs at least one expect line, except the two blocks that start a stack, docker compose up -d --wait and EXPERIMENTLY_PROFILE=full docker compose up -d --wait.",
        ),
    ),
    # The two stack starts may print without one.
    "doc-examples-stack-starts-print": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_both_stack_starts_may_print_without_an_expectation",
        holds=_whole(
            "docs/development/doc-examples.md",
            "A block that prints something needs at least one expect line, except the two blocks that start a stack, docker compose up -d --wait and EXPERIMENTLY_PROFILE=full docker compose up -d --wait.",
        ),
    ),
    # The environment a page runs with.
    "doc-examples-environment": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_a_page_runs_with_only_path_home_user_tmpdir_and_dockers_settings",
        holds=_whole(
            "docs/development/doc-examples.md",
            "Nothing from your own environment is: each page runs with only PATH, HOME, USER, TMPDIR and Docker's own settings.",
        ),
    ),
    # Nothing else from the caller's environment reaches a page.
    "doc-examples-scrubbed": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_the_environment_is_scrubbed",
        holds=_whole(
            "docs/development/doc-examples.md",
            "Nothing from your own environment is: each page runs with only PATH, HOME, USER, TMPDIR and Docker's own settings.",
        ),
    ),
    # No # comment in a shell block.
    "doc-examples-comments": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_a_shell_comment_is_refused",
        holds=_whole(
            "docs/development/doc-examples.md",
            "No # comments in a shell block, skip blocks included (see Rewriting a comment).",
        ),
    ),
    # A comment in a skip block is refused too.
    "doc-examples-skip-comment": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_problems_are_annotated_in_github_actions",
        holds=_whole(
            "docs/development/doc-examples.md",
            "No # comments in a shell block, skip blocks included (see Rewriting a comment).",
        ),
    ),
    # A # that is not a comment is fine.
    "doc-examples-hash-allowed": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_a_hash_that_is_not_a_comment_is_allowed",
        holds=_whole(
            "docs/development/doc-examples.md",
            "A # inside quotes, in the middle of a word ($#, ${#x}, a URL's #anchor) or inside a here-document is not a comment and is fine.",
        ),
    ),
    # A # inside a here-document is fine.
    "doc-examples-heredoc-data": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_a_here_document_body_is_data",
        holds=_whole(
            "docs/development/doc-examples.md",
            "A # inside quotes, in the middle of a word ($#, ${#x}, a URL's #anchor) or inside a here-document is not a comment and is fine.",
        ),
    ),
    # bash -n on every block, fragment skips excepted.
    "doc-examples-bash-n-skip": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_bash_n_runs_on_skip_blocks_too_except_fragments",
        holds=_whole(
            "docs/development/doc-examples.md",
            "Every block passes bash -n, with bash 4.4 or later, the one that runs the examples; macOS's /bin/bash is 3.2 and is refused.",
            "A skip block whose reason is fragment: … is not syntax-checked, since it is a template to fill in.",
            "fragment | is a template to fill in, not runnable as written",
        ),
    ),
    # bash older than 4.4 is refused.
    "doc-examples-old-bash": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_bash_n_is_refused_with_a_bash_older_than_4_4",
        holds=_whole(
            "docs/development/doc-examples.md",
            "Every block passes bash -n, with bash 4.4 or later, the one that runs the examples; macOS's /bin/bash is 3.2 and is refused.",
        ),
    ),
    # What a block that runs may not do.
    "doc-examples-hidden-failures": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_blocks_that_hide_a_failure_or_escape_the_project_are_refused",
        holds=_whole(
            "docs/development/doc-examples.md",
            "Every block passes bash -n, with bash 4.4 or later, the one that runs the examples; macOS's /bin/bash is 3.2 and is refused.",
            "end its last line in , which would swallow what follows;",
            "mention COMPOSE_PROJECT_NAME or unset a COMPOSE_ setting, pass -p or --project-name to docker compose, run env -u or env -i, or remove Docker resources directly (docker rm, docker volume rm, … prune).",
        ),
    ),
    # export, declare, local and readonly X=$(...) are refused.
    "doc-examples-masked-keywords": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_a_masked_substitution_is_refused_for_each_keyword",
        holds=_whole(
            "docs/development/doc-examples.md",
            "write export X=$(command) (or declare, local, readonly), which reports success even when the command fails: assign first, then export;",
        ),
    ),
    # The package managers the page names are refused.
    "doc-examples-package-managers": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_the_package_managers_the_page_names_are_refused",
        holds=_whole(
            "docs/development/doc-examples.md",
            "run a package manager (pip, npm, yarn, go get, cargo install and the others the check lists), or aws, cdk or sam, however the command is written and even inside a here-document.",
        ),
    ),
    # aws, cdk and sam are refused.
    "doc-examples-aws": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_a_block_that_runs_may_not_reach_aws",
        holds=_whole(
            "docs/development/doc-examples.md",
            "run a package manager (pip, npm, yarn, go get, cargo install and the others the check lists), or aws, cdk or sam, however the command is written and even inside a here-document.",
        ),
    ),
    # However the command is written.
    "doc-examples-normalised": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_a_command_word_is_normalised_before_it_is_matched",
        holds=_whole(
            "docs/development/doc-examples.md",
            "run a package manager (pip, npm, yarn, go get, cargo install and the others the check lists), or aws, cdk or sam, however the command is written and even inside a here-document.",
        ),
    ),
    # Even inside a here-document.
    "doc-examples-heredoc-scanned": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_a_here_document_body_is_scanned_like_any_other_line",
        holds=_whole(
            "docs/development/doc-examples.md",
            "run a package manager (pip, npm, yarn, go get, cargo install and the others the check lists), or aws, cdk or sam, however the command is written and even inside a here-document.",
        ),
    ),
    # A plain docker compose down -v is allowed.
    "doc-examples-own-down": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_the_quick_starts_own_down_is_allowed",
        holds=_whole(
            "docs/development/doc-examples.md",
            "A plain docker compose down -v is allowed: each page runs under a compose project of its own.",
        ),
    ),
    # Each page runs under a compose project of its own.
    "doc-examples-own-project": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_each_page_runs_under_a_compose_project_of_its_own",
        holds=_whole(
            "docs/development/doc-examples.md",
            "A plain docker compose down -v is allowed: each page runs under a compose project of its own.",
        ),
    ),
    # The skip-reason categories the page's table lists.
    "doc-examples-categories": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_every_category_the_authoring_page_lists_is_accepted",
        holds=_whole(
            "docs/development/doc-examples.md",
            "A skip reason starts with one of these categories, then : and the words a reader of the source will see:",
            "checkout | clones or enters the repository (the runner is already inside one)",
            "dev | is a contributor's command in a development checkout (venv, make, pytest, this repository's own tooling)",
            "server | starts a long-running process",
            "aws | needs an AWS account",
            "idp | needs an identity-provider tenant (Cognito, SAML, OIDC)",
            "secret | needs a key, a password or a third-party account the reader holds",
            "registry | installs a package from a public registry; it must name the package manager",
            "toolchain | needs a language toolchain the runner doesn't use (Go, .NET, JVM, Swift, …)",
            "destructive | changes state a reader must choose to change (restore, rollback, delete)",
            "demo | needs the demo applications",
            "fragment | is a template to fill in, not runnable as written",
            "timing | gates on latency or throughput, which one run on a shared runner cannot hold: one command, run_load_tests.py without --sla report, and the page runs the same command with --sla report in an exec block",
            "bug #N | fails because of the product defect in issue N: bug #123: …",
        ),
    ),
    # A registry skip must run a package manager.
    "doc-examples-registry-skip": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_a_registry_skip_must_run_a_package_manager",
        holds=_whole(
            "docs/development/doc-examples.md",
            "registry | installs a package from a public registry; it must name the package manager",
        ),
    ),
    # A timing skip holds one command, the one that gates.
    "doc-examples-timing-one-command": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_a_timing_skip_holds_only_the_command_that_gates",
        holds=_whole(
            "docs/development/doc-examples.md",
            "timing | gates on latency or throughput, which one run on a shared runner cannot hold: one command, run_load_tests.py without --sla report, and the page runs the same command with --sla report in an exec block",
        ),
    ),
    # And the page runs it with --sla report.
    "doc-examples-timing-report-run": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_a_timing_skip_needs_the_same_command_run_with_sla_report",
        holds=_whole(
            "docs/development/doc-examples.md",
            "timing | gates on latency or throughput, which one run on a shared runner cannot hold: one command, run_load_tests.py without --sla report, and the page runs the same command with --sla report in an exec block",
        ),
    ),
    # The check prints the skipped blocks per category.
    "doc-examples-category-counts": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_the_check_prints_how_many_skipped_blocks_each_category_holds",
        holds=_whole(
            "docs/development/doc-examples.md",
            "The check prints how many skipped blocks each category holds.",
        ),
    ),
    # What each environment starts before a page runs.
    "doc-examples-environments": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_each_environment_starts_what_it_needs_before_the_page",
        holds=_whole(
            "docs/development/doc-examples.md",
            "bare | Docker is available and nothing is running. The page starts its own stack.",
            "stack | The Quick Start's docker compose up -d --wait has already run.",
            'stack-full | The full profile\'s EXPERIMENTLY_PROFILE=full docker compose up -d --wait has already run, and GET /api/v1/modules said "profile": "full". For a page about a module.',
        ),
    ),
    # Nothing may be running on the stack's ports.
    "doc-examples-preflight": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_a_busy_port_at_preflight_is_refused_naming_what_holds_it",
        holds=_whole(
            "docs/development/doc-examples.md",
            "bare | Docker is available and nothing is running. The page starts its own stack.",
        ),
    ),
    # stack-dev: the venv is checked before the stack starts.
    "doc-examples-stack-dev-venv": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_a_stack_dev_page_needs_the_dev_venv_before_its_stack_starts",
        holds=_whole(
            "docs/development/doc-examples.md",
            "stack-dev | As stack, and the repository root has a development virtual environment, venv/, with backend/requirements.txt installed, for a contributor's page (source venv/bin/activate). A page whose venv cannot import alembic, locust, backend.app.core.config fails before its stack starts.",
        ),
    ),
    # stack-full: GET /api/v1/modules must say full.
    "doc-examples-full-profile": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py::test_the_full_profile_probe",
        holds=_whole(
            "docs/development/doc-examples.md",
            'stack-full | The full profile\'s EXPERIMENTLY_PROFILE=full docker compose up -d --wait has already run, and GET /api/v1/modules said "profile": "full". For a page about a module.',
        ),
    ),
    # Counts, environments and paths of an entry.
    "doc-examples-enrolment": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_enrolment_mismatches_are_refused",
        holds=_whole(
            "docs/development/doc-examples.md",
            "local | Reserved for the demo applications, and refused as not implemented yet.",
            "The check prints the counts it finds, and fails naming both numbers when they differ.",
        ),
    ),
    # --only runs one page.
    "doc-examples-only": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py::test_only_runs_one_page",
        holds=_whole(
            "docs/development/doc-examples.md",
            "--only runs one page:",
        ),
    ),
    # The render check covers every page under docs/.
    "doc-examples-render-every-page": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_render_covers_a_docs_page_that_is_not_enrolled",
        holds=_whole(
            "docs/development/doc-examples.md",
            "The render check covers every page under docs/, enrolled or not: a fence the site doesn't show as code fails it on any page.",
        ),
    ),
    # An entry has exactly five keys.
    "doc-examples-five-keys": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_an_entry_has_exactly_the_five_keys",
        holds=_whole(
            "docs/development/doc-examples.md",
            "An entry has exactly these five keys: the page, its environment (see Environments), and its counts of exec blocks, skip blocks and expectations.",
        ),
    ),
    # The check prints each page's counts.
    "doc-examples-counts": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_the_check_prints_the_counts_it_finds",
        holds=_whole(
            "docs/development/doc-examples.md",
            "The check prints the counts it finds, and fails naming both numbers when they differ.",
        ),
    ),
    # A page with no bash block cannot be enrolled.
    "doc-examples-zero-fence": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_a_zero_fence_document_cannot_hold_a_place",
        holds=_whole(
            "docs/development/doc-examples.md",
            "A page with no bash block needs no entry, and enrolling one is refused.",
        ),
    ),
    # An exempt page: a reason, a bash block, the rules.
    "doc-examples-exempt": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_an_exempt_page_needs_a_reason_and_a_shell_block_and_obeys_the_rules",
        holds=_whole(
            "docs/development/doc-examples.md",
            "A page whose shell blocks are deliberately left untagged is listed as exempt, with a reason (1–200 characters), instead of enrolled:",
            "An exempt page still obeys the rules for every page (languages named, no other shell) and its bash blocks still carry no comments and pass bash -n.",
            "An exempt page with no bash block is refused.",
        ),
    ),
    # An exempt reason is 1-200 characters.
    "doc-examples-exempt-reason": Mechanism(
        "backend/tests/unit/scripts/test_doc_examples.py"
        "::test_an_exempt_reason_is_1_to_200_characters",
        holds=_whole(
            "docs/development/doc-examples.md",
            "A page whose shell blocks are deliberately left untagged is listed as exempt, with a reason (1–200 characters), instead of enrolled:",
        ),
    ),
    # The runner's default is --sla enforce.
    "load-test-default-enforce": Mechanism(
        "backend/tests/unit/scripts/test_load_test_runner.py"
        "::test_the_default_is_enforce",
        holds=_whole(
            "docs/performance/load-testing.md",
            "Run with --sla enforce (the runner's default), a test passes when all of the following hold simultaneously:",
        ),
    ),
    # A run that measured its targets passes.
    "load-test-measured-run-passes": Mechanism(
        "backend/tests/unit/scripts/test_load_test_runner.py"
        "::test_a_measured_run_passes",
        holds=_whole(
            "docs/performance/load-testing.md",
            "Run with --sla enforce (the runner's default), a test passes when all of the following hold simultaneously:",
        ),
    ),
    # A failed request fails the run, naming the endpoint.
    "load-test-failed-request": Mechanism(
        "backend/tests/unit/scripts/test_load_test_runner.py"
        "::test_a_failed_request_fails_the_run",
        holds=_whole(
            "docs/performance/load-testing.md",
            "no request failed (Locust exits 1 on any failed request, and the runner names the endpoints)",
            "It passes --sla report: it fails when a request fails, when nothing was recorded, or when a declared target was not recorded, and it prints each endpoint's p50, p95, p99 and requests per second beside its target without checking them.",
            "Locust counts a 429 as a failed request, so the run fails.",
            "It exits 1 when a request failed (ERROR: Locust exited with code 1, then one FAIL: line per endpoint with failures), and when the run measured nothing or not what it should: Locust recorded no requests (FAIL: no stats), no recorded endpoint matches a target in performance_targets.py (FAIL: no matched endpoint), or a target the locustfile declares in TARGETS was not recorded, or it declares none (FAIL: unmatched target).",
        ),
    ),
    # A declared target that was not recorded fails the run.
    "load-test-declared-target": Mechanism(
        "backend/tests/unit/scripts/test_load_test_runner.py"
        "::test_a_declared_target_that_was_not_recorded_fails",
        holds=_whole(
            "docs/performance/load-testing.md",
            "every target the locustfile declares in its TARGETS tuple was recorded",
            "It passes --sla report: it fails when a request fails, when nothing was recorded, or when a declared target was not recorded, and it prints each endpoint's p50, p95, p99 and requests per second beside its target without checking them.",
            "It exits 1 when a request failed (ERROR: Locust exited with code 1, then one FAIL: line per endpoint with failures), and when the run measured nothing or not what it should: Locust recorded no requests (FAIL: no stats), no recorded endpoint matches a target in performance_targets.py (FAIL: no matched endpoint), or a target the locustfile declares in TARGETS was not recorded, or it declares none (FAIL: unmatched target).",
            "Add the target's key to the locustfile's TARGETS tuple, so a run that does not record it fails",
        ),
    ),
    # A locustfile that declares no TARGETS fails.
    "load-test-no-target-list": Mechanism(
        "backend/tests/unit/scripts/test_load_test_runner.py"
        "::test_a_locustfile_without_a_target_list_fails",
        holds=_whole(
            "docs/performance/load-testing.md",
            "It exits 1 when a request failed (ERROR: Locust exited with code 1, then one FAIL: line per endpoint with failures), and when the run measured nothing or not what it should: Locust recorded no requests (FAIL: no stats), no recorded endpoint matches a target in performance_targets.py (FAIL: no matched endpoint), or a target the locustfile declares in TARGETS was not recorded, or it declares none (FAIL: unmatched target).",
        ),
    ),
    # A run that recorded nothing fails.
    "load-test-no-stats": Mechanism(
        "backend/tests/unit/scripts/test_load_test_runner.py::test_no_stats_fails",
        holds=_whole(
            "docs/performance/load-testing.md",
            "It passes --sla report: it fails when a request fails, when nothing was recorded, or when a declared target was not recorded, and it prints each endpoint's p50, p95, p99 and requests per second beside its target without checking them.",
            "It exits 1 when a request failed (ERROR: Locust exited with code 1, then one FAIL: line per endpoint with failures), and when the run measured nothing or not what it should: Locust recorded no requests (FAIL: no stats), no recorded endpoint matches a target in performance_targets.py (FAIL: no matched endpoint), or a target the locustfile declares in TARGETS was not recorded, or it declares none (FAIL: unmatched target).",
            "Without the with block Locust sends the request and never records it",
        ),
    ),
    # A run that matched no target fails.
    "load-test-no-matched-endpoint": Mechanism(
        "backend/tests/unit/scripts/test_load_test_runner.py"
        "::test_no_matched_endpoint_fails",
        holds=_whole(
            "docs/performance/load-testing.md",
            "It exits 1 when a request failed (ERROR: Locust exited with code 1, then one FAIL: line per endpoint with failures), and when the run measured nothing or not what it should: Locust recorded no requests (FAIL: no stats), no recorded endpoint matches a target in performance_targets.py (FAIL: no matched endpoint), or a target the locustfile declares in TARGETS was not recorded, or it declares none (FAIL: unmatched target).",
        ),
    ),
    # The weekly workflow passes --sla report.
    "load-test-weekly-report": Mechanism(
        "backend/tests/unit/scripts/test_load_test_runner.py"
        "::test_report_is_the_weekly_workflows_mode_on_its_run_line",
        holds=_whole(
            "docs/performance/load-testing.md",
            "The weekly Performance Tests workflow does not gate on latency or throughput.",
            "It passes --sla report: it fails when a request fails, when nothing was recorded, or when a declared target was not recorded, and it prints each endpoint's p50, p95, p99 and requests per second beside its target without checking them.",
        ),
    ),
    # Over budget: enforce fails, report passes.
    "load-test-enforce-or-report": Mechanism(
        "backend/tests/unit/scripts/test_load_test_runner.py"
        "::test_one_over_budget_csv_fails_under_enforce_and_passes_under_report",
        holds=_whole(
            "docs/performance/load-testing.md",
            "The weekly Performance Tests workflow does not gate on latency or throughput.",
            "It passes --sla report: it fails when a request fails, when nothing was recorded, or when a declared target was not recorded, and it prints each endpoint's p50, p95, p99 and requests per second beside its target without checking them.",
            "run_load_tests.py runs Locust headless and judges the result.",
            "Without --sla report it also exits 1 when an endpoint misses a target.",
        ),
    ),
    # A target matches by method and name exactly.
    "load-test-exact-match": Mechanism(
        "backend/tests/unit/scripts/test_load_test_runner.py"
        "::test_an_endpoint_matches_a_target_by_method_and_name_exactly",
        holds=_whole(
            "docs/performance/load-testing.md",
            "A recorded endpoint matches a target when its method and its Locust name= are the target's method and endpoint exactly.",
        ),
    ),
    # Every target is a route in the OpenAPI document.
    "load-test-target-routes": Mechanism(
        "backend/tests/unit/scripts/test_load_test_runner.py"
        "::test_every_target_names_one_route_the_api_serves",
        holds=_whole(
            "docs/performance/load-testing.md",
            "Add a PerformanceTarget entry to PERFORMANCE_TARGETS in performance_targets.py, for a route the API serves (a unit test checks every target against the OpenAPI document)",
        ),
    ),
    # The locustfiles call the seed's experiment and flag.
    "load-test-seed-keys": Mechanism(
        "backend/tests/unit/scripts/test_load_test_runner.py"
        "::test_the_locustfiles_use_the_seeds_experiment_and_flag",
        holds=_whole(
            "docs/performance/load-testing.md",
            "Seed what the locustfiles call: the experiment sdk_contract_ab, the flag sdk_contract_flag, an API key, and the developer account the management tasks sign in as (dev@demo.com, not a superuser).",
        ),
    ),
    # The load-testing page gives no figure.
    "load-testing-no-figures": Mechanism(
        "backend/tests/unit/docs/test_load_testing_page.py"
        "::test_the_page_publishes_no_response_time_or_throughput",
        holds=_whole(
            "docs/performance/load-testing.md",
            "This page publishes no response time or throughput.",
        ),
    ),
    # The environment each of the three pages runs in.
    "migration-pages-environments": Mechanism(
        "backend/tests/unit/docs/test_migration_pages.py"
        "::test_stack_dev_carries_the_two_contributor_pages_and_the_operator_page_is_stack",
        holds=_whole(
            "docs/performance/load-testing.md",
            "CI runs this section and Running Unit Tests for the Load Test Framework as written, against the Quick Start stack.",
        )
        + _whole(
            "docs/self-hosting/migrations.md",
            "CI runs this page's exec blocks as written against the Quick Start stack.",
        ),
    ),
    # The page's AWS block runs the task as the Deploy does.
    "migration-page-aws-block": Mechanism(
        "backend/tests/unit/docs/test_migration_pages.py"
        "::test_the_operator_page_runs_the_migration_task_as_the_deploy_does",
        holds=(
            (
                "docs/self-hosting/migrations.md",
                r"^By hand, from a checkout with AWS [a-z]{1,20} for the environment,"
                r" the Deploy workflow's two steps are these\.$",
            ),
        ),
    ),
    # Every alembic and bootstrap command of the operator page runs in the API
    # container.
    "migration-page-alembic-in-the-api": Mechanism(
        "backend/tests/unit/docs/test_migration_pages.py"
        "::test_every_alembic_and_bootstrap_command_runs_in_the_api_container",
        holds=_whole(
            "docs/self-hosting/migrations.md",
            "Every alembic and bootstrap command on this page runs inside the API container, with the image's own alembic and the container's POSTGRES_ settings: docker compose exec api ..., from the directory of your compose file (the repository root for the Quick Start stack, deploy/compose/ for the production compose file).",
        ),
    ),
    # heads, and the order upgrade heads applies, in a full checkout.
    "alembic-plan-full": Mechanism(
        "backend/tests/unit/db/test_alembic_plan.py"
        "::test_a_full_checkout_has_two_heads_and_this_plan",
        holds=_whole(
            "docs/self-hosting/migrations.md",
            "heads refers to the latest migration of every branch.",
            "This command applies all unapplied migrations in order.",
        ),
    ),
    # The order upgrade heads applies in a core checkout.
    "alembic-plan-core": Mechanism(
        "backend/tests/unit/db/test_alembic_plan.py"
        "::test_a_core_checkout_has_one_head_and_this_plan",
        holds=_whole(
            "docs/self-hosting/migrations.md",
            "This command applies all unapplied migrations in order.",
        ),
    ),
    # The modules branch: its two revisions, in order.
    "alembic-plan-branch": Mechanism(
        "backend/tests/unit/db/test_alembic_plan.py"
        "::test_the_core_plan_is_the_full_plan_without_the_branch",
        holds=_whole(
            "docs/self-hosting/migrations.md",
            "The modules branch has two revisions: modules_0001_rbac and, after it, modules_0002_warehouse_analysis.",
        ),
    ),
    # modules_0001_rbac runs after a7b8c9d0e1f2.
    "alembic-plan-branch-point": Mechanism(
        "backend/tests/unit/db/test_alembic_plan.py"
        "::test_the_branch_runs_after_the_revision_it_undoes",
        holds=_whole(
            "docs/self-hosting/migrations.md",
            "modules_0001_rbac is a child of the core revision a7b8c9d0e1f2, not an alembic base, and with a single tree root alembic cannot filter a downgrade by branch label: downgrade modules@base resolves to the whole core chain to base, and drops every table in the schema.",
        ),
    ),
    # modules@-1 is one revision; modules@base is the whole chain.
    "alembic-plan-unapply": Mechanism(
        "backend/tests/unit/db/test_alembic_plan.py"
        "::test_unapplying_the_branch_is_one_revision_and_modules_at_base_is_all_of_them",
        holds=_whole(
            "docs/self-hosting/migrations.md",
            "Roll back the most recent migration of one branch.",
            "The modules branch has two revisions: modules_0001_rbac and, after it, modules_0002_warehouse_analysis.",
            "Not modules@base.",
            "modules_0001_rbac is a child of the core revision a7b8c9d0e1f2, not an alembic base, and with a single tree root alembic cannot filter a downgrade by branch label: downgrade modules@base resolves to the whole core chain to base, and drops every table in the schema.",
        ),
    ),
    # The page's holdout query orders rows as d29a479daafe keeps one.
    "holdout-runbook-query": Mechanism(
        "backend/tests/unit/docs/test_holdout_runbook_query.py"
        "::test_the_page_query_orders_the_active_holdouts_as_the_revision_keeps_one",
        holds=_whole(
            "docs/self-hosting/migrations.md",
            "Before upgrading, this read-only query lists the active holdouts; with more than one row, the first is the one kept:",
        ),
    ),
    # The runbook's command that checks a revision by hand.
    "rollback-runbook-revision-check": Mechanism(
        "backend/tests/unit/infrastructure/test_rollback_runbook_env_check.py"
        "::test_the_runbook_query_returns_the_backend_containers_value",
        holds=_whole(
            "docs/self-hosting/migrations.md",
            "Rollback does not check its target in the same way; the runbook gives the command to check one by hand.",
        ),
    ),
    # The Deploy checks the API revision it registered.
    "deploy-refuses-migrating-revision": Mechanism(
        "backend/tests/unit/infrastructure/test_refuse_migrating_api_revision.py"
        "::test_it_runs_after_the_registration_and_immediately_before_create_deployment",
        holds=_whole(
            "docs/self-hosting/migrations.md",
            "The Deploy workflow checks the revision it is about to deploy.",
            "After it registers the API revision it reads back what ECS stored, and refuses to create the CodeDeploy deployment unless the backend container sets RUN_MIGRATIONS=false exactly.",
        ),
    ),
    # It reads back what ECS stored.
    "deploy-reads-the-stored-revision": Mechanism(
        "backend/tests/unit/infrastructure/test_refuse_migrating_api_revision.py"
        "::test_the_stored_revision_is_checked_not_the_json_sent",
        holds=_whole(
            "docs/self-hosting/migrations.md",
            "After it registers the API revision it reads back what ECS stored, and refuses to create the CodeDeploy deployment unless the backend container sets RUN_MIGRATIONS=false exactly.",
        ),
    ),
    # Anything but RUN_MIGRATIONS=false exactly is refused.
    "deploy-refuses-anything-but-false": Mechanism(
        "backend/tests/unit/infrastructure/test_refuse_migrating_api_revision.py"
        "::test_anything_but_exactly_false_refuses_and_names_the_fix",
        holds=_whole(
            "docs/self-hosting/migrations.md",
            "After it registers the API revision it reads back what ECS stored, and refuses to create the CodeDeploy deployment unless the backend container sets RUN_MIGRATIONS=false exactly.",
        ),
    ),
    # The Deploy refuses while one of the API's 5xx alarms is in ALARM.
    "deploy-refuses-on-an-api-alarm": Mechanism(
        "backend/tests/unit/infrastructure/test_refuse_alarm_active.py"
        "::test_the_step_refuses_on_alarm_as_written",
        holds=_whole(
            "docs/self-hosting/migrations.md",
            "The first Deploy may be refused by the API's 5xx alarms.",
            "Anything that reaches the schema-less API in that window, a scanner included, can put experimentation-api-5xx-blue-<env> or -green-<env> into ALARM, and the Deploy refuses to start while one is.",
        ),
    ),
    # ... before it builds anything.
    "deploy-alarm-check-runs-first": Mechanism(
        "backend/tests/unit/infrastructure/test_refuse_alarm_active.py"
        "::test_it_runs_before_the_build_and_immediately_before_the_snapshot",
        holds=_whole(
            "docs/self-hosting/migrations.md",
            "Anything that reaches the schema-less API in that window, a scanner included, can put experimentation-api-5xx-blue-<env> or -green-<env> into ALARM, and the Deploy refuses to start while one is.",
        ),
    ),
    # ix_users_email_lower refuses a second account with the address.
    "email-lower-collision": Mechanism(
        "backend/tests/integration/database/test_users_email_lower_migration.py"
        "::test_after_the_upgrade_a_case_only_collision_is_refused",
        holds=_whole(
            "docs/self-hosting/migrations.md",
            "From this release an email address belongs to one account whatever its letter case: Bob@acme.com and bob@acme.com are the same address, and the database refuses a second account holding it (the unique index ix_users_email_lower).",
        ),
    ),
    # The refusal names account ids only.
    "email-lower-no-address": Mechanism(
        "backend/tests/integration/database/test_users_email_lower_migration.py"
        "::test_the_refusal_names_no_address_domain_or_username",
        holds=_whole(
            "docs/self-hosting/migrations.md",
            "The message names account ids only, never an address.",
        ),
    ),
    # At most 20 groups, then a count.
    "email-lower-twenty-groups": Mechanism(
        "backend/tests/integration/database/test_users_email_lower_migration.py"
        "::test_more_than_twenty_groups_are_capped_with_a_count",
        holds=_whole(
            "docs/self-hosting/migrations.md",
            "It lists at most 20 groups, then ...",
        ),
    ),
    # The listing's email column is the address as stored.
    "email-runbook-listing": Mechanism(
        "backend/tests/unit/docs/test_email_case_runbook.py"
        "::test_the_listing_selects_the_address_as_stored",
        holds=_whole(
            "docs/self-hosting/migrations.md",
            "Save this output somewhere private before you change anything: its email column is the record of each address as it was, which is what you need to undo a step by hand.",
        ),
    ),
    # The runbook's SQL retires an account and fits the domain limits.
    "email-runbook-resolves": Mechanism(
        "backend/tests/integration/database/test_users_email_lower_migration.py"
        "::test_the_runbook_sql_resolves_a_refused_upgrade",
        holds=_whole(
            "docs/self-hosting/migrations.md",
            "It keeps its history and everything it owns, but it can no longer sign in and no longer holds the address.",
            "email holds at most 100 characters, and retired-<id>@ takes 45 of them, so this works when the domain is at most 55 characters.",
        ),
    ),
    # An index built by hand and left invalid is refused.
    "email-lower-invalid-index": Mechanism(
        "backend/tests/integration/database/test_users_email_lower_migration.py"
        "::test_an_invalid_index_left_by_a_failed_concurrent_build_is_refused",
        holds=_whole(
            "docs/self-hosting/migrations.md",
            "Do not build the index by hand.",
        ),
    ),
    # An index of that name in another shape is refused.
    "email-lower-index-shape": Mechanism(
        "backend/tests/integration/database/test_users_email_lower_migration.py"
        "::test_an_index_of_that_name_in_another_shape_is_refused",
        holds=_whole(
            "docs/self-hosting/migrations.md",
            "Do not build the index by hand.",
        ),
    ),
    # The upgrade runs in one transaction.
    "email-lower-one-transaction": Mechanism(
        "backend/tests/integration/database/test_users_email_lower_migration.py"
        "::test_a_refusal_rolls_back_an_earlier_pending_revision",
        holds=_whole(
            "docs/self-hosting/migrations.md",
            "Each file represents one atomic schema change, identified by a unique revision ID.",
            "Run the upgrade, which locks the table, checks for groups and builds the index in one transaction.",
        ),
    ),
    # The upgrade locks users and waits at most its lock timeout.
    "email-lower-lock": Mechanism(
        "backend/tests/integration/database/test_users_email_lower_migration.py"
        "::test_a_writer_holding_users_ends_the_upgrade_after_the_lock_timeout",
        holds=_whole(
            "docs/self-hosting/migrations.md",
            "Run the upgrade, which locks the table, checks for groups and builds the index in one transaction.",
            "The upgrade waits at most 30 seconds for the lock on users, so that it never holds up the API's writes for long.",
        ),
    ),
    # The lock timeout is back to its default after.
    "email-lower-lock-reset": Mechanism(
        "backend/tests/integration/database/test_users_email_lower_migration.py"
        "::test_lock_timeout_is_back_to_its_default_after_the_lock",
        holds=_whole(
            "docs/self-hosting/migrations.md",
            "The upgrade waits at most 30 seconds for the lock on users, so that it never holds up the API's writes for long.",
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
CLAIMS_PENDING: Tuple[str, ...] = ()


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

    def document_text(self, document: "Document") -> Optional[str]:
        """What the splitter reads of *document*: its file, through its
        extractor (a surface's file is the path before ``#``); None when the
        file is missing. SplitError for an extractor this file does not have."""
        text = self.text(document.path.partition("#")[0])
        if text is None:
            return None
        return extracted(document.path, document.extractor, text)

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


def docs_reads_problem(text: Optional[str], tree: Any) -> Optional[str]:
    """Why a test in API_SNAPSHOT_TESTS no longer looks like a snapshot reader.

    Exactly two checks: nothing in the file names a page (``.md``) or mkdocs,
    and its only literal containing "docs" (docstrings and other bare string
    statements aside) is one ``"docs"``. A guard against drift, not one an
    author cannot get round: a docs path built without such a literal passes."""
    if text is None or not isinstance(tree, ast.Module):
        return "does not exist or does not parse"
    if _NAMES_A_PAGE.search(text):
        return "names a page (.md) or mkdocs"
    bare = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Expr)}
    found = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and "docs" in str(n.value) and id(n) not in bare
    ]
    if found != ["docs"]:
        return f"has string literals naming docs {found[:3]}, not one 'docs'"
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
    snapshot = path in API_SNAPSHOT_TESTS
    if snapshot and not mechanism:
        problem = docs_reads_problem(ctx.text(path), ctx.tree(path))
        if problem:
            return (
                f"{path} {problem}: it reads documentation, so a claim reaches it"
                " only as mechanism:<name>"
            )
    if not mechanism and not snapshot and ctx.reads_docs(path):
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
        if not evidence_url(target[len("evidence:") :]):
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
    if not has_extractor(document.extractor):
        return problems + [
            f"{rel}: scripts/doc_claims.py has no {document.extractor!r} extractor yet;"
            f" {document.path} stays in CLAIMS_PENDING until it has"
        ], []
    try:
        text = ctx.text(document.path.partition("#")[0])
        if text is None:
            return problems + [f"{rel}: {document.path} does not exist"], []
        on_page = document_units(document.path, document.extractor, text)
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
# The generator, sync and the baseline check (developer tools)
# ---------------------------------------------------------------------------
#: An audit row's tag: uncovered and testable as is (T), with a stack (S), on
#: AWS (A), on a device (D); reword it (R); not verifiable before launch (X).
AUDIT_TAGS: Tuple[Optional[str], ...] = (None, "T", "S", "A", "D", "R", "X")
#: Debt kinds, heaviest first: a unit with rows of two kinds takes the first.
DEBT: Tuple[str, ...] = ("A", "S", "T")
#: What a page may not keep: a registry is not written while one is on it.
MUST_GO: Tuple[str, ...] = ("R", "X")
SNAPSHOT_TEST = "backend/tests/smoke/test_openapi_snapshot.py"
SNAPSHOT_TARGETS: Dict[str, str] = {
    "stable": f"test:{SNAPSHOT_TEST}::test_core_api_matches_stable_snapshot",
    "full": f"test:{SNAPSHOT_TEST}::test_full_api_matches_full_snapshot",
}
#: Shortest excerpt fragment that picks a unit among those on its line, and how
#: far from its line an excerpt is looked for.
FRAGMENT_MIN = 4
LINE_SLACK = 2
#: ``sync``: a registered unit this close to a changed one is printed beside it.
NEAR = 0.9
NEAR_INPUT = 2000
SOURCE_SUFFIXES: Tuple[str, ...] = (
    ".py",
    ".sh",
    ".bash",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".go",
    ".rb",
    ".php",
    ".java",
    ".kt",
    ".swift",
    ".dart",
    ".ex",
    ".exs",
    ".cs",
    ".rs",
)
#: Directories the basename index does not walk.
NOT_WALKED = {
    ".git",
    ".next",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "site",
    "venv",
}
GIT_SHA = re.compile(r"[0-9a-f]{7,40}")

_NAME = r"[A-Za-z_][A-Za-z0-9_]{0,200}"
_PARAMS = r"(?:\[[^\]\n]{0,200}\])?"
_TEST_NAME = r"(?:Test[A-Za-z0-9_]{0,200}::)?[Tt]est[A-Za-z0-9_]{0,200}" + _PARAMS
_EXTENSIONS = (
    "py|pyi|tsx|ts|jsx|js|mjs|cjs|go|rb|php|java|kts|kt|swift|dart|exs|ex|cs|rs"
    "|bash|sh|yml|yaml|json|toml|md|txt|cfg|ini"
)
_LINES = r"[0-9]{1,6}(?:-[0-9]{1,6})?(?:,[0-9]{1,6}(?:-[0-9]{1,6})?){0,20}"
#: ``path.py (test_a, test_b)``: the names in the brackets are the tests.
_PAREN_NAMES = re.compile(
    r"(?P<path>[A-Za-z0-9_./@+-]{1,300}\.py)[ \t]{0,3}\((?P<names>"
    + _TEST_NAME
    + r"(?:[ \t]{0,3},[ \t]{0,3}"
    + _TEST_NAME
    + r"){0,40})\)"
)
_PAREN = re.compile(r"\([^()\n]{0,400}\)")
_DOCEX_REF = re.compile(
    r"doc-examples[ \t]{1,3}(?P<file>[A-Za-z0-9_./@+-]{0,300}\.md)(?![A-Za-z0-9_])"
    r"(?::(?P<lines>" + _LINES + r"))?"
)
_MORE_LINES = re.compile(r"(?<![\w/.:]):(?P<lines>" + _LINES + r")(?![\w:])")
_JOURNEY_REF = re.compile(
    r"journey[ \t]{1,3}(?P<slug>" + SLUG + r")"
    r"(?P<steps>(?:[ \t]{0,3},?[ \t]{0,3}#" + SLUG + r"(?:," + SLUG + r"){0,40}){0,10})"
)
_PATH_TOKEN = re.compile(
    r"(?<![\w./@+-])(?P<path>(?:[\w.@+-]{1,100}/){0,20}[\w@+-][\w.@+-]{0,100}"
    r"\.(?:" + _EXTENSIONS + r"))(?![\w])"
    r"(?::[0-9]{1,6}(?:[-,][0-9]{1,6}){0,20})?"
    r"(?:::(?P<names>"
    + _NAME
    + r"(?:::"
    + _NAME
    + r")?"
    + _PARAMS
    + r"(?:[ \t]{0,3},[ \t]{0,3}(?:::)?"
    + _TEST_NAME
    + r"){0,40}))?"
)
_OPENAPI = re.compile(r"openapi-v1\.(?:\*|stable|full)\.json|^[ \t]{0,3}snapshot\b")
_SPLIT_NAMES = re.compile(r"[ \t]{0,3},[ \t]{0,3}")
_PARAM_TAIL = re.compile(r"\[[^\]\n]{0,200}\]$")
_ELLIPSIS = re.compile("\\.\\.\\.|\u2026")


@dataclasses.dataclass(frozen=True)
class Row:
    """One flow or claim of the audit, where it stood and what held it."""

    doc: str
    kind: str  # "flow" or "claim"
    line: int  # 1-based, in the document at the audit commit
    text: str  # the auditor's excerpt
    covered: bool
    by: Optional[str]
    tag: Optional[str]

    def label(self) -> str:
        tag = f" {self.tag}" if self.tag else ""
        return f"{self.doc}:{self.line} {self.kind}{tag} {self.text[:120]!r}"


def _row_of(data: Any) -> Any:
    """A Row, or what is wrong with the object."""
    if not isinstance(data, dict):
        return "not an object"
    line = data.get("line")
    checks = (
        (isinstance(data.get("doc"), str) and data.get("doc"), "doc is a path"),
        (data.get("kind") in ("flow", "claim"), "kind is flow or claim"),
        (
            isinstance(line, int) and not isinstance(line, bool) and line >= 1,
            "line is a number from 1",
        ),
        (isinstance(data.get("text"), str), "text is a string"),
        (isinstance(data.get("covered"), bool), "covered is true or false"),
        (data.get("by") is None or isinstance(data.get("by"), str), "by is a string"),
        (data.get("tag") in AUDIT_TAGS, "tag is null or one of T S A D R X"),
    )
    for ok, problem in checks:
        if not ok:
            return problem
    return Row(
        data["doc"],
        data["kind"],
        line,
        data["text"],
        data["covered"],
        data.get("by"),
        data.get("tag"),
    )


def load_audit(paths: Sequence[pathlib.Path]) -> Tuple[List[Row], List[str]]:
    """The rows of the audit's JSON Lines files, and every line that is not one."""
    rows: List[Row] = []
    problems: List[str] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").split("\n")
        except OSError as error:
            problems.append(f"{path}: {error}")
            continue
        for number, line in zip(range(1, len(lines) + 1), lines):
            if not line.strip():
                continue
            try:
                row = _row_of(json.loads(line))
            except ValueError:
                row = "not JSON"
            if isinstance(row, Row):
                rows.append(row)
            else:
                problems.append(f"{path.name}:{number}: {row}")
    return rows, problems


def git_reader(root: pathlib.Path, sha: str) -> Any:
    """A function giving a document's text at commit *sha* (None if absent)."""
    if not GIT_SHA.fullmatch(sha):
        raise ValueError(f"{sha!r} is not a commit id (7-40 lowercase hex digits)")
    found = subprocess.run(
        ["git", "-C", str(root), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True,
    )
    if found.returncode != 0:
        raise ValueError(f"{sha} is not a commit of {root} (a shallow clone?)")
    cache: Dict[str, Optional[str]] = {}

    def read(path: str) -> Optional[str]:
        if path not in cache:
            shown = subprocess.run(
                ["git", "-C", str(root), "show", f"{sha}:{path}"],
                capture_output=True,
            )
            cache[path] = (
                shown.stdout.decode("utf-8", "replace")
                if shown.returncode == 0
                else None
            )
        return cache[path]

    return read


@dataclasses.dataclass
class Mapped:
    """What one row's ``by`` names: the targets that resolve in this tree, what
    to map by hand (with why), and what is refused (a script or application
    source, which is not a test)."""

    targets: List[str] = dataclasses.field(default_factory=list)
    by_hand: List[str] = dataclasses.field(default_factory=list)
    refused: List[str] = dataclasses.field(default_factory=list)


class Resolver:
    """Turns an audit row's ``by`` text into registry targets for one tree."""

    def __init__(self, ctx: Context, old_text: Any) -> None:
        self.ctx = ctx
        self.old_text = old_text
        self._index: Optional[Dict[str, List[str]]] = None
        self._old_blocks: Dict[str, Any] = {}
        #: The document whose rows are being mapped (a workflow target needs it).
        self.document = Document("", "", MARKDOWN)

    # -- lookups -----------------------------------------------------------
    def index(self) -> Dict[str, List[str]]:
        """Every test file of the tree, by file name."""
        if self._index is None:
            found: Dict[str, List[str]] = collections.defaultdict(list)
            for directory, dirs, files in os.walk(self.ctx.root):
                dirs[:] = sorted(d for d in dirs if d not in NOT_WALKED)
                for name in sorted(files):
                    if TEST_FILE.search(name):
                        rel = (pathlib.Path(directory) / name).relative_to(
                            self.ctx.root
                        )
                        found[name].append(rel.as_posix())
            self._index = dict(found)
        return self._index

    def resolve(self, path: str) -> Optional[str]:
        """The repository path of a test file the audit named, or None."""
        if (self.ctx.root / path).is_file():
            return path
        name = path.rsplit("/", 1)[-1]
        found = [
            p for p in self.index().get(name, []) if p == name or p.endswith("/" + path)
        ]
        return found[0] if len(found) == 1 else None

    def methods(self, path: str, name: str) -> List[str]:
        """``Class::name`` when exactly one test class of *path* has a method
        *name* (the audit often names a method without its class)."""
        tree = (
            self.ctx.tree(path) if path.endswith(".py") and "::" not in name else None
        )
        if not isinstance(tree, ast.Module):
            return []
        owners = [
            node.name
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name.startswith("Test")
            and any(
                isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))
                and s.name == name
                for s in node.body
            )
        ]
        return [f"{owners[0]}::{name}"] if len(owners) == 1 else []

    def valid(self, target: str, mapped: Mapped, why: str) -> None:
        problem = target_problem(self.ctx, self.document, {"text": ""}, target)
        if problem is None:
            mapped.targets.append(target)
        else:
            mapped.by_hand.append(f"{why} -> {target}: {problem}")

    # -- the parts of a ``by`` ---------------------------------------------
    def targets(self, by: str) -> Mapped:
        mapped = Mapped()
        for part in by.split(";"):
            if part.strip():
                self._part(part.strip(), mapped)
        mapped.targets = list(dict.fromkeys(mapped.targets))
        return mapped

    def _part(self, raw: str, mapped: Mapped) -> None:
        text = _PAREN_NAMES.sub(lambda m: f"{m['path']}::{m['names']}", raw)
        for _ in range(3):
            text = _PAREN.sub(" ", text)
        named = False
        docex = _DOCEX_REF.search(text)
        if docex:
            named = True
            tail = text[docex.end() :]
            specs = [docex["lines"]] if docex["lines"] else []
            specs += [m["lines"] for m in _MORE_LINES.finditer(tail)]
            self._docex(docex["file"], specs, mapped)
            text = text[: docex.start()] + " " + _MORE_LINES.sub(" ", tail)
        for journey in _JOURNEY_REF.finditer(text):
            named = True
            steps = [s for s in re.split(r"[ \t,#]{1,20}", journey["steps"]) if s]
            if not steps:
                mapped.by_hand.append(f"{journey[0]!r}: names no step of the journey")
            for step in steps:
                self.valid(f"journey:{journey['slug']}#{step}", mapped, journey[0])
        text = _JOURNEY_REF.sub(" ", text)
        snapshot = False
        for token in _PATH_TOKEN.finditer(text):
            named = True
            snapshot |= self._path(token["path"], token["names"], raw, mapped)
        if not snapshot and _OPENAPI.search(text):
            named = True
            self.valid(SNAPSHOT_TARGETS[_which(raw)], mapped, raw)
        if not named:
            mapped.by_hand.append(
                f"{raw!r}: names no test, journey step, Doc Examples block or workflow"
            )

    def _path(self, path: str, names: Optional[str], raw: str, mapped: Mapped) -> bool:
        """Map one path the audit named; True when it is the OpenAPI snapshot."""
        name = path.rsplit("/", 1)[-1]
        if re.fullmatch(r"openapi-v1\.(?:stable|full)\.json", name) or (
            name == SNAPSHOT_TEST.rsplit("/", 1)[-1] and not names
        ):
            which = name.split(".")[1] if name.endswith(".json") else _which(raw)
            self.valid(SNAPSHOT_TARGETS[which], mapped, raw)
            return True
        if path.startswith(".github/workflows/") or (
            "/" not in path and (self.ctx.root / ".github/workflows" / path).is_file()
        ):
            if path.endswith(".yml"):
                self.valid(f"workflow:{name}", mapped, raw)
            else:
                mapped.by_hand.append(f"{path}: a workflow target names a .yml file")
            return False
        if path.startswith(NOT_TESTS) or (
            path.endswith(SOURCE_SUFFIXES) and not TEST_FILE.search(path)
        ):
            mapped.refused.append(
                f"{path}: a script or application source is not a test"
            )
            return False
        if not TEST_FILE.search(path):
            mapped.by_hand.append(
                f"{path}: not a test, a journey step, a Doc Examples block or a workflow"
            )
            return False
        resolved = None if ".." in path.split("/") else self.resolve(path)
        if resolved is None:
            mapped.by_hand.append(f"{path}: no such test file here, or several")
            return False
        if not names:
            self.valid(f"test:{resolved}", mapped, raw)
            return False
        listed = [
            _PARAM_TAIL.sub("", n).removeprefix("::")
            for n in _SPLIT_NAMES.split(names)
            if n.strip(":")
        ]
        owner = listed[0].split("::")[0] if "::" in listed[0] else None
        for number, test in zip(range(len(listed)), listed):
            tries = [test]
            if number and owner and "::" not in test:
                tries.insert(0, f"{owner}::{test}")
            tries += self.methods(resolved, test)
            problems = []
            for attempt in tries:
                target = f"test:{resolved}::{attempt}"
                problem = target_problem(self.ctx, self.document, {"text": ""}, target)
                if problem is None:
                    mapped.targets.append(target)
                    break
                problems.append(f"{target}: {problem}")
            else:
                mapped.by_hand.append(f"{raw!r} -> {problems[0]}")
        return False

    def _docex(self, file: str, specs: Sequence[str], mapped: Mapped) -> None:
        """``doc-examples <page>:<line>``: the block there, named by an expectation
        that exactly one exec block of the page carries today."""
        where = f"doc-examples {file}"
        enrolled = self.ctx.enrolled()
        if isinstance(enrolled, str):
            mapped.by_hand.append(f"{where}: {enrolled}")
            return
        path = file if file in enrolled else None
        if path is None:
            found = [p for p in sorted(enrolled) if p == file or p.endswith("/" + file)]
            path = found[0] if len(found) == 1 else None
        if path is None:
            mapped.by_hand.append(f"{where}: not one document Doc Examples enrols")
            return
        if not specs:
            mapped.by_hand.append(f"{where}: names no block (no line)")
            return
        old = self.old_blocks(path)
        if isinstance(old, str):
            mapped.by_hand.append(f"{where}: {old}")
            return
        now = self.ctx.exec_blocks(path)
        if isinstance(now, str):
            mapped.by_hand.append(f"{where}: {now}")
            return
        for spec in specs:
            for low, high in _ranges(spec):
                chosen = [b for start, end, b in old if start <= high and low <= end]
                runs = [b for b in chosen if b is not None and b.kind == "exec"]
                if not runs:
                    mapped.by_hand.append(
                        f"{where}:{spec}: no block that ran at the audit commit"
                    )
                for block in runs:
                    expect = next(
                        (
                            e
                            for e in block.expects
                            if sum(e in b.expects for b in now) == 1
                        ),
                        None,
                    )
                    if expect is None:
                        mapped.by_hand.append(
                            f"{where}:{block.line}: "
                            + (
                                "no expectation of that block names exactly one"
                                " block today"
                                if block.expects
                                else "the block has no expect line, so a docex"
                                " target cannot name it"
                            )
                        )
                        continue
                    self.valid(f"docex:{path}#{expect}", mapped, where)

    def old_blocks(self, path: str) -> Any:
        """(first line, last line, Block or None) per fence at the audit commit."""
        if path not in self._old_blocks:
            module = _load_doc_examples()
            text = self.old_text(path)
            if text is None:
                self._old_blocks[path] = "not at the audit commit"
                return self._old_blocks[path]
            try:
                fences = module.parse_fences(text, path)
            except module.Refused as refusal:
                self._old_blocks[path] = f"at the audit commit: {refusal}"
                return self._old_blocks[path]
            found = []
            for fence in fences:
                try:
                    block = module.classify(fence, path)
                except module.Refused:
                    block = None
                end = fence.line + len(fence.body) + 1 + len(fence.trailing)
                found.append((fence.line, end, block))
            self._old_blocks[path] = found
        return self._old_blocks[path]


def _which(raw: str) -> str:
    """Which OpenAPI snapshot a reference means: the core one only if it says so."""
    return "stable" if "stable" in raw and "full" not in raw else "full"


def _ranges(spec: str) -> List[Tuple[int, int]]:
    found = []
    for piece in spec.split(","):
        low, _, high = piece.partition("-")
        found.append((int(low), int(high or low)))
    return found


def fragments(excerpt: str) -> List[str]:
    """The pieces of an excerpt to look for in a unit: split at an ellipsis
    and after a sentence, normalised like a unit."""
    found = []
    for piece in _ELLIPSIS.split(normalise(excerpt)):
        for sentence in _SENTENCE_END.split(piece.strip()):
            sentence = sentence.strip()
            if len(sentence) >= FRAGMENT_MIN:
                found.append(sentence)
    return found


def _picked(units_near: Sequence[Unit], row: Row) -> List[Unit]:
    """The units that hold a fragment of the row's excerpt no other one holds,
    or that the excerpt holds whole."""
    picked: List[Unit] = []
    for fragment in fragments(row.text):
        holding = [u for u in units_near if fragment in u.text]
        if len(holding) == 1 and holding[0] not in picked:
            picked.append(holding[0])
    whole = normalise(row.text)
    for unit in units_near:
        if unit not in picked and len(unit.text) >= FRAGMENT_MIN and unit.text in whole:
            picked.append(unit)
    return [u for u in units_near if u in picked]


def units_of_row(
    spans: Sequence[Tuple[Unit, int, int]], row: Row
) -> Tuple[List[Unit], List[Unit]]:
    """The units a row maps to, and the units on its line.

    By line overlap, with the excerpt deciding: the units on the row's line
    that hold a piece of it; failing that, the units within LINE_SLACK lines
    that do (an auditor's line can be one off); failing that, the one unit on
    the line, if there is one (the excerpt is not verbatim). Otherwise none."""
    on_line = [unit for unit, first, last in spans if first <= row.line <= last]
    picked = _picked(on_line, row)
    if picked:
        return picked, on_line
    near = [
        unit
        for unit, first, last in spans
        if first <= row.line + LINE_SLACK and row.line - LINE_SLACK <= last
    ]
    picked = _picked(near, row)
    if picked:
        return picked, on_line
    return (on_line if len(on_line) == 1 else []), on_line


def outside_prose(raw: Sequence[str], visible: Sequence[str], line: int) -> bool:
    """True when *line* has text the splitter does not read: a fence, an HTML
    comment or front matter."""
    return (
        1 <= line <= len(raw)
        and bool(raw[line - 1].strip())
        and not (visible[line - 1].strip())
    )


def on_page(row: Row, flat: str) -> bool:
    """Whether an R or X row's own excerpt is still on the page: *flat* is the
    page's whole text (fences included), normalised as a unit is.

    Yes when every fragment of the excerpt (``fragments``: split at an
    ellipsis and after a sentence, normalised) is in *flat*; an excerpt too
    short to give a fragment is looked for whole. Nothing else counts (#1189):
    not the units the row maps to, not the other units on its line, and not
    its line at the audit commit. Each of those can outlive the excerpt -- a
    unit that is only the start of an excerpt running into a fence, a fence's
    first line repeated elsewhere, a sentence sharing the line -- and each
    used to refuse a page whose false text was already rewritten."""
    found = fragments(row.text) or [normalise(row.text)]
    return all(piece and piece in flat for piece in found)


@dataclasses.dataclass
class Plan:
    """What ``generate`` writes for one document, and what it prints."""

    document: str
    entries: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    ledger: List[Tuple[str, str, str]] = dataclasses.field(default_factory=list)
    present: List[str] = dataclasses.field(default_factory=list)
    unplaced: List[str] = dataclasses.field(default_factory=list)
    by_hand: List[str] = dataclasses.field(default_factory=list)
    refused: List[str] = dataclasses.field(default_factory=list)
    outside: List[str] = dataclasses.field(default_factory=list)
    mixed: List[str] = dataclasses.field(default_factory=list)
    new: List[str] = dataclasses.field(default_factory=list)
    error: Optional[str] = None
    counts: collections.Counter = dataclasses.field(default_factory=collections.Counter)


#: Where a row ends up; a row on two units takes the first that applies.
OUTCOMES: Tuple[str, ...] = ("mapped", "refused", "by hand")


def plan_document(resolver: Resolver, document: Document, rows: Sequence[Row]) -> Plan:
    """The registry for *document* from the audit *rows* (S-3 of #1142).

    Rows map to the units of the page at the audit commit by line; a unit whose
    text is unchanged on the page now carries its rows forward:

    * an R or X row whose excerpt is still on the page: listed, and no
      registry is written;
    * an R or X row whose excerpt was not on the page at the audit commit
      either (not verbatim) and whose line holds no unit (a fence, a comment,
      front matter): nothing tells whether it is gone, so it is listed for
      hand mapping and no registry is written;
    * a T, S or A row: ``untested:<tag>`` (the heaviest of A, S, T), ledgered;
    * covered rows: the targets their ``by`` names that resolve here;
    * no row: ``exempt:baseline``, ledgered;
    * a unit new since the audit: no entry (the author gives it one).

    Anything that does not map is printed, never dropped: a row on no unit, a
    row whose line holds several units the excerpt does not tell apart (those
    units get no entry either), a target that does not resolve here, and a
    script or application source named as a test (refused)."""
    plan = Plan(document.path)
    counts = plan.counts
    resolver.document = document
    now = resolver.ctx.text(document.path)
    old = resolver.old_text(document.path)
    if now is None or old is None:
        plan.error = f"{document.path}: " + (
            "does not exist" if now is None else "not at the audit commit"
        )
        return plan
    try:
        now_spans, old_spans = unit_lines(now), unit_lines(old)
    except SplitError as error:
        plan.error = f"{document.path}: {error}"
        return plan
    old_raw = old.replace("\r\n", "\n").split("\n")
    old_visible = visible_lines(old)
    on_now = collections.Counter(u.text for u, _, _ in now_spans)
    flat = normalise(now)
    old_flat = normalise(old)
    first_line: Dict[str, int] = {}
    for unit, line, _ in now_spans:
        first_line.setdefault(unit.text, line)
    was = {u.text for u, _, _ in old_spans}
    rows_of: Dict[str, List[int]] = collections.defaultdict(list)
    held: Set[str] = set()  # units of a row that could not be placed: no entry

    for number, row in zip(range(len(rows)), rows):
        counts["rows"] += 1
        inside = outside_prose(old_raw, old_visible, row.line)
        mapped, on_line = ([], []) if inside else units_of_row(old_spans, row)
        if row.tag in MUST_GO:
            if on_page(row, flat):
                counts["R/X on the page"] += 1
                plan.present.append(row.label())
            elif not (mapped or on_line) and not on_page(row, old_flat):
                # The excerpt cannot say it is gone (it was not on the page at
                # the audit commit either), and no unit is held back for
                # `check` to report: refused rather than counted as gone.
                counts["R/X not placed"] += 1
                plan.unplaced.append(
                    f"{row.label()}: its excerpt is not on the page at the audit"
                    " commit and its line holds no unit (a fence, a comment or"
                    " front matter)"
                )
            else:
                counts["R/X gone"] += 1
            held.update(u.text for u in mapped or on_line)
            continue
        if inside and (row.tag or not row.covered):
            counts["outside prose, uncovered"] += 1
            plan.outside.append(row.label())
            continue
        if inside:
            counts["outside prose, covered"] += 1
            continue
        if not mapped and not on_line:
            counts["by hand"] += 1
            plan.by_hand.append(f"{row.label()}: no unit on or near that line")
            continue
        if not mapped:
            counts["by hand"] += 1
            held.update(u.text for u in on_line)
            plan.by_hand.append(
                f"{row.label()}: {len(on_line)} units on that line and the excerpt"
                " does not pick one"
            )
            continue
        kept = [u.text for u in mapped if u.text in on_now]
        if not kept:
            counts["unit changed since the audit"] += 1
            continue
        for text in kept:
            rows_of[text].append(number)

    outcome: Dict[int, str] = {}
    entries: Dict[str, Dict[str, Any]] = {}
    for text, numbers in rows_of.items():
        entry = _entry_for(
            resolver, plan, text, [(n, rows[n]) for n in numbers], outcome
        )
        if entry is not None:
            entries[text] = entry
    counts.update(outcome.values())
    for text, times in on_now.items():
        if text in entries:
            entry = entries[text]
        elif text in rows_of or text in held:
            continue
        elif text not in was:
            counts["units new since the audit"] += 1
            plan.new.append(f"{document.path}:{first_line[text]}: {text[:160]!r}")
            continue
        else:
            entry = {"text": text, "by": ["exempt:baseline"]}
        if times > 1:
            entry["count"] = times
        plan.entries.append(entry)
        head = entry["by"][0]
        if head in LEDGER_KINDS:
            plan.ledger.append((head, document.path, unit_hash(text)))
        counts[head if kind_of(head) in ("untested", "baseline") else "verified"] += 1
    counts["units gone since the audit"] = len(was - set(on_now))
    for name in ("by_hand", "refused", "mixed"):
        setattr(plan, name, list(dict.fromkeys(getattr(plan, name))))
    return plan


def _entry_for(
    resolver: Resolver,
    plan: Plan,
    text: str,
    rows: Sequence[Tuple[int, Row]],
    outcome: Dict[int, str],
) -> Optional[Dict[str, Any]]:
    """One unit's entry from its rows, or None (its rows are printed). Each
    row's outcome is recorded, the first of OUTCOMES over the units it is on."""

    def settle(number: int, result: str) -> None:
        known = outcome.get(number, OUTCOMES[-1])
        outcome[number] = min(known, result, key=OUTCOMES.index)

    covered = [(n, r) for n, r in rows if r.tag is None and r.covered and r.by]
    debt = [r.tag for _, r in rows if r.tag in DEBT]
    other = [(n, r) for n, r in rows if (n, r) not in covered and r.tag not in DEBT]
    if other:
        for number, row in rows:
            settle(number, "by hand")
            why = (
                "a device"
                if row.tag == "D"
                else "covered, but by names nothing"
                if row.covered and row.tag is None
                else "not covered and no tag"
                if row.tag is None
                else f"on a unit with a row that is not mapped ({text[:80]!r})"
            )
            plan.by_hand.append(f"{row.label()}: {why}; map the unit by hand")
        return None
    if debt:
        kind = next(k for k in DEBT if k in debt)
        for number, _ in rows:
            settle(number, "mapped")
        if covered:
            held = sorted(
                {t for _, r in covered for t in resolver.targets(r.by or "").targets}
            )
            plan.mixed.append(f"{text[:120]!r}: untested:{kind}; also held by {held}")
        return {"text": text, "by": [f"untested:{kind}"]}
    targets: List[str] = []
    for number, row in covered:
        mapped = resolver.targets(row.by or "")
        plan.by_hand += [f"{row.label()}: {p}" for p in mapped.by_hand]
        plan.refused += [f"{row.label()}: {p}" for p in mapped.refused]
        if mapped.targets:
            settle(number, "mapped")
            targets += mapped.targets
        elif mapped.refused and not mapped.by_hand:
            settle(number, "refused")
        else:
            settle(number, "by hand")
    targets = list(dict.fromkeys(targets))
    return {"text": text, "by": targets} if targets else None


def registry_text(document: str, entries: Sequence[Mapping[str, Any]]) -> str:
    """A registry file, in the layout of the hand-written ones."""
    out = [
        f"# What verifies each unit of {document} (#1142).",
        "# The contract is scripts/doc_claims.py's docstring; check with",
        "# `python scripts/doc_claims.py check`.",
        f"document = {json.dumps(document)}",
    ]
    for entry in entries:
        out += [
            "",
            "[[unit]]",
            f"text = {json.dumps(entry['text'], ensure_ascii=False)}",
        ]
        by = [json.dumps(t, ensure_ascii=False) for t in entry["by"]]
        if len(by) == 1:
            out.append(f"by = [{by[0]}]")
        else:
            out += ["by = ["] + [f"    {t}," for t in by] + ["]"]
        if "count" in entry:
            out.append(f"count = {entry['count']}")
    return "\n".join(out) + "\n"


def ledger_text(
    existing: str, add: Iterable[Tuple[str, str, str]], drop: Iterable[str]
) -> str:
    """The ledger with *drop*'s documents' lines replaced by *add*: its
    comment lines first, then every line sorted, once."""
    dropped = set(drop)
    head = [line for line in existing.split("\n") if line.startswith("#")]
    body = set()
    for line in existing.split("\n"):
        fields = line.split("\t")
        if line.strip() and not line.startswith("#"):
            if len(fields) < 2 or fields[1] not in dropped:
                body.add(line)
    body |= {"\t".join(line) for line in add}
    return "".join(f"{line}\n" for line in head + sorted(body))


def plan_lines(plan: Plan) -> List[str]:
    """What ``generate`` prints for one document."""
    if plan.error:
        return [plan.error]
    counts = plan.counts
    lines = [
        f"{plan.document}: {counts['rows']} audit rows; {len(plan.entries)} entries"
        f" (verified {counts['verified']}, untested"
        f" {sum(counts[f'untested:{k}'] for k in UNTESTED_KINDS)}, baseline"
        f" {counts['exempt:baseline']}); units new since the audit"
        f" {counts['units new since the audit']}, gone"
        f" {counts['units gone since the audit']}"
    ]
    sections = (
        ("R OR X STILL ON THE PAGE (reword, date or cut it first)", plan.present),
        ("R OR X NOT PLACED (map it by hand, then run again)", plan.unplaced),
        ("MAP BY HAND", plan.by_hand),
        ("REFUSED (never written)", plan.refused),
        (
            "UNCOVERED OUTSIDE PROSE (a fence or comment: give each a disposition)",
            plan.outside,
        ),
        ("DEBT THAT IS ALSO PARTLY HELD", plan.mixed),
        ("NEW SINCE THE AUDIT (no entry: give each one)", plan.new),
    )
    for title, items in sections:
        if items:
            lines.append(f"  {title}: {len(items)}")
            lines += [f"    {item}" for item in items]
    return lines


def near_matches(text: str, candidates: Iterable[str]) -> List[Tuple[float, str]]:
    """The candidates within NEAR of *text*, closest first (inputs bounded)."""
    found = []
    for candidate in candidates:
        matcher = difflib.SequenceMatcher(
            None, text[:NEAR_INPUT], candidate[:NEAR_INPUT], autojunk=False
        )
        if matcher.real_quick_ratio() < NEAR or matcher.quick_ratio() < NEAR:
            continue
        ratio = matcher.ratio()
        if ratio >= NEAR:
            found.append((ratio, candidate))
    return sorted(found, key=lambda pair: (-pair[0], pair[1]))


def sync_lines(root: pathlib.Path, document: str) -> Tuple[List[str], int]:
    """``sync``: the units of *document* that changed against its registry,
    each with the registered units close to it. It writes nothing: a target is
    carried over only by its author. Exit 0 when they agree, 1 when not, 2
    when there is no registry."""
    rel = registry_path(document)
    try:
        data = tomllib.loads((root / rel).read_text(encoding="utf-8"))
        page = read_units(root, document)
    except (OSError, tomllib.TOMLDecodeError, SplitError) as error:
        return [f"{document}: {error}"], 2
    entries = {
        e["text"]: e
        for e in data.get("unit", [])
        if isinstance(e, dict) and isinstance(e.get("text"), str)
    }
    seen = collections.Counter(u.text for u in page)
    first: Dict[str, int] = {}
    for unit in page:
        first.setdefault(unit.text, unit.line)
    gone = [t for t in entries if t not in seen]
    near_any: Set[str] = set()
    lines = []
    for text in seen:
        if text in entries:
            want = entries[text].get("count", 1)
            if want != seen[text]:
                lines.append(
                    f"{document}:{first[text]}: {text[:160]!r}: registered x{want},"
                    f" found x{seen[text]}"
                )
            continue
        lines.append(f"{document}:{first[text]}: not in the registry: {text[:160]!r}")
        near = near_matches(text, gone)
        for ratio, old in near:
            near_any.add(old)
            lines.append(
                f"    near ({ratio:.2f}): {old[:160]!r} by = {entries[old].get('by')}"
            )
        if not near:
            lines.append(f"    nothing registered within {NEAR}: a new unit")
    for old in gone:
        if old not in near_any:
            lines.append(f"{rel}: listed, no longer on the page: {old[:160]!r}")
    return lines, 1 if lines else 0


def baseline_problems(
    resolver: Resolver, documents: Sequence[str], rows: Sequence[Row]
) -> Tuple[List[str], collections.Counter]:
    """Every audit claim row that maps to a unit registered ``exempt:baseline``,
    and how many claim rows were read and mapped.

    A row the excerpt cannot place among the units on its line counts against
    all of them. Run at a release candidate, privately (the audit is private)."""
    problems = []
    counts: collections.Counter = collections.Counter()
    for document in documents:
        rel = registry_path(document)
        try:
            data = tomllib.loads((resolver.ctx.root / rel).read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as error:
            problems.append(f"{document}: {error}")
            continue
        baseline = {
            e.get("text")
            for e in data.get("unit", [])
            if isinstance(e, dict) and e.get("by") == ["exempt:baseline"]
        }
        old = resolver.old_text(document)
        if old is None:
            counts["documents not at the audit commit"] += 1
            continue
        try:
            spans = unit_lines(old)
        except SplitError as error:
            problems.append(f"{document} at the audit commit: {error}")
            continue
        old_raw = old.replace("\r\n", "\n").split("\n")
        old_visible = visible_lines(old)
        for row in rows:
            if row.doc != document or row.kind != "claim":
                continue
            counts["claim rows"] += 1
            if outside_prose(old_raw, old_visible, row.line):
                counts["outside prose"] += 1
                continue
            mapped, on_line = units_of_row(spans, row)
            counts["mapped to a unit"] += bool(mapped or on_line)
            for unit in mapped or on_line:
                if unit.text in baseline:
                    problems.append(
                        f"{row.label()}: maps to {unit.text[:120]!r}, registered"
                        " exempt:baseline: classify it"
                    )
    return problems, counts


def generate_refusal(
    path: str, documents: Mapping[str, Document], out: pathlib.Path, replace: bool
) -> Optional[str]:
    """Why ``generate`` will not write *path*'s registry, or None."""
    if path not in documents:
        return f"not an in-scope document ({INVENTORY})"
    if documents[path].extractor != MARKDOWN:
        return (
            "generate maps the audit to Markdown pages only; register a surface"
            " by hand (units prints its skeleton)"
        )
    if path in CLAIMS_DEFERRED:
        return "in CLAIMS_DEFERRED (#1107): it has no registry"
    if (out / registry_path(path)).is_file() and not replace:
        return (
            f"{registry_path(path)} exists: edit it (sync shows what changed),"
            " or pass --replace"
        )
    return None


def run_generate(
    root: pathlib.Path,
    audit: Sequence[pathlib.Path],
    sha: str,
    paths: Sequence[str],
    out: Optional[pathlib.Path] = None,
    replace: bool = False,
    old_text: Any = None,
) -> int:
    """``generate``: write the registries and ledger lines of *paths* under
    *out* (default *root*). Exit 0; 1 when an R or X row is still on a page,
    or cannot be placed (that page is not written); 2 when an input is
    refused."""
    out = out or root
    try:
        old_text = old_text or git_reader(root, sha)
    except ValueError as error:
        print(error)
        return 2
    rows, bad = load_audit(audit)
    if bad:
        print("\n".join(["the audit has lines that are not rows:"] + bad))
        return 2
    if not rows:
        print(f"the audit has no rows ({', '.join(map(str, audit))}): refused")
        return 2
    documents, _ = inventory_documents(root)
    resolver = Resolver(Context(root, documents, MECHANISMS), old_text)
    status = 0
    totals: collections.Counter = collections.Counter()
    ledger: Dict[str, List[Tuple[str, str, str]]] = {}
    for path in paths:
        refusal = generate_refusal(path, documents, out, replace)
        if refusal:
            print(f"{path}: {refusal}")
            status = 2
            continue
        plan = plan_document(
            resolver, documents[path], [r for r in rows if r.doc == path]
        )
        print("\n".join(plan_lines(plan)))
        totals.update(plan.counts)
        if plan.error:
            status = 2
            continue
        if plan.present or plan.unplaced:
            print(
                f"  NOT WRITTEN: {len(plan.present)} R or X rows are still on the"
                f" page, {len(plan.unplaced)} not placed"
            )
            status = max(status, 1)
            continue
        target = out / registry_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(registry_text(path, plan.entries), encoding="utf-8")
        ledger[path] = plan.ledger
        note = f"; take {path} off CLAIMS_PENDING" if path in CLAIMS_PENDING else ""
        print(f"  wrote {target} and {len(plan.ledger)} ledger lines{note}")
    if ledger:
        source = out / LEDGER if (out / LEDGER).is_file() else root / LEDGER
        existing = source.read_text(encoding="utf-8") if source.is_file() else ""
        lines = [line for found in ledger.values() for line in found]
        (out / LEDGER).parent.mkdir(parents=True, exist_ok=True)
        (out / LEDGER).write_text(
            ledger_text(existing, lines, ledger), encoding="utf-8"
        )
    print(
        f"rows {totals['rows']}: mapped {totals['mapped']}, by hand"
        f" {totals['by hand']}, refused {totals['refused']}, R/X on the page"
        f" {totals['R/X on the page']} (gone {totals['R/X gone']}), R/X not placed"
        f" {totals['R/X not placed']}, outside prose"
        f" {totals['outside prose, uncovered']} uncovered and"
        f" {totals['outside prose, covered']} covered, unit changed since the"
        f" audit {totals['unit changed since the audit']}"
    )
    return status


def run_baseline(
    root: pathlib.Path,
    audit: Sequence[pathlib.Path],
    sha: str,
    paths: Sequence[str],
    old_text: Any = None,
) -> int:
    """``baseline``: exit 1 when an audit claim row maps to a unit that a
    registry of *paths* (default: every registered document) leaves at
    ``exempt:baseline``; 2 when an input is refused."""
    try:
        old_text = old_text or git_reader(root, sha)
    except ValueError as error:
        print(error)
        return 2
    rows, bad = load_audit(audit)
    if bad:
        print("\n".join(["the audit has lines that are not rows:"] + bad))
        return 2
    if not rows:
        print(f"the audit has no rows ({', '.join(map(str, audit))}): refused")
        return 2
    documents, _ = inventory_documents(root)
    paths = list(paths) or sorted(
        p for p in documents if (root / registry_path(p)).is_file()
    )
    resolver = Resolver(Context(root, documents, MECHANISMS), old_text)
    problems, counts = baseline_problems(resolver, paths, rows)
    print("\n".join(problems))
    print(
        f"rows read {len(rows)}; documents {len(paths)}"
        f" ({counts['documents not at the audit commit']} not at the audit commit);"
        f" claim rows on them {counts['claim rows']}, outside prose"
        f" {counts['outside prose']}, mapped to a unit {counts['mapped to a unit']};"
        f" on a baseline unit {len(problems)}"
    )
    return 1 if problems else 0


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


def read_document(root: pathlib.Path, document: str) -> str:
    """The text the splitter reads of *document*, a page or a surface, through
    the extractor the inventory names for it (Markdown when it names none)."""
    try:
        documents, _ = inventory_documents(root)
    except (OSError, tomllib.TOMLDecodeError):
        documents = {}
    found = documents.get(document)
    extractor = found.extractor if found is not None else MARKDOWN
    raw = (root / document.partition("#")[0]).read_text(encoding="utf-8")
    return extracted(document, extractor, raw)


def read_units(root: pathlib.Path, document: str) -> List[Unit]:
    """The units of *document*, a page or a surface, through the extractor the
    inventory names for it (Markdown when it names none)."""
    try:
        documents, _ = inventory_documents(root)
    except (OSError, tomllib.TOMLDecodeError):
        documents = {}
    found = documents.get(document)
    extractor = found.extractor if found is not None else MARKDOWN
    raw = (root / document.partition("#")[0]).read_text(encoding="utf-8")
    return document_units(document, extractor, raw)


def skeleton(document: str, text: str) -> str:
    """A registry for *document* with every unit and no targets, to fill in."""
    return skeleton_of(document, units(text))


def skeleton_of(document: str, page: Sequence[Unit]) -> str:
    """``skeleton`` for units already read."""
    counted = collections.Counter(u.text for u in page)
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
    for name, text in (
        ("generate", "write registries for documents from the audit"),
        ("baseline", "audit claim rows that map to an exempt:baseline unit"),
    ):
        audited = sub.add_parser(name, help=text)
        audited.add_argument(
            "--audit",
            action="append",
            required=True,
            type=pathlib.Path,
            help="the audit's rows, JSON Lines (repeat for several files)",
        )
        audited.add_argument("--audit-sha", required=True, help="the audited commit")
        audited.add_argument("documents", nargs="+" if name == "generate" else "*")
    generate_parser = sub.choices["generate"]
    generate_parser.add_argument(
        "--out", type=pathlib.Path, help="write docs/_claims/ under this directory"
    )
    generate_parser.add_argument(
        "--replace", action="store_true", help="replace an existing registry"
    )
    sync_parser = sub.add_parser("sync", help="near matches for changed units")
    sync_parser.add_argument("document")
    args = parser.parse_args(argv)
    command = args.command or "check"
    if command == "generate":
        return run_generate(
            ROOT, args.audit, args.audit_sha, args.documents, args.out, args.replace
        )
    if command == "sync":
        lines, status = sync_lines(ROOT, args.document)
        print("\n".join(lines or [f"{args.document}: the page and its registry agree"]))
        return status
    if command == "baseline":
        return run_baseline(ROOT, args.audit, args.audit_sha, args.documents)
    if command == "units":
        targets = list(args.documents)
        if args.all:
            documents, _ = inventory_documents(ROOT)
            targets = sorted(
                p for p, d in documents.items() if has_extractor(d.extractor)
            )
        for document in targets:
            try:
                sys.stdout.write(
                    skeleton_of(document, read_units(ROOT, document)) + "\n"
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
