"""Shared by the ground-truth unit gates: the library on ``sys.path``, and a
fake server that answers a plan as the documented hash says it should.

The library (``tests/ground_truth/ground_truth``) is imported by putting
``tests/ground_truth`` on ``sys.path``, as ``backend/tests/unit/docs`` does
for the docs runner, and the tests take its modules (and the docs runner's
``traffic`` and ``oracles``, which they are checked against) from here, so
the path is set before anything imports them. No network, no product code.
"""

from __future__ import annotations

import io
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
LIBRARY_ROOT = REPO_ROOT / "tests" / "ground_truth"
LIBRARY = LIBRARY_ROOT / "ground_truth"
#: The docs runner's root, not the docs site: nothing here reads a page.
DOCS_RUNNER_ROOT = REPO_ROOT / "tests/acceptance/docs"

for _root in (LIBRARY_ROOT, DOCS_RUNNER_ROOT):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

from docs_runner import oracles, traffic
from ground_truth import contract, generator, hashing, ledger, reference

__all__ = [
    "DOCS_RUNNER_ROOT",
    "LIBRARY",
    "LIBRARY_ROOT",
    "REPO_ROOT",
    "contract",
    "generator",
    "hashing",
    "ledger",
    "oracles",
    "records",
    "reference",
    "replay",
    "traffic",
]

#: (op, default answer) -> (status, body); the default is the honest answer.
Answerer = Callable[[generator.Op, tuple[int, Any]], tuple[int, Any]]


def honest_answer(
    op: generator.Op, allocations: list[tuple[str, int]]
) -> tuple[int, Any]:
    """What a correct server answers *op*: O-ASSIGN's variant, every event stored."""
    key = op.body.get("experiment_key")
    if op.kind == "lifecycle":
        return 200, None
    if op.kind == "assign":
        user = op.body["user_id"]
        variant = hashing.variant_for(user, key, allocations)
        return 200, {
            "experiment_key": key,
            "user_id": user,
            "variant_id": f"id-{variant}",
            "variant_name": variant,
            "is_control": variant == allocations[0][0],
            "configuration": {},
            "assigned": True,
            "reason": "assigned",
        }
    if op.kind == "assign_batch":
        return 200, {
            "experiment_key": key,
            "variants": {f"id-{name}": {"name": name} for name, _ in allocations},
            "assignments": [
                {
                    "user_id": u["user_id"],
                    "variant_id": f"id-{hashing.variant_for(u['user_id'], key, allocations)}",
                    "assigned": True,
                    "reason": "assigned",
                }
                for u in op.body["users"]
            ],
        }
    if op.kind == "track":
        return 200, {"duplicate": False}
    events = op.body["events"]
    return 200, {
        "success_count": len(events),
        "failure_count": 0,
        "duplicate_count": 0,
        "errors": None,
    }


def replay(
    plan: generator.Plan,
    *,
    declared: Mapping[str, Any] | None = None,
    answer: Answerer | None = None,
) -> str:
    """The ledger a driver would write sending *plan* to a fake server."""
    stream = io.StringIO()
    writer = ledger.Writer(stream, **plan.start(declared))
    allocations = plan.allocations()
    for op in plan.ops:
        default = honest_answer(op, allocations)
        status, body = default if answer is None else answer(op, default)
        if op.kind == "lifecycle":
            seq = writer.send_action(op.body["action"], op.body["experiment_key"])
        else:
            seq = writer.send(op.route, op.body)
        writer.answer(seq, status, body)
    writer.end()
    return stream.getvalue()


def records(text: str) -> list[dict[str, Any]]:
    return ledger.read(io.StringIO(text))
