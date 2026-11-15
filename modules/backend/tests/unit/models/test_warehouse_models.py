"""The warehouse-analysis tables: what they accept, refuse and keep (#312).

Run against the test database, which the models built (the migration builds
the same shape; ``test_modules_0002_transitions.py`` compares the two), so every
constraint here is the database's, not a Python check.
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import CheckConstraint, text
from sqlalchemy.exc import IntegrityError

from modules.backend.app import settings as modules_settings
from modules.backend.app.core.credential_crypto import (
    CredentialKeysUnavailable,
    CredentialUndecryptable,
)
from modules.backend.app.models.warehouse_analysis_run import WarehouseAnalysisRun
from modules.backend.app.models.warehouse_connection import (
    BYTE_CAPPED_TYPES,
    ROLE_TYPES,
    WAREHOUSE_TYPES,
    WarehouseConnection,
)
from modules.backend.app.models.warehouse_source import WarehouseSource
from modules.backend.app.services import warehouse_clients as clients

#: A stand-in for a private key: must never appear in a stored column.
SENTINEL = b"w2a-model-sentinel-5d1e: stands in for a private key"


def _connection(**overrides) -> WarehouseConnection:
    fields = {
        "name": f"conn-{uuid.uuid4().hex[:8]}",
        "warehouse_type": "snowflake",
        "parameters": {"account": "MYORG-MYACCOUNT"},
        "query_timeout_seconds": 300,
        "max_runs_per_day": 20,
    }
    fields.update(overrides)
    return WarehouseConnection(**fields)


def _source(connection_id, **overrides) -> WarehouseSource:
    fields = {
        "connection_id": connection_id,
        "kind": "metric",
        "name": f"src-{uuid.uuid4().hex[:8]}",
        "table_reference": {"parts": ["DB", "SCH", "EVENTS"]},
        "column_mapping": {"unit_id": {"name": "USER_ID", "type": "TEXT"}},
        "metric_type": "proportion",
        "conversion_window_hours": 168,
    }
    fields.update(overrides)
    return WarehouseSource(**fields)


def _run(connection, experiment_id=None, **overrides) -> WarehouseAnalysisRun:
    fields = {
        "kind": "analysis",
        "status": "succeeded",
        "connection_id": connection.id,
        "connection_name": connection.name,
        "warehouse_type": connection.warehouse_type,
        "experiment_id": experiment_id,
        "request": {},
    }
    fields.update(overrides)
    return WarehouseAnalysisRun(**fields)


def _refused(db_session, *objects) -> None:
    for obj in objects:
        db_session.add(obj)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


@pytest.fixture
def connection(db_session):
    conn = _connection()
    db_session.add(conn)
    db_session.commit()
    return conn


@pytest.fixture
def experiment(db_session, normal_user):
    from backend.app.models.experiment import Experiment, ExperimentStatus

    exp = Experiment(
        name=f"wh-{uuid.uuid4().hex[:8]}",
        description="warehouse run owner",
        hypothesis="h",
        owner_id=normal_user.id,
        status=ExperimentStatus.DRAFT,
    )
    db_session.add(exp)
    db_session.commit()
    return exp


# ---------------------------------------------------------------------------
# Credentials at rest
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_raw_column_is_fernet(db_session):
    """What the database holds is a Fernet token under the operator's key.

    Read back with raw SQL, not through the model: the stored bytes are a
    token, the plaintext is in neither them nor their base64 decoding, the
    right key opens them and a wrong key does not.
    """
    key = Fernet.generate_key().decode()
    conn = _connection()
    conn.set_credentials(SENTINEL, keys=key)
    db_session.add(conn)
    db_session.commit()

    raw = db_session.execute(
        text("SELECT credentials_ciphertext FROM warehouse_connections WHERE id = :id"),
        {"id": conn.id},
    ).scalar_one()
    raw = bytes(raw)

    assert raw.startswith(b"gAAAAA"), "not a Fernet token (version byte 0x80)"
    assert SENTINEL not in raw
    assert b"w2a-model-sentinel" not in base64.urlsafe_b64decode(raw)
    assert Fernet(key.encode()).decrypt(raw) == SENTINEL
    with pytest.raises(InvalidToken):
        Fernet(Fernet.generate_key()).decrypt(raw)
    assert conn.get_credentials(keys=key) == SENTINEL


def test_pending_credentials_are_encrypted_too(db_session):
    key = Fernet.generate_key().decode()
    conn = _connection()
    conn.set_credentials(b"current", keys=key)
    conn.set_credentials(SENTINEL, pending=True, keys=key)
    db_session.add(conn)
    db_session.commit()

    raw = db_session.execute(
        text(
            "SELECT pending_credentials_ciphertext FROM warehouse_connections "
            "WHERE id = :id"
        ),
        {"id": conn.id},
    ).scalar_one()
    assert bytes(raw).startswith(b"gAAAAA")
    assert conn.get_credentials(pending=True, keys=key) == SENTINEL
    assert conn.get_credentials(keys=key) == b"current"


def test_no_keys_means_nothing_is_stored():
    conn = _connection()
    with pytest.raises(CredentialKeysUnavailable):
        conn.set_credentials(SENTINEL, keys="")
    assert conn.credentials_ciphertext is None


def test_a_removed_key_is_a_typed_error_not_a_crash():
    conn = _connection()
    conn.set_credentials(SENTINEL, keys=Fernet.generate_key().decode())
    with pytest.raises(CredentialUndecryptable):
        conn.get_credentials(keys=Fernet.generate_key().decode())


def test_no_credential_reads_as_none():
    assert _connection().get_credentials(keys=Fernet.generate_key().decode()) is None


def test_an_athena_connection_takes_no_credential():
    conn = _connection(warehouse_type="athena")
    with pytest.raises(ValueError):
        conn.set_credentials(SENTINEL, keys=Fernet.generate_key().decode())


def test_repr_carries_no_parameters_or_credentials():
    conn = _connection(parameters={"user": "w2a-repr-sentinel"})
    conn.credentials_ciphertext = b"w2a-repr-token"
    assert "sentinel" not in repr(conn)
    assert "token" not in repr(conn)


# ---------------------------------------------------------------------------
# Role-based types (ROLE_TYPES, #826): what "athena" stood for, unchanged
# ---------------------------------------------------------------------------
SECRET_TYPES = tuple(t for t in WAREHOUSE_TYPES if t not in ROLE_TYPES)


def _role_connection(warehouse_type: str) -> WarehouseConnection:
    return _connection(
        warehouse_type=warehouse_type,
        parameters={"region": "eu-west-1", "role_arn": "arn:aws:iam::1:role/r"},
        external_id=f"exp-{uuid.uuid4().hex}",
        max_bytes_per_query=(
            10_000_000 if warehouse_type in BYTE_CAPPED_TYPES else None
        ),
    )


def test_the_role_types_are_athena_and_redshift_and_are_known_types():
    assert ROLE_TYPES == ("athena", "redshift")
    assert set(ROLE_TYPES) < set(WAREHOUSE_TYPES)
    assert SECRET_TYPES == ("bigquery", "snowflake")
    assert "redshift" not in BYTE_CAPPED_TYPES


@pytest.mark.parametrize("warehouse_type", ROLE_TYPES)
def test_a_role_type_stores_no_credential(warehouse_type):
    conn = _role_connection(warehouse_type)
    with pytest.raises(ValueError):
        conn.set_credentials(SENTINEL, keys=Fernet.generate_key().decode())
    with pytest.raises(ValueError):
        conn.set_credentials(
            SENTINEL, pending=True, keys=Fernet.generate_key().decode()
        )
    assert conn.credentials_ciphertext is None
    assert conn.pending_credentials_ciphertext is None


@pytest.mark.parametrize("warehouse_type", SECRET_TYPES)
def test_a_secret_type_stores_its_credential(warehouse_type):
    key = Fernet.generate_key().decode()
    conn = _connection(warehouse_type=warehouse_type)
    conn.set_credentials(SENTINEL, keys=key)
    assert conn.get_credentials(keys=key) == SENTINEL


@pytest.mark.parametrize("warehouse_type", ROLE_TYPES)
def test_a_role_connection_is_ok_and_its_spec_carries_its_external_id(
    warehouse_type, monkeypatch
):
    # No keys configured: a role connection needs none.
    monkeypatch.setattr(
        modules_settings.load_modules_settings(), "WAREHOUSE_CREDENTIALS_KEYS", None
    )
    conn = _role_connection(warehouse_type)
    assert clients.credentials_status(conn) == "ok"
    spec = clients.spec_for(conn)
    assert spec.external_id == conn.external_id
    assert spec.credential is None
    assert repr(spec) == f"ConnectionSpec(warehouse_type={warehouse_type!r})"


@pytest.mark.parametrize("warehouse_type", SECRET_TYPES)
def test_a_secret_connections_spec_has_no_external_id(warehouse_type, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(
        modules_settings.load_modules_settings(), "WAREHOUSE_CREDENTIALS_KEYS", key
    )
    conn = _connection(warehouse_type=warehouse_type)
    conn.set_credentials(SENTINEL)
    spec = clients.spec_for(conn)
    assert spec.external_id is None
    assert spec.credential == SENTINEL
    assert clients.credentials_status(conn) == "ok"
    assert "sentinel" not in repr(spec)


def test_the_role_constraints_are_built_from_the_role_types():
    """The type list and the two role constraints name every role type, in
    exactly the text ``modules_0005_redshift`` writes, so a migrated database
    and a bootstrapped one agree (``test_modules_0005_transitions.py``
    compares the two catalogues)."""
    sql = {
        c.name: str(c.sqltext)
        for c in WarehouseConnection.__table__.constraints
        if isinstance(c, CheckConstraint)
    }
    assert sql["ck_warehouse_connections_type"] == (
        "warehouse_type IN ('bigquery', 'snowflake', 'athena', 'redshift')"
    )
    assert sql["ck_warehouse_connections_external_id"] == (
        "(warehouse_type IN ('athena', 'redshift')) = (external_id IS NOT NULL)"
    )
    assert sql["ck_warehouse_connections_athena_no_secret"] == (
        "warehouse_type NOT IN ('athena', 'redshift') OR (credentials_ciphertext "
        "IS NULL AND pending_credentials_ciphertext IS NULL)"
    )
    assert sql["ck_warehouse_connections_byte_cap_type"] == (
        "(warehouse_type IN ('bigquery', 'athena')) = (max_bytes_per_query IS NOT NULL)"
    )


# ---------------------------------------------------------------------------
# warehouse_connections
# ---------------------------------------------------------------------------
#: An Amazon Redshift row's external id: the rows that break a rule below have
#: one, so each is refused by the rule it names and not by the external-id one.
_REDSHIFT_ID = {"external_id": "exp-" + "2" * 32}


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"warehouse_type": "postgres"}, id="unknown-type"),
        pytest.param({"query_timeout_seconds": 9}, id="query-limit-below-10"),
        pytest.param({"max_runs_per_day": 0}, id="no-runs-per-day"),
        pytest.param({"max_bytes_per_query": 10_000_000}, id="snowflake-byte-cap"),
        pytest.param({"warehouse_type": "bigquery"}, id="bigquery-no-byte-cap"),
        pytest.param(
            {"warehouse_type": "bigquery", "max_bytes_per_query": 9_999_999},
            id="byte-cap-below-minimum",
        ),
        pytest.param(
            {"warehouse_type": "athena", "max_bytes_per_query": 10_000_000},
            id="athena-no-external-id",
        ),
        pytest.param({"external_id": "exp-" + "0" * 32}, id="snowflake-external-id"),
        pytest.param(
            {
                "warehouse_type": "athena",
                "max_bytes_per_query": 10_000_000,
                "external_id": "exp-" + "1" * 32,
                "credentials_ciphertext": b"gAAAAA-x",
            },
            id="athena-with-a-secret",
        ),
        pytest.param({"warehouse_type": "redshift"}, id="redshift-no-external-id"),
        pytest.param(
            {
                "warehouse_type": "redshift",
                **_REDSHIFT_ID,
                "credentials_ciphertext": b"gAAAAA-x",
            },
            id="redshift-with-a-secret",
        ),
        pytest.param(
            {
                "warehouse_type": "redshift",
                **_REDSHIFT_ID,
                "pending_credentials_ciphertext": b"gAAAAA-x",
            },
            id="redshift-with-a-pending-secret",
        ),
        pytest.param(
            {
                "warehouse_type": "redshift",
                **_REDSHIFT_ID,
                "max_bytes_per_query": 10_000_000,
            },
            id="redshift-with-a-byte-cap",
        ),
        pytest.param({"name": None}, id="no-name"),
        pytest.param({"parameters": None}, id="no-parameters"),
        pytest.param({"parameters": ["a"]}, id="parameters-not-an-object"),
    ],
)
def test_the_connection_table_refuses(db_session, overrides):
    _refused(db_session, _connection(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"warehouse_type": "bigquery", "max_bytes_per_query": 214_748_364_800},
        {
            "warehouse_type": "athena",
            "max_bytes_per_query": 10_000_000,
            "external_id": f"exp-{uuid.uuid4().hex}",
        },
        {
            "warehouse_type": "redshift",
            "parameters": {"region": "eu-west-1", "workgroup_name": "analytics"},
            "external_id": f"exp-{uuid.uuid4().hex}",
        },
    ],
    ids=["snowflake", "bigquery", "athena", "redshift"],
)
def test_the_connection_table_accepts_each_type(db_session, overrides):
    conn = _connection(**overrides)
    db_session.add(conn)
    db_session.commit()
    assert conn.id is not None


def test_external_ids_are_unique(db_session):
    external_id = f"exp-{uuid.uuid4().hex}"
    athena = {
        "warehouse_type": "athena",
        "max_bytes_per_query": 10_000_000,
        "external_id": external_id,
    }
    db_session.add(_connection(**athena))
    db_session.commit()
    _refused(db_session, _connection(**athena))


def test_defaults(db_session):
    conn = WarehouseConnection(
        name=f"defaults-{uuid.uuid4().hex[:8]}", warehouse_type="snowflake"
    )
    db_session.add(conn)
    db_session.commit()
    assert conn.query_timeout_seconds == 300
    assert conn.max_runs_per_day == 20
    assert conn.parameters == {}


# ---------------------------------------------------------------------------
# warehouse_sources
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"kind": "exposure"}, id="unknown-kind"),
        pytest.param({"metric_type": None}, id="metric-without-type"),
        pytest.param({"metric_type": "ratio"}, id="unknown-metric-type"),
        pytest.param({"conversion_window_hours": None}, id="metric-without-window"),
        pytest.param({"conversion_window_hours": 0}, id="window-0"),
        pytest.param({"conversion_window_hours": 8761}, id="window-over-a-year"),
        pytest.param({"cap_value": 10.0}, id="cap-on-a-proportion"),
        pytest.param({"metric_type": "mean", "cap_value": 0.0}, id="cap-not-positive"),
        pytest.param(
            {"kind": "assignment", "metric_type": None}, id="assignment-with-window"
        ),
        pytest.param(
            {"kind": "assignment", "conversion_window_hours": None},
            id="assignment-with-metric-type",
        ),
        pytest.param({"table_reference": None}, id="no-table"),
        pytest.param({"column_mapping": None}, id="no-mapping"),
        pytest.param({"column_mapping": []}, id="mapping-not-an-object"),
        pytest.param({"filters": None}, id="no-filters"),
        pytest.param({"filters": {}}, id="filters-not-a-list"),
    ],
)
def test_the_source_table_refuses(db_session, connection, overrides):
    _refused(db_session, _source(connection.id, **overrides))


def test_the_source_table_accepts_both_kinds(db_session, connection):
    assignment = _source(
        connection.id, kind="assignment", metric_type=None, conversion_window_hours=None
    )
    mean = _source(connection.id, metric_type="mean", cap_value=500.0)
    db_session.add_all([assignment, mean])
    db_session.commit()
    assert mean.filters == []


def test_source_names_are_unique_per_connection_and_kind(db_session, connection):
    db_session.add(_source(connection.id, name="orders"))
    db_session.commit()
    # Same name, other kind: fine.
    db_session.add(
        _source(
            connection.id,
            name="orders",
            kind="assignment",
            metric_type=None,
            conversion_window_hours=None,
        )
    )
    db_session.commit()
    _refused(db_session, _source(connection.id, name="orders"))


# ---------------------------------------------------------------------------
# warehouse_analysis_runs
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"kind": "sync"}, id="unknown-kind"),
        pytest.param({"status": "done"}, id="unknown-status"),
        pytest.param({"status": "failed"}, id="failed-without-a-code"),
        pytest.param({"error_code": "time_limit"}, id="a-code-without-failing"),
        pytest.param({"connection_name": None}, id="no-connection-name"),
        pytest.param({"request": None}, id="no-request"),
        pytest.param(
            {
                "window_start": datetime(2026, 9, 2, tzinfo=timezone.utc),
                "window_end": datetime(2026, 9, 1, tzinfo=timezone.utc),
            },
            id="window-backwards",
        ),
    ],
)
def test_the_run_table_refuses(db_session, connection, experiment, overrides):
    _refused(db_session, _run(connection, experiment.id, **overrides))


def test_an_analysis_names_its_experiment(db_session, connection):
    _refused(db_session, _run(connection, None, kind="analysis"))


def test_a_preview_needs_no_experiment(db_session, connection):
    db_session.add(_run(connection, None, kind="preview"))
    db_session.commit()


@pytest.mark.regression
def test_one_run_in_flight_per_connection(db_session, connection, experiment):
    """The partial unique index: a second queued or running run is refused."""
    db_session.add(_run(connection, experiment.id, status="queued"))
    db_session.commit()

    _refused(db_session, _run(connection, experiment.id, status="running"))
    _refused(db_session, _run(connection, None, kind="preview", status="queued"))
    # Finished runs are not in flight.
    db_session.add(_run(connection, experiment.id, status="succeeded"))
    db_session.add(
        _run(connection, experiment.id, status="failed", error_code="time_limit")
    )
    db_session.commit()

    # Another connection has its own slot.
    other = _connection()
    db_session.add(other)
    db_session.commit()
    db_session.add(_run(other, experiment.id, status="queued"))
    db_session.commit()


def test_deleting_a_connection_keeps_its_runs_and_drops_its_sources(
    db_session, connection, experiment
):
    source = _source(connection.id)
    run = _run(connection, experiment.id)
    db_session.add_all([source, run])
    db_session.commit()
    name = connection.name

    db_session.execute(
        text("DELETE FROM warehouse_connections WHERE id = :id"), {"id": connection.id}
    )
    db_session.commit()

    assert (
        db_session.execute(
            text("SELECT count(*) FROM warehouse_sources WHERE id = :id"),
            {"id": source.id},
        ).scalar_one()
        == 0
    )
    kept = db_session.execute(
        text(
            "SELECT connection_id, connection_name FROM warehouse_analysis_runs "
            "WHERE id = :id"
        ),
        {"id": run.id},
    ).one()
    assert kept.connection_id is None
    assert kept.connection_name == name


def test_deleting_an_experiment_deletes_its_runs(db_session, connection, experiment):
    run = _run(connection, experiment.id)
    db_session.add(run)
    db_session.commit()

    db_session.execute(
        text("DELETE FROM experiments WHERE id = :id"), {"id": experiment.id}
    )
    db_session.commit()

    assert (
        db_session.execute(
            text("SELECT count(*) FROM warehouse_analysis_runs WHERE id = :id"),
            {"id": run.id},
        ).scalar_one()
        == 0
    )


def test_run_times_are_stored_with_their_offset(db_session, connection, experiment):
    start = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    run = _run(
        connection,
        experiment.id,
        window_start=start,
        window_end=start + timedelta(days=7),
    )
    db_session.add(run)
    db_session.commit()
    stored = db_session.execute(
        text("SELECT window_start FROM warehouse_analysis_runs WHERE id = :id"),
        {"id": run.id},
    ).scalar_one()
    assert stored == start
