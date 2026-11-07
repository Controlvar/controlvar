"""Warehouse failures as a fixed set of codes.

Every failure a warehouse call can produce is a :class:`WarehouseError`
carrying one :class:`WarehouseErrorCode`.  Its message is chosen from a table
in this file by the code; there is no way to give it free text.  Besides the
code it carries only short, structured fields -- the vendor's own error code,
the HTTP status, a request id -- and each of those is dropped unless it is a
short token of letters, digits and ``_ . : -``.

So a vendor's error *message*, an HTTP body or a driver exception's text has
no path into a response, into a message we raise, or into a log record:

* adapters map the vendor's structured code and the HTTP status to a
  :class:`WarehouseErrorCode` (:func:`error_for_status` is the fallback);
* :func:`sanitised` rebuilds an error outside any ``except`` block, so its
  traceback carries no chained exception;
* :func:`log_warehouse_error` logs the code and the structured fields, never
  ``str()`` of anything that came from upstream.
"""

from __future__ import annotations

import enum
import logging
import re
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)


class WarehouseErrorCode(str, enum.Enum):
    """The fixed set.  A code is stable once shipped; the copy may change."""

    CREDENTIALS_UNAVAILABLE = "credentials_unavailable"
    AWS_IDENTITY_UNAVAILABLE = "aws_identity_unavailable"
    AUTH_FAILED = "auth_failed"
    KEY_REVOKED = "key_revoked"
    PERMISSION_DENIED = "permission_denied"
    OBJECT_NOT_FOUND = "object_not_found"
    NOT_A_SELECT = "not_a_select"
    BYTES_LIMIT = "bytes_limit"
    TIME_LIMIT = "time_limit"
    CANCELLED = "cancelled"
    WORKGROUP_UNSAFE = "workgroup_unsafe"
    TIMEZONE_NOT_UTC = "timezone_not_utc"
    RESULT_INVALID = "result_invalid"
    TOO_MANY_VARIANT_VALUES = "too_many_variant_values"
    JOIN_KEY_MISMATCH = "join_key_mismatch"
    NO_UNITS = "no_units"
    FEWER_THAN_2_UNITS = "fewer_than_2_units"
    ABANDONED = "abandoned"
    WAREHOUSE_BUSY = "warehouse_busy"
    RUN_IN_PROGRESS = "run_in_progress"
    DESTINATION_NOT_ALLOWED = "destination_not_allowed"
    REDIRECT_REFUSED = "redirect_refused"
    UNREACHABLE = "unreachable"
    UNRECOGNISED_WAREHOUSE_ERROR = "unrecognised_warehouse_error"
    INTERNAL = "internal"


_C = WarehouseErrorCode

#: The copy for each code.  ``{warehouse}`` and ``{vendor_code}`` are the only
#: substitutions, and both are sanitised before they get here.
MESSAGES: Mapping[WarehouseErrorCode, str] = {
    _C.CREDENTIALS_UNAVAILABLE: "Stored warehouse credentials can't be used on this deployment.",
    _C.AWS_IDENTITY_UNAVAILABLE: (
        "This deployment has no AWS identity to assume your role from, "
        "so {warehouse} can't be used here."
    ),
    _C.AUTH_FAILED: "{warehouse} did not accept the connection's credentials.",
    _C.KEY_REVOKED: "{warehouse} reports that the connection's key has been revoked or disabled.",
    _C.PERMISSION_DENIED: "The warehouse role does not have permission for this query.",
    _C.OBJECT_NOT_FOUND: "A table, view or column the query names was not found.",
    _C.NOT_A_SELECT: "The statement is not a single SELECT, so it was not run.",
    _C.BYTES_LIMIT: "The query would read more than the connection's byte limit, so it was not run.",
    _C.TIME_LIMIT: "The warehouse did not finish within the time limit.",
    _C.CANCELLED: "The query was cancelled.",
    _C.WORKGROUP_UNSAFE: "The workgroup's settings do not meet the requirements.",
    _C.TIMEZONE_NOT_UTC: "The warehouse session is not in UTC.",
    _C.RESULT_INVALID: "The warehouse returned a result in an unexpected shape.",
    _C.TOO_MANY_VARIANT_VALUES: "The variant column has more distinct values than allowed.",
    _C.JOIN_KEY_MISMATCH: "No metric rows matched an exposed unit.",
    _C.NO_UNITS: "No exposed units were found in the window.",
    _C.FEWER_THAN_2_UNITS: "A variant has fewer than 2 units, so its mean can't be compared.",
    _C.ABANDONED: "The run stopped reporting progress and was marked as failed.",
    _C.WAREHOUSE_BUSY: (
        "Another warehouse analysis is using this deployment's capacity. Try again shortly."
    ),
    _C.RUN_IN_PROGRESS: "A warehouse job for this connection is already running.",
    _C.DESTINATION_NOT_ALLOWED: "The warehouse address is not one this deployment connects to.",
    _C.REDIRECT_REFUSED: "The warehouse answered with a redirect, which is not followed.",
    _C.UNREACHABLE: "The warehouse could not be reached.",
    _C.UNRECOGNISED_WAREHOUSE_ERROR: (
        "{warehouse} returned an error we don't recognise (code {vendor_code}). "
        "See Troubleshooting in the warehouse analysis docs."
    ),
    _C.INTERNAL: "Something went wrong on our side while talking to the warehouse.",
}

#: HTTP status for the codes an admission step or a synchronous call answers
#: with directly.  Everything else is a failed upstream call: 502.
HTTP_STATUS: Mapping[WarehouseErrorCode, int] = {
    _C.WAREHOUSE_BUSY: 429,
    _C.RUN_IN_PROGRESS: 409,
    _C.CREDENTIALS_UNAVAILABLE: 503,
    _C.AWS_IDENTITY_UNAVAILABLE: 503,
    _C.TIME_LIMIT: 504,
    _C.INTERNAL: 500,
}

#: Seconds a 429 ``warehouse_busy`` tells the caller to wait.
RETRY_AFTER_SECONDS = 30

#: A structured field (vendor code, request id, warehouse name) is kept only
#: when it is a short token of these characters; anything else is dropped.
_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,64}")

_WAREHOUSE_NAMES = {
    "bigquery": "BigQuery",
    "snowflake": "Snowflake",
    "athena": "Amazon Athena",
    "redshift": "Amazon Redshift",
}


def _token(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value)
    return text if _TOKEN.fullmatch(text) else None


class WarehouseError(Exception):
    """A warehouse failure, as a code.  It has no free-text message."""

    def __init__(
        self,
        code: WarehouseErrorCode,
        *,
        warehouse: Optional[str] = None,
        vendor_code: Any = None,
        http_status: Any = None,
        request_id: Any = None,
    ) -> None:
        self.code = WarehouseErrorCode(code)
        self.warehouse = warehouse if warehouse in _WAREHOUSE_NAMES else None
        self.vendor_code = _token(vendor_code)
        status = http_status if isinstance(http_status, int) else None
        self.http_status = (
            status if status is not None and 100 <= status <= 599 else None
        )
        self.request_id = _token(request_id)
        super().__init__(self.code.value)

    @property
    def message(self) -> str:
        return MESSAGES[self.code].format(
            warehouse=_WAREHOUSE_NAMES.get(self.warehouse or "", "The warehouse"),
            vendor_code=self.vendor_code or "none",
        )

    @property
    def status_code(self) -> int:
        """The HTTP status a route answers with for this error."""
        return HTTP_STATUS.get(self.code, 502)

    def response_headers(self) -> Dict[str, str]:
        if self.code is WarehouseErrorCode.WAREHOUSE_BUSY:
            return {"Retry-After": str(RETRY_AFTER_SECONDS)}
        return {}

    def to_body(self) -> Dict[str, Any]:
        """The JSON body a route answers with: the code, the copy, the vendor code."""
        body: Dict[str, Any] = {"code": self.code.value, "message": self.message}
        if self.vendor_code is not None:
            body["vendor_code"] = self.vendor_code
        return body

    def log_fields(self) -> Dict[str, Any]:
        return {
            "warehouse_error": self.code.value,
            "warehouse": self.warehouse,
            "vendor_code": self.vendor_code,
            "http_status": self.http_status,
            "request_id": self.request_id,
        }

    def __repr__(self) -> str:
        return f"WarehouseError({self.code.value!r}, vendor_code={self.vendor_code!r})"

    def __reduce__(self) -> Any:  # keep the keyword-only constructor picklable
        return (
            _rebuild,
            (
                self.code.value,
                self.warehouse,
                self.vendor_code,
                self.http_status,
                self.request_id,
            ),
        )


def _rebuild(
    code: str,
    warehouse: Optional[str],
    vendor_code: Optional[str],
    http_status: Optional[int],
    request_id: Optional[str],
) -> WarehouseError:
    return WarehouseError(
        WarehouseErrorCode(code),
        warehouse=warehouse,
        vendor_code=vendor_code,
        http_status=http_status,
        request_id=request_id,
    )


def sanitised(error: WarehouseError) -> WarehouseError:
    """A fresh copy of ``error`` with no traceback and no chained exception.

    An adapter may raise a :class:`WarehouseError` while handling a driver or
    HTTP exception; Python then records that exception as ``__context__``, and
    a formatted traceback prints it -- text included -- even under
    ``raise ... from None``.  Call this outside the ``except`` block that
    caught it, and re-raise the copy.
    """
    return _rebuild(
        error.code.value,
        error.warehouse,
        error.vendor_code,
        error.http_status,
        error.request_id,
    )


def error_for_status(
    status: int,
    *,
    warehouse: Optional[str] = None,
    vendor_code: Any = None,
    request_id: Any = None,
    codes: Optional[Mapping[str, WarehouseErrorCode]] = None,
) -> WarehouseError:
    """Map a vendor's structured code, else the HTTP status, to a coded error.

    ``codes`` maps the vendor's own error codes (Snowflake ``code``, BigQuery
    ``reason``, AWS ``Code``) to ours; an adapter passes its table.  Only the
    status and the code are read -- never a message or a body.
    """
    token = _token(vendor_code)
    if codes and token is not None and token in codes:
        code = codes[token]
    elif status == 401:
        code = WarehouseErrorCode.AUTH_FAILED
    elif status == 403:
        code = WarehouseErrorCode.PERMISSION_DENIED
    elif status == 404:
        code = WarehouseErrorCode.OBJECT_NOT_FOUND
    elif status == 408 or status == 504:
        code = WarehouseErrorCode.TIME_LIMIT
    elif 300 <= status < 400:
        code = WarehouseErrorCode.REDIRECT_REFUSED
    else:
        code = WarehouseErrorCode.UNRECOGNISED_WAREHOUSE_ERROR
    return WarehouseError(
        code,
        warehouse=warehouse,
        vendor_code=token,
        http_status=status,
        request_id=request_id,
    )


def log_warehouse_error(error: WarehouseError, *, event: str) -> None:
    """Log a warehouse failure: our code and the structured fields, nothing else."""
    logger.warning("%s: %s", event, error.code.value, extra=error.log_fields())
