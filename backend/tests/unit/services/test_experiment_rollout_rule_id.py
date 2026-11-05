"""Each experiment's partial rollout admits its own users (#533).

The rules engine admits a user to a rule with a partial ``rollout_percentage``
when ``md5("<user_id>:<rule id>") % 100`` is below it. A dashboard rule with no
top-level ``id`` got the shared id ``"dashboard"``, so two such experiments at
50% admitted the same half of the users instead of independent halves.

Now an experiment's partial-rollout rule is given the experiment's id when it
first starts (``start_experiment`` and the scheduler, DRAFT -> ACTIVE only), so:

* 533a: two id-less 50% experiments started now overlap on about a quarter of
  the users, and exactly the users ``md5(user:<experiment id>)`` selects;
* a resume from PAUSED never stamps (an experiment already running keeps the
  users ``"dashboard"`` admits);
* every write of ``ExperimentStatus.ACTIVE`` in ``backend/app`` goes through
  the helper (the listing test fails on a third one);
* 533c: flags never bucket on a rule id, so their evaluation is unchanged.

The API, PAUSED-edit and scheduler journeys against a database are in
``backend/tests/integration/api/test_experiment_rollout_rule_id.py``.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    ExperimentType,
    Metric,
    MetricType,
    Variant,
)
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.services.assignment_service import AssignmentService
from backend.app.services.experiment_service import (
    ExperimentService,
    rules_for_clone,
)
from backend.app.services.feature_flag_service import FeatureFlagService
from backend.app.services.rules_evaluation_service import RulesEvaluationService

pytestmark = pytest.mark.unit

N_USERS = 10_000
USERS = [f"user-{i}" for i in range(N_USERS)]


def _rules(rollout=50, **extra):
    rules = {
        "logical_operator": "AND",
        "groups": [
            {
                "logical_operator": "AND",
                "conditions": [
                    {"attribute": "country", "operator": "equals", "value": "US"}
                ],
            }
        ],
        **extra,
    }
    if rollout is not None:
        rules["rollout_percentage"] = rollout
    return rules


def _experiment(
    rules, status=ExperimentStatus.DRAFT, experiment_id=None, **values
) -> Experiment:
    experiment = Experiment(
        id=experiment_id or uuid.uuid4(),
        name=f"rollout {uuid.uuid4().hex[:6]}",
        key=f"rollout-{uuid.uuid4().hex[:6]}",
        owner_id=uuid.uuid4(),
        status=status,
        experiment_type=ExperimentType.A_B,
        targeting_rules=rules,
        **values,
    )
    experiment.variants = [
        Variant(
            id=uuid.uuid4(), name="Control", is_control=True, traffic_allocation=50
        ),
        Variant(id=uuid.uuid4(), name="B", is_control=False, traffic_allocation=50),
    ]
    experiment.metric_definitions = [
        Metric(
            id=uuid.uuid4(),
            name="Purchase",
            event_name="purchase",
            metric_type=MetricType.CONVERSION,
            is_primary=True,
        )
    ]
    return experiment


def _start(experiment: Experiment) -> None:
    ExperimentService(MagicMock()).start_experiment(experiment)


def _admitted(experiment: Experiment) -> set[str]:
    """The users the assignment path's targeting admits, for US users."""
    service = AssignmentService.__new__(AssignmentService)
    service.rules_evaluation_service = RulesEvaluationService()
    admitted = set()
    for user_id in USERS:
        context = AssignmentService._build_targeting_context(user_id, {"country": "US"})
        if service._evaluate_experiment_targeting(experiment, context)["eligible"]:
            admitted.add(user_id)
    return admitted


def _md5_admitted(salt: str, percentage: int = 50) -> set[str]:
    return {
        user_id
        for user_id in USERS
        if int(
            hashlib.md5(
                f"{user_id}:{salt}".encode(), usedforsecurity=False
            ).hexdigest(),
            16,
        )
        % 100
        < percentage
    }


# --- 533a -----------------------------------------------------------------------


@pytest.mark.regression
def test_two_idless_50pct_experiments_overlap_quarter():
    # Fixed ids, so the counts are the same on every run.
    first = _experiment(_rules(50), experiment_id=uuid.UUID(int=1))
    second = _experiment(_rules(50), experiment_id=uuid.UUID(int=2))
    _start(first)
    _start(second)

    a, b = _admitted(first), _admitted(second)

    # 2,500 +- 5 sd (sd = sqrt(10,000 * .25 * .75) = 43.3) and 5,000 +- 5 sd.
    assert 2_283 <= len(a & b) <= 2_717, (len(a), len(b), len(a & b))
    assert 4_750 <= len(a) <= 5_250, len(a)
    assert 4_750 <= len(b) <= 5_250, len(b)
    # Exactly the users the experiment's own id selects.
    assert a == _md5_admitted(str(first.id))
    assert b == _md5_admitted(str(second.id))


@pytest.mark.regression
def test_first_start_stamps_the_experiments_id():
    experiment = _experiment(_rules(50))
    _start(experiment)
    assert experiment.status == ExperimentStatus.ACTIVE
    assert experiment.targeting_rules == {**_rules(50), "id": str(experiment.id)}


@pytest.mark.parametrize(
    "rules",
    [
        _rules(None),
        _rules(100),
        _rules(100.0),
        _rules(50, id="chosen-id"),
        {"logical_operator": "AND", "groups": [], "rollout_percentage": 50},
        {
            "logical_operator": "AND",
            "groups": [{"logical_operator": "AND", "conditions": []}],
            "rollout_percentage": 50,
        },
        {"rules": [], "default_rule": None},
        None,
        {},
    ],
    ids=[
        "no rollout",
        "rollout 100",
        "rollout 100.0",
        "an id of its own",
        "no groups",
        "only a group without conditions",
        "native shape",
        "no rules",
        "empty",
    ],
)
def test_first_start_leaves_other_rules_alone(rules):
    before = None if rules is None else dict(rules)
    experiment = _experiment(rules)
    _start(experiment)
    assert experiment.targeting_rules == before


@pytest.mark.regression
def test_rollout_below_100_by_fraction_stamps():
    """``_from_dashboard`` reads 99.5 as 99, so it buckets and is stamped."""
    experiment = _experiment(_rules(99.5))
    _start(experiment)
    assert experiment.targeting_rules["id"] == str(experiment.id)


def test_resume_from_paused_writes_nothing():
    """C7: a PAUSED experiment started before the change keeps ``"dashboard"``."""
    experiment = _experiment(_rules(50), status=ExperimentStatus.PAUSED)
    _start(experiment)
    assert experiment.status == ExperimentStatus.ACTIVE
    assert experiment.targeting_rules == _rules(50)
    assert _admitted(experiment) == _md5_admitted("dashboard")


# --- a clone gets its own id ------------------------------------------------------


@pytest.mark.regression
def test_a_clone_drops_the_id_stamped_from_its_source():
    source = _experiment(_rules(50))
    _start(source)
    assert rules_for_clone(source) == _rules(50)
    # The source's stored rules are not changed by the copy.
    assert source.targeting_rules == {**_rules(50), "id": str(source.id)}


@pytest.mark.parametrize(
    "rules",
    [_rules(50, id="checkout-half"), _rules(50), None, {"country": ["US"]}],
    ids=["an id a caller chose", "no id", "no rules", "a flat value"],
)
def test_a_clone_keeps_everything_else(rules):
    source = _experiment(rules)
    assert rules_for_clone(source) == rules


# --- the scheduler -------------------------------------------------------------


def _tick(experiments):
    from backend.app.core.scheduler import ExperimentScheduler

    db = MagicMock()
    activate = MagicMock()
    activate.filter.return_value.all.return_value = experiments
    complete = MagicMock()
    complete.filter.return_value.all.return_value = []
    db.query.side_effect = [activate, complete]
    scheduler = ExperimentScheduler()
    scheduler._notification_service = MagicMock()
    with patch("backend.app.core.scheduler.SessionLocal", return_value=db):
        asyncio.run(scheduler.process_scheduled_experiments())


@pytest.mark.regression
def test_scheduled_first_start_stamps():
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    experiment = _experiment(_rules(50), start_date=past)
    _tick([experiment])
    assert experiment.status == ExperimentStatus.ACTIVE
    assert experiment.targeting_rules == {**_rules(50), "id": str(experiment.id)}


def test_scheduled_resume_writes_nothing():
    """C7, through the scheduler: a resume due now keeps ``"dashboard"``."""
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    experiment = _experiment(_rules(50), status=ExperimentStatus.PAUSED, resume_at=past)
    _tick([experiment])
    assert experiment.status == ExperimentStatus.ACTIVE
    assert experiment.targeting_rules == _rules(50)


# --- every write of ExperimentStatus.ACTIVE stamps ------------------------------

REPO = Path(__file__).resolve().parents[4]

#: (file, enclosing function) of every place that sets an experiment ACTIVE.
#: A third one must call ``stamp_rollout_rule_id`` before it is added here.
ACTIVE_WRITES = {
    ("backend/app/services/experiment_service.py", "start_experiment"),
    ("backend/app/core/scheduler.py", "_process_due"),
}


def _is_active_member(node: ast.AST) -> bool:
    """``ExperimentStatus.ACTIVE`` or ``ExperimentStatus("active")``."""
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "ACTIVE"
        and isinstance(node.value, ast.Name)
        and node.value.id == "ExperimentStatus"
    ):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ExperimentStatus"
        and any(
            isinstance(arg, ast.Constant) and str(arg.value).lower() == "active"
            for arg in node.args
        )
    )


def _active_writes(root: Path, base: Path = REPO) -> dict[tuple[str, str], ast.AST]:
    """Every assignment, keyword argument or dict value that is ACTIVE."""
    found: dict[tuple[str, str], ast.AST] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions = [
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]

        def enclosing(node: ast.AST) -> ast.AST | None:
            inside = [
                f
                for f in functions
                if f.lineno <= node.lineno <= (f.end_lineno or f.lineno)
            ]
            return max(inside, key=lambda f: f.lineno) if inside else None

        for node in ast.walk(tree):
            values: list[ast.AST] = []
            if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
                values = [node.value] if node.value is not None else []
            elif isinstance(node, ast.keyword):
                values = [node.value]
            elif isinstance(node, ast.Dict):
                values = list(node.values)
            for value in values:
                if _is_active_member(value):
                    owner = enclosing(value)
                    name = owner.name if owner is not None else "<module>"
                    found[(path.relative_to(base).as_posix(), name)] = owner
    return found


@pytest.mark.regression
def test_every_active_write_stamps():
    roots = [REPO / "backend" / "app"]
    if (REPO / "modules" / "backend" / "app").is_dir():
        roots.append(REPO / "modules" / "backend" / "app")
    found: dict[tuple[str, str], ast.AST] = {}
    for root in roots:
        found.update(_active_writes(root))

    assert set(found) == ACTIVE_WRITES
    for key, function in found.items():
        calls = {
            n.func.id
            for n in ast.walk(function)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "stamp_rollout_rule_id" in calls, key


def test_the_listing_sees_each_form(tmp_path):
    """The scan is not vacuous: each form of a write is found."""
    package = tmp_path / "backend" / "app"
    package.mkdir(parents=True)
    (package / "forms.py").write_text(
        "def a(e):\n    e.status = ExperimentStatus.ACTIVE\n"
        "def b(q):\n    q.update(status=ExperimentStatus.ACTIVE)\n"
        "def c(q):\n    q.update({'status': ExperimentStatus('active')})\n"
        "def d(e):\n    return e.status == ExperimentStatus.ACTIVE\n",
        encoding="utf-8",
    )
    found = _active_writes(package, base=tmp_path)
    assert {name for _, name in found} == {"a", "b", "c"}


# --- 533c: flags never bucket on a rule id ---------------------------------------


@pytest.mark.parametrize(
    "rule_id",
    [None, "dashboard", "5f0c8a52-6d0e-4c1b-9a43-0e7f1d2b9c11"],
    ids=["no id", "dashboard", "an experiment-style id"],
)
def test_flag_rollout_ignores_the_rule_id(rule_id):
    rules = _rules(50) if rule_id is None else _rules(50, id=rule_id)
    flag = FeatureFlag(
        id=uuid.uuid4(),
        key="checkout-redesign",
        name="Checkout redesign",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=0,
        targeting_rules=rules,
        owner_id=uuid.uuid4(),
    )
    service = FeatureFlagService(MagicMock())
    with patch(
        "backend.app.services.feature_flag_service.MetricsService.record_flag_evaluation"
    ):
        enabled = {
            user_id
            for user_id in USERS
            if service.evaluate_flag(flag, user_id, {"country": "US"})
        }
    assert enabled == _md5_admitted("checkout-redesign")
