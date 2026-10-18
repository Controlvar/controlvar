"""Snowflake connections: the platform generates the key pair and shows only
the public half; a regenerated key waits for a passing test."""

from __future__ import annotations

import base64
import uuid

import pytest
from cryptography.hazmat.primitives import serialization

from modules.backend.app.models.warehouse_connection import WarehouseConnection
from modules.backend.app.warehouse.snowflake import SnowflakeKey
from modules.backend.tests.integration.warehouse.conftest import WA

pytestmark = [pytest.mark.integration, pytest.mark.modules]

BODY = {
    "warehouse_type": "snowflake",
    "name": "Snow",
    "account": "MYORG-MYACCOUNT",
    "user": "CONTROLVAR_READER",
    "role": "ANALYSIS_READER",
    "warehouse": "ANALYSIS_WH",
}


def _private_forms(blob: bytes) -> list[str]:
    key = SnowflakeKey.from_blob(blob)
    pem = key._private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    middle = [line for line in pem.splitlines() if "-----" not in line][5]
    return [base64.b64encode(blob).decode()[:80], middle, blob.hex()[:80]]


def test_snowflake_key_pair_lifecycle(wh):
    wh.enabled = frozenset({"snowflake"})
    admin = wh.as_("ADMIN")
    created = admin.post(f"{WA}/connections", json=BODY)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["parameters"] == {
        "account": "MYORG-MYACCOUNT",
        "user": "CONTROLVAR_READER",
        "role": "ANALYSIS_READER",
        "warehouse": "ANALYSIS_WH",
    }
    assert body["worst_case_seconds_per_day"] == 20 * 11 * 300
    shown = body["public_key"]
    assert shown["statement"] == (
        f"ALTER USER CONTROLVAR_READER SET RSA_PUBLIC_KEY='{shown['public_key']}';"
    )
    assert shown["public_key_fingerprint"] == body["public_key_fingerprint"]
    assert shown["public_key_fingerprint"].startswith("SHA256:")

    row = wh.db.get(WarehouseConnection, uuid.UUID(body["id"]))
    first_blob = row.get_credentials()
    assert (
        SnowflakeKey.from_blob(first_blob).fingerprint == body["public_key_fingerprint"]
    )

    regenerated = admin.post(f"{WA}/connections/{body['id']}/regenerate-key")
    assert regenerated.status_code == 200, regenerated.text
    pending = regenerated.json()
    assert pending["credentials_status"] == "pending_key"
    assert pending["public_key_fingerprint"] == body["public_key_fingerprint"]
    assert pending["public_key"]["statement"].startswith(
        "ALTER USER CONTROLVAR_READER SET RSA_PUBLIC_KEY_2='"
    )
    new_fingerprint = pending["pending_public_key_fingerprint"]
    assert new_fingerprint == pending["public_key"]["public_key_fingerprint"]
    wh.db.expire_all()
    row = wh.db.get(WarehouseConnection, uuid.UUID(body["id"]))
    second_blob = row.get_credentials(pending=True)

    tested = admin.post(f"{WA}/connections/{body['id']}/test")
    assert tested.status_code == 200, tested.text
    assert tested.json()["promoted_pending_key"] is True
    # The test signed in with the pending key, not the current one.
    assert wh.specs[-1].credential == second_blob
    after = admin.get(f"{WA}/connections/{body['id']}").json()
    assert after["credentials_status"] == "ok"
    assert after["public_key_fingerprint"] == new_fingerprint
    assert after["pending_public_key_fingerprint"] is None

    # The next key goes back into the first slot.
    third = admin.post(f"{WA}/connections/{body['id']}/regenerate-key").json()
    assert third["public_key"]["statement"].startswith(
        "ALTER USER CONTROLVAR_READER SET RSA_PUBLIC_KEY='"
    )

    # No response carried a private key, in any of its encodings.
    text = "\n".join(r.text for r in (created, regenerated, tested))
    text += str(after) + str(third)
    for blob in (first_blob, second_blob):
        for form in _private_forms(blob):
            assert form not in text


def test_a_failed_test_keeps_the_current_key(wh):
    from modules.backend.app.warehouse.errors import WarehouseError, WarehouseErrorCode

    wh.enabled = frozenset({"snowflake"})
    admin = wh.as_("ADMIN")
    body = admin.post(f"{WA}/connections", json=BODY).json()
    admin.post(f"{WA}/connections/{body['id']}/regenerate-key")
    wh.fail_with = WarehouseError(WarehouseErrorCode.AUTH_FAILED, warehouse="snowflake")
    tested = admin.post(f"{WA}/connections/{body['id']}/test")
    assert tested.status_code == 502
    assert tested.json()["detail"]["code"] == "auth_failed"
    after = admin.get(f"{WA}/connections/{body['id']}").json()
    assert after["credentials_status"] == "pending_key"
    assert after["public_key_fingerprint"] == body["public_key_fingerprint"]


def test_snowflake_is_tested_after_it_is_created(wh):
    """There is no key before the connection exists, so no test-before-save."""
    wh.enabled = frozenset({"snowflake"})
    response = wh.as_("ADMIN").post(f"{WA}/connections/test", json=BODY)
    assert response.status_code == 422
