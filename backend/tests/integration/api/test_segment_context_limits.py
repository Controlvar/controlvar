"""A segment's ``user_context`` over the targeting context limits answers 422,
and no segment is evaluated for the request.

``POST /api/v1/segments/{segment_id}/evaluate`` and
``POST /api/v1/segments/bulk-evaluate`` hold ``user_context`` to the limits of
``backend/app/core/context_limits.py`` (the flag routes' limits, pinned in
``test_targeting_context_limits.py``), checked once per request in the handler,
before ``AudienceService``. Any signed-in role may call both routes, so the
client here is a VIEWER's.

The segment carries a rule in the targeting rule format, which is matched on
``expand_context(user_context)``: the spy below sees every call made where
``audience_service`` looks the name up, so a context within the limits is seen
to reach the rule, and one over them is seen not to. Evaluate writes nothing
even when it answers, so the row counts are a pin, not the gate; the spy is.
"""

from __future__ import annotations

import json
import sys
import uuid
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from backend.app.core import context_limits
from backend.app.core.context_limits import (
    CONTEXT_TOO_DEEP,
    CONTEXT_TOO_LARGE,
    MAX_CONTEXT_BYTES,
    MAX_CONTEXT_DEPTH,
)
from backend.app.main import app
from backend.app.models.audit_log import AuditLog
from backend.app.models.segment import (
    Segment,
    SegmentKind,
    SegmentMember,
    SegmentStatus,
)
from backend.app.services import audience_service
from backend.tests.integration.api.test_targeting_context_limits import (
    Raw,
    array_chain_text,
)
from backend.tests.unit.core.test_context_limits import (
    array_chain,
    object_chain,
    sized,
)
from backend.tests.unit.docs.test_targeting_context_limit_docs import (
    docs_example_user_contexts,
)

pytestmark = [pytest.mark.integration, pytest.mark.regression]

#: The rule: the targeting rule format, matched through ``expand_context``.
RULES = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [
                {"attribute": "tier", "operator": "equals", "value": "premium"}
            ],
        }
    ],
}
MATCHING = {"tier": "premium"}
ROUTES = ["evaluate", "bulk-evaluate"]

#: Each over the limits and each matching the rule, so that served it would be
#: evaluated and answer a member.
OVER_LIMITS = {
    "size": (lambda: sized(MAX_CONTEXT_BYTES + 1, **MATCHING), CONTEXT_TOO_LARGE),
    "size-multibyte": (
        lambda: sized(MAX_CONTEXT_BYTES + 1, "é", **MATCHING),
        CONTEXT_TOO_LARGE,
    ),
    "depth-objects": (
        lambda: object_chain(MAX_CONTEXT_DEPTH + 1, **MATCHING),
        CONTEXT_TOO_DEEP,
    ),
    "depth-arrays": (
        lambda: array_chain(MAX_CONTEXT_DEPTH + 1, **MATCHING),
        CONTEXT_TOO_DEEP,
    ),
    # About 1.9 KB of nested arrays.
    "deep-array-930": (lambda: array_chain(930, **MATCHING), CONTEXT_TOO_DEEP),
}


@pytest.fixture
def client(viewer_client) -> TestClient:
    """The VIEWER's overrides, with a 500 answered rather than raised."""
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def segment(db_session, admin_user):
    row = Segment(
        name=f"Context limits {uuid.uuid4().hex[:8]}",
        kind=SegmentKind.RULES.value,
        rules=RULES,
        status=SegmentStatus.ACTIVE,
        owner_id=admin_user.id,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    yield row
    db_session.rollback()
    db_session.query(Segment).filter(Segment.id == row.id).delete()
    db_session.commit()


@pytest.fixture
def spy(monkeypatch) -> List[Any]:
    """Every ``expand_context`` call where ``audience_service`` looks it up."""
    calls: List[Any] = []
    real = audience_service.expand_context

    def spying(context):
        calls.append(context)
        return real(context)

    monkeypatch.setattr(audience_service, "expand_context", spying)
    return calls


def _write(value: Any) -> str:
    if isinstance(value, Raw):
        return str(value)
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _send(route: str, client: TestClient, segment: Segment, context: Any):
    if route == "evaluate":
        url = f"/api/v1/segments/{segment.id}/evaluate"
        body = '{"user_context":' + _write(context) + "}"
    else:
        url = "/api/v1/segments/bulk-evaluate"
        body = (
            '{"segment_ids":["'
            + str(segment.id)
            + '"],"user_context":'
            + _write(context)
            + "}"
        )
    return client.post(
        url, content=body.encode("utf-8"), headers={"content-type": "application/json"}
    )


def _answer(response) -> tuple:
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, response.text[:200]


def _member(route: str, response, segment: Segment) -> bool:
    body = response.json()
    if route == "evaluate":
        return body["is_member"]
    return body["memberships"][str(segment.id)]


def _rows(db_session) -> Dict[str, int]:
    db_session.expire_all()
    return {
        "segment": db_session.query(Segment).count(),
        "member": db_session.query(SegmentMember).count(),
        "audit": db_session.query(AuditLog).count(),
    }


# ---------------------------------------------------------------------------
# The fixture is not vacuous
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ROUTES)
def test_a_user_context_within_the_limits_reaches_the_rule(route, client, segment, spy):
    """The control for every refusal below: the same route and segment, a
    context within the limits, evaluated through ``expand_context``."""
    assert segment.rules == RULES
    response = _send(route, client, segment, MATCHING)
    assert response.status_code == 200, response.text
    assert _member(route, response, segment) is True
    assert spy, "expand_context was not called"


# ---------------------------------------------------------------------------
# Over the limits: 422, nothing evaluated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", sorted(OVER_LIMITS))
@pytest.mark.parametrize("route", ROUTES)
def test_a_user_context_over_the_limits_answers_422_and_is_not_evaluated(
    route, case, client, segment, spy, db_session
):
    make, detail = OVER_LIMITS[case]
    before = _rows(db_session)
    response = _send(route, client, segment, make())
    assert _answer(response) == (422, {"detail": detail})
    assert spy == []
    assert _rows(db_session) == before


@pytest.mark.parametrize("route", ROUTES)
def test_exactly_the_limits_are_served_and_one_more_is_refused(
    route, client, segment, spy
):
    for context in (
        sized(MAX_CONTEXT_BYTES, **MATCHING),
        sized(MAX_CONTEXT_BYTES, "é", **MATCHING),
        object_chain(MAX_CONTEXT_DEPTH, **MATCHING),
        array_chain(MAX_CONTEXT_DEPTH, **MATCHING),
    ):
        served = _send(route, client, segment, context)
        assert served.status_code == 200, served.text
        assert _member(route, served, segment) is True
    served_calls = len(spy)
    for context, detail in (
        (sized(MAX_CONTEXT_BYTES + 1, **MATCHING), CONTEXT_TOO_LARGE),
        (object_chain(MAX_CONTEXT_DEPTH + 1, **MATCHING), CONTEXT_TOO_DEEP),
        (array_chain(MAX_CONTEXT_DEPTH + 1, **MATCHING), CONTEXT_TOO_DEEP),
    ):
        assert _answer(_send(route, client, segment, context)) == (
            422,
            {"detail": detail},
        )
    assert len(spy) == served_calls


@pytest.mark.parametrize("route", ROUTES)
def test_a_body_too_deep_to_parse_still_answers_400(route, client, segment, spy):
    response = _send(route, client, segment, array_chain_text(5000))
    assert _answer(response) == (
        400,
        {"detail": "There was an error parsing the body"},
    )
    assert spy == []


#: Depths around the JSON parser's own limit, as in the flag routes' sweep.
SWEEP = range(sys.getrecursionlimit() - 300, sys.getrecursionlimit() + 21)


@pytest.mark.parametrize("route", ROUTES)
def test_user_contexts_around_the_parsers_own_depth_limit_are_refused_cleanly(
    route, client, segment, spy, monkeypatch
):
    """Each depth answers the depth 422, or the body's 400 where it is too
    deep to parse, never 500 or 200; the walk ran for some depths and not for
    others."""
    walked = set()
    sending = {"depth": None}
    real = context_limits.nested_deeper_than

    def counting(value, limit):
        walked.add(sending["depth"])
        return real(value, limit)

    monkeypatch.setattr(context_limits, "nested_deeper_than", counting)
    allowed = [
        (422, {"detail": CONTEXT_TOO_DEEP}),
        (400, {"detail": "There was an error parsing the body"}),
    ]
    answers = {}
    for depth in SWEEP:
        sending["depth"] = depth
        text = Raw('{"a":' + "[" * depth + "1" + "]" * depth + "}")
        answers[depth] = _answer(_send(route, client, segment, text))
    ran = sorted(walked & set(SWEEP))
    print(
        f"{route}: depths {SWEEP.start}-{SWEEP.stop - 1}; the walk ran for "
        f"{ran[0] if ran else None}-{ran[-1] if ran else None} ({len(ran)}); "
        f"answers {sorted({a[0] for a in answers.values()})}"
    )
    wrong = {d: a for d, a in answers.items() if a not in allowed}
    assert not wrong, wrong
    assert ran, "the walk never ran for any depth"
    assert set(SWEEP) - walked, "the walk ran for every depth"
    assert spy == []


def test_bulk_evaluate_checks_its_one_user_context_once(
    client, segment, spy, db_session, admin_user, monkeypatch
):
    """Fifty segments, one context: one check, and one 422."""
    checked = []
    real = context_limits.context_over_limits
    monkeypatch.setattr(
        context_limits,
        "context_over_limits",
        lambda context: checked.append(1) or real(context),
    )
    ids = [str(segment.id)] * 50
    body = {"segment_ids": ids, "user_context": MATCHING}
    response = client.post("/api/v1/segments/bulk-evaluate", json=body)
    assert response.status_code == 200, response.text
    assert len(checked) == 1
    body["user_context"] = sized(MAX_CONTEXT_BYTES + 1, **MATCHING)
    response = client.post("/api/v1/segments/bulk-evaluate", json=body)
    assert _answer(response) == (422, {"detail": CONTEXT_TOO_LARGE})
    assert len(checked) == 2


# ---------------------------------------------------------------------------
# What is sent today is still served
# ---------------------------------------------------------------------------


def test_the_dashboards_segment_check_is_served(client, db_session, admin_user):
    """frontend/src/services/segments.ts ``checkUser``: an id list, asked
    with ``{"user_context": {"user_id": <id>}}``."""
    row = Segment(
        name=f"Context limits ids {uuid.uuid4().hex[:8]}",
        kind=SegmentKind.ID_LIST.value,
        rules=None,
        status=SegmentStatus.ACTIVE,
        owner_id=admin_user.id,
    )
    db_session.add(row)
    db_session.commit()
    try:
        db_session.add(SegmentMember(segment_id=row.id, member_id="cust-002"))
        db_session.commit()
        for user_id, member in (("cust-002", True), ("cust-999", False)):
            response = client.post(
                f"/api/v1/segments/{row.id}/evaluate",
                json={"user_context": {"user_id": user_id}},
            )
            assert response.status_code == 200, response.text
            assert response.json()["is_member"] is member
    finally:
        db_session.rollback()
        db_session.query(SegmentMember).filter(
            SegmentMember.segment_id == row.id
        ).delete()
        db_session.query(Segment).filter(Segment.id == row.id).delete()
        db_session.commit()


@pytest.mark.parametrize("route", ROUTES)
def test_every_example_user_context_in_the_docs_is_served(route, client, segment):
    found, _ = docs_example_user_contexts()
    # docs/api/endpoints.md's two prose examples of the same rule.
    examples = found + [
        ("endpoints.md prose", {"user": {"country": "US"}}),
        ("endpoints.md prose", {"user.country": "US"}),
        ("schema example", {"country": "US", "plan": "pro"}),
    ]
    assert len(examples) >= 5
    for where, context in examples:
        response = _send(route, client, segment, context)
        assert response.status_code == 200, (where, response.text)
