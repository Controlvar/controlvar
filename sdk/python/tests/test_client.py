"""ExperimentationClient unit tests. The HTTP layer is replaced by FakeTransport so every
test can assert the exact method, URL, headers and JSON body that would hit the backend."""

from __future__ import annotations

import dataclasses
import inspect
import json
import threading
from datetime import datetime, timezone
from unittest import mock

import pytest

from controlvar import (
    BATCH_LIMIT,
    Assignment,
    BatchResult,
    ExperimentationClient,
    ExperimentationError,
    FlagEvaluation,
    TransportError,
)
from controlvar.testing import FakeTransport

API_URL = "http://localhost:8000"
API_KEY = "sk-test"

ASSIGN_RESPONSE = {
    "experiment_key": "checkout_flow",
    "user_id": "user-1",
    "variant_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "variant_name": "treatment",
    "is_control": False,
    "configuration": {"button": "green"},
}
FLAG_RESPONSE = {"key": "new_search", "enabled": True, "config": {"variant": "beta", "engine": "v2"}}
BATCH_OK = {"success_count": 2, "failure_count": 0, "errors": None}


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def client(transport: FakeTransport) -> ExperimentationClient:
    return ExperimentationClient(API_URL, API_KEY, transport=transport)


def assign_route(transport: FakeTransport, **overrides) -> None:
    transport.route("POST", "/api/v1/tracking/assign", json={**ASSIGN_RESPONSE, **overrides})


def flag_route(transport: FakeTransport, key: str = "new_search", **overrides) -> None:
    transport.route("GET", f"/api/v1/feature-flags/evaluate/{key}", json={**FLAG_RESPONSE, "key": key, **overrides})


def batch_route(transport: FakeTransport, **overrides) -> None:
    transport.route("POST", "/api/v1/tracking/batch", json={**BATCH_OK, **overrides})


# --------------------------------------------------------------------------- construction


class TestConstruction:
    def test_requires_api_url_and_api_key(self):
        with pytest.raises(ValueError, match="api_url"):
            ExperimentationClient("", "key")
        with pytest.raises(ValueError, match="api_key"):
            ExperimentationClient(API_URL, "")

    def test_defaults_and_trailing_slash(self, transport):
        client = ExperimentationClient("http://example.test/", "key", transport=transport)
        assert (client.timeout_seconds, client.cache_ttl_seconds, client.default_variant) == (5.0, 300, "control")
        flag_route(transport)
        client.get_feature_flag("new_search", "u")
        assert transport.last.url == "http://example.test/api/v1/feature-flags/evaluate/new_search?user_id=u"

    def test_headers_and_timeout_reach_the_transport(self, transport):
        client = ExperimentationClient(API_URL, API_KEY, timeout_seconds=1.5, transport=transport)
        flag_route(transport)
        client.get_feature_flag("new_search", "u")
        request = transport.last
        assert request.headers["X-API-Key"] == API_KEY
        assert request.headers["Content-Type"] == "application/json"
        assert request.headers["Accept"] == "application/json"
        assert request.headers["User-Agent"].startswith("controlvar-python/")
        assert request.timeout == 1.5


# ----------------------------------------------------------------------------- assignment


class TestGetAssignment:
    def test_sends_post_with_context(self, client, transport):
        assign_route(transport)
        client.get_assignment("checkout_flow", "user-1", {"country": "US", "plan": "pro"})
        request = transport.last
        assert request.method == "POST"
        assert request.url == f"{API_URL}/api/v1/tracking/assign"
        assert request.json() == {
            "experiment_key": "checkout_flow",
            "user_id": "user-1",
            "context": {"country": "US", "plan": "pro"},
        }

    def test_omits_context_without_attributes(self, client, transport):
        assign_route(transport)
        client.get_assignment("checkout_flow", "user-1")
        assert transport.last.json() == {"experiment_key": "checkout_flow", "user_id": "user-1"}

    def test_maps_response(self, client, transport):
        assign_route(transport)
        assert client.get_assignment("checkout_flow", "user-1") == Assignment(
            experiment_key="checkout_flow",
            user_id="user-1",
            variant_id="3fa85f64-5717-4562-b3fc-2c963f66afa6",
            variant_name="treatment",
            is_control=False,
            configuration={"button": "green"},
        )

    def test_carries_assigned_true_and_reason(self, client, transport):
        assign_route(transport, assigned=True, reason="assigned")
        assignment = client.get_assignment("checkout_flow", "user-1")
        assert assignment.assigned is True
        assert assignment.reason == "assigned"

    @pytest.mark.parametrize("reason", ["holdout", "mutual_exclusion", "targeting"])
    def test_carries_assigned_false_and_reason_for_an_unenrolled_user(self, client, transport, reason):
        assign_route(transport, variant_name="control", is_control=True, assigned=False, reason=reason)
        assignment = client.get_assignment("checkout_flow", "user-1")
        assert assignment.assigned is False
        assert assignment.reason == reason
        assert assignment.is_control is True

    @pytest.mark.parametrize("reason", ["draft", "paused", "completed", "archived"])
    def test_carries_assigned_false_and_its_status_for_an_experiment_that_is_not_active(
        self, client, transport, reason
    ):
        assign_route(transport, variant_name="control", is_control=True, assigned=False, reason=reason)
        assignment = client.get_assignment("checkout_flow", "user-1")
        assert assignment.assigned is False
        assert assignment.reason == reason
        assert assignment.is_control is True

    def test_assigned_and_reason_are_none_not_false_for_an_older_server(self, client, transport):
        # ASSIGN_RESPONSE has neither field, as a server that predates them answers.
        assign_route(transport)
        assignment = client.get_assignment("checkout_flow", "user-1")
        assert assignment.assigned is None
        assert assignment.reason is None

    def test_control_variant_with_null_configuration(self, client, transport):
        assign_route(transport, variant_name="control", is_control=True, configuration=None)
        assignment = client.get_assignment("checkout_flow", "user-1")
        assert assignment.is_control is True
        assert assignment.configuration is None

    def test_sticky_cache_hit_makes_one_request(self, client, transport):
        assign_route(transport)
        first = client.get_assignment("checkout_flow", "user-1")
        second = client.get_assignment("checkout_flow", "user-1")
        assert first is second
        assert transport.calls() == ["POST /api/v1/tracking/assign"]

    def test_cache_is_per_user_and_key(self, client, transport):
        assign_route(transport)
        client.get_assignment("checkout_flow", "user-1")
        client.get_assignment("checkout_flow", "user-2")
        client.get_assignment("hero_banner", "user-1")
        assert len(transport.requests) == 3

    def test_404_raises_with_status_and_body(self, client, transport):
        transport.respond(404, json={"detail": "No experiment has the key 'nope'."})
        with pytest.raises(ExperimentationError) as info:
            client.get_assignment("nope", "user-1")
        assert info.value.status == 404
        assert "No experiment has the key 'nope'." in info.value.body
        assert "404" in str(info.value)

    def test_failure_is_never_cached(self, client, transport):
        transport.respond(500, body=b"boom")
        assign_route(transport)
        with pytest.raises(ExperimentationError):
            client.get_assignment("checkout_flow", "user-1")
        assert client.get_assignment("checkout_flow", "user-1").variant_name == "treatment"
        assert len(transport.requests) == 2

    @pytest.mark.parametrize("exc", [TransportError("connection refused"), RuntimeError("boom")])
    def test_transport_failure_raises_experimentation_error(self, client, transport, exc):
        transport.fail(exc)
        with pytest.raises(ExperimentationError) as info:
            client.get_assignment("checkout_flow", "user-1")
        assert info.value.status is None
        assert client.cached_assignments("user-1") == []

    @pytest.mark.parametrize("payload", [{"json": {"unexpected": True}}, {"body": b"<html>"}])
    def test_malformed_response_raises(self, client, transport, payload):
        transport.respond(200, **payload)
        with pytest.raises(ExperimentationError):
            client.get_assignment("checkout_flow", "user-1")


class TestGetVariant:
    def test_returns_variant_name_and_forwards_attributes(self, client, transport):
        assign_route(transport)
        assert client.get_variant("checkout_flow", "user-1", {"plan": "pro"}) == "treatment"
        assert transport.last.json()["context"] == {"plan": "pro"}

    def test_returns_default_on_404(self, client, transport):
        transport.respond(404, json={"detail": "not found"})
        assert client.get_variant("checkout_flow", "user-1") == "control"

    def test_returns_custom_default_on_network_error(self, transport):
        client = ExperimentationClient(API_URL, API_KEY, default_variant="baseline", transport=transport)
        transport.fail(TransportError("down"))
        assert client.get_variant("checkout_flow", "user-1") == "baseline"


# -------------------------------------------------------------------------- feature flags


class TestGetFeatureFlag:
    def test_url_encodes_key_and_user_id(self, client, transport):
        transport.respond(200, json={"key": "my flag/β", "enabled": False, "config": None})
        client.get_feature_flag("my flag/β", "u 1")
        request = transport.last
        assert request.method == "GET"
        assert request.body is None
        assert request.url == f"{API_URL}/api/v1/feature-flags/evaluate/my%20flag%2F%CE%B2?user_id=u%201"
        assert request.query == {"user_id": "u 1"}

    def test_no_context_without_attributes(self, client, transport):
        flag_route(transport)
        client.get_feature_flag("new_search", "user-1")
        assert transport.last.url == f"{API_URL}/api/v1/feature-flags/evaluate/new_search?user_id=user-1"
        assert transport.last.query == {"user_id": "user-1"}

    def test_sends_attributes_as_url_encoded_json_context(self, client, transport):
        flag_route(transport)
        client.get_feature_flag("new_search", "user-1", {"device": "mobile", "country": "US"})
        assert transport.last.method == "GET"
        assert transport.last.body is None
        assert transport.last.url == (
            f"{API_URL}/api/v1/feature-flags/evaluate/new_search"
            "?user_id=user-1&context=%7B%22device%22%3A%22mobile%22%2C%22country%22%3A%22US%22%7D"
        )

    def test_context_round_trips_nested_attributes(self, client, transport):
        flag_route(transport)
        attributes = {"os": "iOS", "os_version": "17.4.0", "employee": False, "app": {"version": "3.2.1"}, "n": 3}
        client.get_feature_flag("new_search", "user-1", attributes)
        assert json.loads(transport.last.query["context"]) == attributes
        # compact separators: no spaces in the wire form
        assert " " not in transport.last.query["context"]

    def test_empty_attributes_send_no_context(self, client, transport):
        flag_route(transport)
        client.get_feature_flag("new_search", "user-1", {})
        assert transport.last.query == {"user_id": "user-1"}

    def test_context_is_encoded_alongside_key_and_user_id(self, client, transport):
        transport.respond(200, json={"key": "my flag", "enabled": False, "config": None})
        client.get_feature_flag("my flag", "u 1", {"q": "a&b=c"})
        assert transport.last.url == (
            f"{API_URL}/api/v1/feature-flags/evaluate/my%20flag?user_id=u%201&context=%7B%22q%22%3A%22a%26b%3Dc%22%7D"
        )
        assert transport.last.query == {"user_id": "u 1", "context": '{"q":"a&b=c"}'}

    def test_maps_response(self, client, transport):
        flag_route(transport)
        assert client.get_feature_flag("new_search", "user-1") == FlagEvaluation(
            key="new_search", enabled=True, config={"variant": "beta", "engine": "v2"}
        )

    def test_maps_reason_when_present(self, client, transport):
        flag_route(transport, reason="targeting_rule")
        evaluation = client.get_feature_flag("new_search", "user-1", {"country": "US"})
        assert evaluation.reason == "targeting_rule"
        assert evaluation == FlagEvaluation(
            key="new_search", enabled=True, config={"variant": "beta", "engine": "v2"}, reason="targeting_rule"
        )

    def test_reason_is_none_when_absent_or_not_a_string(self, client, transport):
        flag_route(transport)
        assert client.get_feature_flag("new_search", "user-1").reason is None
        flag_route(transport, key="other", reason=7)
        assert client.get_feature_flag("other", "user-1").reason is None

    def test_cache_key_ignores_attributes_until_clear_cache(self, client, transport):
        flag_route(transport)
        client.get_feature_flag("new_search", "user-1", {"country": "US"})
        client.get_feature_flag("new_search", "user-1", {"country": "DE"})
        assert len(transport.requests) == 1
        client.clear_cache()
        client.get_feature_flag("new_search", "user-1", {"country": "DE"})
        assert len(transport.requests) == 2
        assert transport.last.query["context"] == '{"country":"DE"}'

    def test_disabled_flag_with_null_config(self, client, transport):
        flag_route(transport, enabled=False, config=None)
        assert client.get_feature_flag("new_search", "user-1") == FlagEvaluation("new_search", False, None)

    def test_cache_expires_after_ttl(self, client, transport):
        flag_route(transport)
        with mock.patch("controlvar.cache._now") as now:
            now.return_value = 1000.0
            client.get_feature_flag("new_search", "user-1")
            now.return_value = 1299.0
            client.get_feature_flag("new_search", "user-1")
            assert len(transport.requests) == 1
            now.return_value = 1300.0
            client.get_feature_flag("new_search", "user-1")
            assert len(transport.requests) == 2

    def test_custom_cache_ttl(self, transport):
        client = ExperimentationClient(API_URL, API_KEY, cache_ttl_seconds=10, transport=transport)
        flag_route(transport)
        with mock.patch("controlvar.cache._now") as now:
            now.return_value = 0.0
            client.get_feature_flag("new_search", "user-1")
            now.return_value = 9.0
            client.get_feature_flag("new_search", "user-1")
            now.return_value = 10.0
            client.get_feature_flag("new_search", "user-1")
        assert len(transport.requests) == 2

    def test_404_raises_and_is_not_cached(self, client, transport):
        transport.respond(404, json={"detail": "Feature flag with key 'x' not found"})
        flag_route(transport, key="x")
        with pytest.raises(ExperimentationError) as info:
            client.get_feature_flag("x", "user-1")
        assert info.value.status == 404
        assert client.cached_flags("user-1") == []
        assert client.get_feature_flag("x", "user-1").enabled is True
        assert len(transport.requests) == 2


class TestIsFeatureEnabled:
    def test_true_and_false(self, client, transport):
        flag_route(transport, key="on", enabled=True)
        flag_route(transport, key="off", enabled=False)
        assert client.is_feature_enabled("on", "user-1") is True
        assert client.is_feature_enabled("off", "user-1") is False

    def test_forwards_attributes_as_context(self, client, transport):
        flag_route(transport, key="on", enabled=True)
        assert client.is_feature_enabled("on", "user-1", {"tier": "premium"}) is True
        assert transport.last.query == {"user_id": "user-1", "context": '{"tier":"premium"}'}

    def test_false_on_404(self, client, transport):
        transport.respond(404, json={"detail": "not found"})
        assert client.is_feature_enabled("missing", "user-1") is False

    def test_false_on_network_error(self, client, transport):
        transport.fail(TransportError("down"))
        assert client.is_feature_enabled("new_search", "user-1") is False


class TestGetAllFlags:
    def test_url_and_mapping(self, client, transport):
        transport.route("GET", "/api/v1/feature-flags/user/user%201", json={"a": True, "b": False, "c": 1})
        assert client.get_all_flags("user 1") == {"a": True, "b": False, "c": True}
        assert transport.last.url == f"{API_URL}/api/v1/feature-flags/user/user%201"
        assert transport.last.method == "GET"

    def test_sends_attributes_as_context(self, client, transport):
        transport.route("GET", "/api/v1/feature-flags/user/user%201", json={"a": True})
        assert client.get_all_flags("user 1", {"device": "mobile", "country": "US"}) == {"a": True}
        assert transport.last.url == (
            f"{API_URL}/api/v1/feature-flags/user/user%201"
            "?context=%7B%22device%22%3A%22mobile%22%2C%22country%22%3A%22US%22%7D"
        )
        client.get_all_flags("user 1", {})
        assert transport.last.url == f"{API_URL}/api/v1/feature-flags/user/user%201"

    def test_not_cached_and_raises_on_error(self, client, transport):
        transport.route("GET", "/api/v1/feature-flags/user/u", json={"a": True})
        client.get_all_flags("u")
        client.get_all_flags("u")
        assert len(transport.requests) == 2
        transport.respond(401, json={"detail": "Invalid API key"})
        with pytest.raises(ExperimentationError) as info:
            client.get_all_flags("u")
        assert info.value.status == 401


# --------------------------------------------------------------------------- typed values

GETTERS = ("get_string_value", "get_number_value", "get_json_value")
#: A default of each getter's type, different from every value served below.
DEFAULTS = {"get_string_value": "the default", "get_number_value": -1, "get_json_value": {"the": "default"}}


def served(**fields):
    """An evaluate answer for flag ``f``."""
    return {"key": "f", "enabled": True, "config": None, "reason": "rollout", **fields}


def value_client(transport, **kwargs):
    errors = []
    client = ExperimentationClient(
        API_URL, API_KEY, transport=transport, on_error=lambda error, operation: errors.append((error, operation)), **kwargs
    )
    return client, errors


class TestFlagEvaluationValue:
    def test_copies_the_servers_value_and_value_type(self, client, transport):
        transport.respond(200, json=served(value="Complete purchase", value_type="string"))
        assert client.get_feature_flag("f", "user-1") == FlagEvaluation(
            key="f", enabled=True, config=None, reason="rollout", value="Complete purchase", value_type="string"
        )

    def test_both_are_none_when_an_older_server_sends_neither(self, client, transport):
        flag_route(transport)
        evaluation = client.get_feature_flag("new_search", "user-1")
        assert (evaluation.value, evaluation.value_type) == (None, None)

    def test_a_value_type_that_is_not_a_string_is_none(self, client, transport):
        transport.respond(200, json=served(value="x", value_type=7))
        assert client.get_feature_flag("f", "user-1").value_type is None

    def test_value_and_value_type_come_after_every_older_field(self):
        assert [f.name for f in dataclasses.fields(FlagEvaluation)] == [
            "key",
            "enabled",
            "config",
            "reason",
            "source",
            "value",
            "value_type",
        ]

    def test_positional_construction_by_existing_callers_keeps_working(self):
        evaluation = FlagEvaluation("k", True, {"a": 1}, "rollout", "local")
        assert (evaluation.key, evaluation.enabled, evaluation.config, evaluation.reason, evaluation.source) == (
            "k",
            True,
            {"a": 1},
            "rollout",
            "local",
        )
        assert (evaluation.value, evaluation.value_type) == (None, None)
        assert FlagEvaluation("k", False).value is None
        full = FlagEvaluation("k", True, None, "rollout", None, "Buy now", "string")
        assert (full.value, full.value_type) == ("Buy now", "string")

    def test_an_experimentation_error_built_by_position_keeps_working(self):
        error = ExperimentationError("GET /x failed with HTTP 404", 404, '{"detail":"no"}')
        assert (error.message, error.status, error.body, error.code) == (
            "GET /x failed with HTTP 404",
            404,
            '{"detail":"no"}',
            None,
        )
        assert TransportError("down").code is None


class TestTypedValues:
    @pytest.mark.parametrize(
        "method, fields, expected",
        [
            ("get_string_value", {"value": "Complete purchase", "value_type": "string"}, "Complete purchase"),
            ("get_string_value", {"value": "", "value_type": "string"}, ""),
            ("get_number_value", {"value": 10, "value_type": "number"}, 10),
            ("get_number_value", {"value": 2.5, "value_type": "number"}, 2.5),
            ("get_number_value", {"value": 0, "value_type": "number"}, 0),
            ("get_number_value", {"value": -(2**53 - 1), "value_type": "number"}, -(2**53 - 1)),
            ("get_json_value", {"value": {"color": "blue"}, "value_type": "json"}, {"color": "blue"}),
            ("get_json_value", {"value": ["a", 1], "value_type": "json"}, ["a", 1]),
            ("get_json_value", {"value": {}, "value_type": "json"}, {}),
        ],
    )
    def test_returns_the_value_the_flag_serves(self, transport, method, fields, expected):
        transport.respond(200, json=served(**fields))
        client, errors = value_client(transport)
        assert getattr(client, method)("f", "user-1", {"plan": "pro"}, default=DEFAULTS[method]) == expected
        assert transport.last.url == f"{API_URL}/api/v1/feature-flags/evaluate/f?user_id=user-1&context=%7B%22plan%22%3A%22pro%22%7D"
        assert errors == []

    def test_an_integral_float_is_a_number_and_stays_a_float(self, transport):
        """OPEN-11: JSON's ``10.0`` reaches Python as a float; it is a number, served as saved."""
        transport.respond(200, body=b'{"key": "f", "enabled": true, "value": 10.0, "value_type": "number"}')
        client, errors = value_client(transport)
        value = client.get_number_value("f", "user-1", default=-1)
        assert value == 10.0 and isinstance(value, float)
        assert errors == []

    @pytest.mark.parametrize("reason", ["rollout", "targeting_rule", "inactive", "error"])
    def test_returns_the_off_value_the_server_sends_when_the_flag_is_off_for_the_user(self, transport, reason):
        transport.respond(200, json=served(enabled=False, reason=reason, value="Buy now", value_type="string"))
        client, errors = value_client(transport)
        assert client.get_string_value("f", "user-1", default="the default") == "Buy now"
        assert errors == []

    def test_is_evaluated_once_per_user_and_flag_for_cache_ttl_seconds_like_get_feature_flag(self, client, transport):
        transport.route("GET", "/api/v1/feature-flags/evaluate/f", json=served(value=10, value_type="number"))
        client.get_number_value("f", "user-1", default=0)
        client.get_number_value("f", "user-1", default=0)
        client.get_feature_flag("f", "user-1")
        assert len(transport.requests) == 1

    def test_cache_ttl_zero_caches_nothing(self, transport):
        transport.route("GET", "/api/v1/feature-flags/evaluate/f", json=served(value=10, value_type="number"))
        client = ExperimentationClient(API_URL, API_KEY, cache_ttl_seconds=0, transport=transport)
        with mock.patch("controlvar.cache._now", return_value=1000.0):
            client.get_number_value("f", "user-1", default=0)
            client.get_number_value("f", "user-1", default=0)
            assert client.cached_flags("user-1") == []
        assert len(transport.requests) == 2

    @pytest.mark.parametrize(
        "label, prepare, status, code",
        [
            ("a network failure", lambda t: t.fail(TransportError("connection refused")), None, None),
            ("an unknown flag (404)", lambda t: t.respond(404, json={"detail": "not found"}), 404, None),
            ("a 500", lambda t: t.respond(500, body=b"boom"), 500, None),
            ("a body that is not JSON", lambda t: t.respond(200, body=b"<html>"), 200, None),
            ("an older server, which sends no value", lambda t: t.respond(200, json=FLAG_RESPONSE), None, "INVALID_RESPONSE"),
            (
                "a stored value the server cannot serve (value_type and no value)",
                lambda t: t.respond(200, json=served(value_type="string")),
                None,
                "INVALID_RESPONSE",
            ),
        ],
    )
    def test_each_returns_the_default_and_reports(self, label, prepare, status, code):
        for method in GETTERS:
            transport = FakeTransport()
            prepare(transport)
            client, errors = value_client(transport)
            assert getattr(client, method)("f", "user-1", default=DEFAULTS[method]) is DEFAULTS[method]
            assert [(type(error), error.status, error.code, operation) for error, operation in errors] == [
                (TransportError if label == "a network failure" else ExperimentationError, status, code, "value")
            ], label

    def test_names_what_an_older_server_lacks(self, transport):
        flag_route(transport)
        client, errors = value_client(transport)
        client.get_string_value("new_search", "user-1", default="x")
        assert errors[0][0].message == (
            'Flag "new_search" came back without a value (from a server without flag values, or one that cannot '
            "serve this flag's stored value); get_string_value returned your default"
        )

    STRING = {"value": "Buy now", "value_type": "string"}
    NUMBER = {"value": 10, "value_type": "number"}
    JSON_VALUE = {"value": {"color": "blue"}, "value_type": "json"}
    BOOLEAN = {"value": True, "value_type": "boolean"}

    @pytest.mark.parametrize(
        "method, name, fields",
        [
            ("get_number_value", "a string", STRING),
            ("get_json_value", "a string", STRING),
            ("get_string_value", "a number", NUMBER),
            ("get_json_value", "a number", NUMBER),
            ("get_string_value", "a JSON value", JSON_VALUE),
            ("get_number_value", "a JSON value", JSON_VALUE),
            ("get_string_value", "a boolean", BOOLEAN),
            ("get_number_value", "a boolean", BOOLEAN),
            ("get_json_value", "a boolean", BOOLEAN),
        ],
    )
    def test_on_a_flag_of_another_type_the_default_and_type_mismatch(self, transport, method, name, fields):
        transport.respond(200, json=served(key="checkout-button-label", **fields))
        client, errors = value_client(transport)
        assert getattr(client, method)("checkout-button-label", "user-1", default=DEFAULTS[method]) is DEFAULTS[method]
        assert len(errors) == 1
        error, operation = errors[0]
        assert (operation, error.code, error.status) == ("value", "TYPE_MISMATCH", None)
        assert error.message == f'Flag "checkout-button-label" serves {name}; {method} returned your default'

    def test_never_coerces_a_string_10_is_not_a_number_and_10_is_not_a_string(self, transport):
        transport.respond(200, json=served(key="a", value="10", value_type="string"))
        transport.respond(200, json=served(key="b", value=10, value_type="number"))
        client, errors = value_client(transport)
        assert client.get_number_value("a", "user-1", default=-1) == -1
        assert client.get_string_value("b", "user-1", default="x") == "x"
        assert [error.code for error, _ in errors] == ["TYPE_MISMATCH", "TYPE_MISMATCH"]

    def test_reads_the_values_own_type_when_the_server_sends_no_value_type(self, transport):
        transport.respond(200, json=served(value="Buy now"))
        client, errors = value_client(transport)
        assert client.get_string_value("f", "user-1", default="x") == "Buy now"
        assert client.get_number_value("f", "user-1", default=-1) == -1
        assert [error.message for error, _ in errors] == ['Flag "f" serves a string; get_number_value returned your default']

    @pytest.mark.parametrize(
        "method, fields",
        [
            ("get_number_value", {"value": "10", "value_type": "number"}),
            ("get_number_value", {"value": True, "value_type": "number"}),
            ("get_number_value", {"value": 2**53, "value_type": "number"}),
            ("get_number_value", {"value": float("inf"), "value_type": "number"}),
            ("get_number_value", {"value": float("nan"), "value_type": "number"}),
            ("get_json_value", {"value": "x", "value_type": "json"}),
            ("get_string_value", {"value": 7, "value_type": "string"}),
        ],
    )
    def test_a_value_that_is_not_of_the_type_the_server_names_is_the_default_never_a_wrong_value(
        self, transport, method, fields
    ):
        transport.respond(200, json=served(**fields))
        client, errors = value_client(transport)
        assert getattr(client, method)("f", "user-1", default=DEFAULTS[method]) is DEFAULTS[method]
        assert [(error.code, operation) for error, operation in errors] == [("INVALID_RESPONSE", "value")]
        assert "but the server's value is not one" in errors[0][0].message

    def test_reports_every_call_that_returns_the_default_not_only_the_first(self, transport):
        transport.route("GET", "/api/v1/feature-flags/evaluate/f", json=served(value="Buy now", value_type="string"))
        client, errors = value_client(transport)
        client.get_number_value("f", "user-1", default=0)
        client.get_number_value("f", "user-1", default=0)
        assert len(errors) == 2
        assert len(transport.requests) == 1  # the second answer came from the cache

    def test_logs_a_warning_as_is_feature_enabled_does(self, transport, caplog):
        transport.respond(404, json={"detail": "not found"})
        client = ExperimentationClient(API_URL, API_KEY, transport=transport)
        with caplog.at_level("WARNING", logger="controlvar.client"):
            assert client.get_string_value("missing", "user-1", default="x") == "x"
        assert [r.getMessage() for r in caplog.records] == [
            "get_string_value(missing, user-1) failed: GET /api/v1/feature-flags/evaluate/missing failed with HTTP 404"
        ]

    def test_never_raises_for_attributes_it_cannot_send_without_on_error_or_with_a_raising_on_error(self, transport):
        client, errors = value_client(transport)
        assert client.get_string_value("f", "user-1", {"tags": {"a"}}, default="x") == "x"
        assert transport.requests == []
        [(error, operation)] = errors
        assert (operation, error.code) == ("value", None)
        assert isinstance(error, ExperimentationError) and isinstance(error.__cause__, TypeError)
        assert error.message.startswith('Flag "f" could not be evaluated (TypeError: ')
        assert error.message.endswith("; get_string_value returned your default")

        transport.respond(500)
        quiet = ExperimentationClient(API_URL, API_KEY, transport=transport)
        assert quiet.get_number_value("f", "user-1", default=3) == 3

        def boom(error, operation):
            raise RuntimeError("handler bug")

        transport.respond(500)
        throwing = ExperimentationClient(API_URL, API_KEY, transport=transport, on_error=boom)
        assert throwing.get_json_value("f", "user-1", default=[]) == []

    def test_default_is_keyword_only_so_a_swapped_call_is_a_type_error(self, client, transport):
        for method in GETTERS:
            parameter = inspect.signature(getattr(client, method)).parameters["default"]
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
            assert parameter.default is inspect.Parameter.empty
        # The mistake keyword-only turns into a TypeError: the default passed where the user id goes.
        with pytest.raises(TypeError):
            client.get_string_value("k", "Buy now", "u1")
        with pytest.raises(TypeError):
            client.get_string_value("k", "u1", None, "Buy now")
        with pytest.raises(TypeError):
            client.get_number_value("k", "u1")
        assert transport.requests == []

    def test_get_json_value_returns_a_new_copy_on_every_call(self, client, transport):
        transport.route(
            "GET", "/api/v1/feature-flags/evaluate/theme", json=served(key="theme", value={"color": "blue", "sizes": [1, 2]}, value_type="json")
        )
        first = client.get_json_value("theme", "user-1", default={"color": "", "sizes": []})
        first["color"] = "red"
        first["sizes"].append(3)
        second = client.get_json_value("theme", "user-1", default={})
        assert second == {"color": "blue", "sizes": [1, 2]}
        assert second is not first
        assert client.get_feature_flag("theme", "user-1").value == {"color": "blue", "sizes": [1, 2]}
        assert len(transport.requests) == 1  # both later answers came from the cache


# ------------------------------------------------------------------------ track with a key


class TestTrackWithKey:
    def test_experiment_key_posts_to_track(self, client, transport):
        transport.route("POST", "/api/v1/tracking/track", json={"id": "evt-1"})
        ok = client.track(
            "user-1", "purchase", event_value=12.5, properties={"sku": "A1"}, experiment_key="checkout_flow"
        )
        assert ok is True
        assert transport.calls() == ["POST /api/v1/tracking/track"]
        assert transport.last.json() == {
            "event_type": "purchase",
            "event_name": "purchase",
            "user_id": "user-1",
            "value": 12.5,
            "metadata": {"sku": "A1"},
            "experiment_key": "checkout_flow",
        }

    def test_feature_flag_key_with_event_type_and_timestamp(self, client, transport):
        transport.route("POST", "/api/v1/tracking/track", json={})
        stamp = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
        client.track("user-1", "signup", feature_flag_key="new_search", event_type="conversion", timestamp=stamp)
        assert transport.last.json() == {
            "event_type": "conversion",
            "event_name": "signup",
            "user_id": "user-1",
            "feature_flag_key": "new_search",
            "timestamp": "2026-09-11T12:00:00+00:00",
        }

    def test_naive_datetime_is_utc_and_strings_pass_through(self, client, transport):
        transport.route("POST", "/api/v1/tracking/track", json={})
        client.track("u", "e", experiment_key="x", timestamp=datetime(2026, 1, 1))
        assert transport.last.json()["timestamp"] == "2026-01-01T00:00:00+00:00"
        client.track("u", "e", experiment_key="x", timestamp="2026-02-02T00:00:00Z")
        assert transport.last.json()["timestamp"] == "2026-02-02T00:00:00Z"

    def test_both_keys_are_sent(self, client, transport):
        transport.route("POST", "/api/v1/tracking/track", json={})
        client.track("user-1", "click", experiment_key="exp", feature_flag_key="flag")
        body = transport.last.json()
        assert (body["experiment_key"], body["feature_flag_key"]) == ("exp", "flag")
        assert "value" not in body and "metadata" not in body and "timestamp" not in body


# ------------------------------------------------------------------------ track fan-out


class TestTrackFanOut:
    def test_fans_out_to_cached_assignment_and_flag(self, client, transport):
        assign_route(transport)
        flag_route(transport)
        batch_route(transport)
        client.get_assignment("checkout_flow", "user-1")
        client.get_feature_flag("new_search", "user-1")

        assert client.track("user-1", "page_view", properties={"page": "/"}) is True

        assert transport.calls()[-1] == "POST /api/v1/tracking/batch"
        base = {"event_type": "page_view", "event_name": "page_view", "user_id": "user-1", "metadata": {"page": "/"}}
        assert transport.last.json() == {
            "events": [
                {**base, "experiment_key": "checkout_flow"},
                {**base, "feature_flag_key": "new_search"},
            ]
        }

    def test_nothing_attributed_sends_the_event_once_to_track_with_no_key(self, client, transport):
        transport.route("POST", "/api/v1/tracking/track", json={"id": "evt-1"})
        assert client.track("user-1", "page_view", event_value=2, properties={"page": "/"}) is True
        assert transport.calls() == ["POST /api/v1/tracking/track"]
        assert transport.last.json() == {
            "event_type": "page_view",
            "event_name": "page_view",
            "user_id": "user-1",
            "value": 2.0,
            "metadata": {"page": "/"},
        }

    def test_another_users_keys_never_go_out_with_this_users_event(self, client, transport):
        assign_route(transport)
        transport.route("POST", "/api/v1/tracking/track", json={})
        client.get_assignment("checkout_flow", "user-1")
        assert client.track("user-2", "page_view") is True
        assert transport.calls() == ["POST /api/v1/tracking/assign", "POST /api/v1/tracking/track"]
        assert transport.last.json() == {"event_type": "page_view", "event_name": "page_view", "user_id": "user-2"}

    def test_entries_past_the_cache_ttl_are_still_fanned_out(self, client, transport):
        flag_route(transport)
        batch_route(transport)
        with mock.patch("controlvar.cache._now") as now:
            now.return_value = 0.0
            client.get_feature_flag("new_search", "user-1")
            now.return_value = 400.0
            assert client.cached_flags("user-1") == []
            assert client.track("user-1", "page_view") is True
        assert transport.calls() == ["GET /api/v1/feature-flags/evaluate/new_search", "POST /api/v1/tracking/batch"]
        assert [e["feature_flag_key"] for e in transport.last.json()["events"]] == ["new_search"]

    def test_chunks_at_batch_limit(self, client, transport):
        for i in range(150):
            flag_route(transport, key=f"flag-{i}")
            client.get_feature_flag(f"flag-{i}", "user-1")
        batch_route(transport, success_count=100)

        assert client.track("user-1", "page_view") is True

        batches = [r for r in transport.requests if r.path == "/api/v1/tracking/batch"]
        assert [len(b.json()["events"]) for b in batches] == [BATCH_LIMIT, 50]
        assert batches[0].json()["events"][0]["feature_flag_key"] == "flag-0"
        assert batches[1].json()["events"][-1]["feature_flag_key"] == "flag-149"

    def test_a_batch_answered_2xx_returns_true_even_when_an_entry_failed(self, client, transport):
        # True means "sent and answered 2xx". A failed entry (for example a key the
        # server no longer has) is in the batch's body; track_batch reports it.
        assign_route(transport)
        client.get_assignment("checkout_flow", "user-1")
        batch_route(transport, success_count=0, failure_count=1, errors=[{"index": 0, "error": "nope"}])
        assert client.track("user-1", "page_view") is True

    @pytest.mark.parametrize("status", [401, 422, 500])
    def test_a_fan_out_answered_non_2xx_returns_false(self, client, transport, status):
        assign_route(transport)
        client.get_assignment("checkout_flow", "user-1")
        transport.route("POST", "/api/v1/tracking/batch", status=status, json={"detail": "no"})
        assert client.track("user-1", "page_view") is False

    def test_a_failed_chunk_does_not_stop_the_next_one(self, client, transport):
        for i in range(150):
            flag_route(transport, key=f"flag-{i}")
            client.get_feature_flag(f"flag-{i}", "user-1")
        transport.respond(500, json={"detail": "no"}).respond(200, json=BATCH_OK)
        assert client.track("user-1", "page_view") is False
        batches = [r for r in transport.requests if r.path == "/api/v1/tracking/batch"]
        assert [len(b.json()["events"]) for b in batches] == [BATCH_LIMIT, 50]

    @pytest.mark.parametrize("status", [401, 422, 500])
    def test_an_untagged_send_answered_non_2xx_returns_false(self, client, transport, status):
        transport.route("POST", "/api/v1/tracking/track", status=status, json={"detail": "no"})
        assert client.track("user-1", "page_view") is False
        assert transport.calls() == ["POST /api/v1/tracking/track"]


def batch_keys(request) -> list:
    """A batch's entries as ``experiment_key``, or ``flag:<feature_flag_key>``."""
    return [e.get("experiment_key") or f"flag:{e['feature_flag_key']}" for e in request.json()["events"]]


class TestLateConversions:
    """A key-less track() reaches the keys this client answered for the user in the
    last 24 hours, whether or not the answer is still in the 300 s cache."""

    @pytest.mark.regression
    def test_a_purchase_301_seconds_after_the_assignment_reaches_the_experiment(self, client, transport):
        assign_route(transport)
        batch_route(transport, success_count=1)
        with mock.patch("controlvar.cache._now") as now:
            now.return_value = 1000.0
            client.get_assignment("checkout_flow", "user-1")
            now.return_value = 1301.0  # past the 300 s cache TTL
            assert client.cached_assignments("user-1") == []
            assert client.track("user-1", "purchase", event_value=120) is True
        assert transport.calls() == ["POST /api/v1/tracking/assign", "POST /api/v1/tracking/batch"]
        assert transport.last.json() == {
            "events": [
                {
                    "event_type": "purchase",
                    "event_name": "purchase",
                    "user_id": "user-1",
                    "value": 120.0,
                    "experiment_key": "checkout_flow",
                }
            ]
        }

    @pytest.mark.regression
    def test_a_cached_answer_older_than_24_hours_still_tags_a_keyless_track_1423(self, transport):
        """#1423: with cache_ttl_seconds above 24 hours the answer outlives its 24 hour record."""
        client = ExperimentationClient(API_URL, API_KEY, cache_ttl_seconds=48 * 3600, transport=transport)
        assign_route(transport)
        batch_route(transport, success_count=1)
        with mock.patch("controlvar.cache._now") as now:
            now.return_value = 1000.0
            client.get_assignment("checkout_flow", "user-1")
            now.return_value = 1000.0 + 25 * 3600  # past 24 hours, inside the 48 hour cache
            assert len(client.cached_assignments("user-1")) == 1
            assert client.track("user-1", "purchase", event_value=120) is True
        assert transport.calls() == ["POST /api/v1/tracking/assign", "POST /api/v1/tracking/batch"]
        assert batch_keys(transport.last) == ["checkout_flow"]

    def test_control_the_same_purchase_at_299_seconds_while_still_cached(self, client, transport):
        assign_route(transport)
        batch_route(transport, success_count=1)
        with mock.patch("controlvar.cache._now") as now:
            now.return_value = 1000.0
            client.get_assignment("checkout_flow", "user-1")
            now.return_value = 1299.0
            assert [a.experiment_key for a in client.cached_assignments("user-1")] == ["checkout_flow"]
            assert client.track("user-1", "purchase", event_value=120) is True
        assert transport.calls() == ["POST /api/v1/tracking/assign", "POST /api/v1/tracking/batch"]
        assert batch_keys(transport.last) == ["checkout_flow"]

    def test_n_experiments_plus_m_flags_each_once_experiments_first(self, client, transport):
        for key in ("hero", "checkout_flow"):
            transport.respond(200, json={**ASSIGN_RESPONSE, "experiment_key": key})
        transport.respond(200, json={**FLAG_RESPONSE, "key": "new_search"})
        transport.respond(200, json={**ASSIGN_RESPONSE, "experiment_key": "sort"})
        transport.respond(200, json={**FLAG_RESPONSE, "key": "free_shipping"})
        transport.respond(200, json={**ASSIGN_RESPONSE, "experiment_key": "checkout_flow"})
        batch_route(transport, success_count=5)
        with mock.patch("controlvar.cache._now") as now:
            now.return_value = 0.0
            client.get_assignment("hero", "user-1")
            client.get_assignment("checkout_flow", "user-1")
            client.get_feature_flag("new_search", "user-1")
            client.get_assignment("sort", "user-1")
            client.get_feature_flag("free_shipping", "user-1")
            now.return_value = 400.0
            client.get_assignment("checkout_flow", "user-1")  # answered again: one entry, first place
            now.return_value = 800.0
            assert client.track("user-1", "purchase") is True
        assert transport.calls()[-1] == "POST /api/v1/tracking/batch"
        assert len(transport.last.json()["events"]) == 5
        assert batch_keys(transport.last) == ["hero", "checkout_flow", "sort", "flag:new_search", "flag:free_shipping"]

    @pytest.mark.parametrize("reason", ["holdout", "mutual_exclusion", "targeting"])
    def test_an_assigned_false_answer_is_kept_past_the_ttl(self, client, transport, reason):
        assign_route(transport, variant_name="control", is_control=True, assigned=False, reason=reason)
        batch_route(transport, success_count=1)
        with mock.patch("controlvar.cache._now") as now:
            now.return_value = 0.0
            client.get_assignment("checkout_flow", "user-1")
            now.return_value = 301.0
            assert client.track("user-1", "purchase") is True
        assert transport.calls()[-1] == "POST /api/v1/tracking/batch"
        assert batch_keys(transport.last) == ["checkout_flow"]

    @pytest.mark.parametrize("reason", ["draft", "paused", "completed", "archived"])
    def test_an_experiment_whose_latest_answer_has_a_lifecycle_reason_is_skipped(self, client, transport, reason):
        transport.respond(
            200,
            json={**ASSIGN_RESPONSE, "variant_name": "control", "is_control": True, "assigned": False, "reason": reason},
        )
        transport.respond(200, json={**ASSIGN_RESPONSE, "experiment_key": "hero"})
        batch_route(transport, success_count=1)
        client.get_assignment("checkout_flow", "user-1")
        client.get_assignment("hero", "user-1")
        assert client.track("user-1", "purchase") is True
        assert transport.calls()[-1] == "POST /api/v1/tracking/batch"
        assert batch_keys(transport.last) == ["hero"]

    def test_the_latest_answer_decides_paused_stops_and_resumed_restores(self, client, transport):
        transport.route("POST", "/api/v1/tracking/track", json={})
        batch_route(transport, success_count=1)
        running = {**ASSIGN_RESPONSE, "assigned": True, "reason": "assigned"}
        paused = {**ASSIGN_RESPONSE, "variant_name": "control", "is_control": True, "assigned": False, "reason": "paused"}
        with mock.patch("controlvar.cache._now") as now:
            now.return_value = 0.0
            transport.respond(200, json=running)
            client.get_assignment("checkout_flow", "user-1")
            now.return_value = 301.0
            transport.respond(200, json=paused)
            client.get_assignment("checkout_flow", "user-1")
            assert client.track("user-1", "purchase") is True
            assert transport.calls()[-1] == "POST /api/v1/tracking/track"
            assert "experiment_key" not in transport.last.json()

            now.return_value = 602.0
            transport.respond(200, json=running)
            client.get_assignment("checkout_flow", "user-1")
            assert client.track("user-1", "purchase") is True
        assert transport.calls()[-1] == "POST /api/v1/tracking/batch"
        assert batch_keys(transport.last) == ["checkout_flow"]

    @pytest.mark.regression
    def test_an_experiment_whose_assignment_is_later_answered_404_is_dropped(self, client, transport):
        transport.route("POST", "/api/v1/tracking/track", json={})
        with mock.patch("controlvar.cache._now") as now:
            now.return_value = 0.0
            transport.respond(200, json=ASSIGN_RESPONSE)
            client.get_assignment("checkout_flow", "user-1")
            now.return_value = 301.0  # the cache has expired; the server now answers 404
            transport.respond(404, json={"detail": "Active experiment with key 'checkout_flow' not found"})
            assert client.get_variant("checkout_flow", "user-1") == "control"
            now.return_value = 3600.0
            assert client.track("user-1", "purchase") is True
        assert transport.calls() == [
            "POST /api/v1/tracking/assign",
            "POST /api/v1/tracking/assign",
            "POST /api/v1/tracking/track",
        ]
        assert transport.last.json() == {"event_type": "purchase", "event_name": "purchase", "user_id": "user-1"}

    @pytest.mark.parametrize("failure", [500, TransportError("reset")], ids=["500", "network"])
    def test_the_key_is_kept_when_the_later_assignment_fails_otherwise(self, client, transport, failure):
        batch_route(transport, success_count=1)
        with mock.patch("controlvar.cache._now") as now:
            now.return_value = 0.0
            transport.respond(200, json=ASSIGN_RESPONSE)
            client.get_assignment("checkout_flow", "user-1")
            now.return_value = 301.0
            if isinstance(failure, int):
                transport.respond(failure, json={"detail": "no"})
            else:
                transport.fail(failure)
            assert client.get_variant("checkout_flow", "user-1") == "control"
            assert client.track("user-1", "purchase") is True
        assert transport.calls()[-1] == "POST /api/v1/tracking/batch"
        assert batch_keys(transport.last) == ["checkout_flow"]

    def test_a_key_answered_24_hours_less_1_second_ago_is_kept(self, client, transport):
        assign_route(transport)
        batch_route(transport, success_count=1)
        with mock.patch("controlvar.cache._now") as now:
            now.return_value = 0.0
            client.get_assignment("checkout_flow", "user-1")
            now.return_value = 24 * 3600 - 1.0
            assert client.track("user-1", "purchase") is True
        assert transport.calls()[-1] == "POST /api/v1/tracking/batch"
        assert batch_keys(transport.last) == ["checkout_flow"]

    def test_a_key_answered_more_than_24_hours_ago_is_dropped_and_the_event_goes_with_no_key(
        self, client, transport
    ):
        assign_route(transport)
        transport.route("POST", "/api/v1/tracking/track", json={})
        with mock.patch("controlvar.cache._now") as now:
            now.return_value = 0.0
            client.get_assignment("checkout_flow", "user-1")
            now.return_value = 24 * 3600 + 1.0
            assert client.track("user-1", "purchase") is True
        assert transport.calls() == ["POST /api/v1/tracking/assign", "POST /api/v1/tracking/track"]
        assert transport.last.json() == {"event_type": "purchase", "event_name": "purchase", "user_id": "user-1"}

    def test_clear_cache_keeps_the_attributed_keys(self, client, transport):
        assign_route(transport)
        flag_route(transport)
        batch_route(transport, success_count=2)
        client.get_assignment("checkout_flow", "user-1")
        client.get_feature_flag("new_search", "user-1")
        client.clear_cache()
        assert client.cached_assignments("user-1") == [] and client.cached_flags("user-1") == []
        assert client.track("user-1", "purchase") is True
        assert transport.calls()[-1] == "POST /api/v1/tracking/batch"
        assert batch_keys(transport.last) == ["checkout_flow", "flag:new_search"]

    def test_another_client_instance_has_none_of_them(self, client, transport):
        # Say, an order service: its event reaches no experiment unless it names one.
        assign_route(transport)
        transport.route("POST", "/api/v1/tracking/track", json={})
        client.get_assignment("checkout_flow", "user-1")
        order_service = ExperimentationClient(API_URL, API_KEY, transport=transport)
        assert order_service.track("user-1", "purchase") is True
        assert transport.calls() == ["POST /api/v1/tracking/assign", "POST /api/v1/tracking/track"]
        assert transport.last.json() == {"event_type": "purchase", "event_name": "purchase", "user_id": "user-1"}


class TestTrackNeverRaises:
    def test_transport_exception(self, client, transport):
        transport.fail(RuntimeError("boom"))
        assert client.track("user-1", "purchase", experiment_key="x") is False

    @pytest.mark.parametrize("status", [401, 404, 422, 500])
    def test_http_error(self, client, transport, status):
        transport.respond(status, json={"detail": "error"})
        assert client.track("user-1", "purchase", experiment_key="x") is False

    def test_unserialisable_properties(self, client, transport):
        transport.route("POST", "/api/v1/tracking/track", json={})
        assert client.track("user-1", "purchase", properties={"obj": object()}, experiment_key="x") is False

    def test_fan_out_transport_exception(self, client, transport):
        assign_route(transport)
        client.get_assignment("checkout_flow", "user-1")
        transport.fail(TransportError("down"))
        assert client.track("user-1", "page_view") is False

    def test_a_timeout_returns_false_although_the_event_was_sent(self, client, transport):
        """The whole request went out, and then no answer came in time: the
        default transport raises ``TransportError`` on a timeout
        (``test_transport.py``). The server may have stored the event by then;
        ``track`` cannot tell, and returns False."""
        transport.fail(TransportError("POST /api/v1/tracking/track failed: timed out"))
        assert client.track("user-1", "purchase", event_value=49.99, experiment_key="checkout_flow") is False
        assert transport.calls() == ["POST /api/v1/tracking/track"]
        sent = transport.last.json()
        assert (sent["user_id"], sent["event_name"], sent["value"]) == ("user-1", "purchase", 49.99)


# ---------------------------------------------------------------------------- track_batch


class TestTrackBatch:
    def test_chunks_aggregates_and_offsets_error_indexes(self, client, transport):
        events = [{"event_name": "click", "user_id": "u", "experiment_key": "x"} for _ in range(250)]
        transport.respond(200, json={"success_count": 100, "failure_count": 0, "errors": None})
        transport.respond(200, json={"success_count": 99, "failure_count": 1, "errors": [{"index": 1, "error": "bad"}]})
        transport.respond(200, json={"success_count": 50, "failure_count": 0})

        result = client.track_batch(events)

        assert result == BatchResult(success_count=249, failure_count=1, errors=[{"index": 101, "error": "bad"}])
        assert result.ok is False
        assert [len(r.json()["events"]) for r in transport.requests] == [100, 100, 50]
        assert transport.requests[0].json()["events"][0] == {
            "event_type": "click",
            "event_name": "click",
            "user_id": "u",
            "experiment_key": "x",
        }

    def test_failed_chunk_counts_all_events_without_raising(self, client, transport):
        transport.respond(500, body=b"boom")
        result = client.track_batch([{"event_type": "e", "user_id": "u", "experiment_key": "x"}] * 3)
        assert (result.success_count, result.failure_count, result.ok) == (0, 3, False)
        assert result.errors[0]["index"] == 0
        assert result.errors[0]["count"] == 3
        assert result.errors[0]["status"] == 500

    def test_an_entry_with_no_key_is_sent_as_given_never_fanned_out(self, client, transport):
        assign_route(transport)
        batch_route(transport, success_count=1)
        client.get_assignment("checkout_flow", "user-1")
        client.track_batch([{"event_type": "purchase", "user_id": "user-1"}])
        assert transport.last.json() == {"events": [{"event_type": "purchase", "user_id": "user-1"}]}

    def test_empty_batch_sends_nothing(self, client, transport):
        assert client.track_batch([]) == BatchResult()
        assert transport.requests == []

    def test_datetime_timestamp_is_serialised(self, client, transport):
        batch_route(transport)
        client.track_batch([{"event_type": "e", "user_id": "u", "experiment_key": "x", "timestamp": datetime(2026, 3, 1)}])
        assert transport.last.json()["events"][0]["timestamp"] == "2026-03-01T00:00:00+00:00"


# ------------------------------------------------------------------------ report_error

ERRORS_PATH = "/api/v1/tracking/errors"
STORED = {"id": "err-1", "error_type": "crash", "timestamp": "2026-10-26T10:00:00"}


class TestReportError:
    @pytest.fixture(autouse=True)
    def no_sleep(self, client):
        client._sleep = mock.Mock()
        return client._sleep

    def test_posts_one_report_with_the_server_field_names_and_returns_true_on_201(self, client, transport):
        transport.route("POST", ERRORS_PATH, status=201, json=STORED)
        stored = client.report_error(
            "crash",
            "TypeError in checkout",
            feature_flag_key="new-checkout",
            experiment_key="checkout",
            user_id="user-1",
            stack_trace="at pay (checkout.py:12)",
            metadata={"os": "linux", "release": "3.2.1"},
            timestamp=datetime(2026, 10, 26, 9, 59, tzinfo=timezone.utc),
        )
        assert stored is True
        assert transport.calls() == [f"POST {ERRORS_PATH}"]
        assert transport.last.headers["X-API-Key"] == API_KEY
        assert transport.last.json() == {
            "error_type": "crash",
            "message": "TypeError in checkout",
            "feature_flag_key": "new-checkout",
            "experiment_key": "checkout",
            "user_id": "user-1",
            "stack_trace": "at pay (checkout.py:12)",
            "metadata": {"os": "linux", "release": "3.2.1"},
            "timestamp": "2026-10-26T09:59:00+00:00",
        }

    def test_sends_only_the_fields_given_a_naive_datetime_as_utc_and_a_string_as_it_is(self, client, transport):
        transport.route("POST", ERRORS_PATH, status=201, json=STORED)
        client.report_error("crash", "boom", feature_flag_key="f", timestamp=datetime(2026, 1, 2, 3, 4, 5))
        assert transport.last.json() == {
            "error_type": "crash",
            "message": "boom",
            "feature_flag_key": "f",
            "timestamp": "2026-01-02T03:04:05+00:00",
        }
        client.report_error("crash", "boom", experiment_key="x", timestamp="2026-01-02T03:04:05Z")
        assert transport.last.json()["timestamp"] == "2026-01-02T03:04:05Z"

    def test_the_target_and_the_rest_are_keyword_only(self, client):
        with pytest.raises(TypeError):
            client.report_error("crash", "boom", "a-flag")  # type: ignore[misc]

    def test_does_not_check_the_report_itself_and_a_422_returns_false(self, client, transport, caplog):
        transport.route("POST", ERRORS_PATH, status=422, json={"detail": "At least one of feature_flag_key or experiment_key is required"})
        with caplog.at_level("WARNING", logger="controlvar.client"):
            assert client.report_error("crash", "boom") is False
        assert transport.last.json() == {"error_type": "crash", "message": "boom"}
        assert "HTTP 422" in caplog.text

    @pytest.mark.parametrize("status", [401, 404, 422, 500, 503])
    def test_returns_false_without_raising_on(self, client, transport, status):
        transport.route("POST", ERRORS_PATH, status=status, json={"detail": "no"})
        assert client.report_error("crash", "boom", feature_flag_key="f") is False

    def test_returns_false_on_a_2xx_that_is_not_201(self, client, transport):
        transport.route("POST", ERRORS_PATH, status=200, json=STORED)
        assert client.report_error("crash", "boom", feature_flag_key="f") is False

    def test_returns_false_on_a_429_that_is_still_a_429_after_its_one_retry(self, client, transport, no_sleep):
        transport.respond(429, headers={"Retry-After": "1"}).respond(429, headers={"Retry-After": "1"})
        assert client.report_error("crash", "boom", feature_flag_key="f") is False
        assert len(transport.requests) == 2
        no_sleep.assert_called_once_with(1.0)

    def test_returns_true_when_the_retry_after_a_429_is_answered_201(self, client, transport):
        transport.respond(429, headers={"Retry-After": "1"}).respond(201, json=STORED)
        assert client.report_error("crash", "boom", feature_flag_key="f") is True
        assert len(transport.requests) == 2

    @pytest.mark.parametrize("exc", [TransportError("down"), RuntimeError("boom"), TimeoutError("slow")])
    def test_returns_false_on_a_network_failure(self, client, transport, exc):
        transport.fail(exc)
        assert client.report_error("crash", "boom", feature_flag_key="f") is False

    def test_returns_false_when_the_metadata_cannot_be_sent(self, client, transport):
        assert client.report_error("crash", "boom", feature_flag_key="f", metadata={"obj": object()}) is False
        assert transport.requests == []

    def test_works_in_local_mode_before_and_after_close_with_the_same_key(self, transport, monkeypatch):
        # Local mode is server-side code: errors are reported with the same sdk:ruleset key.
        from controlvar.local import LocalEvaluation

        monkeypatch.setattr(LocalEvaluation, "_start_thread", lambda self: None)
        local = ExperimentationClient(API_URL, "sk-scoped", transport=transport, evaluation="local")
        transport.route("POST", ERRORS_PATH, status=201, json=STORED)
        assert local.report_error("crash", "boom", feature_flag_key="f") is True
        local.close()
        assert local.report_error("crash", "boom", feature_flag_key="f") is True
        sent = [r for r in transport.requests if r.path == ERRORS_PATH]
        assert len(sent) == 2
        assert all(r.headers["X-API-Key"] == "sk-scoped" for r in sent)


# ------------------------------------------------------------------------------- 429


class TestRateLimit:
    @pytest.fixture(autouse=True)
    def no_sleep(self, client):
        client._sleep = mock.Mock()
        return client._sleep

    def test_retries_once_after_retry_after(self, client, transport, no_sleep):
        transport.respond(429, headers={"Retry-After": "2"}).respond(200, json=FLAG_RESPONSE)
        assert client.get_feature_flag("new_search", "user-1").enabled is True
        no_sleep.assert_called_once_with(2.0)
        assert len(transport.requests) == 2

    @pytest.mark.parametrize(
        ("header", "expected"),
        [({"Retry-After": "60"}, 5.0), ({}, 1.0), ({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}, 1.0)],
        ids=["capped", "missing", "unparseable"],
    )
    def test_retry_after_is_capped_or_defaulted(self, client, transport, no_sleep, header, expected):
        transport.respond(429, headers=header).respond(200, json=FLAG_RESPONSE)
        client.get_feature_flag("new_search", "user-1")
        no_sleep.assert_called_once_with(expected)

    def test_second_429_raises(self, client, transport, no_sleep):
        transport.respond(429, headers={"Retry-After": "1"}).respond(429, headers={"Retry-After": "1"})
        with pytest.raises(ExperimentationError) as info:
            client.get_assignment("checkout_flow", "user-1")
        assert info.value.status == 429
        assert no_sleep.call_count == 1
        assert len(transport.requests) == 2

    @pytest.mark.parametrize("status", [400, 401, 404, 500, 503])
    def test_any_other_error_status_raises_at_once(self, client, transport, no_sleep, status):
        transport.respond(status, json={"detail": "no"}).respond(200, json=FLAG_RESPONSE)
        with pytest.raises(ExperimentationError) as info:
            client.get_feature_flag("new_search", "user-1")
        assert info.value.status == status
        assert len(transport.requests) == 1
        no_sleep.assert_not_called()

    def test_track_returns_false_after_retry(self, client, transport, no_sleep):
        transport.respond(429).respond(429)
        assert client.track("user-1", "purchase", experiment_key="x") is False
        assert len(transport.requests) == 2


# ------------------------------------------------------------------------------ misc


class TestAssignmentsAndCache:
    def test_get_assignments_url(self, client, transport):
        transport.route("GET", "/api/v1/tracking/assignments/user%201", json=[{"experiment_key": "x"}])
        assert client.get_assignments("user 1") == [{"experiment_key": "x"}]
        assert transport.last.query == {"active_only": "true"}
        client.get_assignments("user 1", active_only=False)
        assert transport.last.query == {"active_only": "false"}

    def test_clear_cache_forces_refetch(self, client, transport):
        assign_route(transport)
        flag_route(transport)
        client.get_assignment("checkout_flow", "user-1")
        client.get_feature_flag("new_search", "user-1")
        client.clear_cache()
        assert client.cached_assignments("user-1") == [] and client.cached_flags("user-1") == []
        client.get_assignment("checkout_flow", "user-1")
        client.get_feature_flag("new_search", "user-1")
        assert len(transport.requests) == 4

    def test_cached_accessors_list_live_entries(self, client, transport):
        assign_route(transport)
        flag_route(transport)
        assignment = client.get_assignment("checkout_flow", "user-1")
        evaluation = client.get_feature_flag("new_search", "user-1")
        assert client.cached_assignments("user-1") == [assignment]
        assert client.cached_flags("user-1") == [evaluation]
        assert client.cached_flags("someone-else") == []

    def test_concurrent_evaluations_are_thread_safe(self, client, transport):
        for i in range(20):
            flag_route(transport, key=f"flag-{i}")
        errors = []

        def worker(index: int) -> None:
            try:
                for _ in range(5):
                    for i in range(20):
                        assert client.get_feature_flag(f"flag-{i}", f"user-{index % 4}").enabled is True
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        seen = len(transport.requests)
        for i in range(20):
            for user in range(4):
                client.get_feature_flag(f"flag-{i}", f"user-{user}")
        assert len(transport.requests) == seen  # everything is served from cache now
