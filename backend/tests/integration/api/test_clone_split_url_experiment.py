"""A clone of a split URL experiment can start (#1781), over HTTP.

``POST /experiments/{id}/clone`` copied the variants of a split URL experiment
but not its ``split_url_config``, so the clone's start answered 400 ("This
split URL experiment has no split_url_config") and the dashboard, which cannot
edit that field, had no way to fix it. The clone now carries the source's
configuration, and its start makes the usual split URL checks: so it starts,
and while it is active the source, which has the same control page, does not.

Creating a split URL experiment, and cloning one, answers 501 in the core
profile, so these tests need the modules. The database is shared by the
session, so each test uses host names of its own.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any, Dict

import pytest

from backend.app.models.experiment import Experiment, ExperimentStatus, Variant

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_db,
    pytest.mark.modules,
    pytest.mark.regression,
]

EXPERIMENTS = "/api/v1/experiments"

#: The start refusal for a second active split URL experiment on a control page.
SAME_ENTRY = (
    "Another active split URL experiment already uses the same control page (host "
    "and path) as this experiment. Two active split URL experiments cannot share "
    "a control page. Pause or complete the other one, or change this "
    "experiment's control URL with PUT /api/v1/experiments/{id}; the dashboard "
    "cannot edit split_url_config."
)


def _split_url_body(host: str) -> Dict[str, Any]:
    return {
        "name": f"split url clone {uuid.uuid4().hex[:8]}",
        "experiment_type": "split_url",
        "variants": [
            {"name": "Control", "is_control": True, "traffic_allocation": 40},
            {"name": "Treatment", "is_control": False, "traffic_allocation": 60},
        ],
        "metrics": [
            {
                "name": "Purchase",
                "event_name": "purchase",
                "metric_type": "conversion",
                "is_primary": True,
            }
        ],
        "split_url_config": {
            "variants": [
                {
                    "name": "Control",
                    "url": f"https://{host}/checkout",
                    "traffic_allocation": 40,
                },
                {
                    "name": "Treatment",
                    "url": f"https://{host}/checkout-b",
                    "traffic_allocation": 60,
                },
            ],
            "cookie_ttl_days": 14,
        },
    }


def _row(db_session, experiment_id: str) -> Experiment:
    db_session.expire_all()
    return db_session.get(Experiment, uuid.UUID(experiment_id))


def _variants(db_session, experiment_id: str):
    db_session.expire_all()
    return sorted(
        (v.name, v.is_control, v.traffic_allocation)
        for v in db_session.query(Variant).filter(
            Variant.experiment_id == uuid.UUID(experiment_id)
        )
    )


def _source_and_clone(admin_client, db_session):
    """A split URL draft made through the API, and its clone."""
    host = f"c{uuid.uuid4().hex[:12]}.example"
    created = admin_client.post(f"{EXPERIMENTS}/", json=_split_url_body(host))
    assert created.status_code == 201, created.text
    source_id = created.json()["id"]

    cloned = admin_client.post(f"{EXPERIMENTS}/{source_id}/clone")
    assert cloned.status_code == 201, cloned.text
    clone_id = cloned.json()["id"]
    assert clone_id != source_id
    return source_id, clone_id


def test_a_clone_of_a_split_url_draft_carries_its_config_and_starts(
    admin_client, db_session
):
    source_id, clone_id = _source_and_clone(admin_client, db_session)

    source_config = copy.deepcopy(_row(db_session, source_id).split_url_config)
    clone = _row(db_session, clone_id)
    assert source_config is not None
    assert clone.split_url_config == source_config
    assert clone.status == ExperimentStatus.DRAFT
    assert _variants(db_session, clone_id) == _variants(db_session, source_id)

    started = admin_client.post(f"{EXPERIMENTS}/{clone_id}/start")
    assert started.status_code == 200, started.text
    assert _row(db_session, clone_id).status == ExperimentStatus.ACTIVE
    # The source is untouched by its clone's start.
    source_after = _row(db_session, source_id)
    assert source_after.status == ExperimentStatus.DRAFT
    assert source_after.split_url_config == source_config


def test_while_the_clone_is_active_the_source_does_not_start(admin_client, db_session):
    source_id, clone_id = _source_and_clone(admin_client, db_session)
    started = admin_client.post(f"{EXPERIMENTS}/{clone_id}/start")
    assert started.status_code == 200, started.text

    refused = admin_client.post(f"{EXPERIMENTS}/{source_id}/start")
    assert refused.status_code == 400, refused.text
    assert refused.json() == {"detail": SAME_ENTRY}
    assert _row(db_session, source_id).status == ExperimentStatus.DRAFT
