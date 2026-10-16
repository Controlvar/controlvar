"""The StreamPulse seed's two API keys.

``streampulse-app`` is the app's key: it goes into ``.env.local`` as
``NEXT_PUBLIC_CONTROLVAR_API_KEY`` and from there into the browser bundle, so
it carries no scope. ``streampulse-server`` carries ``sdk:ruleset``; the device
simulator sends crash reports with it, so they count toward automatic
rollback. It is written only to ``.api_key_server`` (0600) and never to
``.env.local``.
"""

from __future__ import annotations

import stat
import uuid

import pytest

from backend.app.api.sdk_scope import presented_key_is_server_key
from backend.app.core.api_key_scopes import SDK_RULESET_SCOPE, has_scope
from backend.app.core.security import hash_api_key
from backend.app.models.api_key import APIKey
from backend.scripts import seed_streampulse

pytestmark = [pytest.mark.integration, pytest.mark.regression]


@pytest.fixture
def seed_files(tmp_path, monkeypatch, db_session):
    """The seed's key files in ``tmp_path`` and key names unique to this test."""
    suffix = uuid.uuid4().hex[:8]
    app_name = f"streampulse-app-{suffix}"
    server_name = f"streampulse-server-{suffix}"
    monkeypatch.setattr(seed_streampulse, "STREAMPULSE_DIR", tmp_path)
    monkeypatch.setattr(seed_streampulse, "API_KEY_FILE", tmp_path / ".api_key")
    monkeypatch.setattr(
        seed_streampulse, "SERVER_KEY_FILE", tmp_path / ".api_key_server"
    )
    monkeypatch.setattr(seed_streampulse, "ENV_LOCAL_FILE", tmp_path / ".env.local")
    monkeypatch.setattr(seed_streampulse, "API_KEY_NAME", app_name)
    monkeypatch.setattr(seed_streampulse, "SERVER_KEY_NAME", server_name)
    monkeypatch.delenv("STREAMPULSE_API_KEY", raising=False)
    yield tmp_path, app_name, server_name
    db_session.rollback()
    db_session.query(APIKey).filter(APIKey.name.in_((app_name, server_name))).delete(
        synchronize_session=False
    )
    db_session.commit()


def _rows(db_session, *names) -> dict:
    db_session.expire_all()
    return {
        row.name: row
        for row in db_session.query(APIKey).filter(APIKey.name.in_(names)).all()
    }


def test_the_app_key_has_no_scope_and_the_server_key_stays_out_of_the_app(
    db_session, admin_user, seed_files
):
    folder, app_name, server_name = seed_files

    app_key = seed_streampulse.seed_api_key(db_session, admin_user)
    server_key = seed_streampulse.seed_server_api_key(db_session, admin_user)

    rows = _rows(db_session, app_name, server_name)
    assert set(rows) == {app_name, server_name}
    assert not has_scope(rows[app_name].scopes, SDK_RULESET_SCOPE)
    assert has_scope(rows[server_name].scopes, SDK_RULESET_SCOPE)
    assert rows[server_name].user_id == admin_user.id
    assert rows[app_name].key == hash_api_key(app_key)
    assert rows[server_name].key == hash_api_key(server_key)
    assert app_key != server_key

    env_local = (folder / ".env.local").read_text()
    assert f"NEXT_PUBLIC_CONTROLVAR_API_KEY={app_key}" in env_local
    assert server_key not in env_local
    assert (folder / ".api_key").read_text().strip() == app_key
    server_file = folder / ".api_key_server"
    assert server_file.read_text().strip() == server_key
    assert stat.S_IMODE(server_file.stat().st_mode) == 0o600

    # What the error report routes decide from each key.
    assert presented_key_is_server_key(db_session, server_key) is True
    assert presented_key_is_server_key(db_session, app_key) is False


def test_a_second_run_keeps_both_keys_and_reset_removes_both(
    db_session, admin_user, seed_files
):
    folder, app_name, server_name = seed_files
    app_key = seed_streampulse.seed_api_key(db_session, admin_user)
    server_key = seed_streampulse.seed_server_api_key(db_session, admin_user)

    assert seed_streampulse.seed_api_key(db_session, admin_user) == app_key
    assert seed_streampulse.seed_server_api_key(db_session, admin_user) == server_key
    assert len(_rows(db_session, app_name, server_name)) == 2

    counts = seed_streampulse.reset_streampulse(db_session)

    assert counts["api_keys"] == 2
    assert _rows(db_session, app_name, server_name) == {}
    assert not (folder / ".api_key").exists()
    assert not (folder / ".api_key_server").exists()
