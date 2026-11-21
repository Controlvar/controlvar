"""The demo catalogue seeder against a real API, with both simulators (#1299, C13).

``scripts/seed_demo_catalogue.py`` gives any install the ShopLab and
StreamPulse catalogue through the public API. Here it runs as an operator runs
it, a separate process with the password in ``CONTROLVAR_ADMIN_PASSWORD``,
against the API started as the image starts it (``uvicorn
backend.app.main:app``, a separate process, on a free port on 127.0.0.1). The
API's database is one of its own on the test session's PostgreSQL server,
built from the models as the session's own is, holding one administrator: the
seeder turns on the whole install's global holdout and safety settings, which
every other test in the session would otherwise see.

* The seeder creates the whole catalogue; a second run changes no catalogue
  row, no key and no file; then both simulators, copied beside the key files
  as they sit in ``demo/``, run against it and exit 0: ShopLab bounded by
  ``--max-visitors``, StreamPulse by a short ``--duration``, once with the
  crash incident, which reads the server key's file. A catalogue that misses
  an experiment makes a simulator exit 3.
* The StreamPulse server key is written to ``.api_key_server`` only (0600),
  never to ``.env.local`` or a ``NEXT_PUBLIC_`` line, and the API takes it as
  the server key and the app's key not, as ``test_seed_streampulse_keys.py``
  pins for the database seeder. A key whose file is gone, or holds a refused
  key, is replaced and the old one revoked.
* A field the API refuses stops the seeder, non-zero, with no key made and no
  key file written.
* Nothing either run prints holds a key or the password.
"""

from __future__ import annotations

import importlib.util
import io
import os
import pathlib
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.request
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from backend.app.api.sdk_scope import presented_key_is_server_key
from backend.app.core.api_key_scopes import SDK_RULESET_SCOPE, has_scope
from backend.app.core.security import get_password_hash, hash_api_key
from backend.app.db.session import Base
from backend.app.models.api_key import APIKey
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.mutual_exclusion_group import MutualExclusionGroup
from backend.app.models.rollout_schedule import RolloutSchedule, RolloutScheduleStatus
from backend.app.models.safety import FeatureFlagSafetyConfig, SafetySettings
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.integration]

REPO = pathlib.Path(__file__).resolve().parents[4]
SEEDER = REPO / "scripts" / "seed_demo_catalogue.py"
SCHEMA = "test_experimentation"

# Loaded from its file, as an operator's checkout has it (it is not a package).
_spec = importlib.util.spec_from_file_location("_seed_demo_catalogue_real_api", SEEDER)
seeder = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = seeder  # its dataclasses look their module up by name
_spec.loader.exec_module(seeder)

#: The catalogue's tables, compared row for row before and after a second run.
CATALOGUE_TABLES = (
    "experiments",
    "variants",
    "metrics",
    "feature_flags",
    "rollout_schedules",
    "rollout_stages",
    "feature_flag_safety_configs",
    "safety_settings",
    "mutual_exclusion_groups",
    "global_holdouts",
    "api_keys",
)
#: Written by a read, not by the seeder: checking a key records its use, at most
#: once a minute (LAST_USED_RESOLUTION, backend/app/models/api_key.py), so
#: whether a second run moves it depends only on how long the first one took.
WRITTEN_BY_READS = {("api_keys", "last_used_at")}
#: All a second run adds to the two audit trails: its one sign-in (measured).
SIGN_IN_AUDIT = Counter({"audit_logs:user_login": 1})


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class Install:
    url: str
    email: str
    password: str
    engine: Engine


def _serve(url: URL, log_path: pathlib.Path):
    port = _free_port()
    env = dict(
        os.environ,
        LOG_FORMAT="json",
        POSTGRES_SERVER=url.host,
        POSTGRES_PORT=str(url.port),
        POSTGRES_USER=url.username,
        POSTGRES_PASSWORD=url.password or "",
        POSTGRES_DB=url.database,
        POSTGRES_SCHEMA=SCHEMA,
        DATABASE_URI=url.render_as_string(hide_password=False),
        # As the API task runs: it never migrates or seeds, and signs in with
        # passwords.
        RUN_MIGRATIONS="false",
        SEED="",
        AUTH_PROVIDER="local",
        DEV_AUTH_BYPASS="false",
    )
    log = log_path.open("w")
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "backend.app.main:app",
            "--http",
            "h11",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=REPO,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 90
    while True:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health/live", timeout=1)
            break
        except OSError:
            if server.poll() is not None or time.monotonic() > deadline:
                server.terminate()
                log.close()
                pytest.fail("uvicorn did not start:\n" + log_path.read_text()[-3000:])
            time.sleep(0.3)
    return server, log, f"http://127.0.0.1:{port}"


@pytest.fixture
def install(test_db, tmp_path):
    """An install: its own database, one administrator, and the API serving it."""
    name = f"demo_seed_{uuid.uuid4().hex[:12]}"
    admin = create_engine(
        test_db.url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = test_db.url.set(database=name)
    engine = create_engine(url, poolclass=NullPool)

    @event.listens_for(engine, "connect")
    def _search_path(dbapi_conn, record):
        cursor = dbapi_conn.cursor()
        cursor.execute(f"SET search_path TO {SCHEMA}")
        cursor.close()

    server = log = None
    try:
        with engine.begin() as conn:
            conn.execute(text(f"CREATE SCHEMA {SCHEMA}"))
        # The models the session's database was built from (backend/tests/conftest.py).
        Base.metadata.create_all(bind=engine)
        email = f"admin_{uuid.uuid4().hex[:8]}@seed.test"
        password = "Seed-Admin-" + uuid.uuid4().hex
        with Session(bind=engine) as session:
            session.add(
                User(
                    username=email.split("@")[0],
                    email=email,
                    full_name="Install Administrator",
                    hashed_password=get_password_hash(password),
                    is_active=True,
                    is_superuser=True,
                    role=UserRole.ADMIN,
                )
            )
            session.commit()
        server, log, base = _serve(url, tmp_path / "uvicorn.log")
        yield Install(url=base, email=email, password=password, engine=engine)
    finally:
        if server is not None:
            server.terminate()
            server.wait(timeout=20)
            log.close()
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :name"
                ),
                {"name": name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


@pytest.fixture
def demo(tmp_path):
    """``demo/<app>/simulator`` copied into a folder of its own, so the
    simulators look for their key files where the seeder writes them, and the
    checkout's own key files are never touched."""
    root = tmp_path / "demo"
    for app, files in (
        ("shoplab", ("traffic.py",)),
        ("streampulse", ("traffic.py", "devices.py")),
    ):
        (root / app / "simulator").mkdir(parents=True)
        for name in files:
            shutil.copy2(
                REPO / "demo" / app / "simulator" / name,
                root / app / "simulator" / name,
            )
    return root


def _seed(install: Install, demo: pathlib.Path) -> subprocess.CompletedProcess:
    env = dict(
        os.environ,
        CONTROLVAR_ADMIN_PASSWORD=install.password,
        SHOPLAB_DEMO_DIR=str(demo / "shoplab"),
        STREAMPULSE_DEMO_DIR=str(demo / "streampulse"),
    )
    return subprocess.run(
        [
            sys.executable,
            str(SEEDER),
            "--api-url",
            install.url,
            "--email",
            install.email,
        ],
        cwd=REPO,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _simulate(demo: pathlib.Path, app: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(demo / app / "simulator" / "traffic.py"), *args],
        cwd=REPO,
        env=dict(os.environ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _key_files(demo: pathlib.Path) -> Dict[str, pathlib.Path]:
    return {
        "shoplab .api_key": demo / "shoplab" / ".api_key",
        "shoplab .env.local": demo / "shoplab" / ".env.local",
        "streampulse .api_key": demo / "streampulse" / ".api_key",
        "streampulse .env.local": demo / "streampulse" / ".env.local",
        "streampulse .api_key_server": demo / "streampulse" / ".api_key_server",
    }


def _key(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _rows(engine: Engine) -> Dict[str, List[Tuple[Any, ...]]]:
    """Every row of the catalogue's tables, less what a read writes."""
    found: Dict[str, List[Tuple[Any, ...]]] = {}
    with engine.connect() as conn:
        for table in CATALOGUE_TABLES:
            result = conn.execute(text(f"SELECT * FROM {SCHEMA}.{table} ORDER BY id"))
            columns = [c for c in result.keys() if (table, c) not in WRITTEN_BY_READS]
            found[table] = [tuple(row._mapping[c] for c in columns) for row in result]
    return found


def _audit_actions(engine: Engine) -> Counter:
    """How many entries of each action the two audit trails hold."""
    with engine.connect() as conn:
        found = Counter(
            f"audit_logs:{action}"
            for (action,) in conn.execute(
                text(f"SELECT action_type FROM {SCHEMA}.audit_logs")
            )
        )
        found.update(
            f"audit_events_v2:{action}"
            for (action,) in conn.execute(
                text(f"SELECT action FROM {SCHEMA}.audit_events_v2")
            )
        )
    return found


def _exited(run: subprocess.CompletedProcess) -> str:
    """A failed run's exit code and everything it printed, for the assertion."""
    return f"exit {run.returncode}:\n{run.stdout}{run.stderr}"


def _assert_holds_no_secret(printed: str, secrets: List[str]) -> None:
    for secret in secrets:
        tail = secret[5:] if secret.startswith("eptk_") else secret
        for i in range(max(len(tail) - 10 + 1, 1)):
            assert tail[i : i + 10] not in printed, "a key or the password was printed"


def test_the_seeder_creates_the_catalogue_a_second_run_changes_nothing_and_both_simulators_run(
    install, demo
):
    first = _seed(install, demo)
    assert first.returncode == 0, _exited(first)

    with Session(bind=install.engine) as session:
        experiments = {e.key: e for e in session.query(Experiment).all()}
        assert sorted(experiments) == sorted(
            [s["key"] for s in seeder.SHOPLAB_EXPERIMENTS]
            + [s["key"] for s in seeder.STREAMPULSE_EXPERIMENTS]
        )
        assert {e.status for e in experiments.values()} == {ExperimentStatus.ACTIVE}
        for spec in seeder.SHOPLAB_EXPERIMENTS + seeder.STREAMPULSE_EXPERIMENTS:
            stored = experiments[spec["key"]]
            assert sorted(v.name for v in stored.variants) == sorted(
                v["name"] for v in spec["variants"]
            )
            assert sorted(m.name for m in stored.metric_definitions) == sorted(
                m["name"] for m in spec["metrics"]
            )
        (group,) = session.query(MutualExclusionGroup).all()
        assert group.name == "streampulse-profile"
        assert sorted(
            k for k, e in experiments.items() if e.mutual_exclusion_group_id == group.id
        ) == sorted(seeder.STREAMPULSE_GROUPED)
        (holdout,) = session.query(GlobalHoldout).all()
        assert (holdout.name, holdout.holdout_percentage, holdout.is_active) == (
            "streampulse-holdout",
            2,
            True,
        )
        flags = {f.key: f for f in session.query(FeatureFlag).all()}
        assert sorted(flags) == sorted(
            [s["flag"]["key"] for s in seeder.SHOPLAB_FLAGS]
            + [s["flag"]["key"] for s in seeder.STREAMPULSE_FLAGS]
        )
        assert {f.status for f in flags.values()} == {FeatureFlagStatus.ACTIVE}
        assert flags["streampulse_player_v2"].targeting_rules == seeder.PLAYER_V2_RULES
        schedules = {
            s.feature_flag.key: s for s in session.query(RolloutSchedule).all()
        }
        assert sorted(schedules) == ["shoplab_new_search", "streampulse_player_v2"]
        assert {s.status for s in schedules.values()} == {RolloutScheduleStatus.ACTIVE}
        stages = sorted(
            schedules["streampulse_player_v2"].stages, key=lambda st: st.stage_order
        )
        assert [st.target_percentage for st in stages] == [5, 25, 50, 100]
        configs = {
            c.feature_flag.key: c for c in session.query(FeatureFlagSafetyConfig).all()
        }
        assert {k: c.rollback_percentage for k, c in configs.items()} == {
            "shoplab_new_search": 0,
            "streampulse_recs_v2": 0,
            "streampulse_player_v2": 5,
        }
        (settings,) = session.query(SafetySettings).all()
        assert settings.enable_automatic_rollbacks is True
        keys = {k.name: k for k in session.query(APIKey).all()}
        assert {name: k.scopes for name, k in keys.items()} == {
            "shoplab-storefront": "read,write",
            "streampulse-app": "read,write",
            "streampulse-server": "sdk:ruleset",
        }

    rows, audit = _rows(install.engine), _audit_actions(install.engine)
    files = {
        name: (p.read_bytes(), p.stat().st_mtime_ns)
        for name, p in _key_files(demo).items()
    }
    second = _seed(install, demo)
    assert second.returncode == 0, _exited(second)
    assert "Done: nothing to change." in second.stdout
    assert _rows(install.engine) == rows
    after = _audit_actions(install.engine)
    assert after - audit == SIGN_IN_AUDIT and not audit - after, after - audit
    assert {
        name: (p.read_bytes(), p.stat().st_mtime_ns)
        for name, p in _key_files(demo).items()
    } == files

    secrets = [install.password] + [
        _key(p) for n, p in _key_files(demo).items() if ".api_key" in n
    ]
    assert len(secrets) == 4
    for run in (first, second):
        _assert_holds_no_secret(run.stdout + run.stderr, secrets)
        assert "--server-api-key" not in run.stdout + run.stderr

    shoplab = _simulate(
        demo,
        "shoplab",
        "--api-url",
        install.url,
        "--max-visitors",
        "20",
        "--rate",
        "20",
        "--seed",
        "1",
    )
    assert shoplab.returncode == 0, _exited(shoplab)
    assert "visitors=20" in shoplab.stdout
    streampulse = _simulate(
        demo,
        "streampulse",
        "--api-url",
        install.url,
        "--duration",
        "3",
        "--rate",
        "5",
        "--seed",
        "1",
    )
    assert streampulse.returncode == 0, _exited(streampulse)
    incident = _simulate(
        demo,
        "streampulse",
        "--api-url",
        install.url,
        "--incident",
        "android12",
        "--duration",
        "2",
        "--rate",
        "5",
        "--seed",
        "1",
        "--no-probe",
    )
    assert incident.returncode == 0, _exited(incident)


def test_the_server_key_is_written_only_to_its_own_file_and_a_lost_key_is_replaced(
    install, demo
):
    seeded = _seed(install, demo)
    assert seeded.returncode == 0, _exited(seeded)
    files = _key_files(demo)
    server_key = _key(files["streampulse .api_key_server"])
    app_key = _key(files["streampulse .api_key"])
    shoplab_key = _key(files["shoplab .api_key"])
    assert len({server_key, app_key, shoplab_key}) == 3

    for name, path in files.items():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, name
    env_local = files["streampulse .env.local"].read_text(encoding="utf-8")
    assert f"NEXT_PUBLIC_CONTROLVAR_API_KEY={app_key}" in env_local.splitlines()
    assert server_key not in env_local
    for name, path in files.items():
        text_ = path.read_text(encoding="utf-8")
        if name != "streampulse .api_key_server":
            assert server_key not in text_, name
        for line in text_.splitlines():
            if line.startswith("NEXT_PUBLIC_"):
                assert server_key not in line, name
    assert (
        f"NEXT_PUBLIC_CONTROLVAR_API_KEY={shoplab_key}"
        in files["shoplab .env.local"].read_text(encoding="utf-8").splitlines()
    )

    with Session(bind=install.engine) as session:
        rows = {k.name: k for k in session.query(APIKey).all()}
        assert rows["streampulse-server"].key == hash_api_key(server_key)
        assert rows["streampulse-app"].key == hash_api_key(app_key)
        assert rows["shoplab-storefront"].key == hash_api_key(shoplab_key)
        assert has_scope(rows["streampulse-server"].scopes, SDK_RULESET_SCOPE)
        assert not has_scope(rows["streampulse-app"].scopes, SDK_RULESET_SCOPE)
        assert not has_scope(rows["shoplab-storefront"].scopes, SDK_RULESET_SCOPE)
        # What the error report routes decide from each key.
        assert presented_key_is_server_key(session, server_key) is True
        assert presented_key_is_server_key(session, app_key) is False
        old = {name: row.id for name, row in rows.items()}

    # A key whose file is gone, and one whose file holds a key the install refuses.
    files["streampulse .api_key_server"].unlink()
    files["shoplab .api_key"].write_text(
        "eptk_" + uuid.uuid4().hex + "\n", encoding="utf-8"
    )
    again = _seed(install, demo)
    assert again.returncode == 0, _exited(again)
    with Session(bind=install.engine) as session:
        rows = {}
        for row in session.query(APIKey).all():
            assert row.name not in rows, (
                f"two {row.name} keys: the old one was not revoked"
            )
            rows[row.name] = row
        assert rows["streampulse-app"].id == old["streampulse-app"]
        for name, file in (
            ("streampulse-server", "streampulse .api_key_server"),
            ("shoplab-storefront", "shoplab .api_key"),
        ):
            assert rows[name].id != old[name], name
            assert rows[name].key == hash_api_key(_key(files[file])), name
            assert stat.S_IMODE(files[file].stat().st_mode) == 0o600, name
        assert (
            presented_key_is_server_key(
                session, _key(files["streampulse .api_key_server"])
            )
            is True
        )
    assert (
        f"NEXT_PUBLIC_CONTROLVAR_API_KEY={_key(files['shoplab .api_key'])}"
        in files["shoplab .env.local"].read_text(encoding="utf-8").splitlines()
    )
    assert _key(files["streampulse .api_key_server"]) not in files[
        "streampulse .env.local"
    ].read_text(encoding="utf-8")
    _assert_holds_no_secret(
        seeded.stdout + seeded.stderr + again.stdout + again.stderr,
        [install.password, server_key, app_key, shoplab_key]
        + [_key(p) for n, p in files.items() if ".api_key" in n],
    )


def test_a_field_the_api_refuses_stops_the_seeder_before_any_key_file(
    install, demo, monkeypatch
):
    """One flag body carries a field the API does not take: 422, exit 1, no key."""
    flags = list(seeder.STREAMPULSE_FLAGS)
    refused = dict(flags[2], flag={**flags[2]["flag"], "unknown_field": True})
    flags[2] = refused
    monkeypatch.setattr(seeder, "STREAMPULSE_FLAGS", tuple(flags))
    out, err = io.StringIO(), io.StringIO()
    code = seeder.main(
        ["--api-url", install.url, "--email", install.email],
        environ={
            seeder.PASSWORD_ENV: install.password,
            seeder.SHOPLAB_DIR_ENV: str(demo / "shoplab"),
            seeder.STREAMPULSE_DIR_ENV: str(demo / "streampulse"),
        },
        stdin=io.StringIO(),
        out=out,
        err=err,
    )
    assert code == 1, out.getvalue() + err.getvalue()
    assert "POST /api/v1/feature-flags/: HTTP 422" in err.getvalue()
    assert not [p for p in _key_files(demo).values() if p.exists()]
    with Session(bind=install.engine) as session:
        assert session.query(APIKey).count() == 0
        assert (
            session.query(FeatureFlag)
            .filter(FeatureFlag.key == refused["flag"]["key"])
            .count()
            == 0
        )
