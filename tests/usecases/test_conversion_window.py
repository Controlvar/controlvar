"""A metric's conversion window over the wire: the results count a user's
events only within the window of their assignment, against a running
Controlvar (``_platform.yml``'s ``usecases-flags`` suite, nightly).

An experiment is made with two conversion metrics on ``purchase``: one with a
2-hour window, one with none. It is started and 12 users are assigned through
``POST /tracking/assign``; each then sends purchases through
``POST /tracking/track`` with a ``timestamp`` set from the moment the user was
assigned (the window is 2 hours, so every case is at least 30 minutes from an
edge):

* ``in``: one purchase 30 minutes after the assignment (4 users);
* ``late``: one purchase 90 minutes after, still inside (1 user);
* ``both``: one inside, one 3 hours after (1 user);
* ``now``: one purchase with no ``timestamp``, dated when it is received (1);
* ``after``: one purchase 3 hours after the assignment (2 users);
* ``before``: one purchase an hour before the assignment (1 user);
* ``twice``: one an hour before and one 5 hours after (1 user);
* ``none``: no purchase (1 user).

* **W1, the windowed metric** counts the 7 users with a purchase inside the
  window (``in``, ``late``, ``both``, ``now``), per variant, out of the 12
  assigned: ``conversions`` and ``sample_size`` as ``GET /results/{id}``
  answers them.
* **W2, the metric with no window** counts every user with a purchase, 11,
  and answers ``open_window_users`` ``null``.
* **W3, still inside their window.** The users were assigned moments ago, so
  every one of them had less than the 2 hours: the windowed metric's
  ``open_window_users`` is each variant's ``sample_size``.
* **W4, the window as stored.** The results read the metrics back with
  ``conversion_window_hours`` 2 and ``null``.

Wall clock is never gated: the offsets are hours, and the case reads the
clock only to date the events. The signed-in run is ``conftest.py``'s.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from conftest import Live, _fail_on

WINDOW_HOURS = 2
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)

#: (name, offsets from the assignment of the purchases, None: no timestamp,
#: how many users, how many of them convert inside the window)
PLAN: tuple[tuple[str, tuple[timedelta | None, ...], int, bool], ...] = (
    ("in", (30 * MINUTE,), 4, True),
    ("late", (90 * MINUTE,), 1, True),
    ("both", (30 * MINUTE, 3 * HOUR), 1, True),
    ("now", (None,), 1, True),
    ("after", (3 * HOUR,), 2, False),
    ("before", (-HOUR,), 1, False),
    ("twice", (-HOUR, 5 * HOUR), 1, False),
    ("none", (), 1, False),
)
USERS = sum(count for _, _, count, _ in PLAN)
INSIDE = sum(count for _, _, count, inside in PLAN if inside)
EVER = sum(count for _, offsets, count, _ in PLAN if offsets)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _metric(results: dict[str, Any], name: str) -> dict[str, Any] | None:
    found = [m for m in results.get("metrics", []) if m.get("metric_name") == name]
    return found[0] if len(found) == 1 else None


def test_a_metric_counts_only_the_events_inside_its_window(live: Live) -> None:
    api = live.admin
    key = f"window-{live.run}"
    experiment = api.create_experiment(
        key,
        [
            {
                "name": "In two hours",
                "event_name": "purchase",
                "metric_type": "conversion",
                "is_primary": True,
                "conversion_window_hours": WINDOW_HOURS,
            },
            {
                "name": "Ever",
                "event_name": "purchase",
                "metric_type": "conversion",
            },
        ],
    )
    experiment_id = str(experiment["id"])
    api.start_experiment(experiment_id)

    assigned: dict[str, int] = {}
    converting: dict[str, int] = {}
    ever: dict[str, int] = {}
    number = 0
    for name, offsets, count, inside in PLAN:
        for _ in range(count):
            number += 1
            user = f"{key}-{name}{number}"
            variant = str(api.assign(key, user, {})["variant_name"])
            at = datetime.now(timezone.utc)
            for offset in offsets:
                api.track(
                    key,
                    user,
                    "purchase",
                    None if offset is None else _stamp(at + offset),
                )
            assigned[variant] = assigned.get(variant, 0) + 1
            converting[variant] = converting.get(variant, 0) + inside
            ever[variant] = ever.get(variant, 0) + bool(offsets)
    for variant in ("control", "treatment"):
        assigned.setdefault(variant, 0)
        converting.setdefault(variant, 0)
        ever.setdefault(variant, 0)

    results = api.results(experiment_id)
    findings: list[str] = []
    windowed = _metric(results, "In two hours")
    unwindowed = _metric(results, "Ever")
    for label, metric, counts, open_users in (
        ("In two hours", windowed, converting, assigned),
        ("Ever", unwindowed, ever, None),
    ):
        if metric is None:
            findings.append(f"{label}: the results carry no one metric of that name")
            continue
        hours = WINDOW_HOURS if open_users is not None else None
        if metric.get("conversion_window_hours") != hours:
            findings.append(
                f"{label}: conversion_window_hours is"
                f" {metric.get('conversion_window_hours')!r}, expected {hours!r}"
            )
        by_variant = {v.get("variant_name"): v for v in metric.get("variants", [])}
        for variant, users in assigned.items():
            row = by_variant.get(variant, {})
            want = {
                "sample_size": users,
                "conversions": counts[variant],
                "open_window_users": None if open_users is None else users,
            }
            findings += [
                f"{label} / {variant}: {field} is {row.get(field)!r}, expected"
                f" {value!r}"
                for field, value in want.items()
                if row.get(field) != value
            ]
    live.summary.append(
        f"Conversion window: {USERS} users, {INSIDE} inside the window, {EVER}"
        f" with a purchase, {len(findings)} findings"
    )
    _fail_on(findings, "the conversion window use case")
