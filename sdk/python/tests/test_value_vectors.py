"""The typed getters against ``tests/sdk-contract/value-vectors.json`` (#1810).

For each answer of ``GET /api/v1/feature-flags/evaluate/{key}`` in the file:
what ``get_string_value``, ``get_number_value`` or ``get_json_value`` returns
and what it passes to ``on_error``. The served half is generated from the
server's own answer builder (drift-checked in the backend suite); the
malformed half is answers no server sends.

One test, not one per vector: pytest over an empty parametrize list passes as
"skipped", so the test counts the vectors it answered and checked, and
requires 49.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

from controlvar import ExperimentationClient, ExperimentationError
from controlvar.testing import FakeTransport

VECTORS_PATH = Path(__file__).resolve().parents[3] / "tests" / "sdk-contract" / "value-vectors.json"
#: Pinned: 35 served + 14 malformed (backend/tests/unit/services/test_sdk_value_vectors.py).
EXPECTED = 49
USER_ID = "value-vector-user"


def _load() -> List[Dict[str, Any]]:
    document = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
    return [*document.get("served", []), *document.get("malformed", [])]


def _same(a: Any, b: Any) -> bool:
    """Equal as JSON, type for type: ``1`` is not ``true`` and ``10`` is not ``10.0``."""
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _answer(vector: Dict[str, Any]) -> Tuple[Any, List[Tuple[ExperimentationError, str]]]:
    errors: List[Tuple[ExperimentationError, str]] = []
    transport = FakeTransport().respond(vector["response"]["status"], json=vector["response"]["body"])
    client = ExperimentationClient(
        "http://test",
        "test-api-key",
        transport=transport,
        on_error=lambda error, operation: errors.append((error, operation)),
    )
    getter = {
        "string": client.get_string_value,
        "number": client.get_number_value,
        "json": client.get_json_value,
    }[vector["getter"]]
    return getter("value-vector", USER_ID, default=vector["default"]), errors


def test_the_typed_getters_answer_each_value_vector_as_the_contract_says():
    problems: List[str] = []
    answered = asserted = 0
    for vector in _load():
        name = vector["name"]
        got, errors = _answer(vector)
        answered += 1

        if vector["expect"] == "default":
            if got is not vector["default"]:
                problems.append(f"{name}: got {got!r}, expected the default")
        elif not _same(got, vector["expect"]):
            problems.append(f"{name}: got {got!r}, expected {vector['expect']!r}")
        reports = sum(1 for _, operation in errors if operation == "value")
        if reports != len(errors):
            problems.append(f"{name}: reported as {[operation for _, operation in errors]}")
        if reports != (1 if vector["reported"] else 0):
            problems.append(f"{name}: reported {reports}, expected {1 if vector['reported'] else 0}")
        # A null code is not compared (this SDK gives a 404 or a 503 no code, only a status).
        if vector["error_code"] is not None and (not errors or errors[0][0].code != vector["error_code"]):
            code = errors[0][0].code if errors else None
            problems.append(f"{name}: code {code!r}, expected {vector['error_code']!r}")
        asserted += 1

    assert problems == []
    assert f"answered {answered} of {EXPECTED}, asserted {asserted}" == (
        f"answered {EXPECTED} of {EXPECTED}, asserted {EXPECTED}"
    )
