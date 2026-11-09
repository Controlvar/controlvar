"""The sequential analysis through the API, against a real database.

* any signed-in user reads ``GET /results/{id}/sequential``;
* it counts the users who sent the primary metric's event, once each, whatever
  the metric's ``metric_type``;
* read after each of three slices of traffic on one experiment, it answers for
  the counts so far, as an oracle written in the test computes them;
* the block embedded in ``GET /results/{id}`` is computed at the stored alpha,
  and that route's ``confidence_level`` and ``correction_method`` do not move it;
* that block is cached with the rest of the results (5 minutes while the
  experiment runs, 24 hours otherwise), so a change to the sequential settings,
  made here while the experiment is a draft, shows there only once the cache is
  cleared, while ``/sequential`` is not cached;
* once the experiment is running, the ``PUT`` that changes those settings
  answers 400 for everyone but a superuser.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Any, Dict, Tuple

import pytest

from backend.app.models.user import UserRole
from backend.app.services.sequential_testing_service import SequentialTestingService
from backend.tests.integration.api.route_helpers import (
    api_client,
    api_key,
    assign,
    auth,
    create_experiment,
    fake_results_cache,
    local_sign_in,
    make_user,
    start,
    track,
)

pytestmark = [pytest.mark.integration, pytest.mark.regression]

ACTIVE_UPDATE = "Cannot update experiments in active status"


@pytest.fixture(autouse=True)
def _sign_in(monkeypatch):
    local_sign_in(monkeypatch)


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


def _enable(client, experiment, user, config: Dict[str, Any]) -> None:
    resp = client.put(
        f"/api/v1/experiments/{experiment['id']}",
        json={"sequential_testing_enabled": True, "sequential_testing_config": config},
        headers=auth(user),
    )
    assert resp.status_code == 200, resp.text


def _running(client, db_session, *, metric_type="conversion", config=None, **fields):
    """A running experiment with sequential testing on, owned by a superuser."""
    owner = make_user(db_session, UserRole.ADMIN, superuser=True, tag="seq")
    experiment = create_experiment(client, owner, metric_type=metric_type, **fields)
    _enable(client, experiment, owner, config or {})
    start(client, experiment, owner)
    return owner, experiment


def _two_in_three(is_control: bool, i: int) -> bool:
    return i % 3 != 0


def _traffic(
    client, owner, experiment, prefix: str, converts=_two_in_three
) -> Tuple[int, int, int, int]:
    """Assign 24 users; some convert, a few of them more than once.

    User ``i`` converts when ``converts(is_control, i)``: by default two in
    three, whatever their arm.

    Returns the counts the analysis should use: (control converters, control
    users, treatment converters, treatment users).
    """
    key = api_key(client, owner)
    event = experiment["metrics"][0]["event_name"]
    control_id = next(v["id"] for v in experiment["variants"] if v["is_control"])
    counts = {True: [0, 0], False: [0, 0]}
    for i in range(24):
        user_id = f"{prefix}-{i}"
        answer = assign(client, key, experiment["key"], user_id)
        is_control = str(answer["variant_id"]) == str(control_id)
        counts[is_control][1] += 1
        if not converts(is_control, i):
            continue
        counts[is_control][0] += 1
        # Every other converter sends the event three times, with values that
        # would move a sum or a mean.
        for _ in range(3 if i % 2 else 1):
            track(
                client,
                key,
                user_id,
                event,
                experiment_key=experiment["key"],
                value=250.0 if i % 2 else 0.0,
            )
    # Events from a user who was never assigned do not count.
    track(client, key, f"{prefix}-stranger", event, experiment_key=experiment["key"])
    assert counts[True][1] and counts[False][1], counts
    return counts[True][0], counts[True][1], counts[False][0], counts[False][1]


@pytest.mark.parametrize(
    "role, superuser",
    [
        (UserRole.VIEWER, False),
        (UserRole.ANALYST, False),
        (UserRole.DEVELOPER, False),
        (UserRole.ADMIN, True),
    ],
)
def test_every_shipped_role_reads_the_sequential_analysis(
    client, db_session, role, superuser
):
    _, experiment = _running(client, db_session)
    reader = make_user(db_session, role, superuser=superuser, tag="seqread")
    resp = client.get(
        f"/api/v1/results/{experiment['id']}/sequential", headers=auth(reader)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["method"] == "msprt"
    anonymous = client.get(f"/api/v1/results/{experiment['id']}/sequential")
    assert anonymous.status_code == 401


@pytest.mark.parametrize(
    "metric_type", ["conversion", "revenue", "count", "duration", "custom"]
)
def test_it_counts_users_who_sent_the_event_whatever_the_metric_type(
    client, db_session, metric_type
):
    owner, experiment = _running(client, db_session, metric_type=metric_type)
    counts = _traffic(client, owner, experiment, f"seq-{metric_type}")
    expected = SequentialTestingService().compute_always_valid_ci(
        *counts, alpha=0.05, tau_squared=0.001
    )
    resp = client.get(
        f"/api/v1/results/{experiment['id']}/sequential", headers=auth(owner)
    )
    assert resp.status_code == 200, resp.text
    interval = resp.json()["confidence_sequence"]
    assert interval["sample_size"] == counts[1] + counts[3] == 24
    assert interval["lower"] == pytest.approx(expected.lower, rel=1e-12)
    assert interval["upper"] == pytest.approx(expected.upper, rel=1e-12)


#: The stored settings of the slice test. Not the defaults (0.05, 0.001), so a
#: route that ignored the stored configuration would not agree with the oracle.
SLICE_ALPHA = 0.1
SLICE_TAU_SQUARED = 0.01

#: Who converts in each slice: in the first only the treatment's users, in the
#: next two half of each arm (by user index, whatever the arm).
SLICES = (
    lambda is_control, i: not is_control,
    lambda is_control, i: i % 4 in (1, 2),
    lambda is_control, i: i % 4 in (1, 2),
)


def _decimal(value: Fraction) -> Decimal:
    return Decimal(value.numerator) / Decimal(value.denominator)


def _oracle(cs: int, cn: int, ts: int, tn: int) -> Tuple[float, float, float, float]:
    """lambda, always-valid p and the bounds of the confidence sequence for
    these counts, computed here and not by the product.

    ``fractions.Fraction`` for the Agresti-Caffo variance V of the difference
    (p~ = (x + 1) / (n + 2), V = p~c (1 - p~c) / (nc + 2) + p~t (1 - p~t) /
    (nt + 2)) and the observed difference delta; 50-digit ``decimal.Decimal``
    for the rest: lambda = sqrt(V / (V + tau^2)) * exp(tau^2 delta^2 / V /
    (2 (V + tau^2))), p = min(1, 1 / lambda), and the bounds delta -/+ sqrt(V
    (V + tau^2) / tau^2 * (2 ln(1 / alpha) + ln((V + tau^2) / V))) clipped to
    [-1, 1]. docs/api/sequential-testing.md states V and the bounds; lambda is
    the normal-mixture ratio that interval inverts.
    """
    with localcontext() as ctx:
        ctx.prec = 50
        pc = Fraction(cs + 1, cn + 2)
        pt = Fraction(ts + 1, tn + 2)
        v = _decimal(pc * (1 - pc) / (cn + 2) + pt * (1 - pt) / (tn + 2))
        delta = _decimal(Fraction(ts, tn) - Fraction(cs, cn))
        tau2 = Decimal(str(SLICE_TAU_SQUARED))
        alpha = Decimal(str(SLICE_ALPHA))
        lam = (v / (v + tau2)).sqrt() * (tau2 * delta**2 / v / (2 * (v + tau2))).exp()
        p_value = min(Decimal(1), 1 / lam)
        half = (
            v * (v + tau2) / tau2 * (2 * (1 / alpha).ln() + ((v + tau2) / v).ln())
        ).sqrt()
        lower = max(delta - half, Decimal(-1))
        upper = min(delta + half, Decimal(1))
        return float(lam), float(p_value), float(lower), float(upper)


def test_after_each_slice_of_traffic_the_answer_is_the_one_for_the_counts_so_far(
    client, db_session
):
    """Three slices of traffic on ONE experiment, ``/sequential`` read after each.

    Each answer equals the oracle above for the counts so far, and its sample
    size is the number of users assigned so far: the route keeps nothing
    between calls, so a cache or memo of the counts answers slice 2 with slice
    1's numbers and fails here. The request carries no ``use_cache``: the route
    has no such parameter (an undeclared one is ignored), and a cache that
    honoured one would be bypassed by it rather than caught.

    The experiment key is fixed, so the same users land in the same arms on
    every run (the database is built afresh for each pytest session), and the
    answers move as the docs say they can: stop_for_effect after the first two
    slices, continue after the third.
    """
    owner, experiment = _running(
        client,
        db_session,
        config={"alpha": SLICE_ALPHA, "tau_squared": SLICE_TAU_SQUARED},
        key="seq-three-slices",
    )
    url = f"/api/v1/results/{experiment['id']}/sequential"
    totals = (0, 0, 0, 0)
    actions = []
    for slice_number, converts in enumerate(SLICES, start=1):
        counts = _traffic(
            client, owner, experiment, f"seq-slice{slice_number}", converts
        )
        totals = tuple(total + count for total, count in zip(totals, counts))
        resp = client.get(url, headers=auth(owner))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        msprt, interval = body["msprt_result"], body["confidence_sequence"]
        where = f"slice {slice_number}, counts {totals}"
        lam, p_value, lower, upper = _oracle(*totals)
        can_stop = lam >= 1 / SLICE_ALPHA
        actions.append(body["recommended_action"])

        assert interval["sample_size"] == 24 * slice_number, where
        assert totals[1] + totals[3] == 24 * slice_number, where
        assert msprt["lambda_ratio"] == pytest.approx(lam, rel=1e-9), where
        assert msprt["always_valid_p_value"] == pytest.approx(p_value, rel=1e-9), where
        assert interval["lower"] == pytest.approx(lower, rel=1e-9, abs=1e-12), where
        assert interval["upper"] == pytest.approx(upper, rel=1e-9, abs=1e-12), where
        assert msprt["boundary"] == pytest.approx(1 / SLICE_ALPHA), where
        assert msprt["can_stop"] is can_stop, where
        assert body["recommended_action"] == (
            "stop_for_effect" if can_stop else "continue"
        ), where
        trajectory = body["evidence_trajectory"]
        assert [point["sample_size"] for point in trajectory] == [24 * slice_number]
    # On this key's split the evidence crosses the boundary, then falls back
    # below it: a later answer says continue after stop_for_effect.
    assert actions == ["stop_for_effect", "stop_for_effect", "continue"], actions


def test_the_results_routes_settings_do_not_change_the_embedded_block(
    client, db_session
):
    owner, experiment = _running(client, db_session, config={"alpha": 0.1})
    _traffic(client, owner, experiment, "seq-settings")
    url = f"/api/v1/results/{experiment['id']}"
    blocks = []
    for params in (
        {},
        {"confidence_level": 0.8},
        {"confidence_level": 0.99, "correction_method": "bonferroni"},
        {"correction_method": "none"},
        # No alpha on this route: a query parameter of that name changes nothing.
        {"alpha": 0.01},
    ):
        resp = client.get(
            url, params={**params, "use_cache": "false"}, headers=auth(owner)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["confidence_level"] == params.get("confidence_level", 0.95)
        blocks.append(resp.json()["sequential_testing"])
    assert blocks[0]["msprt_result"]["boundary"] == pytest.approx(10.0)
    assert all(block == blocks[0] for block in blocks), blocks
    # The same analysis as /sequential without alpha; with alpha only there.
    sequential = client.get(f"{url}/sequential", headers=auth(owner)).json()
    assert sequential["msprt_result"] == blocks[0]["msprt_result"]
    other = client.get(
        f"{url}/sequential", params={"alpha": 0.01}, headers=auth(owner)
    ).json()
    assert other["msprt_result"]["boundary"] == pytest.approx(100.0)


def _boundary(resp) -> float:
    assert resp.status_code == 200, resp.text
    body = resp.json()
    block = body.get("sequential_testing", body)
    return block["msprt_result"]["boundary"]


def test_once_running_only_a_superuser_changes_the_sequential_settings(
    client, db_session
):
    owner, experiment = _running(client, db_session)
    developer = make_user(db_session, UserRole.DEVELOPER, tag="seqdev")
    admin = make_user(db_session, UserRole.ADMIN, tag="seqadmin")
    exp_url = f"/api/v1/experiments/{experiment['id']}"
    seq_url = f"/api/v1/results/{experiment['id']}/sequential"
    for change in (
        {"sequential_testing_config": {"alpha": 0.1}},
        {"sequential_testing_config": {"tau_squared": 0.01}},
        {"sequential_testing_enabled": False},
    ):
        for user in (developer, admin):
            refused = client.put(exp_url, json=change, headers=auth(user))
            assert refused.status_code == 400, refused.text
            assert refused.json()["detail"] == ACTIVE_UPDATE
    assert _boundary(client.get(seq_url, headers=auth(owner))) == 20.0
    stored = client.get(exp_url, headers=auth(owner)).json()
    assert stored["sequential_testing_enabled"] is True
    assert stored["sequential_testing_config"].get("alpha", 0.05) == 0.05

    accepted = client.put(
        exp_url,
        json={"sequential_testing_config": {"alpha": 0.1}},
        headers=auth(owner),
    )
    assert accepted.status_code == 200, accepted.text
    assert _boundary(client.get(seq_url, headers=auth(owner))) == pytest.approx(10.0)


def _written(redis, experiment) -> list:
    """The TTLs of the results cache entries written for *experiment*."""
    prefix = f"results:{experiment['id']}:"
    return [ttl for key, ttl in redis.sets if key.startswith(prefix)]


def test_a_settings_change_reaches_the_cached_results_only_once_cleared(
    client, db_session, monkeypatch
):
    """The change is made while the experiment is a draft, whose results are
    kept 24 hours; a running experiment's are kept 5 minutes."""
    redis = fake_results_cache(monkeypatch)
    owner = make_user(db_session, UserRole.ADMIN, superuser=True, tag="seqcache")
    draft = create_experiment(client, owner)
    _enable(client, draft, owner, {})
    exp_url = f"/api/v1/experiments/{draft['id']}"
    url = f"/api/v1/results/{draft['id']}"

    assert _boundary(client.get(url, headers=auth(owner))) == pytest.approx(20.0)
    assert _written(redis, draft) == [86400], redis.sets

    accepted = client.put(
        exp_url,
        json={"sequential_testing_config": {"alpha": 0.1}},
        headers=auth(owner),
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "draft"

    # /sequential is computed on every request; the results come from the cache.
    assert _boundary(client.get(f"{url}/sequential", headers=auth(owner))) == 10.0
    assert _boundary(client.get(url, headers=auth(owner))) == 20.0, "cached 20.0"
    cleared = client.post(f"{url}/invalidate-cache", headers=auth(owner))
    assert cleared.status_code == 200, cleared.text
    assert _boundary(client.get(url, headers=auth(owner))) == pytest.approx(10.0)

    _, running = _running(client, db_session)
    running_url = f"/api/v1/results/{running['id']}"
    assert _boundary(client.get(running_url, headers=auth(owner))) == pytest.approx(
        20.0
    )
    assert _written(redis, running) == [300], redis.sets


def test_results_of_an_experiment_that_is_not_running_are_kept_24_hours(
    client, db_session, monkeypatch
):
    redis = fake_results_cache(monkeypatch)
    owner, experiment = _running(client, db_session)
    completed = client.post(
        f"/api/v1/experiments/{experiment['id']}/complete", headers=auth(owner)
    )
    assert completed.status_code == 200, completed.text
    resp = client.get(f"/api/v1/results/{experiment['id']}", headers=auth(owner))
    assert resp.status_code == 200, resp.text
    written = [
        ttl for key, ttl in redis.sets if key.startswith(f"results:{experiment['id']}:")
    ]
    assert written == [86400], redis.sets


def test_the_configuration_is_stored_with_its_defaults_and_ranges(client, db_session):
    owner = make_user(db_session, UserRole.ADMIN, superuser=True, tag="seqcfg")
    experiment = create_experiment(
        client,
        owner,
        sequential_testing_enabled=True,
        sequential_testing_config={"alpha": 0.05},
    )
    assert experiment["sequential_testing_config"] == {
        "method": "msprt",
        "alpha": 0.05,
        "tau_squared": 0.001,
        "spending_function": "obrien_fleming",
        "planned_looks": 10,
    }
    url = f"/api/v1/experiments/{experiment['id']}"
    for field, good, bad in (
        ("planned_looks", (1, 100, 5), (0, 101)),
        ("tau_squared", (1.0, 0.25), (0.0, 1.5)),
    ):
        for value in bad:
            resp = client.put(
                url,
                json={"sequential_testing_config": {field: value}},
                headers=auth(owner),
            )
            assert resp.status_code == 422, resp.text
            loc = resp.json()["detail"][0]["loc"]
            assert loc[-1] == field, loc
        for value in good:
            resp = client.put(
                url,
                json={"sequential_testing_config": {field: value}},
                headers=auth(owner),
            )
            assert resp.status_code == 200, resp.text
            stored = client.get(url, headers=auth(owner)).json()
            assert stored["sequential_testing_config"][field] == value
