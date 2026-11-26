"""How the analysis paths count linked people (#449).

No analysis path changed: events for an experiment are stored under the
person's holder, so every endpoint counts data written after a link once,
under the person's assignment. What existed before a link stays as it was:

* a person whose two ids were both assigned before the link counts once per
  row (IA-A1's P2 and P3, IA-C1, IA-M7);
* holdout results count a linked person's conversions only under the id the
  holdout first recorded, before and after the link (IA-A3);
* interaction detection (IA-A4), CUPED history (IA-A7) and the audience
  preview (IA-A11) count ids.

IA-A1 holds one fixture to every endpoint's numbers, written on the helpers of
``test_converting_users_consistency.py``. The stated limits are pinned as they
behave today (inverted pins): lifting one changes a published
number, which is a change of statistical method with its own issue.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.models.event import Event
from backend.app.models.holdout_population import HoldoutPopulation
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.audience_service import AudienceService
from backend.app.services.balanced_groups_results import stored_strata
from backend.app.services.event_matching import MetricEvents, covariate_user_ids
from backend.app.services.holdout_results import (
    IN_HOLDOUT,
    NOT_IN_HOLDOUT,
    holdout_results,
)
from backend.app.services.interaction_detection_service import (
    InteractionDetectionService,
)
from backend.app.services.segment_breakdown import user_values
from backend.app.services.srm_service import compute_srm_for_experiment
from backend.tests.integration.api.test_converting_users_consistency import (
    _bandit_counts,
    _bayesian_counts,
    _breakdown_conversions,
    _cumulative_counts,
    _get,
    _results_counts,
    _segmented_conversions,
    _sequential_inputs,
    _streaming_counts,
)
from backend.tests.integration.identity_alias_world import (
    World,
    assign,
    held_and_free,
    measurable_holdout,
    no_active_holdout,
    track,
    variant_of,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

#: P1 to P5 in control, Q1 to Q5 in treatment, P3's purchase a row stored
#: before the link (IA-A1).
EXPECTED = {
    "control": {"n": 6, "converted": 4},
    "treatment": {"n": 6, "converted": 3},
}
#: The same people with P3's purchase sent after the link: it is stored under
#: a3, P3's holder, in control, while u3's row stays in treatment's n.
P3_AFTER_THE_LINK = {
    "control": {"n": 6, "converted": 5},
    "treatment": {"n": 6, "converted": 2},
}


@pytest.fixture
def world(db_session, make_experiment):
    with no_active_holdout(db_session):
        w = World(db_session, make_experiment)
        yield w
        w.close()


def _naive(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def _purchase(exp, user_id, variant, at, value=None) -> Event:
    """A purchase row as the tracking routes stored it before any link."""
    return Event(
        event_type="purchase",
        event_name="purchase",
        user_id=user_id,
        experiment_id=exp.id,
        variant_id=variant.id,
        value=value,
        created_at=at.isoformat(),
    )


def _people(admin_client, db_session, world, *, p3_after_the_link: bool):
    """The fixture of IA-A1, which pins when P3's purchase is stored.

    * P1: a1 (control) and u1, linked, no row of its own. a1 buys 10.0 before
      the link; u1 buys 20.0 after it, through ``/track``.
    * P2: a2 (control at t0) and u2 (its own control row at t1, as a release
      that ignores links writes it); one purchase, a row under u2.
    * P3: a3 (control at t0) and u3 (treatment at t1); one purchase under u3,
      a row tagged treatment written before the link, or with
      ``p3_after_the_link`` sent through ``/track`` after it.
    * P4: one id, control, buys.
    * P5: a5 (control) and u5, linked; its only purchase is u5's, after the
      link, through ``/track``.
    * Q1 to Q5: one id each, treatment; Q1 and Q2 buy.
    Only P1's purchases carry a value.
    """
    exp = world.experiment(
        "Alias counts",
        sequential_testing_enabled=True,
        sequential_testing_config={"method": "msprt"},
        bayesian_enabled=True,
        bayesian_config={"prior_family": "beta", "alpha": 1.0, "beta": 1.0},
    )
    control, treatment = variant_of(exp, "control"), variant_of(exp, "treatment")
    t0 = exp.start_date.replace(tzinfo=timezone.utc) + timedelta(days=1, hours=12)
    t1 = t0 + timedelta(hours=1)
    ids = {
        name: world.id(name)
        for name in ("a1", "u1", "a2", "u2", "a3", "u3", "p4", "a5", "u5")
    }
    qs = [world.id(f"q{i}") for i in range(1, 6)]
    for name in ("a1", "a2", "a3", "p4", "a5"):
        world.store_assignment(exp, ids[name], "control", _naive(t0))
    world.store_assignment(exp, ids["u2"], "control", _naive(t1))
    world.store_assignment(exp, ids["u3"], "treatment", _naive(t1))
    for q in qs:
        world.store_assignment(exp, q, "treatment", _naive(t0))

    rows = [
        _purchase(exp, ids["a1"], control, t1, value=10.0),
        _purchase(exp, ids["u2"], control, t1 + timedelta(hours=1)),
        _purchase(exp, ids["p4"], control, t1),
        _purchase(exp, qs[0], treatment, t1),
        _purchase(exp, qs[1], treatment, t1),
    ]
    if not p3_after_the_link:
        # P3's purchase, stored before the link (its timing decides P3's arm).
        rows.append(_purchase(exp, ids["u3"], treatment, t1 + timedelta(hours=1)))
    db_session.add_all(rows)
    db_session.commit()

    for anonymous, known in (("a1", "u1"), ("a2", "u2"), ("a3", "u3"), ("a5", "u5")):
        assert world.link(ids[anonymous], ids[known]) is True

    track(admin_client, exp, ids["u1"], value=20.0)
    track(admin_client, exp, ids["u5"])
    if p3_after_the_link:
        track(admin_client, exp, ids["u3"])
    db_session.refresh(exp)
    return exp, ids


def _export_counts(admin_client, exp):
    response = admin_client.get(
        "/api/v1/export/variants",
        params={
            "format": "json",
            "start_date": (exp.created_at - timedelta(seconds=1)).isoformat(),
        },
    )
    assert response.status_code == 200, response.text
    return {
        r["variant_name"]: {"n": r["assignments"], "converted": r["conversions"]}
        for r in response.json()
        if r["experiment_id"] == str(exp.id)
    }


def test_linked_people_count_per_row_on_every_endpoint(admin_client, db_session, world):
    """IA-A1 (with IA-A2, IA-A8, and IA-E4's pre-link events): post-link data
    counts under the person's assignment; P2 and P3, assigned twice before the
    link, count once per row."""
    exp, ids = _people(admin_client, db_session, world, p3_after_the_link=False)
    base = f"/api/v1/results/{exp.id}"

    results = _get(admin_client, base, breakdown="country")
    assert _results_counts(results) == EXPECTED
    assert results["summary"]["total_conversions"] == 7
    assert _sequential_inputs(admin_client, exp) == EXPECTED
    assert _bayesian_counts(_get(admin_client, f"{base}/bayesian")) == EXPECTED
    assert _bayesian_counts(results["bayesian_results"]) == EXPECTED
    assert _cumulative_counts(_get(admin_client, f"{base}/daily")) == EXPECTED
    assert _streaming_counts(db_session, exp) == EXPECTED
    assert _bandit_counts(db_session, exp) == EXPECTED
    assert _export_counts(admin_client, exp) == EXPECTED
    assert _breakdown_conversions(results) == EXPECTED
    assert _segmented_conversions(db_session, exp) == EXPECTED

    srm = compute_srm_for_experiment(db_session, exp.id)
    names = {str(v.id): v.name for v in exp.variants}
    assert {names[k]: v for k, v in srm.observed.items()} == {
        "control": 6,
        "treatment": 6,
    }

    (metric,) = _get(admin_client, f"{base}/cuped")["metrics"]
    assert metric["method"] == "none"
    assert metric["adjusted_control_mean"] == pytest.approx(4 / 6, abs=1e-4)
    assert metric["adjusted_treatment_mean"] == pytest.approx(0.5, abs=1e-4)
    assert round(metric["adjusted_control_mean"], 4) == 0.6667

    service = AnalysisService(db_session)
    values = {
        v.name: service.value_totals(exp.id, v.id, MetricEvents("purchase", None))
        for v in exp.variants
    }
    assert values == {"control": [30.0], "treatment": []}
    stored = {(e.user_id, e.value) for e in world.events(exp, [ids["u1"], ids["a1"]])}
    assert stored == {(ids["a1"], 10.0), (ids["a1"], 20.0)}


def test_a_purchase_sent_after_the_link_counts_in_the_holders_arm(
    admin_client, db_session, world
):
    """IA-A1, P3's purchase sent after the link: the conflicted person converts in
    the winning arm, while u3's row stays in treatment's n."""
    exp, _ = _people(admin_client, db_session, world, p3_after_the_link=True)
    base = f"/api/v1/results/{exp.id}"

    results = _get(admin_client, base)
    assert _results_counts(results) == P3_AFTER_THE_LINK
    assert results["summary"]["total_conversions"] == 7
    assert _bandit_counts(db_session, exp) == P3_AFTER_THE_LINK
    assert _streaming_counts(db_session, exp) == P3_AFTER_THE_LINK


# ---------------------------------------------------------------------------
# Stated limits, pinned as they behave
# ---------------------------------------------------------------------------


def _holdout_arms(db_session, holdout):
    """``{IN_HOLDOUT: (users, conversions), NOT_IN_HOLDOUT: (...)}``."""
    answer = holdout_results(
        db_session,
        holdout,
        "purchase",
        now=_naive(datetime.now(timezone.utc) + timedelta(minutes=1)),
    )
    return {g["group"]: (g["users"], g["conversions"]) for g in answer["groups"]}


def test_holdout_results_count_a_linked_person_under_its_recorded_id(
    admin_client, db_session, world
):
    """IA-A3, a stated limit.

    Before the link: a1 (held out) and u1 (not) are each recorded, so the
    person is in both arms. After the link as well: the anonymous id b1 is
    held out, the known id v1 is linked to it and buys; the person holds no
    assignment, so the purchase is stored under the known id v1, and b1
    counts as not converted."""
    exp = world.experiment("Holdout limit")
    with measurable_holdout(db_session) as holdout:
        a1, u1 = held_and_free(holdout, world)
        b1, v1 = held_and_free(holdout, world)
        assert assign(admin_client, exp, a1)["reason"] == "holdout"
        assert assign(admin_client, exp, u1)["reason"] == "assigned"
        world.link(a1, u1)
        before = _holdout_arms(db_session, holdout)

        assert assign(admin_client, exp, b1)["reason"] == "holdout"
        world.link(b1, v1)
        assert assign(admin_client, exp, v1)["reason"] == "holdout"
        track(admin_client, exp, v1)
        after = _holdout_arms(db_session, holdout)

        stored = [e.user_id for e in world.events(exp, [b1, v1], "purchase")]

    assert before == {IN_HOLDOUT: (1, 0), NOT_IN_HOLDOUT: (1, 0)}
    assert stored == [v1]
    assert after == {IN_HOLDOUT: (2, 0), NOT_IN_HOLDOUT: (1, 0)}, (
        "b1 counted as converted"
    )


def test_a_linked_persons_holdout_follows_the_first_recorded_row(
    admin_client, db_session, world
):
    """D3, "the earliest population row decides": a1 was recorded held out
    before u1 was recorded outside, so after the link both ids answer
    ``holdout`` in a new experiment, with no assignment and no new row."""
    first = world.experiment("Holdout first")
    second = world.experiment("Holdout second")
    with measurable_holdout(db_session) as holdout:
        a1, u1 = held_and_free(holdout, world)
        assert assign(admin_client, first, a1)["reason"] == "holdout"
        assert assign(admin_client, first, u1)["reason"] == "assigned"
        world.link(a1, u1)

        def population():
            db_session.expire_all()
            return sorted(
                (r.user_id, r.in_holdout, r.first_seen_at)
                for r in db_session.query(HoldoutPopulation).filter(
                    HoldoutPopulation.holdout_id == holdout.id,
                    HoldoutPopulation.user_id.in_([a1, u1]),
                )
            )

        before = population()
        assert [(u, h) for u, h, _ in before] == sorted([(a1, True), (u1, False)])

        for user_id in (a1, u1):
            answer = assign(admin_client, second, user_id)
            assert (answer["assigned"], answer["reason"]) == (False, "holdout"), (
                f"{user_id} answered {answer['reason']}"
            )
        assert world.rows(second, [a1, u1]) == []
        assert population() == before


def test_interaction_detection_counts_ids(admin_client, db_session, world):
    """IA-A4, a stated limit: a1 in E1 before the link and u1 in E2 after it
    are one person, and the two experiments still share no user."""
    e1, e2 = world.experiment("Overlap one"), world.experiment("Overlap two")
    a1, u1 = world.id("anon"), world.id("known")
    assign(admin_client, e1, a1)
    world.link(a1, u1)
    assign(admin_client, e2, u1)

    service = InteractionDetectionService()
    shared = service._shared_assignments(db_session, e1.id, e2.id)
    overlap = service.compute_user_overlap(
        service._get_experiment_users(str(e1.id), db_session),
        service._get_experiment_users(str(e2.id), db_session),
    )
    assert [r.user_id for r in world.rows(e2, [a1, u1])] == [u1]
    assert shared == {}
    assert overlap == 0.0


def test_a_linked_id_takes_no_second_place_in_a_balanced_group(
    admin_client, db_session, world
):
    """IA-A5: u1 asks after the link with a1 holding gold's first place."""
    exp = world.experiment(
        "Balanced alias", balance_within={"attribute": "tier", "values": ["gold"]}
    )
    a1, u1 = world.id("anon"), world.id("known")
    first = assign(admin_client, exp, a1, context={"tier": "gold"})
    world.link(a1, u1)

    answer = assign(admin_client, exp, u1, context={"tier": "gold"})

    assert answer["variant_id"] == first["variant_id"]
    strata = stored_strata(db_session, exp.id, "tier")
    group = [user for user, tier in strata.items() if tier == "gold"]
    assert group == [a1], f"group n {len(group)}"


def test_a_linked_person_is_in_one_segment(admin_client, db_session, world):
    """IA-A6: the person's first exposure's country, as for any user."""
    exp = world.experiment("Segment alias")
    a1, u1 = world.id("anon"), world.id("known")
    assign(admin_client, exp, a1, context={"country": "US"})
    world.link(a1, u1)
    assign(admin_client, exp, u1, context={"country": "DE"})

    values = user_values(db_session, exp.id, "country")
    ours = {u: v for u, v in values.items() if u in (a1, u1)}
    assert ours == {a1: "US"}, f"person in {len(ours)} segments: {ours}"


def test_cuped_history_is_read_per_id(admin_client, db_session, world):
    """IA-A7, a stated limit: u1's purchase before a1 was assigned is not the
    person's pre-period covariate."""
    exp = world.experiment("CUPED alias")
    a1, u1 = world.id("anon"), world.id("known")
    db_session.add(
        Event(
            event_type="purchase",
            event_name="purchase",
            user_id=u1,
            experiment_id=None,
            value=None,
            created_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
        )
    )
    db_session.commit()
    assign(admin_client, exp, a1)
    world.link(a1, u1)
    (row,) = world.rows(exp, [a1, u1])

    found, skipped = covariate_user_ids(
        db_session, {row.user_id: row.created_at}, "purchase", lookback_days=14
    )
    probe, _ = covariate_user_ids(
        db_session, {u1: row.created_at}, "purchase", lookback_days=14
    )

    assert row.user_id == a1
    assert probe == {u1}  # the history is there, under u1's own id
    assert (found, skipped) == (set(), 0), "u1's history read as a1's covariate"


def test_the_audience_preview_counts_ids(admin_client, db_session, world):
    """IA-A11, a stated limit: a1 and u1, both seen before the link, are one
    person and two users in the preview."""
    e1, e2 = world.experiment("Audience one"), world.experiment("Audience two")
    a1, u1 = world.id("anon"), world.id("known")
    token = world.id("probe")
    assign(admin_client, e1, a1, context={"alias_probe": token})
    assign(admin_client, e2, u1, context={"alias_probe": token})
    world.link(a1, u1)

    preview = AudienceService.preview_audience_size(
        db_session,
        {
            "logical_operator": "AND",
            "groups": [
                {
                    "logical_operator": "AND",
                    "conditions": [
                        {
                            "attribute": "alias_probe",
                            "operator": "equals",
                            "value": token,
                        }
                    ],
                }
            ],
        },
    )
    assert preview.matched == 2, f"person counted {preview.matched} times"
