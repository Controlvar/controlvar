"""The split URL guide prints what the code is (#393).

``docs/api/split-url.md`` prints the reference integration whole, in the
block that saves it as ``controlvar_split_url.py``: Doc Examples runs that
copy, and ``test_split_url_reference_integration.py`` runs this directory's,
so the two must be the same text. The page's table of reasons and its list
of crawler substrings are the route's, exactly.

It reads only docs/api/split-url.md, which a docs-only change never touches
alone (``scripts/classify_changes.py``), so it is in the docs-only gate's
``NOT_DOCS_TESTS``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from modules.backend.app.api.v1.endpoints.split_url_routing import REASONS
from modules.backend.app.services.split_url_service import BOT_SUBSTRINGS

pytestmark = [pytest.mark.unit]

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[4]
PAGE = REPO_ROOT / "docs" / "api" / "split-url.md"
REFERENCE = HERE / "controlvar_split_url.py"


def page() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_the_page_prints_the_reference_integration_whole():
    match = re.search(
        r"^cat > controlvar_split_url\.py <<'PY'\n(.*?)\nPY\n```$",
        page(),
        re.S | re.M,
    )
    assert match, "the block that saves controlvar_split_url.py is missing"
    assert match.group(1) + "\n" == REFERENCE.read_text(encoding="utf-8")


def test_the_reasons_table_lists_exactly_the_route_s_reasons():
    text = page()
    table = text[text.index("| `reason` | When |") :]
    table = table[table.index("\n") + 1 : table.index("\n\n")]
    listed = re.findall(r"^\| `([a-z_]+)` \|", table, re.M)
    assert listed == list(REASONS)


def test_the_crawler_list_is_the_route_s():
    sentence = re.search(r"in any case, one of:\s+(.*?)\.\n", page(), re.S)
    assert sentence
    listed = re.findall(r"`([a-z]+)`", sentence.group(1))
    assert tuple(listed) == BOT_SUBSTRINGS
