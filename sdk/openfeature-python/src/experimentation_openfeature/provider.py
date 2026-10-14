"""
ExperimentationProvider — OpenFeature provider for Experimently.

Every flag is evaluated **by the server**: the provider delegates to the
``experimentation`` Python SDK, which calls
``GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=<targeting_key>`` and
caches the answer per user + key for ``cache_ttl`` seconds. Nothing is
bucketed locally and no flag definitions are downloaded.

Resolution rules
----------------
* ``EvaluationContext.targeting_key`` is the platform ``user_id`` and is
  required: without it the default value is returned with
  ``TARGETING_KEY_MISSING``. Context attributes are **not** sent: the
  evaluate endpoint takes a ``context``, which this provider does not send yet.
* boolean flags   -> ``enabled``.
* string, integer/float and object flags -> the default value with reason
  ``DEFAULT``: the evaluate endpoint returns ``config: null`` for every flag,
  so there is nothing to read. The provider would read ``config["variant"]``
  (or ``config`` itself when it is a string) for a string, ``config["value"]``
  (or ``config`` itself when numeric; ``bool`` is never accepted as a number)
  for a number, and ``config`` when it is a dict or list for an object, if the
  server ever sent one.
* A disabled flag resolves non-boolean requests to the default value with
  reason ``DISABLED``.
* Failures never raise: 404 -> ``FLAG_NOT_FOUND``, anything else ->
  ``GENERAL``; both return the default value with reason ``ERROR``.

Reasons: ``TARGETING_MATCH`` (fresh from the server, flag on), ``DISABLED``
(flag off for this user), ``CACHED`` (served from the SDK cache), ``DEFAULT``
(``config`` lacks the requested field), ``ERROR``.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, List, Optional, Tuple, Union

from openfeature.evaluation_context import EvaluationContext
from openfeature.exception import ErrorCode
from openfeature.flag_evaluation import FlagResolutionDetails, Reason
from openfeature.hook import Hook
from openfeature.provider import AbstractProvider, Metadata, TrackingEventDetails

from experimentation import ExperimentationClient, ExperimentationError, FlagEvaluation
from experimentation.transport import Transport

logger = logging.getLogger(__name__)

PROVIDER_NAME = "experimently-provider"

# (value, found) — ``found`` is False when the config carries nothing usable.
_Picker = Callable[[FlagEvaluation], Tuple[Any, bool]]


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_object(value: Any) -> bool:
    return isinstance(value, (dict, list))


def _from_config(config: Any, field: str, accept: Callable[[Any], bool]) -> Tuple[Any, bool]:
    """``config[field]`` when it passes ``accept``, else ``config`` itself when it does."""
    if isinstance(config, dict) and accept(config.get(field)):
        return config[field], True
    if accept(config):
        return config, True
    return None, False


def _pick_bool(evaluation: FlagEvaluation) -> Tuple[Any, bool]:
    return evaluation.enabled, True


def _pick_str(evaluation: FlagEvaluation) -> Tuple[Any, bool]:
    if not evaluation.enabled:
        return None, False
    return _from_config(evaluation.config, "variant", _is_str)


def _pick_int(evaluation: FlagEvaluation) -> Tuple[Any, bool]:
    if not evaluation.enabled:
        return None, False
    return _from_config(evaluation.config, "value", _is_int)


def _pick_float(evaluation: FlagEvaluation) -> Tuple[Any, bool]:
    if not evaluation.enabled:
        return None, False
    value, found = _from_config(evaluation.config, "value", _is_number)
    return (float(value), True) if found else (None, False)


def _pick_object(evaluation: FlagEvaluation) -> Tuple[Any, bool]:
    if evaluation.enabled and _is_object(evaluation.config):
        return evaluation.config, True
    return None, False


def _variant_label(config: Any) -> Optional[str]:
    if isinstance(config, dict) and isinstance(config.get("variant"), str):
        return config["variant"]
    return None


def _targeting_key(evaluation_context: Optional[EvaluationContext]) -> Optional[str]:
    if evaluation_context is None:
        return None
    key = evaluation_context.targeting_key
    return key if isinstance(key, str) and key else None


class ExperimentationProvider(AbstractProvider):
    """
    OpenFeature provider backed by the Experimently public API.

    Example::

        from openfeature import api
        from openfeature.evaluation_context import EvaluationContext
        from experimentation_openfeature import ExperimentationProvider

        api.set_provider(ExperimentationProvider(api_key="your-api-key"))
        client = api.get_client()
        enabled = client.get_boolean_value("dark-mode", False, EvaluationContext("user-123"))

    ``provider.client`` exposes the underlying
    :class:`experimentation.ExperimentationClient` for experiment assignment
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
            cache_ttl: Seconds a successful evaluation is reused per user + flag. Default 300.
            timeout:   HTTP request timeout in seconds. Default 10.
            client:    A pre-configured :class:`experimentation.ExperimentationClient` to share
                       with the rest of your application (overrides the other arguments).
            transport: Custom HTTP transport (tests use ``experimentation.testing.FakeTransport``).
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
        """OpenFeature tracking → ``ExperimentationClient.track`` (fans out to the user's
        cached assignments and evaluated flags; never raises)."""
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
        return self._resolve(flag_key, default_value, evaluation_context, _pick_bool)

    def resolve_string_details(
        self,
        flag_key: str,
        default_value: str,
        evaluation_context: Optional[EvaluationContext] = None,
    ) -> FlagResolutionDetails[str]:
        return self._resolve(flag_key, default_value, evaluation_context, _pick_str)

    def resolve_integer_details(
        self,
        flag_key: str,
        default_value: int,
        evaluation_context: Optional[EvaluationContext] = None,
    ) -> FlagResolutionDetails[int]:
        return self._resolve(flag_key, default_value, evaluation_context, _pick_int)

    def resolve_float_details(
        self,
        flag_key: str,
        default_value: float,
        evaluation_context: Optional[EvaluationContext] = None,
    ) -> FlagResolutionDetails[float]:
        return self._resolve(flag_key, default_value, evaluation_context, _pick_float)

    def resolve_object_details(
        self,
        flag_key: str,
        default_value: Union[dict, list],
        evaluation_context: Optional[EvaluationContext] = None,
    ) -> FlagResolutionDetails[Union[dict, list]]:
        return self._resolve(flag_key, default_value, evaluation_context, _pick_object)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _resolve(
        self,
        flag_key: str,
        default_value: Any,
        evaluation_context: Optional[EvaluationContext],
        pick: _Picker,
    ) -> FlagResolutionDetails[Any]:
        user_id = _targeting_key(evaluation_context)
        if user_id is None:
            return FlagResolutionDetails(
                value=default_value,
                reason=Reason.ERROR,
                error_code=ErrorCode.TARGETING_KEY_MISSING,
                error_message="EvaluationContext.targeting_key (the platform user_id) is required",
            )

        served_from_cache = any(cached.key == flag_key for cached in self._client.cached_flags(user_id))
        try:
            evaluation = self._client.get_feature_flag(flag_key, user_id)
        except ExperimentationError as exc:
            code = ErrorCode.FLAG_NOT_FOUND if exc.status == 404 else ErrorCode.GENERAL
            logger.warning("Flag %r could not be evaluated for %r: %s", flag_key, user_id, exc)
            return FlagResolutionDetails(
                value=default_value,
                reason=Reason.ERROR,
                error_code=code,
                error_message=str(exc),
            )

        variant = _variant_label(evaluation.config)
        value, found = pick(evaluation)
        if not found:
            return FlagResolutionDetails(
                value=default_value,
                reason=Reason.DEFAULT if evaluation.enabled else Reason.DISABLED,
                variant=variant,
            )

        if served_from_cache:
            reason = Reason.CACHED
        elif evaluation.enabled:
            reason = Reason.TARGETING_MATCH
        else:
            reason = Reason.DISABLED
        return FlagResolutionDetails(value=value, reason=reason, variant=variant)
