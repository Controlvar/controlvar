"""Feature-flag targeting rules are validated when saved (#535), through the routes.

* ``POST /feature-flags/`` and ``PUT /feature-flags/{id}`` answer 422 for rules
  the flag evaluator would not apply as written, with ``loc`` ``["body",
  "targeting_rules"]`` and a fixed message naming the place and the reason, and
  nothing is written (V1, V2, V5);
* the 422 never repeats the submitted value (V4);
* accepted rules are stored exactly as sent;
* D41: a GET body can be sent back unchanged unless its ``targeting_rules`` are
  ones PUT now refuses (422); omitting the field still works (V9);
* stored rules are not re-judged on read: a flag stored with rules the API now
  refuses still answers GET, the list and evaluate as before (V11);
* ``python -m backend.scripts.check_targeting_rules`` lists such flags and
  writes nothing.

The unit tables (``backend/tests/unit/core/test_flag_targeting_validation.py``)
are the refusal list; every row is sent through both routes here.
"""

from __future__ import annotations

import copy
import json
import uuid

import pytest
from sqlalchemy import text

from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.metrics.metric import ErrorLog, RawMetric
from backend.scripts import check_targeting_rules
from backend.tests.integration.helpers import unique_flag_key
from backend.tests.unit.core.test_flag_targeting_validation import (
    ACCEPTED,
    NATIVE_SENTINEL,
    REFUSED,
    SENTINEL,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


@pytest.fixture(autouse=True)
def _remove_flags_created_here(db_session):
    """Delete the flags each test made, so the list pages other tests read
    are not filled with them (the suite shares one database)."""
    db_session.expire_all()
    before = {row[0] for row in db_session.query(FeatureFlag.id).all()}
    yield
    db_session.rollback()
    created = [
        row[0] for row in db_session.query(FeatureFlag.id).all() if row[0] not in before
    ]
    if created:
        db_session.query(RawMetric).filter(
            RawMetric.feature_flag_id.in_(created)
        ).delete(synchronize_session=False)
        db_session.query(ErrorLog).filter(ErrorLog.feature_flag_id.in_(created)).delete(
            synchronize_session=False
        )
        db_session.query(FeatureFlag).filter(FeatureFlag.id.in_(created)).delete(
            synchronize_session=False
        )
        db_session.commit()


COLLECTION = "/api/v1/feature-flags"

RULES = {
    "logical_operator": "OR",
    "groups": [
        {
            "id": "id-1759000000000-1",
            "logical_operator": "AND",
            "conditions": [
                {
                    "id": "id-1759000000000-2",
                    "attribute": "user.country",
                    "operator": "in",
                    "value": "US, CA",
                }
            ],
        }
    ],
}

#: Stored through the ORM: rules a save now refuses.
STORED_INVALID = {
    "old seed shape": {"operator": "and", "rules": []},
    "XOR": {
        "logical_operator": "XOR",
        "groups": [
            {
                "conditions": [
                    {"attribute": "country", "operator": "equals", "value": "DE"}
                ]
            }
        ],
    },
    "unknown operator": {
        "logical_operator": "AND",
        "groups": [
            {
                "conditions": [
                    {"attribute": "country", "operator": "equal", "value": "DE"}
                ]
            }
        ],
    },
    # #828: 1 group and 1,000 conditions, one over the cap.
    "over the condition cap": {
        "logical_operator": "AND",
        "groups": [
            {
                "conditions": [
                    {"attribute": "country", "operator": "equals", "value": "DE"}
                ]
                * 1000
            }
        ],
    },
}


def _row(db_session, flag_id) -> FeatureFlag:
    db_session.expire_all()
    return db_session.get(FeatureFlag, uuid.UUID(str(flag_id)))


def _refusal(response, message):
    assert response.status_code == 422, response.text
    assert response.json() == {
        "detail": [
            {
                "type": "value_error",
                "loc": ["body", "targeting_rules"],
                "msg": f"Value error, {message}",
                "ctx": {"error": {}},
            }
        ]
    }


# --- V1 / V5: POST ----------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("name", list(REFUSED))
def test_post_refuses_rules_the_evaluator_would_not_apply(
    admin_client, db_session, name
):
    value, message = REFUSED[name]
    key = unique_flag_key("refused")

    response = admin_client.post(
        f"{COLLECTION}/",
        json={"key": key, "name": "Refused", "targeting_rules": copy.deepcopy(value)},
    )

    _refusal(response, message)
    db_session.expire_all()
    assert db_session.query(FeatureFlag).filter(FeatureFlag.key == key).count() == 0


# --- V2 / V5: PUT -----------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("name", list(REFUSED))
def test_put_refuses_rules_the_evaluator_would_not_apply(
    admin_client, db_session, make_feature_flag, name
):
    value, message = REFUSED[name]
    flag = make_feature_flag(targeting_rules=RULES, rollout_percentage=10)

    response = admin_client.put(
        f"{COLLECTION}/{flag.id}",
        json={"targeting_rules": copy.deepcopy(value), "rollout_percentage": 90},
    )

    _refusal(response, message)
    row = _row(db_session, flag.id)
    assert row.targeting_rules == RULES
    assert row.rollout_percentage == 10


# --- V4 ---------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "value",
    [
        {
            "groups": [
                {"conditions": [{"attribute": "c", "operator": SENTINEL, "value": "x"}]}
            ]
        },
        {
            "groups": [
                {
                    "conditions": [
                        {"attribute": SENTINEL, "operator": "equals", "value": "x"}
                    ]
                }
            ]
        },
        {
            "groups": [
                {
                    "conditions": [
                        {
                            "attribute": "age",
                            "operator": "greater_than",
                            "value": SENTINEL,
                        }
                    ]
                }
            ]
        },
        {SENTINEL: ["US"]},
        {"rules": [{"id": SENTINEL, "rule": {"operator": SENTINEL}}]},
        SENTINEL,
        *NATIVE_SENTINEL.values(),
    ],
    ids=["operator", "attribute", "value", "key", "native", "string", *NATIVE_SENTINEL],
)
def test_the_422_never_repeats_the_submitted_value(
    admin_client, make_feature_flag, value
):
    flag = make_feature_flag()
    responses = [
        admin_client.post(
            f"{COLLECTION}/",
            json={
                "key": unique_flag_key("refused-value"),
                "name": "n",
                "targeting_rules": value,
            },
        ),
        admin_client.put(f"{COLLECTION}/{flag.id}", json={"targeting_rules": value}),
    ]
    for response in responses:
        assert response.status_code == 422, response.text
        assert "7731" not in response.text, response.text
        assert "\\u" not in response.text, response.text


# --- accepted rules are stored as sent --------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "builder payload with id keys",
        "native",
        "empty dict",
        "None",
        "regex",
        "rollout_percentage 100.0",
        "native, every schema key at every level",
    ],
)
def test_accepted_rules_are_stored_as_sent(admin_client, db_session, name):
    value = ACCEPTED[name]
    created = admin_client.post(
        f"{COLLECTION}/",
        json={"key": unique_flag_key("ok"), "name": "n", "targeting_rules": value},
    )
    assert created.status_code == 201, created.text
    assert created.json()["targeting_rules"] == value
    assert _row(db_session, created.json()["id"]).targeting_rules == value

    updated = admin_client.put(
        f"{COLLECTION}/{created.json()['id']}", json={"targeting_rules": RULES}
    )
    assert updated.status_code == 200, updated.text
    assert _row(db_session, created.json()["id"]).targeting_rules == RULES


# --- V9: D41 ----------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("name", list(STORED_INVALID))
def test_d41_stored_rules_the_api_refuses_cannot_be_sent_back(
    admin_client, db_session, make_feature_flag, name
):
    """D41: a GET body can be sent back unchanged unless its ``targeting_rules``
    are ones PUT now refuses (422); omitting the field still works."""
    stored = STORED_INVALID[name]
    flag = make_feature_flag(
        targeting_rules=copy.deepcopy(stored),
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=20,
    )
    body = admin_client.get(f"{COLLECTION}/{flag.id}").json()
    assert body["targeting_rules"] == stored

    sent_back = admin_client.put(f"{COLLECTION}/{flag.id}", json=body)
    assert sent_back.status_code == 422, sent_back.text
    assert sent_back.json()["detail"][0]["loc"] == ["body", "targeting_rules"]

    without_rules = {k: v for k, v in body.items() if k != "targeting_rules"}
    without_rules["rollout_percentage"] = 30
    omitted = admin_client.put(f"{COLLECTION}/{flag.id}", json=without_rules)
    assert omitted.status_code == 200, omitted.text
    row = _row(db_session, flag.id)
    assert row.rollout_percentage == 30
    assert row.targeting_rules == stored, "the stored rules were rewritten"


def test_d41_a_valid_stored_value_still_round_trips(admin_client, make_feature_flag):
    flag = make_feature_flag(targeting_rules=RULES)
    body = admin_client.get(f"{COLLECTION}/{flag.id}").json()
    response = admin_client.put(f"{COLLECTION}/{flag.id}", json=body)
    assert response.status_code == 200, response.text


# --- V11: stored rows are not re-judged on read -----------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "stored,reason",
    [
        (STORED_INVALID["old seed shape"], "rollout"),
        # The evaluator reads an unknown logical operator as AND, so this rule
        # still matches the DE user, as it did before.
        (STORED_INVALID["XOR"], "targeting_rule"),
        (STORED_INVALID["unknown operator"], "rollout"),
        # Over the cap, still evaluated in full: every condition matches DE.
        (STORED_INVALID["over the condition cap"], "targeting_rule"),
        ([{"type": "user_id", "user_ids": ["someone-else"]}], "rollout"),
        ({"country": ["DE"]}, "rollout"),
    ],
    ids=[*STORED_INVALID, "legacy list", "flat dict"],
)
def test_stored_rules_the_api_refuses_are_still_read_and_evaluated(
    admin_client, make_feature_flag, stored, reason
):
    flag = make_feature_flag(
        key=unique_flag_key("stored"),
        targeting_rules=copy.deepcopy(stored),
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=100,
    )

    detail = admin_client.get(f"{COLLECTION}/{flag.id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["targeting_rules"] == stored

    listed = admin_client.get(f"{COLLECTION}/", params={"search": flag.key})
    assert listed.status_code == 200, listed.text
    assert [i["targeting_rules"] for i in listed.json()["items"]] == [stored]

    evaluated = admin_client.get(
        f"{COLLECTION}/evaluate/{flag.key}",
        params={"user_id": "stored-user", "context": json.dumps({"country": "DE"})},
    )
    assert evaluated.status_code == 200, evaluated.text
    # As before this change: rules the evaluator cannot read are no rules and
    # the global 100% decides; rules it can read (XOR, read as AND) apply.
    assert evaluated.json()["enabled"] is True
    assert evaluated.json()["reason"] == reason


# --- the check script -------------------------------------------------------


def _all_flag_rows(db_session):
    db_session.expire_all()
    return [
        tuple(r)
        for r in db_session.execute(
            text(
                "SELECT id, key, name, status, rollout_percentage, "
                "targeting_rules::text, updated_at, xmin::text "
                "FROM feature_flags ORDER BY id"
            )
        ).all()
    ]


@pytest.mark.regression
def test_the_check_script_lists_stored_rules_and_writes_nothing(
    db_session, make_feature_flag, capsys
):
    planted = {
        "typo": make_feature_flag(targeting_rules=STORED_INVALID["unknown operator"]),
        "seed": make_feature_flag(targeting_rules=STORED_INVALID["old seed shape"]),
        "legacy-eq": make_feature_flag(
            targeting_rules=[
                {
                    "type": "context",
                    "conditions": [
                        {"attribute": "plan", "operator": "eq", "value": "pro"}
                    ],
                }
            ]
        ),
        "legacy-typo": make_feature_flag(
            targeting_rules=[
                {
                    "type": "context",
                    "conditions": [
                        {"attribute": "plan", "operator": "equal", "value": "pro"}
                    ],
                }
            ]
        ),
        "scalar": make_feature_flag(targeting_rules=42),
        "wide": make_feature_flag(
            targeting_rules=STORED_INVALID["over the condition cap"]
        ),
        "clean": make_feature_flag(targeting_rules=RULES),
    }
    before = _all_flag_rows(db_session)

    assert check_targeting_rules.main([]) == 0

    after = _all_flag_rows(db_session)
    assert after == before, "the check wrote to feature_flags"
    out = capsys.readouterr().out.splitlines()

    def line(name, path, reason):
        flag = planted[name]
        return f"feature_flag\t{flag.id}\t{flag.key}\t{path}\t{reason}"

    for expected in (
        line("typo", "groups[0].conditions[0].operator", "unknown operator"),
        line("seed", "targeting_rules", "unknown key"),
        line(
            "legacy-eq",
            "targeting_rules",
            "a list of rules is not supported; use the groups shape",
        ),
        line(
            "legacy-typo",
            "[0].conditions[0].operator",
            check_targeting_rules.LEGACY_MATCHES_NO_USER,
        ),
        line("scalar", "targeting_rules", "must be an object"),
        line(
            "wide", "targeting_rules", "at most 1000 groups and conditions are allowed"
        ),
    ):
        assert expected in out
    assert not [o for o in out if f"\t{planted['clean'].id}\t" in o]
