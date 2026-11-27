"""
Datasets and an in-memory database for the tests of the mean-metric views.

A revenue, count, duration or custom metric is analysed by ``/results`` as
its mean per assigned user, from the values ``mean_arms.prepare_mean_arms``
reads.  The other results views (sequential, Bayesian, sample size, daily,
live and the breakdown) still read the share of users who sent the event.
The tests that move each view onto the mean, and the test that no view moves
without an engine bump (``test_mean_views_engine_bump.py``), share what is
here:

* ``SHARE_IS_NOT_THE_MEAN``: 2,000 users per arm and 200 buyers in each.  The
  control's buyers spend 40 or 60 (100 each), the treatment's 52 or 78, so the
  means per user are 5.0 and 6.5 while the share of buyers is 0.10 in both.
  A view that reads the share answers "no difference" on it; a view that
  reads the mean answers a difference of exactly 1.5.  Every user also has a
  day of assignment (one of three) and a platform (``web`` or ``ios``; every
  ``web`` buyer spends the low amount), so the daily series and the
  breakdown have rows to read.
* ``ZERO_INFLATED_REVENUE``: the zero-inflated, right-skewed revenue of
  ``test_false_winner_unequal_split.py`` (a user buys with probability 0.05,
  orders again with probability 0.3, each order lognormal(3, 1)).  A battery
  that draws from it commits its seed before it is first run.
* ``reference``: the dataset's means, variances and difference worked out
  exactly with ``fractions.Fraction`` from the dataset's definition, with no
  product code.
* ``ViewsSession`` and ``install``: the dataset as a database.  The session
  answers the statements the six views make on the dataset, and ``install``
  replaces the readers that do not go through the session (the converting
  users, the first conversion times, each user's value and event count, the
  breakdown's rows).  ``run_view`` runs one view as its route runs it.

A statement the session does not know, and a reader asked about another
experiment, variant or metric, is recorded in ``ViewsSession.unexpected``
rather than raised (the live view catches what its reads raise), and every
test that runs a view asserts the list is empty.  A view that starts reading
something new therefore fails until this module answers it.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, Sequence

import pytest

from backend.app.api.v1.endpoints import results as results_module
from backend.app.models.assignment import Assignment
from backend.app.models.experiment import Experiment, MetricType, Variant
from backend.app.services import analysis_service as analysis_module
from backend.app.services import event_matching
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.event_matching import MetricEvents
from backend.app.services.results_streaming_service import ResultsStreamingService
from backend.app.services.segment_breakdown import Segment
from backend.tests.unit.stats_validation.test_false_winner_unequal_split import (
    Revenue,
)

# ---------------------------------------------------------------------------
# The datasets
# ---------------------------------------------------------------------------

EXPERIMENT_ID = "6f1e0b5a-3c2d-4e8f-9a01-00000000e001"
METRIC_ID = "6f1e0b5a-3c2d-4e8f-9a01-00000000a001"
#: The control's id sorts after the treatment's, so a view that took the
#: control to be the first id would answer differently.
CONTROL_ID = "ffffffff-3c2d-4e8f-9a01-0000000000c0"
TREATMENT_ID = "00000000-3c2d-4e8f-9a01-0000000000a1"

#: The metric's event.
EVENT = "purchase"
#: Users per arm, and every ``BUYER_EVERY``-th of them buys.
USERS_PER_ARM = 2_000
BUYER_EVERY = 10
#: What a buyer spends: the low amount, then the high one, alternately.
SPEND = {CONTROL_ID: (40.0, 60.0), TREATMENT_ID: (52.0, 78.0)}
#: The three days users are assigned on (a buyer buys an hour later).
FIRST_DAY = datetime(2026, 1, 5, tzinfo=timezone.utc)
DAYS = 3
#: The breakdown's attribute and its two values.
DIMENSION = "platform"
PLATFORMS = ("ios", "web")

#: When the Bayesian view runs, and the live view's timestamp: fixed, because
#: the Monte Carlo seed is derived from the day.
AS_OF = datetime(2026, 1, 8, 9, 0, tzinfo=timezone.utc)

#: The six views that read a metric beside ``/results`` itself.
VIEWS = ("sequential", "bayesian", "sample_size", "daily", "live", "breakdown")


@dataclass(frozen=True)
class DatasetUser:
    """One assigned user of the dataset."""

    user_id: str
    variant_id: str
    assigned_at: datetime
    platform: str
    #: What the user spent, or None for a user who did not buy.
    value: Optional[float]


def _arm(variant_id: str) -> List[DatasetUser]:
    low, high = SPEND[variant_id]
    users = []
    for i in range(USERS_PER_ARM):
        buyer = i % BUYER_EVERY == 0
        users.append(
            DatasetUser(
                user_id=f"{variant_id[:8]}-{i:04d}",
                variant_id=variant_id,
                assigned_at=FIRST_DAY + timedelta(days=i % DAYS, hours=12),
                platform="web" if i % (2 * BUYER_EVERY) < BUYER_EVERY else "ios",
                value=(low if (i // BUYER_EVERY) % 2 == 0 else high) if buyer else None,
            )
        )
    return users


#: Every assigned user of ``SHARE_IS_NOT_THE_MEAN``, the control's first.
SHARE_IS_NOT_THE_MEAN: Sequence[DatasetUser] = tuple(
    _arm(CONTROL_ID) + _arm(TREATMENT_ID)
)

#: Draws one arm of zero-inflated revenue: ``draw(rng, users)`` gives the
#: number of buyers and each buyer's total.
ZERO_INFLATED_REVENUE = Revenue()


class Reference(NamedTuple):
    """``SHARE_IS_NOT_THE_MEAN`` worked out exactly from its definition."""

    users: Dict[str, int]
    buyers: Dict[str, int]
    means: Dict[str, Fraction]
    #: Each arm's variance over its users (ddof 1), zeros included.
    variances: Dict[str, Fraction]
    #: The within-arm variance pooled over both arms (ddof 1 in each).
    pooled_variance: Fraction
    #: The treatment's mean minus the control's.
    difference: Fraction


def reference() -> Reference:
    """The means and variances by hand: 1,800 zeros and 100 of each spend."""
    users: Dict[str, int] = {}
    buyers: Dict[str, int] = {}
    means: Dict[str, Fraction] = {}
    variances: Dict[str, Fraction] = {}
    squares: Dict[str, Fraction] = {}
    for variant_id, (low, high) in SPEND.items():
        n = USERS_PER_ARM
        each = n // BUYER_EVERY // 2
        mean = Fraction(each * (int(low) + int(high)), n)
        ss = (
            (n - 2 * each) * mean**2
            + each * (int(low) - mean) ** 2
            + each * (int(high) - mean) ** 2
        )
        users[variant_id] = n
        buyers[variant_id] = 2 * each
        means[variant_id] = mean
        squares[variant_id] = ss
        variances[variant_id] = ss / (n - 1)
    pooled = sum(squares.values()) / sum(n - 1 for n in users.values())
    return Reference(
        users=users,
        buyers=buyers,
        means=means,
        variances=variances,
        pooled_variance=pooled,
        difference=means[TREATMENT_ID] - means[CONTROL_ID],
    )


# ---------------------------------------------------------------------------
# The experiment and the in-memory database
# ---------------------------------------------------------------------------


def experiment(*, dated: bool = False) -> SimpleNamespace:
    """The dataset's experiment, with one revenue metric as its primary.

    ``dated`` gives it a start and an end around the three days, which only
    the daily series reads; the sequential and live views read the time since
    the start, so for them it has none and their answer does not depend on
    the day the test runs.
    """
    metric = SimpleNamespace(
        id=METRIC_ID,
        name="Revenue per user",
        event_name=EVENT,
        metric_type=MetricType.REVENUE,
        is_primary=True,
        lower_is_better=False,
        minimum_sample_size=100,
        expected_effect=None,
        conversion_window_hours=None,
    )
    variants = [
        SimpleNamespace(
            id=CONTROL_ID, name="Control", is_control=True, traffic_allocation=50.0
        ),
        SimpleNamespace(
            id=TREATMENT_ID, name="Treatment", is_control=False, traffic_allocation=50.0
        ),
    ]
    return SimpleNamespace(
        id=EXPERIMENT_ID,
        name="Share is not the mean",
        status="active",
        start_date=FIRST_DAY if dated else None,
        end_date=FIRST_DAY + timedelta(days=DAYS - 1, hours=23) if dated else None,
        variants=variants,
        metric_definitions=[metric],
        confidence_level=0.95,
        correction_method="benjamini_hochberg",
        optimization_type="fixed",
        sequential_testing_enabled=True,
        sequential_testing_config={},
        bayesian_enabled=True,
        bayesian_config={},
        bayesian_decision=None,
        variance_reduction_config=None,
    )


class _CountRow(NamedTuple):
    variant_id: str
    cnt: int


def _describe(entity: Any) -> str:
    return str(getattr(entity, "element", entity))


class _Query:
    """One statement: the entities asked for and the filters applied."""

    def __init__(self, session: "ViewsSession", entities: Sequence[Any]) -> None:
        self._session = session
        self._entities = tuple(entities)
        self._clauses: List[Any] = []
        self._grouped = False

    def options(self, *_options: Any) -> "_Query":
        return self

    def filter(self, *clauses: Any) -> "_Query":
        self._clauses.extend(clauses)
        return self

    def group_by(self, *_columns: Any) -> "_Query":
        self._grouped = True
        return self

    # -- what a filter keeps --------------------------------------------------

    def _wanted(self, table: str) -> Dict[str, Any]:
        """``{column: [(operator, value)]}`` of the filters on ``table``; any
        other filter is recorded."""
        wanted: Dict[str, Any] = {}
        for clause in self._clauses:
            left = getattr(clause, "left", None)
            key = getattr(left, "key", None)
            on = getattr(getattr(left, "table", None), "name", None)
            right = getattr(getattr(clause, "right", None), "value", None)
            op = getattr(clause, "operator", None)
            if (
                key is None
                or on != table
                or op not in (operator.eq, operator.ge, operator.le)
            ):
                self._session.record(f"filter {clause}")
                continue
            wanted.setdefault(key, []).append((op, right))
        return wanted

    def _is(self, *entities: Any) -> bool:
        """Whether the statement asks for exactly ``entities`` (by identity:
        ``==`` on a column builds an expression)."""
        return len(self._entities) == len(entities) and all(
            mine is theirs for mine, theirs in zip(self._entities, entities)
        )

    def _users(self) -> List[DatasetUser]:
        wanted = self._wanted("assignments")
        kept = []
        for user in SHARE_IS_NOT_THE_MEAN:
            row = {
                "experiment_id": EXPERIMENT_ID,
                "variant_id": user.variant_id,
                "created_at": user.assigned_at,
            }
            if all(
                key in row and all(op(row[key], value) for op, value in tests)
                for key, tests in wanted.items()
            ):
                kept.append(user)
        for key in wanted:
            if key not in ("experiment_id", "variant_id", "created_at"):
                self._session.record(f"assignments filtered on {key}")
        return kept

    @staticmethod
    def _is_assignment_count(entity: Any) -> bool:
        """``func.count(Assignment.id)``, labelled or not, in any schema."""
        text = _describe(entity)
        return text.startswith("count(") and text.endswith("assignments.id)")

    # -- results --------------------------------------------------------------

    def first(self) -> Any:
        if self._is(Experiment):
            ids = [str(value) for _, value in self._wanted("experiments").get("id", [])]
            if ids == [EXPERIMENT_ID]:
                return self._session.experiment
            self._session.record(f"experiment {ids}")
            return None
        self._session.record(f"first() of {self._shape()}")
        return None

    def scalar(self) -> int:
        if len(self._entities) == 1 and self._is_assignment_count(self._entities[0]):
            return len(self._users())
        self._session.record(f"scalar() of {self._shape()}")
        return 0

    def all(self) -> List[Any]:
        if (
            len(self._entities) == 2
            and self._entities[0] is Assignment.variant_id
            and self._is_assignment_count(self._entities[1])
            and self._grouped
        ):
            counts: Dict[str, int] = {}
            for user in self._users():
                counts[user.variant_id] = counts.get(user.variant_id, 0) + 1
            return [_CountRow(variant_id, n) for variant_id, n in counts.items()]
        self._session.record(f"all() of {self._shape()}")
        return []

    def __iter__(self) -> Iterator[Any]:
        if self._is(Variant):
            wanted = self._wanted("variants")
            ids = [str(value) for _, value in wanted.get("experiment_id", [])]
            if ids == [EXPERIMENT_ID]:
                return iter(self._session.experiment.variants)
        self._session.record(f"iteration of {self._shape()}")
        return iter(())

    def _shape(self) -> str:
        return ", ".join(_describe(e) for e in self._entities)


class ViewsSession:
    """``SHARE_IS_NOT_THE_MEAN`` as a database session (see the module)."""

    def __init__(self, experiment: SimpleNamespace) -> None:
        self.experiment = experiment
        self.unexpected: List[str] = []

    def record(self, what: str) -> None:
        self.unexpected.append(what)

    def query(self, *entities: Any) -> _Query:
        return _Query(self, entities)

    def add(self, _row: Any) -> None:
        """The Bayesian view records its decision on the experiment."""

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


def _buyers(session: ViewsSession, experiment_id: Any, variant_id: Any, events: Any):
    """The variant's buyers, when asked about this dataset's metric."""
    if (
        str(experiment_id) != EXPERIMENT_ID
        or str(variant_id) not in SPEND
        or events != MetricEvents(EVENT, None)
    ):
        session.record(f"reader asked about {experiment_id} {variant_id} {events}")
        return []
    return [
        user
        for user in SHARE_IS_NOT_THE_MEAN
        if user.variant_id == str(variant_id) and user.value is not None
    ]


def install(session: ViewsSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the readers that do not go through the session from the dataset."""

    def converting(_db, experiment_id, variant_id, events):
        return len(_buyers(session, experiment_id, variant_id, events))

    def first_times(_db, experiment_id, variant_id, events):
        return [
            (user.assigned_at + timedelta(hours=1)).isoformat()
            for user in _buyers(session, experiment_id, variant_id, events)
        ]

    def value_totals(_self, experiment_id, variant_id, events):
        return [
            float(user.value)
            for user in _buyers(session, experiment_id, variant_id, events)
        ]

    def event_counts(_self, experiment_id, variant_id, events):
        return [1.0 for _ in _buyers(session, experiment_id, variant_id, events)]

    def rows(_db, experiment_id, dimension, metrics, *, value_source=None):
        metrics = list(metrics)
        if (
            str(experiment_id) != EXPERIMENT_ID
            or dimension != DIMENSION
            or value_source is not None
        ):
            session.record(f"segment rows of {experiment_id} by {dimension}")
            return []
        segments = []
        for platform in PLATFORMS:
            members = [u for u in SHARE_IS_NOT_THE_MEAN if u.platform == platform]
            users: Dict[str, int] = {}
            for user in members:
                users[user.variant_id] = users.get(user.variant_id, 0) + 1
            converters = {
                events: {
                    variant_id: sum(
                        1
                        for user in _buyers(session, experiment_id, variant_id, events)
                        if user.platform == platform
                    )
                    for variant_id in SPEND
                }
                for events in metrics
            }
            segments.append(Segment(value=platform, users=users, converters=converters))
        return segments

    monkeypatch.setattr(event_matching, "count_converting_users", converting)
    monkeypatch.setattr(analysis_module, "count_converting_users", converting)
    monkeypatch.setattr(analysis_module, "first_conversion_times", first_times)
    monkeypatch.setattr(AnalysisService, "value_totals", value_totals)
    monkeypatch.setattr(AnalysisService, "event_counts", event_counts)
    monkeypatch.setattr(results_module, "segment_rows", rows)


# ---------------------------------------------------------------------------
# The six views
# ---------------------------------------------------------------------------


def run_view(view: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    """``view`` on ``SHARE_IS_NOT_THE_MEAN``, as its route computes it, with
    the fields that are text rather than a computed answer (the analysis
    notice, the heterogeneity warning's sentence) and the engine version set
    aside.  Asserts that nothing unexpected was read."""
    session = ViewsSession(experiment(dated=view == "daily"))
    install(session, monkeypatch)
    exp = session.experiment
    output: Any
    if view == "sequential":
        output = results_module._compute_sequential_response(
            exp, session, alpha=None
        ).model_dump(mode="json")
        output.pop("analysis_status")
        output.pop("analysis_notice")
    elif view == "bayesian":
        output = (
            AnalysisService(session)
            .compute_bayesian_results(exp, as_of=AS_OF)
            .model_dump(mode="json")
        )
        output.pop("engine_version")
    elif view == "sample_size":
        output = results_module.get_sample_size_status(
            exp.id,
            baseline_conversion_rate=None,
            mde=None,
            confidence_level=None,
            power_target=0.80,
            correction_method=None,
            db=session,
            current_user=None,
        ).model_dump(mode="json")
    elif view == "daily":
        output = results_module.get_experiment_daily_results(
            exp.id,
            metric_id=None,
            start_date=None,
            end_date=None,
            db=session,
            current_user=None,
        ).model_dump(mode="json")
    elif view == "live":
        output = ResultsStreamingService(lambda: session)._compute_snapshot(
            exp.id, session, AS_OF.isoformat()
        )
    elif view == "breakdown":
        output = results_module._compute_dimensional_breakdown(
            exp.id, DIMENSION, session, 0.05
        ).model_dump(mode="json")
        output.pop("hte_warning")
    else:
        raise ValueError(f"no view {view!r}")
    assert session.unexpected == [], (
        f"{view} read something this harness does not answer: {session.unexpected}"
    )
    return output
