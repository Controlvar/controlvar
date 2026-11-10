"""The pages that state the paging bounds state the code's (#1001).

``docs/api/stability.md`` has one row for #1001, and it must name every stable
operation that takes ``skip``, ``offset`` or ``page``: the core routes that
declare one, read from the application, so a new paged route fails here until
the row names it. ``docs/api/endpoints.md`` (Pagination, and the feature flag
list's parameters) and that row give the bound as a number; every such number
must be ``MAX_OFFSET`` or ``MAX_PAGE``, and the page refusal's text the one
``page_offset_too_large`` builds.
"""

import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from backend.app.api.query_params import MAX_OFFSET, MAX_PAGE, page_offset_too_large
from backend.app.main import app

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
STABILITY = REPO_ROOT / "docs" / "api" / "stability.md"
ENDPOINTS = REPO_ROOT / "docs" / "api" / "endpoints.md"

#: A number long enough to be one of the bounds (19 digits), or near one.
LONG_NUMBER = re.compile(r"\d{16,}")


def _the_row() -> str:
    rows = [
        line
        for line in STABILITY.read_text(encoding="utf-8").splitlines()
        if line.startswith("| ") and " | #1001 | " in line
    ]
    assert len(rows) == 1, rows
    return rows[0]


def _paged_core_operations() -> set:
    """Every operation in the document whose core route takes skip, offset or
    page."""
    found = set()
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            if not isinstance(route, APIRoute):
                continue
            contexts = (route,)
        for ctx in contexts() if callable(contexts) else contexts:
            dependant = getattr(ctx, "dependant", None)
            module = getattr(getattr(ctx, "endpoint", None), "__module__", "")
            if dependant is None or not getattr(ctx, "include_in_schema", True):
                continue
            if not module.startswith("backend."):
                continue
            names = {p.alias for p in dependant.query_params}
            if names & {"skip", "offset", "page"}:
                for method in ctx.methods:
                    found.add(f"{method} {ctx.path}")
    return found


def test_the_row_names_every_stable_paged_operation():
    first_cell = _the_row().split(" | ")[0]
    named = set(re.findall(r"`([A-Z]+ /api/v1/[^`]+)`", first_cell))
    expected = _paged_core_operations()
    assert len(expected) >= 15, expected
    assert named == expected, (
        f"not in the row: {sorted(expected - named)}; "
        f"in the row but not paged: {sorted(named - expected)}"
    )


def test_the_row_states_the_codes_numbers_and_message():
    row = _the_row()
    assert set(LONG_NUMBER.findall(row)) == {str(MAX_OFFSET), str(MAX_PAGE)}
    assert f"`{page_offset_too_large('limit')}`" in row


def _pagination_section() -> str:
    page = ENDPOINTS.read_text(encoding="utf-8")
    start = page.index("### Pagination")
    end = page.index("\n### ", start + 1)
    return page[start:end]


def test_the_pagination_section_states_the_codes_numbers_and_message():
    section = _pagination_section()
    assert set(LONG_NUMBER.findall(section)) == {
        str(MAX_OFFSET),
        str(MAX_OFFSET + 1),
        str(MAX_PAGE),
        str(2**62 + 1),
    }
    assert f"<!-- expect: {page_offset_too_large('limit')} -->" in section


def test_the_flag_list_parameters_state_the_bound():
    page = ENDPOINTS.read_text(encoding="utf-8")
    start = page.index("### List Feature Flags")
    section = page[start : page.index("\n### ", start + 1)]
    assert f"skip: int (default: 0; 0 to {MAX_OFFSET})" in section
    assert f"limit: int (default: 100; 0 to {MAX_OFFSET}," in section
