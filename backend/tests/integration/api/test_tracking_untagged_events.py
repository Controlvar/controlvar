"""
``/tracking/track`` and ``/tracking/batch`` store an event that names no
experiment and no flag, as history (#217).

Such an event has ``experiment_id``, ``feature_flag_id`` and ``variant_id``
all null, so it counts in no experiment's results. Only an absent key
(missing, ``null`` or ``""``) records it; a key that is given and not found
keeps the 404, or the batch item's error. ``/tracking/events`` (by ids) and
``EventService``'s own writers (``track_conversion`` and the variant-view
writer) still refuse an event with neither id.

The gates here, with the defect each was run against, are listed in the pull
request that added them:

* T1/T2 the untagged event is stored (``/track``) or counted as a success
  (``/batch``);
* T3a/T3b the three 404 texts, none printing ``None``, and ``" "`` is a key;
* T6 every results path is unchanged by 1,000 untagged events;
* T7 the global event totals rise by exactly that many;
* T8 an untagged event sent after assignment counts nowhere.

Rows created here are deleted in teardown: the shared test database is not
truncated between tests.
"""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.core.bandit_scheduler import BanditScheduler
from backend.app.models.analysis_snapshot import AnalysisSnapshot
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import (
    ExperimentStatus,
    ExperimentType,
    Metric,
    MetricType,
    Variant,
)
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.event_matching import (
    count_converting_users,
    count_converting_users_any,
)
from backend.app.services.event_service import EventService
from backend.app.services.export_service import ExportService
from backend.app.services.results_streaming_service import ResultsStreamingService

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

#: Keys whose value is the time of the request, not a result.
VOLATILE_KEYS = frozenset(
    {"timestamp", "computed_at", "generated_at", "calculated_at", "last_updated"}
)


def _user() -> str:
    return f"untagged-{uuid.uuid4().hex[:10]}"


def _delete_untagged(db_session, user_ids) -> None:
    db_session.rollback()
    db_session.query(Event).filter(
        Event.experiment_id.is_(None),
        Event.feature_flag_id.is_(None),
        Event.user_id.in_(list(user_ids)),
    ).delete(synchronize_session=False)
    db_session.commit()


@pytest.fixture
def untagged_users(db_session):
    """User ids a test registers here have their untagged rows deleted after it."""
    users: set = set()
    yield users
    if users:
        _delete_untagged(db_session, users)


@pytest.fixture
def experiment(db_session, make_experiment):
    """
    An ACTIVE A/B experiment, five users per arm, a primary ``purchase``
    conversion metric, and tagged events carrying a ``country``:

    control:   c0 buys twice (US), c1 once (DE), c2 views only, c3/c4 nothing;
    treatment: t0 buys three times (DE), t1 once (US), t2 once (US),
               t3 views only, t4 nothing.
    """
    suffix = uuid.uuid4().hex[:8]
    start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=3)
    exp = make_experiment(
        name=f"Untagged history {suffix}",
        key=f"untagged-history-{suffix}",
        status=ExperimentStatus.ACTIVE,
        experiment_type=ExperimentType.A_B,
        start_date=start,
        sequential_testing_enabled=True,
        sequential_testing_config={"method": "msprt"},
        bayesian_enabled=True,
        bayesian_config={"prior_family": "beta", "alpha": 1.0, "beta": 1.0},
    )
    control = Variant(
        experiment_id=exp.id, name="control", is_control=True, traffic_allocation=50
    )
    treatment = Variant(
        experiment_id=exp.id, name="treatment", is_control=False, traffic_allocation=50
    )
    db_session.add_all(
        [
            control,
            treatment,
            Metric(
                experiment_id=exp.id,
                name="Purchase",
                event_name="purchase",
                metric_type=MetricType.CONVERSION,
                is_primary=True,
            ),
        ]
    )
    db_session.commit()
    day1 = start + timedelta(days=1, hours=12)
    day2 = start + timedelta(days=2, hours=12)
    plan = {
        control: [
            ("US", [day1, day2]),
            ("DE", [day2]),
            ("US", []),
            (None, None),
            (None, None),
        ],
        treatment: [
            ("DE", [day1, day1 + timedelta(hours=1), day2]),
            ("US", [day2]),
            ("US", [day1]),
            ("DE", []),
            (None, None),
        ],
    }
    rows = []
    for variant, users in plan.items():
        for i, (country, purchases) in enumerate(users):
            user_id = f"uh-{exp.key}-{variant.name}-{i}"
            rows.append(
                Assignment(experiment_id=exp.id, variant_id=variant.id, user_id=user_id)
            )
            if purchases is None:
                continue
            for name, at in [("page_view", day1)] + [
                ("purchase", p) for p in purchases
            ]:
                rows.append(
                    Event(
                        event_type=name,
                        event_name=name,
                        user_id=user_id,
                        experiment_id=exp.id,
                        variant_id=variant.id,
                        value=1.0,
                        event_metadata={"country": country},
                        created_at=at.isoformat(),
                    )
                )
    db_session.add_all(rows)
    db_session.commit()
    db_session.refresh(exp)
    yield exp, start
    db_session.rollback()
    for model in (AnalysisSnapshot, Event, Assignment):
        db_session.query(model).filter(model.experiment_id == exp.id).delete(
            synchronize_session=False
        )
    db_session.commit()


def _get(client, path, **params):
    response = client.get(path, params={"use_cache": "false", **params})
    assert response.status_code == 200, response.text
    return response.json()


def _canonical(value):
    """``value`` without the request-time keys, and with each breakdown's
    segments sorted: the segment query has no ORDER BY, so their order can
    change with nothing else changing."""
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items() if k not in VOLATILE_KEYS}
    if isinstance(value, list):
        items = [_canonical(v) for v in value]
        if items and all(isinstance(i, dict) and "segment_value" in i for i in items):
            items.sort(key=lambda i: str(i["segment_value"]))
        return items
    return value


class _SharedSession:
    """The test session, handed to the streaming service; close() is a no-op."""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self):
        pass


def _snapshot(client, db_session, exp) -> str:
    """Every analysis path's answer for ``exp``, as one canonical JSON string."""
    base = f"/api/v1/results/{exp.id}"
    variants = sorted(exp.variants, key=lambda v: v.name)
    variant_ids = [str(v.id) for v in variants]
    since = (exp.created_at - timedelta(seconds=1)).isoformat()

    streaming = ResultsStreamingService(lambda: _SharedSession(db_session))
    bandit = BanditScheduler(db_session)._stats_from_postgres(exp.id, variant_ids, exp)
    exported_variants = _get(
        client, "/api/v1/export/variants", format="json", start_date=since
    )
    exported_experiments = _get(
        client, "/api/v1/export/experiments", format="json", start_date=since
    )
    snapshot = {
        "results": _get(client, base, breakdown="country"),
        "daily": _get(client, f"{base}/daily"),
        "sequential": _get(client, f"{base}/sequential"),
        "bayesian": _get(client, f"{base}/bayesian"),
        "cuped": _get(client, f"{base}/cuped"),
        "segmented_results": _get(
            client, f"/api/v1/experiments/{exp.id}/segmented-results/country"
        ),
        "segmented_service": AnalysisService(db_session).get_segmented_results(
            experiment_id=exp.id, segment_by="country"
        ),
        "live": asyncio.run(streaming.get_live_snapshot(str(exp.id))),
        "bandit": {
            vid: {"pulls": stats.pulls, "successes": stats.successes}
            for vid, stats in (bandit or {}).items()
        },
        "count_converting_users": {
            str(v.id): count_converting_users(db_session, exp.id, v.id, "purchase")
            for v in variants
        },
        "count_converting_users_any": count_converting_users_any(
            db_session, exp.id, ["purchase"]
        ),
        "export_variants": [
            r for r in exported_variants if r.get("experiment_id") == str(exp.id)
        ],
        "export_experiments": [
            r
            for r in exported_experiments
            if str(exp.id) in (r.get("experiment_id"), r.get("id"))
        ],
        "export_report": _get(
            client, f"/api/v1/export/reports/experiments/{exp.id}", format="json"
        ),
    }
    assert snapshot["export_variants"], "the export lists no variant of the experiment"
    assert snapshot["export_experiments"], "the export does not list the experiment"
    return json.dumps(_canonical(snapshot), sort_keys=True, default=str)


def _leaves(value, path=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _leaves(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _leaves(item, f"{path}[{index}]")
    else:
        yield path, value


def _differences(before: str, after: str) -> list:
    """The snapshot leaves that differ, for the assertion message."""
    old = dict(_leaves(json.loads(before)))
    new = dict(_leaves(json.loads(after)))
    return [
        f"{path}: {old.get(path)!r} -> {new.get(path)!r}"
        for path in sorted(set(old) | set(new))
        if old.get(path) != new.get(path)
    ][:20]


def _send_untagged(client, events) -> None:
    for i in range(0, len(events), 100):
        response = client.post(
            "/api/v1/tracking/batch", json={"events": events[i : i + 100]}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["failure_count"] == 0, body
        assert body["success_count"] == len(events[i : i + 100])


class TestUntaggedTrack:
    """T1: an event with no key is stored, with every id null."""

    @pytest.mark.parametrize(
        "keys",
        [
            {},
            {"experiment_key": None, "feature_flag_key": None},
            {"experiment_key": "", "feature_flag_key": ""},
        ],
        ids=["missing", "null", "empty"],
    )
    def test_an_event_with_no_key_is_stored_as_history(
        self, admin_client, db_session, untagged_users, keys
    ):
        user_id = _user()
        untagged_users.add(user_id)
        response = admin_client.post(
            "/api/v1/tracking/track",
            json={
                "event_type": "checkout_completed",
                "user_id": user_id,
                "timestamp": "2026-09-01T09:30:00Z",
                "value": 12.5,
                **keys,
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["experiment_id"] is None
        assert data["feature_flag_id"] is None
        assert data["variant_id"] is None
        assert data["event_name"] == "checkout_completed"

        row = db_session.query(Event).filter(Event.id == uuid.UUID(data["id"])).one()
        assert row.user_id == user_id
        assert row.experiment_id is None
        assert row.feature_flag_id is None
        assert row.variant_id is None
        assert row.created_at.startswith("2026-09-01T09:30:00")

    def test_the_stored_model_still_refuses_a_long_event_name(
        self, admin_client, db_session, untagged_users
    ):
        """``UntaggedEventCreate`` keeps ``event_name``'s 100-character limit."""
        user_id = _user()
        untagged_users.add(user_id)
        response = admin_client.post(
            "/api/v1/tracking/track",
            json={"event_type": "x", "event_name": "n" * 101, "user_id": user_id},
        )
        assert response.status_code == 422, response.text
        assert response.json() == {"detail": "Invalid event: event_name not valid"}
        # Refused before the write: EventResponse has the same limit, and
        # would answer the same 422 after storing the row.
        assert db_session.query(Event).filter(Event.user_id == user_id).count() == 0


class TestUntaggedBatch:
    """T2: a batch counts its untagged items as successes."""

    def test_untagged_items_are_successes(
        self, admin_client, db_session, untagged_users
    ):
        user_id = _user()
        untagged_users.add(user_id)
        response = admin_client.post(
            "/api/v1/tracking/batch",
            json={
                "events": [
                    {"event_type": "checkout_completed", "user_id": user_id},
                    {
                        "event_type": "page_view",
                        "user_id": user_id,
                        "experiment_key": None,
                        "feature_flag_key": "",
                    },
                ]
            },
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "success_count": 2,
            "failure_count": 0,
            "duplicate_count": 0,
            "errors": None,
        }
        stored = db_session.query(Event).filter(Event.user_id == user_id).all()
        assert sorted(e.event_type for e in stored) == [
            "checkout_completed",
            "page_view",
        ]
        assert all(e.experiment_id is None and e.variant_id is None for e in stored)


UNKNOWN_KEY_CASES = [
    (
        {"experiment_key": "nope-exp"},
        "No experiment has the key 'nope-exp'. Leave experiment_key out to "
        "record the event without an experiment.",
    ),
    (
        {"feature_flag_key": "nope-flag"},
        "No feature flag has the key 'nope-flag'. Leave feature_flag_key out to "
        "record the event without a flag.",
    ),
    (
        {"experiment_key": "nope-exp", "feature_flag_key": "nope-flag"},
        "Neither experiment key 'nope-exp' nor feature flag key 'nope-flag' was found.",
    ),
    (
        {"experiment_key": " "},
        "No experiment has the key ' '. Leave experiment_key out to record the "
        "event without an experiment.",
    ),
    (
        {"experiment_key": "nope-exp", "feature_flag_key": None},
        "No experiment has the key 'nope-exp'. Leave experiment_key out to "
        "record the event without an experiment.",
    ),
]
UNKNOWN_KEY_IDS = ["experiment", "flag", "both", "blank-is-a-key", "flag-null"]


class TestGivenKeysThatAreNotFound:
    """T3a/T3b: a key that is given and not found is never history."""

    @pytest.mark.parametrize(("keys", "detail"), UNKNOWN_KEY_CASES, ids=UNKNOWN_KEY_IDS)
    def test_track_answers_404(
        self, admin_client, db_session, untagged_users, keys, detail
    ):
        user_id = _user()
        untagged_users.add(user_id)
        response = admin_client.post(
            "/api/v1/tracking/track",
            json={"event_type": "click", "user_id": user_id, **keys},
        )
        assert response.status_code == 404, response.text
        assert response.json() == {"detail": detail}
        assert "None" not in response.text
        assert db_session.query(Event).filter(Event.user_id == user_id).count() == 0

    @pytest.mark.parametrize(("keys", "detail"), UNKNOWN_KEY_CASES, ids=UNKNOWN_KEY_IDS)
    def test_batch_reports_the_item(
        self, admin_client, db_session, untagged_users, keys, detail
    ):
        user_id = _user()
        untagged_users.add(user_id)
        response = admin_client.post(
            "/api/v1/tracking/batch",
            json={
                "events": [
                    {"event_type": "history", "user_id": user_id},
                    {"event_type": "click", "user_id": user_id, **keys},
                ]
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["success_count"] == 1
        assert body["failure_count"] == 1
        assert body["errors"] == [
            {"index": 1, "event_type": "click", "user_id": user_id, "error": detail}
        ]
        assert "None" not in response.text
        stored = db_session.query(Event).filter(Event.user_id == user_id).all()
        assert [e.event_type for e in stored] == ["history"]


class TestOtherWritersStillRefuse:
    """``/tracking/events`` (by ids) and EventService's own writers."""

    def test_events_by_ids_refuses_an_event_with_neither_id(
        self, admin_client, db_session, untagged_users
    ):
        user_id = _user()
        untagged_users.add(user_id)
        response = admin_client.post(
            "/api/v1/tracking/events",
            json={"event_name": "checkout_completed", "user_id": user_id},
        )
        assert response.status_code == 422, response.text
        assert db_session.query(Event).filter(Event.user_id == user_id).count() == 0

    def test_track_conversion_refuses_an_event_with_neither_id(
        self, db_session, untagged_users
    ):
        user_id = _user()
        untagged_users.add(user_id)
        with pytest.raises(ValueError, match="experiment_id or feature_flag_id"):
            EventService(db_session).track_conversion(user_id, "purchase")
        assert db_session.query(Event).filter(Event.user_id == user_id).count() == 0

    def test_the_variant_view_writer_refuses_an_event_with_neither_id(
        self, db_session, untagged_users
    ):
        user_id = _user()
        untagged_users.add(user_id)
        with pytest.raises(ValueError, match="experiment_id or feature_flag_id"):
            EventService(db_session).track_exposure(user_id, None, None)
        assert db_session.query(Event).filter(Event.user_id == user_id).count() == 0


def _untagged_events(exp, start, count):
    """``count`` untagged events from the experiment's own users and others,
    with the metric's own name, inside the experiment window and after it."""
    users = [
        f"uh-{exp.key}-{arm}-{i}" for arm in ("control", "treatment") for i in range(5)
    ]
    users += [f"uh-{exp.key}-stranger-{i}" for i in range(5)]
    times = [
        start - timedelta(days=2),
        start + timedelta(days=1, hours=13),
        start + timedelta(days=2, hours=13),
        datetime.now(timezone.utc) + timedelta(hours=1),
    ]
    names = ["purchase", "page_view"]
    return [
        {
            "event_type": names[i % len(names)],
            "user_id": users[i % len(users)],
            "value": 1.0,
            "metadata": {"country": ("US", "DE")[i % 2]},
            "timestamp": times[i % len(times)].isoformat(),
        }
        for i in range(count)
    ]


def test_untagged_events_change_no_results_path(
    admin_client, db_session, experiment, untagged_users
):
    """T6: 1,000 untagged events with the metric's event name and the
    experiment's own user ids leave every analysis path byte-identical."""
    exp, start = experiment
    before = _snapshot(admin_client, db_session, exp)
    assert json.loads(before)["count_converting_users_any"] == 5

    events = _untagged_events(exp, start, 1000)
    untagged_users.update(e["user_id"] for e in events)
    _send_untagged(admin_client, events)
    assert (
        db_session.query(Event)
        .filter(
            Event.experiment_id.is_(None),
            Event.user_id.in_(list(untagged_users)),
        )
        .count()
        == 1000
    )

    db_session.expire_all()
    after = _snapshot(admin_client, db_session, exp)
    assert after == before, _differences(before, after)


def test_global_event_totals_rise_by_exactly_the_untagged_events(
    admin_client, db_session, experiment, untagged_users
):
    """T7: admin ``events.total`` and the export overview's event count are
    global, so they do count history: by exactly the number stored."""
    exp, start = experiment
    export = ExportService(db_session)

    def totals():
        stats = _get(admin_client, "/api/v1/admin/stats")
        return stats["events"]["total"], export._count_events(None, None)

    admin_before, export_before = totals()
    events = _untagged_events(exp, start, 250)
    untagged_users.update(e["user_id"] for e in events)
    _send_untagged(admin_client, events)
    admin_after, export_after = totals()

    assert admin_after - admin_before == 250
    assert export_after - export_before == 250


def test_an_untagged_event_after_assignment_counts_nowhere(
    admin_client, db_session, experiment, untagged_users
):
    """T8: tag outcome events. An untagged purchase from an assigned user, sent
    after the assignment, is stored with no variant and counts in no result."""
    exp, _ = experiment
    user_id = _user()
    untagged_users.add(user_id)
    assigned = admin_client.post(
        "/api/v1/tracking/assign",
        json={"experiment_key": exp.key, "user_id": user_id},
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["variant_id"]
    db_session.expire_all()
    before = _snapshot(admin_client, db_session, exp)

    response = admin_client.post(
        "/api/v1/tracking/track",
        json={"event_type": "purchase", "user_id": user_id, "value": 30.0},
    )
    assert response.status_code == 200, response.text

    db_session.expire_all()
    after = _snapshot(admin_client, db_session, exp)
    assert after == before, _differences(before, after)
    assert response.json()["experiment_id"] is None
    assert response.json()["variant_id"] is None
