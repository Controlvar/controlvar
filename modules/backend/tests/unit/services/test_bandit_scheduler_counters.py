"""BanditScheduler's first stats source: the counters module's DynamoDB counters.

The core scheduler asks the ``counters.service`` capability for the counter
service class and duck-types what it answers, so the core suite
(``backend/tests/unit/services/test_bandit_scheduler.py``) covers the
fallback chain with fakes.  The two tests here drive the *real* provider that
``modules.register(hooks)`` installs -- ``DynamoDBCounterService`` resolved at
its definition site, ``ExperimentCounters`` from the module's schema -- which
is why they live in the modules tree: a core file could not import either
without crossing the boundary.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

from backend.app.core.bandit_scheduler import BanditScheduler
from backend.tests.unit.services.test_bandit_scheduler import (
    _make_experiment,
    _mock_metric,
)
from modules.backend.app.schemas.realtime_counters import (
    ExperimentCounters,
    VariantCounters,
)

_COUNTER_SERVICE = (
    "modules.backend.app.services.dynamodb_counter_service.DynamoDBCounterService"
)


def test_get_variant_stats_from_counters_uses_dynamodb():
    """get_variant_stats_from_counters reads DynamoDB via get_experiment_counters."""
    db = MagicMock()
    scheduler = BanditScheduler(db=db)

    exp_id = uuid.uuid4()
    vid1, vid2 = str(uuid.uuid4()), str(uuid.uuid4())

    counters = ExperimentCounters(
        experiment_id=str(exp_id),
        total_assignments=200,
        total_events=0,
        total_conversions=60,
        variants=[
            VariantCounters(
                variant_id=vid1,
                variant_name=vid1,
                is_control=True,
                assignments=100,
                conversions=40,
                conversion_rate=0.4,
            ),
            VariantCounters(
                variant_id=vid2,
                variant_name=vid2,
                is_control=False,
                assignments=100,
                conversions=20,
                conversion_rate=0.2,
            ),
        ],
    )

    mock_counter_service = MagicMock()
    mock_counter_service.get_experiment_counters.return_value = counters

    # The registered provider resolves the class at each call, so patching it
    # at its definition site (the service module) is what the scheduler sees.
    # PostgreSQL is consulted first (#581): the experiment has no assignment
    # rows there, so the real-time counters are the only data there is.
    with (
        patch(_COUNTER_SERVICE, return_value=mock_counter_service),
        patch.object(
            scheduler, "_count_assignments_by_variant", return_value={}
        ) as count_pulls,
        patch.object(scheduler, "_count_conversions_by_variant", return_value={}),
    ):
        stats = scheduler.get_variant_stats_from_counters(exp_id, [vid1, vid2])

    mock_counter_service.get_experiment_counters.assert_called_once_with(str(exp_id))
    count_pulls.assert_called_once_with(exp_id)
    assert set(stats) == {vid1, vid2}
    # assignments -> pulls, conversions -> successes (capped at pulls),
    # failures = pulls - successes
    assert stats[vid1].pulls == 100
    assert stats[vid1].successes == 40
    assert stats[vid1].failures == 60
    assert stats[vid2].successes == 20
    assert stats[vid2].failures == 80


def test_postgres_stats_win_over_the_real_dynamodb_provider():
    """#581: with assignment rows in PostgreSQL the real DynamoDB counter
    service's event counts (40 and 20 conversions) are not weighed; the
    PostgreSQL stats come back whole."""
    db = MagicMock()
    scheduler = BanditScheduler(db=db)
    exp_id = uuid.uuid4()
    vid1, vid2 = str(uuid.uuid4()), str(uuid.uuid4())
    counters = ExperimentCounters(
        experiment_id=str(exp_id),
        total_assignments=200,
        total_events=0,
        total_conversions=60,
        variants=[
            VariantCounters(
                variant_id=vid1,
                variant_name=vid1,
                is_control=True,
                assignments=100,
                conversions=40,
                conversion_rate=0.4,
            ),
            VariantCounters(
                variant_id=vid2,
                variant_name=vid2,
                is_control=False,
                assignments=100,
                conversions=20,
                conversion_rate=0.2,
            ),
        ],
    )
    mock_counter_service = MagicMock()
    mock_counter_service.get_experiment_counters.return_value = counters

    with (
        patch(_COUNTER_SERVICE, return_value=mock_counter_service),
        patch.object(
            scheduler,
            "_count_assignments_by_variant",
            return_value={vid1: 90, vid2: 100},
        ),
        patch.object(
            scheduler, "_count_conversions_by_variant", return_value={vid1: 1}
        ),
    ):
        stats = scheduler.get_variant_stats_from_counters(exp_id, [vid1, vid2])

    assert (stats[vid1].pulls, stats[vid1].successes) == (90, 1)
    assert (stats[vid2].pulls, stats[vid2].successes) == (100, 0)


def test_falls_back_to_postgres_when_dynamodb_has_no_pulls():
    """DynamoDB reachable but empty for this experiment -> PostgreSQL."""
    db = MagicMock()
    scheduler = BanditScheduler(db=db)
    exp = _make_experiment(str(uuid.uuid4()))
    exp.metric_definitions = [_mock_metric("purchase", True)]
    exp.metrics = None
    vid1, vid2 = (str(v.id) for v in exp.variants)

    empty = ExperimentCounters(
        experiment_id=str(exp.id),
        total_assignments=0,
        total_events=0,
        total_conversions=0,
        variants=[],
    )
    mock_counter_service = MagicMock()
    mock_counter_service.get_experiment_counters.return_value = empty

    with (
        patch(_COUNTER_SERVICE, return_value=mock_counter_service),
        patch.object(
            scheduler,
            "_count_assignments_by_variant",
            return_value={vid1: 10, vid2: 10},
        ),
        patch.object(
            scheduler, "_count_conversions_by_variant", return_value={vid1: 3}
        ),
    ):
        stats = scheduler.get_variant_stats_from_counters(
            exp.id, [vid1, vid2], experiment=exp
        )

    assert stats[vid1].pulls == 10
    assert stats[vid1].successes == 3
    assert stats[vid2].successes == 0
