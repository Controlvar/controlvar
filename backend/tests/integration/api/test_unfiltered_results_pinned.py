"""The unfiltered results, byte for byte, as main answered them (#1933).

#1933 adds a route that computes an experiment's results for the members of
one saved segment. It must not move a single byte of any answer that existed
before it. This pins seven of them, on one fixed experiment, as sha256 digests
of their canonical JSON (floats at 10 significant digits, as
``test_engine_fingerprint._canonical`` rounds them, keys sorted):

* ``GET /api/v1/results/{id}?use_cache=false``, computed;
* the same request answered from the results cache;
* the alias ``GET /api/v1/experiments/{id}/results``, computed;
* ``GET /api/v1/results/{id}?breakdown=tier&use_cache=false``;
* the three ``analysis_snapshots`` payloads the computed request writes
  (frequentist, Bayesian and sequential).

The digests were captured from main's code, in the commit that added this
file, before any of #1933's code existed: a pin captured from the new code
would pin nothing. ``golden/unfiltered_results.json`` holds the canonical JSON
each digest was taken over, so a failure names the first key that moved.

The fixture has Bayesian analysis and sequential testing on, fixed ids and
fixed timestamps, and the clock frozen at 2026-11-16T12:00:00Z (the Bayesian
seed and the sequential look schedule read the day). It holds the event shapes
the results read differently: a value beyond ``VALUE_BOUND`` and one with no
value, a count metric's event with no value, an exposure carrying a metric's
event name, a conversion tagged with a variant the user is not assigned to,
and an event from a user with no assignment.

This pin is permanent. It may be edited only in a pull request that bumps
``ENGINE_VERSION`` (add the new version's digests beside the old) or that adds a
``docs/api/stability.md`` "Stable operations changed" row for ``/results``.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
from freezegun import freeze_time

from backend.app.api.v1.endpoints import results as results_module
from backend.app.core.stats_engine import ENGINE_VERSION
from backend.app.models.analysis_snapshot import AnalysisSnapshot
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    ExperimentType,
    Metric,
    MetricType,
    Variant,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

GOLDEN = Path(__file__).parent / "golden" / "unfiltered_results.json"
FROZEN_AT = "2026-11-16T12:00:00Z"

#: The digests of main's answers, by the engine version that computed them.
PINNED: Dict[str, Dict[str, str]] = {
    "1.12.0": {
        "computed": "56ccde8265418a0a48956781a7111d674a3151625ce3654bfc394969b4178f76",
        "cache_hit": "56ccde8265418a0a48956781a7111d674a3151625ce3654bfc394969b4178f76",
        "alias": "56ccde8265418a0a48956781a7111d674a3151625ce3654bfc394969b4178f76",
        "breakdown_tier": "bd1eed327f1bacd4949df3eb625fb5f61df82cdbc73007b59b179548700bae16",
        "snapshot_frequentist": (
            "56ccde8265418a0a48956781a7111d674a3151625ce3654bfc394969b4178f76"
        ),
        "snapshot_bayesian": (
            "394e798838ef9745f7faefa4029f0bd8e4902cadfae9329b7bc4aaae98f05a3d"
        ),
        "snapshot_sequential": (
            "d9d705ebd77321676e62a674b2885b6e0e2eb5a9c4bfe88d511a8b7ddf5eec3f"
        ),
    },
    # #2041: each mean metric's p-value comes from a permutation test of
    # Welch's t (permutation_t_test): Load time 0.2344 -> 0.266, Revenue
    # 0.2202 -> 0.168; Sessions only renames its test; engine_version 1.13.0.
    # The sequential snapshot carries no mean p-value and no engine version.
    "1.13.0": {
        "computed": "bddb9b3a23f316a334397d0ed9ef764443f962382f0c7df2162158b20cc124f2",
        "cache_hit": "bddb9b3a23f316a334397d0ed9ef764443f962382f0c7df2162158b20cc124f2",
        "alias": "bddb9b3a23f316a334397d0ed9ef764443f962382f0c7df2162158b20cc124f2",
        "breakdown_tier": "6a2d0fb48b0d51135a620a50f43b4b90c0026cdf2a663b11cb24927401e1be32",
        "snapshot_frequentist": (
            "bddb9b3a23f316a334397d0ed9ef764443f962382f0c7df2162158b20cc124f2"
        ),
        "snapshot_bayesian": (
            "4b0f2aebc8d8d6ed31d183c730ab1f9e24ade50a182fad88f4c2a01421f53c9d"
        ),
        "snapshot_sequential": (
            "d9d705ebd77321676e62a674b2885b6e0e2eb5a9c4bfe88d511a8b7ddf5eec3f"
        ),
    },
    # #1934: each metric carries conversion_window_hours and each of its
    # variants open_window_users, both null here (no metric has a window), and
    # engine_version is 1.14.0. Without exactly those, every document is
    # 1.13.0's: test_the_window_keys_are_all_that_moved_from_1_13_0.
    "1.14.0": {
        "computed": "426a6504804dd12b52d3479e376aef2d18620551bb05d779c006247ce2b0a57d",
        "cache_hit": "426a6504804dd12b52d3479e376aef2d18620551bb05d779c006247ce2b0a57d",
        "alias": "426a6504804dd12b52d3479e376aef2d18620551bb05d779c006247ce2b0a57d",
        "breakdown_tier": "5c60a6580ed524bf84a39d68073b99ff31bd1e6665bfda9530bd2d1ff749f3da",
        "snapshot_frequentist": (
            "426a6504804dd12b52d3479e376aef2d18620551bb05d779c006247ce2b0a57d"
        ),
        "snapshot_bayesian": (
            "fdcec1ddfc9bc1e13a6ab9ea7f189abb8a9846bd5958a3c26235433ef599969d"
        ),
        "snapshot_sequential": (
            "d9d705ebd77321676e62a674b2885b6e0e2eb5a9c4bfe88d511a8b7ddf5eec3f"
        ),
    },
}

#: The keys 1.14.0 added (#1934): on each metric, and on each of its variants.
WINDOW_KEYS = ("conversion_window_hours", "open_window_users")

# --- the fixed experiment ------------------------------------------------------

EXPERIMENT_ID = uuid.UUID("19330000-0000-4000-8000-000000000001")
CONTROL_ID = uuid.UUID("19330000-0000-4000-8000-000000000011")
TREATMENT_ID = uuid.UUID("19330000-0000-4000-8000-000000000012")
PURCHASE_ID = uuid.UUID("19330000-0000-4000-8000-000000000021")
REVENUE_ID = uuid.UUID("19330000-0000-4000-8000-000000000022")
SESSIONS_ID = uuid.UUID("19330000-0000-4000-8000-000000000023")
LOAD_TIME_ID = uuid.UUID("19330000-0000-4000-8000-000000000024")

BASE = datetime(2026, 11, 2, 9, 0, 0)

#: user -> variant, as Fixture M assigns them.
ASSIGNED = {
    "u01": CONTROL_ID,
    "u02": CONTROL_ID,
    "u03": CONTROL_ID,
    "u07": CONTROL_ID,
    "u08": CONTROL_ID,
    "u12": CONTROL_ID,
    "u04": TREATMENT_ID,
    "u05": TREATMENT_ID,
    "u06": TREATMENT_ID,
    "u09": TREATMENT_ID,
    "u10": TREATMENT_ID,
    "u11": TREATMENT_ID,
}

#: (user, event_type, event_name, value, day offset, variant or None for the
#: user's own, "untagged" for no variant). Every event is tagged with the
#: experiment.
EVENTS: List[tuple] = [
    # purchases (the primary conversion metric)
    ("u01", "purchase", "purchase", 1.0, 1, None),
    ("u01", "purchase", "purchase", 1.0, 3, None),
    ("u04", "purchase", "purchase", 1.0, 1, None),
    ("u05", "purchase", "purchase", 1.0, 2, None),
    ("u07", "purchase", "purchase", 1.0, 2, None),
    ("u09", "purchase", "purchase", 1.0, 4, None),
    ("u10", "purchase", "purchase", 1.0, 5, None),
    ("u11", "purchase", "purchase", 1.0, 6, None),
    # revenue (a mean metric): beyond VALUE_BOUND and NULL add nothing
    ("u01", "revenue", "revenue", 5.0, 1, None),
    ("u04", "revenue", "revenue", 10.0, 1, None),
    ("u05", "revenue", "revenue", 30.0, 2, None),
    ("u09", "revenue", "revenue", 100.0, 4, None),
    ("u02", "revenue", "revenue", 2e12, 3, None),
    ("u06", "revenue", "revenue", None, 3, None),
    # sessions (a count metric): an event with no value still counts 1
    ("u01", "session", "session", 1.0, 1, None),
    ("u01", "session", "session", None, 2, None),
    ("u01", "session", "session", 1.0, 3, None),
    ("u04", "session", "session", 1.0, 1, None),
    ("u10", "session", "session", 1.0, 5, None),
    ("u10", "session", "session", 1.0, 6, None),
    # load time (a duration metric, lower is better)
    ("u01", "load_time", "load_time", 1.5, 1, None),
    ("u02", "load_time", "load_time", 2.0, 2, None),
    ("u03", "load_time", "load_time", 2.5, 2, None),
    ("u04", "load_time", "load_time", 0.5, 1, None),
    ("u05", "load_time", "load_time", 0.7, 2, None),
    ("u06", "load_time", "load_time", 0.9, 3, None),
    # an exposure that carries a metric's event name is never a conversion
    ("u03", "exposure", "purchase", None, 2, None),
    # a conversion tagged with a variant the user is not assigned to
    ("u02", "purchase", "purchase", 1.0, 4, TREATMENT_ID),
    # an event tagged with the experiment from a user with no assignment
    ("x14", "purchase", "purchase", 1.0, 3, "untagged"),
]


def _tier(user_id: str) -> str:
    return "gold" if int(user_id[1:]) % 2 else "silver"


def _build(db_session, owner_id) -> None:
    experiment = Experiment(
        id=EXPERIMENT_ID,
        name="Unfiltered results pin",
        key="unfiltered-results-pin-1933",
        description="The unfiltered answer, pinned (#1933)",
        hypothesis="Nothing moves",
        status=ExperimentStatus.ACTIVE,
        experiment_type=ExperimentType.A_B,
        owner_id=owner_id,
        start_date=datetime(2026, 11, 1, 0, 0, 0),
        sequential_testing_enabled=True,
        sequential_testing_config={"method": "msprt"},
        bayesian_enabled=True,
        bayesian_config={"prior_family": "beta", "alpha": 1.0, "beta": 1.0},
        created_at=datetime(2026, 11, 1, 0, 0, 0),
        updated_at=datetime(2026, 11, 1, 0, 0, 0),
    )
    db_session.add(experiment)
    db_session.flush()
    for variant_id, name, is_control, second in (
        (CONTROL_ID, "control", True, 1),
        (TREATMENT_ID, "treatment", False, 2),
    ):
        db_session.add(
            Variant(
                id=variant_id,
                experiment_id=EXPERIMENT_ID,
                name=name,
                is_control=is_control,
                traffic_allocation=50,
                created_at=datetime(2026, 11, 1, 0, 0, second),
                updated_at=datetime(2026, 11, 1, 0, 0, second),
            )
        )
    for metric_id, name, event_name, metric_type, primary, lower in (
        (PURCHASE_ID, "Purchase", "purchase", MetricType.CONVERSION, True, False),
        (REVENUE_ID, "Revenue", "revenue", MetricType.REVENUE, False, False),
        (SESSIONS_ID, "Sessions", "session", MetricType.COUNT, False, False),
        (LOAD_TIME_ID, "Load time", "load_time", MetricType.DURATION, False, True),
    ):
        db_session.add(
            Metric(
                id=metric_id,
                experiment_id=EXPERIMENT_ID,
                name=name,
                event_name=event_name,
                metric_type=metric_type,
                is_primary=primary,
                lower_is_better=lower,
                created_at=datetime(2026, 11, 1, 0, 0, 0),
                updated_at=datetime(2026, 11, 1, 0, 0, 0),
            )
        )
    db_session.flush()
    for index, (user_id, variant_id) in enumerate(sorted(ASSIGNED.items())):
        at = BASE + timedelta(minutes=index)
        db_session.add(
            Assignment(
                id=uuid.UUID(f"19330000-0000-4000-8001-{index:012d}"),
                experiment_id=EXPERIMENT_ID,
                variant_id=variant_id,
                user_id=user_id,
                created_at=at,
                updated_at=at,
            )
        )
        # The assignment event, carrying the user's tier for the breakdown.
        db_session.add(
            Event(
                id=uuid.UUID(f"19330000-0000-4000-8002-{index:012d}"),
                event_type="exposure",
                event_name="variant_exposure",
                user_id=user_id,
                experiment_id=EXPERIMENT_ID,
                variant_id=variant_id,
                event_metadata={"tier": _tier(user_id)},
                created_at=at.isoformat() + "Z",
                updated_at=at,
            )
        )
    for index, (user_id, event_type, event_name, value, day, tag) in enumerate(EVENTS):
        at = BASE + timedelta(days=day, minutes=index)
        if tag == "untagged":
            variant_id = None
        elif tag is not None:
            variant_id = tag
        else:
            variant_id = ASSIGNED[user_id]
        db_session.add(
            Event(
                id=uuid.UUID(f"19330000-0000-4000-8003-{index:012d}"),
                event_type=event_type,
                event_name=event_name,
                user_id=user_id,
                experiment_id=EXPERIMENT_ID,
                variant_id=variant_id,
                value=value,
                created_at=at.isoformat() + "Z",
                updated_at=at,
            )
        )
    db_session.commit()


def _remove(db_session) -> None:
    db_session.rollback()
    for model in (AnalysisSnapshot, Event, Assignment, Metric, Variant):
        db_session.query(model).filter(model.experiment_id == EXPERIMENT_ID).delete(
            synchronize_session=False
        )
    db_session.query(Experiment).filter(Experiment.id == EXPERIMENT_ID).delete(
        synchronize_session=False
    )
    db_session.commit()


class _DictCache:
    """The results cache, kept in a dict: the same strings Redis would hold,
    in every environment (locally Redis is unreachable)."""

    def __init__(self) -> None:
        self.store: Dict[str, str] = {}

    def get(self, key: str) -> Optional[str]:
        return self.store.get(key)

    def set(self, key: str, value: str, expire: int = 0) -> None:
        self.store[key] = value


@pytest.fixture
def pinned_experiment(db_session, admin_user) -> Iterator[None]:
    _remove(db_session)
    _build(db_session, admin_user.id)
    try:
        yield
    finally:
        _remove(db_session)


# --- canonical JSON ----------------------------------------------------------------


def canonical(value: Any) -> Any:
    """JSON with floats at 10 significant digits, as the engine fingerprint
    rounds them: last-bit differences between BLAS/libm builds disappear,
    nothing an operator would read does."""
    if isinstance(value, dict):
        return {str(k): canonical(v) for k, v in value.items()}
    if isinstance(value, list):
        return [canonical(v) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return repr(value)
        return float(f"{value:.10g}")
    raise TypeError(f"not JSON: {type(value).__name__}")


def digest(value: Any) -> str:
    payload = json.dumps(canonical(value), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def first_difference(old: Any, new: Any, path: str = "") -> Optional[str]:
    """The first key path where two canonical documents differ, or None."""
    if type(old) is not type(new):
        return f"{path or '.'}: {old!r} != {new!r}"
    if isinstance(old, dict):
        for key in sorted(set(old) | set(new)):
            if key not in old:
                return f"{path}.{key}: added"
            if key not in new:
                return f"{path}.{key}: removed"
            found = first_difference(old[key], new[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(old, list):
        if len(old) != len(new):
            return f"{path}: {len(old)} items != {len(new)} items"
        for index, (a, b) in enumerate(zip(old, new)):
            found = first_difference(a, b, f"{path}[{index}]")
            if found:
                return found
        return None
    if old != new:
        return f"{path or '.'}: {old!r} != {new!r}"
    return None


# --- the seven answers ---------------------------------------------------------------


def _ok(response) -> Dict[str, Any]:
    assert response.status_code == 200, response.text
    return response.json()


def _snapshot_payloads(db_session) -> Dict[str, Any]:
    db_session.expire_all()
    rows = (
        db_session.query(AnalysisSnapshot)
        .filter(AnalysisSnapshot.experiment_id == EXPERIMENT_ID)
        .all()
    )
    by_kind = {str(getattr(row.kind, "value", row.kind)): row.payload for row in rows}
    assert sorted(by_kind) == ["bayesian", "frequentist", "sequential"], sorted(by_kind)
    return by_kind


def answers(client, db_session, monkeypatch) -> Dict[str, Any]:
    """The seven documents, in the order they are pinned."""
    cache = _DictCache()
    monkeypatch.setattr(results_module, "_get_cache_service", lambda: cache)
    # Imported by the engine on first use. Importing pandas (which statsmodels
    # imports) under a frozen clock does not finish, so it is done first.
    importlib.import_module("statsmodels.stats.power")
    results = f"/api/v1/results/{EXPERIMENT_ID}"
    out: Dict[str, Any] = {}
    # real_asyncio: the test client's event loop keeps the real monotonic
    # clock, or it would wait for ever on a timer that never comes due.
    with freeze_time(FROZEN_AT, real_asyncio=True):
        out["computed"] = _ok(client.get(results, params={"use_cache": "false"}))
        snapshots = _snapshot_payloads(db_session)
        assert len(cache.store) == 1, sorted(cache.store)
        out["cache_hit"] = _ok(client.get(results))
        cache.store.clear()
        out["alias"] = _ok(client.get(f"/api/v1/experiments/{EXPERIMENT_ID}/results"))
        out["breakdown_tier"] = _ok(
            client.get(results, params={"breakdown": "tier", "use_cache": "false"})
        )
    out["snapshot_frequentist"] = snapshots["frequentist"]
    out["snapshot_bayesian"] = snapshots["bayesian"]
    out["snapshot_sequential"] = snapshots["sequential"]
    return out


def _golden() -> Dict[str, Any]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


# --- tests ------------------------------------------------------------------------------


def test_the_pin_is_for_this_engine_version():
    assert ENGINE_VERSION in PINNED, (
        f"no unfiltered results pin for ENGINE_VERSION {ENGINE_VERSION}: an "
        "engine bump adds its own digests, captured from the bumping PR's "
        "merge base and the bump together, beside the old ones"
    )
    assert sorted(PINNED[ENGINE_VERSION]) == sorted(_golden()["documents"])


def test_the_golden_file_is_what_the_digests_were_taken_over():
    """The stored canonical JSON cannot drift from the pinned digests."""
    golden = _golden()
    assert golden["engine_version"] == ENGINE_VERSION
    for name, document in golden["documents"].items():
        assert digest(document) == PINNED[ENGINE_VERSION][name], name


def test_the_unfiltered_answers_are_byte_identical(
    pinned_experiment, admin_client, db_session, monkeypatch
):
    found = answers(admin_client, db_session, monkeypatch)
    golden = _golden()["documents"]
    moved = []
    for name, document in found.items():
        new = digest(document)
        if new != PINNED[ENGINE_VERSION][name]:
            where = first_difference(golden[name], canonical(document))
            moved.append(
                f"unfiltered {name} digest {new} != pinned "
                f"{PINNED[ENGINE_VERSION][name]}; first difference {where}"
            )
    assert moved == [], "\n".join(moved)


def without_the_window_keys(document: Any) -> Tuple[Any, Tuple[int, int, int]]:
    """*document* with exactly the two keys 1.14.0 added removed, after
    checking each is null, and each ``engine_version`` 1.14.0 set back to
    1.13.0; and how many metrics, variants and versions that touched."""
    metric_key, variant_key = WINDOW_KEYS
    touched = {"metrics": 0, "variants": 0, "versions": 0}

    def walk(value: Any) -> Any:
        if isinstance(value, list):
            return [walk(v) for v in value]
        if not isinstance(value, dict):
            return value
        out = {}
        for key, item in value.items():
            if key == "metrics" and isinstance(item, list):
                out[key] = []
                for metric in item:
                    metric = dict(metric)
                    assert metric.pop(metric_key) is None
                    touched["metrics"] += 1
                    metric["variants"] = [dict(v) for v in metric["variants"]]
                    for variant in metric["variants"]:
                        assert variant.pop(variant_key) is None
                        touched["variants"] += 1
                    out[key].append(walk(metric))
            elif key == "engine_version":
                assert item == "1.14.0", item
                out[key] = "1.13.0"
                touched["versions"] += 1
            else:
                out[key] = walk(item)
        return out

    stripped = walk(document)
    return stripped, (touched["metrics"], touched["variants"], touched["versions"])


def test_the_window_keys_are_all_that_moved_from_1_13_0():
    """Byte for byte (#1934): every pinned document of this engine, without
    exactly the two window keys and with its engine version set back, has
    1.13.0's digest, so the pins above are main's answers with nothing else
    moved."""
    assert ENGINE_VERSION == "1.14.0"
    golden = _golden()["documents"]
    counts = {}
    for name, document in golden.items():
        stripped, counts[name] = without_the_window_keys(document)
        assert digest(stripped) == PINNED["1.13.0"][name], name
    # Four metrics of two variants each, and one engine version (the
    # Bayesian block's); the breakdown's answer has the same; the Bayesian
    # snapshot only its engine version, the sequential one neither.
    assert counts == {
        "computed": (4, 8, 1),
        "cache_hit": (4, 8, 1),
        "alias": (4, 8, 1),
        "breakdown_tier": (4, 8, 1),
        "snapshot_frequentist": (4, 8, 1),
        "snapshot_bayesian": (0, 0, 1),
        "snapshot_sequential": (0, 0, 0),
    }


def test_the_documents_carry_what_the_pin_is_meant_to_cover(
    pinned_experiment, admin_client, db_session, monkeypatch
):
    """A guard on the fixture, so the pin cannot go vacuous: the Bayesian and
    sequential blocks are present, the breakdown is the tier one, and the
    metrics are the four kinds the results read differently."""
    found = answers(admin_client, db_session, monkeypatch)
    computed = found["computed"]
    assert computed["bayesian_results"] is not None
    assert computed["sequential_testing"] is not None
    assert computed["srm"] is not None
    assert computed["breakdown"] is None
    assert [m["metric_name"] for m in computed["metrics"]] == [
        "Purchase",
        "Load time",
        "Revenue",
        "Sessions",
    ]
    assert computed["summary"]["total_users"] == 12
    # 12 assignment events and 29 others, x14's included.
    assert computed["summary"]["total_events"] == 12 + len(EVENTS)
    assert found["breakdown_tier"]["breakdown"]["dimension"] == "tier"
    assert computed == found["cache_hit"] == found["alias"]
