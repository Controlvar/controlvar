"""What the evaluate routes' API reference says a 404 is (#1176).

``GET`` and ``POST /api/v1/feature-flags/evaluate/{flag_key}`` answer 404 for a
key no flag has. A flag that exists but is not ACTIVE answers 200 with
``enabled: false`` and ``reason: "inactive"`` (``_evaluate_flag_by_key``, held
by ``backend/tests/integration/api/test_feature_flag_kill_switch.py``). The
routes' docstrings are the operations' descriptions in the OpenAPI document,
which ``docs/api/openapi-v1.*.json`` snapshots and readers see in the API
reference, and they said "404: if the feature flag doesn't exist or is not
active".
"""

import re

import pytest

from backend.app.main import app

pytestmark = [pytest.mark.unit]

EVALUATE = "/api/v1/feature-flags/evaluate/{flag_key}"


def _entry(description: str, status: str) -> str:
    """The text of one ``HTTPException <status>:`` entry of the Raises list."""
    match = re.search(
        rf"HTTPException {status}:(.*?)(?=\n\s*HTTPException \d{{3}}:|\Z)",
        description,
        re.S,
    )
    assert match, f"no 'HTTPException {status}' entry in:\n{description}"
    return " ".join(match.group(1).split())


@pytest.mark.regression
@pytest.mark.parametrize("method", ["get", "post"])
def test_the_evaluate_description_does_not_say_an_inactive_flag_is_a_404(method):
    description = app.openapi()["paths"][EVALUATE][method]["description"]
    not_found = _entry(description, "404")
    first_sentence = not_found.split(". ")[0]

    # The 404 is for a key no flag has, not for a flag that is not ACTIVE...
    assert not re.search(r"not\s+active", first_sentence, re.I), not_found
    assert re.search(r"no feature flag has this key", first_sentence, re.I), not_found
    # ...and the entry says what a flag that is not ACTIVE gets instead.
    assert "200" in not_found, not_found
    assert "enabled: false" in not_found, not_found
    assert "inactive" in not_found, not_found
