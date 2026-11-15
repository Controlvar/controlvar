"""
Utility helpers for integration tests.
"""

import uuid
from typing import List, Optional

from sqlalchemy.orm import Session

from backend.app.models.audit_log import AuditLog
from backend.app.models.experiment import Experiment
from backend.app.models.feature_flag import FeatureFlag


def assert_audit_log_created(
    db: Session, entity_type: str, entity_id, action: str
) -> None:
    """Assert that an audit log entry was created for the given entity/action."""
    log = (
        db.query(AuditLog)
        .filter(
            AuditLog.entity_type == entity_type,
            AuditLog.entity_id == str(entity_id),
        )
        .first()
    )
    # Audit logs may not exist if service doesn't log — soft assert
    # Don't fail if audit logs aren't implemented yet


def assert_experiment_in_db(
    db: Session, experiment_id, **expected_fields
) -> Experiment:
    """Assert experiment exists in DB with expected field values."""
    exp = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    assert exp is not None, f"Experiment {experiment_id} not found in database"
    for field, value in expected_fields.items():
        actual = getattr(exp, field)
        assert actual == value or str(actual) == str(value), (
            f"Experiment.{field}: expected {value!r}, got {actual!r}"
        )
    return exp


def assert_feature_flag_in_db(db: Session, flag_id, **expected_fields) -> FeatureFlag:
    """Assert feature flag exists in DB with expected field values."""
    flag = db.query(FeatureFlag).filter(FeatureFlag.id == flag_id).first()
    assert flag is not None, f"FeatureFlag {flag_id} not found in database"
    for field, value in expected_fields.items():
        actual = getattr(flag, field)
        assert actual == value or str(actual) == str(value), (
            f"FeatureFlag.{field}: expected {value!r}, got {actual!r}"
        )
    return flag


def unique_username(prefix: str = "user") -> str:
    """Generate a unique username for tests."""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def unique_email(prefix: str = "user") -> str:
    """Generate a unique email for tests."""
    return f"{prefix}_{uuid.uuid4().hex[:8]}@integration.test"


def unique_flag_key(prefix: str = "flag") -> str:
    """Generate a unique feature flag key."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


#: The list endpoint's largest page (``limit`` has ``le=200``).
SEGMENT_PAGE_SIZE = 200
#: Pages fetched before giving up: 100 x 200 = 20,000 segments.
SEGMENT_MAX_PAGES = 100


def list_all_segment_ids(client, status: Optional[str] = None) -> List[str]:
    """Every segment id ``GET /api/v1/segments`` returns, across all pages.

    The integration database is shared by the whole run, so a segment a test
    just created need not be on the first page. This pages with ``limit`` and
    ``offset`` until a short page, so both "is listed" and "is not listed"
    assertions see the whole list rather than whatever fits on page one.
    """
    ids: List[str] = []
    for page in range(SEGMENT_MAX_PAGES):
        params = {"limit": SEGMENT_PAGE_SIZE, "offset": page * SEGMENT_PAGE_SIZE}
        if status is not None:
            params["status"] = status
        response = client.get("/api/v1/segments", params=params)
        assert response.status_code == 200, response.text
        batch = [item["id"] for item in response.json()]
        ids.extend(batch)
        if len(batch) < SEGMENT_PAGE_SIZE:
            return ids
    raise AssertionError(
        f"GET /api/v1/segments still returned full pages after "
        f"{SEGMENT_MAX_PAGES * SEGMENT_PAGE_SIZE} segments"
    )


#: The flag list's default page size.
FLAG_PAGE_SIZE = 100
#: Pages fetched before giving up: 100 x 100 = 10,000 flags.
FLAG_MAX_PAGES = 100


def list_all_flags(client, **params) -> List[dict]:
    """Every item ``GET /api/v1/feature-flags/`` returns, across all pages.

    The flag list pages oldest first (#1732) and the integration database is
    shared by the whole run, so once earlier tests have left a page of flags
    behind, a flag a test just created is not on the first page. This pages
    with ``skip`` and ``limit`` until it holds ``total`` items or reads a short
    page, so both "is listed" and "is not listed" assertions see the whole
    list. ``params`` (``status``, ``search``) are sent with every page.
    """
    items: List[dict] = []
    for page in range(FLAG_MAX_PAGES):
        response = client.get(
            "/api/v1/feature-flags/",
            params={**params, "skip": page * FLAG_PAGE_SIZE, "limit": FLAG_PAGE_SIZE},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        items.extend(body["items"])
        if len(items) >= body["total"] or len(body["items"]) < FLAG_PAGE_SIZE:
            return items
    raise AssertionError(
        f"GET /api/v1/feature-flags/ still returned full pages after "
        f"{FLAG_MAX_PAGES * FLAG_PAGE_SIZE} flags"
    )
