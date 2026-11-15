"""An API key stops working when its owner's account is deactivated.

``docs/mcp-server.md`` tells the reader that removing a user from the identity
provider does not revoke their API keys, and that deactivating the Controlvar
user (or deleting the keys) does. The second half is this: ``deps.get_api_key``
looks the owner up on every request and answers ``401 Invalid API Key`` for an
inactive or missing one, whatever the key's own state.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from backend.app.api import deps

pytestmark = [pytest.mark.unit]


def _db(key, owner):
    """A session whose first query finds *key* and whose second finds *owner*."""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [key, owner]
    return db


def test_a_valid_key_of_an_active_owner_is_accepted():
    owner = SimpleNamespace(id="u1", is_active=True)
    key = MagicMock(is_valid=True, user_id="u1")
    assert deps.get_api_key(db=_db(key, owner), api_key_header="any-key") is owner


@pytest.mark.parametrize(
    "owner", [SimpleNamespace(id="u1", is_active=False), None], ids=["inactive", "gone"]
)
def test_a_valid_key_of_a_deactivated_or_missing_owner_is_refused(owner):
    key = MagicMock(is_valid=True, user_id="u1")
    with pytest.raises(HTTPException) as refused:
        deps.get_api_key(db=_db(key, owner), api_key_header="any-key")
    assert (refused.value.status_code, refused.value.detail) == (
        401,
        "Invalid API Key",
    )
