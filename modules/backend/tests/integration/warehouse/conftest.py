"""The warehouse analysis API against a real database and a fake warehouse.

:class:`Harness` drives the real application (``backend.app.main:app``) as a
non-superuser of each role, with these seams overridden:

* the connectors enabled on the "deployment" (``ENABLED_CONNECTORS`` itself is
  pinned by ``test_enabled_connectors_exact``);
* the client factory: an ``athena``-typed connection is served by an
  in-memory DuckDB warehouse (DuckDB's identifier rules are Athena's), and a
  ``bigquery`` one by the real BigQuery connector over the recorded-response
  Google fake (nothing leaves the machine);
* a fresh warehouse executor, a clock the test sets, and a database session
  factory for the jobs bound to the test database;
* the proportion estimator, by the core function wrapped to record the
  counts it is given (:mod:`.recording_estimator`).

Nothing here reaches a network.
"""

from __future__ import annotations

import dataclasses
import threading
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from backend.app.main import app
from backend.app.models.experiment import Experiment, ExperimentStatus, Variant
from backend.app.models.user import User, UserRole
from backend.tests.integration.conftest import HASHED_PASSWORD, make_client_for_user
from modules.backend.app import settings as modules_settings
from modules.backend.app.api.v1.endpoints import warehouse_analysis as wa
from modules.backend.app.services.warehouse_clients import (
    NUMERIC_TYPES,
    TIME_TYPES,
    ConnectionSpec,
    WarehouseClient,
)
from modules.backend.app.services.warehouse_query_builder import BIGQUERY_SQL
from modules.backend.app.warehouse.executor import WarehouseExecutor
from modules.backend.tests.integration.warehouse import recording_estimator
from modules.backend.tests.unit.warehouse.duckdb_adapter import (
    DUCKDB_SQL,
    DuckDBWarehouse,
)

WA = "/api/v1/warehouse/analysis"

#: DuckDB's tokens plus the epoch-seconds one the previews need.
DUCKDB_PREVIEW_SQL = dataclasses.replace(
    DUCKDB_SQL, epoch_seconds=lambda e: f"CAST(floor(epoch({e})) AS BIGINT)"
)

DUCKDB_TIME_TYPES = frozenset({"TIMESTAMP WITH TIME ZONE", "TIMESTAMP"})
DUCKDB_NUMERIC_TYPES = frozenset({"DOUBLE", "INTEGER", "BIGINT", "DECIMAL"})

ATHENA_BODY: Dict[str, Any] = {
    "warehouse_type": "athena",
    "name": "Lake",
    "region": "us-east-1",
    "role_arn": "arn:aws:iam::123456789012:role/controlvar-reader",
    "workgroup": "primary",
    "database": "main",
    "max_bytes_per_query": 10_000_000_000,
}

EXPOSURE_COLUMNS = [
    ("user_id", "VARCHAR"),
    ("experiment_key", "VARCHAR"),
    ("variant", "VARCHAR"),
    ("exposed_at", "TIMESTAMPTZ"),
]
EVENT_COLUMNS = [
    ("user_id", "VARCHAR"),
    ("event_at", "TIMESTAMPTZ"),
    ("amount", "DOUBLE"),
]


def ts(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=timezone.utc)


class DuckAdapter:
    """A warehouse adapter over DuckDB, with the connectors' three calls."""

    def __init__(self, warehouse: DuckDBWarehouse, harness: "Harness") -> None:
        self._warehouse = warehouse
        self._harness = harness
        self._lock = threading.Lock()

    def _hold(self) -> None:
        gate = self._harness.gate
        if gate is not None:
            self._harness.entered.release()
            gate.wait(30)

    def run_query(self, built, deadline):
        self._hold()
        self._harness.queries.append(built)
        if self._harness.fail_with is not None:
            raise self._harness.fail_with
        with self._lock:
            rows = self._warehouse.fetch(built)
        return SimpleNamespace(rows=tuple(rows), elapsed_ms=3)

    def table_columns(self, parts, deadline):
        self._hold()
        schema, table = parts
        with self._lock:
            found = self._warehouse.con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
                [schema, table],
            ).fetchall()
        return tuple((name, kind) for name, kind in found)

    def check_connection(self, deadline):
        self._hold()
        if self._harness.fail_with is not None:
            raise self._harness.fail_with
        return {"ok": True}


class Harness:
    def __init__(self, db_session: Session, monkeypatch) -> None:
        self.db = db_session
        self.monkeypatch = monkeypatch
        self.warehouse = DuckDBWarehouse()
        self.executor = WarehouseExecutor(4, per_organisation=2)
        self.now = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
        self.enabled = frozenset({"athena", "bigquery"})
        self.gate: Optional[threading.Event] = None
        self.entered = threading.Semaphore(0)
        self.fail_with: Optional[BaseException] = None
        self.queries: List[Any] = []
        self.google_factory: Optional[Callable] = None
        self.specs: List[ConnectionSpec] = []
        self.users: Dict[str, User] = {}
        engine = db_session.get_bind()
        maker = sessionmaker(bind=engine, autocommit=False, autoflush=False)

        def session_factory() -> Session:
            session = maker()
            session.execute(text("SET search_path TO test_experimentation"))
            return session

        self.session_factory = session_factory
        recording_estimator.CALLS.clear()

    # -- seams -------------------------------------------------------------

    def client_factory(self, spec: ConnectionSpec) -> WarehouseClient:
        self.specs.append(spec)
        if spec.warehouse_type == "bigquery":
            assert self.google_factory is not None, "no Google fake configured"
            return self.google_factory(spec)
        return WarehouseClient(
            spec.warehouse_type,
            DuckAdapter(self.warehouse, self),
            DUCKDB_PREVIEW_SQL,
            DUCKDB_TIME_TYPES,
            DUCKDB_NUMERIC_TYPES,
        )

    def install(self) -> None:
        overrides = app.dependency_overrides
        overrides[wa.get_enabled_connectors] = lambda: self.enabled
        overrides[wa.get_client_factory] = lambda: self.client_factory
        overrides[wa.get_warehouse_executor] = lambda: self.executor
        overrides[wa.get_job_session_factory] = lambda: self.session_factory
        overrides[wa.get_clock] = lambda: lambda: self.now
        overrides[wa.get_estimator] = lambda: recording_estimator.binomial_metric_result

    # -- users -------------------------------------------------------------

    def user(self, role: str, *, superuser: bool = False) -> User:
        key = f"{role}:{superuser}"
        if key not in self.users:
            suffix = uuid.uuid4().hex[:8]
            user = User(
                username=f"wh_{role}_{suffix}",
                email=f"wh_{role}_{suffix}@int.test",
                full_name=f"Warehouse {role}",
                hashed_password=HASHED_PASSWORD,
                is_active=True,
                is_superuser=superuser,
                role=UserRole[role],
            )
            self.db.add(user)
            self.db.commit()
            self.db.refresh(user)
            self.users[key] = user
        return self.users[key]

    def as_(self, role: str, *, superuser: bool = False):
        """A client authenticated as a non-superuser of ``role``."""
        client = make_client_for_user(self.db, self.user(role, superuser=superuser))
        self.install()
        return client

    # -- data --------------------------------------------------------------

    def experiment(
        self,
        *,
        key: Optional[str] = "checkout",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        optimization_type: Optional[str] = None,
        allocations=(50, 50),
    ) -> Experiment:
        experiment = Experiment(
            name=f"Checkout {uuid.uuid4().hex[:6]}",
            key=key and f"{key}-{uuid.uuid4().hex[:6]}",
            status=ExperimentStatus.ACTIVE,
            owner_id=self.user("ADMIN").id,
            description="warehouse",
            hypothesis="warehouse",
            start_date=(start or ts(1)).replace(tzinfo=None),
            end_date=end.replace(tzinfo=None) if end else None,
        )
        if optimization_type is not None:
            experiment.optimization_type = optimization_type
        self.db.add(experiment)
        self.db.commit()
        for name, is_control, allocation in (
            ("control", True, allocations[0]),
            ("treatment", False, allocations[1]),
        ):
            self.db.add(
                Variant(
                    experiment_id=experiment.id,
                    name=name,
                    is_control=is_control,
                    traffic_allocation=allocation,
                )
            )
        self.db.commit()
        self.db.refresh(experiment)
        return experiment

    def exposures(self, rows) -> None:
        self.warehouse.create("exposures", EXPOSURE_COLUMNS, rows)

    def events(self, rows, table: str = "events") -> None:
        self.warehouse.create(table, EVENT_COLUMNS, rows)

    # -- API helpers --------------------------------------------------------

    def athena_connection(self, client, **overrides) -> Dict[str, Any]:
        response = client.post(f"{WA}/connections", json={**ATHENA_BODY, **overrides})
        assert response.status_code == 201, response.text
        return response.json()

    def assignment_source(
        self, client, connection_id: str, **overrides
    ) -> Dict[str, Any]:
        body = {
            "kind": "assignment",
            "connection_id": connection_id,
            "name": "Exposures",
            "table": "main.exposures",
            "columns": {
                "unit_id": "user_id",
                "experiment_key": "experiment_key",
                "variant": "variant",
                "exposed_at": "exposed_at",
            },
            **overrides,
        }
        response = client.post(f"{WA}/sources", json=body)
        assert response.status_code == 201, response.text
        return response.json()

    def metric_source(self, client, connection_id: str, **overrides) -> Dict[str, Any]:
        body = {
            "kind": "metric",
            "connection_id": connection_id,
            "name": "Purchases",
            "table": "main.events",
            "columns": {"unit_id": "user_id", "event_at": "event_at"},
            "metric_type": "proportion",
            "conversion_window_hours": 168,
            **overrides,
        }
        response = client.post(f"{WA}/sources", json=body)
        assert response.status_code == 201, response.text
        return response.json()

    def validate(self, client, source_id: str) -> Dict[str, Any]:
        response = client.post(f"{WA}/sources/{source_id}/validate")
        assert response.status_code == 200, response.text
        return response.json()

    def ready(self, client) -> Dict[str, str]:
        """A connection with a validated assignment and metric source."""
        connection = self.athena_connection(client)
        assignment = self.assignment_source(client, connection["id"])
        metric = self.metric_source(client, connection["id"])
        self.validate(client, assignment["id"])
        self.validate(client, metric["id"])
        return {
            "connection_id": connection["id"],
            "assignment_source_id": assignment["id"],
            "metric_source_id": metric["id"],
        }

    def start_run(self, client, experiment: Experiment, ids: Dict[str, str], **body):
        return client.post(
            f"{WA}/experiments/{experiment.id}/runs",
            json={
                "connection_id": ids["connection_id"],
                "assignment_source_id": ids["assignment_source_id"],
                "metric_source_ids": [ids["metric_source_id"]],
                **body,
            },
        )

    def wait_for_run(
        self, client, run_id: str, timeout: float = 20.0
    ) -> Dict[str, Any]:
        import time

        deadline = time.monotonic() + timeout
        while True:
            response = client.get(f"{WA}/runs/{run_id}")
            assert response.status_code == 200, response.text
            run = response.json()
            if run["status"] in ("succeeded", "failed"):
                return run
            assert time.monotonic() < deadline, f"run did not finish: {run}"
            time.sleep(0.05)

    def close(self) -> None:
        if self.gate is not None:
            self.gate.set()
        self.executor.shutdown(wait=True)
        app.dependency_overrides.clear()


@pytest.fixture
def wh(db_session, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(modules_settings.settings, "WAREHOUSE_CREDENTIALS_KEYS", key)
    harness = Harness(db_session, monkeypatch)
    harness.fernet_key = key
    yield harness
    harness.close()


@pytest.fixture(scope="session")
def service_account_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def bigquery_client_factory(fake) -> Callable[[ConnectionSpec], WarehouseClient]:
    """The real BigQuery connector over a recorded-response Google fake."""
    from modules.backend.app.warehouse.bigquery import (
        AccessTokenCache,
        BigQueryAdapter,
        BigQueryConnection,
        ServiceAccountKey,
    )
    from modules.backend.tests.unit.warehouse.google_fake import client_factory

    def make(spec: ConnectionSpec) -> WarehouseClient:
        adapter = BigQueryAdapter(
            BigQueryConnection(
                billing_project=spec.parameters["billing_project"],
                location=spec.parameters["location"],
                max_bytes_per_query=spec.max_bytes_per_query,
                query_timeout_seconds=spec.query_timeout_seconds,
            ),
            ServiceAccountKey.from_blob(spec.credential),
            client_factory=client_factory(fake),
            token_cache=AccessTokenCache(),
            sleeper=lambda seconds: None,
        )
        return WarehouseClient(
            "bigquery",
            adapter,
            BIGQUERY_SQL,
            TIME_TYPES["bigquery"],
            NUMERIC_TYPES["bigquery"],
        )

    return make


def days_ago(harness: Harness, days: int) -> datetime:
    return harness.now - timedelta(days=days)
