"""The values a journey saves from an API answer, for the steps after it.

An ``api`` step may ``save`` values from its JSON answer, each under a name::

    save:
      experiment-id: {path: items.0.id}
      sdk-key: {path: key, secret: true}

A later step uses a saved value by writing ``{{experiment-id}}`` in an api
step's path, in a string of its body or of its expected JSON, or in a
``goto``: the runner puts the value in before the step runs, as a reader
pastes an id they were shown. A
value saved with ``secret: true`` (an API key the API answered with) is never
put into a path, a body or an address: a later api step or ``traffic`` step
sends it as its ``key``, in the ``X-API-Key`` header, and it is never written
to the run directory (``redaction.py``).

A value saved that is a number (a JSON number, not a boolean) is also kept as
one, so a later step can expect a number to have moved from it by an exact
amount (``expect.delta`` on an api step, ``delta`` on a cell): a count read
before an action, and the same count read after it, one more.

A journey on demo-local may name ShopLab's visitor (``visitor``): the runner
puts that id into the storefront's localStorage, under the key the storefront
keeps its visitor in, before the journey's first page (``storage_state``), so
the storefront's visitor is one the journey chose instead of a random one. The
id is a UUIDv4 (the storefront drops any other id and makes up its own) chosen
offline by ``choose_visitor``: outside the demo's global holdout, whose bucket
is ``holdout_bucket``, and, where the journey needs it, in a chosen arm of an
experiment by the documented assignment hash (``traffic.variant_for``).

Nothing here imports Playwright or the product, so the unit job tests it.
"""

from __future__ import annotations

import hashlib
import re
import struct
import uuid
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

from docs_runner import traffic

#: ``{{name}}``: a saved value's name is a slug, as a step's id is.
PLACEHOLDER = re.compile(r"\{\{([a-z0-9]+(?:-[a-z0-9]+)*)\}\}")


def placeholders(text: str) -> List[str]:
    """The names *text* refers to, in order."""
    return PLACEHOLDER.findall(text)


def placeholders_in(node: Any) -> List[str]:
    """The names every string in a nested body refers to (keys included)."""
    found: List[str] = []
    if isinstance(node, str):
        found.extend(placeholders(node))
    elif isinstance(node, dict):
        for key, value in node.items():
            found.extend(placeholders_in(key))
            found.extend(placeholders_in(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(placeholders_in(value))
    return found


def substitute(text: str, values: Mapping[str, str]) -> str:
    """*text* with each ``{{name}}`` replaced; KeyError naming an unknown one."""

    def value(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name not in values:
            raise KeyError(f"no value saved as {name!r}")
        return values[name]

    return PLACEHOLDER.sub(value, text)


def substitute_in(node: Any, values: Mapping[str, str]) -> Any:
    """A copy of a nested body with every string's placeholders replaced."""
    if isinstance(node, str):
        return substitute(node, values)
    if isinstance(node, dict):
        return {
            substitute_in(key, values): substitute_in(value, values)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [substitute_in(value, values) for value in node]
    return node


def as_text(value: Any) -> str:
    """A saved JSON value as the text put into a path: a string or a number.

    ValueError for anything else (an object, a list, null, a boolean): a
    path or an id is never one of those, so saving one is a mistake to see.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{value!r} is not a string or a number")
    if isinstance(value, (str, int, float)):
        return str(value)
    raise ValueError(f"a {type(value).__name__} is not a string or a number")


def as_number(value: Any) -> Optional[float]:
    """A saved JSON value as a number, or None when it is not one.

    A boolean is not a number here (JSON's ``true`` is not ``1``), and neither
    is a string of digits: a count the API answers as text is not the count.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


# ---------------------------------------------------------------------------
# ShopLab's visitor, seeded before the journey's first page
# ---------------------------------------------------------------------------
#: ShopLab's address as its README gives it; on demo-local the runner seeds the
#: visitor on the address the stack serves for it (``Running.published``).
STOREFRONT = "http://localhost:3200"
#: The localStorage key ShopLab keeps its visitor id under
#: (``demo/shoplab/src/lib/visitor.ts``, ``VISITOR_ID_KEY``).
VISITOR_KEY = "shoplab.visitor_id"
#: A UUIDv4 as ``crypto.randomUUID()`` writes it: lower case. The storefront
#: keeps only a UUIDv4 (``isUuidV4``); anything else it replaces with its own.
UUID_V4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
#: The global holdout the demo seed keeps active
#: (``backend/scripts/seed_streampulse.py``): its salt, fixed so an id stays
#: where it is across re-seeds, and the share of new users it keeps out.
DEMO_HOLDOUT_SALT = "holdout:streampulse-demo-22"
DEMO_HOLDOUT_PERCENTAGE = 2
#: What ``choose_visitor`` names its candidates after:
#: ``MD5("docs-journey-visitor:1")``, ``...:2``, ...
VISITOR_NAME = "docs-journey-visitor"
#: How many candidates ``choose_visitor`` tries before giving up.
VISITOR_CANDIDATES = 1000


def is_uuid_v4(value: Any) -> bool:
    return isinstance(value, str) and UUID_V4.fullmatch(value) is not None


def holdout_bucket(user_id: str, salt: str) -> int:
    """The user's global-holdout bucket, 0-99, under the holdout's *salt*.

    The first four bytes of ``MD5("{user_id}:{salt}")`` read as a
    little-endian unsigned integer, modulo 100: the bucket the API answers for
    ``GET /api/v1/holdout/check/{user_id}`` (``docs/api/mutual-exclusion-groups.md``).
    A new user is held out when it is below the holdout's percentage. The
    StreamPulse README gives one: ``sp-holdout-15`` is bucket 1 under the
    demo holdout's salt.
    """
    digest = hashlib.md5(
        f"{user_id}:{salt}".encode("utf-8"), usedforsecurity=False
    ).digest()
    (number,) = struct.unpack_from("<I", digest[:4])
    return number % 100


def in_demo_holdout(user_id: str) -> bool:
    """True when the demo's global holdout keeps *user_id* out of every experiment."""
    return holdout_bucket(user_id, DEMO_HOLDOUT_SALT) < DEMO_HOLDOUT_PERCENTAGE


def visitor_candidate(number: int, name: str = VISITOR_NAME) -> str:
    """The *number*-th candidate visitor: ``MD5("{name}:{number}")`` as a UUIDv4.

    The digest's 16 bytes with the version and variant bits set as RFC 4122
    says (``uuid.UUID(..., version=4)``), so the same number always gives the
    same id, and the id is one the storefront keeps.
    """
    digest = hashlib.md5(
        f"{name}:{number}".encode("utf-8"), usedforsecurity=False
    ).digest()
    return str(uuid.UUID(bytes=digest, version=4))


#: For ``choose_visitor``: per experiment key, its variants' allocations in the
#: experiment's order and the arm the visitor must be in.
Arms = Mapping[str, Tuple[Sequence[Tuple[str, int]], str]]


def choose_visitor(arms: Optional[Arms] = None, name: str = VISITOR_NAME) -> str:
    """The first candidate visitor outside the demo's global holdout and in each
    experiment's chosen arm (``arms``), by the documented hashes.

    Run offline: a journey's ``visitor`` is its answer, written in the file, and
    a unit test pins it. ValueError when ``VISITOR_CANDIDATES`` are not enough.
    """
    for number in range(1, VISITOR_CANDIDATES + 1):
        candidate = visitor_candidate(number, name)
        if in_demo_holdout(candidate):
            continue
        if all(
            traffic.variant_for(candidate, key, allocations) == arm
            for key, (allocations, arm) in (arms or {}).items()
        ):
            return candidate
    raise ValueError(
        f"none of {VISITOR_CANDIDATES} candidates is outside the holdout and in"
        f" the arms asked for ({dict(arms or {})})"
    )


def storage_state(served: str, visitor: str) -> Dict[str, Any]:
    """The browser's storage before the journey's first page: *visitor* as
    ShopLab's, on the origin of *served* (the address the stack serves the
    storefront on), as Playwright's ``storage_state`` takes it."""
    parsed = urlparse(served)
    return {
        "cookies": [],
        "origins": [
            {
                "origin": f"{parsed.scheme}://{parsed.netloc}",
                "localStorage": [{"name": VISITOR_KEY, "value": visitor}],
            }
        ],
    }
