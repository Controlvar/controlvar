"""Which warehouse connectors a deployment may use.

A connector is **enabled** only once it has passed a check against a real
warehouse account, and enabling one is a one-line change to
:data:`ENABLED_CONNECTORS` in its own pull request, which carries that
evidence.  Until then the connector's code ships but cannot be used: it is
not offered for selection, and creating a connection of that type is refused
with 422 ``connector_disabled``.  There is no operator setting that overrides
this.
"""

from __future__ import annotations

from typing import AbstractSet, Final, Optional

#: Every connector the code knows about, enabled or not.
KNOWN_CONNECTORS: Final = ("bigquery", "snowflake", "athena", "redshift")

#: The connectors a deployment may use; see the module docstring.
#:
#: * ``snowflake`` -- the real-account check passed on 2026-10-04, run
#:   ``wl-snowflake-20261004T225624Z-3787b33c`` (11 checks, all passed).
#: * ``bigquery`` -- the real-account check passed on 2026-10-05, run
#:   ``wl-bigquery-20261005T150837Z-7782e712`` (10 checks, all passed).
#:
#: Amazon Athena stays disabled until after launch.  Amazon Redshift stays
#: disabled until its client exists and its real-account check has passed.
ENABLED_CONNECTORS: Final[frozenset[str]] = frozenset({"snowflake", "bigquery"})

_NAMES: Final = {
    "bigquery": "BigQuery",
    "snowflake": "Snowflake",
    "athena": "Amazon Athena",
    "redshift": "Amazon Redshift",
}


class ConnectorDisabled(ValueError):
    """A connection of a type this deployment cannot use (422)."""

    code: Final = "connector_disabled"
    status_code: Final = 422

    def __init__(self, warehouse_type: str) -> None:
        name = _NAMES.get(warehouse_type, "This warehouse")
        self.warehouse_type = warehouse_type if warehouse_type in _NAMES else None
        self.message = f"{name} isn't available on this deployment yet."
        super().__init__(self.message)

    def to_body(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def is_enabled(
    warehouse_type: object, enabled: Optional[AbstractSet[str]] = None
) -> bool:
    """Whether ``warehouse_type`` may be used on this deployment."""
    allowed = ENABLED_CONNECTORS if enabled is None else enabled
    return isinstance(warehouse_type, str) and warehouse_type in allowed


def require_enabled(
    warehouse_type: object, enabled: Optional[AbstractSet[str]] = None
) -> str:
    """Return ``warehouse_type`` if it is enabled; otherwise raise ``connector_disabled``."""
    if not is_enabled(warehouse_type, enabled):
        raise ConnectorDisabled(
            warehouse_type if isinstance(warehouse_type, str) else ""
        )
    return warehouse_type  # type: ignore[return-value]


__all__ = [
    "ENABLED_CONNECTORS",
    "KNOWN_CONNECTORS",
    "ConnectorDisabled",
    "is_enabled",
    "require_enabled",
]
