"""A JSON request body holding NaN, Infinity or 1e400 answers 422 (#1630).

Through ``backend.app.main.app``, the application as it is served, so these
rows hold main.py's wiring as well as the check
(``backend/app/core/finite_json.py``). The database is a stand-in that finds
nothing, the API key is accepted, and the event store records what it would
store, so a row shows both the answer and that nothing was stored.

* V2, the matrix: each row puts one non-finite number somewhere (a field, deep
  in ``metadata``, in an array, in a batch entry, the whole body) under a
  content type FastAPI parses, and must answer 422 ``finite_number`` at that
  place with nothing stored. Each row has a twin, the same text with the
  number made finite, which must not answer 422: so the 422 is the number's,
  not the body's. A ``/batch`` body with one such number anywhere is refused
  as a whole: none of its events is stored.
* V3: a body FastAPI does not parse as JSON (``text/json``, ``text/plain``, a
  form type, no content type) still answers its own 422,
  ``model_attributes_type``, and stores nothing. It holds the outcome, not
  the mechanism, so a FastAPI upgrade that parses another content type fails
  here.
* A body that is not JSON is never a 500: ``{bad`` on a JSON route answers
  FastAPI's ``json_invalid``, and on the two form routes (``/auth/token``,
  and in the full profile the SAML ACS) what FastAPI's form handling answers,
  which the check does not touch.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List

import pytest
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.core.finite_json import FINITE_NUMBER_MESSAGE, FINITE_NUMBER_TYPE
from backend.app.main import app
from backend.app.services.event_service import EventService

pytestmark = [pytest.mark.unit]

JSON = "application/json"
UUID = "00000000-0000-4000-8000-000000000001"


class _Query:
    """A query that finds nothing, whatever it is chained with."""

    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: self

    def first(self) -> None:
        return None

    def one_or_none(self) -> None:
        return None

    def scalar(self) -> None:
        return None

    def all(self) -> List[Any]:
        return []

    def count(self) -> int:
        return 0

    def __iter__(self) -> Iterator[Any]:
        return iter(())


class _Session:
    """A database session in which nothing is stored."""

    def query(self, *args: Any, **kwargs: Any) -> _Query:
        return _Query()

    def execute(self, *args: Any, **kwargs: Any) -> _Query:
        return _Query()

    def get(self, *args: Any, **kwargs: Any) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        # add, commit, rollback, flush, refresh, close: nothing to do.
        return lambda *args, **kwargs: None


@pytest.fixture
def stored(monkeypatch) -> List[Any]:
    """The events the tracking routes store, in order."""
    events: List[Any] = []

    def track_event_once(self, event_data, event_id, *, allow_untagged):
        event = EventService.build_event(event_data, allow_untagged=allow_untagged)
        event.id = uuid.uuid4()
        event.updated_at = datetime.now(timezone.utc)
        events.append(event)
        return event.id, True, event

    monkeypatch.setattr(EventService, "track_event_once", track_event_once)
    monkeypatch.setattr(EventService, "unknown_id_fields", lambda self, data: [])
    return events


@pytest.fixture
def client(stored) -> Iterator[TestClient]:
    def session() -> Iterator[_Session]:
        yield _Session()

    app.dependency_overrides[deps.get_db] = session
    app.dependency_overrides[deps.get_api_key] = lambda: None
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


def _finite_number(response) -> List[Dict[str, Any]]:
    assert response.status_code == 422, response.text
    return response.json()["detail"]


def _event(**extra: Any) -> str:
    """An event body with no key (stored as history), as text, *extra*
    written raw (so a value can be NaN)."""
    fields = ['"event_type":"click"', '"user_id":"u-1630"']
    fields += [f'"{name}":{raw}' for name, raw in extra.items()]
    return "{" + ",".join(fields) + "}"


#: (id, method, path, content type, body with the token "X" where the
#: non-finite number goes, the number, the loc after "body").
ROWS = [
    (
        "track-nan",
        "POST",
        "/api/v1/tracking/track",
        JSON,
        _event(value="X"),
        "NaN",
        ["value"],
    ),
    (
        "track-infinity",
        "POST",
        "/api/v1/tracking/track",
        JSON,
        _event(value="X"),
        "Infinity",
        ["value"],
    ),
    (
        "track-minus-infinity",
        "POST",
        "/api/v1/tracking/track",
        JSON,
        _event(value="X"),
        "-Infinity",
        ["value"],
    ),
    (
        "track-1e400",
        "POST",
        "/api/v1/tracking/track",
        JSON,
        _event(value="X"),
        "1e400",
        ["value"],
    ),
    (
        "track-minus-1e400",
        "POST",
        "/api/v1/tracking/track",
        JSON,
        _event(value="X"),
        "-1e400",
        ["value"],
    ),
    (
        "track-deep-metadata",
        "POST",
        "/api/v1/tracking/track",
        JSON,
        _event(metadata='{"a":{"b":[1,{"c":X}]}}'),
        "NaN",
        ["metadata", "a", "b", 1, "c"],
    ),
    ("track-whole-body", "POST", "/api/v1/tracking/track", JSON, "X", "NaN", []),
    (
        "track-charset",
        "POST",
        "/api/v1/tracking/track",
        "application/json; charset=utf-8",
        _event(value="X"),
        "NaN",
        ["value"],
    ),
    (
        "track-capitals",
        "POST",
        "/api/v1/tracking/track",
        "Application/JSON",
        _event(value="X"),
        "NaN",
        ["value"],
    ),
    (
        "track-vendor-json",
        "POST",
        "/api/v1/tracking/track",
        "application/vnd.x+json; charset=utf-8",
        _event(value="X"),
        "Infinity",
        ["value"],
    ),
    (
        "batch-second-entry",
        "POST",
        "/api/v1/tracking/batch",
        JSON,
        '{"events":[' + _event(value="1") + "," + _event(value="X") + "]}",
        "NaN",
        ["events", 1, "value"],
    ),
    (
        "batch-metadata-array",
        "POST",
        "/api/v1/tracking/batch",
        JSON,
        '{"events":[' + _event(metadata='{"a":[1,X]}') + "," + _event(value="2") + "]}",
        "-Infinity",
        ["events", 0, "metadata", "a", 1],
    ),
    (
        "events-value",
        "POST",
        "/api/v1/tracking/events",
        JSON,
        '{"event_type":"click","event_name":"click","user_id":"u-1630",'
        f'"experiment_id":"{UUID}","value":X}}',
        "1e400",
        ["value"],
    ),
    (
        "assign-context",
        "POST",
        "/api/v1/tracking/assign",
        JSON,
        '{"experiment_key":"k-1630","user_id":"u-1630","context":{"x":[X]}}',
        "NaN",
        ["context", "x", 0],
    ),
    (
        "errors-metadata",
        "POST",
        "/api/v1/tracking/errors",
        JSON,
        '{"feature_flag_key":"f-1630","error_type":"crash","message":"m",'
        '"metadata":{"score":X}}',
        "Infinity",
        ["metadata", "score"],
    ),
    (
        "evaluate-context",
        "POST",
        "/api/v1/feature-flags/evaluate/f-1630",
        JSON,
        '{"user_id":"u-1630","context":{"score":X}}',
        "-Infinity",
        ["context", "score"],
    ),
    (
        "stage-trigger-configuration-signed-out",
        "PUT",
        f"/api/v1/rollout-schedules/stages/{UUID}",
        JSON,
        '{"trigger_configuration":{"k":X}}',
        "NaN",
        ["trigger_configuration", "k"],
    ),
]

#: The rows that store events when finite: their twin must store.
STORING = {"track", "batch", "events"}

#: The rows of #1630's report, each answered 200 before the check.
REPORTED = {"track-nan", "batch-metadata-array", "assign-context", "errors-metadata"}

#: The one twin that is still a 422, for another reason: a bare number is
#: not an event object.
TWIN_422 = {"track-whole-body": ["model_attributes_type"]}


def _params(rows):
    for row in rows:
        marks = [pytest.mark.regression] if row[0] in REPORTED else []
        yield pytest.param(*row, id=row[0], marks=marks)


ARGS = "label, method, path, content_type, body, number, loc"


@pytest.mark.parametrize(ARGS, _params(ROWS))
def test_a_non_finite_number_answers_422_at_its_place_and_nothing_is_stored(
    client, stored, label, method, path, content_type, body, number, loc
):
    response = client.request(
        method,
        path,
        content=body.replace("X", number),
        headers={"Content-Type": content_type},
    )
    assert _finite_number(response) == [
        {
            "type": FINITE_NUMBER_TYPE,
            "loc": ["body", *loc],
            "msg": FINITE_NUMBER_MESSAGE,
        }
    ]
    assert stored == []


@pytest.mark.parametrize(ARGS, _params(ROWS))
def test_the_same_body_with_a_finite_number_is_not_refused_for_it(
    client, stored, label, method, path, content_type, body, number, loc
):
    response = client.request(
        method,
        path,
        content=body.replace("X", "1.5"),
        headers={"Content-Type": content_type},
    )
    assert response.status_code < 500, response.text
    if label in TWIN_422:
        assert response.status_code == 422, response.text
        assert [e["type"] for e in response.json()["detail"]] == TWIN_422[label]
        return
    assert response.status_code != 422, response.text
    if path.rsplit("/", 1)[-1] in STORING:
        assert response.status_code == 200, response.text
        assert stored, "the finite twin stored nothing"


@pytest.mark.regression
def test_a_check_before_authentication_answers_without_a_key():
    """It answers where FastAPI's own ``json_invalid`` answers: before the
    key is read. A finite body with no key is the key's 401."""
    plain = TestClient(app, raise_server_exceptions=False)
    refused = plain.post(
        "/api/v1/tracking/track",
        content=_event(value="NaN"),
        headers={"Content-Type": JSON},
    )
    assert _finite_number(refused)[0]["loc"] == ["body", "value"]
    finite = plain.post(
        "/api/v1/tracking/track",
        content=_event(value="1.5"),
        headers={"Content-Type": JSON},
    )
    assert finite.status_code == 401, finite.text


def test_one_non_finite_number_refuses_the_whole_batch(client, stored):
    """The check reads the body before any entry is stored: the finite
    entries around it are not stored either."""
    body = "[" + ",".join(_event(value=v) for v in ("1", "2", "NaN", "4")) + "]"
    response = client.post(
        "/api/v1/tracking/batch",
        content='{"events":' + body + "}",
        headers={"Content-Type": JSON},
    )
    assert _finite_number(response)[0]["loc"] == ["body", "events", 2, "value"]
    assert stored == []


@pytest.mark.regression
@pytest.mark.parametrize("field", ["properties", "metadata"])
@pytest.mark.parametrize(
    "text, below",
    [
        ('{"a": NaN}', ["a"]),
        ('{"a": [1, -Infinity]}', ["a", 1]),
        ('{"b": 1e400}', ["b"]),
    ],
    ids=["nan", "minus-infinity-in-array", "1e400"],
)
def test_events_holds_attributes_sent_as_text_to_the_same_rule(
    client, stored, field, text, below
):
    """``/events`` decodes an attribute field sent as a JSON-encoded string;
    a non-finite number in it is refused at its place, as in the body."""
    body = {
        "event_type": "click",
        "event_name": "click",
        "user_id": "u-1630",
        "experiment_id": UUID,
        field: text,
    }
    response = client.post("/api/v1/tracking/events", json=body)
    assert _finite_number(response) == [
        {
            "type": FINITE_NUMBER_TYPE,
            "loc": ["body", field, *below],
            "msg": FINITE_NUMBER_MESSAGE,
        }
    ]
    assert stored == []
    body[field] = (
        text.replace("NaN", "1.5").replace("-Infinity", "2").replace("1e400", "3")
    )
    response = client.post("/api/v1/tracking/events", json=body)
    assert response.status_code == 200, response.text
    assert len(stored) == 1


def test_the_string_nan_in_a_free_form_field_is_stored_as_text(client, stored):
    response = client.post(
        "/api/v1/tracking/track",
        content=_event(metadata='{"note":"NaN","big":1e308,"zero":-0.0}'),
        headers={"Content-Type": JSON},
    )
    assert response.status_code == 200, response.text
    assert stored[0].event_metadata == {"note": "NaN", "big": 1e308, "zero": -0.0}


# ---------------------------------------------------------------------------
# V3: bodies FastAPI does not parse
# ---------------------------------------------------------------------------

UNPARSED = ["text/json", "text/plain", "application/x-www-form-urlencoded", None]


@pytest.mark.parametrize("content_type", UNPARSED, ids=lambda c: c or "none")
def test_a_body_fastapi_does_not_parse_answers_its_own_422_and_stores_nothing(
    client, stored, content_type
):
    headers = {} if content_type is None else {"Content-Type": content_type}
    response = client.post(
        "/api/v1/tracking/track", content=_event(value="NaN"), headers=headers
    )
    assert response.status_code == 422, response.text
    assert [e["type"] for e in response.json()["detail"]] == ["model_attributes_type"]
    assert stored == []


# ---------------------------------------------------------------------------
# A body that is not JSON
# ---------------------------------------------------------------------------


def test_a_body_that_is_not_json_answers_json_invalid(client, stored):
    response = client.post(
        "/api/v1/tracking/track", content="{bad", headers={"Content-Type": JSON}
    )
    assert response.status_code == 422, response.text
    assert [e["type"] for e in response.json()["detail"]] == ["json_invalid"]
    assert stored == []


@pytest.mark.regression
@pytest.mark.parametrize("body", ["{bad", '{"username":NaN,"password":"p"}'])
def test_the_token_route_reads_its_form_whatever_the_body(body):
    """``/auth/token`` takes a form. Sent as JSON, FastAPI's form handling
    answers 422 for the missing fields; the check neither parses the body
    (which answered 500 for ``{bad``) nor answers for it (``finite_number``)."""
    plain = TestClient(app, raise_server_exceptions=False)
    response = plain.post(
        "/api/v1/auth/token", content=body, headers={"Content-Type": JSON}
    )
    assert response.status_code == 422, response.text
    assert {e["type"] for e in response.json()["detail"]} == {"missing"}


@pytest.mark.modules
@pytest.mark.parametrize("body", ["{bad", '{"SAMLResponse":NaN}'])
def test_the_saml_acs_reads_its_form_whatever_the_body(client, body):
    """The full profile's other form route: FastAPI's form handling reads an
    empty form, and the route answers as it does for any request with no
    ``SAMLResponse``, never 500 and never ``finite_number``."""
    response = client.post(
        f"/api/v1/auth/sso/saml/{UUID}/acs",
        content=body,
        headers={"Content-Type": JSON},
    )
    assert response.status_code < 500, response.text
    assert FINITE_NUMBER_TYPE not in response.text
    form = client.post(f"/api/v1/auth/sso/saml/{UUID}/acs", data={})
    assert (response.status_code, response.text) == (form.status_code, form.text)
