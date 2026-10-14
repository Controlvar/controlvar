"""Requests as a dashboard session and an SDK make them, for integration tests.

Real ``User`` rows, real local JWTs from ``create_local_access_token`` and real
API keys made through ``POST /api/v1/api-keys``; the only dependency override
is ``deps.get_db``, so every request goes through the API's own sign-in and
permission checks. ``FakeRedis`` stands in for the results cache's Redis: it
keeps what the API stores, with the expiry it asked for, and answers the calls
``CacheService`` and the cache routes make.
"""

from __future__ import annotations

import contextlib
import fnmatch
import uuid
from typing import Any, Dict, Iterator, List, Optional, Tuple

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from backend.app.api import deps
from backend.app.core.security import create_local_access_token, get_password_hash
from backend.app.main import app
from backend.app.models.user import User, UserRole

SCHEMA = "test_experimentation"


def local_sign_in(monkeypatch) -> None:
    """The local JWT path, with the development sign-in switched off."""
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")


def make_user(
    db_session: Session, role: UserRole, *, superuser: bool = False, tag: str = "u"
) -> User:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{tag}_{role.value.lower()}_{suffix}",
        email=f"{tag}_{role.value.lower()}_{suffix}@claims.test",
        full_name="Page claims",
        hashed_password=get_password_hash("Demo1234!"),
        is_active=True,
        is_superuser=superuser,
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def auth(user: User) -> Dict[str, str]:
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


@contextlib.contextmanager
def api_client(db_session: Session) -> Iterator[TestClient]:
    factory = sessionmaker(
        bind=db_session.get_bind(), autocommit=False, autoflush=False
    )

    def override_get_db():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


def create_experiment(
    client: TestClient,
    owner: User,
    *,
    variants: Optional[List[Dict[str, Any]]] = None,
    metric_type: str = "conversion",
    **fields: Any,
) -> Dict[str, Any]:
    """A draft experiment created through the API; returns its JSON."""
    suffix = uuid.uuid4().hex[:8]
    body: Dict[str, Any] = {
        "name": f"Claims {suffix}",
        "key": f"claims-{suffix}",
        "hypothesis": "the page is true",
        "experiment_type": "a_b",
        "variants": variants
        or [
            {"name": "control", "is_control": True, "traffic_allocation": 50},
            {"name": "treatment", "is_control": False, "traffic_allocation": 50},
        ],
        "metrics": [
            {
                "name": "Purchase",
                "event_name": f"purchase_{suffix}",
                "metric_type": metric_type,
                "is_primary": True,
            }
        ],
        **fields,
    }
    resp = client.post("/api/v1/experiments/", json=body, headers=auth(owner))
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def start(client: TestClient, experiment: Dict[str, Any], user: User) -> None:
    resp = client.post(
        f"/api/v1/experiments/{experiment['id']}/start", headers=auth(user)
    )
    assert resp.status_code == 200, resp.text


def api_key(client: TestClient, user: User, **fields: Any) -> Dict[str, str]:
    resp = client.post(
        "/api/v1/api-keys", json={"name": "claims", **fields}, headers=auth(user)
    )
    assert resp.status_code == 201, resp.text
    return {"X-API-Key": resp.json()["key"]}


def assign(
    client: TestClient, key: Dict[str, str], experiment_key: str, user_id: str
) -> Dict[str, Any]:
    resp = client.post(
        "/api/v1/tracking/assign",
        json={"experiment_key": experiment_key, "user_id": user_id},
        headers=key,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def track(
    client: TestClient,
    key: Dict[str, str],
    user_id: str,
    event_name: str,
    *,
    experiment_key: Optional[str] = None,
    value: Optional[float] = None,
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "event_type": "conversion",
        "event_name": event_name,
        "user_id": user_id,
    }
    if experiment_key is not None:
        body["experiment_key"] = experiment_key
    if value is not None:
        body["value"] = value
    if timestamp is not None:
        body["timestamp"] = timestamp
    resp = client.post("/api/v1/tracking/track", json=body, headers=key)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


class FakeRedis:
    """What the results cache asks of Redis, kept in a dict.

    ``sets`` records every write as ``(key, expiry in seconds)``.
    """

    def __init__(self) -> None:
        self.store: Dict[str, Any] = {}
        self.sets: List[Tuple[str, Optional[int]]] = []

    def ping(self) -> bool:
        return True

    def get(self, key: str) -> Any:
        return self.store.get(key)

    def set(self, key: str, value: Any, ex: Optional[int] = None, **_: Any) -> bool:
        # Redis answers bytes, whatever was written.
        self.store[key] = value.encode() if isinstance(value, str) else value
        self.sets.append((key, ex))
        return True

    def setex(self, key: str, seconds: int, value: Any) -> bool:
        return self.set(key, value, ex=seconds)

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            removed += self.store.pop(key, None) is not None
        return removed

    def exists(self, key: str) -> int:
        return int(key in self.store)

    def keys(self, pattern: str = "*") -> List[str]:
        return [key for key in self.store if fnmatch.fnmatchcase(key, pattern)]

    def scan_iter(self, match: str = "*", **_: Any) -> Iterator[str]:
        return iter(self.keys(match))


def fake_results_cache(monkeypatch) -> FakeRedis:
    """Every Redis client the API creates is this one ``FakeRedis``."""
    fake = FakeRedis()
    monkeypatch.setattr(
        "backend.app.core.redis_client.create_redis_client",
        lambda **_options: fake,
    )
    return fake
