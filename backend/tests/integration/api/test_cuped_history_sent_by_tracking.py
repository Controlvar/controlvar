"""Pre-experiment history sent through the tracking API, as CUPED reads it.

History is sent to ``POST /api/v1/tracking/track`` without an experiment or
flag key, before the users are assigned; ``GET /results/{id}/cuped`` then
reports the share of the comparison's users with history
(``covariate_coverage_pct``). The users differ in one thing each:

* ``user_id`` is matched exactly: history sent as ``Hist-Case`` is not the
  history of the assigned user ``hist-case``;
* a ``timestamp`` with no offset is UTC: the process runs in a time zone seven
  hours behind UTC, where reading it as local time would put an event sent
  30 minutes before the assignment seven hours after it;
* an event sent without a ``timestamp`` is dated when it is received, so it is
  history for a user assigned after it was sent;
* an event dated after the assignment, or before the lookback window, is not.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.models.user import UserRole
from backend.tests.integration.api.route_helpers import (
    api_client,
    api_key,
    assign,
    auth,
    create_experiment,
    local_sign_in,
    make_user,
    start,
    track,
)

pytestmark = [pytest.mark.integration, pytest.mark.regression]


@pytest.fixture(autouse=True)
def _sign_in(monkeypatch):
    local_sign_in(monkeypatch)


@pytest.fixture
def los_angeles(monkeypatch):
    """The process's local time is UTC-7 (or -8) while the test runs."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _naive(moment: datetime) -> str:
    """ISO 8601 with no offset."""
    return moment.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


def test_history_is_matched_by_exact_id_and_dated_in_utc(db_session, los_angeles):
    with api_client(db_session) as client:
        owner = make_user(db_session, UserRole.ADMIN, superuser=True, tag="cuped")
        experiment = create_experiment(
            client,
            owner,
            variance_reduction_config={
                "method": "cuped",
                "covariate_lookback_days": 14,
            },
        )
        start(client, experiment, owner)
        key = api_key(client, owner)
        event = experiment["metrics"][0]["event_name"]
        s = uuid.uuid4().hex[:6]
        now = datetime.now(timezone.utc)

        sent = {
            # Sent with another case: not this user's history.
            f"Hist-Case-{s}": _naive(now - timedelta(days=2)),
            f"hist-plain-{s}": _naive(now - timedelta(days=2)),
            # 30 minutes ago in UTC; seven hours ahead if read as local time.
            f"hist-utc-{s}": _naive(now - timedelta(minutes=30)),
            f"hist-received-{s}": None,
            f"hist-after-{s}": _naive(now + timedelta(hours=1)),
            f"hist-old-{s}": _naive(now - timedelta(days=30)),
        }
        for user_id, timestamp in sent.items():
            track(client, key, user_id, event, timestamp=timestamp)

        assigned = [
            f"hist-case-{s}",
            f"hist-plain-{s}",
            f"hist-utc-{s}",
            f"hist-received-{s}",
            f"hist-after-{s}",
            f"hist-old-{s}",
            f"hist-none-{s}",
        ]
        for user_id in assigned:
            assign(client, key, experiment["key"], user_id)
        # Users with no history, half of whom convert during the experiment, so
        # that the outcome varies in both arms.
        for i in range(20):
            user_id = f"fill-{s}-{i}"
            assign(client, key, experiment["key"], user_id)
            if i % 2:
                track(client, key, user_id, event, experiment_key=experiment["key"])

        resp = client.get(
            f"/api/v1/results/{experiment['id']}/cuped", headers=auth(owner)
        )
        assert resp.status_code == 200, resp.text
        (row,) = resp.json()["metrics"]
        assert row["unavailable_reason"] is None, row
        assert row["covariate_event_name"] == event
        # plain, utc and received have history: 3 of the 27 users.
        assert row["covariate_coverage_pct"] == pytest.approx(100.0 * 3 / 27)
