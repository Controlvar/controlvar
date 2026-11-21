"""``alias(anonymous_id, user_id)`` (#449): one ``POST /api/v1/tracking/alias`` with the client's key.

* It returns ``created``; any other answer raises :class:`ExperimentationError`
  carrying the server's status and body, as ``get_assignment`` does, and a 429 is
  retried once.
* On a 200 the client copies the anonymous id's experiment keys (never flag keys)
  to the known id, read through ``keys()``, with the 24 hours starting at the
  alias, and drops the known id's cached assignments. The anonymous id's own
  events keep the anonymous id.

HTTP is faked with :class:`FakeTransport`: no network.
"""

from __future__ import annotations

import json
import socket
from unittest import mock

import pytest

from controlvar import ExperimentationClient, ExperimentationError, TransportError
from controlvar.testing import FakeTransport

pytestmark = pytest.mark.regression

API_URL = "http://localhost:8000"
API_KEY = "server-key"
ANON = "visitor-7f3a92"
KNOWN = "customer-1042"
HOUR = 3600.0
T0 = 1_000_000.0

NOT_SERVER_SIDE = (
    "This API key does not have the 'sdk:ruleset' scope. Create a key with the "
    "'sdk:ruleset' scope for server-side SDK use."
)
LINKED_ELSEWHERE = (
    "That anonymous_id is already linked to a different user_id. An anonymous_id can be "
    "linked to one user_id."
)
EQUAL = [{"type": "equal_ids", "loc": ["body"], "msg": "anonymous_id and user_id must differ."}]


def assigned(user_id, experiment_key="pricing", variant="treatment", reason="assigned"):
    return {
        "experiment_key": experiment_key,
        "user_id": user_id,
        "variant_id": f"var-{variant}",
        "variant_name": variant,
        "is_control": variant == "control",
        "configuration": None,
        "assigned": reason == "assigned",
        "reason": reason,
    }


def linked(created):
    return {"anonymous_id": ANON, "user_id": KNOWN, "created": created}


FLAG_ON = {"key": "new_search", "enabled": True, "config": None}
BATCH_OK = {"success_count": 1, "failure_count": 0, "errors": []}


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def client(transport: FakeTransport) -> ExperimentationClient:
    made = ExperimentationClient(API_URL, API_KEY, transport=transport)
    made._sleep = mock.Mock()  # a 429's wait
    return made


def sent(transport: FakeTransport, index: int):
    request = transport.requests[index]
    return request.path, request.json()


# --------------------------------------------------------------- IA-SDK1: the request


def test_alias_sends_one_post_with_the_two_ids_in_the_body_and_the_client_key(client, transport):
    transport.respond(json=linked(True))
    client.alias(ANON, KNOWN)
    assert transport.calls() == ["POST /api/v1/tracking/alias"]
    request = transport.last
    assert request.url == "http://localhost:8000/api/v1/tracking/alias"
    assert request.headers["X-API-Key"] == API_KEY
    assert request.headers["Content-Type"] == "application/json"
    assert request.json() == {"anonymous_id": ANON, "user_id": KNOWN}


def test_alias_returns_created_as_the_server_answered_it_true_then_false(client, transport):
    transport.respond(json=linked(True)).respond(json=linked(False))
    assert client.alias(ANON, KNOWN) is True
    assert client.alias(ANON, KNOWN) is False


def test_a_200_whose_body_has_no_boolean_created_raises(client, transport):
    transport.respond(json={"anonymous_id": ANON, "user_id": KNOWN, "created": "yes"})
    with pytest.raises(ExperimentationError) as raised:
        client.alias(ANON, KNOWN)
    assert raised.value.status == 200
    assert "unexpected body" in raised.value.message


# ------------------------------------------------- IA-SDK2: a failure reaches the caller


@pytest.mark.parametrize(
    ("status", "body"),
    [
        pytest.param(404, {"detail": "Not Found"}, id="404-a-server-without-the-route"),
        pytest.param(403, {"detail": NOT_SERVER_SIDE}, id="403-not-a-server-side-key"),
        pytest.param(409, {"detail": LINKED_ELSEWHERE}, id="409-a-rule-refuses-the-link"),
        pytest.param(422, {"detail": EQUAL}, id="422-an-id-the-server-refuses"),
    ],
)
def test_alias_raises_experimentation_error_with_the_status_and_the_server_body(client, transport, status, body):
    transport.respond(status, json=body)
    with pytest.raises(ExperimentationError) as raised:
        client.alias(ANON, KNOWN)
    assert raised.value.status == status
    assert json.loads(raised.value.body) == body
    assert raised.value.message == f"POST /api/v1/tracking/alias failed with HTTP {status}"
    assert transport.calls() == ["POST /api/v1/tracking/alias"]


def test_alias_raises_on_a_network_failure_with_no_status_and_does_not_send_it_again(client, transport):
    transport.fail(TransportError("the host refused it"))
    with pytest.raises(ExperimentationError) as raised:
        client.alias(ANON, KNOWN)
    assert raised.value.status is None
    assert transport.calls() == ["POST /api/v1/tracking/alias"]


def test_alias_raises_on_a_timeout_with_no_status(client, transport):
    transport.fail(socket.timeout("timed out"))
    with pytest.raises(ExperimentationError) as raised:
        client.alias(ANON, KNOWN)
    assert raised.value.status is None
    assert raised.value.message == "POST http://localhost:8000/api/v1/tracking/alias failed: timed out"


def test_alias_retries_a_429_once_as_every_call_does(client, transport):
    transport.respond(429, headers={"Retry-After": "1"}).respond(json=linked(True))
    assert client.alias(ANON, KNOWN) is True
    assert transport.calls() == ["POST /api/v1/tracking/alias"] * 2
    assert transport.requests[0].json() == transport.requests[1].json()
    client._sleep.assert_called_once_with(1.0)


def test_alias_raises_on_a_429_that_is_still_a_429_after_its_one_retry(client, transport):
    transport.respond(429, headers={"Retry-After": "1"}).respond(429, headers={"Retry-After": "1"})
    with pytest.raises(ExperimentationError) as raised:
        client.alias(ANON, KNOWN)
    assert raised.value.status == 429


def test_a_failed_alias_keeps_nothing_calling_it_again_sends_it_again(client, transport):
    transport.respond(json=assigned(ANON))
    transport.respond(409, json={"detail": LINKED_ELSEWHERE})
    transport.respond(json={})
    transport.respond(json=linked(True))
    client.get_assignment("pricing", ANON)
    with pytest.raises(ExperimentationError):
        client.alias(ANON, KNOWN)
    # Nothing was copied: the known id's key-less event has nothing to name.
    assert client.track(KNOWN, "purchase") is True
    path, body = sent(transport, 2)
    assert path == "/api/v1/tracking/track" and "experiment_key" not in body
    assert client.alias(ANON, KNOWN) is True
    assert transport.requests[3].path == "/api/v1/tracking/alias"


# ----------------------------------------------- IA-SDK3: the fan-out follows the link


def test_after_alias_a_key_less_track_for_the_known_id_carries_the_anonymous_ids_experiment(client, transport):
    transport.respond(json=assigned(ANON)).respond(json=linked(True)).respond(json=BATCH_OK)
    client.get_assignment("pricing", ANON)
    client.alias(ANON, KNOWN)
    assert client.track(KNOWN, "purchase", event_value=30) is True
    path, body = sent(transport, 2)
    assert path == "/api/v1/tracking/batch"
    assert body["events"] == [
        {
            "event_type": "purchase",
            "event_name": "purchase",
            "user_id": KNOWN,
            "value": 30.0,
            "experiment_key": "pricing",
        }
    ]


def test_alias_copies_experiment_keys_only_never_flag_keys(client, transport):
    transport.respond(json=assigned(ANON)).respond(json=FLAG_ON).respond(json=linked(True)).respond(json=BATCH_OK)
    client.get_assignment("pricing", ANON)
    client.get_feature_flag("new_search", ANON)
    client.alias(ANON, KNOWN)
    client.track(KNOWN, "purchase")
    _, body = sent(transport, 3)
    assert [(e.get("experiment_key"), e.get("feature_flag_key")) for e in body["events"]] == [("pricing", None)]


def test_alias_copies_what_keys_gives_not_a_paused_experiment_nor_one_past_its_24_hours(transport):
    client = ExperimentationClient(API_URL, API_KEY, transport=transport, cache_ttl_seconds=0)
    transport.respond(json=assigned(ANON, "old_test"))
    transport.respond(json=assigned(ANON, "paused_test", "control", "paused"))
    transport.respond(json=assigned(ANON, "pricing"))
    transport.respond(json=linked(True)).respond(json=BATCH_OK)
    with mock.patch("controlvar.cache._now") as now:
        now.return_value = T0
        client.get_assignment("old_test", ANON)
        now.return_value = T0 + 25 * HOUR
        client.get_assignment("paused_test", ANON)
        client.get_assignment("pricing", ANON)
        client.alias(ANON, KNOWN)
        client.track(KNOWN, "purchase")
    _, body = sent(transport, 4)
    assert [e["experiment_key"] for e in body["events"]] == ["pricing"]


def test_the_copied_keys_24_hours_start_at_the_alias(client, transport):
    transport.respond(json=assigned(ANON)).respond(json=linked(True)).respond(json=BATCH_OK).respond(json={})
    with mock.patch("controlvar.cache._now") as now:
        now.return_value = T0
        client.get_assignment("pricing", ANON)
        now.return_value = T0 + 23 * HOUR
        client.alias(ANON, KNOWN)
        # 25 hours after the anonymous id's answer, 2 after the alias: still sent to the experiment.
        now.return_value = T0 + 25 * HOUR
        client.track(KNOWN, "purchase")
        # More than 24 hours after the alias: the key is gone.
        now.return_value = T0 + 23 * HOUR + 24 * HOUR + 1
        client.track(KNOWN, "purchase")
    path, body = sent(transport, 2)
    assert path == "/api/v1/tracking/batch" and body["events"][0]["experiment_key"] == "pricing"
    path, body = sent(transport, 3)
    assert path == "/api/v1/tracking/track" and "experiment_key" not in body


def test_the_copy_helps_only_the_client_that_assigned_the_anonymous_id(client, transport):
    """A backend client that never answered the anonymous id has nothing to copy, so it
    assigns the known id before a key-less track."""
    transport.respond(json=linked(True)).respond(json={}).respond(json=assigned(KNOWN)).respond(json=BATCH_OK)
    client.alias(ANON, KNOWN)
    client.track(KNOWN, "purchase")
    path, body = sent(transport, 1)
    assert path == "/api/v1/tracking/track" and "experiment_key" not in body
    client.get_assignment("pricing", KNOWN)
    client.track(KNOWN, "purchase")
    path, body = sent(transport, 3)
    assert path == "/api/v1/tracking/batch" and body["events"][0]["experiment_key"] == "pricing"


# ----------------------------------------------- IA-SDK4: the known id is asked again


def test_alias_drops_the_known_ids_cached_assignments_so_its_next_get_assignment_asks_the_server(
    client, transport
):
    transport.respond(json=assigned(KNOWN, "pricing", "control"))
    transport.respond(json=linked(True))
    transport.respond(json=assigned(KNOWN, "pricing", "treatment"))
    assert client.get_assignment("pricing", KNOWN).variant_name == "control"
    client.alias(ANON, KNOWN)
    assert client.cached_assignments(KNOWN) == []
    assert client.get_assignment("pricing", KNOWN).variant_name == "treatment"
    assert len(transport.requests) == 3


def test_alias_keeps_the_anonymous_ids_cached_assignments_and_the_known_ids_cached_flags(client, transport):
    transport.respond(json=assigned(ANON)).respond(json=FLAG_ON).respond(json=linked(True))
    client.get_assignment("pricing", ANON)
    client.get_feature_flag("new_search", KNOWN)
    client.alias(ANON, KNOWN)
    assert client.get_assignment("pricing", ANON).variant_name == "treatment"
    assert client.get_feature_flag("new_search", KNOWN).enabled is True
    assert len(transport.requests) == 3


def test_a_failed_alias_drops_nothing_the_known_ids_cached_assignment_is_still_served(client, transport):
    transport.respond(json=assigned(KNOWN, "pricing", "control"))
    transport.respond(403, json={"detail": NOT_SERVER_SIDE})
    client.get_assignment("pricing", KNOWN)
    with pytest.raises(ExperimentationError):
        client.alias(ANON, KNOWN)
    assert client.get_assignment("pricing", KNOWN).variant_name == "control"
    assert len(transport.requests) == 2


# ------------------------------------- IA-SDK5: events keep the id they were sent with


def test_after_alias_the_anonymous_ids_own_events_keep_the_anonymous_id_and_its_experiment(client, transport):
    """The client has no queue: an event is built and sent when ``track`` is called. So what
    is pinned is that the link changes nothing about the anonymous id's own events."""
    transport.respond(json=assigned(ANON)).respond(json=linked(True)).respond(json=BATCH_OK).respond(json=BATCH_OK)
    client.get_assignment("pricing", ANON)
    client.alias(ANON, KNOWN)
    client.track(ANON, "page_view")
    client.track_batch([{"event_name": "scroll", "user_id": ANON, "experiment_key": "pricing"}])
    _, body = sent(transport, 2)
    assert [(e["user_id"], e["experiment_key"]) for e in body["events"]] == [(ANON, "pricing")]
    _, body = sent(transport, 3)
    assert [(e["user_id"], e["experiment_key"]) for e in body["events"]] == [(ANON, "pricing")]
