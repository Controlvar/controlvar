"""Warehouse connections: what is accepted, what is stored, what is returned."""

from __future__ import annotations

import base64
import json
import logging
import traceback
import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from backend.app.main import app
from modules.backend.app import settings as modules_settings
from modules.backend.app.api.v1.endpoints import warehouse_analysis as wa
from modules.backend.app.models.warehouse_analysis_run import WarehouseAnalysisRun
from modules.backend.app.models.warehouse_connection import WarehouseConnection
from modules.backend.app.models.warehouse_source import WarehouseSource
from modules.backend.app.warehouse import bigquery as bq
from modules.backend.tests.integration.warehouse.conftest import (
    ATHENA_BODY,
    WA,
    bigquery_client_factory,
    ts,
)
from modules.backend.tests.unit.warehouse.google_fake import GoogleFake, Script

pytestmark = [pytest.mark.integration, pytest.mark.modules]

KEY_ID = "idsentinel7f3a9c0d1e2b4a5968"
EMAIL = "analysis-reader@acme-analytics.iam.gserviceaccount.com"


def service_account_json(pem: str, **overrides) -> str:
    data = {
        "type": bq.SERVICE_ACCOUNT_TYPE,
        "project_id": "acme-analytics",
        "private_key_id": KEY_ID,
        "private_key": pem,
        "client_email": EMAIL,
        "token_uri": bq.TOKEN_URL,
        "universe_domain": "googleapis.com",
        **overrides,
    }
    return json.dumps(data)


def bigquery_body(pem: str, **overrides) -> dict:
    return {
        "warehouse_type": "bigquery",
        "name": "Prod analytics",
        "billing_project": "acme-analytics",
        "location": "US",
        "max_bytes_per_query": 50_000_000_000,
        "service_account_json": service_account_json(pem),
        **overrides,
    }


@pytest.fixture
def google(wh):
    fake = GoogleFake(Script({"token": ["token_ok"], "dry_run": ["dry_run_select"]}))
    wh.google_factory = bigquery_client_factory(fake)
    return fake


def test_disabled_connector_create_422(wh):
    wh.enabled = frozenset()
    admin = wh.as_("ADMIN")
    before = wh.db.query(WarehouseConnection).count()
    response = admin.post(f"{WA}/connections", json=ATHENA_BODY)
    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "connector_disabled",
        "message": "Amazon Athena isn't available on this deployment yet.",
    }
    assert wh.db.query(WarehouseConnection).count() == before


def test_a_connector_disabled_after_creation_is_refused_at_use(wh):
    admin = wh.as_("ADMIN")
    connection = wh.athena_connection(admin)
    wh.enabled = frozenset()
    response = wh.as_("ADMIN").post(f"{WA}/connections/{connection['id']}/test")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "connector_disabled"
    listed = wh.as_("ADMIN").get(f"{WA}/connections/{connection['id']}")
    assert listed.json()["enabled"] is False


def test_the_connector_list_follows_the_enabled_set(wh):
    viewer = wh.as_("VIEWER")
    listed = viewer.get(f"{WA}/connectors").json()["connectors"]
    assert {c["warehouse_type"]: c["enabled"] for c in listed} == {
        "bigquery": True,
        "snowflake": False,
        "athena": True,
    }
    # Without the test's override: what this code ships with.
    app.dependency_overrides.pop(wa.get_enabled_connectors)
    shipped = viewer.get(f"{WA}/connectors").json()["connectors"]
    assert {c["warehouse_type"]: c["enabled"] for c in shipped} == {
        "bigquery": True,
        "snowflake": True,
        "athena": False,
    }


def test_as_shipped_snowflake_and_bigquery_are_created_and_athena_is_422(
    wh, google, service_account_pem
):
    """With no override, Snowflake and BigQuery connections are created; Athena is not."""
    admin = wh.as_("ADMIN")
    app.dependency_overrides.pop(wa.get_enabled_connectors)
    before = wh.db.query(WarehouseConnection).count()
    created = admin.post(
        f"{WA}/connections",
        json={
            "warehouse_type": "snowflake",
            "name": "Snow",
            "account": "MYORG-MYACCOUNT",
            "user": "CONTROLVAR_READER",
            "role": "ANALYSIS_READER",
            "warehouse": "ANALYSIS_WH",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["enabled"] is True
    created = admin.post(f"{WA}/connections", json=bigquery_body(service_account_pem))
    assert created.status_code == 201, created.text
    assert created.json()["enabled"] is True
    refused = admin.post(f"{WA}/connections", json=ATHENA_BODY)
    assert refused.status_code == 422
    assert refused.json()["detail"] == {
        "code": "connector_disabled",
        "message": "Amazon Athena isn't available on this deployment yet.",
    }
    assert wh.db.query(WarehouseConnection).count() == before + 2


def test_bigquery_connection_create_and_test(wh, google, service_account_pem):
    admin = wh.as_("ADMIN")
    response = admin.post(f"{WA}/connections", json=bigquery_body(service_account_pem))
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["parameters"] == {
        "billing_project": "acme-analytics",
        "location": "US",
        "client_email": EMAIL,
    }
    assert created["credentials_status"] == "ok"
    assert created["worst_case_bytes_per_day"] == 20 * 11 * 50_000_000_000
    assert created["worst_case_seconds_per_day"] is None
    tested = admin.post(f"{WA}/connections/{created['id']}/test")
    assert tested.status_code == 200, tested.text
    assert tested.json() == {
        "ok": True,
        "warehouse_type": "bigquery",
        "promoted_pending_key": False,
    }
    # The stored key holds exactly the four fields the connector keeps.
    row = wh.db.get(WarehouseConnection, uuid.UUID(created["id"]))
    assert set(json.loads(row.get_credentials())) == set(bq.ServiceAccountKey.FIELDS)


def test_a_connection_can_be_tested_before_it_is_saved(wh, google, service_account_pem):
    admin = wh.as_("ADMIN")
    before = wh.db.query(WarehouseConnection).count()
    body = bigquery_body(service_account_pem)
    body.pop("name")
    body["name"] = "Trial"
    response = admin.post(f"{WA}/connections/test", json=body)
    assert response.status_code == 200, response.text
    assert wh.db.query(WarehouseConnection).count() == before
    assert [r.host for r in google.requests] == [
        "oauth2.googleapis.com",
        "bigquery.googleapis.com",
    ]


def test_a_key_naming_another_token_endpoint_is_refused(
    wh, google, service_account_pem
):
    admin = wh.as_("ADMIN")
    body = bigquery_body(
        service_account_pem,
        service_account_json=service_account_json(
            service_account_pem, token_uri="https://example.test/token"
        ),
    )
    response = admin.post(f"{WA}/connections", json=body)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "token_endpoint_not_allowed"
    assert google.requests == []


@pytest.mark.parametrize(
    "field", ["output_location", "external_id", "password", "credentials", "sql"]
)
def test_fields_outside_the_contract_are_refused(wh, field):
    response = wh.as_("ADMIN").post(
        f"{WA}/connections", json={**ATHENA_BODY, field: "s3://bucket/prefix"}
    )
    assert response.status_code == 422


def test_limits_above_the_operator_ceiling_are_refused(wh, monkeypatch):
    monkeypatch.setattr(
        modules_settings.settings, "WAREHOUSE_MAX_BYTES_PER_QUERY", 10**11
    )
    monkeypatch.setattr(modules_settings.settings, "WAREHOUSE_MAX_RUNS_PER_DAY", 50)
    admin = wh.as_("ADMIN")
    for override, field in (
        ({"max_bytes_per_query": 10**11 + 1}, "max_bytes_per_query"),
        ({"max_runs_per_day": 51}, "max_runs_per_day"),
        ({"query_timeout_seconds": 1801}, "query_timeout_seconds"),
    ):
        response = admin.post(f"{WA}/connections", json={**ATHENA_BODY, **override})
        assert response.status_code == 422, override
        assert response.json()["detail"] == {
            "code": "limit_too_high",
            "message": response.json()["detail"]["message"],
            "field": field,
        }


def test_athena_external_id_is_generated(wh):
    created = wh.athena_connection(wh.as_("ADMIN"))
    assert created["external_id"].startswith("exp-")
    assert len(created["external_id"]) == 36
    assert created["credentials_status"] == "ok"


def test_snowflake_admin_roles_are_refused(wh):
    wh.enabled = frozenset({"snowflake"})
    for role in ("ACCOUNTADMIN", "sysadmin", "OrgAdmin"):
        response = wh.as_("ADMIN").post(
            f"{WA}/connections",
            json={
                "warehouse_type": "snowflake",
                "name": "Snow",
                "account": "MYORG-MYACCOUNT",
                "user": "CONTROLVAR",
                "role": role,
                "warehouse": "ANALYSIS_WH",
            },
        )
        assert response.status_code == 422, role
        assert response.json()["detail"]["code"] == "role_not_allowed"


def test_absent_keys_503(wh, google, service_account_pem, monkeypatch):
    monkeypatch.setattr(modules_settings.settings, "WAREHOUSE_CREDENTIALS_KEYS", None)
    admin = wh.as_("ADMIN")
    before = wh.db.query(WarehouseConnection).count()
    response = admin.post(f"{WA}/connections", json=bigquery_body(service_account_pem))
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "credentials_unavailable"
    assert (
        "WAREHOUSE_CREDENTIALS_KEYS is not set" in response.json()["detail"]["message"]
    )
    assert wh.db.query(WarehouseConnection).count() == before


def test_keys_removed_after_saving(wh, google, service_account_pem, monkeypatch):
    admin = wh.as_("ADMIN")
    created = admin.post(
        f"{WA}/connections", json=bigquery_body(service_account_pem)
    ).json()
    monkeypatch.setattr(
        modules_settings.settings,
        "WAREHOUSE_CREDENTIALS_KEYS",
        Fernet.generate_key().decode(),
    )
    shown = admin.get(f"{WA}/connections/{created['id']}").json()
    assert shown["credentials_status"] == "needs_new_credentials"
    tested = admin.post(f"{WA}/connections/{created['id']}/test")
    assert tested.status_code == 409
    assert tested.json()["detail"]["code"] == "credentials_undecryptable"
    monkeypatch.setattr(modules_settings.settings, "WAREHOUSE_CREDENTIALS_KEYS", None)
    shown = admin.get(f"{WA}/connections/{created['id']}").json()
    assert shown["credentials_status"] == "unavailable"
    tested = admin.post(f"{WA}/connections/{created['id']}/test")
    assert tested.status_code == 503


def test_a_connection_keeps_its_type(wh):
    admin = wh.as_("ADMIN")
    created = wh.athena_connection(admin)
    response = admin.put(
        f"{WA}/connections/{created['id']}",
        json={
            "warehouse_type": "bigquery",
            "name": "x",
            "billing_project": "acme-analytics",
            "location": "US",
            "max_bytes_per_query": 10**10,
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "warehouse_type_immutable"
    regenerate = admin.post(f"{WA}/connections/{created['id']}/regenerate-key")
    assert regenerate.status_code == 422
    assert regenerate.json()["detail"]["code"] == "not_applicable"


# -- credentials never come back ---------------------------------------------------


def _forms(secret: str) -> list[str]:
    raw = secret.encode()
    return [
        secret,
        repr(secret),
        base64.b64encode(raw).decode(),
        base64.urlsafe_b64encode(raw).decode(),
    ]


def _pem_line(pem: str) -> str:
    """One full 64-character line from the middle of the PEM's base64 body.

    A key that got out -- in a JSON string with ``\n`` escapes, in a log line,
    base64-encoded again -- still carries its lines whole, so one line is a
    needle that survives every encoding a whole-key needle would miss."""
    lines = [line for line in pem.splitlines() if "-----" not in line]
    return lines[len(lines) // 2]


def _uvicorn_access_line(response) -> str:
    request = response.request
    target = request.url.raw_path.decode()
    return (
        f'127.0.0.1:50000 - "{request.method} {target} HTTP/1.1" {response.status_code}'
    )


def _record_text(record: logging.LogRecord) -> str:
    parts = [record.getMessage(), repr(record.__dict__)]
    if record.exc_info:
        parts.append("".join(traceback.format_exception(*record.exc_info)))
    return "\n".join(parts)


def _module_rows_text(wh) -> str:
    """Every row of every warehouse table and the audit log, as text."""
    chunks = []
    for table in (
        "warehouse_connections",
        "warehouse_sources",
        "warehouse_analysis_runs",
        "audit_events_v2",
    ):
        for row in wh.db.execute(text(f"SELECT * FROM test_experimentation.{table}")):  # nosec B608 - fixed table names
            for value in row:
                if isinstance(value, (bytes, memoryview)):
                    value = bytes(value)
                    chunks.append(value.decode("latin-1"))
                    try:
                        chunks.append(base64.urlsafe_b64decode(value).decode("latin-1"))
                    except Exception:
                        pass
                else:
                    chunks.append(str(value))
    return "\n".join(chunks)


def test_credential_sentinels_absent(wh, google, service_account_pem, caplog):
    """A pasted key never comes back: not in a response (including a refused
    one), a log record, a traceback, an access-log line, the audit log, or any
    column of the warehouse tables in any encoding."""
    caplog.set_level(logging.DEBUG)
    admin = wh.as_("ADMIN")
    body = bigquery_body(service_account_pem)
    responses = []
    created = admin.post(f"{WA}/connections", json=body)
    responses.append(created)
    connection_id = created.json()["id"]
    responses.append(admin.get(f"{WA}/connections"))
    responses.append(admin.get(f"{WA}/connections/{connection_id}"))
    responses.append(admin.post(f"{WA}/connections/{connection_id}/test"))
    update = dict(body)
    update["name"] = "Renamed"
    responses.append(admin.put(f"{WA}/connections/{connection_id}", json=update))
    responses.append(admin.post(f"{WA}/connections/test", json=body))
    # Refused bodies that carry the key: a bad field, an unknown field, an
    # unknown type, a model-level refusal and malformed JSON.
    responses.append(
        admin.post(f"{WA}/connections", json={**body, "billing_project": "BAD"})
    )
    responses.append(admin.post(f"{WA}/connections", json={**body, "extra_field": 1}))
    responses.append(
        admin.post(f"{WA}/connections", json={**body, "warehouse_type": "redshift"})
    )
    responses.append(
        admin.post(
            f"{WA}/connections",
            json={
                **body,
                "service_account_json": body["service_account_json"] + "\x00",
            },
        )
    )
    responses.append(
        admin.post(
            f"{WA}/connections",
            content=json.dumps(body)[:-5],
            headers={"Content-Type": "application/json"},
        )
    )
    assert [r.status_code for r in responses] == [
        201,
        200,
        200,
        200,
        200,
        200,
        422,
        422,
        422,
        422,
        422,
    ]

    secrets = [_pem_line(service_account_pem), KEY_ID, body["service_account_json"]]
    haystacks = {
        "responses": "\n".join(r.text for r in responses),
        "headers": "\n".join(repr(dict(r.headers)) for r in responses),
        "logs": "\n".join(_record_text(r) for r in caplog.records),
        "access log": "\n".join(_uvicorn_access_line(r) for r in responses),
        "database": _module_rows_text(wh),
    }
    found = [
        (where, secret[:12])
        for where, haystack in haystacks.items()
        for secret in secrets
        for form in _forms(secret)
        if form in haystack
    ]
    assert found == []
    # The probe is not vacuous: every secret is in what was sent.
    sent = json.dumps(body)
    assert all(any(form in sent for form in _forms(s)) for s in secrets[:2])


def _columns_holding(wh, needle: bytes) -> list[tuple[str, str]]:
    """Every (table, column) in the test schema whose value contains ``needle``."""
    columns = wh.db.execute(
        text(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'test_experimentation' AND data_type IN "
            "('bytea', 'text', 'character varying', 'json', 'jsonb')"
        )
    ).all()
    hits = []
    for table, column, kind in columns:
        if kind == "bytea":
            condition = f'position(:needle in "{column}") > 0'
            params = {"needle": needle}
        else:
            condition = f'strpos("{column}"::text, :needle) > 0'
            params = {"needle": needle.decode("latin-1")}
        found = wh.db.execute(
            text(
                f'SELECT 1 FROM test_experimentation."{table}" WHERE {condition} LIMIT 1'
            ),  # nosec B608 - names from information_schema
            params,
        ).first()
        if found:
            hits.append((table, column))
    return hits


def test_delete_connection_leaves_no_ciphertext(wh, google, service_account_pem):
    """Deleting a connection deletes the row: its ciphertext is nowhere in the
    database afterwards, its sources are gone, and its runs keep its name."""
    admin = wh.as_("ADMIN")
    created = admin.post(
        f"{WA}/connections", json=bigquery_body(service_account_pem)
    ).json()
    connection_id = uuid.UUID(created["id"])
    source = admin.post(
        f"{WA}/sources",
        json={
            "kind": "metric",
            "connection_id": created["id"],
            "name": "Orders",
            "table": "acme-analytics.shop.orders",
            "columns": {"unit_id": "user_id", "event_at": "event_at"},
            "metric_type": "proportion",
        },
    )
    assert source.status_code == 201, source.text
    run = WarehouseAnalysisRun(
        kind="preview",
        status="succeeded",
        connection_id=connection_id,
        connection_name="Prod analytics",
        warehouse_type="bigquery",
        request={},
    )
    wh.db.add(run)
    wh.db.commit()
    row = wh.db.get(WarehouseConnection, connection_id)
    ciphertext = bytes(row.credentials_ciphertext)
    assert _columns_holding(wh, ciphertext) == [
        ("warehouse_connections", "credentials_ciphertext")
    ]

    response = admin.delete(f"{WA}/connections/{created['id']}")
    assert response.status_code == 204
    wh.db.expire_all()
    assert _columns_holding(wh, ciphertext) == []
    assert _columns_holding(wh, _pem_line(service_account_pem).encode()) == []
    assert wh.db.get(WarehouseConnection, connection_id) is None
    assert wh.db.get(WarehouseSource, uuid.UUID(source.json()["id"])) is None
    wh.db.refresh(run)
    assert run.connection_id is None and run.connection_name == "Prod analytics"
    assert admin.get(f"{WA}/connections/{created['id']}").status_code == 404


def _audit(wh, resource_type: str, resource_id: str):
    return wh.db.execute(
        text(
            "SELECT action, old_value, new_value FROM test_experimentation.audit_events_v2 "
            "WHERE resource_type = :type AND resource_id = :id ORDER BY timestamp"
        ),
        {"type": resource_type, "id": resource_id},
    ).all()


def test_connection_changes_are_audited_without_credentials(
    wh, google, service_account_pem
):
    admin = wh.as_("ADMIN")
    body = bigquery_body(service_account_pem)
    created = admin.post(f"{WA}/connections", json=body).json()
    admin.put(f"{WA}/connections/{created['id']}", json={**body, "name": "Renamed"})
    admin.delete(f"{WA}/connections/{created['id']}")
    rows = _audit(wh, "warehouse_connection", created["id"])
    assert [str(r.action).split(".")[-1] for r in rows] == [
        "CREATE",
        "UPDATE",
        "DELETE",
    ]
    assert rows[0].new_value["parameters"] == {
        "billing_project": "acme-analytics",
        "location": "US",
        "client_email": EMAIL,
    }
    assert rows[1].old_value["name"] == "Prod analytics"
    assert rows[1].new_value["name"] == "Renamed"
    assert rows[2].old_value["name"] == "Renamed"
    assert "private_key" not in str(rows) and KEY_ID not in str(rows)
