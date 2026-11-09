"""The demo seed's CUPED history is balanced across the arms (#217).

``seed_demo_data.py`` gives ``checkout_button_color``'s users a key-less
``checkout_completed`` before their assignment, with a probability that
depends on whether they convert in the experiment and on their arm.  That
models a habit formed before assignment only if the share of users with
history is the same in both arms: ``P(X=1 | arm) = p1 * cvr + p0 * (1 - cvr)``.
Equal probabilities in both arms would not be (green converts more), and the
documentation's CUPED example would then remove part of the real effect.

The gate is arithmetic on the seed's own constants, so it is exact.  With the
probabilities (0.6, 0.04) in both arms the shares are 0.0848 and 0.0904, a gap
of 0.0056, and it fails.

The documentation reads that history with CUPED, so the seed creates the
experiment with it: the method ``cuped`` and a 7-day lookback, stored as a
``POST`` stores it (#848). A demo stack seeded before that keeps a NULL
setting, and the seed run again (``SEED_FORCE=true``) gives it the same one;
a setting that is already there, whatever it says, is left alone. The
backfill test runs against the test database.
"""

import inspect
import random
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend.app.models.experiment import Experiment
from backend.app.schemas.variance_reduction import (
    VarianceReductionConfig,
    VarianceReductionMethod,
)
from backend.scripts import seed_demo_data as seed

pytestmark = pytest.mark.unit

#: docs/api/cuped.md's setting for the demo experiment, as the schema dumps it.
CUPED_7_DAYS = VarianceReductionConfig(
    method=VarianceReductionMethod.CUPED, covariate_lookback_days=7
).model_dump(mode="json")
CHECKOUT = "Checkout Button Color"


def test_history_is_equally_likely_in_both_arms():
    blue = seed.checkout_history_share("blue_button")
    green = seed.checkout_history_share("green_button")
    assert abs(blue - green) < 1e-3, (blue, green)


def test_the_shares_come_from_the_arms_own_conversion_rates():
    assert seed.CHECKOUT_CVR == {"blue_button": 0.08, "green_button": 0.09}
    p1, p0 = seed.CHECKOUT_HISTORY_P["blue_button"]
    assert seed.checkout_history_share("blue_button") == pytest.approx(
        p1 * 0.08 + p0 * 0.92, abs=1e-15
    )


def test_a_history_event_is_untagged_and_stored_a_day_before_assignment():
    assigned = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc)
    rng = random.Random(1)
    events = [
        seed._checkout_history_event(rng, "green_button", "u", assigned, True)
        for _ in range(200)
    ]
    events = [e for e in events if e is not None]
    assert events, "the probe drew no history at all"
    for event in events:
        assert event.experiment_id is None
        assert event.variant_id is None
        assert event.feature_flag_id is None
        assert event.event_name == "checkout_completed"
        # Naive UTC, at least a day before the (aware) assignment.
        assert event.updated_at.tzinfo is None
        received = event.updated_at.replace(tzinfo=timezone.utc)
        assert received <= assigned - timedelta(days=1)
        assert datetime.fromisoformat(event.created_at) == received


def test_checkout_button_color_is_created_with_cuped_and_a_7_day_lookback():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    with patch.object(seed, "_bulk_insert"):
        experiment = seed.seed_checkout_button_experiment(
            db, SimpleNamespace(id=uuid.uuid4())
        )
    assert experiment.name == CHECKOUT
    assert experiment.variance_reduction_config == CUPED_7_DAYS
    assert experiment.variance_reduction_config == {
        "method": "cuped",
        "covariate_metric_id": None,
        "covariate_lookback_days": 7,
        "winsorization_percentile": 99.0,
    }


def _checkout_row(db_session, config):
    row = Experiment(name=CHECKOUT, variance_reduction_config=config)
    db_session.add(row)
    db_session.commit()
    return row.id


def test_a_stack_seeded_before_gets_the_setting_only_where_it_is_null(db_session):
    unset = _checkout_row(db_session, None)
    chosen = {**CUPED_7_DAYS, "method": "none"}
    kept = _checkout_row(db_session, chosen)

    assert seed.backfill_checkout_variance_reduction(db_session) >= 1
    db_session.expire_all()
    assert db_session.get(Experiment, unset).variance_reduction_config == CUPED_7_DAYS
    assert db_session.get(Experiment, kept).variance_reduction_config == chosen

    # A second run finds nothing left to set.
    assert seed.backfill_checkout_variance_reduction(db_session) == 0
    db_session.expire_all()
    assert db_session.get(Experiment, kept).variance_reduction_config == chosen


def test_every_run_of_the_seed_backfills_after_the_experiments():
    source = inspect.getsource(seed.main)
    call = source.index("backfill_checkout_variance_reduction(db)")
    assert source.index("seed_checkout_button_experiment(db, admin_user)") < call
