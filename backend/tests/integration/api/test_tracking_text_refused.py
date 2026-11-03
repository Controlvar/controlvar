"""Tracking requests carrying text the database cannot store answer 422 (#543, #402).

PostgreSQL refuses U+0000 in ``text``/``varchar`` and in ``jsonb``, and a lone
UTF-16 surrogate (U+D800-U+DFFF) cannot be encoded as UTF-8 at all. Before
this change most such values passed validation and reached the database
driver: a 500, in plain text where the failing call ran outside the route's
error handling (a NUL in ``user_id`` on ``/tracking/track``). The rest were
answered inconsistently: 400 or a 422 carrying the driver's message for some
NUL fields, a silently dropped ``session_id``, a per-item batch error, and a
422 for a lone surrogate only in the fields that carry a length limit.

Every SDK tracking route now refuses such a value in its request schema: 422,
a fixed message naming the field, the value itself absent from the body, and
nothing written. Ordinary non-ASCII text is still accepted.

The surrogate is sent as a JSON escape (``"\\ud800"``) in raw bytes, which is
the only way a client can deliver one.
"""

import json
import uuid
from typing import Any, Callable, Dict, List, Tuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.app.core.security import hash_api_key
from backend.app.main import app
from backend.app.models.api_key import APIKey
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import ExperimentStatus, Variant
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.metrics.metric import ErrorLog, RawMetric
from backend.app.models.sdk_evaluation_count import (
    SdkEvaluationFlagCount,
    SdkEvaluationKeyCount,
)
from backend.tests.integration.conftest import make_client_for_user
from backend.tests.integration.helpers import unique_flag_key

pytestmark = [pytest.mark.integration, pytest.mark.regression]

#: Marks the refused value so the test can tell whether the body repeats it.
MARK = "zq543mark"

#: The characters refused, and a name for each in the test id.
REFUSED = [
    ("nul", "\x00"),
    ("high-surrogate", "\ud800"),
    ("low-surrogate", "\udfff"),
]

#: Text the database stores; still accepted everywhere.
VALID_TEXT = "été-\U0001f642"


def _message(field: str) -> str:
    return f"{field} must not contain NUL or unpaired surrogate characters"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def experiment(db_session, make_experiment):
    suffix = uuid.uuid4().hex[:8]
    created = make_experiment(
        name=f"Text refused {suffix}",
        key=f"text-refused-{suffix}",
        status=ExperimentStatus.ACTIVE,
    )
    for name, is_control in (("control", True), ("treatment", False)):
        db_session.add(
            Variant(
                experiment_id=created.id,
                name=name,
                is_control=is_control,
                traffic_allocation=50,
                configuration={},
            )
        )
    db_session.commit()
    db_session.refresh(created)
    yield created
    db_session.rollback()
    db_session.query(Event).filter(Event.experiment_id == created.id).delete()
    db_session.query(Assignment).filter(Assignment.experiment_id == created.id).delete()
    db_session.commit()


@pytest.fixture
def flag(db_session, make_feature_flag):
    created = make_feature_flag(
        key=unique_flag_key("textref"),
        name="Text refused flag",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=25,
    )
    yield created
    db_session.rollback()
    db_session.query(ErrorLog).filter(ErrorLog.feature_flag_id == created.id).delete()
    db_session.query(Event).filter(Event.feature_flag_id == created.id).delete()
    db_session.query(SdkEvaluationFlagCount).filter(
        SdkEvaluationFlagCount.flag_key == created.key
    ).delete()
    db_session.query(SdkEvaluationKeyCount).filter(
        SdkEvaluationKeyCount.flag_key == created.key
    ).delete()
    db_session.query(RawMetric).filter(RawMetric.feature_flag_id == created.id).delete()
    db_session.query(FeatureFlag).filter(FeatureFlag.id == created.id).delete()
    db_session.commit()


@pytest.fixture
def sdk_client(db_session, admin_user):
    """An admin client that shows a 500 as a response instead of raising.

    It also carries a real ``sdk:ruleset`` key, which ``/tracking/evaluations``
    reads from the database.
    """
    raw = f"tr543-{uuid.uuid4().hex}"
    key = APIKey(
        key=hash_api_key(raw),
        name=f"text-refused-{uuid.uuid4().hex[:6]}",
        is_active=True,
        scopes="sdk:ruleset",
        user_id=admin_user.id,
    )
    db_session.add(key)
    db_session.commit()
    make_client_for_user(db_session, admin_user)
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            c.headers.update({"X-API-Key": raw})
            yield c
    finally:
        app.dependency_overrides.clear()
        db_session.rollback()
        db_session.query(APIKey).filter(APIKey.id == key.id).delete()
        db_session.commit()


def _written(db_session) -> Tuple[int, ...]:
    """Row counts of every table a tracking route writes, committed only."""
    with db_session.get_bind().connect() as conn:
        conn.execute(text("SET search_path TO test_experimentation"))
        return tuple(
            conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()
            for table in (
                "events",
                "event_dedup_keys",
                "assignments",
                "error_logs",
                "sdk_evaluation_flag_counts",
                "raw_metrics",
            )
        )


# ---------------------------------------------------------------------------
# The matrix: route, body with one slot, the field named in the message
# ---------------------------------------------------------------------------


def _user() -> str:
    return f"u543-{uuid.uuid4().hex[:10]}"


def _window() -> Dict[str, str]:
    return {
        "window_start": "2026-09-27T12:00:00Z",
        "window_end": "2026-09-27T12:01:00Z",
    }


Body = Callable[[Any, Any, str], Dict[str, Any]]

# (id, url, body(experiment, flag, text under test), field named in the message)
CASES: List[Tuple[str, str, Body, str]] = [
    # /tracking/assign
    (
        "assign-user_id",
        "/api/v1/tracking/assign",
        lambda e, f, s: {"experiment_key": e.key, "user_id": s},
        "user_id",
    ),
    (
        "assign-experiment_key",
        "/api/v1/tracking/assign",
        lambda e, f, s: {"experiment_key": s, "user_id": _user()},
        "experiment_key",
    ),
    (
        "assign-context-value",
        "/api/v1/tracking/assign",
        lambda e, f, s: {
            "experiment_key": e.key,
            "user_id": _user(),
            "context": {"country": s},
        },
        "context",
    ),
    (
        "assign-context-key",
        "/api/v1/tracking/assign",
        lambda e, f, s: {
            "experiment_key": e.key,
            "user_id": _user(),
            "context": {s: "x"},
        },
        "context",
    ),
    # /tracking/track
    (
        "track-user_id",
        "/api/v1/tracking/track",
        lambda e, f, s: {"event_type": "click", "user_id": s, "experiment_key": e.key},
        "user_id",
    ),
    (
        "track-event_type",
        "/api/v1/tracking/track",
        lambda e, f, s: {"event_type": s, "user_id": _user(), "experiment_key": e.key},
        "event_type",
    ),
    (
        "track-event_name",
        "/api/v1/tracking/track",
        lambda e, f, s: {
            "event_type": "click",
            "event_name": s,
            "user_id": _user(),
            "experiment_key": e.key,
        },
        "event_name",
    ),
    (
        "track-experiment_key",
        "/api/v1/tracking/track",
        lambda e, f, s: {
            "event_type": "click",
            "user_id": _user(),
            "experiment_key": s,
        },
        "experiment_key",
    ),
    (
        "track-feature_flag_key",
        "/api/v1/tracking/track",
        lambda e, f, s: {
            "event_type": "click",
            "user_id": _user(),
            "feature_flag_key": s,
        },
        "feature_flag_key",
    ),
    (
        "track-metadata-value",
        "/api/v1/tracking/track",
        lambda e, f, s: {
            "event_type": "click",
            "user_id": _user(),
            "experiment_key": e.key,
            "metadata": {"page": [s]},
        },
        "metadata",
    ),
    (
        "track-metadata-key",
        "/api/v1/tracking/track",
        lambda e, f, s: {
            "event_type": "click",
            "user_id": _user(),
            "experiment_key": e.key,
            "metadata": {"nested": {s: 1}},
        },
        "metadata",
    ),
    (
        "track-event_id",
        "/api/v1/tracking/track",
        lambda e, f, s: {
            "event_type": "click",
            "user_id": _user(),
            "experiment_key": e.key,
            "event_id": s,
        },
        "event_id",
    ),
    # /tracking/events
    (
        "events-user_id",
        "/api/v1/tracking/events",
        lambda e, f, s: {
            "event_type": "click",
            "event_name": "click",
            "user_id": s,
            "experiment_id": str(e.id),
        },
        "user_id",
    ),
    (
        "events-event_name",
        "/api/v1/tracking/events",
        lambda e, f, s: {
            "event_type": "click",
            "event_name": s,
            "user_id": _user(),
            "experiment_id": str(e.id),
        },
        "event_name",
    ),
    (
        "events-session_id",
        "/api/v1/tracking/events",
        lambda e, f, s: {
            "event_type": "click",
            "event_name": "click",
            "user_id": _user(),
            "session_id": s,
            "experiment_id": str(e.id),
        },
        "session_id",
    ),
    (
        "events-properties",
        "/api/v1/tracking/events",
        lambda e, f, s: {
            "event_type": "click",
            "event_name": "click",
            "user_id": _user(),
            "experiment_id": str(e.id),
            "properties": {"k": s},
        },
        "properties",
    ),
    (
        # properties sent as a JSON-encoded string: the character only appears
        # once the schema has parsed it
        "events-properties-json-string",
        "/api/v1/tracking/events",
        lambda e, f, s: {
            "event_type": "click",
            "event_name": "click",
            "user_id": _user(),
            "experiment_id": str(e.id),
            "properties": json.dumps({"k": s}),
        },
        "properties",
    ),
    (
        "events-event_id",
        "/api/v1/tracking/events",
        lambda e, f, s: {
            "event_type": "click",
            "event_name": "click",
            "user_id": _user(),
            "experiment_id": str(e.id),
            "event_id": s,
        },
        "event_id",
    ),
    # /tracking/batch
    (
        "batch-user_id",
        "/api/v1/tracking/batch",
        lambda e, f, s: {
            "events": [{"event_type": "click", "user_id": s, "experiment_key": e.key}]
        },
        "user_id",
    ),
    (
        "batch-metadata",
        "/api/v1/tracking/batch",
        lambda e, f, s: {
            "events": [
                {
                    "event_type": "click",
                    "user_id": _user(),
                    "experiment_key": e.key,
                    "metadata": {"k": s},
                }
            ]
        },
        "metadata",
    ),
    (
        "batch-event_id",
        "/api/v1/tracking/batch",
        lambda e, f, s: {
            "events": [
                {
                    "event_type": "click",
                    "user_id": _user(),
                    "experiment_key": e.key,
                    "event_id": s,
                }
            ]
        },
        "event_id",
    ),
    # /tracking/errors
    (
        "errors-user_id",
        "/api/v1/tracking/errors",
        lambda e, f, s: {
            "feature_flag_key": f.key,
            "user_id": s,
            "error_type": "crash",
            "message": "m",
        },
        "user_id",
    ),
    (
        "errors-feature_flag_key",
        "/api/v1/tracking/errors",
        lambda e, f, s: {"feature_flag_key": s, "error_type": "crash", "message": "m"},
        "feature_flag_key",
    ),
    (
        "errors-message",
        "/api/v1/tracking/errors",
        lambda e, f, s: {
            "feature_flag_key": f.key,
            "error_type": "crash",
            "message": s,
        },
        "message",
    ),
    (
        "errors-stack_trace",
        "/api/v1/tracking/errors",
        lambda e, f, s: {
            "feature_flag_key": f.key,
            "error_type": "crash",
            "message": "m",
            "stack_trace": s,
        },
        "stack_trace",
    ),
    (
        "errors-metadata",
        "/api/v1/tracking/errors",
        lambda e, f, s: {
            "feature_flag_key": f.key,
            "error_type": "crash",
            "message": "m",
            "metadata": {"os": s},
        },
        "metadata",
    ),
    # /tracking/errors/batch
    (
        "errors-batch-error_type",
        "/api/v1/tracking/errors/batch",
        lambda e, f, s: {
            "errors": [{"feature_flag_key": f.key, "error_type": s, "message": "m"}]
        },
        "error_type",
    ),
    (
        "errors-batch-stack_trace",
        "/api/v1/tracking/errors/batch",
        lambda e, f, s: {
            "errors": [
                {
                    "feature_flag_key": f.key,
                    "error_type": "crash",
                    "message": "m",
                    "stack_trace": s,
                }
            ]
        },
        "stack_trace",
    ),
    # /tracking/evaluations
    (
        "evaluations-flag_key",
        "/api/v1/tracking/evaluations",
        lambda e, f, s: {
            "evaluations": [
                {"flag_key": s, "count": 1, "enabled_count": 0, **_window()}
            ]
        },
        "flag_key",
    ),
]

IDS = [case[0] for case in CASES]


def _post(sdk_client, url: str, body: Dict[str, Any]):
    # ensure_ascii writes U+0000 as \u0000 and a lone surrogate as \ud800:
    # exactly the bytes a client sends.
    raw = json.dumps(body, ensure_ascii=True).encode("ascii")
    return sdk_client.post(
        url, content=raw, headers={"Content-Type": "application/json"}
    )


@pytest.mark.parametrize("char_id,char", REFUSED, ids=[r[0] for r in REFUSED])
@pytest.mark.parametrize("case_id,url,body,field", CASES, ids=IDS)
def test_refused_text_answers_422_and_writes_nothing(
    sdk_client, db_session, experiment, flag, case_id, url, body, field, char_id, char
):
    before = _written(db_session)
    resp = _post(sdk_client, url, body(experiment, flag, f"{MARK}{char}x"))

    assert resp.status_code == 422, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    messages = [item["msg"] for item in resp.json()["detail"]]
    assert any(msg.endswith(_message(field)) for msg in messages), messages
    assert MARK.encode() not in resp.content
    assert _written(db_session) == before


@pytest.mark.parametrize("case_id,url,body,field", CASES, ids=IDS)
def test_non_ascii_text_is_still_accepted(
    sdk_client, db_session, experiment, flag, case_id, url, body, field
):
    """The same slot with storable non-ASCII text is not refused.

    Keys in the slot resolve to nothing, so those cases answer 404 (or a
    per-item error); any answer but 422 or 500 shows the text got through.
    """
    resp = _post(sdk_client, url, body(experiment, flag, VALID_TEXT))
    assert resp.status_code not in (422, 500), resp.text
    assert resp.status_code < 500


def test_non_ascii_text_is_stored_unchanged(sdk_client, db_session, experiment):
    user_id = f"{VALID_TEXT}-{uuid.uuid4().hex[:6]}"
    resp = _post(
        sdk_client,
        "/api/v1/tracking/track",
        {
            "event_type": "click",
            "user_id": user_id,
            "experiment_key": experiment.key,
            "metadata": {VALID_TEXT: VALID_TEXT},
        },
    )
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    stored = db_session.query(Event).filter(Event.user_id == user_id).one()
    assert stored.event_metadata == {VALID_TEXT: VALID_TEXT}
