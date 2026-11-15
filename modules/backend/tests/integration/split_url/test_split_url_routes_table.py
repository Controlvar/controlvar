"""``GET /api/v1/tracking/split-url/routes`` (#393), and no redirect loop.

The table lists every ACTIVE split URL experiment with a key whose stored
configuration passes the rules, as exactly ``experiment_key``,
``experiment_id``, ``host`` and ``path``, ordered by host then path. Taken in
``start_date`` order (ties by id), one whose entry a listed one has, or that
would chain with a listed one, is left out with one warning: the same
predicate ``/start`` refuses with. The other split URL experiments of the
shared database are paused for each test (``conftest.py``).
"""

from __future__ import annotations

import logging
from collections import Counter

import pytest
from sqlalchemy import update

from backend.app.models.experiment import Experiment, ExperimentStatus
from modules.backend.app.services.split_url_service import (
    ROUTE_OMITTED_CONFLICT,
    ROUTE_OMITTED_NOT_VALID,
)
from modules.backend.tests.integration.split_url.controlvar_split_url import SplitUrl
from modules.backend.tests.integration.split_url.transport import client_transport

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

ROUTES = "/api/v1/tracking/split-url/routes"


def pages(control: str, treatment: str):
    return [("Control", True, 50, control), ("Treatment", False, 50, treatment)]


def table(client):
    response = client.get(ROUTES)
    assert response.status_code == 200, response.text
    return response


def test_an_empty_table(admin_client, split_experiment):
    split_experiment("split-paused", status=ExperimentStatus.PAUSED)
    response = table(admin_client)
    assert response.json() == {"routes": [], "max_age": 60}
    assert response.headers["cache-control"] == "private, max-age=60"


def test_the_entries_are_exact_and_ordered(admin_client, split_experiment):
    """V33: ACTIVE and keyed only; exactly four fields; the host in lower
    case and the path without its trailing slash; ordered by host, then
    path."""
    b = split_experiment(
        "split-b", pages("https://B.example/z/", "https://b.example/z2")
    )
    a2 = split_experiment(
        "split-a2", pages("https://a.example/y", "https://a.example/y2")
    )
    a1 = split_experiment(
        "split-a1", pages("https://a.example/x", "https://a.example/x2")
    )
    split_experiment(
        "split-draft",
        pages("https://c.example/1", "https://c.example/2"),
        status=ExperimentStatus.DRAFT,
    )
    split_experiment(
        "split-done",
        pages("https://c.example/3", "https://c.example/4"),
        status=ExperimentStatus.COMPLETED,
    )

    response = table(admin_client)
    assert response.headers["cache-control"] == "private, max-age=60"
    body = response.json()
    assert body["max_age"] == 60
    assert body["routes"] == [
        {
            "experiment_key": "split-a1",
            "experiment_id": str(a1.id),
            "host": "a.example",
            "path": "/x",
        },
        {
            "experiment_key": "split-a2",
            "experiment_id": str(a2.id),
            "host": "a.example",
            "path": "/y",
        },
        {
            "experiment_key": "split-b",
            "experiment_id": str(b.id),
            "host": "b.example",
            "path": "/z",
        },
    ]


def test_an_experiment_without_a_key_is_not_listed(
    admin_client, split_experiment, db_session
):
    experiment = split_experiment("split-keyless")
    db_session.execute(
        update(Experiment).where(Experiment.id == experiment.id).values(key=None)
    )
    db_session.commit()
    assert table(admin_client).json()["routes"] == []


def test_a_stored_configuration_that_breaks_the_rules_is_left_out(
    admin_client, split_experiment, db_session, caplog
):
    """V33: a legacy row is skipped with one warning; the others stay."""
    good = split_experiment(
        "split-good", pages("https://g.example/a", "https://g.example/b")
    )
    bad = split_experiment(
        "split-legacy", pages("https://l.example/a", "https://l.example/b")
    )
    db_session.execute(
        update(Experiment)
        .where(Experiment.id == bad.id)
        .values(
            split_url_config={
                "variants": [
                    {"name": "Control", "url": "nope", "traffic_allocation": 100}
                ]
            }
        )
    )
    db_session.commit()
    with caplog.at_level(logging.WARNING):
        routes = table(admin_client).json()["routes"]
    assert [r["experiment_id"] for r in routes] == [str(good.id)]
    warnings = [r for r in caplog.records if r.msg == ROUTE_OMITTED_NOT_VALID]
    assert [w.args for w in warnings] == [(bad.id,)]


@pytest.mark.parametrize(
    "first, second",
    [
        (
            pages("https://e.com/a", "https://e.com/b"),
            pages("https://e.com/a/", "https://e.com/c"),
        ),
        (
            pages("https://e.com/a", "https://e.com/b"),
            pages("https://e.com/b", "https://e.com/c"),
        ),
        (
            pages("https://e.com/b", "https://e.com/c"),
            pages("https://e.com/a", "https://e.com/b"),
        ),
    ],
    ids=["same-entry", "chain-into-its-destination", "chain-from-its-entry"],
)
def test_the_later_started_of_two_that_clash_is_left_out(
    admin_client, split_experiment, caplog, first, second
):
    """V33: greedy, in start_date order: the earlier is listed."""
    earlier = split_experiment("split-earlier", first, started_days_ago=2)
    later = split_experiment("split-later", second, started_days_ago=1)
    with caplog.at_level(logging.WARNING):
        routes = table(admin_client).json()["routes"]
    assert [r["experiment_id"] for r in routes] == [str(earlier.id)]
    warnings = [r for r in caplog.records if r.msg == ROUTE_OMITTED_CONFLICT]
    assert [w.args for w in warnings] == [(later.id,)]


def test_a_start_date_tie_goes_to_the_lower_id(
    admin_client, split_experiment, db_session
):
    one = split_experiment("split-tie-1", pages("https://e.com/a", "https://e.com/b"))
    two = split_experiment("split-tie-2", pages("https://e.com/a", "https://e.com/c"))
    db_session.execute(
        update(Experiment)
        .where(Experiment.id.in_([one.id, two.id]))
        .values(start_date=one.start_date)
    )
    db_session.commit()
    routes = table(admin_client).json()["routes"]
    assert [r["experiment_id"] for r in routes] == [str(min(one.id, two.id))]


def test_the_same_path_on_two_hosts_is_two_entries(admin_client, split_experiment):
    """V33: hosts are compared, so two sites' experiments on one path are both
    listed."""
    split_experiment(
        "split-site-1", pages("https://one.example/checkout", "https://one.example/v2")
    )
    split_experiment(
        "split-site-2", pages("https://two.example/checkout", "https://two.example/v2")
    )
    routes = table(admin_client).json()["routes"]
    assert [(r["host"], r["path"]) for r in routes] == [
        ("one.example", "/checkout"),
        ("two.example", "/checkout"),
    ]


def follow(split_url: SplitUrl, uid: str, url: str, cap: int = 10) -> int:
    """Follow a visitor's redirects through the reference integration, as a
    browser would, with the cookie it was given; the number of hops."""
    cookie = ""
    for hop in range(cap):
        host, _, path = url.split("://", 1)[1].partition("/")
        path, _, query = ("/" + path).partition("?")
        answer = split_url.answer(
            "GET", host, path, query, cookie or f"controlvar_uid={uid}"
        )
        if answer.status != 302:
            return hop
        url = dict(answer.headers)["Location"]
    return cap


@pytest.mark.regression
def test_no_visitor_is_sent_round_in_a_circle(admin_client, split_experiment):
    """V10: X sends /a to /b and Y sends /b to /a, both ACTIVE (a scheduled
    start, or two starts at once). The table lists only X, so every visitor
    takes at most one hop: a treatment visitor one, a control visitor none,
    and a visitor asking for a treatment URL is not intercepted."""
    split_experiment(
        "split-x", pages("https://e.com/a", "https://e.com/b"), started_days_ago=2
    )
    split_experiment(
        "split-y", pages("https://e.com/b", "https://e.com/a"), started_days_ago=1
    )
    calls = []
    split_url = SplitUrl(
        "http://api", "key", http=client_transport(admin_client, calls)
    )
    hops = Counter(
        follow(split_url, f"loop-{i}", "https://e.com/a") for i in range(200)
    )
    assert set(hops) <= {0, 1}
    assert hops[1] > 0 and hops[0] > 0

    calls.clear()
    assert follow(split_url, "loop-direct", "https://e.com/b") == 0
    assert [c for c in calls if c[1].endswith("/assign")] == []
