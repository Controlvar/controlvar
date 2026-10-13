"""What the default `demo` seed creates on a new database (#1142).

The development stack applies the `demo` seed on its first start: the four
demo accounts, one per role and all with the same password, three experiments
and two feature flags. The seed's own functions run here against a session
that records what they add, so no database is needed.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, List

import pytest

from backend.app.models.experiment import Experiment
from backend.app.models.feature_flag import FeatureFlag
from backend.app.models.user import User, UserRole
from backend.scripts import seed_demo_data as seed

pytestmark = [pytest.mark.unit]


class RecordingSession:
    """Finds nothing, and keeps what it is given, with an id as a flush would."""

    def __init__(self) -> None:
        self.added: List[Any] = []

    def query(self, *args: Any, **kwargs: Any) -> "RecordingSession":
        return self

    def filter(self, *args: Any, **kwargs: Any) -> "RecordingSession":
        return self

    def first(self) -> None:
        return None

    def add(self, obj: Any) -> None:
        if getattr(obj, "id", "no id") is None:
            obj.id = uuid.uuid4()
        self.added.append(obj)

    def add_all(self, objs: List[Any]) -> None:
        for obj in objs:
            self.add(obj)

    def flush(self) -> None:
        pass

    def commit(self) -> None:
        pass


def added(session: RecordingSession, cls: type) -> List[Any]:
    return [obj for obj in session.added if isinstance(obj, cls)]


def test_the_demo_seed_adds_one_account_per_role_with_one_password(monkeypatch):
    monkeypatch.setattr(seed, "get_password_hash", lambda password: f"hash:{password}")
    session = RecordingSession()
    seed.seed_users(session)
    users = {user.email: user for user in added(session, User)}
    assert {email: user.role for email, user in users.items()} == {
        "admin@demo.com": UserRole.ADMIN,
        "dev@demo.com": UserRole.DEVELOPER,
        "analyst@demo.com": UserRole.ANALYST,
        "viewer@demo.com": UserRole.VIEWER,
    }
    assert {user.hashed_password for user in users.values()} == {"hash:Demo1234!"}


def test_the_demo_seed_adds_three_experiments_and_two_flags():
    admin = SimpleNamespace(id=uuid.uuid4())
    session = RecordingSession()
    for make in (
        seed.seed_homepage_hero_experiment,
        seed.seed_checkout_button_experiment,
        seed.seed_recommendation_mab_experiment,
    ):
        make(session, admin)
    seed.seed_feature_flags(session, admin)
    assert len(added(session, Experiment)) == 3
    assert len(added(session, FeatureFlag)) == 2


def test_main_runs_exactly_those_three_experiment_seeds():
    """main() is what the container runs: it calls the three functions above
    and no other experiment seed."""
    called = [
        name
        for name in seed.main.__code__.co_names
        if name.startswith("seed_") and name.endswith("_experiment")
    ]
    assert called == [
        "seed_homepage_hero_experiment",
        "seed_checkout_button_experiment",
        "seed_recommendation_mab_experiment",
    ]
