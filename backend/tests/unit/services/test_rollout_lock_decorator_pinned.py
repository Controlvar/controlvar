"""Every method that locks a schedule ends its transaction on every path.

``_lock_schedule_for_change`` takes the flag and schedule locks. A method that
calls it and returns or raises without ending the transaction leaves the locks
held by an idle session, and the rollout tick waits on them (#1604 follow-up).
``_ends_transaction_unless_done`` is what ends it, so it must be on every such
method and it must roll back on a ``None`` return as well as on a raise.
"""

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.app.services import rollout_service
from backend.app.services.rollout_service import _ends_transaction_unless_done

LOCK_HELPER = "_lock_schedule_for_change"
DECORATOR = "_ends_transaction_unless_done"

EXPECTED_LOCKING_METHODS = {
    "update_rollout_schedule",
    "add_rollout_stage",
    "update_rollout_stage",
    "activate_rollout_schedule",
    "manually_advance_stage",
}


def _calls_lock_helper(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            )
            if name == LOCK_HELPER:
                return True
    return False


def _decorator_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names = set()
    for dec in fn.decorator_list:
        if isinstance(dec, ast.Name):
            names.add(dec.id)
        elif isinstance(dec, ast.Attribute):
            names.add(dec.attr)
    return names


@pytest.mark.regression
def test_every_method_that_locks_a_schedule_carries_the_decorator():
    tree = ast.parse(Path(rollout_service.__file__).read_text())
    locking = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name != LOCK_HELPER
        ):
            if _calls_lock_helper(node):
                locking[node.name] = node

    # A renamed lock helper makes this empty or different: fail loudly.
    assert locking, f"no function calls {LOCK_HELPER}; was it renamed?"
    assert set(locking) == EXPECTED_LOCKING_METHODS

    missing = sorted(
        n for n, fn in locking.items() if DECORATOR not in _decorator_names(fn)
    )
    assert not missing, f"{missing} take the schedule lock without {DECORATOR}"


@pytest.mark.regression
def test_decorator_rolls_back_once_on_a_none_return():
    db = MagicMock()

    @_ends_transaction_unless_done
    def nothing_to_change(db):
        return None

    assert nothing_to_change(db) is None
    db.rollback.assert_called_once_with()


@pytest.mark.regression
def test_decorator_rolls_back_once_on_a_raise():
    db = MagicMock()

    @_ends_transaction_unless_done
    def refuses(db):
        raise ValueError("refused")

    with pytest.raises(ValueError):
        refuses(db)
    db.rollback.assert_called_once_with()


@pytest.mark.regression
def test_decorator_leaves_a_successful_result_alone():
    db = MagicMock()

    @_ends_transaction_unless_done
    def commits(db):
        return "done"

    assert commits(db) == "done"
    db.rollback.assert_not_called()
