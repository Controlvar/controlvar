"""The CloudFront edge router splits through the API (#393).

The router (``modules/lambda/split_url_router/handler.py``) is driven by the
simulated CloudFront of ``modules/backend/tests/unit/split_url_router/
harness.py``, with events in the shape AWS documents, and its transport
reaching the in-process API. For 1000 visitors without a cookie, each with
its own ``config.requestId`` and ``clientIp``, one User-Agent and no
``X-Forwarded-For``:

* each is sent to the variant the API stored for them (1000 of 1000), and a
  fresh ``/tracking/assign`` on the same ids, after the rows are wiped,
  answers the same (a comparison on ids already assigned would agree under
  any defect);
* the split is exact, from the Locations and from the rows;
* the cookie each one gets holds the id sent to the API, and a return visit
  with it gets the same variant, no new cookie and no new row;
* every location vector the API test and the reference integration share
  is the Location the router answers;
* a treatment URL is not intercepted, so a visitor takes at most one hop.
"""

from __future__ import annotations

import base64
import hashlib
from collections import Counter
from typing import Dict, List, Optional
from urllib.parse import urlsplit

import pytest
from sqlalchemy.orm import Session

from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from modules.backend.tests.integration.split_url.conftest import (
    CONTROL_URL,
    TREATMENT_URL,
)
from modules.backend.tests.integration.split_url.transport import client_transport
from modules.backend.tests.unit.split_url_router.harness import (
    VECTORS,
    Clock,
    cookie_id,
    deliver,
    router_module,
    viewer_request,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

KEY = "split-router-e2e"
VISITORS = 1000
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64)"


def request_id(i: int) -> str:
    """A request id shaped like CloudFront's (56 base64 characters)."""
    digest = hashlib.sha512(f"split-router-request-{i}".encode()).digest()[:40]
    return base64.urlsafe_b64encode(digest).decode()


def router(admin_client, calls: Optional[List] = None):
    return router_module.Router(
        {"api_url": "http://api", "api_key": "key"},
        http=client_transport(admin_client, calls),
        clock=Clock(),
    )


def visit(edge, i: int, *, cookies=(), client_ip=None, **request):
    return deliver(
        edge,
        viewer_request(
            request_id=request_id(i),
            client_ip=client_ip or f"10.{i // 256}.{i % 256}.7",
            user_agent=USER_AGENT,
            cookies=cookies,
            **request,
        ),
    )


def variant_of(delivery, urls: Dict[str, str]) -> str:
    """The variant a visitor was sent to: a Location names its URL; a
    visitor served the page is in the control."""
    if delivery.generated is not None:
        by_url = {url: name for name, url in urls.items()}
        return by_url[delivery.header("Location").split("?")[0]]
    assert delivery.header("X-Split-URL") == "served"
    return "Control"


def rows(db: Session, experiment) -> Dict[str, str]:
    db.expire_all()
    names = {v.id: v.name for v in experiment.variants}
    return {
        a.user_id: names[a.variant_id]
        for a in db.query(Assignment).filter(Assignment.experiment_id == experiment.id)
    }


URLS = {"Control": CONTROL_URL, "Treatment": TREATMENT_URL}


@pytest.mark.regression
def test_a_split_url_experiment_splits_its_visitors_through_the_router(
    admin_client, split_experiment, db_session
):
    """R1 (router half) / V13 / V1 / V2 / V4(b)."""
    experiment = split_experiment(KEY)
    calls: List = []
    edge = router(admin_client, calls)
    deliveries = [visit(edge, i) for i in range(VISITORS)]
    sent = [c for c in calls if c[1].endswith("/assign")]
    assert len(sent) == VISITORS

    ids = [cookie_id(d) for d in deliveries]
    assert ids == [router_module.visitor_id(request_id(i)) for i in range(VISITORS)]
    assert len(set(ids)) == VISITORS
    sent_to = {uid: variant_of(d, URLS) for uid, d in zip(ids, deliveries)}
    # bucket_of(visitor_id(request_id(i)), KEY) against 50/50, for i < 1000.
    assert Counter(sent_to.values()) == {"Control": 529, "Treatment": 471}
    stored = rows(db_session, experiment)
    assert len(stored) == VISITORS
    assert sum(stored.get(uid) == sent_to[uid] for uid in ids) == VISITORS
    for d in deliveries:
        for value in d.headers("Set-Cookie"):
            assert "://" not in value and "Treatment" not in value, value
    assert {d.header("Location") for d in deliveries if d.generated} == {
        f"{TREATMENT_URL}?utm_source=x"
    }

    # Return visits, with the cookie: the same variant, no cookie, no row.
    again = [
        visit(edge, VISITORS + i, cookies=[f"controlvar_uid={uid}"])
        for i, uid in enumerate(ids)
    ]
    assert [variant_of(d, URLS) for d in again] == [sent_to[uid] for uid in ids]
    assert [d.headers("Set-Cookie") for d in again] == [[]] * VISITORS
    assert len(rows(db_session, experiment)) == VISITORS

    # A fresh /tracking/assign on the same ids gives the same answers.
    for model in (Event, Assignment):
        db_session.query(model).filter(model.experiment_id == experiment.id).delete(
            synchronize_session=False
        )
    db_session.commit()
    fresh = {}
    for uid in ids:
        response = admin_client.post(
            "/api/v1/tracking/assign", json={"experiment_key": KEY, "user_id": uid}
        )
        assert response.status_code == 200, response.text
        fresh[uid] = response.json()["variant_name"]
    assert sum(fresh[uid] == sent_to[uid] for uid in ids) == VISITORS


@pytest.mark.regression
def test_the_router_does_not_bucket_on_x_forwarded_for(admin_client, split_experiment):
    """R3 / V14: 1000 visitors behind one address with one User-Agent and no
    X-Forwarded-For are 1000 visitors, in both variants."""
    split_experiment(KEY)
    calls: List = []
    edge = router(admin_client, calls)
    deliveries = [visit(edge, i, client_ip="203.0.113.178") for i in range(VISITORS)]
    ids = {cookie_id(d) for d in deliveries}
    assert len(ids) == VISITORS
    assert ids == {router_module.visitor_id(request_id(i)) for i in range(VISITORS)}
    assert len([c for c in calls if c[1].endswith("/assign")]) == VISITORS
    assert set(Counter(variant_of(d, URLS) for d in deliveries)) == {
        "Control",
        "Treatment",
    }


@pytest.mark.parametrize("vector", VECTORS["location"], ids=lambda v: v["id"])
def test_the_location_is_the_api_s(admin_client, split_experiment, vector):
    """V9 through the router: exactly the vector's Location, and no assign
    call off the entry."""
    split_experiment(
        "split-router-vectors",
        [
            ("Control", True, 50, vector["control"]),
            ("Treatment", False, 50, vector["variant"]),
        ],
        optimization_type="thompson_sampling",
        bandit_weights={"Control": 0.0, "Treatment": 1.0},
    )
    calls: List = []
    delivery = deliver(
        router(admin_client, calls),
        viewer_request(
            host=vector["host"], uri=vector["path"], querystring=vector["query"]
        ),
    )
    assigned = [c for c in calls if c[1].endswith("/assign")]
    if vector["expect"] == "not_entry":
        assert (delivery.generated, assigned) == (None, []), vector["id"]
        assert delivery.header("X-Split-URL") is None
    else:
        assert delivery.header("Location") == vector["expect"], vector["id"]
        assert delivery.status == "302"


def test_a_visitor_takes_at_most_one_hop(admin_client, split_experiment):
    """V10 through the router: a treatment visitor follows one redirect, to a
    page the router does not intercept; a control visitor follows none."""
    split_experiment(KEY)
    calls: List = []
    edge = router(admin_client, calls)
    hops = Counter()
    for i in range(50):
        delivery, cookies, taken = visit(edge, i), [], 0
        while delivery.generated is not None:
            taken += 1
            assert taken <= 1
            cookies = [v.split(";", 1)[0] for v in delivery.headers("Set-Cookie")]
            target = urlsplit(delivery.header("Location"))
            delivery = deliver(
                edge,
                viewer_request(
                    host=target.hostname,
                    uri=target.path,
                    querystring=target.query,
                    cookies=cookies,
                    request_id=f"hop-{i}",
                ),
            )
        hops[taken] += 1
    assert set(hops) == {0, 1}
    assert len([c for c in calls if c[1].endswith("/assign")]) == 50


#: A visitor's host, query or User-Agent beyond what the API accepts, and the
#: Location they are sent to: the entry's host is sent, and a query string or
#: User-Agent the API does not accept is left out.
BEYOND_THE_LIMITS = {
    "host-with-a-space": ({"host": "e.com:1 2"}, f"{TREATMENT_URL}?q=1"),
    "host-too-long": ({"host": "e.com:" + "1" * 300}, f"{TREATMENT_URL}?q=1"),
    "query-with-a-hash": ({"querystring": "a=1#x"}, TREATMENT_URL),
    "query-too-long": ({"querystring": "a=" + "x" * 9000}, TREATMENT_URL),
    "query-with-a-space": ({"querystring": "a=1 2"}, TREATMENT_URL),
    "query-with-a-control": ({"querystring": "a=1\x01"}, TREATMENT_URL),
    "user-agent-with-nul": ({"user_agent": "Mozilla/5.0\x00"}, f"{TREATMENT_URL}?q=1"),
}


@pytest.mark.regression
@pytest.mark.parametrize("kind", sorted(BEYOND_THE_LIMITS))
def test_only_what_the_api_accepts_is_sent(admin_client, split_experiment, kind):
    """Against the in-process API: a treatment visitor whose host, query or
    User-Agent the API would not accept is still sent to the treatment, and
    the next visitor is still asked about."""
    split_experiment(
        "split-router-accepted",
        optimization_type="thompson_sampling",
        bandit_weights={"Control": 0.0, "Treatment": 1.0},
    )
    beyond, expected = BEYOND_THE_LIMITS[kind]
    calls: List = []
    edge = router(admin_client, calls)
    first = deliver(
        edge, viewer_request(**{"querystring": "q=1", **beyond}, request_id="r-1")
    )
    following = deliver(edge, viewer_request(querystring="q=1", request_id="r-2"))
    assigned = [c for c in calls if c[1].endswith("/assign")]
    assert (first.status, first.header("Location")) == ("302", expected), kind
    assert (following.status, len(assigned)) == ("302", 2), kind
