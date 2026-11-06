"""
``GET /api/v1/results/{id}?breakdown=``: each variant by name, and the real
control (#218), on rows seeded straight into the database.

Before the fix the breakdown built each segment from the rows it found, with
no name and no control flag: ``variant_name`` was the variant's id, a variant
with no rows in a segment was missing, and the control was guessed -- the
alphabetically smallest id.  So whenever the treatment's id sorted first,
every p-value in the breakdown compared the control against the treatment.

The control here is always given the LARGER id, so a guess from the id picks
the treatment.  Also here: the results cache key carries the engine version
under the ``results:{experiment_id}:`` prefix that ``invalidate-cache`` clears.

Rows created here are deleted in fixture teardown because the shared test
database is not truncated between tests.
"""

import fnmatch
import json
import math
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from backend.app.core.stats_engine import ENGINE_VERSION
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

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def _id_with_prefix(first: str) -> uuid.UUID:
    """A random UUID whose first hex digit is ``first``, so the order is fixed."""
    return uuid.UUID(first + uuid.uuid4().hex[1:])


@pytest.fixture
def experiment(db_session, make_experiment):
    """ACTIVE 50/50 experiment whose control has the larger variant id."""
    suffix = uuid.uuid4().hex[:8]
    exp = make_experiment(
        name=f"Breakdown variants {suffix}",
        key=f"breakdown-variants-{suffix}",
        status=ExperimentStatus.ACTIVE,
        experiment_type=ExperimentType.A_B,
        start_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    control_id = _id_with_prefix("f")
    treatment_id = _id_with_prefix("0")
    assert str(treatment_id) < str(control_id)
    db_session.add_all(
        [
            Variant(
                id=control_id,
                experiment_id=exp.id,
                name="control",
                is_control=True,
                traffic_allocation=50,
            ),
            Variant(
                id=treatment_id,
                experiment_id=exp.id,
                name="treatment",
                is_control=False,
                traffic_allocation=50,
            ),
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
    db_session.refresh(exp)
    yield exp
    db_session.rollback()
    for model in (AnalysisSnapshot, Event, Assignment):
        db_session.query(model).filter(model.experiment_id == exp.id).delete(
            synchronize_session=False
        )
    db_session.commit()


def _variants(exp):
    by_name = {v.name: v for v in exp.variants}
    return by_name["control"], by_name["treatment"]


def _seed(db_session, exp, variant, device, n_users, n_converted):
    """``n_users`` assigned to ``variant``, each with one ``device`` event;
    the first ``n_converted`` of them purchase."""
    now_iso = datetime.now(timezone.utc).isoformat()
    prefix = f"{variant.name}-{device}-{uuid.uuid4().hex[:6]}"
    rows = []
    for i in range(n_users):
        user_id = f"{prefix}-{i:04d}"
        rows.append(
            Assignment(experiment_id=exp.id, variant_id=variant.id, user_id=user_id)
        )
        rows.append(
            Event(
                event_type="page_view",
                event_name="page_view",
                user_id=user_id,
                experiment_id=exp.id,
                variant_id=variant.id,
                event_metadata={"device": device},
                created_at=now_iso,
            )
        )
        if i < n_converted:
            rows.append(
                Event(
                    event_type="purchase",
                    event_name="purchase",
                    user_id=user_id,
                    experiment_id=exp.id,
                    variant_id=variant.id,
                    value=1.0,
                    event_metadata={"device": device},
                    created_at=now_iso,
                )
            )
    db_session.add_all(rows)
    db_session.commit()


def _breakdown(client, exp):
    response = client.get(
        f"/api/v1/results/{exp.id}",
        params={"breakdown": "device", "use_cache": "false"},
    )
    assert response.status_code == 200, response.text
    breakdown = response.json()["breakdown"]
    assert breakdown is not None
    return {seg["segment_value"]: seg for seg in breakdown["segments"]}


def _fisher_p(c1, n1, c2, n2):
    """Two-sided Fisher's exact p-value of ``c2/n2`` against ``c1/n1``, written
    out independently: the probability, under the margins, of every table no
    more likely than the observed one."""
    converted = c1 + c2

    def weight(x):
        return math.comb(n2, x) * math.comb(n1, converted - x)

    observed = weight(c2)
    tables = range(max(0, converted - n1), min(n2, converted) + 1)
    as_extreme = sum(w for w in map(weight, tables) if w <= observed)
    return as_extreme / math.comb(n1 + n2, converted)


@pytest.mark.regression
def test_breakdown_names_each_variant(admin_client, db_session, experiment):
    control, treatment = _variants(experiment)
    for device in ("desktop", "mobile"):
        _seed(db_session, experiment, control, device, 40, 4)
        _seed(db_session, experiment, treatment, device, 40, 12)

    segments = _breakdown(admin_client, experiment)

    assert len(segments) == 2
    assert set(segments) == {"desktop", "mobile"}
    for seg in segments.values():
        names = {v["variant_name"] for v in seg["variants"]}
        assert names == {"control", "treatment"}


@pytest.mark.regression
def test_breakdown_control_is_the_real_control_not_the_smallest_id(
    admin_client, db_session, experiment
):
    control, treatment = _variants(experiment)
    # The control's id sorts AFTER the treatment's, so a control guessed from
    # the smallest id would be the treatment.
    assert str(treatment.id) < str(control.id)
    _seed(db_session, experiment, control, "desktop", 200, 20)
    _seed(db_session, experiment, treatment, "desktop", 200, 40)

    segments = _breakdown(admin_client, experiment)

    assert len(segments) == 1
    variants = {v["variant_id"]: v for v in segments["desktop"]["variants"]}
    assert set(variants) == {str(control.id), str(treatment.id)}
    ctrl = variants[str(control.id)]
    treat = variants[str(treatment.id)]
    assert ctrl["is_control"] is True
    assert treat["is_control"] is False
    assert [v["is_control"] for v in variants.values()].count(True) == 1
    # The p-value is the treatment's, tested against the control's counts.
    assert ctrl["p_value"] is None
    assert treat["p_value"] == pytest.approx(_fisher_p(20, 200, 40, 200), rel=1e-6)
    assert (ctrl["sample_size"], ctrl["conversions"]) == (200, 20)
    assert (treat["sample_size"], treat["conversions"]) == (200, 40)


@pytest.mark.regression
def test_breakdown_lists_a_variant_with_no_rows_in_a_segment(
    admin_client, db_session, experiment
):
    control, treatment = _variants(experiment)
    _seed(db_session, experiment, control, "desktop", 30, 3)
    _seed(db_session, experiment, treatment, "desktop", 30, 6)
    # Only the treatment has tablet users.
    _seed(db_session, experiment, treatment, "tablet", 10, 2)

    segments = _breakdown(admin_client, experiment)

    assert len(segments) == 2
    tablet = {v["variant_name"]: v for v in segments["tablet"]["variants"]}
    assert set(tablet) == {"control", "treatment"}
    assert tablet["control"]["is_control"] is True
    assert tablet["control"]["variant_id"] == str(control.id)
    assert (tablet["control"]["sample_size"], tablet["control"]["conversions"]) == (
        0,
        0,
    )
    assert tablet["treatment"]["sample_size"] == 10
    assert tablet["treatment"]["is_control"] is False


# ---------------------------------------------------------------------------
# Results cache: versioned key, cleared by invalidate-cache
# ---------------------------------------------------------------------------


class _FakeRedis:
    """The handful of Redis calls CacheService makes, over one shared dict."""

    store: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    def ping(self):
        return True

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value.encode() if isinstance(value, str) else value
        return True

    def keys(self, pattern):
        return [k for k in self.store if fnmatch.fnmatchcase(k, pattern)]

    def delete(self, *keys):
        return sum(1 for k in keys if self.store.pop(k, None) is not None)


def test_results_cache_key_is_versioned_and_invalidate_cache_clears_it(
    admin_client, db_session, experiment
):
    control, treatment = _variants(experiment)
    _seed(db_session, experiment, control, "desktop", 20, 2)
    _seed(db_session, experiment, treatment, "desktop", 20, 4)
    _FakeRedis.store = {}

    with patch("redis.Redis", _FakeRedis):
        response = admin_client.get(f"/api/v1/results/{experiment.id}")
        assert response.status_code == 200, response.text

        keys = list(_FakeRedis.store)
        assert len(keys) == 1, keys
        (key,) = keys
        # The prefix invalidate-cache clears, then the engine version.
        assert key.startswith(f"results:{experiment.id}:{ENGINE_VERSION}:"), key

        response = admin_client.post(
            f"/api/v1/results/{experiment.id}/invalidate-cache"
        )
        assert response.status_code == 200, response.text

    assert _FakeRedis.store == {}, (
        f"invalidate-cache left {list(_FakeRedis.store)} behind"
    )


def test_a_cached_answer_from_another_engine_version_is_not_served(
    admin_client, db_session, experiment
):
    """An answer cached under the old, unversioned key is never read back."""
    control, treatment = _variants(experiment)
    _seed(db_session, experiment, control, "desktop", 20, 2)
    _seed(db_session, experiment, treatment, "desktop", 20, 4)

    with patch("redis.Redis", _FakeRedis):
        _FakeRedis.store = {}
        fresh = admin_client.get(f"/api/v1/results/{experiment.id}").json()
        (key,) = list(_FakeRedis.store)
        stale = dict(fresh, experiment_name="served from a stale cache entry")
        stale_bytes = json.dumps(stale).encode()
        # The key a 1.0.0 engine wrote, and the same key under another version.
        old_unversioned = key.replace(f":{ENGINE_VERSION}:", ":", 1)
        other_version = key.replace(f":{ENGINE_VERSION}:", ":0.0.0-old:", 1)
        _FakeRedis.store = {old_unversioned: stale_bytes, other_version: stale_bytes}

        body = admin_client.get(f"/api/v1/results/{experiment.id}").json()

    assert body["experiment_name"] == experiment.name
