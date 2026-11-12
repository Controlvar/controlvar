"""``GET /api/v1/results/{experiment_id}/balanced-groups`` (#448).

An experiment that balances its split within groups gets its results by the
group each user was balanced in, the group stored on their assignment, and
one size-weighted estimate per treatment over the groups. These tests pin:

* the rows are the stored groups, built the way a breakdown's are, and per
  variant they add up to every user assigned, rows that stored no group (as
  an older server version writes them) counted under ``unknown``;
* for users assigned through ``/tracking/assign`` with a listed value, the
  rows are exactly ``?breakdown=<attribute>``'s; where the two differ, they
  differ as documented: users assigned from a list, a later event with the
  key, a value of 101 to 255 characters;
* ``users_outside_blocks`` counts every row with no place in its group, a
  fallback row that kept its group included; ``unknown_share`` counts only
  the rows that stored a group;
* each row's threshold is the Bonferroni one over the rows returned, and the
  overall estimate is corrected across the treatments;
* a mean primary metric, sequential testing and too few users give no
  estimate, with the reason; an experiment that balances nothing answers 409;
* the group source reads one column of ``assignments`` and nothing else, and
  the route reads and writes no cache;
* no body carries the experiment's block-order key or a user's place.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pytest
from sqlalchemy import event as sa_event

from backend.app.api.v1.endpoints import results as results_module
from backend.app.core.stats_engine import ENGINE_VERSION
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    Metric,
    MetricType,
    Variant,
)
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.services.assignment_service import AssignmentService
from backend.app.services.balanced_groups_results import stored_strata
from backend.app.services.sufficient_stats_analysis import adjusted_p_values

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

TIER = {"attribute": "tier", "values": ["gold", "silver"]}
PREFIX = "bg448-"
BASE = datetime(2026, 11, 1, 12, 0, 0)
NOT_BALANCED = (
    "This experiment does not balance its split within groups, so it has no "
    "balanced groups. For its results by an attribute, add "
    "?breakdown=<attribute> to its results."
)


# ---------------------------------------------------------------------------
# Fixtures and seeding
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_active_holdout(db_session):
    """No global holdout during the test; restored after."""
    parked = (
        db_session.query(GlobalHoldout).filter(GlobalHoldout.is_active.is_(True)).all()
    )
    for row in parked:
        row.is_active = False
    db_session.commit()
    yield
    db_session.rollback()
    for row in parked:
        db_session.merge(row).is_active = True
    db_session.commit()


@pytest.fixture
def make_balanced(db_session, admin_user):
    """``make(setting=TIER, shares=(50, 50), metric_type=CONVERSION, **fields)``."""
    created: List[uuid.UUID] = []

    def make(
        setting: Optional[dict] = TIER,
        shares: Tuple[int, ...] = (50, 50),
        metric_type: MetricType = MetricType.CONVERSION,
        **fields: Any,
    ) -> Experiment:
        suffix = uuid.uuid4().hex[:8]
        experiment = Experiment(
            name=f"Balanced groups {suffix}",
            key=f"bg448-{suffix}",
            status=ExperimentStatus.ACTIVE,
            owner_id=admin_user.id,
            balance_within=setting,
            **fields,
        )
        db_session.add(experiment)
        db_session.flush()
        created.append(experiment.id)
        for index, share in enumerate(shares):
            db_session.add(
                Variant(
                    experiment_id=experiment.id,
                    name="control" if index == 0 else f"treatment{index}",
                    is_control=index == 0,
                    traffic_allocation=share,
                )
            )
        db_session.add(
            Metric(
                experiment_id=experiment.id,
                name="Purchase",
                event_name="purchase",
                metric_type=metric_type,
                is_primary=True,
            )
        )
        db_session.commit()
        db_session.refresh(experiment)
        return experiment

    yield make
    db_session.rollback()
    for experiment_id in created:
        for model in (Event, Assignment, Metric, Variant):
            db_session.query(model).filter(model.experiment_id == experiment_id).delete(
                synchronize_session=False
            )
        db_session.query(Experiment).filter(Experiment.id == experiment_id).delete(
            synchronize_session=False
        )
    db_session.commit()


def _variant(experiment: Experiment, name: str) -> Variant:
    (variant,) = [v for v in experiment.variants if v.name == name]
    return variant


def _assign(client, experiment, user: str, context: Optional[dict]) -> Dict[str, Any]:
    body: Dict[str, Any] = {"experiment_key": experiment.key, "user_id": PREFIX + user}
    if context is not None:
        body["context"] = context
    response = client.post("/api/v1/tracking/assign", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["assigned"], response.json()
    return response.json()


def _convert(db, experiment, user: str, index: int = 0) -> None:
    """A purchase of an assigned user, tagged with their variant."""
    variant_id = (
        db.query(Assignment.variant_id)
        .filter(
            Assignment.experiment_id == experiment.id,
            Assignment.user_id == PREFIX + user,
        )
        .scalar()
    )
    db.add(
        Event(
            event_type="purchase",
            event_name="purchase",
            user_id=PREFIX + user,
            experiment_id=experiment.id,
            variant_id=variant_id,
            value=1.0,
            event_metadata=None,
            created_at=(BASE + timedelta(minutes=index)).isoformat() + "+00:00",
            updated_at=BASE + timedelta(seconds=index),
        )
    )


def _rows_written(
    db, experiment, rows: Iterable[Tuple[str, str, Optional[str], Optional[int]]]
) -> None:
    """Assignment rows written directly: ``(user, variant name, group, seq)``."""
    db.bulk_insert_mappings(
        Assignment,
        [
            {
                "id": uuid.uuid4(),
                "experiment_id": experiment.id,
                "variant_id": _variant(experiment, arm).id,
                "user_id": PREFIX + user,
                "balance_group": group,
                "balance_seq": seq,
            }
            for user, arm, group, seq in rows
        ],
    )
    db.commit()


def _balanced(client, experiment) -> Dict[str, Any]:
    response = client.get(f"/api/v1/results/{experiment.id}/balanced-groups")
    assert response.status_code == 200, response.text
    return response.json()


def _breakdown(client, experiment, dimension: str = "tier") -> Dict[str, Any]:
    response = client.get(
        f"/api/v1/results/{experiment.id}",
        params={"breakdown": dimension, "use_cache": "false"},
    )
    assert response.status_code == 200, response.text
    return response.json()["breakdown"]


def _rows(groups: Dict[str, Any]) -> Dict[str, Dict[str, Tuple[int, int]]]:
    return {
        s["segment_value"]: {
            v["variant_name"]: (v["sample_size"], v["conversions"])
            for v in s["variants"]
        }
        for s in groups["segments"]
    }


def _stored(db, experiment) -> Dict[str, Dict[str, int]]:
    """Independently: the stored group of every row (NULL as unknown), per arm."""
    db.rollback()
    names = {v.id: v.name for v in experiment.variants}
    counts: Dict[str, Dict[str, int]] = {}
    for group, variant_id in db.query(
        Assignment.balance_group, Assignment.variant_id
    ).filter(Assignment.experiment_id == experiment.id):
        row = counts.setdefault(group or "unknown", {})
        row[names[variant_id]] = row.get(names[variant_id], 0) + 1
    return counts


def _total(rows: Dict[str, Dict[str, Tuple[int, int]]]) -> int:
    return sum(n for row in rows.values() for n, _ in row.values())


def _assigned(db, experiment) -> int:
    db.rollback()
    return (
        db.query(Assignment).filter(Assignment.experiment_id == experiment.id).count()
    )


# ---------------------------------------------------------------------------
# The rows are the stored groups
# ---------------------------------------------------------------------------


def test_the_rows_are_the_stored_groups_and_add_up_to_every_user(
    admin_client, db_session, make_balanced
):
    experiment = make_balanced()
    contexts = [{"tier": "gold"}] * 7 + [{"tier": "silver"}] * 5
    contexts += [{"tier": "bronze"}] * 3 + [None] * 2 + [{}] * 2
    for index, context in enumerate(contexts):
        _assign(admin_client, experiment, f"u{index}", context)
    for index in range(0, len(contexts), 3):
        _convert(db_session, experiment, f"u{index}", index)
    db_session.commit()

    body = _balanced(admin_client, experiment)
    rows = _rows(body["groups"])
    stored = _stored(db_session, experiment)
    assert {g: {a: n for a, (n, _) in r.items() if n} for g, r in rows.items()} == {
        g: dict(r) for g, r in stored.items()
    }
    assert list(rows) == ["gold", "silver", "other", "unknown"]
    total = _total(rows)
    assigned = _assigned(db_session, experiment)
    assert total == assigned, f"Σ {total} != {assigned:,}"
    assert sum(c for row in rows.values() for _, c in row.values()) == 7
    assert body["engine_version"] == ENGINE_VERSION == "1.11.0"
    assert body["groups"]["dimension"] == "tier"
    assert body["groups"]["is_exploratory"] is True
    assert body["users_outside_blocks"] == 0
    assert body["unknown_share"] == pytest.approx(4 / 19)


def test_for_users_assigned_with_a_listed_value_the_rows_are_the_breakdowns(
    admin_client, db_session, make_balanced
):
    """Every user assigned through /tracking/assign with a listed value, so
    their exposure carries it: the two routes' rows are the same."""
    experiment = make_balanced()
    # Twice as many silver as gold, so the rows' order (by users) is not the
    # values' order.
    for index in range(24):
        _assign(
            admin_client,
            experiment,
            f"l{index}",
            {"tier": ("gold", "silver", "silver")[index % 3]},
        )
    for index in range(0, 24, 4):
        _convert(db_session, experiment, f"l{index}", index)
    db_session.commit()

    balanced = _balanced(admin_client, experiment)["groups"]
    breakdown = _breakdown(admin_client, experiment)
    assert balanced["segments"] == breakdown["segments"]
    assert balanced["adjusted_alpha"] == breakdown["adjusted_alpha"]


# ---------------------------------------------------------------------------
# Where the rows differ from the breakdown's
# ---------------------------------------------------------------------------


def test_users_assigned_from_a_list_are_in_their_group_here_and_unknown_there(
    admin_client, db_session, make_balanced
):
    """A list writes no exposure, so the breakdown has no value for them."""
    experiment = make_balanced()
    service = AssignmentService(db_session)
    for index in range(6):
        result = service.assign_user(
            PREFIX + f"b{index}",
            experiment.id,
            track_exposure=False,
            context={"tier": ("gold", "silver", "gold")[index % 3]},
        )
        assert result["assigned"], result
    db_session.commit()

    here = _rows(_balanced(admin_client, experiment)["groups"])
    there = _rows(_breakdown(admin_client, experiment))
    assert sorted(here) == ["gold", "silver"]
    assert sum(n for n, _ in here["gold"].values()) == 4
    assert list(there) == ["unknown"]
    assert sum(n for n, _ in there["unknown"].values()) == 6


def test_a_later_keyed_event_moves_the_breakdown_and_not_the_group(
    admin_client, db_session, make_balanced
):
    experiment = make_balanced()
    _assign(admin_client, experiment, "k0", None)
    variant_id = (
        db_session.query(Assignment.variant_id)
        .filter(Assignment.user_id == PREFIX + "k0")
        .scalar()
    )
    db_session.add(
        Event(
            event_type="page_view",
            event_name="page_view",
            user_id=PREFIX + "k0",
            experiment_id=experiment.id,
            variant_id=variant_id,
            event_metadata={"tier": "gold"},
            created_at=(BASE + timedelta(days=1)).isoformat() + "+00:00",
            updated_at=BASE + timedelta(days=1),
        )
    )
    db_session.commit()

    assert list(_rows(_balanced(admin_client, experiment)["groups"])) == ["unknown"]
    assert list(_rows(_breakdown(admin_client, experiment))) == ["gold"]


def test_a_value_of_101_to_255_characters_is_other_here_and_itself_there(
    admin_client, db_session, make_balanced
):
    experiment = make_balanced()
    long_value = "g" * 150
    _assign(admin_client, experiment, "v0", {"tier": long_value})

    assert list(_rows(_balanced(admin_client, experiment)["groups"])) == ["other"]
    assert list(_rows(_breakdown(admin_client, experiment))) == [long_value]


def test_rows_with_no_stored_group_count_under_unknown_and_outside_the_blocks(
    admin_client, db_session, make_balanced
):
    """Rows as an older server version writes them: no group, no place. They
    are under unknown and in users_outside_blocks, and not in unknown_share;
    the rows still add up to every user assigned."""
    experiment = make_balanced()
    rows = []
    for index in range(990):
        group = ("gold", "silver", "unknown")[index % 3]
        arm = ("control", "treatment1")[(index // 3) % 2]
        rows.append((f"s{index}", arm, group, index))
    rows += [
        (f"n{index}", ("control", "treatment1")[index % 2], None, None)
        for index in range(10)
    ]
    _rows_written(db_session, experiment, rows)

    body = _balanced(admin_client, experiment)
    found = _rows(body["groups"])
    total = _total(found)
    assert total == 1_000, f"Σ {total} != {1_000:,}"
    assert sum(n for n, _ in found["unknown"].values()) == 330 + 10
    assert body["users_outside_blocks"] == 10
    assert body["unknown_share"] == pytest.approx(330 / 990)


def test_a_fallback_row_that_kept_its_group_is_outside_the_blocks(
    admin_client, db_session, make_balanced
):
    """A user the server assigned by hash while it was busy keeps their group
    with no place in it."""
    experiment = make_balanced()
    rows = [
        (f"f{index}", ("control", "treatment1")[index % 2], "gold", index)
        for index in range(5)
    ]
    rows.append(("f-hash", "control", "gold", None))
    _rows_written(db_session, experiment, rows)

    body = _balanced(admin_client, experiment)
    assert body["users_outside_blocks"] == 1, (
        f"users_outside_blocks {body['users_outside_blocks']} != 1"
    )
    assert sum(n for n, _ in _rows(body["groups"])["gold"].values()) == 6
    assert body["unknown_share"] == 0.0


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------


def test_each_rows_threshold_counts_every_row_returned(
    admin_client, db_session, make_balanced
):
    experiment = make_balanced(
        setting={"attribute": "tier", "values": ["gold", "silver", "bronze"]}
    )
    rows = []
    for index, group in enumerate(
        ["gold"] * 8 + ["silver"] * 6 + ["bronze"] * 4 + ["other"] * 4 + ["unknown"] * 4
    ):
        rows.append((f"c{index}", ("control", "treatment1")[index % 2], group, index))
    _rows_written(db_session, experiment, rows)

    groups = _balanced(admin_client, experiment)["groups"]
    returned = len(groups["segments"])
    assert returned == 5
    assert groups["adjusted_alpha"] == pytest.approx(0.05 / returned), (
        f"{groups['adjusted_alpha']} != 0.05/{returned}"
    )


def test_the_overall_estimate_is_corrected_across_the_treatments(
    admin_client, db_session, make_balanced
):
    experiment = make_balanced(shares=(40, 30, 30))
    rows = []
    arms = ("control", "treatment1", "treatment2")
    for index in range(180):
        rows.append(
            (f"t{index}", arms[index % 3], ("gold", "silver")[index % 2], index)
        )
    _rows_written(db_session, experiment, rows)
    for index in range(0, 180, 5):
        _convert(db_session, experiment, f"t{index}", index)
    for index in range(1, 180, 9):
        _convert(db_session, experiment, f"t{index}", index)
    db_session.commit()

    body = _balanced(admin_client, experiment)
    assert body["correction_method"] == "benjamini_hochberg"
    overall = body["overall"]
    assert [e["variant_name"] for e in overall] == ["treatment1", "treatment2"]
    p_values = [e["p_value"] for e in overall]
    assert all(p is not None for p in p_values)
    assert [e["adjusted_p_value"] for e in overall] == pytest.approx(
        adjusted_p_values(p_values, "benjamini_hochberg")
    )


# ---------------------------------------------------------------------------
# No estimate, and the 409
# ---------------------------------------------------------------------------


def test_a_mean_primary_metric_has_no_estimate(admin_client, db_session, make_balanced):
    experiment = make_balanced(metric_type=MetricType.REVENUE)
    _rows_written(
        db_session,
        experiment,
        [("m0", "control", "gold", 0), ("m1", "treatment1", "gold", 1)],
    )
    body = _balanced(admin_client, experiment)
    assert body["overall"] is None and body["reason"] == "mean_metric"
    assert _total(_rows(body["groups"])) == 2


def test_sequential_testing_has_no_estimate(admin_client, db_session, make_balanced):
    experiment = make_balanced(sequential_testing_enabled=True)
    rows = [(f"q{i}", ("control", "treatment1")[i % 2], "gold", i) for i in range(8)]
    _rows_written(db_session, experiment, rows)
    body = _balanced(admin_client, experiment)
    assert body["overall"] is None and body["reason"] == "sequential_testing"


def test_too_few_users_has_no_estimate(admin_client, db_session, make_balanced):
    experiment = make_balanced()
    _rows_written(db_session, experiment, [("w0", "control", "gold", 0)])
    body = _balanced(admin_client, experiment)
    assert body["overall"] is None and body["reason"] == "too_few_users"
    no_users = make_balanced()
    body = _balanced(admin_client, no_users)
    assert body["groups"]["segments"] == []
    assert body["reason"] == "too_few_users"
    assert body["unknown_share"] is None


def test_a_small_group_is_merged_into_the_largest(
    admin_client, db_session, make_balanced
):
    experiment = make_balanced()
    rows = [(f"g{i}", ("control", "treatment1")[i % 2], "gold", i) for i in range(10)]
    rows += [(f"s{i}", ("control", "treatment1")[i % 2], "silver", i) for i in range(6)]
    rows += [("o0", "control", "other", 0)]
    _rows_written(db_session, experiment, rows)
    body = _balanced(admin_client, experiment)
    assert body["merged_groups"] == [{"group": "other", "into": "gold"}]
    assert body["reason"] is None and len(body["overall"]) == 1


def test_an_experiment_that_balances_nothing_answers_409(
    admin_client, db_session, make_balanced
):
    experiment = make_balanced(setting=None)
    response = admin_client.get(f"/api/v1/results/{experiment.id}/balanced-groups")
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == NOT_BALANCED


def test_an_unknown_experiment_answers_404(admin_client):
    response = admin_client.get(f"/api/v1/results/{uuid.uuid4()}/balanced-groups")
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# The group source reads one column of assignments
# ---------------------------------------------------------------------------


@pytest.fixture
def statements(db_session):
    """Every SQL statement the session's engine runs while the test reads."""
    seen: List[str] = []
    engine = db_session.get_bind()

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    sa_event.listen(engine, "before_cursor_execute", record)
    yield seen
    sa_event.remove(engine, "before_cursor_execute", record)


def test_the_group_source_reads_one_column_of_assignments(
    db_session, make_balanced, statements
):
    """One statement, from assignments alone, selecting the user id and the
    stored group; no value longer than the column's 100 characters, even when
    the row's context holds a longer one."""
    experiment = make_balanced()
    _rows_written(
        db_session,
        experiment,
        [("x0", "control", "g" * 100, 0), ("x1", "treatment1", "other", 0)],
    )
    db_session.query(Assignment).filter(Assignment.user_id == PREFIX + "x1").update(
        {"context": {"tier": "t" * 300}}, synchronize_session=False
    )
    db_session.commit()
    assert Assignment.__table__.c.balance_group.type.length == 100

    statements.clear()
    values = stored_strata(db_session, experiment.id, "tier")
    assert values == {PREFIX + "x0": "g" * 100, PREFIX + "x1": "other"}
    assert max(len(v) for v in values.values()) <= 100
    reads = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(reads) == 1, reads
    (statement,) = reads
    selected, rest = re.split(r"\s+FROM\s+", statement, maxsplit=1)
    columns = sorted(
        ".".join(c.strip().split(" AS ")[0].split(".")[-2:])
        for c in re.sub(r"^\s*SELECT\s+", "", selected).split(",")
    )
    assert columns == ["assignments.balance_group", "assignments.user_id"], statement
    tables = re.findall(r"\b(?:FROM|JOIN)\s+([\w.]+)", "FROM " + rest)
    assert [t.split(".")[-1] for t in tables] == ["assignments"], statement
    assert "events" not in statement, statement
    assert "context" not in statement, statement


def test_the_route_reads_and_writes_no_cache(
    admin_client, db_session, make_balanced, monkeypatch
):
    """The route never builds a cache client, so it shares nothing with
    ``/results?breakdown=``'s cached answers."""
    experiment = make_balanced()
    _rows_written(
        db_session,
        experiment,
        [(f"r{i}", ("control", "treatment1")[i % 2], "gold", i) for i in range(4)],
    )
    calls: List[str] = []

    class Recording:
        def get(self, key):
            calls.append(f"get {key}")
            return

        def set(self, key, value, expire=None):
            calls.append(f"set {key}")

    monkeypatch.setattr(results_module, "_get_cache_service", lambda: Recording())
    _balanced(admin_client, experiment)
    assert calls == [], calls
    _breakdown(admin_client, experiment)
    assert calls and all(":tier" in c for c in calls), calls


# ---------------------------------------------------------------------------
# No body carries the block-order key or a user's place
# ---------------------------------------------------------------------------


def _keys(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def test_the_body_carries_no_block_order_key_and_no_place(
    admin_client, db_session, make_balanced
):
    experiment = make_balanced()
    for index in range(6):
        _assign(admin_client, experiment, f"p{index}", {"tier": "gold"})
    # One silver user: a group merged into gold, so every kind of entry is in
    # the body (a group row, an overall entry, a merged group).
    _assign(admin_client, experiment, "p-silver", {"tier": "silver"})
    db_session.rollback()
    seed = (
        db_session.query(Experiment.balance_seed)
        .filter(Experiment.id == experiment.id)
        .scalar()
    )
    assert seed and len(seed) == 64
    body = _balanced(admin_client, experiment)
    assert body["groups"]["segments"] and body["overall"] and body["merged_groups"]
    text = json.dumps(body)
    assert seed not in text, "balance_seed value found in the balanced-groups body"
    found = {k for k in _keys(body) if k in ("balance_seed", "balance_seq", "next_seq")}
    assert not found, f"{sorted(found)} found in the balanced-groups body"
