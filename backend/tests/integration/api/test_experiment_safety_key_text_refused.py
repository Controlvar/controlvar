"""Saving an experiment, a flag's safety settings or a new API key with text
holding NUL or an unpaired surrogate answers 422 (#2180).

PostgreSQL refuses U+0000 in ``text``, ``varchar`` and ``jsonb``, and a lone
surrogate cannot be encoded at all, so these saves failed at the database and
answered 500 (an experiment create, for its tags, rules and a variant's
configuration, 400; a surrogate in a key of a configuration was stored as
three replacement characters). Each field is now checked where its request
model declares it, with the helper #961 and #2163 use: 422 at the field, a
fixed message that never repeats the value, nothing saved and no audit entry.

* ``POST /api/v1/experiments/`` and ``PUT /api/v1/experiments/{id}``: ``name``,
  ``description``, ``hypothesis``, ``targeting_rules``, ``tags``; a variant's
  ``name``, ``description`` and ``configuration``; a metric's ``name``,
  ``description``, ``event_name``, ``aggregation_method`` and
  ``event_value_path``; ``variance_reduction_config.covariate_metric_id`` and
  ``split_url_config``'s ``cookie_name`` and ``canonical_url``, each at its
  own field. A split URL variant's ``url`` and ``visual_config.editor_url``
  refuse an unpaired surrogate (which in a query reached the database) with
  the fixed message they give NUL.
* ``POST /api/v1/safety/feature-flags/{id}/config``: ``metrics``, a metric's
  name or anything in its thresholds.
* ``POST /api/v1/api-keys``: ``name``, ``description``, ``scopes``.

The check runs as the body is read: authentication still answers 401 first,
and the safety route's superuser check (a dependency) still answers 403 first;
the 403 and 404 the routes answer themselves now come after it, as every
other body 422 there does. A valid surrogate pair (an emoji) is saved as
before.

The bodies are sent as raw JSON text: a JSON client cannot encode ``\\ud800``,
and sending the escapes is what a client on the wire does.
"""

from __future__ import annotations

import copy
import json
import uuid
from typing import Any, Callable, Dict, Tuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token
from backend.app.main import app
from backend.app.models.api_key import APIKey
from backend.app.models.audit_log import AuditLog
from backend.app.models.experiment import Experiment
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.safety import FeatureFlagSafetyConfig
from backend.app.models.user import User, UserRole
from backend.app.schemas.split_url_config import (
    SPLIT_URL_CONFIG_ERROR,
    URL_HAS_FORBIDDEN_CHARACTER,
)
from backend.app.schemas.visual import EDITOR_URL_REFUSED, VISUAL_CONFIG_ERROR

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

PREFIX = "t2180"
SCHEMA = "test_experimentation"
EXPERIMENTS = "/api/v1/experiments"
API_KEYS = "/api/v1/api-keys"
JSON = {"Content-Type": "application/json"}

#: Marks the refused text, so a test can tell whether an answer repeats it.
MARK = "zq2180mark"
#: An ASCII placeholder, replaced in the raw JSON text by the escape sent.
X = "QQ2180QQ"
#: The characters, as JSON escapes: NUL, and an unpaired high and low surrogate.
CHARACTERS = {"nul": "\\u0000", "surrogate": "\\ud800", "low-surrogate": "\\udc00"}
#: A valid surrogate pair: U+1F600, an emoji.
EMOJI_ESCAPE = "\\ud83d\\ude00"
EMOJI = "\U0001f600"

Place = Tuple[Callable[[Dict[str, Any]], None], Tuple[Any, ...]]


def _experiment_body() -> Dict[str, Any]:
    return {
        "name": f"{PREFIX} experiment {uuid.uuid4().hex[:8]}",
        "description": "a description",
        "hypothesis": "a hypothesis",
        "experiment_type": "a_b",
        "tags": ["ok"],
        "targeting_rules": {
            "logical_operator": "AND",
            "groups": [
                {
                    "logical_operator": "AND",
                    "conditions": [
                        {"attribute": "country", "operator": "equals", "value": "US"}
                    ],
                }
            ],
        },
        "variants": [
            {"name": "Control", "is_control": True, "traffic_allocation": 50},
            {
                "name": "Treatment",
                "description": "blue",
                "is_control": False,
                "traffic_allocation": 50,
                "configuration": {"color": "blue"},
            },
        ],
        "metrics": [
            {
                "name": "Purchase",
                "event_name": "purchase",
                "metric_type": "conversion",
                "is_primary": True,
            },
            {
                "name": "Revenue",
                "description": "revenue per user",
                "event_name": "purchase",
                "metric_type": "revenue",
                "aggregation_method": "sum",
                "event_value_path": "value",
            },
        ],
    }


def _condition(body):
    return body["targeting_rules"]["groups"][0]["conditions"][0]


def _append(path: Tuple[Any, ...]) -> Callable[[Dict[str, Any]], None]:
    """Add the marked character to the text at *path*."""

    def change(body: Dict[str, Any]) -> None:
        target = body
        for step in path[:-1]:
            target = target[step]
        target[path[-1]] = target[path[-1]] + MARK + X

    return change


#: Where in an experiment body the character is: every place #2180 measured
#: answering 500 or 400, and the 422 each now answers at.
EXPERIMENT_PLACES: Dict[str, Place] = {
    "name": (_append(("name",)), ("name",)),
    "description": (_append(("description",)), ("description",)),
    "hypothesis": (_append(("hypothesis",)), ("hypothesis",)),
    "a-tag": (_append(("tags", 0)), ("tags",)),
    "a-targeting-rule-value": (
        lambda b: _condition(b).update(value="US" + MARK + X),
        ("targeting_rules",),
    ),
    "a-variant-name": (_append(("variants", 0, "name")), ("variants", 0, "name")),
    "a-variant-description": (
        _append(("variants", 1, "description")),
        ("variants", 1, "description"),
    ),
    "a-variant-configuration-value": (
        _append(("variants", 1, "configuration", "color")),
        ("variants", 1, "configuration"),
    ),
    "a-variant-configuration-key": (
        lambda b: b["variants"][1].update(configuration={"color" + MARK + X: "blue"}),
        ("variants", 1, "configuration"),
    ),
    "a-variant-configuration-nested-key": (
        lambda b: b["variants"][1].update(
            configuration={"button": {"color" + MARK + X: "blue"}}
        ),
        ("variants", 1, "configuration"),
    ),
    "a-metric-name": (_append(("metrics", 0, "name")), ("metrics", 0, "name")),
    "a-metric-description": (
        _append(("metrics", 1, "description")),
        ("metrics", 1, "description"),
    ),
    "a-metric-event-name": (
        _append(("metrics", 0, "event_name")),
        ("metrics", 0, "event_name"),
    ),
    "a-metric-aggregation-method": (
        _append(("metrics", 1, "aggregation_method")),
        ("metrics", 1, "aggregation_method"),
    ),
    "a-metric-event-value-path": (
        _append(("metrics", 1, "event_value_path")),
        ("metrics", 1, "event_value_path"),
    ),
}

#: Rules another check reads: the character in a condition's attribute or
#: operator.
RULES_READ_BEFORE: Dict[str, Place] = {
    "attribute": (
        lambda b: _condition(b).update(attribute="country" + X),
        ("targeting_rules",),
    ),
    "operator": (
        lambda b: _condition(b).update(operator="equals" + X),
        ("targeting_rules",),
    ),
}

#: An experiment's nested settings, each refused at its own field (the leaf):
#: a check on the setting as a whole would move the 422 another check gives a
#: NUL in a URL. The URLs refuse an unpaired surrogate with their own fixed
#: message, as they refuse NUL; a surrogate in a query reached the database.
SETTINGS = {
    "variance_reduction_config": {"method": "cuped", "covariate_metric_id": "m"},
    "split_url_config": {
        "variants": [
            {
                "name": "Control",
                "url": "https://shop.example.com/a?q=v",
                "traffic_allocation": 50,
            },
            {
                "name": "Treatment",
                "url": "https://shop.example.com/b",
                "traffic_allocation": 50,
            },
        ],
        "cookie_name": "c",
        "canonical_url": "https://shop.example.com/a",
    },
    "visual_config": {
        "version": 1,
        "editor_url": "https://shop.example.com/?q=v",
        "pages": [{"match": "prefix", "url": "https://shop.example.com/"}],
        "goals": [],
    },
}
URL_REFUSED = {
    "type": SPLIT_URL_CONFIG_ERROR,
    "msg": URL_HAS_FORBIDDEN_CHARACTER.format(name="Control"),
}
EDITOR_REFUSED = {"type": VISUAL_CONFIG_ERROR, "msg": EDITOR_URL_REFUSED}

#: (the setting, the path to the text in it, the error's loc, the error when it
#: is not this check's, the characters measured answering 400 or 500).
SETTING_PLACES = {
    "covariate-metric-id": (
        "variance_reduction_config",
        ("covariate_metric_id",),
        ("variance_reduction_config", "covariate_metric_id"),
        None,
        CHARACTERS,
    ),
    "cookie-name": (
        "split_url_config",
        ("cookie_name",),
        ("split_url_config", "cookie_name"),
        None,
        CHARACTERS,
    ),
    "canonical-url": (
        "split_url_config",
        ("canonical_url",),
        ("split_url_config", "canonical_url"),
        None,
        CHARACTERS,
    ),
    "a-url-query": (
        "split_url_config",
        ("variants", 0, "url"),
        ("split_url_config", "variants", 0, "url"),
        URL_REFUSED,
        ("surrogate", "low-surrogate"),
    ),
    "the-editor-url-query": (
        "visual_config",
        ("editor_url",),
        ("visual_config", "editor_url"),
        EDITOR_REFUSED,
        ("surrogate", "low-surrogate"),
    ),
}
SETTING_CASES = [
    (place, character)
    for place, (*_, characters) in SETTING_PLACES.items()
    for character in characters
]


def _setting(place: str, text: str) -> Tuple[str, Any]:
    """The setting of *place*, with *text* added to its text."""
    name, path, *_ = SETTING_PLACES[place]
    value = copy.deepcopy(SETTINGS[name])
    target = value
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = target[path[-1]] + text
    return name, value


SAFETY_PLACES = {
    "a-metric-name": {"error_rate" + MARK + X: {"critical_threshold": 0.1}},
    "a-comparison-type": {
        "error_rate": {
            "critical_threshold": 0.1,
            "comparison_type": "greater_than" + MARK + X,
        }
    },
}

API_KEY_PLACES = {
    "name": ({"name": "key " + MARK + X}, "name"),
    "description": (
        {"name": f"{PREFIX} key", "description": "d " + MARK + X},
        "description",
    ),
    "a-scope": ({"name": f"{PREFIX} key", "scopes": ["flags:" + MARK + X]}, "scopes"),
}


def _refusal(loc: Tuple[Any, ...]) -> Dict[str, Any]:
    field = loc[-1]
    return {
        "type": "value_error",
        "loc": ["body", *loc],
        "msg": f"Value error, {field} must not contain NUL or unpaired surrogate characters",
    }


def _raw(body: Dict[str, Any], escape: str) -> bytes:
    """*body* as JSON text, with the placeholder replaced by *escape*."""
    sent = json.dumps(body)
    assert X in sent
    return sent.replace(X, escape).encode("ascii")


def _assert_refused(response, loc: Tuple[Any, ...], error=None) -> None:
    assert response.status_code == 422, response.text
    assert MARK not in response.text, "the answer repeats the value sent"
    errors = response.json()["detail"]
    expected = _refusal(loc) if error is None else {**error, "loc": ["body", *loc]}
    assert [{k: e[k] for k in ("type", "loc", "msg")} for e in errors] == [expected]


def _with(place: Place) -> Dict[str, Any]:
    body = _experiment_body()
    place[0](body)
    return body


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


def _user(db_session, role: UserRole, superuser: bool = False) -> User:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{PREFIX}_{role.value}_{suffix}",
        email=f"{PREFIX}_{suffix}@savedtext.test",
        full_name="Saved Text User",
        hashed_password="unused: this user signs in by token only",
        is_active=True,
        is_superuser=superuser,
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def developer(db_session) -> User:
    return _user(db_session, UserRole.DEVELOPER)


@pytest.fixture
def viewer(db_session) -> User:
    return _user(db_session, UserRole.VIEWER)


@pytest.fixture
def admin(db_session) -> User:
    """An ADMIN who is not a superuser: the safety route refuses them."""
    return _user(db_session, UserRole.ADMIN)


@pytest.fixture
def superuser(db_session) -> User:
    return _user(db_session, UserRole.ADMIN, superuser=True)


@pytest.fixture
def fresh(test_db):
    """A session of the test's own, opened per read, never the request's."""
    factory = sessionmaker(bind=test_db, expire_on_commit=False)

    def open_session():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        return session

    return open_session


@pytest.fixture
def client(test_db):
    """The real routes and the real authentication; only the database is the
    test one. A 500 is an answer here, not an exception."""
    factory = sessionmaker(bind=test_db, autocommit=False, autoflush=False)

    def override_get_db():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


@pytest.fixture(autouse=True)
def _cleanup(db_session):
    """Leave nothing behind: other suites list experiments, flags and keys."""
    yield
    s = db_session
    s.rollback()
    s.query(Experiment).filter(Experiment.name.like(f"{PREFIX} %")).delete(
        synchronize_session=False
    )
    s.query(FeatureFlag).filter(FeatureFlag.key.like(f"{PREFIX}-%")).delete(
        synchronize_session=False
    )
    user_ids = [
        r.id for r in s.query(User.id).filter(User.username.like(f"{PREFIX}_%"))
    ]
    s.query(Experiment).filter(Experiment.owner_id.in_(user_ids)).delete(
        synchronize_session=False
    )
    s.query(APIKey).filter(APIKey.user_id.in_(user_ids)).delete(
        synchronize_session=False
    )
    s.query(AuditLog).filter(AuditLog.user_id.in_(user_ids)).delete(
        synchronize_session=False
    )
    s.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
    s.commit()


# --- helpers ------------------------------------------------------------------


def _auth(user: User) -> Dict[str, str]:
    return {**JSON, "Authorization": f"Bearer {create_local_access_token(user)}"}


def _totals(fresh) -> Tuple[int, ...]:
    """How many experiments, variants, metrics, API keys, safety
    configurations and audit entries of either kind there are."""
    session = fresh()
    try:
        return tuple(
            session.execute(text(f"select count(*) from {table}")).scalar()
            for table in (
                "experiments",
                "variants",
                "metrics",
                "api_keys",
                "feature_flag_safety_configs",
                "audit_logs",
                "audit_events_v2",
            )
        )
    finally:
        session.close()


def _stored_experiment(fresh, experiment_id) -> Tuple[Any, ...]:
    """The experiment row, its variants and metrics, as stored, and the
    audit totals."""
    session = fresh()
    try:
        row = session.execute(
            text("select row_to_json(e)::text from experiments e where id = :i"),
            {"i": str(experiment_id)},
        ).scalar()
        variants = (
            session.execute(
                text(
                    "select row_to_json(v)::text from variants v "
                    "where experiment_id = :i order by name"
                ),
                {"i": str(experiment_id)},
            )
            .scalars()
            .all()
        )
        metrics = (
            session.execute(
                text(
                    "select row_to_json(m)::text from metrics m "
                    "where experiment_id = :i order by name"
                ),
                {"i": str(experiment_id)},
            )
            .scalars()
            .all()
        )
        assert row is not None and variants and metrics
        return row, variants, metrics
    finally:
        session.close()


def _create_experiment(client, user: User, body=None) -> str:
    created = client.post(
        f"{EXPERIMENTS}/", json=body or _experiment_body(), headers=_auth(user)
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _post_experiment(client, user, body, escape):
    return client.post(
        f"{EXPERIMENTS}/", content=_raw(body, escape), headers=_auth(user)
    )


def _put_experiment(client, user, experiment_id, body, escape):
    return client.put(
        f"{EXPERIMENTS}/{experiment_id}",
        content=_raw(body, escape),
        headers=_auth(user),
    )


def _flag(db_session, owner: User) -> FeatureFlag:
    flag = FeatureFlag(
        key=f"{PREFIX}-{uuid.uuid4().hex[:10]}",
        name=f"{PREFIX} flag",
        status=FeatureFlagStatus.INACTIVE,
        owner_id=owner.id,
    )
    db_session.add(flag)
    db_session.commit()
    return flag


def _safety_path(flag_id) -> str:
    return f"/api/v1/safety/feature-flags/{flag_id}/config"


def _stored_safety(fresh, flag_id):
    session = fresh()
    try:
        return session.execute(
            text(
                "select row_to_json(c)::text from feature_flag_safety_configs c "
                "where feature_flag_id = :i"
            ),
            {"i": str(flag_id)},
        ).scalar()
    finally:
        session.close()


def _post_safety(client, user, flag_id, body, escape):
    return client.post(
        _safety_path(flag_id), content=_raw(body, escape), headers=_auth(user)
    )


def _post_key(client, user, body, escape):
    return client.post(API_KEYS, content=_raw(body, escape), headers=_auth(user))


# --- experiments --------------------------------------------------------------


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("place", EXPERIMENT_PLACES)
def test_an_experiment_create_refuses_the_character(
    client, fresh, developer, place, character
):
    body = _with(EXPERIMENT_PLACES[place])
    before = _totals(fresh)

    response = _post_experiment(client, developer, body, CHARACTERS[character])

    _assert_refused(response, EXPERIMENT_PLACES[place][1])
    assert _totals(fresh) == before


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("place", EXPERIMENT_PLACES)
def test_an_experiment_update_refuses_the_character(
    client, fresh, developer, place, character
):
    experiment_id = _create_experiment(client, developer)
    before = (_stored_experiment(fresh, experiment_id), _totals(fresh))
    loc = EXPERIMENT_PLACES[place][1]
    field = loc[0]
    body = {field: _with(EXPERIMENT_PLACES[place])[field]}

    response = _put_experiment(
        client, developer, experiment_id, body, CHARACTERS[character]
    )

    _assert_refused(response, loc)
    assert (_stored_experiment(fresh, experiment_id), _totals(fresh)) == before


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("place", RULES_READ_BEFORE)
@pytest.mark.parametrize("route", ("create", "update"))
def test_rules_another_check_reads_get_this_answer(
    client, fresh, developer, route, place, character
):
    """The character in a condition's attribute or operator is refused before
    the rules are read, with this answer."""
    body = _with(RULES_READ_BEFORE[place])
    escape = CHARACTERS[character]
    if route == "create":
        before = _totals(fresh)
        _assert_refused(
            _post_experiment(client, developer, body, escape), ("targeting_rules",)
        )
        assert _totals(fresh) == before
        return
    experiment_id = _create_experiment(client, developer)
    before = _stored_experiment(fresh, experiment_id)
    _assert_refused(
        _put_experiment(
            client,
            developer,
            experiment_id,
            {"targeting_rules": body["targeting_rules"]},
            escape,
        ),
        ("targeting_rules",),
    )
    assert _stored_experiment(fresh, experiment_id) == before


@pytest.mark.parametrize(
    "place,character", SETTING_CASES, ids=[f"{p}-{c}" for p, c in SETTING_CASES]
)
def test_an_experiment_create_refuses_the_character_in_a_setting(
    client, fresh, developer, place, character
):
    name, value = _setting(place, MARK + X)
    body = {**_experiment_body(), name: value}
    before = _totals(fresh)

    response = _post_experiment(client, developer, body, CHARACTERS[character])

    _assert_refused(response, SETTING_PLACES[place][2], SETTING_PLACES[place][3])
    assert _totals(fresh) == before


@pytest.mark.parametrize(
    "place,character", SETTING_CASES, ids=[f"{p}-{c}" for p, c in SETTING_CASES]
)
def test_an_experiment_update_refuses_the_character_in_a_setting(
    client, fresh, developer, place, character
):
    experiment_id = _create_experiment(client, developer)
    before = (_stored_experiment(fresh, experiment_id), _totals(fresh))
    name, value = _setting(place, MARK + X)

    response = _put_experiment(
        client, developer, experiment_id, {name: value}, CHARACTERS[character]
    )

    _assert_refused(response, SETTING_PLACES[place][2], SETTING_PLACES[place][3])
    assert (_stored_experiment(fresh, experiment_id), _totals(fresh)) == before


@pytest.mark.parametrize("place", SETTING_PLACES)
def test_a_setting_with_an_emoji_is_still_saved_as_sent(
    client, fresh, developer, place
):
    """On a create and on an update, as each setting's text was sent."""
    name, value = _setting(place, X)
    raw = json.dumps({**_experiment_body(), name: value}).replace(X, EMOJI_ESCAPE)

    created = client.post(
        f"{EXPERIMENTS}/", content=raw.encode("ascii"), headers=_auth(developer)
    )

    assert created.status_code == 201, created.text
    experiment_id = created.json()["id"]
    updated = client.put(
        f"{EXPERIMENTS}/{experiment_id}",
        content=json.dumps({name: value}).replace(X, EMOJI_ESCAPE).encode("ascii"),
        headers=_auth(developer),
    )
    assert updated.status_code == 200, updated.text
    session = fresh()
    try:
        stored = getattr(session.get(Experiment, uuid.UUID(experiment_id)), name)
        assert EMOJI in json.dumps(stored, ensure_ascii=False)
    finally:
        session.close()


@pytest.mark.parametrize("character", CHARACTERS)
def test_a_key_still_answers_its_own_422(client, fresh, developer, character):
    """``key`` refused both characters already (its pattern, or as text that
    is not a string); that answer does not change."""
    body = _experiment_body()
    body["key"] = "k" + MARK + X
    before = _totals(fresh)

    response = _post_experiment(client, developer, body, CHARACTERS[character])

    assert response.status_code == 422, response.text
    assert [e["loc"] for e in response.json()["detail"]] == [["body", "key"]]
    assert response.json()["detail"][0]["type"] in (
        "string_pattern_mismatch",
        "string_unicode",
    )
    assert _totals(fresh) == before


def test_an_emoji_in_an_experiment_is_still_saved_as_sent(client, fresh, developer):
    body = _experiment_body()
    for place, _ in EXPERIMENT_PLACES.values():
        place(body)
    sent = json.dumps(body).replace(MARK, "")

    created = client.post(
        f"{EXPERIMENTS}/",
        content=sent.replace(X, EMOJI_ESCAPE).encode("ascii"),
        headers=_auth(developer),
    )

    assert created.status_code == 201, created.text
    got = created.json()
    assert got["name"].endswith(EMOJI)
    assert got["tags"] == ["ok" + EMOJI]
    assert _condition(got)["value"] == "US" + EMOJI
    treatment = next(v for v in got["variants"] if v["name"] == "Treatment")
    assert treatment["description"] == "blue" + EMOJI
    assert treatment["configuration"] == {"button": {"color" + EMOJI: "blue"}}
    assert sorted(m["event_name"] for m in got["metrics"]) == [
        "purchase",
        "purchase" + EMOJI,
    ]

    update = {
        "description": "d" + X,
        "variants": [
            {"name": "Control" + X, "is_control": True, "traffic_allocation": 50},
            {
                "name": "Treatment",
                "is_control": False,
                "traffic_allocation": 50,
                "configuration": {"color": "blue" + X},
            },
        ],
        "metrics": [
            {
                "name": "Purchase" + X,
                "event_name": "purchase",
                "metric_type": "conversion",
                "is_primary": True,
            }
        ],
    }
    updated = client.put(
        f"{EXPERIMENTS}/{got['id']}",
        content=json.dumps(update).replace(X, EMOJI_ESCAPE).encode("ascii"),
        headers=_auth(developer),
    )
    assert updated.status_code == 200, updated.text
    session = fresh()
    try:
        stored = session.get(Experiment, uuid.UUID(got["id"]))
        assert stored.description == "d" + EMOJI
        assert sorted(v.name for v in stored.variants) == [
            "Control" + EMOJI,
            "Treatment",
        ]
        assert [m.name for m in stored.metric_definitions] == ["Purchase" + EMOJI]
    finally:
        session.close()


# --- a flag's safety settings -------------------------------------------------


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("place", SAFETY_PLACES)
@pytest.mark.parametrize("stored", ("none-stored", "one-stored"))
def test_a_safety_configuration_refuses_the_character(
    client, fresh, db_session, developer, superuser, stored, place, character
):
    flag = _flag(db_session, developer)
    if stored == "one-stored":
        kept = client.post(
            _safety_path(flag.id),
            json={"metrics": {"error_rate": {"critical_threshold": 0.2}}},
            headers=_auth(superuser),
        )
        assert kept.status_code == 200, kept.text
    before = (_stored_safety(fresh, flag.id), _totals(fresh))

    response = _post_safety(
        client,
        superuser,
        flag.id,
        {"enabled": True, "metrics": SAFETY_PLACES[place]},
        CHARACTERS[character],
    )

    _assert_refused(response, ("metrics",))
    assert (_stored_safety(fresh, flag.id), _totals(fresh)) == before


def test_an_emoji_in_a_safety_metric_name_is_still_saved(
    client, fresh, db_session, developer, superuser
):
    flag = _flag(db_session, developer)

    saved = client.post(
        _safety_path(flag.id),
        content=_raw(
            {"metrics": {"error_rate" + X: {"critical_threshold": 0.1}}},
            EMOJI_ESCAPE,
        ),
        headers=_auth(superuser),
    )

    assert saved.status_code == 200, saved.text
    assert list(saved.json()["metrics"]) == ["error_rate" + EMOJI]
    session = fresh()
    try:
        row = (
            session.query(FeatureFlagSafetyConfig)
            .filter_by(feature_flag_id=flag.id)
            .one()
        )
        assert list(row.metrics) == ["error_rate" + EMOJI]
    finally:
        session.close()


# --- API keys -----------------------------------------------------------------


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("place", API_KEY_PLACES)
def test_an_api_key_create_refuses_the_character(
    client, fresh, developer, place, character
):
    body, field = API_KEY_PLACES[place]
    before = _totals(fresh)

    response = _post_key(client, developer, body, CHARACTERS[character])

    _assert_refused(response, (field,))
    assert _totals(fresh) == before


def test_an_emoji_in_an_api_key_is_still_saved(client, fresh, developer):
    created = _post_key(
        client,
        developer,
        {"name": "key " + X, "description": "d " + X},
        EMOJI_ESCAPE,
    )

    assert created.status_code == 201, created.text
    assert created.json()["name"] == "key " + EMOJI
    session = fresh()
    try:
        row = session.get(APIKey, uuid.UUID(created.json()["id"]))
        assert (row.name, row.description) == ("key " + EMOJI, "d " + EMOJI)
    finally:
        session.close()


# --- the order of the refusals ------------------------------------------------

NUL = CHARACTERS["nul"]
NUL_NAME = {"name": "n " + MARK + X}
NUL_METRICS = {"metrics": SAFETY_PLACES["a-metric-name"]}


def test_authentication_still_answers_401_first(client, fresh, db_session, developer):
    experiment_id = _create_experiment(client, developer)
    flag = _flag(db_session, developer)
    before = _totals(fresh)
    for method, path, body in (
        ("POST", f"{EXPERIMENTS}/", _with(EXPERIMENT_PLACES["name"])),
        ("PUT", f"{EXPERIMENTS}/{experiment_id}", NUL_NAME),
        ("POST", _safety_path(flag.id), NUL_METRICS),
        ("POST", API_KEYS, NUL_NAME),
    ):
        response = client.request(method, path, content=_raw(body, NUL), headers=JSON)
        assert response.status_code == 401, (path, response.text)
        assert MARK not in response.text
    assert _totals(fresh) == before


def test_on_an_experiment_create_the_422_comes_before_the_403(client, fresh, viewer):
    body = _with(EXPERIMENT_PLACES["name"])
    before = _totals(fresh)

    _assert_refused(_post_experiment(client, viewer, body, NUL), ("name",))
    # A storable body is still the 403 it was.
    assert _post_experiment(client, viewer, body, "").status_code == 403
    assert _totals(fresh) == before


def test_on_an_experiment_update_the_422_comes_before_the_403(
    client, fresh, developer, viewer
):
    experiment_id = _create_experiment(client, developer)
    before = _stored_experiment(fresh, experiment_id)

    _assert_refused(
        _put_experiment(client, viewer, experiment_id, NUL_NAME, NUL), ("name",)
    )
    assert _put_experiment(client, viewer, experiment_id, NUL_NAME, "").status_code == (
        403
    )
    assert _stored_experiment(fresh, experiment_id) == before


def test_on_an_experiment_update_the_422_comes_before_the_404(client, developer):
    unknown = uuid.uuid4()

    _assert_refused(
        _put_experiment(client, developer, unknown, NUL_NAME, NUL), ("name",)
    )
    assert _put_experiment(client, developer, unknown, NUL_NAME, "").status_code == 404


def test_on_a_safety_configuration_the_403_for_a_non_superuser_still_comes_first(
    client, fresh, db_session, developer, admin
):
    flag = _flag(db_session, developer)
    before = (_stored_safety(fresh, flag.id), _totals(fresh))

    refused = _post_safety(client, admin, flag.id, NUL_METRICS, NUL)

    assert refused.status_code == 403, refused.text
    assert MARK not in refused.text
    assert (_stored_safety(fresh, flag.id), _totals(fresh)) == before


def test_on_a_safety_configuration_the_422_comes_before_the_404(client, superuser):
    unknown = uuid.uuid4()

    _assert_refused(
        _post_safety(client, superuser, unknown, NUL_METRICS, NUL), ("metrics",)
    )
    assert _post_safety(client, superuser, unknown, NUL_METRICS, "").status_code == 404


def test_on_an_api_key_create_the_422_comes_before_the_403(client, fresh, viewer):
    """A role that may not change flags is refused a key with the
    ``sdk:ruleset`` scope (403); a name it cannot store is refused first."""
    body = {**NUL_NAME, "scopes": ["sdk:ruleset"]}
    before = _totals(fresh)

    _assert_refused(_post_key(client, viewer, body, NUL), ("name",))
    assert _post_key(client, viewer, body, "").status_code == 403
    assert _totals(fresh) == before
