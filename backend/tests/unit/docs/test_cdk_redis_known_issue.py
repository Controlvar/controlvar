"""The known-issue note on the CDK page shows the change as the stack's tests hold it.

``docs/self-hosting/cdk.md`` carries the one-line change for the Redis subnet group
name. What the change does is held in
``test_cdk_known_issue_redis_name.py``; this file holds only that
the page shows it in the form written there. Reads files only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

PAGE = Path(__file__).resolve().parents[4] / "docs/self-hosting/cdk.md"


def test_the_page_shows_the_documented_change() -> None:
    text = PAGE.read_text(encoding="utf-8")
    assert 'change `[:40]` to `[:40].rstrip("-")`' in text
    assert '.replace("_", "-")[:40].rstrip("-"),' in text
    assert "Known issue" in text and "v0.45.0" in text
