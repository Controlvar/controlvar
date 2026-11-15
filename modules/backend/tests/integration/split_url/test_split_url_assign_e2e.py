"""A split URL experiment splits its visitors, and counts them where they went (#393).

``POST /api/v1/tracking/split-url/assign`` answers through
``/tracking/assign``'s own function. These tests hold, for 1000 visitor ids
(``split-visitor-0..999``) on an experiment keyed ``split-e2e``:

* what a visitor is sent to is what is stored and counted, compared with a
  fresh ``/tracking/assign`` on the same ids after the rows are wiped (a
  comparison on ids already assigned would agree under any defect, because
  ``/tracking/assign`` answers a stored row);
* the split follows the weights exactly, from the Locations and the rows;
* conversions count for the variant seen, both ways the docs give;
* the same rules as ``/tracking/assign`` (bandit, targeting, holdout, status),
  each on fresh ids, and the preview agrees with the route and records
  nothing;
* bots and exposures.

The exact counts depend only on the ids and the key: the variant is
``bucket_of(user_id, "split-e2e")`` against the allocation.
"""

from __future__ import annotations

from collections import Counter
from typing import Dict, List

import pytest
from sqlalchemy.orm import Session

from backend.app.models.assignment import Assignment
from backend.app.models.event import Event, EventType
from backend.app.models.experiment import ExperimentStatus, ExperimentType, MetricType
from backend.app.services.global_holdout_service import GlobalHoldoutService
from modules.backend.app.services.split_url_service import BOT_SUBSTRINGS
from modules.backend.tests.integration.split_url.conftest import (
    CONTROL_URL,
    TREATMENT_URL,
)
from modules.backend.tests.integration.split_url.controlvar_split_url import SplitUrl
from modules.backend.tests.integration.split_url.transport import client_transport

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

ASSIGN = "/api/v1/tracking/split-url/assign"
KEY = "split-e2e"
IDS = [f"split-visitor-{i}" for i in range(1000)]


def visit(client, user_id: str, *, key: str = KEY, **request) -> Dict:
    body = {
        "experiment_key": key,
        "user_id": user_id,
        "host": "e.com",
        "path": "/checkout",
        "query": "utm_source=x",
        **request,
    }
    response = client.post(ASSIGN, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def rows(db: Session, experiment) -> Dict[str, str]:
    """user_id -> variant name of the experiment's stored assignments."""
    db.expire_all()
    names = {v.id: v.name for v in experiment.variants}
    return {
        a.user_id: names[a.variant_id]
        for a in db.query(Assignment).filter(Assignment.experiment_id == experiment.id)
    }


def exposures(db: Session, experiment) -> int:
    db.expire_all()
    return (
        db.query(Event)
        .filter(
            Event.experiment_id == experiment.id,
            Event.event_type == EventType.EXPOSURE.value,
        )
        .count()
    )


def wipe(db: Session, experiment) -> None:
    for model in (Event, Assignment):
        db.query(model).filter(model.experiment_id == experiment.id).delete(
            synchronize_session=False
        )
    db.commit()


def split_of(answers: Dict[str, Dict], urls: Dict[str, str]) -> Counter:
    """The split read from the answers alone: a Location names its variant's
    URL; a visitor served the page is counted as the control."""
    by_url = {url: name for name, url in urls.items()}
    split = Counter()
    for answer in answers.values():
        if answer["action"] == "redirect":
            split[by_url[answer["location"].split("?")[0]]] += 1
        else:
            split[answer["variant_name"]] += 1
    return split


def results(client, experiment) -> Dict[str, Dict]:
    response = client.get(f"/api/v1/results/{experiment.id}?use_cache=false")
    assert response.status_code == 200, response.text
    primary = next(m for m in response.json()["metrics"] if m["is_primary"])
    return {v["variant_name"]: v for v in primary["variants"]}


@pytest.mark.regression
def test_a_split_url_experiment_splits_its_visitors(admin_client, split_experiment):
    """R1 / V2, 50/50: every visitor is recorded, control and treatment alike,
    and the split is exact."""
    experiment = split_experiment(KEY)
    answers = {uid: visit(admin_client, uid) for uid in IDS}
    urls = {"Control": CONTROL_URL, "Treatment": TREATMENT_URL}

    assert split_of(answers, urls) == {"Control": 505, "Treatment": 495}
    redirects = [a for a in answers.values() if a["action"] == "redirect"]
    assert {a["location"] for a in redirects} == {f"{TREATMENT_URL}?utm_source=x"}


@pytest.mark.regression
def test_the_variant_seen_is_the_variant_counted(
    admin_client, split_experiment, db_session
):
    """R2 / V1 / V3(a): the variant each visitor is sent to is the stored row's
    for 1000 of 1000; a fresh /tracking/assign on the same ids, after the rows
    are wiped, gives the same 1000 answers; conversions sent with the
    experiment's key count for the variant seen."""
    experiment = split_experiment(KEY)
    answers = {uid: visit(admin_client, uid) for uid in IDS}
    stored = rows(db_session, experiment)
    sent_to = {uid: a["variant_name"] for uid, a in answers.items()}
    assert sum(stored.get(uid) == sent_to[uid] for uid in IDS) == 1000
    assert Counter(stored.values()) == {"Control": 505, "Treatment": 495}

    wipe(db_session, experiment)
    fresh = {}
    for uid in IDS:
        response = admin_client.post(
            "/api/v1/tracking/assign", json={"experiment_key": KEY, "user_id": uid}
        )
        assert response.status_code == 200, response.text
        fresh[uid] = response.json()["variant_name"]
    assert sum(fresh[uid] == sent_to[uid] for uid in IDS) == 1000

    for i, uid in enumerate(IDS):
        converts = i % 5 == 0 if sent_to[uid] == "Treatment" else i % 10 == 0
        if converts:
            response = admin_client.post(
                "/api/v1/tracking/track",
                json={"event_type": "purchase", "user_id": uid, "experiment_key": KEY},
            )
            assert response.status_code == 200, response.text
    counted = results(admin_client, experiment)
    assert {name: v["conversions"] for name, v in counted.items()} == {
        "Control": 57,
        "Treatment": 95,
    }


def test_conversions_through_the_per_site_pattern_count_for_the_variant_seen(
    admin_client, split_experiment
):
    """V3(b): the documented per-site pattern (the routes table's ids, the
    visitor's assignments filtered to them, then the event by ids) counts the
    same conversions as the key in the call."""
    experiment = split_experiment(KEY)
    split_url = SplitUrl("http://api", "key", http=client_transport(admin_client))
    sent_to = {uid: visit(admin_client, uid)["variant_name"] for uid in IDS}
    for i, uid in enumerate(IDS):
        converts = i % 5 == 0 if sent_to[uid] == "Treatment" else i % 10 == 0
        if converts:
            assert split_url.track(uid, "purchase") == 1
    counted = results(admin_client, experiment)
    assert {name: v["conversions"] for name, v in counted.items()} == {
        "Control": 57,
        "Treatment": 95,
    }


def test_the_per_site_pattern_reaches_only_split_url_experiments(
    admin_client, split_experiment
):
    """V3(b), the mixed case: a visitor in split-e2e and in an A/B experiment
    with a count metric on the same event. The purchase goes through the
    per-site pattern and, with the A/B experiment's key, through /track: it
    counts exactly once in each."""
    split = split_experiment(KEY)
    other = split_experiment(
        "split-e2e-other",
        experiment_type=ExperimentType.A_B,
        metric_type=MetricType.COUNT,
    )
    uid = "split-visitor-mixed"
    visit(admin_client, uid)
    response = admin_client.post(
        "/api/v1/tracking/assign",
        json={"experiment_key": "split-e2e-other", "user_id": uid},
    )
    assert response.status_code == 200, response.text

    split_url = SplitUrl("http://api", "key", http=client_transport(admin_client))
    sent = split_url.track(uid, "purchase")
    response = admin_client.post(
        "/api/v1/tracking/track",
        json={
            "event_type": "purchase",
            "user_id": uid,
            "experiment_key": "split-e2e-other",
        },
    )
    assert response.status_code == 200, response.text

    # The count metric of the A/B experiment reads the event exactly once.
    other_counts = results(admin_client, other)
    seen = [v for v in other_counts.values() if v["sample_size"]]
    assert len(seen) == 1
    assert seen[0]["mean"] == 1
    split_counts = results(admin_client, split)
    assert sum(v["conversions"] for v in split_counts.values()) == 1
    assert sent == 1


def test_the_stored_assignment_wins(admin_client, split_experiment, db_session):
    """V4(a): a stored row the hash would not give decides the redirect."""
    experiment = split_experiment(KEY)
    uid = next(u for u in IDS if visit(admin_client, u)["variant_name"] == "Control")
    wipe(db_session, experiment)
    treatment = next(v for v in experiment.variants if v.name == "Treatment")
    db_session.add(
        Assignment(experiment_id=experiment.id, user_id=uid, variant_id=treatment.id)
    )
    db_session.commit()

    answer = visit(admin_client, uid)
    assert (answer["action"], answer["location"]) == (
        "redirect",
        f"{TREATMENT_URL}?utm_source=x",
    )


@pytest.mark.parametrize(
    "shares, expected",
    [
        ((90, 10), {"Control": 915, "Treatment": 85}),
        ((34, 33, 33), {"Control": 334, "B": 353, "C": 313}),
    ],
    ids=["90-10", "34-33-33"],
)
def test_the_split_follows_the_weights_exactly(
    admin_client, split_experiment, db_session, shares, expected
):
    """V2: the counts are exact, from the Locations and from the rows."""
    names = ["Control", "Treatment"] if len(shares) == 2 else ["Control", "B", "C"]
    urls = {
        name: CONTROL_URL if i == 0 else f"https://e.com/checkout-{name.lower()}"
        for i, name in enumerate(names)
    }
    experiment = split_experiment(
        KEY,
        [
            (name, i == 0, share, urls[name])
            for i, (name, share) in enumerate(zip(names, shares))
        ],
    )
    answers = {uid: visit(admin_client, uid) for uid in IDS}
    assert split_of(answers, urls) == expected
    assert Counter(rows(db_session, experiment).values()) == expected


def test_a_bandit_sends_new_visitors_by_its_weights(admin_client, split_experiment):
    """V5: at weights 0/100, all 50 fresh ids are sent to Treatment."""
    split_experiment(
        KEY,
        optimization_type="thompson_sampling",
        bandit_weights={"Control": 0.0, "Treatment": 1.0},
    )
    answers = [visit(admin_client, f"bandit-{i}") for i in range(50)]
    assert Counter(a["variant_name"] for a in answers) == {"Treatment": 50}
    assert all(a["action"] == "redirect" for a in answers)


def test_a_visitor_targeting_excludes_is_served_and_not_recorded(
    admin_client, split_experiment, db_session
):
    """V5: targeting evaluated against the context the request carries."""
    rule = {
        "logical_operator": "AND",
        "groups": [
            {
                "logical_operator": "AND",
                "conditions": [
                    {"attribute": "country", "operator": "equals", "value": "US"}
                ],
            }
        ],
    }
    experiment = split_experiment(KEY, targeting_rules=rule)
    answer = visit(admin_client, "targeted-out")
    assert (answer["action"], answer["assigned"], answer["reason"]) == (
        "serve",
        False,
        "targeting",
    )
    assert rows(db_session, experiment) == {}
    assert visit(admin_client, "targeted-in", context={"country": "US"})["assigned"]


@pytest.mark.regression
def test_split_url_experiments_honour_the_global_holdout(
    admin_client, split_experiment, db_session
):
    """V5: a visitor the global holdout buckets is served the page they asked
    for, reason holdout, with no assignment row; the others are assigned."""
    experiment = split_experiment(KEY)
    service = GlobalHoldoutService(db_session)
    holdout = service.create_holdout(
        name="split-url-holdout", holdout_percentage=20, is_active=True
    )
    try:
        held = [u for u in IDS[:100] if service.is_user_in_holdout(u, holdout)[0]]
        assert held
        for uid in IDS[:100]:
            answer = visit(admin_client, uid)
            if uid in held:
                assert (answer["action"], answer["reason"]) == ("serve", "holdout")
                assert answer["assigned"] is False
        stored = rows(db_session, experiment)
        assert not set(held) & set(stored)
        assert len(stored) == 100 - len(held)
    finally:
        db_session.rollback()
        db_session.delete(db_session.merge(holdout))
        db_session.commit()


@pytest.mark.parametrize(
    "status",
    [ExperimentStatus.PAUSED, ExperimentStatus.DRAFT, ExperimentStatus.COMPLETED],
)
def test_an_experiment_that_is_not_running_serves_everyone(
    admin_client, split_experiment, db_session, status
):
    """V5: everyone is served the page, with the status as the reason, and
    nothing new is recorded."""
    experiment = split_experiment(KEY, status=status)
    for uid in IDS[:20]:
        answer = visit(admin_client, uid)
        assert (answer["action"], answer["reason"]) == ("serve", status.value)
    assert rows(db_session, experiment) == {}
    assert exposures(db_session, experiment) == 0


def test_the_preview_agrees_with_the_route_and_records_nothing(
    admin_client, split_experiment, db_session
):
    """V6(a)(b): the preview runs first, on ids never routed; it records
    nothing, and then agrees with the route for 1000 of 1000."""
    experiment = split_experiment(KEY)
    previewed = {}
    for uid in IDS:
        response = admin_client.get(
            f"/api/v1/experiments/{experiment.id}/split-url/preview",
            params={"user_id": uid},
        )
        assert response.status_code == 200, response.text
        previewed[uid] = response.json()
    assert rows(db_session, experiment) == {}
    assert exposures(db_session, experiment) == 0

    disagree = [
        uid
        for uid in IDS
        if visit(admin_client, uid)["variant_name"] != previewed[uid]["variant_name"]
    ]
    assert disagree == []
    urls = {p["variant_name"]: p["url"] for p in previewed.values()}
    assert urls == {"Control": CONTROL_URL, "Treatment": TREATMENT_URL}


def test_the_preview_of_a_bandit_agrees_with_the_route(admin_client, split_experiment):
    """V6(c): at weights 0/100 the preview answers Treatment for 50 fresh ids,
    as the route then does."""
    experiment = split_experiment(
        KEY,
        optimization_type="thompson_sampling",
        bandit_weights={"Control": 0.0, "Treatment": 1.0},
    )
    ids = [f"bandit-preview-{i}" for i in range(50)]
    previewed = [
        admin_client.get(
            f"/api/v1/experiments/{experiment.id}/split-url/preview",
            params={"user_id": uid},
        ).json()["variant_name"]
        for uid in ids
    ]
    routed = [visit(admin_client, uid)["variant_name"] for uid in ids]
    assert previewed == routed == ["Treatment"] * 50


def test_the_preview_answers_a_stored_assignment(
    admin_client, split_experiment, db_session
):
    """The preview reads a visitor's stored row, as the route does."""
    experiment = split_experiment(KEY)
    uid = next(u for u in IDS if visit(admin_client, u)["variant_name"] == "Control")
    wipe(db_session, experiment)
    treatment = next(v for v in experiment.variants if v.name == "Treatment")
    db_session.add(
        Assignment(experiment_id=experiment.id, user_id=uid, variant_id=treatment.id)
    )
    db_session.commit()
    response = admin_client.get(
        f"/api/v1/experiments/{experiment.id}/split-url/preview",
        params={"user_id": uid},
    )
    assert response.json()["url"] == TREATMENT_URL


@pytest.mark.parametrize("substring", BOT_SUBSTRINGS)
def test_a_crawler_is_answered_as_a_new_visitor_and_recorded_nowhere(
    admin_client, split_experiment, db_session, substring
):
    """V11: action and location as for a new visitor, reason bot, 0 rows and 0
    exposures, for each listed substring in any case."""
    experiment = split_experiment(KEY)
    user_agent = f"Mozilla/5.0 (compatible; {substring.upper()}/2.1)"
    for uid in IDS[:20]:
        bot = visit(admin_client, uid, user_agent=user_agent)
        assert bot["reason"] == "bot"
        assert bot["assigned"] is False
        human = admin_client.get(
            f"/api/v1/experiments/{experiment.id}/split-url/preview",
            params={"user_id": uid},
        ).json()
        assert bot["variant_name"] == human["variant_name"]
        expected = "redirect" if human["variant_name"] == "Treatment" else "serve"
        assert bot["action"] == expected
    assert rows(db_session, experiment) == {}
    assert exposures(db_session, experiment) == 0


def test_a_crawler_on_a_bandit_follows_the_weights(admin_client, split_experiment):
    """V11: the bot path chooses as the route does for a new visitor."""
    split_experiment(
        KEY,
        optimization_type="thompson_sampling",
        bandit_weights={"Control": 0.0, "Treatment": 1.0},
    )
    answers = [
        visit(admin_client, f"bot-{i}", user_agent="Googlebot/2.1") for i in range(20)
    ]
    assert {(a["action"], a["variant_name"]) for a in answers} == {
        ("redirect", "Treatment")
    }


def test_only_the_first_512_characters_of_a_user_agent_are_read(
    admin_client, split_experiment, db_session
):
    """V11: a crawler's token past character 512 is not looked at."""
    experiment = split_experiment(KEY)
    answer = visit(admin_client, IDS[0], user_agent="x" * 512 + "Googlebot")
    assert answer["reason"] == "assigned"
    assert len(rows(db_session, experiment)) == 1


def test_exposures_are_exact(admin_client, split_experiment, db_session):
    """V12: a first visit records 1 row and 1 exposure; a repeat, 0 new rows
    and 1 more exposure; not_entry and an ineligible visitor record nothing."""
    experiment = split_experiment(KEY)
    visit(admin_client, IDS[0])
    assert (len(rows(db_session, experiment)), exposures(db_session, experiment)) == (
        1,
        1,
    )
    visit(admin_client, IDS[0])
    assert (len(rows(db_session, experiment)), exposures(db_session, experiment)) == (
        1,
        2,
    )
    visit(admin_client, IDS[1], path="/elsewhere")
    visit(admin_client, IDS[2], host="other.example")
    assert (len(rows(db_session, experiment)), exposures(db_session, experiment)) == (
        1,
        2,
    )
