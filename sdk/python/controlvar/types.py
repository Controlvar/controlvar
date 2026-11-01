"""Public data types returned by :class:`controlvar.ExperimentationClient`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

__all__ = ["Assignment", "BatchResult", "ExperimentationError", "FlagEvaluation"]


class ExperimentationError(Exception):
    """Raised by ``get_assignment``, ``get_feature_flag``, ``get_all_flags`` and
    ``get_assignments`` when the API call fails, and passed to ``on_error`` for
    the failures the other methods swallow.

    Attributes:
        status: HTTP status code of the failed response, or ``None`` for
            network errors and timeouts.
        body: Raw response body (text) when a response was received.
        code: Set on the errors a typed getter (``get_string_value``,
            ``get_number_value``, ``get_json_value``) makes itself:
            ``"TYPE_MISMATCH"`` when the flag serves another type, and
            ``"INVALID_RESPONSE"`` when its answer carries no value of the type
            it names. ``None`` on every other error; ``status`` describes those.
    """

    def __init__(
        self,
        message: str,
        status: Optional[int] = None,
        body: Optional[str] = None,
        *,
        code: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.body = body
        self.code = code

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


@dataclass(frozen=True)
class Assignment:
    """A user's (sticky) variant assignment, from ``POST /api/v1/tracking/assign``.

    ``assigned`` is ``True`` when the server enrolled the user and ``False`` when
    it did not (global holdout, mutual exclusion group or targeting rules) and
    returned the control variant instead, with no exposure recorded. ``reason``
    says why: ``"assigned"``, ``"holdout"``, ``"mutual_exclusion"`` or
    ``"targeting"``. Both are ``None`` when the server predates the fields; such
    a server cannot tell you, so do not read ``None`` as either answer. For an
    experiment that is not ACTIVE, ``assigned`` is ``False`` and ``reason`` is its
    status (``"draft"``, ``"paused"``, ``"completed"`` or ``"archived"``).
    """

    experiment_key: str
    user_id: str
    variant_id: Optional[str]
    variant_name: str
    is_control: bool
    configuration: Optional[Dict[str, Any]]
    assigned: Optional[bool] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class FlagEvaluation:
    """A server-side flag decision, from ``GET /api/v1/feature-flags/evaluate/{key}``.

    ``reason`` says why the server decided as it did (``"targeting_rule"``,
    ``"rollout"``, ``"inactive"`` or ``"error"``); ``None`` when the server did
    not send one. A local answer carries the reason the server would have given.
    ``source`` is set only with ``evaluation="local"``: ``"local"`` when answered
    in-process from the ruleset, ``"server"`` when the server evaluated it.

    ``value`` is what the flag serves this user: its on value when ``enabled``
    is true, its off value otherwise (a ``str``; an ``int`` or a ``float``; a
    ``dict`` or a ``list``); for a boolean flag, ``enabled``. ``None`` when the
    server did not send one (a server without flag values, or a stored value it
    cannot serve). In local mode a ruleset entry that names no type is a boolean
    flag, so every flag of a server from before flag values is answered with
    ``enabled`` and ``"boolean"``. A JSON value is the object the SDK caches for
    this user and flag: read it, or call ``get_json_value``, which returns a copy.
    ``value_type`` is the flag's type, ``"boolean"``, ``"string"``,
    ``"number"`` or ``"json"``; ``None`` when the server did not send one.

    ``value`` and ``value_type`` come after the older fields, so code that
    builds a ``FlagEvaluation`` by position keeps working.
    """

    key: str
    enabled: bool
    config: Any = None
    reason: Optional[str] = None
    source: Optional[str] = None
    value: Any = None
    value_type: Optional[str] = None


@dataclass
class BatchResult:
    """Aggregated outcome of ``track_batch`` (summed over all 100-event chunks)."""

    success_count: int = 0
    failure_count: int = 0
    errors: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """``True`` when no event failed."""
        return self.failure_count == 0
