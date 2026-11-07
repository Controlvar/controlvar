"""The overview's ``experiments_with_winners`` (#244).

It counts the experiments created in the period whose current results name a
winner: the winner the experiment export's ``winner_variant`` column shows.
Each experiment's results come from the call the export makes,
``get_experiment_results(id, include_bayesian=False)``, read through the same
per-service memo. An experiment whose results cannot be computed
(``ValueError``) is not counted; any other error fails the overview.

It used to count saved reports through a column they do not have, swallow
the error and answer 0 for every request.

The database is a list of experiments and the analysis a fake that answers
each experiment's results by id and records every call it is asked.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.sql import operators

from backend.app.api import deps
from backend.app.main import app
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.user import UserRole
from backend.app.schemas.export import ExportFormat, ExportRequest
from backend.app.services.export_service import ExportService

ANALYSIS = "backend.app.services.export_service.AnalysisService"
JAN = datetime(2026, 1, 1, tzinfo=timezone.utc)
FEB = JAN + timedelta(days=31)


class _Rows:
    """``db.query(Model)`` over rows held in a list.

    ``filter`` applies a comparison on one of the model's own columns to the
    rows, as the database would; a condition on another table's column
    narrows nothing.
    """

    def __init__(self, table: Optional[str], rows: List[Any]) -> None:
        self.table = table
        self.rows = rows

    def filter(self, condition: Any) -> "_Rows":
        column = condition.left
        if self.table is None or column.table.name != self.table:
            return self
        if condition.operator is operators.is_not:
            kept = [r for r in self.rows if getattr(r, column.key) is not None]
        else:
            bound = condition.right.value
            kept = [
                r
                for r in self.rows
                if condition.operator(getattr(r, column.key), bound)
            ]
        return _Rows(self.table, kept)

    def group_by(self, *columns: Any) -> "_Rows":
        return self

    def count(self) -> int:
        return len(self.rows)

    def all(self) -> List[Any]:
        return list(self.rows)


class _Db:
    """A session whose experiments table holds ``experiments``; every other table is empty."""

    def __init__(self, experiments: List[Any]) -> None:
        self.experiments = experiments

    def query(self, *entities: Any) -> _Rows:
        if entities[0] is Experiment:
            return _Rows(Experiment.__tablename__, self.experiments)
        return _Rows(None, [])


class _Analyses:
    """Stands in for ``AnalysisService``: each experiment's results by id.

    ``calls`` records the arguments of every ``get_experiment_results`` call.
    An answer that is an exception is raised.
    """

    def __init__(self, answers: Dict[Any, Any]) -> None:
        self.answers = answers
        self.calls: List[Tuple[tuple, dict]] = []

    def __call__(self, db: Any) -> "_Analyses":
        return self

    def get_experiment_results(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append((args, kwargs))
        answer = self.answers[args[0]]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _experiment(created_at: datetime = JAN, status=ExperimentStatus.ACTIVE):
    """An experiment with a control and a treatment variant."""
    experiment_id = uuid.uuid4()
    return SimpleNamespace(
        id=experiment_id,
        name=f"Experiment {experiment_id.hex[:6]}",
        status=status,
        experiment_type=SimpleNamespace(value="a_b"),
        created_at=created_at,
        start_date=None,
        end_date=None,
        variants=[
            SimpleNamespace(id=uuid.uuid4(), name="control", is_control=True),
            SimpleNamespace(id=uuid.uuid4(), name="treatment", is_control=False),
        ],
    )


def _results(winning_variant_id: Optional[str], recommendation: str):
    return {
        "metrics": [],
        "summary": {
            "has_winner": winning_variant_id is not None,
            "winning_variant_id": winning_variant_id,
            "recommendation": recommendation,
        },
    }


def _won(experiment, recommendation: str = "SHIP_VARIANT"):
    """Results whose summary names the experiment's treatment as the winner."""
    return _results(str(experiment.variants[1].id), recommendation)


def _no_winner():
    return _results(None, "CONTINUE_TESTING")


def _overview(experiments, answers, start=None, end=None):
    analyses = _Analyses(answers)
    with patch(ANALYSIS, analyses):
        report = ExportService(_Db(experiments)).generate_platform_overview(
            start_date=start, end_date=end
        )
    return report, analyses


@pytest.mark.regression
def test_overview_counts_experiments_with_a_winner():
    """Three experiments whose results name a winner count three; it was 0."""
    experiments = [_experiment() for _ in range(3)]

    report, _ = _overview(experiments, {e.id: _won(e) for e in experiments})

    assert report.experiments_with_winners == 3


@pytest.mark.regression
def test_the_count_is_the_export_winner_column_for_the_same_period():
    """2 of the 5 experiments in the period count, as the export's column shows.

    Not counted: one whose results cannot be computed, one with no winner,
    and one whose winning id names none of its variants (a state only a fake
    produces). A winner created before the period is outside it.
    """
    inside = JAN + timedelta(days=10)
    won = [_experiment(inside), _experiment(inside)]
    cannot = _experiment(inside)
    no_winner = _experiment(inside)
    unresolved = _experiment(inside)
    before = _experiment(JAN - timedelta(days=1))
    experiments = won + [cannot, no_winner, unresolved, before]
    answers = {
        **{e.id: _won(e) for e in won},
        cannot.id: ValueError("Experiment has no control variant"),
        no_winner.id: _no_winner(),
        unresolved.id: _results(str(uuid.uuid4()), "SHIP_VARIANT"),
        before.id: _won(before),
    }
    service = ExportService(_Db(experiments))

    with patch(ANALYSIS, _Analyses(answers)):
        report = service.generate_platform_overview(start_date=JAN, end_date=FEB)
        content, _ = service.export_experiments(
            ExportRequest(format=ExportFormat.JSON, start_date=JAN, end_date=FEB)
        )

    rows = json.loads(content)
    assert report.total_experiments == len(rows) == 5
    assert report.experiments_with_winners == 2
    assert sum(1 for row in rows if row["winner_variant"]) == 2


@pytest.mark.regression
def test_each_analysis_is_the_call_the_export_makes():
    """No confidence level and no correction method are passed, and no Bayesian
    analysis is asked for: the experiment's stored settings, as the export."""
    experiments = [_experiment() for _ in range(3)]

    _, analyses = _overview(experiments, {e.id: _won(e) for e in experiments})

    assert [args for args, _ in analyses.calls] == [(e.id,) for e in experiments]
    for _, kwargs in analyses.calls:
        assert kwargs == {"include_bayesian": False}


@pytest.mark.regression
def test_the_period_is_on_the_experiments_created_at():
    """Both ends are inclusive; an experiment created before or after is not counted."""
    in_period = [
        _experiment(JAN),
        _experiment(JAN + timedelta(days=3)),
        _experiment(FEB),
    ]
    out_of_period = [
        _experiment(JAN - timedelta(microseconds=1)),
        _experiment(FEB + timedelta(microseconds=1)),
    ]
    experiments = in_period + out_of_period

    report, analyses = _overview(
        experiments, {e.id: _won(e) for e in experiments}, start=JAN, end=FEB
    )

    assert report.total_experiments == 3
    assert report.experiments_with_winners == 3
    assert {args[0] for args, _ in analyses.calls} == {e.id for e in in_period}


@pytest.mark.regression
def test_an_analysis_error_fails_the_overview():
    """An error other than results that cannot be computed is raised, not counted as 0."""
    healthy, failing = _experiment(), _experiment()
    answers = {
        healthy.id: _won(healthy),
        failing.id: RuntimeError("database went away"),
    }

    with patch(ANALYSIS, _Analyses(answers)):
        with pytest.raises(RuntimeError, match="database went away"):
            ExportService(_Db([healthy, failing])).generate_platform_overview()


@pytest.mark.regression
def test_an_analysis_error_answers_500_and_never_0():
    experiment = _experiment()
    user = SimpleNamespace(
        id=uuid.uuid4(), role=UserRole.ADMIN, is_active=True, is_superuser=False
    )
    app.dependency_overrides[deps.get_current_active_user] = lambda: user
    app.dependency_overrides[deps.get_db] = lambda: _Db([experiment])
    try:
        with patch(
            ANALYSIS, _Analyses({experiment.id: RuntimeError("database went away")})
        ):
            response = TestClient(app, raise_server_exceptions=False).get(
                "/api/v1/export/reports/overview"
            )
    finally:
        app.dependency_overrides.pop(deps.get_current_active_user, None)
        app.dependency_overrides.pop(deps.get_db, None)

    assert response.status_code == 500
    assert "experiments_with_winners" not in response.text
    assert "database went away" not in response.text


@pytest.mark.regression
def test_an_experiment_whose_results_cannot_be_computed_is_not_counted(caplog):
    cannot = _experiment()
    won = [_experiment(), _experiment()]
    answers = {
        cannot.id: ValueError("Experiment has no control variant"),
        **{e.id: _won(e) for e in won},
    }

    with caplog.at_level(logging.INFO, logger="backend.app.services.export_service"):
        report, _ = _overview([cannot] + won, answers)

    assert report.experiments_with_winners == 2
    levels = [r.levelno for r in caplog.records if str(cannot.id) in r.getMessage()]
    assert levels == [logging.INFO]


@pytest.mark.regression
def test_one_analysis_per_experiment_and_none_for_an_empty_period():
    """The count reads each experiment's results once, through the export's memo."""
    experiments = [_experiment() for _ in range(3)]
    answers = {e.id: _won(e) for e in experiments}

    _, alone = _overview(experiments, answers)
    assert len(alone.calls) == 3

    # An export and the overview on one service compute each result once.
    shared = _Analyses(answers)
    service = ExportService(_Db(experiments))
    with patch(ANALYSIS, shared):
        service.export_experiments(ExportRequest(format=ExportFormat.JSON))
        report = service.generate_platform_overview()
    assert report.experiments_with_winners == 3
    assert len(shared.calls) == 3

    report, empty = _overview(experiments, answers, start=FEB, end=FEB)
    assert report.experiments_with_winners == 0
    assert empty.calls == []
