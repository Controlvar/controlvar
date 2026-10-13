"""The documentation's targeting context limits are the code's.

``docs/api/endpoints.md`` (Targeting Context Limits, and the lines that point
to it) and the ``docs/api/stability.md`` row state the limits of
``backend/app/core/context_limits.py``. Every byte and level count in them must
be the module's constant, and every quoted ``detail`` one of its two texts, so
changing a limit fails here until the pages say the new value.

Every example context in ``docs/`` must also be within the limits: an example
over them would answer 422. ``docs_example_contexts`` finds each ``context``
written as JSON (a request body, a ``--data-urlencode`` or URL-encoded query
parameter) or as a Python literal; an example in another language's syntax (a
Go map, a Kotlin ``mapOf``) is not read. The integration test
``test_targeting_context_limits.py`` sends each one to a route and expects 200.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any, Iterator, List, Optional, Tuple
from urllib.parse import unquote

import pytest

from backend.app.core.context_limits import (
    CONTEXT_TOO_DEEP,
    CONTEXT_TOO_LARGE,
    MAX_CONTEXT_BYTES,
    MAX_CONTEXT_DEPTH,
    context_over_limits,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
DOCS = REPO_ROOT / "docs"
ENDPOINTS = DOCS / "api" / "endpoints.md"
STABILITY = DOCS / "api" / "stability.md"
ANCHOR = "#targeting-context-limits"
SECTION = "### Targeting Context Limits"

#: The operations the limits apply to, as the stability row names them.
OPERATIONS = (
    "`GET`/`POST /api/v1/feature-flags/evaluate/{flag_key}`",
    "`GET /api/v1/feature-flags/user/{user_id}`",
    "`POST /api/v1/tracking/assign`",
    "`POST /api/v1/tracking/assign/batch`",
)

NUMBER = re.compile(r"(\d[\d,]*)\s+(bytes|levels)\b")
DETAIL = re.compile(r"`(?:users\[\d+\]: )?(Targeting context [^`]*)`")


# ---------------------------------------------------------------------------
# The numbers
# ---------------------------------------------------------------------------


def _items(text: str) -> Iterator[str]:
    """The page as items: a heading, a table row, a list item with its
    indented continuation lines, or a paragraph."""
    item: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        starts = not stripped or stripped.startswith(("#", "|", "- ", "* ", "```"))
        if starts and item:
            yield "\n".join(item)
            item = []
        if stripped:
            item.append(line)
    if item:
        yield "\n".join(item)


def _section(text: str) -> str:
    start = text.index(SECTION)
    end = text.find("\n## ", start)
    nxt = text.find("\n### ", start + len(SECTION))
    stops = [i for i in (end, nxt) if i != -1]
    return text[start : min(stops) if stops else len(text)]


def _check_numbers(items: List[str]) -> Tuple[int, int]:
    """Every count is the constant; every quoted detail is one of the two."""
    numbers = details = 0
    for item in items:
        for value, unit in NUMBER.findall(item):
            expected = MAX_CONTEXT_BYTES if unit == "bytes" else MAX_CONTEXT_DEPTH
            assert int(value.replace(",", "")) == expected, (
                f"{value} {unit} where the limit is {expected}: {item[:200]}"
            )
            if "," in value:
                assert value == f"{expected:,}", value
            numbers += 1
        for detail in DETAIL.findall(item):
            assert detail in (CONTEXT_TOO_LARGE, CONTEXT_TOO_DEEP), detail
            details += 1
    return numbers, details


def test_the_endpoints_page_states_the_limits_of_the_code():
    text = ENDPOINTS.read_text("utf-8")
    section = _section(text)
    pointing = [
        item
        for item in _items(text)
        if "context" in item.lower() and (ANCHOR in item or NUMBER.search(item))
    ]
    items = list(_items(section)) + pointing
    numbers, details = _check_numbers(items)
    # The section, the user-flags parameter, the assign body, the batch body
    # and its 422 row all state a number.
    assert numbers >= 8, numbers
    assert details >= 4, details
    assert sum(1 for item in pointing if ANCHOR in item) >= 5
    assert CONTEXT_TOO_LARGE in section and CONTEXT_TOO_DEEP in section
    for words in ("Depth is checked first", "as sent", "nobody is assigned", "400"):
        assert words in section, words


def test_the_stability_row_states_the_limits_of_the_code():
    rows = [
        line
        for line in STABILITY.read_text("utf-8").splitlines()
        if line.startswith("| ") and f"endpoints.md{ANCHOR}" in line
    ]
    assert len(rows) == 1, rows
    row = rows[0]
    operations = row.split(" | ")[0]
    for operation in OPERATIONS:
        assert operation in operations, operation
    numbers, details = _check_numbers([row])
    assert numbers >= 3 and details == 2, (numbers, details)
    for words in (
        "answers 422",
        "still answers 400",
        "nobody is assigned",
        "The OpenAPI document does not change.",
    ):
        assert words in row, words


# ---------------------------------------------------------------------------
# The examples
# ---------------------------------------------------------------------------

#: ``context`` (or ``"context"``) then ``:`` or ``=``; not ``user_context``.
_CONTEXT_AT = re.compile(r"""(?<![A-Za-z_])["']?context["']?\s*[:=]\s*""")
_BARE_KEY = re.compile(r"""([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:""")


def _balanced(text: str, start: int) -> Optional[str]:
    """The ``{...}`` starting at ``text[start]``, strings respected."""
    depth = 0
    quote = None
    i = start
    while i < len(text) and i - start < 20_000:
        char = text[i]
        if quote:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    return None


def _parse(literal: str) -> Optional[Any]:
    """JSON, a Python literal, or a JS/Ruby object with bare keys."""
    try:
        return json.loads(literal)
    except ValueError:
        pass
    try:
        return ast.literal_eval(literal)
    except (ValueError, SyntaxError):
        pass
    loose = _BARE_KEY.sub(r'\1"\2":', literal)
    loose = re.sub(r"\btrue\b", "True", loose)
    loose = re.sub(r"\bfalse\b", "False", loose)
    loose = re.sub(r"\bnull\b", "None", loose)
    try:
        return ast.literal_eval(loose)
    except (ValueError, SyntaxError):
        return None


def docs_example_contexts() -> Tuple[List[Tuple[str, dict]], List[str]]:
    """``([(where, context), ...], [where, ...])``: every object literal given
    as a ``context`` in ``docs/``, and the places one could not be read."""
    found: List[Tuple[str, dict]] = []
    unread: List[str] = []
    for path in sorted(DOCS.rglob("*.md")):
        text = path.read_text("utf-8")
        for match in _CONTEXT_AT.finditer(text):
            at = match.end()
            where = f"{path.relative_to(REPO_ROOT)}:{text.count(chr(10), 0, at) + 1}"
            literal = None
            if text.startswith("{", at):
                literal = _balanced(text, at)
            elif text[at : at + 3].upper() == "%7B":
                end = re.search(r"[\s&\"'`)]", text[at:])
                literal = unquote(text[at : at + end.start()] if end else text[at:])
            else:
                continue
            value = _parse(literal) if literal else None
            if isinstance(value, dict):
                found.append((where, value))
            else:
                unread.append(where)
    return found, unread


def test_every_example_context_in_the_docs_is_within_the_limits():
    found, unread = docs_example_contexts()
    assert len(found) >= 15, (len(found), unread)
    assert not unread, unread
    over = [(where, context_over_limits(c)) for where, c in found]
    assert not [o for o in over if o[1] is not None], over
