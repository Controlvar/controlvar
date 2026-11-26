"""
Every results reader takes a metric's ``MetricEvents``, never its bare name.

``event_matching.MetricEvents`` is what each reader of a metric's conversions,
counts or values is handed. A reader that swallowed the wrong argument would
not fail loudly: the live stream catches every error and shows 0, the bandit
falls back to its stored state, and the batched export's rows answer no users
for a key they do not hold. So these are exact numbers on PostgreSQL, from data
on which each of those failures gives a different answer:

* the bandit with no metric counts every assigned user with an event other
  than a view (3 and 2), and with the primary metric only its converters
  (2 and 1);
* the live stream counts 4 and 4 participants and 2 and 1 converters;
* the batched export's rows (``results_batch.load_rows``) answer each metric's
  users and refuse a key they were not read for.
"""

import asyncio
import uuid
from datetime import datetime, timezone

import pytest

from backend.app.core.bandit_scheduler import BanditScheduler
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import (
    ExperimentStatus,
    Metric,
    MetricType,
    Variant,
)
from backend.app.services import results_batch
from backend.app.services.event_matching import (
    MetricEvents,
    count_converting_users,
)
from backend.app.services.results_streaming_service import ResultsStreamingService

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

PURCHASE = MetricEvents("purchase", None)
SIGNUP = MetricEvents("signup", None)


@pytest.fixture
def experiment(db_session, make_experiment):
    """Four users per variant.

    control: c0 purchases twice, c1 once, c2 signs up, c3 only views.
    treatment: t0 purchases, t3 signs up, t1 and t2 only view.
    An unassigned user's purchase is tagged control. Every user has a view.
    """
    suffix = uuid.uuid4().hex[:8]
    exp = make_experiment(
        name=f"Metric events {suffix}",
        key=f"metric-events-{suffix}",
        status=ExperimentStatus.ACTIVE,
        start_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    control = Variant(experiment_id=exp.id, name="control", is_control=True)
    treatment = Variant(experiment_id=exp.id, name="treatment")
    db_session.add_all([control, treatment])
    db_session.flush()
    at = datetime(2026, 9, 2, tzinfo=timezone.utc).isoformat()

    def event(user, variant, name, kind=None, value=None):
        db_session.add(
            Event(
                event_type=kind or name,
                event_name=name,
                user_id=user,
                experiment_id=exp.id,
                variant_id=variant.id,
                value=value,
                created_at=at,
            )
        )

    users = {}
    for variant, prefix in ((control, "c"), (treatment, "t")):
        for i in range(4):
            user = f"me-{suffix}-{prefix}{i}"
            users[f"{prefix}{i}"] = user
            db_session.add(
                Assignment(experiment_id=exp.id, variant_id=variant.id, user_id=user)
            )
            event(user, variant, "purchase", kind="exposure")
    event(users["c0"], control, "purchase", value=2.0)
    event(users["c0"], control, "purchase", value=3.0)
    event(users["c1"], control, "purchase", value=5.0)
    event(users["c2"], control, "signup")
    event(users["t0"], treatment, "purchase", value=7.0)
    event(users["t3"], treatment, "signup")
    event(f"me-{suffix}-nobody", control, "purchase", value=11.0)
    db_session.commit()
    yield exp, control, treatment
    db_session.rollback()
    for model in (Event, Assignment, Metric):
        db_session.query(model).filter(model.experiment_id == exp.id).delete(
            synchronize_session=False
        )
    db_session.commit()


def _with_metric(db_session, exp, event_name, **fields):
    db_session.add(
        Metric(
            experiment_id=exp.id,
            name=f"{event_name} metric",
            event_name=event_name,
            metric_type=fields.pop("metric_type", MetricType.CONVERSION),
            **fields,
        )
    )
    db_session.commit()
    db_session.refresh(exp)
    return exp


def _bandit_successes(db_session, exp, control, treatment):
    stats = BanditScheduler(db_session)._stats_from_postgres(
        exp.id, [str(control.id), str(treatment.id)], exp
    )
    assert stats is not None, "the bandit fell back from PostgreSQL"
    return {
        "control": (stats[str(control.id)].pulls, stats[str(control.id)].successes),
        "treatment": (
            stats[str(treatment.id)].pulls,
            stats[str(treatment.id)].successes,
        ),
    }


class _SharedSession:
    """The test session, for the streaming service; its close() is a no-op."""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self):
        pass


def _streamed(db_session, exp):
    service = ResultsStreamingService(lambda: _SharedSession(db_session))
    snapshot = asyncio.run(service.get_live_snapshot(str(exp.id)))
    assert snapshot.get("event") == "results_update", snapshot
    return {
        v["name"]: (v["participant_count"], v["conversion_count"])
        for v in snapshot["variants"]
    }


def test_the_bandit_with_no_metric_counts_every_event_but_a_view(
    db_session, experiment
):
    exp, control, treatment = experiment
    assert BanditScheduler._primary_metric_events(exp) is None
    assert _bandit_successes(db_session, exp, control, treatment) == {
        "control": (4, 3),
        "treatment": (4, 2),
    }


def test_the_bandit_with_a_metric_counts_its_converters(db_session, experiment):
    exp, control, treatment = experiment
    exp = _with_metric(db_session, exp, "purchase", is_primary=True)
    assert BanditScheduler._primary_metric_events(exp) == PURCHASE
    assert _bandit_successes(db_session, exp, control, treatment) == {
        "control": (4, 2),
        "treatment": (4, 1),
    }


def test_the_live_stream_counts_the_primary_metric(db_session, experiment):
    exp, _, _ = experiment
    exp = _with_metric(db_session, exp, "purchase", is_primary=True)
    assert _streamed(db_session, exp) == {"control": (4, 2), "treatment": (4, 1)}


def test_the_batched_rows_answer_each_metric_and_refuse_any_other_key(
    db_session, experiment
):
    exp, control, treatment = experiment
    exp = _with_metric(db_session, exp, "purchase", is_primary=True)
    exp = _with_metric(db_session, exp, "signup")
    rows = results_batch.load_rows(db_session, [exp])

    def counts(events, variant):
        return sorted(rows.of(exp.id, variant.id, events))

    assert counts(PURCHASE, control) == [(1, 5.0), (2, 5.0)]
    assert counts(PURCHASE, treatment) == [(1, 7.0)]
    assert counts(SIGNUP, control) == [(1, None)]
    assert counts(SIGNUP, treatment) == [(1, None)]
    assert rows.converted == {str(exp.id): 5}
    assert rows.assignments == {str(exp.id): {str(control.id): 4, str(treatment.id): 4}}

    # Not read for this experiment: no metric of it is on "checkout", and it
    # reads no legacy events, so neither key is answered with no users.
    with pytest.raises(KeyError):
        rows.of(exp.id, control.id, MetricEvents("checkout", None))
    with pytest.raises(KeyError):
        rows.of(exp.id, control.id, None)
    with pytest.raises(KeyError):
        rows.of(uuid.uuid4(), control.id, PURCHASE)
    # A bare name, or a tuple that looks like the key, is refused.
    with pytest.raises(TypeError):
        rows.of(exp.id, control.id, "purchase")
    with pytest.raises(TypeError):
        rows.of(exp.id, control.id, ("purchase", None))


def test_a_reader_handed_a_bare_event_name_raises(db_session, experiment):
    exp, control, _ = experiment
    assert count_converting_users(db_session, exp.id, control.id, PURCHASE) == 2
    with pytest.raises(TypeError):
        count_converting_users(db_session, exp.id, control.id, "purchase")
