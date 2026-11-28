"""Which archived experiments ``POST /experiments/{id}/unarchive`` brings back (#2278).

``unarchive_refusal`` answers None only for an ARCHIVED experiment with a
``start_date`` and an ``end_date`` at or before now; the route then makes it
COMPLETED. That is the case in which ``open_window_time`` already treats an
archived experiment as one whose counting stopped at its ``end_date``, so its
results read the same before and after. Every other case is refused, each for
its own reason, and the service refuses it too without committing.

No database: the rule reads three attributes, and the dates are relative to
the moment the test runs.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.app.models.experiment import ExperimentStatus
from backend.app.services.analysis_service import open_window_time
from backend.app.services.experiment_service import (
    NOT_ARCHIVED,
    UNARCHIVE_REFUSED,
    ExperimentService,
    unarchive_refusal,
)

pytestmark = [pytest.mark.unit]

NOW = datetime.now(timezone.utc).replace(tzinfo=None)
DAY = timedelta(days=1)


def _experiment(status=ExperimentStatus.ARCHIVED, start=NOW - 10 * DAY, end=NOW - DAY):
    return SimpleNamespace(
        status=status, start_date=start, end_date=end, id=None, name="unarchive"
    )


#: case -> (start_date, end_date, the answer)
ARCHIVED = {
    "started and ended": (NOW - 10 * DAY, NOW - DAY, None),
    "ended this instant": (NOW - 10 * DAY, NOW, None),
    "no start date": (None, NOW - DAY, UNARCHIVE_REFUSED),
    "no end date": (NOW - 10 * DAY, None, UNARCHIVE_REFUSED),
    # A day ahead, not seconds: the service tests read the real clock, which
    # passes NOW while the suite runs.
    "end date still ahead": (NOW - 10 * DAY, NOW + DAY, UNARCHIVE_REFUSED),
    "neither date": (None, None, UNARCHIVE_REFUSED),
}


@pytest.mark.parametrize("case", list(ARCHIVED))
def test_an_archived_experiment(case):
    start, end, answer = ARCHIVED[case]
    assert unarchive_refusal(_experiment(start=start, end=end), NOW) == answer


@pytest.mark.parametrize(
    "status",
    [s for s in ExperimentStatus if s != ExperimentStatus.ARCHIVED],
    ids=lambda s: s.value,
)
def test_an_experiment_that_is_not_archived_is_refused_whatever_its_dates(status):
    assert unarchive_refusal(_experiment(status=status), NOW) == NOT_ARCHIVED


def test_an_end_date_with_a_zone_is_compared_in_utc():
    """The columns store naive UTC; a request's dates arrive with a zone."""
    ahead = (NOW + timedelta(hours=1)).replace(tzinfo=timezone.utc)
    # The same instant written two hours east of UTC.
    east = ahead.astimezone(timezone(timedelta(hours=2)))
    assert unarchive_refusal(_experiment(end=east), NOW) == UNARCHIVE_REFUSED
    behind = (NOW - timedelta(hours=1)).replace(tzinfo=timezone.utc)
    assert unarchive_refusal(_experiment(end=behind), NOW) is None


@pytest.mark.parametrize("case", list(ARCHIVED))
def test_an_accepted_experiment_counts_its_windows_at_the_same_moment_after(case):
    """Brought back, an experiment's open conversion windows are counted where
    they were while it was archived: at its end date. Refused, an archived
    experiment with an end date still ahead would move from None to its end
    date, which is why the rule names the end date."""
    start, end, answer = ARCHIVED[case]
    archived = _experiment(start=start, end=end)
    completed = _experiment(status=ExperimentStatus.COMPLETED, start=start, end=end)
    before = open_window_time(archived, NOW)
    after = open_window_time(completed, NOW)
    if answer is None:
        assert before == after == end
    elif end is not None and start is not None:
        assert (before, after) == (None, end)


def test_the_refusals_say_what_to_do_and_claim_nothing_about_how_it_was_archived():
    assert UNARCHIVE_REFUSED == (
        "Only an experiment with a start date and a past end date can be "
        "unarchived. Clone it to run it again."
    )
    assert NOT_ARCHIVED == "Experiment is not archived"


@pytest.mark.parametrize("case", [c for c, (_, _, a) in ARCHIVED.items() if a])
def test_the_service_refuses_what_the_rule_refuses_and_commits_nothing(case):
    start, end, answer = ARCHIVED[case]
    db = MagicMock()
    experiment = _experiment(start=start, end=end)
    with pytest.raises(ValueError, match=re.escape(answer)):
        ExperimentService(db).unarchive_experiment(experiment)
    assert experiment.status == ExperimentStatus.ARCHIVED
    db.commit.assert_not_called()


def test_the_service_changes_only_the_status_of_an_accepted_experiment(monkeypatch):
    db = MagicMock()
    monkeypatch.setattr(ExperimentService, "_experiment_to_dict", lambda self, e: {})
    experiment = _experiment()
    start, end = experiment.start_date, experiment.end_date

    ExperimentService(db).unarchive_experiment(experiment)

    assert experiment.status == ExperimentStatus.COMPLETED
    assert (experiment.start_date, experiment.end_date) == (start, end)
    db.commit.assert_called_once()
