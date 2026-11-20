"""The erase never sends a statement timeout of 0 or below (#2042).

``statement_timeout = 0`` turns the timeout off and a negative value is an
error, so every value sent is clamped to ``[1, cap]``: ``CHUNK_TIMEOUT_MS`` in
a chunk, ``FINAL_RESERVE_S`` in the final transaction. The chunk phase stops
when fewer than ``MIN_CHUNK_MS`` are left. A fake session records each
``statement_timeout`` the erase sets; the module clock (``_clock``, never
``time.monotonic``) is driven to the budget's edge. No database.
"""

from __future__ import annotations

import json
import re
import uuid
from types import SimpleNamespace
from typing import List

import pytest
from sqlalchemy.dialects import postgresql

from backend.app.services import app_user_erasure

pytestmark = [pytest.mark.unit, pytest.mark.regression]

TIMEOUT = re.compile(r"set_config\('statement_timeout', '([^']*)'")


class _Result:
    rowcount = 0

    def first(self):
        return None


class FakeSession:
    """Records every ``statement_timeout`` set and every entry added."""

    def __init__(self) -> None:
        self.timeouts: List[str] = []
        self.added: list = []

    def add(self, entry) -> None:
        self.added.append(entry)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def execute(self, statement, *args, **kwargs):
        sql = str(
            statement.compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
        self.timeouts.extend(TIMEOUT.findall(sql))
        return _Result()


ACTOR = SimpleNamespace(id=uuid.uuid4(), email="su@example.test", username=None)


def _erase(monkeypatch, ticks: List[float]) -> FakeSession:
    clock = iter(ticks)
    monkeypatch.setattr(app_user_erasure, "_clock", lambda: next(clock))
    session = FakeSession()
    result = app_user_erasure.erase_app_user(session, "erase-needle-budget", ACTOR)
    assert result.complete is False
    final = json.loads(session.added[-1].new_value)
    assert final["complete"] is False
    return session


#: Times exact in binary, so no rounding moves a value across a boundary.
START = 1024.0
EDGE = START + app_user_erasure.REQUEST_BUDGET_S - app_user_erasure.FINAL_RESERVE_S
DEADLINE = START + app_user_erasure.REQUEST_BUDGET_S


def test_a_chunk_at_the_budget_edge_and_an_overrun_final(monkeypatch):
    """62.5 ms left: the chunk gets 62. Then 31.25: the phase stops. The
    final transaction starts 0.5 s past the deadline: it gets 1, never -500."""
    session = _erase(
        monkeypatch, [START, EDGE - 0.0625, EDGE - 0.03125, DEADLINE + 0.5]
    )
    assert session.timeouts == ["62", "1"]


def test_a_final_transaction_exactly_at_the_deadline_gets_one(monkeypatch):
    session = _erase(monkeypatch, [START, EDGE - 0.125, EDGE - 0.015625, DEADLINE])
    assert session.timeouts == ["125", "1"]


def test_the_documented_constants():
    """The page says one call works for at most 20 seconds."""
    assert app_user_erasure.CHUNK_ROWS == 1000
    assert app_user_erasure.CHUNK_TIMEOUT_MS == 2000
    assert app_user_erasure.REQUEST_BUDGET_S == 20
    assert app_user_erasure.FINAL_RESERVE_S == 2
    assert app_user_erasure.MIN_CHUNK_MS == 50


@pytest.mark.parametrize(
    "ms, cap, sent",
    [
        (-500.0, 2000, 1),
        (0.0, 2000, 1),
        (0.7, 2000, 1),
        (50.5, 2000, 50),
        (2000.0, 2000, 2000),
        (18000.0, 2000, 2000),
    ],
)
def test_every_value_is_clamped(ms, cap, sent):
    assert app_user_erasure._clamped(ms, cap) == sent
