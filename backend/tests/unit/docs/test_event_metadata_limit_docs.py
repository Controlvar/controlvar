"""The documentation's event metadata limits are the code's (#1085).

Five places state the limits of ``backend/app/schemas/tracking.py``: in
``docs/api/endpoints.md`` the Request Size Limits line, Track Event's Event
limits and Batch Track Events' Response line; the ``docs/sdk-guide.md``
paragraph; and the ``docs/api/stability.md`` row. Each is found exactly once,
each byte count in it is ``EVENT_METADATA_MAX_BYTES`` and each level count
``EVENT_METADATA_MAX_DEPTH``, with the number of counts asserted, so changing a
limit fails here until every page says the new value. The ``/track`` and
``/batch`` refusal they quote is the text the route builds. The places that
state the limit name the three routes it holds on, and no page says it holds
on every stored event: ``/tracking/assign`` stores exposure events under the
targeting context's own limit.

Reads the pages and the schema only (no database).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, List

import pytest
from pydantic import ValidationError

from backend.app.api.v1.endpoints.tracking import _invalid_event_detail
from backend.app.schemas.tracking import (
    EVENT_METADATA_MAX_BYTES,
    EVENT_METADATA_MAX_DEPTH,
    UntaggedEventCreate,
)

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
ENDPOINTS = REPO_ROOT / "docs" / "api" / "endpoints.md"
STABILITY = REPO_ROOT / "docs" / "api" / "stability.md"
SDK_GUIDE = REPO_ROOT / "docs" / "sdk-guide.md"
PAGES = (ENDPOINTS, STABILITY, SDK_GUIDE)

BYTES = re.compile(r"(\d[\d,]*) bytes\b")
#: A level count, not the example's "is 2 levels".
LEVELS = re.compile(r"(?<!is )\b(\d[\d,]*) levels\b")

ROUTES = ("/tracking/track", "/tracking/batch", "/tracking/events")


def _items(text: str) -> List[str]:
    """The page as items: a heading, a table row, a list item with its
    indented continuation lines, or a paragraph, each on one line."""
    items: List[List[str]] = []
    current: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        starts = not stripped or stripped.startswith(("#", "|", "- ", "* ", "```"))
        if starts and current:
            items.append(current)
            current = []
        if stripped:
            current.append(stripped)
    if current:
        items.append(current)
    return [" ".join(item) for item in items]


def _section(text: str, heading: str) -> str:
    start = text.index(f"\n{heading}\n")
    end = text.find("\n### ", start + len(heading) + 2)
    return text[start : end if end != -1 else len(text)]


def _one(items: List[str], where: str, chooser: Callable[[str], bool]) -> str:
    found = [item for item in items if chooser(item)]
    assert len(found) == 1, (where, found)
    return found[0]


def _the_places() -> List[tuple]:
    """``(where, item, bytes counts, level counts, names the routes)``."""
    endpoints = ENDPOINTS.read_text("utf-8")
    size_limits = _items(_section(endpoints, "### Request Size Limits"))
    track = _items(_section(endpoints, "### Track Event"))
    batch = _items(_section(endpoints, "### Batch Track Events"))
    guide = _items(SDK_GUIDE.read_text("utf-8"))
    rows = _items(STABILITY.read_text("utf-8"))
    return [
        (
            "endpoints.md Request Size Limits",
            _one(size_limits, "size limits", lambda i: "`metadata`" in i),
            1,
            1,
            True,
        ),
        (
            "endpoints.md Event limits",
            _one(track, "event limits", lambda i: i.startswith("- **Event limits**")),
            1,
            1,
            True,
        ),
        (
            "endpoints.md Batch Response",
            _one(batch, "batch response", lambda i: i.startswith("- **Response**")),
            1,
            0,
            False,
        ),
        (
            "sdk-guide.md",
            _one(
                guide, "sdk guide", lambda i: "`metadata`" in i and "#track-event" in i
            ),
            1,
            1,
            True,
        ),
        (
            "stability.md #1085",
            _one(rows, "stability row", lambda i: "| #1085 |" in i),
            2,
            1,
            True,
        ),
    ]


def test_every_place_states_the_limits_of_the_code():
    for where, item, byte_counts, level_counts, names_routes in _the_places():
        sizes = BYTES.findall(item)
        assert len(sizes) == byte_counts, (where, sizes, item[:300])
        for size in sizes:
            assert size == f"{EVENT_METADATA_MAX_BYTES:,}", (where, size)
        levels = LEVELS.findall(item)
        assert len(levels) == level_counts, (where, levels, item[:300])
        for level in levels:
            assert int(level) == EVENT_METADATA_MAX_DEPTH, (where, level)
        if names_routes:
            for route in ROUTES:
                assert route in item, (where, route)


def test_each_page_states_the_size_exactly_where_this_test_looks():
    # A sixth place, or one of the five gone, changes these counts.
    written = f"{EVENT_METADATA_MAX_BYTES:,} bytes"
    counts = {
        page.name: " ".join(page.read_text("utf-8").split()).count(written)
        for page in PAGES
    }
    assert counts == {"endpoints.md": 3, "stability.md": 2, "sdk-guide.md": 1}, counts


def test_the_quoted_refusal_is_the_one_the_routes_send():
    with pytest.raises(ValidationError) as caught:
        UntaggedEventCreate(
            event_name="click",
            user_id="u",
            properties={"k": "a" * EVENT_METADATA_MAX_BYTES},
        )
    detail = _invalid_event_detail(caught.value)
    places = {where: item for where, item, *_ in _the_places()}
    for where in ("endpoints.md Event limits", "stability.md #1085"):
        assert f"`{detail}`" in places[where], (where, detail)


def test_no_page_says_the_limit_holds_on_every_stored_event():
    for page in PAGES:
        assert "every stored event" not in page.read_text("utf-8").lower(), page.name
