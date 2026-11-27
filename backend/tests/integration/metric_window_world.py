"""The rows a metric's conversion window is tested on (#1934).

Twenty users of one experiment (``TWENTY_ROWS``), each placed against the rule
``floor_to_second(a) <= t < a + 24 h``, and four more (``ADDED_ROWS``) that the
floor and the end tell apart: ``a`` is the user's ``assignments.created_at``,
``t`` the event's own ``created_at``. ``S`` is 2026-11-01 05:00 UTC; Los
Angeles leaves summer time four hours later, so a window taken as "one day"
in that zone is 25 hours long.

With the 24-hour window 9 of ``TWENTY_ROWS`` convert, each with one event inside
it, and 11 of all the rows; with no window 16 and 20. Two variants: users with
an odd number are in control, the others in treatment, and u20 is not
assigned. Every assigned user has an exposure at ``a`` carrying a ``country``
(``country_of``: both countries hold users of both variants), as
``/tracking/assign`` stores one, so the breakdowns have something to group by;
with ``balanced`` the experiment balances its split within the country, and
each assignment stores it as its group.

The rows are written through the ORM, which stores an event's time as UTC text
(``normalize_event_timestamp``), except u24's, which is written with raw SQL
as a legacy value with an offset was stored before that.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app.core.database_config import get_schema_name
from backend.app.models.analysis_snapshot import AnalysisSnapshot
from backend.app.models.assignment import Assignment
from backend.app.models.bandit_state import BanditState
from backend.app.models.event import Event
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    ExperimentType,
    Metric,
    MetricType,
    Variant,
)

#: The start of the fixture: naive UTC, as ``assignments.created_at`` is stored.
S = datetime(2026, 11, 1, 5, 0, 0)
WINDOW_HOURS = 24
HOUR = timedelta(hours=1)
SECOND = timedelta(seconds=1)
MICROSECOND = timedelta(microseconds=1)
DAY = timedelta(hours=WINDOW_HOURS)


@dataclass(frozen=True)
class Sent:
    """One event of a row: when it happened (an offset from ``S``, or the
    stored text itself), and how it differs from a plain purchase."""

    at: Union[timedelta, str]
    tag: Optional[str] = None  # the variant it is tagged with; None: the user's own
    event_type: str = "purchase"
    event_name: str = "purchase"
    value: float = 10.0
    received: Optional[timedelta] = None  # updated_at; None: when it happened
    raw: bool = False  # stored with raw SQL, as written, not normalised


@dataclass(frozen=True)
class Row:
    label: str
    variant: Optional[str]  # "control", "treatment", or None: not assigned
    sent: Tuple[Sent, ...]
    counts: bool  # inside the 24-hour window under the floored rule
    why: str
    assigned: timedelta = timedelta(0)
    exposures: Tuple[timedelta, ...] = ()  # more exposures after the first


def country_of(label: str) -> str:
    return "US" if (int(label[1:]) // 2) % 2 == 0 else "DE"


def _arm(number: int) -> str:
    return "control" if number % 2 else "treatment"


def _row(number, sent, counts, why, **fields) -> Row:
    variant = fields.pop("variant", _arm(number))
    return Row(f"u{number:02d}", variant, tuple(sent), counts, why, **fields)


TWENTY_ROWS: Tuple[Row, ...] = (
    _row(1, [Sent(timedelta(0))], True, "exactly at the start"),
    _row(2, [Sent(DAY)], False, "exactly at the end"),
    _row(3, [Sent(DAY - MICROSECOND)], True, "1 us before the end"),
    _row(4, [Sent(-MICROSECOND)], False, "1 us before the start"),
    _row(5, [Sent(SECOND / 2)], True, "half a second after the start"),
    _row(
        6,
        [Sent(SECOND)],
        True,
        "assigned at .5 s, the event on the next whole second",
        assigned=SECOND / 2,
    ),
    _row(
        7,
        [Sent(SECOND / 5)],
        True,
        "the same second as the assignment, but earlier (the floor counts it)",
        assigned=SECOND * 7 / 10,
    ),
    _row(
        8,
        [Sent(-HOUR, value=100.0), Sent(2 * HOUR)],
        True,
        "one event before the start and one inside",
    ),
    _row(
        9,
        [Sent(3 * HOUR), Sent(30 * HOUR, value=1000.0)],
        True,
        "one event inside and one after the end",
    ),
    _row(10, [Sent(30 * HOUR)], False, "only after the end"),
    _row(11, [Sent(DAY + 30 * timedelta(minutes=1))], False, "24 h 30 min"),
    _row(
        12,
        [Sent(5 * HOUR, received=90 * HOUR)],
        True,
        "inside, but stored 90 h later",
    ),
    _row(
        13,
        [Sent(26 * HOUR, received=HOUR)],
        False,
        "the client's clock ahead: stored inside, dated after the end",
    ),
    _row(
        14,
        [Sent(30 * HOUR)],
        False,
        "a second exposure 20 h after the first, the event 30 h after the first",
        exposures=(20 * HOUR,),
    ),
    _row(
        15,
        [Sent("2026-11-01T21:00:00-08:00")],
        False,
        "an offset timestamp equal to the end",
    ),
    _row(16, [Sent(23 * HOUR)], True, "after the experiment's end date, but inside"),
    _row(
        17,
        [Sent(HOUR, event_type="conversion", event_name="signup")],
        False,
        "no purchase; a legacy 'conversion' event under another name",
    ),
    _row(18, [Sent(HOUR, tag="control")], False, "tagged with the other variant"),
    _row(
        19,
        [Sent(HOUR, event_type="exposure")],
        False,
        "an exposure named like the metric",
    ),
    _row(
        20,
        [Sent(HOUR, tag="control")],
        False,
        "not assigned",
        variant=None,
    ),
)

ADDED_ROWS: Tuple[Row, ...] = (
    _row(
        21,
        [Sent(37 * SECOND)],
        True,
        "assigned at 37.412 s, the event at 37 s",
        assigned=37412 * MICROSECOND * 1000,
    ),
    _row(
        22,
        [Sent(369 * SECOND / 10)],
        False,
        "assigned at 37.412 s, the event at 36.9 s",
        assigned=37412 * MICROSECOND * 1000,
    ),
    _row(
        23,
        [Sent(DAY + SECOND / 5)],
        True,
        "assigned at .5 s, the event at 24 h .2 s",
        assigned=SECOND / 2,
    ),
    _row(
        24,
        [Sent("2026-11-01T22:00:00-08:00", raw=True)],
        False,
        "a legacy offset text (25 h after the start) that sorts inside as text",
    ),
)

ALL_ROWS: Tuple[Row, ...] = TWENTY_ROWS + ADDED_ROWS
#: The experiment's end date in the warehouse parity: after every converting
#: user's inside event but u03's and u16's.
END_DATE = S + 12 * HOUR


def converters(rows: Iterable[Row], windowed: bool = True) -> List[str]:
    """The labels that convert: inside the window, or with no window any
    assigned user's purchase tagged with their own variant."""
    if windowed:
        return [row.label for row in rows if row.counts]
    return [
        row.label
        for row in rows
        if row.variant is not None
        and any(
            sent.event_name == "purchase"
            and sent.event_type == "purchase"
            and sent.tag in (None, row.variant)
            for sent in row.sent
        )
    ]


def per_variant(
    rows: Iterable[Row], windowed: bool = True
) -> Dict[str, Dict[str, int]]:
    """``{variant: {"n": assigned, "converted": converting}}``."""
    rows = list(rows)
    converted = set(converters(rows, windowed))
    out = {"control": {"n": 0, "converted": 0}, "treatment": {"n": 0, "converted": 0}}
    for row in rows:
        if row.variant is None:
            continue
        out[row.variant]["n"] += 1
        out[row.variant]["converted"] += row.label in converted
    return out


@dataclass
class World:
    """One experiment holding the rows; ``user`` maps a label to its user id."""

    experiment: Experiment
    control: Variant
    treatment: Variant
    user: Dict[str, str] = field(default_factory=dict)

    def variant(self, name: str) -> Variant:
        return self.control if name == "control" else self.treatment

    def labels(self, user_ids: Iterable[str]) -> List[str]:
        back = {v: k for k, v in self.user.items()}
        return sorted(back[str(u)] for u in user_ids)


def metric(
    name: str,
    hours: Optional[int] = WINDOW_HOURS,
    metric_type: MetricType = MetricType.CONVERSION,
    event_name: str = "purchase",
    **fields,
) -> Dict:
    """The fields of one ``Metric`` row for :func:`build`."""
    return {
        "name": name,
        "event_name": event_name,
        "metric_type": metric_type,
        "conversion_window_hours": hours,
        **fields,
    }


#: The default: one conversion metric on purchases, 24 hours, primary.
IN_A_DAY = metric("Purchase in a day", is_primary=True)

#: A revenue metric on the same purchases and window, primary.  A view that
#: reads a metric's share of users counts the same users as for ``IN_A_DAY``;
#: one that reads its values per user answers ``REVENUE_MEANS``.
REVENUE_IN_A_DAY = metric(
    "Revenue in a day", metric_type=MetricType.REVENUE, is_primary=True
)

#: The revenue per assigned user inside the window: 70 over the control's 12
#: users and 40 over the treatment's 11.  u08's 100 before the window starts
#: and u09's 1,000 after it ends are not added, and every assigned user is in
#: the denominator.
REVENUE_MEANS = {"control": 70 / 12, "treatment": 40 / 11}


def build(
    db: Session,
    owner_id,
    rows: Sequence[Row] = ALL_ROWS,
    metrics: Sequence[Dict] = (IN_A_DAY,),
    balanced: bool = False,
    users: Optional[str] = None,
    **experiment_fields,
) -> World:
    """Write the experiment, its variants and metrics, and the rows. Commits.

    ``users`` names the user ids (``mw-<users>-u01`` ...), so two experiments
    can hold the same users; by default they are new.
    """
    suffix = uuid.uuid4().hex[:8]
    users = users or suffix
    fields = {
        "name": f"Conversion window {suffix}",
        "key": f"conversion-window-{suffix}",
        "status": ExperimentStatus.ACTIVE,
        "experiment_type": ExperimentType.A_B,
        "owner_id": owner_id,
        "start_date": S - DAY,
        "sequential_testing_enabled": True,
        "sequential_testing_config": {"method": "msprt"},
        "bayesian_enabled": True,
        "bayesian_config": {"prior_family": "beta", "alpha": 1.0, "beta": 1.0},
        **experiment_fields,
    }
    if balanced:
        fields["balance_within"] = {"attribute": "country", "values": ["US", "DE"]}
    experiment = Experiment(**fields)
    db.add(experiment)
    db.flush()
    control = Variant(
        experiment_id=experiment.id,
        name="control",
        is_control=True,
        traffic_allocation=50,
    )
    treatment = Variant(
        experiment_id=experiment.id,
        name="treatment",
        is_control=False,
        traffic_allocation=50,
    )
    db.add_all([control, treatment])
    for fields_of_metric in metrics:
        db.add(Metric(experiment_id=experiment.id, **fields_of_metric))
    db.flush()
    world = World(experiment, control, treatment)
    return add_rows(db, world, rows, balanced=balanced, users=users)


def add_rows(
    db: Session,
    world: World,
    rows: Sequence[Row] = ALL_ROWS,
    balanced: bool = False,
    users: Optional[str] = None,
) -> World:
    """Write the rows into ``world``'s experiment, whose two variants are its
    ``control`` and ``treatment``: an experiment :func:`build` made, or one
    made through the API. Commits. ``users`` as for :func:`build`."""
    experiment = world.experiment
    users = users or uuid.uuid4().hex[:8]
    raw: List[Dict] = []
    for row in rows:
        user_id = f"mw-{users}-{row.label}"
        world.user[row.label] = user_id
        country = country_of(row.label)
        assigned = S + row.assigned
        if row.variant is not None:
            variant = world.variant(row.variant)
            db.add(
                Assignment(
                    experiment_id=experiment.id,
                    variant_id=variant.id,
                    user_id=user_id,
                    balance_group=country if balanced else None,
                    created_at=assigned,
                    updated_at=assigned,
                )
            )
            for at in (timedelta(0),) + row.exposures:
                db.add(
                    Event(
                        event_type="exposure",
                        event_name="variant_exposure",
                        user_id=user_id,
                        experiment_id=experiment.id,
                        variant_id=variant.id,
                        event_metadata={"country": country},
                        created_at=assigned + at,
                        updated_at=assigned + at,
                    )
                )
        for sent in row.sent:
            tag = sent.tag or row.variant
            variant_id = world.variant(tag).id if tag else None
            if isinstance(sent.at, str):
                happened = sent.at
                received = S + (sent.received or timedelta(0))
            else:
                happened = S + sent.at
                received = S + (sent.received if sent.received is not None else sent.at)
            if sent.raw:
                raw.append(
                    {
                        "id": uuid.uuid4(),
                        "event_type": sent.event_type,
                        "event_name": sent.event_name,
                        "user_id": user_id,
                        "experiment_id": experiment.id,
                        "variant_id": variant_id,
                        "value": sent.value,
                        "created_at": happened,
                        "updated_at": received,
                    }
                )
                continue
            db.add(
                Event(
                    event_type=sent.event_type,
                    event_name=sent.event_name,
                    user_id=user_id,
                    experiment_id=experiment.id,
                    variant_id=variant_id,
                    value=sent.value,
                    event_metadata={"country": country},
                    created_at=happened,
                    updated_at=received,
                )
            )
    db.flush()
    for values in raw:
        # As written, past the type that would store it as UTC text.
        db.execute(
            text(
                f"INSERT INTO {get_schema_name()}.events (id, event_type, "
                "event_name, user_id, experiment_id, variant_id, value, "
                "event_metadata, created_at, updated_at) VALUES (:id, "
                ":event_type, :event_name, :user_id, :experiment_id, "
                ":variant_id, :value, NULL, :created_at, :updated_at)"
            ),
            values,
        )
    db.commit()
    db.refresh(experiment)
    return world


def remove(db: Session, experiment_ids: Iterable) -> None:
    """Delete what :func:`build` wrote for these experiments."""
    ids = list(experiment_ids)
    db.rollback()
    for model in (AnalysisSnapshot, BanditState, Event, Assignment, Metric, Variant):
        db.query(model).filter(model.experiment_id.in_(ids)).delete(
            synchronize_session=False
        )
    db.query(Experiment).filter(Experiment.id.in_(ids)).delete(
        synchronize_session=False
    )
    db.commit()
