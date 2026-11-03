"""G1-2 over HTTP: a targeting rule's own percentage decides for the users it
matches, as the API stores the rule and serves the answer to an SDK.

The in-process half (``backend/tests/unit/ground_truth/test_flag_rollout_use_cases.py``)
builds the stored rule itself, so it cannot see a write path that loses the
rule's percentage. Here the flag is created through the API with the rule the
dashboard sends, ``country in [US, CA]`` at 30%, over a global rollout of 10%,
and 200 users are evaluated with ``GET /api/v1/feature-flags/evaluate/{key}``,
an API key and a URL-encoded ``context``, as the SDKs call it. Each answer is
compared with O-FLAG (``ground_truth.hashing.flag_bucket``, which imports
nothing from the product): a matched user is enabled iff O-FLAG is below 30,
with reason ``targeting_rule``; anyone else iff it is below 10, with reason
``rollout``.
"""

from __future__ import annotations

import json
import random
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

import pytest

from backend.app.models.user import UserRole
from backend.tests.integration.api.route_helpers import (
    api_client,
    api_key,
    auth,
    local_sign_in,
    make_user,
)

#: The ground-truth library, alone: it imports only the standard library.
_LIBRARY_ROOT = Path(__file__).resolve().parents[4] / "tests" / "ground_truth"
if str(_LIBRARY_ROOT) not in sys.path:
    sys.path.insert(0, str(_LIBRARY_ROOT))

from ground_truth.hashing import flag_bucket

pytestmark = pytest.mark.integration

FLAGS = "/api/v1/feature-flags"
RULE_PERCENTAGE = 30
GLOBAL_PERCENTAGE = 10
USERS = [f"g12-http-{number:03d}" for number in range(200)]
#: 40/20/15/15/10, so about 60% of the users match the rule.
COUNTRY_SLOTS = ("US",) * 8 + ("CA",) * 4 + ("GB",) * 3 + ("DE",) * 3 + ("FR",) * 2
MATCHED = frozenset({"US", "CA"})
RULE = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [
                {"attribute": "country", "operator": "in", "value": ["US", "CA"]}
            ],
        }
    ],
    "rollout_percentage": RULE_PERCENTAGE,
}


@pytest.fixture(autouse=True)
def _sign_in(monkeypatch):
    local_sign_in(monkeypatch)


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


def test_a_rule_created_through_the_api_decides_with_its_own_percentage(
    client, db_session
):
    admin = make_user(db_session, UserRole.ADMIN, superuser=True, tag="g12http")
    flag_key = f"uc-g12-{uuid.uuid4().hex[:8]}"
    created = client.post(
        f"{FLAGS}/",
        json={
            "key": flag_key,
            "name": "G1-2 rule percentage",
            "is_active": True,
            "rollout_percentage": GLOBAL_PERCENTAGE,
            "targeting_rules": RULE,
        },
        headers=auth(admin),
    )
    assert created.status_code in (200, 201), created.text
    key = api_key(client, admin)

    draw = random.Random(1003)
    countries = [draw.choice(COUNTRY_SLOTS) for _ in USERS]
    buckets = [flag_bucket(user, flag_key) for user in USERS]
    pairs = list(zip(countries, buckets))
    # The users tell each half of the decision apart: matched users the rule's
    # 30% takes and the global 10% would not, matched users it leaves out, and
    # users the rule does not match.
    assert any(
        c in MATCHED and GLOBAL_PERCENTAGE <= b < RULE_PERCENTAGE for c, b in pairs
    ), pairs
    assert any(c in MATCHED and b >= RULE_PERCENTAGE for c, b in pairs), pairs
    assert any(c not in MATCHED for c, b in pairs), pairs

    mismatches = []
    for user, country, bucket in zip(USERS, countries, buckets):
        context = quote(json.dumps({"country": country}), safe="")
        resp = client.get(
            f"{FLAGS}/evaluate/{flag_key}?user_id={quote(user, safe='')}"
            f"&context={context}",
            headers=key,
        )
        assert resp.status_code == 200, resp.text
        answer = resp.json()
        if country in MATCHED:
            expected = (bucket < RULE_PERCENTAGE, "targeting_rule")
        else:
            expected = (bucket < GLOBAL_PERCENTAGE, "rollout")
        if (answer["enabled"], answer["reason"]) != expected:
            mismatches.append(
                f"{user} ({country}, bucket {bucket}): answered "
                f"{answer['enabled']}/{answer['reason']}, O-FLAG "
                f"{expected[0]}/{expected[1]}"
            )
    assert mismatches == [], (
        f"{len(mismatches)}/{len(USERS)} answers differ from O-FLAG:\n"
        + "\n".join(mismatches[:10])
    )
