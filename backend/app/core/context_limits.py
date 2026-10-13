"""The limits on a targeting context sent by a client.

A targeting context is the user's attributes, sent with a flag evaluation or
an experiment assignment so that targeting rules can be matched against them:
``{"country": "US", "plan": "pro"}``. Every context in the SDKs' shared test
vectors is a few hundred bytes at most and no more than three levels deep.
These routes refuse a context nested more than :data:`MAX_CONTEXT_DEPTH`
levels deep or larger than :data:`MAX_CONTEXT_BYTES` bytes, with 422 and a
fixed ``detail``, before it is matched against any rule:

* ``GET`` and ``POST /api/v1/feature-flags/evaluate/{flag_key}``
* ``GET /api/v1/feature-flags/user/{user_id}``
* ``POST /api/v1/tracking/assign``
* ``POST /api/v1/tracking/assign/batch``, for every user's context before the
  first user is assigned

The checks, in order:

1. On a ``GET``, the ``context`` parameter's text as sent (after URL
   decoding, whitespace included), in UTF-8 bytes, against
   :data:`MAX_CONTEXT_BYTES`, before it is parsed (:func:`check_context_text`).
   A parameter nested too deeply for the JSON parser answers the depth 422
   (the route's parser catches ``RecursionError``).
2. Depth (:func:`nested_deeper_than`): every JSON object and array is a level,
   the context itself the first. ``{"a": 1}`` is 1 level, ``{"a": [1]}`` and
   ``{"a": {"b": 1}}`` are 2. The walk uses an explicit stack, never
   recursion, and stops at the first container past the limit, so it runs
   before anything that would recurse into the context.
3. Size (:func:`compact_size`): the context as compact JSON (no spaces,
   characters not escaped) in UTF-8 bytes.

What the checks cost: on a deep context the walk stops after
:data:`MAX_CONTEXT_DEPTH` levels; on a wide one it visits every object and
array, and the size check serialises the whole context. Both are bounded by
the 5 MiB request body limit, and on a ``POST`` FastAPI has already parsed
the body before the route runs, so a refused ``POST`` has paid for that parse.

The limits are constants, as ``MAX_REQUEST_BODY_SIZE`` is, not settings.
``docs/api/endpoints.md`` and ``docs/api/stability.md`` state them, and a test
ties those numbers to these constants.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import HTTPException, status

#: Largest targeting context accepted, in bytes of compact UTF-8 JSON.
MAX_CONTEXT_BYTES = 8192
#: Deepest nesting of JSON objects and arrays accepted in a targeting
#: context, the context itself counted as level 1.
MAX_CONTEXT_DEPTH = 10

CONTEXT_TOO_LARGE = f"Targeting context larger than {MAX_CONTEXT_BYTES} bytes"
CONTEXT_TOO_DEEP = f"Targeting context nested more than {MAX_CONTEXT_DEPTH} levels deep"

_CONTAINERS = (dict, list, tuple)


def nested_deeper_than(value: Any, limit: int) -> bool:
    """Whether ``value`` nests objects and arrays more than ``limit`` levels.

    ``value`` itself is level 1 when it is an object or an array; a scalar
    has no levels. Iterative: the stack holds containers only, and the walk
    returns as soon as one would sit at level ``limit + 1``.
    """
    if not isinstance(value, _CONTAINERS):
        return False
    if limit < 1:
        return True
    stack = [(value, 1)]
    while stack:
        node, level = stack.pop()
        children = node.values() if isinstance(node, dict) else node
        for child in children:
            if isinstance(child, _CONTAINERS):
                if level >= limit:
                    return True
                stack.append((child, level + 1))
    return False


def compact_size(value: Any) -> int:
    """Bytes of ``value`` as compact JSON in UTF-8.

    Call it only on a value :func:`nested_deeper_than` has passed: ``json``
    recurses into each level. A lone surrogate (JSON ``"\\ud800"``) counts as
    the three bytes it would take, rather than failing to encode.
    """
    text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return len(text.encode("utf-8", "surrogatepass"))


def context_over_limits(context: Any) -> Optional[str]:
    """The 422 ``detail`` for a context over a limit, or ``None``.

    Depth is checked first; a ``None`` context (none sent) is within both.
    """
    if context is None:
        return None
    if nested_deeper_than(context, MAX_CONTEXT_DEPTH):
        return CONTEXT_TOO_DEEP
    if compact_size(context) > MAX_CONTEXT_BYTES:
        return CONTEXT_TOO_LARGE
    return None


def check_context(context: Any, field: str = "") -> None:
    """Answer 422 when ``context`` is over a limit.

    ``field`` names where the context sits in the request (``users[3]``); it
    prefixes the ``detail``. The ``detail`` never carries a value from the
    context.
    """
    problem = context_over_limits(context)
    if problem is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{field}: {problem}" if field else problem,
        )


def check_context_text(text: Optional[str]) -> None:
    """Answer 422 when a ``context`` query parameter is over the size limit.

    ``text`` is the parameter as the route receives it, before it is parsed.
    """
    if text and len(text.encode("utf-8", "surrogatepass")) > MAX_CONTEXT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=CONTEXT_TOO_LARGE,
        )
