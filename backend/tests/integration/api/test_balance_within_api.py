"""``balance_within`` through the experiment routes (#448).

Driven through the real app with the real local sign-in and API keys; each
request gets its own session on the test database.

* The setting is stored and echoed by create, ``PUT`` on a draft, GET and the
  list; ``null`` clears it on a draft. So is a setting of saved segments.
* Every refusal, verbatim at its place: the setting's own (R1-R5, R8, R9,
  R13, a setting naming nothing, an unknown key), a segment that does not
  exist, is archived or inactive, or has rules that are not valid (on create
  and on a draft's ``PUT``; clone answers 409), and the experiment's (R6 for
  a bandit in either order and at any status, R10 for a split not in steps of
  5, R12 for a split URL experiment, which holds in a core build). On an
  ACTIVE or PAUSED experiment the setting is locked for every role, the
  superuser included (R7), and ``/start`` refuses an experiment stored past
  the checks.
* A clone balances within the same groups, with a block-order key of its own,
  and its groups start from their first place.
* The block-order key is never answered: no body of the experiment, tracking,
  results, export or audit routes contains it; a body that sends it does not
  set it; no tracking body has a seq; create, ``PUT`` and clone each leave a
  64-hex key of their own.
"""

from __future__ import annotations

import copy
import json
import re
import secrets
import uuid
from typing import Any, Dict, Iterator, List, Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token, get_password_hash
from backend.app.main import app
from backend.app.models import experiment as experiment_model
from backend.app.models.experiment import Experiment, ExperimentStatus, ExperimentType
from backend.app.models.segment import Segment, SegmentKind, SegmentStatus
from backend.app.models.user import User, UserRole

pytestmark = pytest.mark.integration

SCHEMA = "test_experimentation"
EXPERIMENTS = "/api/v1/experiments"
ASSIGN = "/api/v1/tracking/assign"
BATCH = "/api/v1/tracking/assign/batch"
KEYS = "/api/v1/api-keys"

TIER = {"attribute": "tier", "values": ["gold", "silver"]}
HEX64 = re.compile(r"[0-9a-f]{64}")

R1 = (
    "List the values of tier to balance within (1 to 20). Users with any other "
    "value are balanced together as other, and users without tier as unknown."
)
R2 = "Balance within at most 20 values of tier, not 23."
R3 = (
    "other and unknown name the groups for any other value and for no value, so "
    "they cannot be listed."
)
R4 = "tier's value new is listed twice."
R5 = (
    "Balance on a top-level key of the user's attributes, such as country; a key "
    "with a dot, such as user.country, is not read."
)
R6 = (
    "A bandit experiment cannot balance its split within groups, because it "
    "moves traffic between variants as results come in. Turn one of them off."
)
R7_ACTIVE = "Cannot update balance_within for experiments in active status"
R7_PAUSED = "Cannot update balance_within for experiments in paused status"
R9 = "web_device is phone, tablet or desktop; tv is not one of them."
R8_BOTH = "Balance within an attribute or within segments, not both."
R8_TOO_MANY = "Balance within at most 10 segments, not 12."
R12 = (
    "Split URL experiments cannot balance within groups: the edge router sends "
    "no user attributes."
)
R13 = (
    "Balance on a top-level key of the user's attributes, made of letters, "
    "digits, _ and - (up to 100 characters), other than user_id and segment."
)
NEITHER = "Balance within an attribute, such as country, or within segments."

#: Segment ids for the refusals the schema makes before any segment is read.
SEG_A = "0a0a0a0a-0000-4000-8000-000000000001"
TWELVE = [f"00000000-0000-4000-8000-{i:012d}" for i in range(12)]


def r10(split: str) -> str:
    return (
        "To balance within groups, every share must be a multiple of 5, such as "
        f"50/50, 20/80, 35/35/30 or 40/30/30; {split} is not."
    )


def r8_unusable(segment_id: str) -> str:
    return f"Segment {segment_id} does not exist or is archived."


def segment_not_usable(segment_id: str) -> str:
    return f"Segment {segment_id} is inactive or its rules are not valid."


def segment_twice(segment_id: str) -> str:
    return f"Segment {segment_id} is listed twice."


def clone_refused(segment_id: str) -> str:
    return (
        f"The experiment cannot be cloned: it balances within segment {segment_id}, "
        "which does not exist, is not active or has rules that are not valid. "
        "Create the experiment again with active segments."
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_sign_in(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


@pytest.fixture
def session_factory(test_db):
    factory = sessionmaker(bind=test_db, autocommit=False, autoflush=False)

    def make():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        return session

    return make


@pytest.fixture
def client(session_factory) -> Iterator[TestClient]:
    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


def _user(db_session, role: UserRole, superuser: bool = False) -> User:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"bw_{role.value}_{suffix}",
        email=f"bw_{role.value}_{suffix}@balance.test",
        full_name="Balance within",
        hashed_password=get_password_hash("Demo1234!"),
        is_active=True,
        is_superuser=superuser,
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _bearer(user: User) -> Dict[str, str]:
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


@pytest.fixture
def developer(db_session) -> Dict[str, str]:
    return _bearer(_user(db_session, UserRole.DEVELOPER))


@pytest.fixture
def superuser(db_session) -> Dict[str, str]:
    return _bearer(_user(db_session, UserRole.ADMIN, superuser=True))


@pytest.fixture
def sdk_key(client, db_session) -> Dict[str, str]:
    """A key valid on /assign and /assign/batch (``sdk:ruleset``, a developer
    owner)."""
    owner = _user(db_session, UserRole.DEVELOPER)
    response = client.post(
        KEYS,
        json={"name": f"bw-{uuid.uuid4().hex[:8]}", "scopes": ["sdk:ruleset"]},
        headers=_bearer(owner),
    )
    assert response.status_code in (200, 201), response.text
    return {"X-API-Key": response.json()["key"]}


def _body(
    balance: Any = TIER,
    allocations=(50, 50),
    **extra: Any,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "name": f"balance within {uuid.uuid4().hex[:8]}",
        "experiment_type": "a_b",
        "variants": [
            {
                "name": f"Variant {i}" if i else "Control",
                "is_control": i == 0,
                "traffic_allocation": share,
            }
            for i, share in enumerate(allocations)
        ],
        "metrics": [
            {
                "name": "Purchase",
                "event_name": "purchase",
                "metric_type": "conversion",
                "is_primary": True,
            }
        ],
    }
    if balance is not ...:
        body["balance_within"] = balance
    body.update(extra)
    return body


def _create(client, headers, **kwargs) -> Dict[str, Any]:
    response = client.post(f"{EXPERIMENTS}/", json=_body(**kwargs), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _stored(session_factory, experiment_id) -> Experiment:
    session = session_factory()
    try:
        experiment = session.get(Experiment, uuid.UUID(str(experiment_id)))
        session.expunge(experiment)
        return experiment
    finally:
        session.close()


def _write(session_factory, experiment_id, **values) -> None:
    session = session_factory()
    try:
        experiment = session.get(Experiment, uuid.UUID(str(experiment_id)))
        for name, value in values.items():
            setattr(experiment, name, value)
        session.commit()
    finally:
        session.close()


def _refusal(response, loc: List[Any], message: str) -> None:
    assert response.status_code == 422, response.text
    found = [(e["loc"], e["msg"]) for e in response.json()["detail"]]
    assert (loc, message) in found, found


# ---------------------------------------------------------------------------
# Stored and echoed
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_create_stores_and_echoes_the_setting(client, developer, session_factory):
    created = _create(client, developer)

    assert created["balance_within"] == TIER
    got = client.get(f"{EXPERIMENTS}/{created['id']}", headers=developer)
    assert got.status_code == 200, got.text
    assert got.json()["balance_within"] == TIER
    stored = _stored(session_factory, created["id"])
    assert stored.balance_within == TIER
    assert HEX64.fullmatch(stored.balance_seed or ""), stored.balance_seed


@pytest.mark.regression
def test_a_closed_key_needs_no_list_and_is_stored_without_one(client, developer):
    created = _create(client, developer, balance={"attribute": "web_device"})
    assert created["balance_within"] == {"attribute": "web_device"}


@pytest.mark.regression
def test_put_on_a_draft_sets_echoes_and_clears_it(client, developer, session_factory):
    created = _create(client, developer, balance=...)
    assert created["balance_within"] is None
    assert _stored(session_factory, created["id"]).balance_seed is None

    response = client.put(
        f"{EXPERIMENTS}/{created['id']}",
        json={"balance_within": {"attribute": "tier", "values": ["high_value", "new"]}},
        headers=developer,
    )
    assert response.status_code == 200, response.text
    assert response.json()["balance_within"] == {
        "attribute": "tier",
        "values": ["high_value", "new"],
    }
    seed = _stored(session_factory, created["id"]).balance_seed
    assert HEX64.fullmatch(seed or ""), "the PUT that set the setting made no key"

    cleared = client.put(
        f"{EXPERIMENTS}/{created['id']}",
        json={"balance_within": None},
        headers=developer,
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["balance_within"] is None
    assert _stored(session_factory, created["id"]).balance_within is None


# ---------------------------------------------------------------------------
# The setting's own refusals (create)
# ---------------------------------------------------------------------------

SETTING_REFUSALS = [
    ("R1", {"attribute": "tier"}, ["body", "balance_within"], R1),
    ("R1-empty", {"attribute": "tier", "values": []}, ["body", "balance_within"], R1),
    (
        "R2",
        {"attribute": "tier", "values": [f"v{i}" for i in range(23)]},
        ["body", "balance_within"],
        R2,
    ),
    (
        "R3-other",
        {"attribute": "tier", "values": ["other"]},
        ["body", "balance_within"],
        R3,
    ),
    (
        "R3-unknown",
        {"attribute": "tier", "values": ["gold", "unknown"]},
        ["body", "balance_within"],
        R3,
    ),
    (
        "R4",
        {"attribute": "tier", "values": ["new", "new"]},
        ["body", "balance_within"],
        R4,
    ),
    (
        "R5",
        {"attribute": "user.country", "values": ["US"]},
        ["body", "balance_within", "attribute"],
        R5,
    ),
    (
        "R9",
        {"attribute": "web_device", "values": ["phone", "tv"]},
        ["body", "balance_within"],
        R9,
    ),
    (
        "R8-both",
        {"attribute": "tier", "values": ["gold"], "segments": [SEG_A]},
        ["body", "balance_within"],
        R8_BOTH,
    ),
    (
        "R8-both-values",
        {"values": ["gold"], "segments": [SEG_A]},
        ["body", "balance_within"],
        R8_BOTH,
    ),
    (
        "R8-too-many",
        {"segments": TWELVE},
        ["body", "balance_within", "segments"],
        R8_TOO_MANY,
    ),
    (
        "R8-not-an-id",
        {"segments": [SEG_A, "gold"]},
        ["body", "balance_within", "segments"],
        r8_unusable("gold"),
    ),
    (
        "R8-not-lowercase",
        {"segments": [SEG_A.upper()]},
        ["body", "balance_within", "segments"],
        r8_unusable(SEG_A.upper()),
    ),
    (
        "segment-twice",
        {"segments": [SEG_A, SEG_A]},
        ["body", "balance_within", "segments"],
        segment_twice(SEG_A),
    ),
    ("R8-neither-empty", {"segments": []}, ["body", "balance_within"], NEITHER),
    ("R8-neither-values", {"values": ["gold"]}, ["body", "balance_within"], NEITHER),
    (
        "R13-user_id",
        {"attribute": "user_id", "values": ["a"]},
        ["body", "balance_within", "attribute"],
        R13,
    ),
    (
        "R13-segment",
        {"attribute": "segment", "values": ["a"]},
        ["body", "balance_within", "attribute"],
        R13,
    ),
    (
        "R13-space",
        {"attribute": "my tier", "values": ["a"]},
        ["body", "balance_within", "attribute"],
        R13,
    ),
    (
        "R13-dash-first",
        {"attribute": "-tier", "values": ["a"]},
        ["body", "balance_within", "attribute"],
        R13,
    ),
    (
        "R13-too-long",
        {"attribute": "t" * 101, "values": ["a"]},
        ["body", "balance_within", "attribute"],
        R13,
    ),
    ("R8-neither", {}, ["body", "balance_within"], NEITHER),
]


@pytest.mark.regression
@pytest.mark.parametrize(
    ("label", "setting", "loc", "message"),
    SETTING_REFUSALS,
    ids=[case[0] for case in SETTING_REFUSALS],
)
def test_the_setting_is_refused_verbatim(
    client, developer, label, setting, loc, message
):
    response = client.post(
        f"{EXPERIMENTS}/", json=_body(balance=setting), headers=developer
    )
    _refusal(response, loc, message)


def test_an_unknown_key_in_the_setting_is_refused(client, developer):
    response = client.post(
        f"{EXPERIMENTS}/",
        json=_body(balance={**TIER, "values_typo": ["x"]}),
        headers=developer,
    )
    assert response.status_code == 422, response.text
    locs = [e["loc"] for e in response.json()["detail"]]
    assert ["body", "balance_within", "values_typo"] in locs, locs


# ---------------------------------------------------------------------------
# Saved segments
# ---------------------------------------------------------------------------

#: A rules segment's stored rules, in the shape the segment routes store.
PREMIUM_RULES = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [
                {"attribute": "tier", "operator": "equals", "value": "premium"}
            ],
        }
    ],
}


@pytest.fixture
def segments(db_session):
    """Make a segment row (active id list unless told otherwise); its id."""
    owner = _user(db_session, UserRole.DEVELOPER)
    made: List[uuid.UUID] = []

    def make(
        status: SegmentStatus = SegmentStatus.ACTIVE,
        kind: str = SegmentKind.ID_LIST.value,
        rules: Optional[Dict[str, Any]] = None,
    ) -> str:
        row = Segment(
            name=f"Balance within {uuid.uuid4().hex[:8]}",
            kind=kind,
            rules=rules,
            status=status,
            owner_id=owner.id,
        )
        db_session.add(row)
        db_session.commit()
        made.append(row.id)
        return str(row.id)

    yield make
    db_session.rollback()
    db_session.query(Segment).filter(Segment.id.in_(made)).delete(False)
    db_session.commit()


def _set_segment_status(db_session, segment_id: str, status: SegmentStatus) -> None:
    """Past the segment routes, which refuse it for a segment in use."""
    db_session.rollback()
    db_session.query(Segment).filter(Segment.id == uuid.UUID(segment_id)).update(
        {"status": status}, synchronize_session=False
    )
    db_session.commit()


@pytest.mark.regression
def test_segments_are_stored_and_echoed(client, developer, segments, session_factory):
    ids = [
        segments(),
        segments(kind=SegmentKind.RULES.value, rules=PREMIUM_RULES),
    ]
    created = _create(client, developer, balance={"segments": ids})

    assert created["balance_within"] == {"segments": ids}
    got = client.get(f"{EXPERIMENTS}/{created['id']}", headers=developer)
    assert got.status_code == 200, got.text
    assert got.json()["balance_within"] == {"segments": ids}
    stored = _stored(session_factory, created["id"])
    assert stored.balance_within == {"segments": ids}
    assert HEX64.fullmatch(stored.balance_seed or ""), stored.balance_seed

    other = [segments()]
    put = client.put(
        f"{EXPERIMENTS}/{created['id']}",
        json={"balance_within": {"segments": other}},
        headers=developer,
    )
    assert put.status_code == 200, put.text
    assert put.json()["balance_within"] == {"segments": other}
    attribute = client.put(
        f"{EXPERIMENTS}/{created['id']}",
        json={"balance_within": TIER},
        headers=developer,
    )
    assert attribute.status_code == 200, attribute.text
    assert attribute.json()["balance_within"] == TIER


#: A rules segment whose stored rules cannot be read.
BROKEN_RULES = {"groups": "not a list"}


@pytest.mark.regression
@pytest.mark.parametrize("state", ["missing", "archived", "inactive", "rules"])
def test_a_segment_that_cannot_be_used_is_refused(
    client, developer, segments, session_factory, state
):
    """On create and on a draft's ``PUT``, at the segments, for the first
    segment listed that cannot be used; nothing is stored."""
    usable = segments()
    if state == "missing":
        bad = str(uuid.uuid4())
        message = r8_unusable(bad)
    elif state == "archived":
        bad = segments(status=SegmentStatus.ARCHIVED)
        message = r8_unusable(bad)
    elif state == "inactive":
        bad = segments(status=SegmentStatus.INACTIVE)
        message = segment_not_usable(bad)
    else:
        bad = segments(kind=SegmentKind.RULES.value, rules=BROKEN_RULES)
        message = segment_not_usable(bad)
    setting = {"segments": [usable, bad]}

    created = client.post(
        f"{EXPERIMENTS}/", json=_body(balance=setting), headers=developer
    )
    _refusal(created, ["body", "balance_within", "segments"], message)

    draft = _create(client, developer, balance=...)
    put = client.put(
        f"{EXPERIMENTS}/{draft['id']}",
        json={"balance_within": setting},
        headers=developer,
    )
    _refusal(put, ["body", "balance_within", "segments"], message)
    assert _stored(session_factory, draft["id"]).balance_within is None


@pytest.mark.regression
def test_a_clone_of_an_experiment_balanced_within_an_archived_segment_is_refused(
    client, developer, segments, db_session
):
    """The clone would balance within the source's segments; one archived
    since (a completed source no longer holds it) answers 409."""
    usable = segments()
    archived = segments()
    source = _create(client, developer, balance={"segments": [usable, archived]})
    _set_segment_status(db_session, archived, SegmentStatus.ARCHIVED)

    response = client.post(f"{EXPERIMENTS}/{source['id']}/clone", headers=developer)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == clone_refused(archived)


# ---------------------------------------------------------------------------
# What a balanced experiment refuses (create)
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    ("allocations", "split"), [((34, 33, 33), "34/33/33"), ((1, 99), "1/99")]
)
def test_a_split_not_in_steps_of_5_is_refused(client, developer, allocations, split):
    response = client.post(
        f"{EXPERIMENTS}/", json=_body(allocations=allocations), headers=developer
    )
    _refusal(response, ["body", "balance_within"], r10(split))


@pytest.mark.parametrize("allocations", [(35, 35, 30), (40, 30, 30), (20, 80)])
def test_a_split_in_steps_of_5_is_accepted(client, developer, allocations):
    _create(client, developer, allocations=allocations)


@pytest.mark.regression
def test_a_bandit_cannot_balance(client, developer):
    response = client.post(
        f"{EXPERIMENTS}/",
        json=_body(optimization_type="thompson_sampling"),
        headers=developer,
    )
    _refusal(response, ["body", "balance_within"], R6)


def _split_url_config(host: str) -> Dict[str, Any]:
    return {
        "variants": [
            {"name": "Control", "url": f"https://{host}/a", "traffic_allocation": 50},
            {"name": "Variant 1", "url": f"https://{host}/b", "traffic_allocation": 50},
        ]
    }


@pytest.mark.regression
def test_a_split_url_experiment_cannot_balance_on_create(client, developer):
    """Refused by the request's validation, before anything asks whether this
    build can route split URL traffic: the same 422 in a core build."""
    host = f"bw{uuid.uuid4().hex[:10]}.example"
    response = client.post(
        f"{EXPERIMENTS}/",
        json=_body(
            experiment_type="split_url", split_url_config=_split_url_config(host)
        ),
        headers=developer,
    )
    _refusal(response, ["body", "balance_within"], R12)


@pytest.mark.regression
def test_a_split_url_experiment_cannot_balance_on_update(
    client, developer, session_factory
):
    created = _create(client, developer, balance=...)
    host = f"bw{uuid.uuid4().hex[:10]}.example"
    _write(
        session_factory,
        created["id"],
        experiment_type=ExperimentType.SPLIT_URL,
        split_url_config=_split_url_config(host),
    )

    response = client.put(
        f"{EXPERIMENTS}/{created['id']}",
        json={"balance_within": TIER},
        headers=developer,
    )

    _refusal(response, ["body", "balance_within"], R12)
    assert _stored(session_factory, created["id"]).balance_within is None


# ---------------------------------------------------------------------------
# Update: judged on the result, in either order
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_the_setting_sent_to_a_bandit_draft_is_refused(
    client, developer, session_factory
):
    created = _create(client, developer, balance=..., optimization_type="ucb1")
    response = client.put(
        f"{EXPERIMENTS}/{created['id']}",
        json={"balance_within": TIER},
        headers=developer,
    )
    _refusal(response, ["body", "balance_within"], R6)
    assert _stored(session_factory, created["id"]).balance_within is None


@pytest.mark.regression
def test_a_bandit_sent_to_a_balanced_draft_is_refused(
    client, developer, session_factory
):
    created = _create(client, developer)
    response = client.put(
        f"{EXPERIMENTS}/{created['id']}",
        json={"optimization_type": "epsilon_greedy"},
        headers=developer,
    )
    _refusal(response, ["body", "balance_within"], R6)
    assert _stored(session_factory, created["id"]).optimization_type == "fixed"


@pytest.mark.regression
def test_a_split_not_in_steps_of_5_sent_to_a_balanced_draft_is_refused(
    client, developer, session_factory
):
    created = _create(client, developer)
    variants = _body(allocations=(34, 33, 33))["variants"]
    response = client.put(
        f"{EXPERIMENTS}/{created['id']}", json={"variants": variants}, headers=developer
    )
    _refusal(response, ["body", "balance_within"], r10("34/33/33"))


@pytest.mark.regression
def test_the_setting_sent_to_a_draft_split_not_in_steps_of_5_is_refused(
    client, developer
):
    created = _create(client, developer, balance=..., allocations=(34, 33, 33))
    response = client.put(
        f"{EXPERIMENTS}/{created['id']}",
        json={"balance_within": TIER},
        headers=developer,
    )
    _refusal(response, ["body", "balance_within"], r10("34/33/33"))


def _started(client, headers, **kwargs) -> Dict[str, Any]:
    created = _create(client, headers, **kwargs)
    started = client.post(f"{EXPERIMENTS}/{created['id']}/start", headers=headers)
    assert started.status_code == 200, started.text
    return started.json()


@pytest.mark.regression
def test_a_bandit_sent_to_an_active_balanced_experiment_is_refused(
    client, developer, superuser, session_factory
):
    """At any status: the superuser may change ``optimization_type`` after
    draft, and still cannot make a balanced experiment a bandit."""
    started = _started(client, developer)
    response = client.put(
        f"{EXPERIMENTS}/{started['id']}",
        json={"optimization_type": "thompson_sampling"},
        headers=superuser,
    )
    _refusal(response, ["body", "balance_within"], R6)
    assert _stored(session_factory, started["id"]).optimization_type == "fixed"


@pytest.mark.regression
@pytest.mark.parametrize("state", ["active", "paused"])
def test_the_setting_is_locked_after_draft_for_every_role(
    client, developer, superuser, session_factory, state
):
    started = _started(client, developer)
    if state == "paused":
        paused = client.post(f"{EXPERIMENTS}/{started['id']}/pause", headers=developer)
        assert paused.status_code == 200, paused.text

    for headers, detail in (
        (developer, f"Cannot update experiments in {state} status"),
        (superuser, R7_ACTIVE if state == "active" else R7_PAUSED),
    ):
        for value in ({"attribute": "tier", "values": ["gold"]}, TIER, None):
            response = client.put(
                f"{EXPERIMENTS}/{started['id']}",
                json={"balance_within": value},
                headers=headers,
            )
            assert response.status_code == 400, response.text
            assert response.json()["detail"] == detail
    assert _stored(session_factory, started["id"]).balance_within == TIER


@pytest.mark.regression
def test_start_refuses_an_experiment_stored_past_the_checks(
    client, developer, session_factory
):
    """Only a write that bypassed create and update stores one; /start says
    what is wrong instead of assigning by blocks of 100."""
    created = _create(client, developer, balance=..., allocations=(34, 33, 33))
    _write(session_factory, created["id"], balance_within=TIER)

    response = client.post(f"{EXPERIMENTS}/{created['id']}/start", headers=developer)

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == r10("34/33/33")
    assert _stored(session_factory, created["id"]).status == ExperimentStatus.DRAFT


# ---------------------------------------------------------------------------
# Clone
# ---------------------------------------------------------------------------


def _assign(client, key, experiment_key, user_id, tier) -> Dict[str, Any]:
    response = client.post(
        ASSIGN,
        json={
            "experiment_key": experiment_key,
            "user_id": user_id,
            "context": {"tier": tier},
        },
        headers=key,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _seqs(session_factory, experiment_id) -> Dict[str, List[int]]:
    session = session_factory()
    try:
        rows = session.execute(
            text(
                "SELECT balance_group, balance_seq FROM assignments "
                "WHERE experiment_id = :id ORDER BY balance_seq"
            ),
            {"id": str(experiment_id)},
        ).all()
    finally:
        session.close()
    found: Dict[str, List[int]] = {}
    for group, seq in rows:
        found.setdefault(group, []).append(seq)
    return found


@pytest.mark.regression
def test_a_clone_balances_within_the_same_groups_and_starts_fresh(
    client, developer, sdk_key, session_factory
):
    source = _started(client, developer)
    for tier in ("gold", "gold", "silver", "bronze", "gold"):
        _assign(client, sdk_key, source["key"], f"src-{uuid.uuid4().hex[:10]}", tier)
    assert _seqs(session_factory, source["id"]) == {
        "gold": [0, 1, 2],
        "silver": [0],
        "other": [0],
    }

    response = client.post(f"{EXPERIMENTS}/{source['id']}/clone", headers=developer)
    assert response.status_code == 201, response.text
    clone = response.json()
    assert clone["balance_within"] == source["balance_within"] == TIER

    source_seed = _stored(session_factory, source["id"]).balance_seed
    clone_seed = _stored(session_factory, clone["id"]).balance_seed
    assert HEX64.fullmatch(clone_seed or ""), clone_seed
    assert clone_seed != source_seed, "seed equals the source's"

    started = client.post(f"{EXPERIMENTS}/{clone['id']}/start", headers=developer)
    assert started.status_code == 200, started.text
    for tier in ("gold", "silver", "bronze"):
        _assign(client, sdk_key, clone["key"], f"cln-{uuid.uuid4().hex[:10]}", tier)
    firsts = {g: seqs[0] for g, seqs in _seqs(session_factory, clone["id"]).items()}
    assert firsts == {"gold": 0, "silver": 0, "other": 0}, f"first seq {firsts} != 0"
    assert _seqs(session_factory, source["id"])["gold"] == [0, 1, 2]


# ---------------------------------------------------------------------------
# The block-order key stays on the server
# ---------------------------------------------------------------------------

KNOWN_SEED = "c0ffee" * 10 + "c0de"


def _keys(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


@pytest.mark.regression
def test_no_route_answers_the_block_order_key(
    client, developer, superuser, sdk_key, session_factory, monkeypatch
):
    """The key is known from the moment the experiment is created (the one
    place that makes keys is made to answer it), so the create's own answer
    and its audit entry are checked too."""
    bodies: Dict[str, bytes] = {}

    def keep(name: str, response) -> None:
        assert response.status_code in (200, 201), (name, response.text)
        bodies[name] = response.content

    with monkeypatch.context() as patched:
        patched.setattr(experiment_model, "new_balance_seed", lambda: KNOWN_SEED)
        created = client.post(f"{EXPERIMENTS}/", json=_body(), headers=developer)
    keep("create", created)
    experiment_id = created.json()["id"]
    assert _stored(session_factory, experiment_id).balance_seed == KNOWN_SEED
    started_response = client.post(
        f"{EXPERIMENTS}/{experiment_id}/start", headers=developer
    )
    keep("start", started_response)
    started = started_response.json()
    user_id = f"seedcheck-{uuid.uuid4().hex[:10]}"
    keep("get", client.get(f"{EXPERIMENTS}/{experiment_id}", headers=developer))
    keep("list", client.get(f"{EXPERIMENTS}/", headers=developer))
    keep(
        "put",
        client.put(
            f"{EXPERIMENTS}/{experiment_id}",
            json={"name": f"renamed {uuid.uuid4().hex[:6]}"},
            headers=superuser,
        ),
    )
    keep(
        "clone", client.post(f"{EXPERIMENTS}/{experiment_id}/clone", headers=developer)
    )
    keep(
        "assign",
        client.post(
            ASSIGN,
            json={
                "experiment_key": started["key"],
                "user_id": user_id,
                "context": {"tier": "gold"},
            },
            headers=sdk_key,
        ),
    )
    keep(
        "assign/batch",
        client.post(
            BATCH,
            json={
                "experiment_key": started["key"],
                "users": [
                    {"user_id": user_id, "context": {"tier": "gold"}},
                    {"user_id": f"{user_id}-b", "context": {"tier": "silver"}},
                ],
            },
            headers=sdk_key,
        ),
    )
    keep(
        "assignments",
        client.get(f"/api/v1/tracking/assignments/{user_id}", headers=sdk_key),
    )
    keep(
        "results",
        client.get(f"{EXPERIMENTS}/{experiment_id}/results", headers=developer),
    )
    keep(
        "results v1", client.get(f"/api/v1/results/{experiment_id}", headers=developer)
    )
    for path in (
        "/api/v1/export/experiments?format=json",
        "/api/v1/export/experiments?format=csv",
        "/api/v1/export/variants?format=json",
        f"/api/v1/export/reports/experiments/{experiment_id}?format=json",
    ):
        keep(path, client.get(path, headers=developer))
    keep(
        "audit history",
        client.get(
            f"/api/v1/audit-logs/entity/experiment/{experiment_id}", headers=superuser
        ),
    )

    leaked = [name for name, body in bodies.items() if KNOWN_SEED.encode() in body]
    assert leaked == [], f"balance_seed value found in {leaked}"
    assert any(b"balance_within" in bodies[name] for name in ("get", "put", "clone"))

    for name in ("assign", "assign/batch", "assignments"):
        keys = set(_keys(json.loads(bodies[name])))
        assert keys.isdisjoint({"balance_seq", "next_seq", "balance_seed"}), (
            name,
            sorted(keys),
        )


@pytest.mark.regression
def test_a_body_that_sends_the_key_does_not_set_it(client, developer, session_factory):
    sent = secrets.token_hex(32)

    created = client.post(
        f"{EXPERIMENTS}/", json=_body(balance_seed=sent), headers=developer
    )
    assert created.status_code == 201, created.text
    created_id = created.json()["id"]
    after_create = _stored(session_factory, created_id).balance_seed
    assert HEX64.fullmatch(after_create or ""), "seed None after create"
    assert after_create != sent

    put = client.put(
        f"{EXPERIMENTS}/{created_id}",
        json={"balance_seed": sent, "description": "sent a key"},
        headers=developer,
    )
    assert put.status_code == 200, put.text
    after_put = _stored(session_factory, created_id).balance_seed
    assert after_put == after_create, "a PUT body changed the stored key"
    assert after_put != sent

    clone = client.post(f"{EXPERIMENTS}/{created_id}/clone", headers=developer)
    assert clone.status_code == 201, clone.text
    clone_seed = _stored(session_factory, clone.json()["id"]).balance_seed
    assert HEX64.fullmatch(clone_seed or "")
    assert clone_seed not in (sent, after_create)


@pytest.mark.regression
def test_create_put_and_clone_each_make_a_key_of_their_own(
    client, developer, session_factory
):
    created = _create(client, developer)
    seeds = {"create": _stored(session_factory, created["id"]).balance_seed}

    draft = _create(client, developer, balance=...)
    put = client.put(
        f"{EXPERIMENTS}/{draft['id']}", json={"balance_within": TIER}, headers=developer
    )
    assert put.status_code == 200, put.text
    seeds["put"] = _stored(session_factory, draft["id"]).balance_seed

    clone = client.post(f"{EXPERIMENTS}/{created['id']}/clone", headers=developer)
    assert clone.status_code == 201, clone.text
    seeds["clone"] = _stored(session_factory, clone.json()["id"]).balance_seed

    assert all(HEX64.fullmatch(seed or "") for seed in seeds.values()), seeds
    assert len(set(seeds.values())) == 3, seeds
