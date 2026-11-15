"""The retention sentence names the table the keys are in (#1543, U5, EM-GT-7).

``docs/guides/how-events-count.md`` tells an operator which table holds the
``event_id`` keys, so that one erasing a person's events by hand deletes their
keys too. A sentence naming another table would send them to the wrong one, so
the name is read from the page and looked up among the core models.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.app.models import register_core_models

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
PAGE = REPO_ROOT / "docs" / "guides" / "how-events-count.md"

#: The sentence, with the table it names captured.
RETENTION = re.compile(
    r"`(?P<table>[a-z_]{1,64})` holds your app's user ids and keeps them when\s+"
    r"events are deleted\."
)


def _named(text: str) -> list[str]:
    return [m.group("table") for m in RETENTION.finditer(text)]


@pytest.mark.regression
def test_the_retention_sentence_names_the_key_table():
    tables = register_core_models().metadata.tables
    named = _named(PAGE.read_text(encoding="utf-8"))
    assert len(named) == 1, f"the retention sentence is on the page {len(named)} times"
    known = {table.name for table in tables.values()}
    assert named[0] in known
    assert named[0] == "event_dedup_keys"


def test_the_reader_finds_the_sentence_and_its_table():
    """The pattern reads the sentence as written, wrapped or not."""
    assert _named(
        "`some_table` holds your app's user ids and keeps them when events are deleted."
    ) == ["some_table"]
    assert _named(
        "`event_dedup_keys` holds your app's user ids and keeps them when\n"
        "events are deleted."
    ) == ["event_dedup_keys"]
    assert _named("`events` holds your app's user ids.") == []
