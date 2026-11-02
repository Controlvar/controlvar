"""
ExperimentationProvider — OpenFeature provider for Controlvar.

Every flag is evaluated **by the server**: the provider delegates to the
``controlvar`` Python SDK, which calls
``GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=<targeting_key>&context=<attributes>``
and caches the answer per user + flag key for ``cache_ttl`` seconds. Nothing is
bucketed locally and no flag definitions are downloaded, unless ``client=`` is
an SDK client built with ``evaluation="local"``: that client answers each call
it can from its ruleset, with that call's attributes.

Resolution rules
----------------
* ``EvaluationContext.targeting_key`` is the platform ``user_id`` and is
  required: without it the default value is returned with
  ``TARGETING_KEY_MISSING``.
* ``EvaluationContext.attributes`` are the user's attributes: the SDK sends
  them as the evaluate call's ``context``, so the flag's targeting rules see
  them. A ``datetime``, at any depth, is sent as an ISO 8601 string (a naive one
  is taken as UTC). The SDK's cache key is user + flag key, not the attributes:
  within ``cache_ttl`` a user's answer is reused whatever attributes the next
  call brings.
* Every call type follows one table. The value is the flag's when the flag is
  of the type the call reads: a boolean call reads a boolean flag, whose value
  is ``enabled`` (and every flag of a server or an SDK without flag values,
  which sends no ``value_type``); a string call a string flag; an integer or a
  float call a number flag; an object call a json flag. That is its on value
  when the flag is on for the user, its off value otherwise, an inactive flag
  included. The variant is ``"on"`` or ``"off"``; the reason is ``CACHED`` for
  an answer from the SDK cache, otherwise the server's: ``targeting_rule`` ->
  ``TARGETING_MATCH``, ``rollout`` -> ``SPLIT``, ``inactive`` -> ``DISABLED``
  (anything else ``UNKNOWN``). An integer call accepts a number with no
  fractional part (``10`` or ``10.0`` answers ``10``); a float call accepts
  any number (``10`` answers ``10.0``); ``bool`` is never a number; an object
  call returns a new copy each time.
* Otherwise the caller's default with reason ``ERROR``: ``TYPE_MISMATCH``
  when the flag serves another type (or a number with a fractional part to an
  integer call), ``GENERAL`` for the server's ``reason: "error"`` or an
  answer with no value (a server or an SDK from before flag values),
  ``PARSE_ERROR`` for a value that is not of the flag's type.
* Failures never raise: 404 -> ``FLAG_NOT_FOUND``, anything else (a 2xx with
  a body that is not an evaluation included) -> ``GENERAL``; both return the
  default value with reason ``ERROR``.

``value``, ``value_type``, ``reason`` and ``source`` are read from the SDK's
``FlagEvaluation`` with ``getattr``: the 0.2.0 SDK has no ``value`` or
``value_type``, and against it a boolean call answers ``enabled`` and every
string, number and object call answers the caller's default with ``GENERAL``.
"""

from __future__ import annotations

import copy
import json
import logging
import math
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Callable, List, NamedTuple, Optional, Union

from openfeature.evaluation_context import EvaluationContext
from openfeature.exception import ErrorCode
from openfeature.flag_evaluation import FlagResolutionDetails, Reason
from openfeature.hook import Hook
from openfeature.provider import AbstractProvider, Metadata, TrackingEventDetails

from controlvar import ExperimentationClient, ExperimentationError
from controlvar.transport import Transport

logger = logging.getLogger(__name__)

PROVIDER_NAME = "controlvar-provider"

#: OpenFeature's reason for the server's ``reason``.
_SERVER_REASONS = {
    "targeting_rule": Reason.TARGETING_MATCH,
    "rollout": Reason.SPLIT,
    "inactive": Reason.DISABLED,
}


class _NotAnInteger(ValueError):
    """A number with a fractional part, resolved as an integer."""


class _Call(NamedTuple):
    """A resolution: the flag type it reads, how a message names it, and how it turns a
    value of that type into the value OpenFeature expects."""

    value_type: str
    name: str
    convert: Callable[[Any], Any]


def _to_int(value: Any) -> int:
    if isinstance(value, int):
        return value
    if value.is_integer():
        return int(value)
    raise _NotAnInteger()


_BOOLEAN = _Call("boolean", "a boolean", bool)
_STRING = _Call("string", "a string", lambda value: value)
_INTEGER = _Call("number", "an integer", _to_int)
_FLOAT = _Call("number", "a float", float)
# A new copy each call: changing it must not change the evaluation the SDK keeps.
_OBJECT = _Call("json", "an object", copy.deepcopy)


class _Lookup(NamedTuple):
    """The SDK's answer for a flag, and whether it came from the SDK's cache."""

    evaluation: Any
    cached: bool


class _Failure(NamedTuple):
    """Why a call answers the caller's default."""

    code: ErrorCode
    message: str


def _json_type_of(value: Any) -> Optional[str]:
    """The type of a value the server sent without a ``value_type``; ``None`` for no value."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, (dict, list)):
        return "json"
    return type(value).__name__


def _is_value_of_type(value: Any, value_type: str) -> bool:
    if value_type == "string":
        return isinstance(value, str)
    if value_type == "number":
        if isinstance(value, float):
            return math.isfinite(value)
        return isinstance(value, int) and not isinstance(value, bool)
    if value_type == "json":
        return isinstance(value, (dict, list))
    return False


def _describe_type(value_type: str) -> str:
    """A flag type as the error messages name it."""
    names = {"boolean": "a boolean", "string": "a string", "number": "a number", "json": "a JSON value"}
    if value_type in names:
        return names[value_type]
    return f"a value of type {json.dumps(value_type[:64], ensure_ascii=False)}"


def _plain(value: Any) -> Any:
    """``value`` as JSON can carry it: a ``datetime``, at any depth, as an ISO 8601 string
    (a naive one taken as UTC, as the Python SDK sends one)."""
    if isinstance(value, datetime):
        return (value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)).isoformat()
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _attributes_of(evaluation_context: Optional[EvaluationContext]) -> dict:
    """The evaluation context minus ``targeting_key``: its attributes, as the SDK sends them."""
    if evaluation_context is None:
        return {}
    return {key: _plain(value) for key, value in (evaluation_context.attributes or {}).items()}


def _boolean_value(evaluation: Any, flag: str) -> Any:
    """A boolean call's value: ``enabled``, for a boolean flag or for an answer with no
    ``value_type`` (a server or an SDK without flag values, where every flag is boolean)."""
    # getattr: the 0.2.0 SDK's FlagEvaluation has no value_type.
    value_type = getattr(evaluation, "value_type", None)
    if isinstance(value_type, str) and value_type != "boolean":
        return _Failure(
            ErrorCode.TYPE_MISMATCH,
            f"Flag {flag} serves {_describe_type(value_type)}; it was resolved as {_BOOLEAN.name}",
        )
    return bool(evaluation.enabled)


def _typed_value(evaluation: Any, flag: str, call: _Call) -> Any:
    """A string, integer, float or object call's value: the flag's ``value`` when it is of
    the type ``call`` reads, converted as OpenFeature expects it; otherwise a :class:`_Failure`."""
    no_value = _Failure(
        ErrorCode.GENERAL,
        f"Flag {flag} came back without a value (from a server or an SDK without flag values, "
        f"or a server that cannot serve this flag's stored value)",
    )
    # getattr: the 0.2.0 SDK's FlagEvaluation has neither field.
    value = getattr(evaluation, "value", None)
    value_type = getattr(evaluation, "value_type", None)
    served = value_type if isinstance(value_type, str) else _json_type_of(value)
    if served is None:
        return no_value
    if served != call.value_type:
        return _Failure(
            ErrorCode.TYPE_MISMATCH, f"Flag {flag} serves {_describe_type(served)}; it was resolved as {call.name}"
        )
    if value is None:
        return no_value
    if not _is_value_of_type(value, call.value_type):
        return _Failure(
            ErrorCode.PARSE_ERROR,
            f"Flag {flag} serves {_describe_type(call.value_type)}, but the server's value is not one",
        )
    try:
        return call.convert(value)
    except _NotAnInteger:
        return _Failure(
            ErrorCode.TYPE_MISMATCH,
            f"Flag {flag} serves a number with a fractional part; it was resolved as an integer",
        )
    except (OverflowError, RecursionError) as exc:  # a value no supported write can store
        return _Failure(
            ErrorCode.PARSE_ERROR,
            f"Flag {flag} serves {_describe_type(call.value_type)} that {call.name} cannot hold ({type(exc).__name__})",
        )


def _targeting_key(evaluation_context: Optional[EvaluationContext]) -> Optional[str]:
    if evaluation_context is None:
        return None
    key = evaluation_context.targeting_key
    return key if isinstance(key, str) and key else None


def _error(default_value: Any, code: ErrorCode, message: str) -> FlagResolutionDetails[Any]:
    return FlagResolutionDetails(value=default_value, reason=Reason.ERROR, error_code=code, error_message=message)


class ExperimentationProvider(AbstractProvider):
    """
    OpenFeature provider backed by the Controlvar public API.

    Example::

        from openfeature import api
        from openfeature.evaluation_context import EvaluationContext
        from controlvar_openfeature import ExperimentationProvider

        api.set_provider(ExperimentationProvider(api_key="your-api-key"))
        client = api.get_client()
        ctx = EvaluationContext("user-123", {"plan": "enterprise"})
        enabled = client.get_boolean_value("dark-mode", False, ctx)
        label = client.get_string_value("checkout-button-label", "Buy now", ctx)

    ``provider.client`` exposes the underlying
    :class:`controlvar.ExperimentationClient` for experiment assignment
    and event tracking, which OpenFeature does not model.
    """

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "http://localhost:8000",
        cache_ttl: float = 300,
        timeout: float = 10,
        *,
        client: Optional[ExperimentationClient] = None,
        transport: Optional[Transport] = None,
    ) -> None:
        """
        Args:
            api_key:   API key sent in the ``X-API-Key`` header (required unless ``client`` is given).
            base_url:  Backend origin; the SDK appends ``/api/v1/...``.
            cache_ttl: Seconds a successful evaluation is reused per user + flag key, whatever
                       the attributes. Default 300; 0 never reuses one.
            timeout:   HTTP request timeout in seconds. Default 10.
            client:    A pre-configured :class:`controlvar.ExperimentationClient` to share
                       with the rest of your application (overrides the other arguments).
            transport: Custom HTTP transport (tests use ``controlvar.testing.FakeTransport``).
        """
        if client is None:
            if not api_key:
                raise ValueError("ExperimentationProvider: api_key is required")
            client = ExperimentationClient(
                api_url=base_url,
                api_key=api_key,
                timeout_seconds=timeout,
                cache_ttl_seconds=cache_ttl,
                transport=transport,
            )
        self._client = client

    @property
    def client(self) -> ExperimentationClient:
        """The underlying SDK client (use it for ``get_assignment`` / ``track``)."""
        return self._client

    # ------------------------------------------------------------------
    # AbstractProvider interface
    # ------------------------------------------------------------------

    def get_metadata(self) -> Metadata:
        return Metadata(PROVIDER_NAME)

    def get_provider_hooks(self) -> List[Hook]:
        return []

    def initialize(self, evaluation_context: Optional[EvaluationContext] = None) -> None:
        """Nothing to warm up: flags are evaluated per user, on demand."""

    def shutdown(self) -> None:
        """Drop cached evaluations. Called by the OpenFeature SDK on provider change."""
        self._client.clear_cache()

    def track(
        self,
        tracking_event_name: str,
        evaluation_context: Optional[EvaluationContext] = None,
        tracking_event_details: Optional[TrackingEventDetails] = None,
    ) -> None:
        """OpenFeature tracking → ``ExperimentationClient.track`` (an event with no key is
        fanned out by the Python SDK's rule; never raises)."""
        user_id = _targeting_key(evaluation_context)
        if user_id is None:
            logger.warning("track(%s) ignored: targeting_key is required", tracking_event_name)
            return
        value = tracking_event_details.value if tracking_event_details is not None else None
        attributes = dict(tracking_event_details.attributes) if tracking_event_details is not None else {}
        self._client.track(user_id, tracking_event_name, event_value=value, properties=attributes or None)

    # ------------------------------------------------------------------
    # Flag resolution
    # ------------------------------------------------------------------

    def resolve_boolean_details(
        self,
        flag_key: str,
        default_value: bool,
        evaluation_context: Optional[EvaluationContext] = None,
    ) -> FlagResolutionDetails[bool]:
        return self._resolve(flag_key, default_value, evaluation_context, _BOOLEAN)

    def resolve_string_details(
        self,
        flag_key: str,
        default_value: str,
        evaluation_context: Optional[EvaluationContext] = None,
    ) -> FlagResolutionDetails[str]:
        return self._resolve(flag_key, default_value, evaluation_context, _STRING)

    def resolve_integer_details(
        self,
        flag_key: str,
        default_value: int,
        evaluation_context: Optional[EvaluationContext] = None,
    ) -> FlagResolutionDetails[int]:
        return self._resolve(flag_key, default_value, evaluation_context, _INTEGER)

    def resolve_float_details(
        self,
        flag_key: str,
        default_value: float,
        evaluation_context: Optional[EvaluationContext] = None,
    ) -> FlagResolutionDetails[float]:
        return self._resolve(flag_key, default_value, evaluation_context, _FLOAT)

    def resolve_object_details(
        self,
        flag_key: str,
        default_value: Union[dict, list],
        evaluation_context: Optional[EvaluationContext] = None,
    ) -> FlagResolutionDetails[Union[dict, list]]:
        return self._resolve(flag_key, default_value, evaluation_context, _OBJECT)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _lookup(
        self,
        flag_key: str,
        default_value: Any,
        evaluation_context: Optional[EvaluationContext],
    ) -> Union[_Lookup, FlagResolutionDetails[Any]]:
        """The SDK's answer for ``targeting_key`` and the context's attributes, or the
        caller's default with an error. Never raises."""
        user_id = _targeting_key(evaluation_context)
        if user_id is None:
            return _error(
                default_value,
                ErrorCode.TARGETING_KEY_MISSING,
                "EvaluationContext.targeting_key (the platform user_id) is required",
            )
        try:
            attributes = _attributes_of(evaluation_context)
            held = any(entry.key == flag_key for entry in self._client.cached_flags(user_id))
            evaluation = self._client.get_feature_flag(flag_key, user_id, attributes)
            # A client with local evaluation keeps its local answers in the same cache, but
            # computes each one afresh from this call's attributes: only an answer it did not
            # compute came from the cache.
            cached = held and getattr(evaluation, "source", None) != "local"
        except ExperimentationError as exc:
            code = ErrorCode.FLAG_NOT_FOUND if exc.status == 404 else ErrorCode.GENERAL
            logger.warning("Flag %r could not be evaluated for %r: %s", flag_key, user_id, exc)
            return _error(default_value, code, str(exc))
        except Exception as exc:  # a resolution never raises
            logger.warning("Flag %r could not be evaluated for %r: %s", flag_key, user_id, exc)
            return _error(
                default_value,
                ErrorCode.GENERAL,
                f"Flag {json.dumps(str(flag_key), ensure_ascii=False)} could not be evaluated "
                f"({type(exc).__name__}: {exc})",
            )
        return _Lookup(evaluation, cached)

    def _resolve(
        self,
        flag_key: str,
        default_value: Any,
        evaluation_context: Optional[EvaluationContext],
        call: _Call,
    ) -> FlagResolutionDetails[Any]:
        """Every call's one body: the flag's value when it serves the type ``call`` reads (a
        boolean flag's is ``enabled``), and otherwise ``default_value`` with an error."""
        found = self._lookup(flag_key, default_value, evaluation_context)
        if not isinstance(found, _Lookup):
            return found
        evaluation = found.evaluation
        flag = json.dumps(str(flag_key), ensure_ascii=False)
        server_reason = getattr(evaluation, "reason", None)
        if server_reason == "error":
            return _error(default_value, ErrorCode.GENERAL, f"The server could not evaluate flag {flag} for this user")
        value = _boolean_value(evaluation, flag) if call is _BOOLEAN else _typed_value(evaluation, flag, call)
        if isinstance(value, _Failure):
            return _error(default_value, value.code, value.message)
        if found.cached:
            reason = Reason.CACHED
        else:
            reason = _SERVER_REASONS.get(server_reason, Reason.UNKNOWN)
        return FlagResolutionDetails(value=value, reason=reason, variant="on" if evaluation.enabled else "off")
