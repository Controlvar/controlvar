"""Limits on a wizard draft's fields and on how many drafts one user keeps.

A user keeps at most ``MAX_DRAFTS_PER_USER`` drafts: creating one more drops
that user's oldest, and the create still answers 201. All users together keep
at most ``MAX_DRAFTS_TOTAL``, oldest dropped first. A step field larger than
its limit answers 422 with a fixed message that names the field and the limit
and never the value, and the draft is left as it was.

No database is needed: drafts live in process memory.
"""

import json
import uuid
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.main import app
from backend.app.models.user import User, UserRole
from backend.app.services import experiment_wizard_service as wizard
from backend.app.services.experiment_wizard_service import (
    STEP_FIELDS,
    STEP_TEXT_LIMITS,
    ExperimentWizardService,
    _drafts,
)

pytestmark = pytest.mark.unit

MARKER = "Zq9"


def _user() -> User:
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.username = f"user_{uuid.uuid4().hex[:8]}"
    user.email = f"{user.username}@example.com"
    user.is_active = True
    user.is_superuser = False
    user.role = UserRole.DEVELOPER
    return user


@pytest.fixture(autouse=True)
def _isolated():
    _drafts.clear()
    yield
    _drafts.clear()
    app.dependency_overrides.clear()


@pytest.fixture
def client():
    return TestClient(app)


def _as(user: User) -> None:
    app.dependency_overrides[deps.get_current_active_user] = lambda: user


def _create(client: TestClient) -> str:
    response = client.post("/api/v1/wizard/drafts", json={"experiment_type": "ab"})
    assert response.status_code == 201
    return response.json()["id"]


def _step(client: TestClient, draft_id: str, data: dict):
    return client.put(
        f"/api/v1/wizard/drafts/{draft_id}/step",
        json={"step": "define_hypothesis", "data": data},
    )


# -- how many drafts ---------------------------------------------------------


@pytest.mark.regression
def test_the_draft_after_the_limit_drops_the_users_oldest(client):
    user = _user()
    _as(user)
    ids = [_create(client) for _ in range(wizard.MAX_DRAFTS_PER_USER + 1)]

    listed = {d["id"] for d in client.get("/api/v1/wizard/drafts").json()["drafts"]}
    assert len(listed) == wizard.MAX_DRAFTS_PER_USER
    assert ids[0] not in listed
    assert set(ids[1:]) == listed
    assert client.get(f"/api/v1/wizard/drafts/{ids[0]}").status_code == 404


def test_one_users_drafts_never_drop_another_users(client):
    other = _user()
    _as(other)
    kept = _create(client)

    user = _user()
    _as(user)
    for _ in range(wizard.MAX_DRAFTS_PER_USER * 2):
        _create(client)

    _as(other)
    assert client.get(f"/api/v1/wizard/drafts/{kept}").status_code == 200


@pytest.mark.regression
def test_drafts_of_all_users_together_are_capped_oldest_first(monkeypatch):
    monkeypatch.setattr(wizard, "MAX_DRAFTS_TOTAL", 5)
    made = [ExperimentWizardService.create_draft(f"user-{i}").id for i in range(8)]
    assert list(_drafts) == made[-5:]


def test_the_limits_keep_a_wide_gap_between_one_user_and_all_users():
    assert wizard.MAX_DRAFTS_PER_USER < wizard.MAX_DRAFTS_TOTAL


# -- how large a draft ------------------------------------------------------


def test_every_text_step_field_has_a_limit_at_least_the_experiment_limit():
    from backend.app.schemas.experiment import ExperimentCreate

    for name in ("name", "hypothesis", "description"):
        create_limit = next(
            m.max_length
            for m in ExperimentCreate.model_fields[name].metadata
            if hasattr(m, "max_length")
        )
        assert STEP_TEXT_LIMITS[name] >= create_limit
    string_fields = STEP_FIELDS - {
        "baseline_rate",
        "mde",
        "guardrail_metric_ids",
        "targeting_rules",
    }
    assert set(STEP_TEXT_LIMITS) == string_fields


@pytest.mark.regression
@pytest.mark.parametrize("field", sorted(STEP_TEXT_LIMITS))
def test_a_text_field_over_its_limit_is_refused_and_not_stored(client, field):
    _as(_user())
    draft_id = _create(client)
    limit = STEP_TEXT_LIMITS[field]

    assert _step(client, draft_id, {field: "a" * limit}).status_code == 200

    response = _step(client, draft_id, {field: MARKER * limit})
    assert response.status_code == 422
    assert field in response.json()["detail"]
    assert str(limit) in response.json()["detail"]
    assert MARKER not in response.text
    assert client.get(f"/api/v1/wizard/drafts/{draft_id}").json()[field] == "a" * limit


GUARDRAILS_TOO_MANY = [f"{MARKER}{i}" for i in range(wizard.GUARDRAIL_MAX_ITEMS + 1)]
GUARDRAIL_TOO_LONG = [MARKER * wizard.GUARDRAIL_ITEM_MAX_CHARS]
RULES_TOO_MANY = [{"attribute": MARKER}] * (wizard.TARGETING_MAX_ITEMS + 1)
RULES_TOO_BIG = [{"attribute": "a", "value": MARKER * wizard.TARGETING_MAX_BYTES}]
RULES_TOO_DEEP: list = []
_node: list = RULES_TOO_DEEP
for _ in range(3000):
    _inner: list = []
    _node.append(_inner)
    _node = _inner


@pytest.mark.regression
@pytest.mark.parametrize(
    "field,value",
    [
        ("guardrail_metric_ids", GUARDRAILS_TOO_MANY),
        ("guardrail_metric_ids", GUARDRAIL_TOO_LONG),
        ("targeting_rules", RULES_TOO_MANY),
        ("targeting_rules", RULES_TOO_BIG),
    ],
    ids=["guardrails-count", "guardrail-length", "rules-count", "rules-size"],
)
def test_a_list_field_over_its_limit_is_refused_and_not_stored(client, field, value):
    _as(_user())
    draft_id = _create(client)
    before = client.get(f"/api/v1/wizard/drafts/{draft_id}").json()

    response = _step(client, draft_id, {field: value})

    assert response.status_code == 422
    assert field in response.json()["detail"]
    assert MARKER not in response.text
    assert client.get(f"/api/v1/wizard/drafts/{draft_id}").json() == before


def test_lists_at_their_limits_are_still_saved(client):
    _as(_user())
    draft_id = _create(client)
    guardrails = ["g" * wizard.GUARDRAIL_ITEM_MAX_CHARS] * wizard.GUARDRAIL_MAX_ITEMS
    rules = [{"a": 1}] * wizard.TARGETING_MAX_ITEMS

    response = _step(
        client, draft_id, {"guardrail_metric_ids": guardrails, "targeting_rules": rules}
    )

    assert response.status_code == 200
    assert response.json()["guardrail_metric_ids"] == guardrails


def test_rules_nested_too_deeply_to_measure_are_refused(client):
    with pytest.raises(wizard.WizardStepDataError) as caught:
        wizard.check_step_field_limits({"targeting_rules": [RULES_TOO_DEEP]})
    assert "targeting_rules" in str(caught.value)


def _chain(levels: int) -> list:
    """One rule whose value is *levels* lists nested one inside the other."""
    inner: list = []
    chain = inner
    for _ in range(levels - 1):
        nxt: list = []
        inner.append(nxt)
        inner = nxt
    return [{"a": chain}]


def _refused(rules: list) -> bool:
    try:
        wizard.check_step_field_limits({"targeting_rules": rules})
    except wizard.WizardStepDataError:
        return True
    return False


@pytest.mark.regression
def test_rules_nested_past_the_depth_limit_are_refused():
    # The list and the rule count two levels, so this is the deepest allowed.
    deepest = wizard.TARGETING_MAX_DEPTH - 2
    assert not _refused(_chain(deepest))
    assert _refused(_chain(deepest + 1))


@pytest.mark.regression
def test_rules_holding_too_many_values_are_refused():
    few = [{"a": [[] for _ in range(wizard.TARGETING_MAX_VALUES // 2)]}]
    many = [{"a": [[] for _ in range(wizard.TARGETING_MAX_VALUES)]}]
    assert len(json_text(few)) < wizard.TARGETING_MAX_BYTES
    assert len(json_text(many)) < wizard.TARGETING_MAX_BYTES  # small as text
    assert not _refused(few)
    assert _refused(many)
    many_dicts = [{"a": [{} for _ in range(wizard.TARGETING_MAX_VALUES)]}]
    assert _refused(many_dicts)


def json_text(value) -> str:
    return json.dumps(value, separators=(",", ":"))


@pytest.mark.regression
def test_a_create_with_an_over_long_experiment_type_is_refused_and_stores_nothing(
    client,
):
    _as(_user())
    limit = STEP_TEXT_LIMITS["experiment_type"]

    response = client.post(
        "/api/v1/wizard/drafts", json={"experiment_type": MARKER * limit}
    )

    assert response.status_code == 422
    assert "experiment_type" in response.json()["detail"]
    assert str(limit) in response.json()["detail"]
    assert MARKER not in response.text
    assert not _drafts

    ok = client.post("/api/v1/wizard/drafts", json={"experiment_type": "a" * limit})
    assert ok.status_code == 201
    assert len(_drafts) == 1


def test_the_numbers_the_docs_state_are_the_limits():
    assert wizard.MAX_DRAFTS_PER_USER == 20
    assert wizard.MAX_DRAFTS_TOTAL == 350
    assert STEP_TEXT_LIMITS["description"] == 2000


def test_the_limit_message_names_the_field_and_the_limit_only():
    for field in STEP_FIELDS - {"baseline_rate", "mde"}:
        text = wizard.step_field_limit_error(field)
        assert f"'{field}'" in text
        assert any(ch.isdigit() for ch in text)
