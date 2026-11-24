"""``UserResponse.email`` (#342), and ``UserMe.email`` and ``UserMe.username``
(#2067), are nullable and still required.

The fix for #342 lets ``email`` be ``null`` so that an account stored without
an email address no longer makes the user endpoints answer 500. Clients were
promised the key is always present, so it stays in the schema's ``required``
list: ``Optional[str]`` with no default. ``Optional[str] = None`` would also
fix the 500 but silently drop ``email`` from ``required`` -- that is what this
test refuses, against both the published stable snapshot and the live app.
#2067 made the same choice for the two fields of the signed-in account's own
profile.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from backend.app.main import app

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
STABLE_SNAPSHOT = REPO_ROOT / "docs" / "api" / "openapi-v1.stable.json"

NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}

PROFILE_FIELDS = ("email", "username")


def _schema(document: Dict[str, Any], name: str) -> Dict[str, Any]:
    return document["components"]["schemas"][name]


def _snapshot() -> Dict[str, Any]:
    return json.loads(STABLE_SNAPSHOT.read_text(encoding="utf-8"))


def _assert_nullable_and_required(schema: Dict[str, Any], field: str) -> None:
    assert field in schema["required"], (field, schema["required"])
    described = schema["properties"][field]
    assert described.get("anyOf") == NULLABLE_STRING["anyOf"], (field, described)
    assert "default" not in described, (field, described)


def test_stable_snapshot_keeps_email_required_and_nullable():
    _assert_nullable_and_required(_schema(_snapshot(), "UserResponse"), "email")


def test_live_openapi_keeps_email_required_and_nullable():
    _assert_nullable_and_required(_schema(app.openapi(), "UserResponse"), "email")


@pytest.mark.parametrize("field", PROFILE_FIELDS)
def test_stable_snapshot_keeps_the_profile_field_required_and_nullable(field):
    _assert_nullable_and_required(_schema(_snapshot(), "UserMe"), field)


@pytest.mark.parametrize("field", PROFILE_FIELDS)
def test_live_openapi_keeps_the_profile_field_required_and_nullable(field):
    _assert_nullable_and_required(_schema(app.openapi(), "UserMe"), field)
