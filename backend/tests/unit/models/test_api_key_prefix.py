"""Every API key starts with ``eptk_``, followed by 32 hex characters."""

import re

import pytest

from backend.app.models.api_key import generate_api_key

pytestmark = [pytest.mark.unit]


def test_every_generated_key_starts_with_eptk():
    keys = {generate_api_key() for _ in range(200)}
    assert len(keys) == 200
    assert all(re.fullmatch(r"eptk_[0-9a-f]{32}", key) for key in keys), keys
