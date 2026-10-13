"""
Tests for ExperimentationProvider (OpenFeature Python provider).

The provider delegates to the ``experimentation`` SDK; its HTTP transport is
replaced with ``experimentation.testing.FakeTransport`` so every test can
assert the exact request that would reach the backend.

Coverage:
  - Construction, metadata, hooks, client injection, base_url/timeout/ttl forwarding
  - Request shape: GET /api/v1/feature-flags/evaluate/{key}?user_id=<targeting_key>, no context
  - resolve_boolean/string/integer/float/object_details value + reason mapping
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
from pathlib import Path

import pytest
from openfeature import api
from openfeature.evaluation_context import EvaluationContext
from openfeature.exception import ErrorCode
from openfeature.flag_evaluation import Reason
from openfeature.provider import TrackingEventDetails
from openfeature.provider.no_op_provider import NoOpProvider

from experimentation import ExperimentationClient, TransportError
from experimentation.testing import FakeTransport
from experimentation_openfeature import PROVIDER_NAME, ExperimentationProvider, ProviderConfig
from experimentation_openfeature.evaluator import hash_user

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


def flag(transport: FakeTransport, key: str, enabled: bool = True, config=None) -> FakeTransport:
    return transport.route("GET", f"{EVALUATE}/{key}", json={"key": key, "enabled": enabled, "config": config})


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_requires_api_key(self):
        with pytest.raises(ValueError, match="api_key is required"):
            ExperimentationProvider(api_key="")

    def test_metadata_name(self):
        provider, _ = make_provider()
        assert provider.get_metadata().name == PROVIDER_NAME == "experimently-provider"

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
    def test_evaluate_request_url_headers_and_no_context(self):
        provider, transport = make_provider()
        flag(transport, "dark-mode")
        ctx = EvaluationContext(targeting_key="user-1", attributes={"plan": "pro", "country": "US"})
        provider.resolve_boolean_details("dark-mode", False, ctx)

        request = transport.last
        assert request.method == "GET"
        assert request.path == "/api/v1/feature-flags/evaluate/dark-mode"
        assert request.query == {"user_id": "user-1"}
        assert request.body is None
        assert request.headers["X-API-Key"] == "test-key"
        assert request.headers["Accept"] == "application/json"
        assert "plan" not in request.url and "pro" not in request.url  # attributes are not sent

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

    def test_not_found_returns_default_with_flag_not_found(self):
        provider, transport = make_provider()
        transport.respond(404, json={"detail": "Feature flag with key 'missing' not found or not active"})
        result = provider.resolve_boolean_details("missing", True, CTX)
        assert result.value is True
        assert result.error_code == ErrorCode.FLAG_NOT_FOUND
        assert result.reason == Reason.ERROR
        assert "404" in result.error_message

    @pytest.mark.parametrize("status", [401, 500])
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
# String resolution
# ---------------------------------------------------------------------------


class TestResolveStringDetails:
    def test_returns_config_variant(self):
        provider, transport = make_provider()
        flag(transport, "ab-test", config={"variant": "new-design", "cta": "Buy"})
        result = provider.resolve_string_details("ab-test", "original", CTX)
        assert result.value == "new-design"
        assert result.variant == "new-design"
        assert result.reason == Reason.TARGETING_MATCH

    def test_returns_scalar_string_config(self):
        provider, transport = make_provider()
        flag(transport, "layout", config="compact")
        assert provider.resolve_string_details("layout", "wide", CTX).value == "compact"

    @pytest.mark.parametrize("config", [None, {"engine": "v2"}, {"variant": 42}, 7], ids=["null", "no-variant", "non-str", "int"])
    def test_default_when_config_has_no_string_variant(self, config):
        provider, transport = make_provider()
        flag(transport, "f", config=config)
        result = provider.resolve_string_details("f", "my-default", CTX)
        assert result.value == "my-default"
        assert result.reason == Reason.DEFAULT
        assert result.error_code is None

    def test_disabled_flag_returns_default_with_disabled(self):
        provider, transport = make_provider()
        flag(transport, "off-flag", enabled=False, config={"variant": "x"})
        result = provider.resolve_string_details("off-flag", "my-default", CTX)
        assert result.value == "my-default"
        assert result.reason == Reason.DISABLED

    def test_not_found_returns_default(self):
        provider, transport = make_provider()
        transport.respond(404, json={"detail": "not found"})
        result = provider.resolve_string_details("missing", "my-default", CTX)
        assert result.value == "my-default"
        assert result.error_code == ErrorCode.FLAG_NOT_FOUND


# ---------------------------------------------------------------------------
# Integer resolution
# ---------------------------------------------------------------------------


class TestResolveIntegerDetails:
    def test_returns_config_value(self):
        provider, transport = make_provider()
        flag(transport, "count-flag", config={"value": 42})
        result = provider.resolve_integer_details("count-flag", 0, CTX)
        assert result.value == 42 and isinstance(result.value, int)
        assert result.reason == Reason.TARGETING_MATCH

    def test_returns_scalar_int_config(self):
        provider, transport = make_provider()
        flag(transport, "n", config=7)
        assert provider.resolve_integer_details("n", 0, CTX).value == 7

    @pytest.mark.parametrize("config", [{"value": True}, {"value": 3.14}, {"value": "12"}, {"variant": "a"}, None])
    def test_default_when_value_is_not_an_integer(self, config):
        """Python bool is a subclass of int; the provider must reject it."""
        provider, transport = make_provider()
        flag(transport, "f", config=config)
        result = provider.resolve_integer_details("f", 99, CTX)
        assert result.value == 99
        assert result.reason == Reason.DEFAULT

    def test_not_found_returns_default(self):
        provider, transport = make_provider()
        transport.respond(404, json={"detail": "not found"})
        result = provider.resolve_integer_details("missing", 99, CTX)
        assert result.value == 99
        assert result.error_code == ErrorCode.FLAG_NOT_FOUND


# ---------------------------------------------------------------------------
# Float resolution
# ---------------------------------------------------------------------------


class TestResolveFloatDetails:
    def test_returns_config_value(self):
        provider, transport = make_provider()
        flag(transport, "price-flag", config={"value": 9.99})
        result = provider.resolve_float_details("price-flag", 0.0, CTX)
        assert abs(result.value - 9.99) < 1e-9
        assert result.error_code is None

    def test_integer_value_coerced_to_float(self):
        provider, transport = make_provider()
        flag(transport, "int-as-float", config={"value": 10})
        result = provider.resolve_float_details("int-as-float", 0.0, CTX)
        assert result.value == 10.0 and isinstance(result.value, float)

    def test_scalar_numeric_config(self):
        provider, transport = make_provider()
        flag(transport, "m", config=1.5)
        assert provider.resolve_float_details("m", 1.0, CTX).value == 1.5

    @pytest.mark.parametrize("config", [{"value": "not-float"}, {"value": False}, None])
    def test_default_when_value_is_not_numeric(self, config):
        provider, transport = make_provider()
        flag(transport, "f", config=config)
        result = provider.resolve_float_details("f", 3.14, CTX)
        assert abs(result.value - 3.14) < 1e-9
        assert result.reason == Reason.DEFAULT


# ---------------------------------------------------------------------------
# Object resolution
# ---------------------------------------------------------------------------


class TestResolveObjectDetails:
    def test_returns_dict_config(self):
        provider, transport = make_provider()
        config = {"theme": "dark", "maxRetries": 3, "variant": "v1"}
        flag(transport, "config-flag", config=config)
        result = provider.resolve_object_details("config-flag", {}, CTX)
        assert result.value == config
        assert result.variant == "v1"
        assert result.reason == Reason.TARGETING_MATCH

    def test_returns_list_config(self):
        provider, transport = make_provider()
        flag(transport, "arr-flag", config=["a", "b", "c"])
        assert provider.resolve_object_details("arr-flag", [], CTX).value == ["a", "b", "c"]

    @pytest.mark.parametrize("config", ["not-object", 99, None])
    def test_default_when_config_is_not_an_object(self, config):
        provider, transport = make_provider()
        flag(transport, "f", config=config)
        result = provider.resolve_object_details("f", {"default": True}, CTX)
        assert result.value == {"default": True}
        assert result.reason == Reason.DEFAULT

    def test_disabled_returns_default(self):
        provider, transport = make_provider()
        flag(transport, "f", enabled=False, config={"theme": "dark"})
        result = provider.resolve_object_details("f", {}, CTX)
        assert result.value == {}
        assert result.reason == Reason.DISABLED


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
        flag(transport, "reg-flag", config={"variant": "treatment"})
        api.set_provider(provider)
        client = api.get_client()
        ctx = EvaluationContext("u1")

        assert client.get_boolean_value("reg-flag", False, ctx) is True
        assert client.get_string_value("reg-flag", "control", ctx) == "treatment"
        details = client.get_boolean_details("reg-flag", False, ctx)
        assert details.reason == Reason.CACHED
        assert details.variant == "treatment"
        assert len(transport.requests) == 1

    def test_error_details_are_surfaced_through_the_client(self):
        provider, transport = make_provider()
        transport.route("GET", f"{EVALUATE}/missing", status=404, json={"detail": "not found"})
        api.set_provider(provider)
        details = api.get_client().get_string_details("missing", "fallback", EvaluationContext("u1"))
        assert details.value == "fallback"
        assert details.error_code == ErrorCode.FLAG_NOT_FOUND
        assert details.reason == Reason.ERROR
