"""A flag update's compliance event shows only the status changes that happened (#2066).

The event for ``PUT /feature-flags/{id}`` holds the flag's status before and
after. The "before" was written as ``str(FeatureFlagStatus.ACTIVE)``
(``FeatureFlagStatus.ACTIVE``) and the "after" as the response's ``active``, so
every update, a rename included, looked like a status change. Driven without a
database: the service is replaced by one that applies the change and calls the
endpoint's ``record`` callback, and the audit writer is a recorder.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend.app.api.v1.endpoints import feature_flags
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.schemas.feature_flag import FeatureFlagUpdate

ENDPOINT = "backend.app.api.v1.endpoints.feature_flags"


def _flag(status: FeatureFlagStatus) -> FeatureFlag:
    now = datetime.now(timezone.utc)
    return FeatureFlag(
        id=uuid.uuid4(),
        key="checkout",
        name="Checkout",
        description=None,
        status=status,
        rollout_percentage=30,
        default_value=False,
        value_type="boolean",
        on_value=None,
        off_value=None,
        targeting_rules=None,
        tags=[],
        owner_id=uuid.uuid4(),
        created_at=now,
        updated_at=now,
    )


async def _update(flag: FeatureFlag, body: dict, new_status=None):
    """Run the endpoint; return the (old_value, new_value) the compliance event got."""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = flag
    logged: dict = {}

    def apply(row, update, record=None):
        for field, value in update.model_dump(exclude_unset=True).items():
            setattr(row, field, value)
        if new_status is not None:
            row.status = new_status
        record(row)
        return row

    service = MagicMock()
    service.return_value.update_feature_flag.side_effect = apply
    log = MagicMock(
        side_effect=lambda **kw: logged.update(old=kw["old_value"], new=kw["new_value"])
    )

    @contextmanager
    def change(*_a, **_k):
        yield

    with (
        patch(f"{ENDPOINT}.FeatureFlagService", service),
        patch(f"{ENDPOINT}.can_act_on_feature_flag", return_value=True),
        patch(f"{ENDPOINT}.AuditLogService") as audit_log,
        patch(f"{ENDPOINT}.AuditService") as audit_service,
    ):
        audit_log.return_value.log = log
        audit_service.audit_with_change.side_effect = change
        await feature_flags.update_feature_flag(
            flag_id=flag.id,
            feature_flag_in=FeatureFlagUpdate(**body),
            db=db,
            current_user=SimpleNamespace(id=uuid.uuid4()),
            _skip_cache=None,
        )
    return logged["old"], logged["new"]


@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [FeatureFlagStatus.ACTIVE, FeatureFlagStatus.INACTIVE]
)
async def test_a_rename_records_the_same_status_before_and_after(status):
    """Fails on 1a63851f: before ``FeatureFlagStatus.ACTIVE``, after ``active``."""
    old, new = await _update(_flag(status), {"name": "Checkout v2"})

    assert old["status"] == new["status"] == status.value.lower()
    assert (old["name"], new["name"]) == ("Checkout", "Checkout v2")


@pytest.mark.regression
@pytest.mark.asyncio
async def test_a_real_status_change_is_still_recorded():
    """Control: an update that does change the status shows both sides."""
    old, new = await _update(
        _flag(FeatureFlagStatus.ACTIVE),
        {"name": "Checkout"},
        new_status=FeatureFlagStatus.INACTIVE,
    )

    assert (old["status"], new["status"]) == ("active", "inactive")
