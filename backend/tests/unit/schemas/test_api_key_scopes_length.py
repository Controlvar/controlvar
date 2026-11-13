"""The joined length of an API key's scopes is bounded by its column (#1936).

``APIKeyCreate.scopes`` refuses a scope set whose stored form (the names
trimmed, the empty ones dropped, joined with commas) is longer than
``SCOPES_MAX_LENGTH``, the length of ``APIKey.scopes``. This is the boundary
table without a database; the HTTP answers and the stored rows are pinned on
PostgreSQL by ``backend/tests/integration/api/test_api_key_scopes_length_api.py``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.core.api_key_scopes import SCOPES_MAX_LENGTH, format_scopes
from backend.app.models.api_key import APIKey
from backend.app.schemas.api_key import APIKeyCreate

pytestmark = pytest.mark.unit


def _nine(count: int):
    """*count* distinct scope names of 9 characters each."""
    return [f"scope{i:04d}" for i in range(count)]


def _padded(count: int):
    return [f"  {name}  " for name in _nine(count)]


def test_the_bound_is_the_column_and_the_column_is_255():
    """A bound moved without a migration lets the insert fail again: CI builds
    its databases from the models, so only this literal would notice."""
    assert SCOPES_MAX_LENGTH == 255
    assert APIKey.__table__.c.scopes.type.length == SCOPES_MAX_LENGTH


@pytest.mark.parametrize(
    ("sent", "stored_length"),
    [
        pytest.param(None, 0, id="null"),
        pytest.param([], 0, id="empty-list"),
        pytest.param(["a" * 255], 255, id="list-one-of-255"),
        pytest.param(["x"] * 128, 255, id="list-128-of-1"),
        pytest.param(_nine(25), 249, id="list-25-of-9"),
        pytest.param("a" * 255, 255, id="string-of-255"),
        pytest.param({"a" * 255: 1}, 255, id="object-key-of-255"),
        pytest.param(_padded(25), 249, id="padded-list-349-sent"),
        pytest.param(",".join(_padded(25)), 249, id="padded-string-349-sent"),
        pytest.param(_nine(25) + ["", "  "], 249, id="empty-names-dropped"),
        pytest.param(["é" * 255], 255, id="255-two-byte-characters"),
    ],
)
def test_a_stored_form_of_255_characters_or_fewer_is_accepted(sent, stored_length):
    body = APIKeyCreate(name="k", scopes=sent)
    assert len(format_scopes(body.scopes) or "") == stored_length


@pytest.mark.parametrize(
    ("sent", "length"),
    [
        pytest.param(["a" * 256], 256, id="list-one-of-256"),
        pytest.param(["a"] * 129, 257, id="list-129-of-1"),
        pytest.param(_nine(26), 259, id="list-26-of-9"),
        pytest.param(["a" * 128, "b" * 130], 259, id="list-two-joining-to-259"),
        pytest.param("a" * 256, 256, id="string-of-256"),
        pytest.param(",".join(_nine(26)), 259, id="string-26-of-9"),
        pytest.param({"a" * 256: 1}, 256, id="object-key-of-256"),
        pytest.param(_padded(26), 259, id="padded-list"),
        pytest.param(["é" * 256], 256, id="256-two-byte-characters"),
    ],
)
def test_a_stored_form_over_255_characters_is_refused_naming_its_length(sent, length):
    with pytest.raises(ValidationError) as caught:
        APIKeyCreate(name="k", scopes=sent)
    errors = caught.value.errors()
    assert [(e["type"], e["loc"], e["msg"]) for e in errors] == [
        (
            "value_error",
            ("scopes",),
            "Value error, scopes joined with commas may be at most 255 characters; "
            f"these are {length}",
        )
    ]
