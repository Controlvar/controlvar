"""``event_id`` (#1543): the caller's id goes out as ``event_id`` on ``/track``, on every fan-out
entry and in ``track_batch``, is left out when not given, and is the same on a 429 retry. The SDK
makes no id."""

from __future__ import annotations

from unittest import mock

import pytest

from controlvar import ExperimentationClient, TransportError
from controlvar.testing import FakeTransport

API_URL = "http://localhost:8000"
API_KEY = "sk-test"

ASSIGN = {
    "experiment_key": "checkout_flow",
    "user_id": "user-1",
    "variant_id": "var-2",
    "variant_name": "treatment",
    "is_control": False,
    "configuration": None,
}
FLAG = {"key": "new_search", "enabled": True, "config": None}


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def client(transport: FakeTransport) -> ExperimentationClient:
    return ExperimentationClient(API_URL, API_KEY, transport=transport)


def fanned_out_client(transport: FakeTransport, client: ExperimentationClient) -> ExperimentationClient:
    """*client* has answered for user-1 in one experiment and one flag, so a key-less event fans out to both."""
    transport.route("POST", "/api/v1/tracking/assign", json=ASSIGN)
    transport.route("GET", "/api/v1/feature-flags/evaluate/new_search", json=FLAG)
    transport.route("POST", "/api/v1/tracking/batch", json={"success_count": 2, "failure_count": 0, "errors": None})
    client.get_assignment("checkout_flow", "user-1")
    client.get_feature_flag("new_search", "user-1")
    return client


class TestEventId:
    def test_sends_event_id_on_a_track_with_an_explicit_key(self, client, transport):
        transport.route("POST", "/api/v1/tracking/track", json={})
        assert client.track("user-1", "purchase", experiment_key="checkout_flow", event_id="order-42") is True
        assert transport.calls() == ["POST /api/v1/tracking/track"]
        assert transport.last.json() == {
            "event_type": "purchase",
            "event_name": "purchase",
            "user_id": "user-1",
            "experiment_key": "checkout_flow",
            "event_id": "order-42",
        }

    def test_sends_event_id_on_a_track_with_nothing_to_attribute_it_to(self, client, transport):
        transport.route("POST", "/api/v1/tracking/track", json={})
        client.track("nobody", "page_view", event_id="view-1")
        assert transport.calls() == ["POST /api/v1/tracking/track"]
        assert transport.last.json()["event_id"] == "view-1"

    def test_sends_the_same_event_id_on_every_fan_out_entry(self, client, transport):
        fanned_out_client(transport, client)
        assert client.track("user-1", "purchase", event_value=5, event_id="order-42") is True
        assert transport.calls()[-1] == "POST /api/v1/tracking/batch"
        events = transport.last.json()["events"]
        assert [e.get("experiment_key") or e.get("feature_flag_key") for e in events] == [
            "checkout_flow",
            "new_search",
        ]
        assert [e["event_id"] for e in events] == ["order-42", "order-42"]

    def test_sends_no_event_id_on_any_entry_when_none_is_given(self, client, transport):
        fanned_out_client(transport, client)
        client.track("user-1", "purchase")
        events = transport.last.json()["events"]
        assert len(events) == 2
        assert all("event_id" not in e for e in events)

    @pytest.mark.parametrize("given", [None, ""], ids=["none", "empty"])
    def test_sends_no_event_id_for_none_or_an_empty_string(self, client, transport, given):
        transport.route("POST", "/api/v1/tracking/track", json={})
        client.track("user-1", "purchase", experiment_key="checkout_flow", event_id=given)
        assert "event_id" not in transport.last.json()

    def test_makes_no_id_of_its_own_two_tracks_without_an_event_id_send_none(self, client, transport):
        transport.route("POST", "/api/v1/tracking/track", json={})
        client.track("user-1", "purchase", experiment_key="checkout_flow")
        client.track("user-1", "purchase", experiment_key="checkout_flow")
        assert [("event_id" in r.json()) for r in transport.requests] == [False, False]

    def test_track_batch_passes_each_events_event_id_through_as_given(self, client, transport):
        transport.route("POST", "/api/v1/tracking/batch", json={"success_count": 3, "failure_count": 0})
        client.track_batch(
            [
                {"event_name": "purchase", "user_id": "user-1", "experiment_key": "x", "event_id": "order-1"},
                {"event_name": "refund", "user_id": "user-1", "feature_flag_key": "f", "event_id": "refund-1"},
                {"event_name": "click", "user_id": "user-1", "experiment_key": "x"},
            ]
        )
        events = transport.last.json()["events"]
        assert [e.get("event_id") for e in events] == ["order-1", "refund-1", None]
        assert "event_id" not in events[2]

    def test_sends_the_same_event_id_when_a_429_is_retried(self, client, transport):
        client._sleep = mock.Mock()
        transport.respond(429, headers={"Retry-After": "0"}).respond(200, json={})
        assert client.track("user-1", "purchase", experiment_key="checkout_flow", event_id="order-42") is True
        assert len(transport.requests) == 2
        assert [r.json()["event_id"] for r in transport.requests] == ["order-42", "order-42"]

    def test_sends_the_same_event_id_on_every_entry_when_a_fanned_out_429_is_retried(self, client, transport):
        fanned_out_client(transport, client)
        client._sleep = mock.Mock()
        before = len(transport.requests)
        transport.respond(429, headers={"Retry-After": "0"}).respond(200, json={"success_count": 2, "failure_count": 0})
        client.track("user-1", "purchase", event_id="order-42")
        sent = transport.requests[before:]
        assert len(sent) == 2
        for request in sent:
            assert [e["event_id"] for e in request.json()["events"]] == ["order-42", "order-42"]

    def test_does_not_send_a_request_that_failed_on_the_network_again(self, client, transport):
        transport.fail(TransportError("timed out"))
        assert client.track("user-1", "purchase", experiment_key="checkout_flow", event_id="order-42") is False
        assert len(transport.requests) == 1
