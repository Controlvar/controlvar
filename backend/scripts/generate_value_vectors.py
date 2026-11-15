"""
Generate ``tests/sdk-contract/value-vectors.json``: how an SDK's typed getter
(JavaScript ``getStringValue``/``getNumberValue``/``getJsonValue``, Python
``get_string_value``/``get_number_value``/``get_json_value``) answers each
answer of ``GET /api/v1/feature-flags/evaluate/{key}`` (#1810).

Each vector is one answer and one getter: ``name``, ``response`` (``status``
and ``body``), ``getter`` (``string``, ``number`` or ``json``), ``default``
(what the caller passes), ``expect`` (``"default"``, or the value the getter
returns: compared by identity for the default, as JSON otherwise),
``error_code`` (``TYPE_MISMATCH``, ``INVALID_RESPONSE``, or ``null`` for not
compared) and ``reported`` (one ``onError(err, 'value')`` call when true, none
when false).

The contract, in order, for a getter of type X:

1. no ``value``, or ``value`` null: the default, ``INVALID_RESPONSE``;
2. the served type is ``value_type`` when it is a string, otherwise the
   value's own JSON type; another type than X: the default,
   ``TYPE_MISMATCH``;
3. a value that is not of X (a string; a number that is not a boolean, is
   finite and within +/-(2**53 - 1); a JSON object or array): the default,
   ``INVALID_RESPONSE``;
4. otherwise the value. A request that fails, or a 404, is the default,
   reported; its code is not compared (``null``), because each SDK names
   transport failures its own way.

Two halves:

* ``served`` (35): what this server answers. Each body is built from an
  in-memory flag by the route's own code: the flag service's
  ``evaluate_flag_detailed`` and the route's ``evaluation_answer`` (32), or
  the route's 404 for an unknown key (3). ``expect``, ``error_code`` and
  ``reported`` are NOT read from those bodies: they come from the case table
  below (each flag's stored values and the getter's type), so a change in
  what the route serves shows up as a body that no longer carries what its
  case expects (``test_sdk_value_vectors.py``) as well as a diff.
* ``malformed`` (14): answers no server sends, written by hand in
  :data:`MALFORMED`: older servers, impossible combinations, a 503.

Nothing here imports an SDK: the expectations are the contract's, not any
SDK's answers.

Usage (from the repository root, venv active):

    python -m backend.scripts.generate_value_vectors          # rewrite the file
    python -m backend.scripts.generate_value_vectors --check  # exit 1 on drift

``backend/tests/unit/services/test_sdk_value_vectors.py`` runs the same
comparison as ``--check`` in every backend job (no database, no ``.git``).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]

if __name__ == "__main__":  # pragma: no cover - run as a script
    # The route module needs settings; generate under the test settings, as
    # the drift test does. (Imported by the test suite, this changes nothing.)
    os.environ.setdefault("APP_ENV", "test")
    os.environ.setdefault("TESTING", "true")
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

from fastapi import HTTPException

from backend.app.api.v1.endpoints import feature_flags as route
from backend.app.core.flag_values import BOOLEAN, JSON, NUMBER, STRING
from backend.app.models import register_core_models
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.services.feature_flag_service import FeatureFlagService
from backend.app.services.metrics_service import MetricsService

VECTORS_PATH = REPO_ROOT / "tests" / "sdk-contract" / "value-vectors.json"

#: The getters, by the type each reads.
GETTERS = (STRING, NUMBER, JSON)
#: What each getter is passed as its default: none is a value any flag here
#: serves, so a default is never mistaken for a value.
DEFAULTS: Dict[str, Any] = {STRING: "fb", NUMBER: -1, JSON: {"fb": True}}
#: The user every answer is evaluated for (each flag is at 0% or 100%).
USER_ID = "value-vector-user"
#: A key no flag has: the route answers 404.
UNKNOWN_KEY = "vector-unknown"

TYPE_MISMATCH = "TYPE_MISMATCH"
INVALID_RESPONSE = "INVALID_RESPONSE"

#: The flags of the served half: what each row stores. ``unservable`` marks a
#: stored value outside the value rules (only a direct database write can
#: leave one); the route answers such a flag without ``value``. The string,
#: number and json values are the live contract seed's
#: (backend/scripts/seed_sdk_contract.py VALUE_FLAGS).
FLAGS: Dict[str, Dict[str, Any]] = {
    "vector-string": {
        "value_type": STRING,
        "on_value": "on-copy",
        "off_value": "off-copy",
    },
    "vector-number": {"value_type": NUMBER, "on_value": 10, "off_value": 2.5},
    "vector-json": {
        "value_type": JSON,
        "on_value": {"color": "blue"},
        "off_value": ["fallback"],
    },
    "vector-boolean": {"value_type": BOOLEAN},
    # Falsy values are values, never the default.
    "vector-empty-string": {
        "value_type": STRING,
        "on_value": "",
        "off_value": "off-copy",
    },
    "vector-zero": {"value_type": NUMBER, "on_value": 0, "off_value": 2.5},
    "vector-empty-json": {"value_type": JSON, "on_value": {}, "off_value": []},
    # Not ACTIVE, at 100%: the user gets the off value, reason "inactive".
    "vector-inactive": {
        "value_type": STRING,
        "on_value": "on-copy",
        "off_value": "off-copy",
        "status": "INACTIVE",
    },
    "vector-unservable-string": {
        "value_type": STRING,
        "on_value": 7,
        "off_value": "off-copy",
        "unservable": True,
    },
    "vector-unservable-number": {
        "value_type": NUMBER,
        "on_value": 2**53,
        "off_value": 2.5,
        "unservable": True,
    },
    "vector-unservable-json": {
        "value_type": JSON,
        "on_value": 7,
        "off_value": ["fallback"],
        "unservable": True,
    },
}

#: The served half's cases: (flag key, on or off, getter). "on" is the flag
#: ACTIVE at 100%, "off" ACTIVE at 0%; ``vector-inactive`` is off however it
#: is rolled out. ``UNKNOWN_KEY`` is the route's 404.
SERVED_CASES: List[Tuple[str, str, str]] = [
    # Each value type, on and off, read by every getter: 6 values, 12 TYPE_MISMATCH.
    *(
        (key, state, getter)
        for key in ("vector-string", "vector-number", "vector-json")
        for state in ("on", "off")
        for getter in GETTERS
    ),
    # A boolean flag is no getter's type: 6 TYPE_MISMATCH.
    *(
        ("vector-boolean", state, getter)
        for state in ("on", "off")
        for getter in GETTERS
    ),
    ("vector-empty-string", "on", STRING),
    ("vector-zero", "on", NUMBER),
    ("vector-empty-json", "on", JSON),
    ("vector-empty-json", "off", JSON),
    ("vector-inactive", "off", STRING),
    ("vector-unservable-string", "on", STRING),
    ("vector-unservable-number", "on", NUMBER),
    ("vector-unservable-json", "on", JSON),
    *((UNKNOWN_KEY, "unknown", getter) for getter in GETTERS),
]


def _answer(key: str, **fields: Any) -> Dict[str, Any]:
    """An evaluate body as the route orders it, *fields* in place of value
    and value_type."""
    return {"key": key, "enabled": True, **fields, "config": None, "reason": "rollout"}


#: The malformed half: (name, status, body, getter, expect, error_code).
#: ``reported`` is ``expect == "default"``.
MALFORMED: List[Tuple[str, int, Dict[str, Any], str, Any, Optional[str]]] = [
    (
        "no value_type, the string value read as string",
        200,
        _answer("vector-malformed", value="x"),
        STRING,
        "x",
        None,
    ),
    (
        "a number above 2^53-1 read as number",
        200,
        _answer("vector-malformed", value=9007199254740993, value_type=NUMBER),
        NUMBER,
        "default",
        INVALID_RESPONSE,
    ),
    (
        "value_type json with the number 7 read as json",
        200,
        _answer("vector-malformed", value=7, value_type=JSON),
        JSON,
        "default",
        INVALID_RESPONSE,
    ),
    (
        "no value_type, the number 7 read as string",
        200,
        _answer("vector-malformed", value=7),
        STRING,
        "default",
        TYPE_MISMATCH,
    ),
    (
        'value_type number with the string "7" read as number',
        200,
        _answer("vector-malformed", value="7", value_type=NUMBER),
        NUMBER,
        "default",
        INVALID_RESPONSE,
    ),
    (
        "value_type number with true read as number",
        200,
        _answer("vector-malformed", value=True, value_type=NUMBER),
        NUMBER,
        "default",
        INVALID_RESPONSE,
    ),
    (
        'value_type json with the string "x" read as json',
        200,
        _answer("vector-malformed", value="x", value_type=JSON),
        JSON,
        "default",
        INVALID_RESPONSE,
    ),
    (
        "value_type string with value null read as string",
        200,
        _answer("vector-malformed", value=None, value_type=STRING),
        STRING,
        "default",
        INVALID_RESPONSE,
    ),
    (
        "value_type string with an object read as string",
        200,
        _answer("vector-malformed", value={"a": 1}, value_type=STRING),
        STRING,
        "default",
        INVALID_RESPONSE,
    ),
    (
        'an unknown value_type "uuid" read as string',
        200,
        _answer("vector-malformed", value="x", value_type="uuid"),
        STRING,
        "default",
        TYPE_MISMATCH,
    ),
    *(
        (
            f"a server from before flag values (neither field) read as {getter}",
            200,
            _answer("vector-malformed"),
            getter,
            "default",
            INVALID_RESPONSE,
        )
        for getter in GETTERS
    ),
    (
        "HTTP 503 read as string",
        503,
        {"detail": "Service Unavailable"},
        STRING,
        "default",
        None,
    ),
]


class _NoFlags:
    """A session in which no flag exists: the route's lookup of an unknown key."""

    def query(self, *entities: Any) -> "_NoFlags":
        return self

    def filter(self, *criteria: Any) -> "_NoFlags":
        return self

    def first(self) -> None:
        return None


@contextlib.contextmanager
def _quiet_logs() -> Iterator[None]:
    """Silence the server's logging: the unservable flags provoke its notes."""
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous)


def stored_flag(key: str, state: str) -> FeatureFlag:
    """A transient ``FeatureFlag`` carrying what the row would: a boolean flag
    stores no values (both columns NULL)."""
    spec = FLAGS[key]
    return FeatureFlag(
        key=key,
        name=key,
        status=FeatureFlagStatus[spec.get("status", "ACTIVE")],
        rollout_percentage=100 if state == "on" or "status" in spec else 0,
        targeting_rules=None,
        value_type=spec["value_type"],
        on_value=spec.get("on_value"),
        off_value=spec.get("off_value"),
    )


def server_response(key: str, state: str) -> Dict[str, Any]:
    """``{status, body}``: what the evaluate route answers for this case."""
    if key == UNKNOWN_KEY:
        try:
            route._evaluate_flag_by_key(_NoFlags(), key, USER_ID, None)
        except HTTPException as exc:
            # FastAPI's answer to an HTTPException (the app adds no handler for it).
            return {"status": exc.status_code, "body": {"detail": exc.detail}}
        raise AssertionError(f"{key} was found")
    flag = stored_flag(key, state)
    with (
        mock.patch.object(MetricsService, "record_flag_evaluation"),
        mock.patch.object(MetricsService, "log_error"),
    ):
        evaluation = FeatureFlagService(db=None).evaluate_flag_detailed(flag, USER_ID)
    return {"status": 200, "body": route.evaluation_answer(key, flag, evaluation)}


def expectation(key: str, state: str, getter: str) -> Tuple[Any, Optional[str]]:
    """``(expect, error_code)`` from the case table, never from a body: an
    unknown key is the default with no code compared; an unservable flag has
    no value (INVALID_RESPONSE); a getter of another type than the flag's is
    TYPE_MISMATCH; otherwise the stored on value (on) or off value (off)."""
    if key == UNKNOWN_KEY:
        return "default", None
    spec = FLAGS[key]
    if spec.get("unservable"):
        return "default", INVALID_RESPONSE
    if spec["value_type"] != getter:
        return "default", TYPE_MISMATCH
    return (spec["on_value"] if state == "on" else spec["off_value"]), None


def vector(
    name: str,
    response: Dict[str, Any],
    getter: str,
    expect: Any,
    error_code: Optional[str],
) -> Dict[str, Any]:
    return {
        "name": name,
        "response": response,
        "getter": getter,
        "default": DEFAULTS[getter],
        "expect": expect,
        "error_code": error_code,
        "reported": expect == "default",
    }


def build_vectors() -> Dict[str, Any]:
    """The whole vectors document."""
    register_core_models()
    served = []
    with _quiet_logs():
        for key, state, getter in SERVED_CASES:
            expect, code = expectation(key, state, getter)
            name = f"{key} ({state}) read as {getter}"
            served.append(
                vector(name, server_response(key, state), getter, expect, code)
            )
    malformed = [
        vector(name, {"status": status, "body": body}, getter, expect, code)
        for name, status, body, getter, expect, code in MALFORMED
    ]
    return {
        "about": (
            "How a typed getter answers each evaluate answer (#1810). Generated by "
            "backend/scripts/generate_value_vectors.py; do not edit by hand. `served`: "
            "bodies built by the server's evaluate route, expectations from the "
            "generator's case table. `malformed`: answers no server sends, written by "
            'hand in the generator. `expect` is "default" or the value; an '
            "`error_code` of null is not compared."
        ),
        "served": served,
        "malformed": malformed,
    }


def ordered_json(value: Any) -> str:
    """Compact, ASCII-only, keys in insertion order (the route's order)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def render(vectors: Dict[str, Any]) -> str:
    """One vector per line, so a drift is a readable diff."""
    lines = ["{", f'"about": {ordered_json(vectors["about"])},']
    halves = [key for key in vectors if key != "about"]
    for index, half in enumerate(halves):
        items = vectors[half]
        lines.append(f"{json.dumps(half)}: [")
        for position, item in enumerate(items):
            tail = "," if position < len(items) - 1 else ""
            lines.append(f"  {ordered_json(item)}{tail}")
        lines.append("]" + ("," if index < len(halves) - 1 else ""))
    lines.append("}")
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n\n")[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare with the committed file and exit 1 if it differs",
    )
    parser.add_argument("--output", type=Path, default=VECTORS_PATH)
    args = parser.parse_args(argv)

    text = render(build_vectors())
    if args.check:
        current = (
            args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        )
        if current != text:
            print(
                f"{args.output} is out of date: run "
                "`python -m backend.scripts.generate_value_vectors`",
                file=sys.stderr,
            )
            return 1
        print(f"{args.output} is current")
        return 0
    args.output.write_text(text, encoding="utf-8")
    document = json.loads(text)
    print(
        f"wrote {args.output}: {len(document['served'])} served, "
        f"{len(document['malformed'])} malformed"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
