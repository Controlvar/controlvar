"""
The one rule for reading an API key's ``scopes`` column.

``APIKey.scopes`` is a comma-separated string (``String(255)``, nullable).
Several writers fill it -- the create endpoint, which normalises, and the demo
and contract seed scripts, which write literals such as ``"read,write"`` -- so
every reader goes through :func:`parse_scopes`:

* split on ``","``;
* strip surrounding whitespace from each entry;
* drop empty entries.

Membership (:func:`has_scope`) is exact and case-sensitive on the parsed
entries: ``"sdk:ruleset"`` is granted by ``"read, sdk:ruleset "`` but not by
``"SDK:RULESET"``, ``"xsdk:ruleset"`` or ``"sdk:ruleset-ro"``. There is no
prefix, wildcard or substring matching.

This module only parses. Whether a route requires a scope is decided where the
route is declared (``backend/app/api/sdk_scope.py`` for ``sdk:ruleset``,
``backend/app/api/read_scope.py`` for ``mcp:read``).
"""

from typing import Iterable, List, Optional

# The two scope names the platform enforces. Any other name is a label.
#
# sdk:ruleset gates the server-side SDK routes (GET /api/v1/sdk/ruleset and
# the two tracking routes in backend/app/api/sdk_scope.py).
SDK_RULESET_SCOPE = "sdk:ruleset"
# mcp:read lets a key read flags, experiments and results on the four read
# operations of backend/app/api/read_scope.py.
MCP_READ_SCOPE = "mcp:read"


def parse_scopes(scopes: Optional[str]) -> List[str]:
    """Return the scope names stored in *scopes*, in order.

    ``None`` and ``""`` give ``[]``. Entries are stripped and empty entries
    are dropped; case is preserved.
    """
    if not scopes:
        return []
    return [entry.strip() for entry in scopes.split(",") if entry.strip()]


def normalise_scope_list(scopes: Iterable[object]) -> List[str]:
    """Strip each entry of a list of scope names and drop the empty ones."""
    return [str(entry).strip() for entry in scopes if str(entry).strip()]


def format_scopes(scopes: Optional[Iterable[object]]) -> Optional[str]:
    """Join scope names into the stored comma-separated form.

    An empty or missing list is stored as ``None`` (no scopes).
    """
    if scopes is None:
        return None
    cleaned = normalise_scope_list(scopes)
    return ",".join(cleaned) if cleaned else None


def has_scope(scopes: Optional[str], name: str) -> bool:
    """Whether the stored *scopes* string grants exactly *name*.

    Exact, case-sensitive membership among the parsed entries.
    """
    return name in parse_scopes(scopes)
