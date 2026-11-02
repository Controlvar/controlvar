"""List status filters accept any case and answer 422 for an unknown value (#955).

Four list routes took a status-like filter as plain text and passed it on, so a
value outside the enum, or one in a case the enum does not spell, answered 500
with the generic body, and each route accepted a different case:

==========================================  ============  ===================
Route                                       Parameter     Values (any case)
==========================================  ============  ===================
``GET /api/v1/feature-flags/``              ``status``    active, inactive,
                                                          archived
``GET /api/v1/llm-experiments/``            ``status``    DRAFT, ACTIVE,
                                                          PAUSED, COMPLETED
``GET /api/v1/llm-experiments/``            ``task_type`` the six task types
``GET /api/v1/mutual-exclusion-groups``     ``status``    active, archived
``GET /api/v1/notifications/delivery-log``  ``status``    sent, failed, skipped
==========================================  ============  ===================

Now each value matches in any case and filters (gates 9a, 9b); anything else
answers 422 at ``["query", <param>]`` with type ``enum`` and a message that
lists the allowed values and never repeats the value sent (9c); an empty value
is no filter; and the order is 401, then the route's 403, then the 422 (9g):
the value is parsed only after the permission check.

Everything runs on PostgreSQL through the real routes and the local JWT: the
500s came from the database refusing the value, so only the database shows
that the accepted spellings now reach it as the enum.

Each test scopes what it reads to the rows it wrote, because the test database
is shared by the session: flags by ``search`` on a unique suffix and delivery
log entries by a unique ``event_type`` (so items and ``total`` are exact);
groups and LLM experiments, which have no such filter, by checking that every
item has the filtered status, that this test's rows are in or out as their
status says, and that ``total`` equals the database's own count.
"""

import uuid
from typing import Dict, List
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token
from backend.app.main import app
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.llm_experiment import (
    LLMEvaluationMetric,
    LLMExperiment,
    LLMExperimentStatus,
    LLMTaskType,
)
from backend.app.models.mutual_exclusion_group import (
    MutualExclusionGroup,
    MutualExclusionGroupStatus,
)
from backend.app.models.notification import (
    NotificationChannel,
    NotificationDeliveryLog,
    NotificationStatus,
)

pytestmark = [pytest.mark.integration, pytest.mark.regression]

SCHEMA = "test_experimentation"

FLAGS = "/api/v1/feature-flags/"
LLM = "/api/v1/llm-experiments/"
GROUPS = "/api/v1/mutual-exclusion-groups"
DELIVERY = "/api/v1/notifications/delivery-log"

#: The 422 messages, written out (UX texts, B9): the test pins the text, not
#: whatever the routes' constants happen to hold.
FLAG_STATUS_MSG = "status must be one of: active, inactive, archived (any case)"
LLM_STATUS_MSG = "status must be one of: DRAFT, ACTIVE, PAUSED, COMPLETED (any case)"
LLM_TASK_TYPE_MSG = (
    "task_type must be one of: chat_completion, text_generation, "
    "classification, summarization, code_generation, embedding (any case)"
)
GROUP_STATUS_MSG = "status must be one of: active, archived (any case)"
DELIVERY_STATUS_MSG = "status must be one of: sent, failed, skipped (any case)"

#: Values no route accepts. ``null`` is what the nightly fuzz sent; `` active``
#: (a leading space) must not be trimmed into a match.
UNKNOWN = ["bogus", " active", "null", "true", "ACTIVE "]


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")


@pytest.fixture
def client(db_session):
    engine = db_session.get_bind()
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_get_db():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


def _get(client, user, url: str, params: Dict[str, str]):
    headers = {}
    if user is not None:
        headers["Authorization"] = f"Bearer {create_local_access_token(user)}"
    return client.get(url, params=params, headers=headers)


def _cases(*values: str) -> List[str]:
    """Each value lower-case, upper-case and mixed (first letter upper)."""
    out = []
    for value in values:
        lower = value.lower()
        out += [lower, lower.upper(), lower[:1].upper() + lower[1:]]
    return out


def _assert_refused(resp, param: str, msg: str) -> None:
    """The whole body, exactly: one error, no ``input``, a fixed message, so
    the value sent is not repeated anywhere."""
    assert resp.status_code == 422, resp.text
    assert resp.json() == {
        "detail": [{"type": "enum", "loc": ["query", param], "msg": msg}]
    }


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


@pytest.fixture
def flags(db_session):
    """One flag per status, under a unique suffix the list's `search` scopes to."""
    suffix = uuid.uuid4().hex[:10]
    rows = {}
    for member in FeatureFlagStatus:
        flag = FeatureFlag(
            key=f"b9-{suffix}-{member.value.lower()}",
            name=f"b9 {suffix} {member.value.lower()}",
            status=member,
            rollout_percentage=0,
        )
        db_session.add(flag)
        rows[member] = flag
    db_session.commit()
    return suffix, {member: str(flag.id) for member, flag in rows.items()}


@pytest.fixture
def llm_experiments(db_session):
    """One experiment per status, each with its own task type (two share one)."""
    task_types = list(LLMTaskType)
    rows = {}
    for i, member in enumerate(LLMExperimentStatus):
        exp = LLMExperiment(
            name=f"b9 llm {uuid.uuid4().hex[:8]} {member.value}",
            status=member,
            task_type=task_types[i % len(task_types)],
            evaluation_metric=LLMEvaluationMetric.ACCURACY,
        )
        db_session.add(exp)
        rows[member] = exp
    db_session.commit()
    return {member: (str(exp.id), exp.task_type) for member, exp in rows.items()}


@pytest.fixture
def groups(db_session):
    rows = {}
    for member in MutualExclusionGroupStatus:
        group = MutualExclusionGroup(
            name=f"b9 group {uuid.uuid4().hex[:10]} {member.value}",
            status=member,
            traffic_allocation=1.0,
        )
        db_session.add(group)
        rows[member] = group
    db_session.commit()
    return {member: str(group.id) for member, group in rows.items()}


@pytest.fixture
def deliveries(db_session):
    """One log entry per status, under a unique `event_type` the list scopes to."""
    event_type = f"b9_{uuid.uuid4().hex[:12]}"
    rows = {}
    for member in NotificationStatus:
        entry = NotificationDeliveryLog(
            event_type=event_type,
            channel=NotificationChannel.EMAIL,
            recipient="b9@example.test",
            status=member,
        )
        db_session.add(entry)
        rows[member] = entry
    db_session.commit()
    return event_type, {member: str(entry.id) for member, entry in rows.items()}


def _db_count(db_session, column, wanted) -> int:
    db_session.expire_all()
    return (
        db_session.query(func.count())
        .select_from(column.class_)
        .filter(column == wanted)
        .scalar()
    )


# ---------------------------------------------------------------------------
# 9a, 9b: every allowed value, in any case, filters to exactly its rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("wanted", list(FeatureFlagStatus), ids=lambda m: m.value)
@pytest.mark.parametrize("case", [0, 1, 2], ids=["lower", "UPPER", "Mixed"])
def test_flag_status_any_case_filters_to_its_rows(
    client, viewer_user, flags, wanted, case
):
    suffix, ids = flags
    sent = _cases(wanted.value)[case]
    resp = _get(client, viewer_user, FLAGS, {"status": sent, "search": suffix})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [item["id"] for item in body["items"]] == [ids[wanted]]
    assert body["total"] == 1
    assert body["items"][0]["status"] == wanted.value.lower()


@pytest.mark.parametrize("wanted", list(LLMExperimentStatus), ids=lambda m: m.value)
@pytest.mark.parametrize("case", [0, 1, 2], ids=["lower", "UPPER", "Mixed"])
def test_llm_status_any_case_filters_to_its_rows(
    client, db_session, viewer_user, llm_experiments, wanted, case
):
    sent = _cases(wanted.value)[case]
    resp = _get(client, viewer_user, LLM, {"status": sent, "page_size": "100"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    got = {item["id"] for item in body["items"]}
    assert {item["status"] for item in body["items"]} == {wanted.value}
    for other, (exp_id, _) in llm_experiments.items():
        assert (exp_id in got) == (other is wanted), other
    assert body["total"] == _db_count(db_session, LLMExperiment.status, wanted)


@pytest.mark.parametrize("task_type", list(LLMTaskType), ids=lambda m: m.value)
@pytest.mark.parametrize("case", [0, 1, 2], ids=["lower", "UPPER", "Mixed"])
def test_llm_task_type_any_case_filters_to_its_rows(
    client, db_session, viewer_user, llm_experiments, task_type, case
):
    sent = _cases(task_type.value)[case]
    resp = _get(client, viewer_user, LLM, {"task_type": sent, "page_size": "100"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    got = {item["id"] for item in body["items"]}
    assert {item["task_type"] for item in body["items"]} <= {task_type.value}
    for exp_id, stored in llm_experiments.values():
        assert (exp_id in got) == (stored is task_type), stored
    assert body["total"] == _db_count(db_session, LLMExperiment.task_type, task_type)


@pytest.mark.parametrize(
    "wanted", list(MutualExclusionGroupStatus), ids=lambda m: m.value
)
@pytest.mark.parametrize("case", [0, 1, 2], ids=["lower", "UPPER", "Mixed"])
def test_group_status_any_case_filters_to_its_rows(
    client, db_session, developer_user, groups, wanted, case
):
    sent = _cases(wanted.value)[case]
    resp = _get(client, developer_user, GROUPS, {"status": sent, "limit": "1000"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    got = {item["id"] for item in body["items"]}
    assert {item["status"] for item in body["items"]} == {wanted.value}
    for other, group_id in groups.items():
        assert (group_id in got) == (other is wanted), other
    assert body["total"] == _db_count(db_session, MutualExclusionGroup.status, wanted)


@pytest.mark.parametrize("wanted", list(NotificationStatus), ids=lambda m: m.value)
@pytest.mark.parametrize("case", [0, 1, 2], ids=["lower", "UPPER", "Mixed"])
def test_delivery_status_any_case_filters_to_its_rows(
    client, admin_user, deliveries, wanted, case
):
    event_type, ids = deliveries
    sent = _cases(wanted.value)[case]
    resp = _get(
        client, admin_user, DELIVERY, {"status": sent, "event_type": event_type}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [item["id"] for item in body["items"]] == [ids[wanted]]
    assert body["total"] == 1
    assert body["items"][0]["status"] == wanted.value


# ---------------------------------------------------------------------------
# An empty value is no filter
# ---------------------------------------------------------------------------


def test_an_empty_flag_status_is_no_filter(client, viewer_user, flags):
    suffix, ids = flags
    resp = _get(client, viewer_user, FLAGS, {"status": "", "search": suffix})
    assert resp.status_code == 200, resp.text
    assert {item["id"] for item in resp.json()["items"]} == set(ids.values())
    assert resp.json()["total"] == len(ids)


def test_an_empty_llm_filter_is_no_filter(client, viewer_user, llm_experiments):
    resp = _get(
        client, viewer_user, LLM, {"status": "", "task_type": "", "page_size": "100"}
    )
    assert resp.status_code == 200, resp.text
    got = {item["id"] for item in resp.json()["items"]}
    assert {exp_id for exp_id, _ in llm_experiments.values()} <= got


def test_an_empty_group_status_is_no_filter(client, developer_user, groups):
    resp = _get(client, developer_user, GROUPS, {"status": "", "limit": "1000"})
    assert resp.status_code == 200, resp.text
    assert set(groups.values()) <= {item["id"] for item in resp.json()["items"]}


def test_an_empty_delivery_status_is_no_filter(client, admin_user, deliveries):
    event_type, ids = deliveries
    resp = _get(client, admin_user, DELIVERY, {"status": "", "event_type": event_type})
    assert resp.status_code == 200, resp.text
    assert {item["id"] for item in resp.json()["items"]} == set(ids.values())
    assert resp.json()["total"] == len(ids)


# ---------------------------------------------------------------------------
# 9c: an unknown value answers 422, naming the allowed values, not the value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sent", UNKNOWN)
def test_an_unknown_flag_status_is_422(client, viewer_user, sent):
    resp = _get(client, viewer_user, FLAGS, {"status": sent})
    _assert_refused(resp, "status", FLAG_STATUS_MSG)


@pytest.mark.parametrize("sent", UNKNOWN + ["draft "])
def test_an_unknown_llm_status_is_422(client, viewer_user, sent):
    resp = _get(client, viewer_user, LLM, {"status": sent})
    _assert_refused(resp, "status", LLM_STATUS_MSG)


@pytest.mark.parametrize("sent", UNKNOWN + ["x", "chat-completion"])
def test_an_unknown_llm_task_type_is_422(client, viewer_user, sent):
    resp = _get(client, viewer_user, LLM, {"task_type": sent})
    _assert_refused(resp, "task_type", LLM_TASK_TYPE_MSG)


@pytest.mark.parametrize("sent", UNKNOWN)
def test_an_unknown_group_status_is_422(client, developer_user, sent):
    resp = _get(client, developer_user, GROUPS, {"status": sent})
    _assert_refused(resp, "status", GROUP_STATUS_MSG)


@pytest.mark.parametrize("sent", UNKNOWN + ["archived"])
def test_an_unknown_delivery_status_is_422(client, admin_user, sent):
    resp = _get(client, admin_user, DELIVERY, {"status": sent})
    _assert_refused(resp, "status", DELIVERY_STATUS_MSG)


# ---------------------------------------------------------------------------
# 9g: 401, then the route's 403, then the 422
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "param"),
    [
        (FLAGS, "status"),
        (LLM, "status"),
        (LLM, "task_type"),
        (GROUPS, "status"),
        (DELIVERY, "status"),
    ],
)
def test_without_a_token_a_bad_value_answers_401(client, url, param):
    resp = _get(client, None, url, {param: "bogus"})
    assert resp.status_code == 401, resp.text


def test_a_viewer_refused_the_group_list_gets_403_not_422(client, viewer_user):
    """The groups list refuses a VIEWER (DEVELOPER and above may list)."""
    resp = _get(client, viewer_user, GROUPS, {"status": "bogus"})
    assert resp.status_code == 403, resp.text
    assert "must be one of" not in resp.text


def test_a_developer_refused_the_delivery_log_gets_403_not_422(client, developer_user):
    """The delivery log refuses every role but ADMIN."""
    resp = _get(client, developer_user, DELIVERY, {"status": "bogus"})
    assert resp.status_code == 403, resp.text
    assert "must be one of" not in resp.text


@pytest.mark.parametrize(
    ("url", "module", "param"),
    [
        (FLAGS, "feature_flags", "status"),
        (LLM, "llm_experiments", "status"),
        (LLM, "llm_experiments", "task_type"),
    ],
)
def test_a_refused_list_permission_answers_403_not_422(
    client, viewer_user, url, module, param
):
    """All four roles may list flags and LLM experiments, so no role is
    refused there; the permission check is forced to refuse instead (EMBS-3(c))."""
    target = f"backend.app.api.v1.endpoints.{module}.check_permission"
    with patch(target, return_value=False):
        resp = _get(client, viewer_user, url, {param: "bogus"})
    assert resp.status_code == 403, resp.text
    assert "must be one of" not in resp.text
