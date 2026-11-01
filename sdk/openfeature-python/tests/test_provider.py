"""
Tests for ExperimentationProvider (OpenFeature Python provider).

The provider delegates to the ``controlvar`` SDK; its HTTP transport is
replaced with ``controlvar.testing.FakeTransport`` so every test can
assert the exact request that would reach the backend.

Coverage:
  - Construction, metadata, hooks, client injection, base_url/timeout/ttl forwarding
  - Request shape: GET /api/v1/feature-flags/evaluate/{key}?user_id=<targeting_key>&context=<attributes>
  - The context: attributes sent as ``context`` (targeting_key not among them), datetimes as
    ISO 8601, the cache keyed on user + flag only
  - resolve_boolean_details: ``enabled`` for every flag
  - resolve_string/integer/float/object_details: one test per row of the resolution table
    (on/off value, reason, variant, CACHED, TYPE_MISMATCH, GENERAL, PARSE_ERROR), and the
    integer/float rules
  - A 0.2.0-shaped SDK (no ``value``/``value_type``): never raises
  - targeting_key missing, 404 → FLAG_NOT_FOUND, other errors → GENERAL (never raise)
  - Cache: hit, per-user, TTL expiry, failures not cached, shutdown clears
  - Tracking via the OpenFeature tracking API
  - Thread safety
  - Hash utility (cross-SDK golden vector)
  - Registration with openfeature.api
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest
from openfeature import api
from openfeature.evaluation_context import EvaluationContext
from openfeature.exception import ErrorCode
from openfeature.flag_evaluation import Reason
from openfeature.provider import TrackingEventDetails
from openfeature.provider.no_op_provider import NoOpProvider

from controlvar import ExperimentationClient, TransportError
from controlvar.testing import FakeTransport
from controlvar_openfeature import PROVIDER_NAME, ExperimentationProvider, ProviderConfig
from controlvar_openfeature.evaluator import hash_user

EVALUATE = "/api/v1/feature-flags/evaluate"
CTX = EvaluationContext(targeting_key="user-1")
GOLDEN_VECTORS = Path(__file__).resolve().parents[3] / "tests" / "sdk-contract" / "golden-vectors.json"


def _golden_hash_vectors() -> list:
    if not GOLDEN_VECTORS.exists():  # running from an sdist without the monorepo
        return []
    return json.loads(GOLDEN_VECTORS.read_text())["hash_vectors"]


def make_provider(**kwargs):
    transport = FakeTransport()
    provider = ExperimentationProvider(api_key="test-key", transport=transport, **kwargs)
    return provider, transport


def flag(transport: FakeTransport, key: str, enabled: bool = True, config=None, **fields) -> FakeTransport:
    """A boolean flag's answer as a server from before flag values sends it, plus ``fields``."""
    return transport.route(
        "GET", f"{EVALUATE}/{key}", json={"key": key, "enabled": enabled, "config": config, **fields}
    )


def value_flag(
    transport: FakeTransport, key: str, value_type: str, value: Any, *, enabled: bool = True, reason: str = "rollout"
) -> FakeTransport:
    """A flag's answer as the server sends it: ``value`` and ``value_type`` beside ``enabled``."""
    return flag(transport, key, enabled=enabled, reason=reason, value=value, value_type=value_type)


def sent_context(transport: FakeTransport) -> Optional[dict]:
    """The ``context`` the last request carried, decoded; ``None`` when it carried none."""
    raw = transport.last.query.get("context")
    return None if raw is None else json.loads(raw)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_requires_api_key(self):
        with pytest.raises(ValueError, match="api_key is required"):
            ExperimentationProvider(api_key="")

    def test_metadata_name(self):
        provider, _ = make_provider()
        assert provider.get_metadata().name == PROVIDER_NAME == "controlvar-provider"

    def test_get_provider_hooks_returns_empty_list(self):
        provider, _ = make_provider()
        assert provider.get_provider_hooks() == []

    def test_client_property_exposes_sdk_client(self):
        provider, _ = make_provider()
        assert isinstance(provider.client, ExperimentationClient)

    def test_accepts_prebuilt_client(self):
        transport = FakeTransport()
        client = ExperimentationClient("http://shared", "k", transport=transport)
        provider = ExperimentationProvider(client=client)
        assert provider.client is client
        flag(transport, "f")
        provider.resolve_boolean_details("f", False, CTX)
        assert transport.last.url.startswith("http://shared/api/v1/")

    def test_default_base_url(self):
        provider, transport = make_provider()
        flag(transport, "f")
        provider.resolve_boolean_details("f", False, CTX)
        assert transport.last.url == "http://localhost:8000/api/v1/feature-flags/evaluate/f?user_id=user-1"

    def test_custom_base_url_with_trailing_slash(self):
        provider, transport = make_provider(base_url="https://api.example.com/")
        flag(transport, "f")
        provider.resolve_boolean_details("f", False, CTX)
        assert transport.last.url == "https://api.example.com/api/v1/feature-flags/evaluate/f?user_id=user-1"

    def test_timeout_and_cache_ttl_are_forwarded(self):
        provider, transport = make_provider(timeout=3, cache_ttl=42)
        flag(transport, "f")
        provider.resolve_boolean_details("f", False, CTX)
        assert transport.last.timeout == 3.0
        assert provider.client.cache_ttl_seconds == 42

    def test_provider_config_defaults(self):
        config = ProviderConfig(api_key="k")
        assert (config.base_url, config.cache_ttl, config.timeout) == ("http://localhost:8000", 300, 10)
        provider = ExperimentationProvider(**vars(config))
        assert provider.get_metadata().name == PROVIDER_NAME


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


class TestRequestShape:
    def test_evaluate_request_url_headers_and_context(self):
        provider, transport = make_provider()
        flag(transport, "dark-mode")
        ctx = EvaluationContext(targeting_key="user-1", attributes={"plan": "pro", "country": "US"})
        provider.resolve_boolean_details("dark-mode", False, ctx)

        request = transport.last
        assert request.method == "GET"
        assert request.path == "/api/v1/feature-flags/evaluate/dark-mode"
        assert request.query == {"user_id": "user-1", "context": '{"plan":"pro","country":"US"}'}
        assert request.body is None
        assert request.headers["X-API-Key"] == "test-key"
        assert request.headers["Accept"] == "application/json"

    def test_no_attributes_sends_no_context(self):
        provider, transport = make_provider()
        flag(transport, "f")
        provider.resolve_boolean_details("f", False, EvaluationContext("user-1", {}))
        assert transport.last.query == {"user_id": "user-1"}

    def test_flag_key_is_url_encoded(self):
        provider, transport = make_provider()
        transport.route("GET", f"{EVALUATE}/my%20flag", json={"key": "my flag", "enabled": True, "config": None})
        result = provider.resolve_boolean_details("my flag", False, EvaluationContext("u 1"))
        assert result.value is True
        assert transport.last.url.endswith("/evaluate/my%20flag?user_id=u%201")

    def test_initialize_makes_no_requests(self):
        provider, transport = make_provider()
        provider.initialize(CTX)
        provider.initialize()
        assert transport.requests == []


# ---------------------------------------------------------------------------
# The context: attributes reach the server; the cache is keyed on user + flag
# ---------------------------------------------------------------------------


class TestContext:
    @pytest.mark.parametrize(
        "resolve, default",
        [
            ("resolve_boolean_details", False),
            ("resolve_string_details", "d"),
            ("resolve_integer_details", 0),
            ("resolve_float_details", 0.0),
            ("resolve_object_details", {}),
        ],
    )
    def test_every_call_sends_the_attributes_and_not_the_targeting_key(self, resolve, default):
        provider, transport = make_provider()
        flag(transport, "f")
        attributes = {"plan": "enterprise", "seats": 12, "beta": True, "tags": ["a", "b"], "org": {"tier": "gold"}}
        getattr(provider, resolve)("f", default, EvaluationContext("user-1", attributes))
        assert transport.last.query["user_id"] == "user-1"
        assert sent_context(transport) == attributes
        assert "user-1" not in transport.last.query["context"]

    def test_a_datetime_at_any_depth_is_sent_as_iso_8601(self):
        provider, transport = make_provider()
        flag(transport, "f")
        aware = datetime(2026, 11, 1, 8, 15, 30, tzinfo=timezone.utc)
        offset = datetime(2026, 11, 1, 9, 0, tzinfo=timezone(timedelta(hours=2)))
        naive = datetime(2026, 1, 2, 3, 4, 5)
        attributes = {"signed_up": aware, "nested": {"at": offset}, "history": [naive]}
        provider.resolve_string_details("f", "d", EvaluationContext("user-1", attributes))
        assert sent_context(transport) == {
            "signed_up": "2026-11-01T08:15:30+00:00",
            "nested": {"at": "2026-11-01T09:00:00+02:00"},
            "history": ["2026-01-02T03:04:05+00:00"],  # a naive datetime is taken as UTC
        }

    def test_the_cache_key_is_user_and_flag_not_the_attributes(self):
        """A second call with other attributes within cache_ttl gets the first answer, CACHED."""
        provider, transport = make_provider()
        value_flag(transport, "label", "string", "Complete purchase", reason="targeting_rule")
        first = provider.resolve_string_details("label", "Buy now", EvaluationContext("u1", {"plan": "enterprise"}))
        value_flag(transport, "label", "string", "Buy now", enabled=False, reason="targeting_rule")
        second = provider.resolve_string_details("label", "Buy now", EvaluationContext("u1", {"plan": "free"}))
        assert (first.value, first.reason) == ("Complete purchase", Reason.TARGETING_MATCH)
        assert (second.value, second.reason, second.variant) == ("Complete purchase", Reason.CACHED, "on")
        assert len(transport.requests) == 1

    def test_zero_cache_ttl_sends_each_calls_attributes(self):
        provider, transport = make_provider(cache_ttl=0)
        value_flag(transport, "label", "string", "Complete purchase", reason="targeting_rule")
        provider.resolve_string_details("label", "Buy now", EvaluationContext("u1", {"plan": "enterprise"}))
        value_flag(transport, "label", "string", "Buy now", enabled=False, reason="targeting_rule")
        second = provider.resolve_string_details("label", "Buy now", EvaluationContext("u1", {"plan": "free"}))
        assert (second.value, second.reason, second.variant) == ("Buy now", Reason.TARGETING_MATCH, "off")
        assert [json.loads(r.query["context"]) for r in transport.requests] == [
            {"plan": "enterprise"},
            {"plan": "free"},
        ]

    def test_an_attribute_the_sdk_cannot_send_answers_the_default_and_never_raises(self):
        provider, transport = make_provider()
        flag(transport, "f")
        ctx = EvaluationContext("user-1", {"odd": object()})
        result = provider.resolve_string_details("f", "my-default", ctx)
        assert (result.value, result.reason, result.error_code) == ("my-default", Reason.ERROR, ErrorCode.GENERAL)
        assert provider.resolve_boolean_details("f", True, ctx).error_code == ErrorCode.GENERAL
        assert transport.requests == []


# ---------------------------------------------------------------------------
# Boolean resolution
# ---------------------------------------------------------------------------


class TestResolveBooleanDetails:
    def test_enabled_flag_returns_true(self):
        provider, transport = make_provider()
        flag(transport, "feature-a", enabled=True)
        result = provider.resolve_boolean_details("feature-a", False, CTX)
        assert result.value is True
        assert result.reason == Reason.TARGETING_MATCH
        assert result.error_code is None

    def test_disabled_flag_returns_false_even_with_true_default(self):
        provider, transport = make_provider()
        flag(transport, "feature-b", enabled=False)
        result = provider.resolve_boolean_details("feature-b", True, CTX)
        assert result.value is False
        assert result.reason == Reason.DISABLED

    def test_variant_label_comes_from_config(self):
        provider, transport = make_provider()
        flag(transport, "f", config={"variant": "beta"})
        assert provider.resolve_boolean_details("f", False, CTX).variant == "beta"

    def test_no_variant_when_config_is_null(self):
        provider, transport = make_provider()
        flag(transport, "f", config=None)
        result = provider.resolve_boolean_details("f", False, CTX)
        assert (result.value, result.variant) == (True, None)

    @pytest.mark.parametrize("enabled, reason", [(True, Reason.TARGETING_MATCH), (False, Reason.DISABLED)])
    def test_a_value_flag_answers_enabled(self, enabled, reason):
        """Boolean resolution is ``enabled`` for every flag, a string, number or json flag included."""
        provider, transport = make_provider()
        value_flag(transport, "label", "string", "Complete purchase" if enabled else "Buy now", enabled=enabled)
        result = provider.resolve_boolean_details("label", not enabled, CTX)
        assert (result.value, result.reason, result.error_code) == (enabled, reason, None)

    def test_not_found_returns_default_with_flag_not_found(self):
        provider, transport = make_provider()
        transport.respond(404, json={"detail": "Feature flag with key 'missing' not found"})
        result = provider.resolve_boolean_details("missing", True, CTX)
        assert result.value is True
        assert result.error_code == ErrorCode.FLAG_NOT_FOUND
        assert result.reason == Reason.ERROR
        assert "404" in result.error_message

    @pytest.mark.parametrize("status", [401, 422, 500])
    def test_http_error_returns_default_with_general(self, status):
        provider, transport = make_provider()
        transport.respond(status, json={"detail": "nope"})
        result = provider.resolve_boolean_details("f", True, CTX)
        assert result.value is True
        assert result.error_code == ErrorCode.GENERAL
        assert result.reason == Reason.ERROR

    def test_network_error_returns_default_with_general(self):
        provider, transport = make_provider()
        transport.fail(TransportError("connection refused"))
        result = provider.resolve_boolean_details("f", False, CTX)
        assert result.value is False
        assert result.error_code == ErrorCode.GENERAL

    @pytest.mark.parametrize("ctx", [None, EvaluationContext(), EvaluationContext(targeting_key="")])
    def test_missing_targeting_key_returns_default_without_request(self, ctx):
        provider, transport = make_provider()
        result = provider.resolve_boolean_details("f", True, ctx)
        assert result.value is True
        assert result.error_code == ErrorCode.TARGETING_KEY_MISSING
        assert result.reason == Reason.ERROR
        assert transport.requests == []

    def test_second_evaluation_is_served_from_cache(self):
        provider, transport = make_provider()
        flag(transport, "f")
        first = provider.resolve_boolean_details("f", False, CTX)
        second = provider.resolve_boolean_details("f", False, CTX)
        assert (first.reason, second.reason) == (Reason.TARGETING_MATCH, Reason.CACHED)
        assert second.value is True
        assert len(transport.requests) == 1


# ---------------------------------------------------------------------------
# String, integer, float and object resolution: the resolution table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    """One typed call, a flag of the type it reads, and the values that flag serves."""

    method: str
    value_type: str
    on: Any
    off: Any
    default: Any
    other_type: str  # a flag type this call does not read
    other_value: Any
    name: str  # how the TYPE_MISMATCH message names the call


CALLS = [
    Call("resolve_string_details", "string", "Complete purchase", "Buy now", "fallback", "number", 3, "a string"),
    Call("resolve_integer_details", "number", 10, 3, -1, "string", "ten", "an integer"),
    Call("resolve_float_details", "number", 2.5, 1.25, -1.0, "json", {"a": 1}, "a float"),
    Call(
        "resolve_object_details",
        "json",
        {"theme": "dark", "max": 3},
        ["a", "b"],
        {"d": True},
        "string",
        "x",
        "an object",
    ),
]
CALL_IDS = [c.method.split("_")[1] for c in CALLS]


def resolve(provider: ExperimentationProvider, call: Call, key: str = "f", ctx: EvaluationContext = CTX):
    return getattr(provider, call.method)(key, call.default, ctx)


@pytest.mark.parametrize("call", CALLS, ids=CALL_IDS)
class TestResolutionTable:
    @pytest.mark.parametrize(
        "server_reason, enabled, reason, variant",
        [
            ("targeting_rule", True, Reason.TARGETING_MATCH, "on"),
            ("targeting_rule", False, Reason.TARGETING_MATCH, "off"),
            ("rollout", True, Reason.SPLIT, "on"),
            ("rollout", False, Reason.SPLIT, "off"),
        ],
    )
    def test_the_on_or_off_value_with_the_servers_reason(self, call, server_reason, enabled, reason, variant):
        provider, transport = make_provider()
        value_flag(
            transport, "f", call.value_type, call.on if enabled else call.off, enabled=enabled, reason=server_reason
        )
        result = resolve(provider, call)
        assert (result.value, result.reason, result.variant) == (call.on if enabled else call.off, reason, variant)
        assert result.error_code is None

    def test_an_inactive_flag_answers_its_off_value_with_disabled(self, call):
        provider, transport = make_provider()
        value_flag(transport, "f", call.value_type, call.off, enabled=False, reason="inactive")
        result = resolve(provider, call)
        assert (result.value, result.reason, result.variant, result.error_code) == (
            call.off,
            Reason.DISABLED,
            "off",
            None,
        )

    @pytest.mark.parametrize("server_reason", [None, "a-newer-reason"])
    def test_a_reason_it_does_not_know_is_unknown(self, call, server_reason):
        provider, transport = make_provider()
        value_flag(transport, "f", call.value_type, call.on, reason=server_reason)
        result = resolve(provider, call)
        assert (result.value, result.reason, result.variant) == (call.on, Reason.UNKNOWN, "on")

    def test_the_servers_error_reason_answers_the_default_with_general(self, call):
        provider, transport = make_provider()
        value_flag(transport, "f", call.value_type, call.off, enabled=False, reason="error")
        result = resolve(provider, call)
        assert (result.value, result.reason, result.error_code, result.variant) == (
            call.default,
            Reason.ERROR,
            ErrorCode.GENERAL,
            None,
        )
        assert result.error_message == 'The server could not evaluate flag "f" for this user'

    @pytest.mark.parametrize("enabled", [True, False])
    def test_a_cached_answer_is_cached_with_the_value_and_variant_it_had(self, call, enabled):
        provider, transport = make_provider()
        value_flag(transport, "f", call.value_type, call.on if enabled else call.off, enabled=enabled)
        first = resolve(provider, call)
        second = resolve(provider, call)
        assert first.reason == Reason.SPLIT
        assert (second.value, second.reason, second.variant) == (first.value, Reason.CACHED, first.variant)
        assert len(transport.requests) == 1

    def test_another_type_answers_the_default_with_type_mismatch(self, call):
        provider, transport = make_provider()
        value_flag(transport, "f", call.other_type, call.other_value)
        result = resolve(provider, call)
        assert (result.value, result.reason, result.error_code, result.variant) == (
            call.default,
            Reason.ERROR,
            ErrorCode.TYPE_MISMATCH,
            None,
        )
        served = {"string": "a string", "number": "a number", "json": "a JSON value"}[call.other_type]
        assert result.error_message == f'Flag "f" serves {served}; it was resolved as {call.name}'

    def test_a_boolean_flag_answers_the_default_with_type_mismatch(self, call):
        provider, transport = make_provider()
        flag(transport, "f", enabled=True, reason="rollout", value=True, value_type="boolean")
        result = resolve(provider, call)
        assert (result.value, result.error_code) == (call.default, ErrorCode.TYPE_MISMATCH)
        assert result.error_message == f'Flag "f" serves a boolean; it was resolved as {call.name}'

    def test_an_answer_with_no_value_answers_the_default_with_general(self, call):
        """A server from before flag values: ``{key, enabled, config, reason}`` and no value."""
        provider, transport = make_provider()
        flag(transport, "f", enabled=True, reason="rollout")
        result = resolve(provider, call)
        assert (result.value, result.reason, result.error_code) == (call.default, Reason.ERROR, ErrorCode.GENERAL)
        assert "came back without a value" in result.error_message

    def test_a_value_that_is_not_of_the_flags_type_answers_the_default_with_parse_error(self, call):
        provider, transport = make_provider()
        value_flag(transport, "f", call.value_type, 42 if call.value_type == "string" else "not-it")
        result = resolve(provider, call)
        assert (result.value, result.reason, result.error_code) == (call.default, Reason.ERROR, ErrorCode.PARSE_ERROR)

    def test_without_a_value_type_the_value_decides_the_type(self, call):
        provider, transport = make_provider()
        flag(transport, "f", enabled=True, reason="rollout", value=call.on)
        assert resolve(provider, call).value == call.on

    def test_not_found_returns_default_with_flag_not_found(self, call):
        provider, transport = make_provider()
        transport.respond(404, json={"detail": "not found"})
        result = resolve(provider, call, key="missing")
        assert (result.value, result.reason, result.error_code) == (
            call.default,
            Reason.ERROR,
            ErrorCode.FLAG_NOT_FOUND,
        )

    def test_a_failed_request_returns_default_with_general(self, call):
        provider, transport = make_provider()
        transport.fail(TransportError("connection refused"))
        result = resolve(provider, call)
        assert (result.value, result.reason, result.error_code) == (call.default, Reason.ERROR, ErrorCode.GENERAL)

    def test_missing_targeting_key_returns_default_without_request(self, call):
        provider, transport = make_provider()
        result = getattr(provider, call.method)("f", call.default, EvaluationContext())
        assert (result.value, result.error_code) == (call.default, ErrorCode.TARGETING_KEY_MISSING)
        assert transport.requests == []


class TestIntegerAndFloat:
    def test_an_integer_call_accepts_a_number_with_no_fractional_part(self):
        provider, transport = make_provider()
        value_flag(transport, "n", "number", 10.0)
        result = provider.resolve_integer_details("n", 0, CTX)
        assert result.value == 10 and type(result.value) is int

    def test_an_integer_call_on_a_fractional_number_is_a_type_mismatch(self):
        provider, transport = make_provider()
        value_flag(transport, "n", "number", 2.5)
        result = provider.resolve_integer_details("n", 7, CTX)
        assert (result.value, result.error_code) == (7, ErrorCode.TYPE_MISMATCH)
        assert result.error_message == 'Flag "n" serves a number with a fractional part; it was resolved as an integer'

    def test_a_float_call_accepts_an_integer(self):
        provider, transport = make_provider()
        value_flag(transport, "n", "number", 10)
        result = provider.resolve_float_details("n", 0.0, CTX)
        assert result.value == 10.0 and type(result.value) is float

    @pytest.mark.parametrize("method", ["resolve_integer_details", "resolve_float_details"])
    def test_a_bool_is_never_a_number(self, method):
        provider, transport = make_provider()
        value_flag(transport, "n", "number", True)
        result = getattr(provider, method)("n", 5, CTX)
        assert (result.value, result.error_code) == (5, ErrorCode.PARSE_ERROR)

    @pytest.mark.parametrize("method", ["resolve_integer_details", "resolve_float_details"])
    def test_a_non_finite_number_is_not_one(self, method):
        provider, transport = make_provider()
        value_flag(transport, "n", "number", float("inf"))
        assert getattr(provider, method)("n", 5, CTX).error_code == ErrorCode.PARSE_ERROR

    def test_a_float_call_on_an_integer_too_large_for_a_float_never_raises(self):
        provider, transport = make_provider()
        value_flag(transport, "n", "number", 10**400)
        result = provider.resolve_float_details("n", 1.5, CTX)
        assert (result.value, result.error_code) == (1.5, ErrorCode.PARSE_ERROR)


class TestObject:
    def test_each_call_returns_a_new_copy(self):
        provider, transport = make_provider()
        value_flag(transport, "cfg", "json", {"theme": "dark", "limits": {"max": 3}})
        first = provider.resolve_object_details("cfg", {}, CTX)
        first.value["limits"]["max"] = 99
        second = provider.resolve_object_details("cfg", {}, CTX)
        assert second.reason == Reason.CACHED
        assert second.value == {"theme": "dark", "limits": {"max": 3}}

    def test_an_array_is_an_object_value(self):
        provider, transport = make_provider()
        value_flag(transport, "arr", "json", ["a", "b", "c"])
        assert provider.resolve_object_details("arr", [], CTX).value == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# A 0.2.0-shaped SDK: FlagEvaluation without value or value_type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlagEvaluation020:
    """The 0.2.0 SDK's ``FlagEvaluation``, field for field: no ``value``, no ``value_type``."""

    key: str
    enabled: bool
    config: Any = None
    reason: Optional[str] = None
    source: Optional[str] = None


class Sdk020Client:
    """The part of the 0.2.0 SDK's ``ExperimentationClient`` the provider calls."""

    def __init__(self, answer: FlagEvaluation020) -> None:
        self.answer = answer
        self.calls: list = []

    def cached_flags(self, user_id: str) -> list:
        return []

    def get_feature_flag(self, flag_key: str, user_id: str, user_attributes=None) -> FlagEvaluation020:
        self.calls.append((flag_key, user_id, user_attributes))
        return self.answer

    def clear_cache(self) -> None:
        pass


class TestAgainstA020Sdk:
    def test_the_stub_has_no_value_fields(self):
        answer = FlagEvaluation020("f", True, None, "rollout")
        assert not hasattr(answer, "value") and not hasattr(answer, "value_type")

    @pytest.mark.parametrize("call", CALLS, ids=CALL_IDS)
    @pytest.mark.parametrize("enabled, reason", [(True, "rollout"), (False, "inactive")])
    def test_a_typed_call_answers_the_default_and_never_raises(self, call, enabled, reason):
        client = Sdk020Client(FlagEvaluation020("f", enabled, None, reason))
        provider = ExperimentationProvider(client=client)
        result = resolve(provider, call, ctx=EvaluationContext("u1", {"plan": "pro"}))
        assert (result.value, result.reason, result.error_code) == (call.default, Reason.ERROR, ErrorCode.GENERAL)
        assert client.calls == [("f", "u1", {"plan": "pro"})]

    def test_boolean_answers_enabled(self):
        provider = ExperimentationProvider(client=Sdk020Client(FlagEvaluation020("f", True, None, "rollout")))
        result = provider.resolve_boolean_details("f", False, CTX)
        assert (result.value, result.reason) == (True, Reason.TARGETING_MATCH)


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


class TestCache:
    def test_cache_hit_makes_no_additional_requests(self):
        provider, transport = make_provider()
        flag(transport, "f1")
        for _ in range(5):
            provider.resolve_boolean_details("f1", False, CTX)
        assert len(transport.requests) == 1

    def test_cache_is_per_user(self):
        provider, transport = make_provider()
        flag(transport, "f1")
        provider.resolve_boolean_details("f1", False, EvaluationContext("u1"))
        provider.resolve_boolean_details("f1", False, EvaluationContext("u2"))
        assert [r.query["user_id"] for r in transport.requests] == ["u1", "u2"]

    def test_zero_ttl_refetches_every_time(self):
        provider, transport = make_provider(cache_ttl=0)
        flag(transport, "f1")
        for _ in range(3):
            assert provider.resolve_boolean_details("f1", False, CTX).reason == Reason.TARGETING_MATCH
        assert len(transport.requests) == 3

    def test_failed_evaluation_is_not_cached(self):
        provider, transport = make_provider()
        transport.respond(500, body=b"boom")
        flag(transport, "f")
        assert provider.resolve_boolean_details("f", False, CTX).error_code == ErrorCode.GENERAL
        assert provider.resolve_boolean_details("f", False, CTX).reason == Reason.TARGETING_MATCH
        assert len(transport.requests) == 2

    def test_shutdown_clears_cache(self):
        provider, transport = make_provider()
        flag(transport, "f")
        provider.resolve_boolean_details("f", False, CTX)
        provider.shutdown()
        assert provider.resolve_boolean_details("f", False, CTX).reason == Reason.TARGETING_MATCH
        assert len(transport.requests) == 2

    def test_shutdown_can_be_called_when_cache_empty(self):
        provider, _ = make_provider()
        provider.shutdown()  # must not raise


# ---------------------------------------------------------------------------
# Tracking
# ---------------------------------------------------------------------------


class TestTracking:
    def test_track_fans_out_to_cached_flags(self):
        provider, transport = make_provider()
        flag(transport, "f")
        transport.route("POST", "/api/v1/tracking/batch", json={"success_count": 1, "failure_count": 0})
        provider.resolve_boolean_details("f", False, CTX)

        provider.track("purchase", CTX, TrackingEventDetails(value=12.5, attributes={"sku": "A1"}))

        assert transport.last.path == "/api/v1/tracking/batch"
        assert transport.last.json() == {
            "events": [
                {
                    "event_type": "purchase",
                    "event_name": "purchase",
                    "user_id": "user-1",
                    "value": 12.5,
                    "metadata": {"sku": "A1"},
                    "feature_flag_key": "f",
                }
            ]
        }

    def test_track_without_targeting_key_sends_nothing(self):
        provider, transport = make_provider()
        provider.track("purchase", None, TrackingEventDetails(value=1.0))
        assert transport.requests == []

    def test_track_never_raises(self):
        provider, transport = make_provider()
        flag(transport, "f")
        provider.resolve_boolean_details("f", False, CTX)
        transport.fail(RuntimeError("boom"))
        provider.track("purchase", CTX)  # must not raise


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_evaluations_do_not_corrupt_state(self):
        provider, transport = make_provider()
        flag(transport, "f1", enabled=True)
        flag(transport, "f2", enabled=False)
        errors: list = []

        def evaluate(user_id: str) -> None:
            try:
                ctx = EvaluationContext(user_id)
                assert provider.resolve_boolean_details("f1", False, ctx).value is True
                assert provider.resolve_boolean_details("f2", True, ctx).value is False
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            concurrent.futures.wait([executor.submit(evaluate, f"user-{i}") for i in range(100)])

        assert errors == []
        seen = len(transport.requests)
        evaluate("user-0")
        assert len(transport.requests) == seen  # served from cache


# ---------------------------------------------------------------------------
# Hash algorithm correctness (cross-SDK test vectors)
# ---------------------------------------------------------------------------


class TestHashAlgorithm:
    def test_hash_user_returns_float_in_unit_interval(self):
        assert 0.0 <= hash_user("user-123", "my-flag") < 1.0

    def test_hash_user_is_deterministic(self):
        assert hash_user("alice", "dark-mode") == hash_user("alice", "dark-mode")

    def test_hash_user_differs_for_different_inputs(self):
        assert hash_user("alice", "flag") != hash_user("bob", "flag")
        assert hash_user("alice", "flag-a") != hash_user("alice", "flag-b")

    def test_hash_user_empty_user_id(self):
        assert 0.0 <= hash_user("", "some-flag") < 1.0

    @pytest.mark.parametrize("vector", _golden_hash_vectors(), ids=lambda v: f"{v['user_id']}:{v['flag_key']}")
    def test_matches_shared_golden_vectors(self, vector):
        assert abs(hash_user(vector["user_id"], vector["flag_key"]) - vector["expected_hash"]) < 1e-10

    def test_cross_sdk_vector_user123_my_flag(self):
        """MD5("user-123:my-flag") = 43bc57b1e81dec71c5242122ac05170f → 0.6927449859213084."""
        assert abs(hash_user("user-123", "my-flag") - 0.6927449859213084) < 1e-10

    def test_cross_sdk_vector_alice_dark_mode(self):
        assert abs(hash_user("alice", "dark-mode") - 0.0353864398784935) < 1e-10

    def test_cross_sdk_vector_manually_verified(self):
        digest = hashlib.md5("user-123:my-flag".encode("utf-8")).digest()  # noqa: S324
        (uint32,) = struct.unpack_from("<I", digest[:4])
        assert hash_user("user-123", "my-flag") == uint32 / 4294967296


# ---------------------------------------------------------------------------
# OpenFeature api integration
# ---------------------------------------------------------------------------


class TestOpenFeatureIntegration:
    @pytest.fixture(autouse=True)
    def reset_provider(self):
        yield
        api.set_provider(NoOpProvider())

    def test_provider_can_be_registered_with_api_set_provider(self):
        provider, transport = make_provider()
        flag(transport, "reg-flag")
        value_flag(transport, "label", "string", "Complete purchase", reason="targeting_rule")
        api.set_provider(provider)
        client = api.get_client()
        ctx = EvaluationContext("u1", {"plan": "enterprise"})

        assert client.get_boolean_value("reg-flag", False, ctx) is True
        assert client.get_string_value("label", "Buy now", ctx) == "Complete purchase"
        details = client.get_string_details("label", "Buy now", ctx)
        assert (details.reason, details.variant) == (Reason.CACHED, "on")
        assert len(transport.requests) == 2
        assert [json.loads(r.query["context"]) for r in transport.requests] == [{"plan": "enterprise"}] * 2

    def test_the_client_returns_the_off_value_of_an_inactive_flag(self):
        provider, transport = make_provider()
        value_flag(transport, "label", "string", "Buy now", enabled=False, reason="inactive")
        api.set_provider(provider)
        details = api.get_client().get_string_details("label", "my-default", EvaluationContext("u1"))
        assert (details.value, details.reason, details.variant, details.error_code) == (
            "Buy now",
            Reason.DISABLED,
            "off",
            None,
        )

    def test_the_clients_typed_calls_get_the_python_type_they_check(self):
        provider, transport = make_provider()
        value_flag(transport, "whole", "number", 10.0)
        value_flag(transport, "count", "number", 10)
        value_flag(transport, "cfg", "json", {"theme": "dark"})
        api.set_provider(provider)
        client = api.get_client()
        ctx = EvaluationContext("u1")
        integer = client.get_integer_details("whole", 0, ctx)
        assert (integer.value, type(integer.value), integer.error_code) == (10, int, None)
        number = client.get_float_details("count", 0.0, ctx)
        assert (number.value, type(number.value), number.error_code) == (10.0, float, None)
        assert client.get_object_value("cfg", {}, ctx) == {"theme": "dark"}

    def test_a_type_mismatch_reaches_the_caller_as_its_default(self):
        provider, transport = make_provider()
        value_flag(transport, "count", "number", 10)
        api.set_provider(provider)
        details = api.get_client().get_string_details("count", "fallback", EvaluationContext("u1"))
        assert (details.value, details.reason, details.error_code) == (
            "fallback",
            Reason.ERROR,
            ErrorCode.TYPE_MISMATCH,
        )
        assert details.error_message == 'Flag "count" serves a number; it was resolved as a string'

    def test_error_details_are_surfaced_through_the_client(self):
        provider, transport = make_provider()
        transport.route("GET", f"{EVALUATE}/missing", status=404, json={"detail": "not found"})
        api.set_provider(provider)
        details = api.get_client().get_string_details("missing", "fallback", EvaluationContext("u1"))
        assert details.value == "fallback"
        assert details.error_code == ErrorCode.FLAG_NOT_FOUND
        assert details.reason == Reason.ERROR
