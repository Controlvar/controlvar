"""The core permission decision, generated against its spec (#891, check #23).

Custom roles and direct grants take effect: ``check_permission``,
``can_act_on_feature_flag`` and ``can_read_all_audit_logs`` admit a user when
the superuser flag, the built-in role, or a grantable pair their custom roles
and grants hold says so. What a user's roles and grants hold comes from the
``rbac.granted_permissions`` capability, which the RBAC module registers; here
a fake provider stands in for it, so these tests need no database and no
``modules`` package and run in the core build too.

The users are real ``User`` rows bound to a ``Session`` without a database
(``make_transient_to_detached`` then ``Session.add``), because the decision
reads grants only for a row bound to a session (check #25 holds the rest).

The oracle is written here, from the rules, and never calls the functions it
checks:

* ``check_permission``: superuser, or ``has_permission(role, ...)`` (False
  for a role of None), or the pair is in the literal :data:`ELEVEN` and in the
  provider's answer;
* ``can_act_on_feature_flag``: superuser; else, when the built-in role holds
  the action, the scope table decides as before; else a grant of the
  ``feature_flag`` action admits ANY flag;
* ``can_read_all_audit_logs``: superuser, ADMIN or ANALYST, or a grant of
  ``audit_log (read)``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Iterable, List, Tuple
from unittest.mock import MagicMock, Mock

import pytest
from sqlalchemy.orm import Session, make_transient_to_detached

from backend.app.core import hooks
from backend.app.core import permissions as core
from backend.app.core.permissions import (
    FEATURE_FLAG_SCOPE,
    GRANTABLE,
    Action,
    ResourceType,
    Scope,
    can_act_on_feature_flag,
    can_read_all_audit_logs,
    check_permission,
    forget_granted_permissions,
    granted_permissions,
    has_permission,
)
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.unit, pytest.mark.regression]

KEY = "rbac.granted_permissions"

#: The eleven pairs, written out: the oracle does not read GRANTABLE.
ELEVEN = frozenset(
    {("experiment", a) for a in ("create", "read", "update", "delete", "list")}
    | {("feature_flag", a) for a in ("create", "read", "update", "delete", "list")}
    | {("audit_log", "read")}
)

#: The vocabulary a role or grant could store before PR 1's 422: 8 x 5.
VOCABULARY_RESOURCES = (
    "experiment",
    "feature_flag",
    "user",
    "role",
    "permission",
    "report",
    "audit_log",
    "export",
)
VOCABULARY_ACTIONS = ("create", "read", "update", "delete", "list")
VOCABULARY = tuple((r, a) for r in VOCABULARY_RESOURCES for a in VOCABULARY_ACTIONS)

#: Spellings that are not a pair, and must never take effect.
NEAR_MISSES = (
    ("Feature_Flag", "update"),
    ("feature_flag", "UPDATE"),
    (" feature_flag", "update"),
    ("feature_flag", "update "),
)

#: The provider's 46 answers: nothing, each pair alone, all 40, 4 near-misses.
STATES: Tuple[Tuple[str, frozenset], ...] = (
    (("empty", frozenset()),)
    + tuple((f"{r}:{a}", frozenset({(r, a)})) for r, a in VOCABULARY)
    + (("all-40", frozenset(VOCABULARY)),)
    + tuple((f"near:{r!r}:{a!r}", frozenset({(r, a)})) for r, a in NEAR_MISSES)
)

ROLES = (UserRole.ADMIN, UserRole.DEVELOPER, UserRole.ANALYST, UserRole.VIEWER, None)
ACTORS = tuple((role, su) for role in ROLES for su in (False, True))
OWNERS = ("own", "other", "null")
FLAG_ACTIONS = (Action.READ, Action.UPDATE, Action.DELETE)


class FakeProvider:
    """``rbac.granted_permissions``: answers a fixed set, counting its calls."""

    def __init__(self, pairs: Iterable[Tuple[str, str]] = ()) -> None:
        self.pairs = list(pairs)
        self.calls: List[Tuple[int, object]] = []

    def __call__(self, session, user_id):
        self.calls.append((id(session), user_id))
        return list(self.pairs)


@pytest.fixture
def provider():
    """Install a fake provider for one test; put back what was there."""
    saved = hooks.get_capability(KEY)
    fake = FakeProvider()
    hooks.register_capability(KEY, fake)
    try:
        yield fake
    finally:
        hooks.register_capability(KEY, saved)


@pytest.fixture
def no_provider():
    """No provider at all, as in the core profile."""
    saved = hooks.get_capability(KEY)
    hooks.register_capability(KEY, None)
    try:
        yield
    finally:
        hooks.register_capability(KEY, saved)


def bound_user(role, superuser: bool = False, session: Session = None) -> User:
    """A ``User`` row bound to a ``Session`` (no database is ever touched)."""
    user = User(
        id=uuid.uuid4(),
        username=f"m_{uuid.uuid4().hex[:8]}",
        email=f"m_{uuid.uuid4().hex[:8]}@matrix.test",
        hashed_password="x",
        is_active=True,
        is_superuser=superuser,
        role=role,
    )
    make_transient_to_detached(user)
    (session or Session()).add(user)
    return user


# ---------------------------------------------------------------------------
# The oracle
# ---------------------------------------------------------------------------
def _base(role, resource: ResourceType, action: Action) -> bool:
    if role is None:
        return False
    return action in core.ROLE_PERMISSIONS.get(role, {}).get(resource, [])


def oracle_check(role, su, state, resource, action) -> bool:
    if su or _base(role, resource, action):
        return True
    pair = (resource.value, action.value)
    return pair in ELEVEN and pair in state


def oracle_can_act(role, su, state, action, owner) -> bool:
    if su:
        return True
    if _base(role, ResourceType.FEATURE_FLAG, action):
        scope = FEATURE_FLAG_SCOPE.get(role, {}).get(action, Scope.OWN)
        return scope is Scope.ANY or owner == "own"
    pair = ("feature_flag", action.value)
    return pair in ELEVEN and pair in state


def oracle_audit(role, su, state) -> bool:
    if su or role in (UserRole.ADMIN, UserRole.ANALYST):
        return True
    return ("audit_log", "read") in state


def _owner_id(user: User, owner: str):
    return {"own": user.id, "other": uuid.uuid4(), "null": None}[owner]


# ---------------------------------------------------------------------------
# Check #23: the matrix
# ---------------------------------------------------------------------------
def test_the_decision_equals_the_spec_for_every_actor_state_and_decision(provider):
    evaluated = 0
    mismatches = []
    calls_per_state = {}
    for role, su in ACTORS:
        for label, state in STATES:
            provider.pairs = sorted(state)
            provider.calls.clear()
            user = bound_user(role, su)
            for resource in ResourceType:
                for action in Action:
                    got = check_permission(user, resource, action)
                    want = oracle_check(role, su, state, resource, action)
                    evaluated += 1
                    if got != want:
                        mismatches.append(
                            (
                                "check",
                                role,
                                su,
                                label,
                                resource.value,
                                action.value,
                                got,
                            )
                        )
            for action in FLAG_ACTIONS:
                for owner in OWNERS:
                    got = can_act_on_feature_flag(user, _owner_id(user, owner), action)
                    want = oracle_can_act(role, su, state, action, owner)
                    evaluated += 1
                    if got != want:
                        mismatches.append(
                            ("can_act", role, su, label, action.value, owner, got)
                        )
            got = can_read_all_audit_logs(user)
            evaluated += 1
            if got != oracle_audit(role, su, state):
                mismatches.append(("audit", role, su, label, got))
            calls_per_state[(role, su, label)] = len(provider.calls)

    assert evaluated == 20_700
    assert mismatches == [], f"{len(mismatches)} mismatches, first: {mismatches[:5]}"

    # Provider calls: none for a superuser or an ADMIN (the role admits every
    # decision), exactly one per session for everyone else (then the cache).
    wrong_calls = {
        key: count
        for key, count in calls_per_state.items()
        if count != (0 if key[1] or key[0] is UserRole.ADMIN else 1)
    }
    assert wrong_calls == {}, (
        f"provider calls {next(iter(wrong_calls.values()))} != expected, "
        f"first: {sorted(wrong_calls.items(), key=str)[:5]}"
    )


def test_a_role_of_none_gets_no_base_permission_but_its_grants(provider):
    """A user whose role is None is refused every base permission, exactly as
    before custom roles took effect, and is admitted to what it is granted."""
    user = bound_user(None)
    assert not any(check_permission(user, r, a) for r in ResourceType for a in Action)
    provider.pairs = [("experiment", "create")]
    other = bound_user(None)
    assert check_permission(other, ResourceType.EXPERIMENT, Action.CREATE)
    assert not check_permission(other, ResourceType.EXPERIMENT, Action.READ)


# ---------------------------------------------------------------------------
# Check #24: the set itself
# ---------------------------------------------------------------------------
def test_grantable_is_exactly_the_eleven_pairs():
    assert set(GRANTABLE) == set(ELEVEN), (
        "Widening GRANTABLE activates, at once, every pair already stored in an "
        "active role or grant. Widening needs its own plan. Set difference: "
        f"added {sorted(set(GRANTABLE) - ELEVEN)}, removed "
        f"{sorted(ELEVEN - set(GRANTABLE))}"
    )
    assert len(GRANTABLE) == 11


def test_a_pair_outside_grantable_is_never_effective(provider):
    """The filter is load-bearing: a provider that yields administration pairs
    (``api_key``, ``role``, ``user``) admits none of them."""
    provider.pairs = [
        ("api_key", "list"),
        ("api_key", "delete"),
        ("role", "create"),
        ("user", "update"),
        ("permission", "create"),
        ("report", "read"),
    ]
    user = bound_user(UserRole.VIEWER)
    assert not check_permission(user, ResourceType.API_KEY, Action.LIST)
    assert not check_permission(user, ResourceType.API_KEY, Action.DELETE)
    assert not check_permission(user, ResourceType.ROLE, Action.CREATE)
    assert not check_permission(user, ResourceType.USER, Action.UPDATE)
    assert not check_permission(user, ResourceType.PERMISSION, Action.CREATE)
    assert granted_permissions(user) == frozenset()
    # Positive control: the same user with one grantable pair is admitted.
    provider.pairs.append(("experiment", "update"))
    other = bound_user(UserRole.VIEWER)
    assert check_permission(other, ResourceType.EXPERIMENT, Action.UPDATE)


# ---------------------------------------------------------------------------
# Check #25: a user not bound to a session fails closed
# ---------------------------------------------------------------------------
def _transient():
    return User(id=uuid.uuid4(), role=UserRole.VIEWER, is_superuser=False)


def _pending():
    user = User(role=UserRole.VIEWER, is_superuser=False)
    Session().add(user)
    return user


def _detached_after_close():
    session = Session()
    user = bound_user(UserRole.VIEWER, session=session)
    session.close()
    return user


UNBOUND = {
    "SimpleNamespace": lambda: SimpleNamespace(
        id=uuid.uuid4(), role=UserRole.VIEWER, is_superuser=False
    ),
    "MagicMock": lambda: MagicMock(role=UserRole.VIEWER, is_superuser=False),
    "MagicMock(spec=User)": lambda: MagicMock(
        spec=User, role=UserRole.VIEWER, is_superuser=False
    ),
    "Mock(spec=User)": lambda: Mock(
        spec=User, role=UserRole.VIEWER, is_superuser=False
    ),
    "transient": _transient,
    "pending": _pending,
    "detached after close": _detached_after_close,
}


@pytest.mark.parametrize("kind", sorted(UNBOUND))
def test_a_user_not_bound_to_a_session_gets_its_role_alone(provider, kind):
    provider.pairs = list(VOCABULARY)
    user = UNBOUND[kind]()
    for resource in ResourceType:
        for action in Action:
            assert check_permission(user, resource, action) == has_permission(
                UserRole.VIEWER, resource, action
            ), (kind, resource, action)
    assert not can_act_on_feature_flag(user, None, Action.UPDATE)
    assert not can_read_all_audit_logs(user)
    assert len(provider.calls) == 0, f"provider calls {len(provider.calls)} != 0"
    # Positive control: a bound VIEWER with the same provider is admitted.
    assert check_permission(
        bound_user(UserRole.VIEWER), ResourceType.FEATURE_FLAG, Action.UPDATE
    )


# ---------------------------------------------------------------------------
# Check #26: without a provider, today's answers exactly
# ---------------------------------------------------------------------------
def test_without_a_provider_every_answer_is_the_built_in_roles(no_provider):
    cases = 0
    for role in ROLES:
        user = bound_user(role)
        for resource in ResourceType:
            for action in Action:
                cases += 1
                expected = role is not None and has_permission(role, resource, action)
                assert check_permission(user, resource, action) == expected, (
                    role,
                    resource,
                    action,
                )
        assert can_read_all_audit_logs(user) == (
            role in (UserRole.ADMIN, UserRole.ANALYST)
        )
    assert cases == 175


# ---------------------------------------------------------------------------
# Check #27: the seam works without the modules package
# ---------------------------------------------------------------------------
def test_a_registered_provider_admits_exactly_the_granted_pair(provider):
    provider.pairs = [("feature_flag", "update")]
    user = bound_user(UserRole.VIEWER)
    assert check_permission(user, ResourceType.FEATURE_FLAG, Action.UPDATE) is True, (
        "expected True for ('feature_flag','update')"
    )
    assert can_act_on_feature_flag(user, uuid.uuid4(), Action.UPDATE)
    assert can_act_on_feature_flag(user, None, Action.UPDATE)
    # Negative controls: the neighbouring pairs are not granted.
    assert not check_permission(user, ResourceType.FEATURE_FLAG, Action.DELETE)
    assert not check_permission(user, ResourceType.EXPERIMENT, Action.UPDATE)
    assert not can_act_on_feature_flag(user, None, Action.DELETE)
    assert not can_read_all_audit_logs(user)
    assert provider.calls == [(provider.calls[0][0], user.id)]


def test_the_provider_is_called_with_the_users_own_session(provider):
    session = Session()
    user = bound_user(UserRole.VIEWER, session=session)
    check_permission(user, ResourceType.EXPERIMENT, Action.CREATE)
    assert provider.calls == [(id(session), user.id)]


# ---------------------------------------------------------------------------
# Check #33: the cache, and forgetting it (hygiene, not the revoke gate)
# ---------------------------------------------------------------------------
def test_a_session_keeps_its_answer_until_it_is_forgotten(provider):
    session = Session()
    provider.pairs = [("experiment", "update")]
    user = bound_user(UserRole.VIEWER, session=session)
    assert check_permission(user, ResourceType.EXPERIMENT, Action.UPDATE)

    provider.pairs = []  # what a revoke in the same session would leave
    assert check_permission(user, ResourceType.EXPERIMENT, Action.UPDATE), (
        "the session keeps its answer for its life"
    )
    assert len(provider.calls) == 1

    forget_granted_permissions(session)
    assert not check_permission(user, ResourceType.EXPERIMENT, Action.UPDATE), (
        "stale cached grant observed after forget_granted_permissions"
    )
    assert len(provider.calls) == 2


def test_forgetting_a_session_without_answers_is_harmless():
    forget_granted_permissions(Session())
    forget_granted_permissions(object())
    forget_granted_permissions(None)


def test_a_provider_error_is_not_swallowed_and_nothing_is_kept(provider):
    def broken(session, user_id):
        raise RuntimeError("database is down")

    hooks.register_capability(KEY, broken)
    session = Session()
    user = bound_user(UserRole.VIEWER, session=session)
    # A check the role admits never asks the provider.
    assert check_permission(user, ResourceType.EXPERIMENT, Action.READ)
    with pytest.raises(RuntimeError):
        check_permission(user, ResourceType.EXPERIMENT, Action.UPDATE)
    assert core.GRANTED_PERMISSIONS_CACHE_KEY not in session.info or (
        str(user.id) not in session.info[core.GRANTED_PERMISSIONS_CACHE_KEY]
    )
