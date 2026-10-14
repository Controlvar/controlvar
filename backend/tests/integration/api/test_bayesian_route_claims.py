"""``GET /api/v1/results/{id}/bayesian`` through the API, against a real database.

* it is computed on every request: traffic that arrives after the results were
  cached shows there at once, and the route writes nothing to the cache;
* ``prior_family`` is stored as sent (``beta``, ``normal`` or ``gamma``), and
  every config is analysed as Beta-Binomial: each variant's posterior is
  Beta(alpha + converting users, beta + users who did not convert), whatever
  the family; any other family answers 422.
"""

from __future__ import annotations

from typing import Dict, Tuple

import pytest

from backend.app.models.user import UserRole
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


@pytest.fixture(autouse=True)
def _sign_in(monkeypatch):
    local_sign_in(monkeypatch)


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


def _bayesian_experiment(client, owner, config):
    experiment = create_experiment(
        client, owner, bayesian_enabled=True, bayesian_config=config
    )
    start(client, experiment, owner)
    return experiment


def _traffic(client, key, experiment, prefix, users=20) -> Dict[str, Tuple[int, int]]:
    """Assign ``users`` users; every third does not convert, some convert twice.

    Returns {variant name: (converting users, users)}.
    """
    names = {str(v["id"]): v["name"] for v in experiment["variants"]}
    event = experiment["metrics"][0]["event_name"]
    counts: Dict[str, list] = {name: [0, 0] for name in names.values()}
    for i in range(users):
        user_id = f"{prefix}-{i}"
        name = names[str(assign(client, key, experiment["key"], user_id)["variant_id"])]
        counts[name][1] += 1
        if i % 3 == 0:
            continue
        counts[name][0] += 1
        for _ in range(2 if i % 2 else 1):
            track(client, key, user_id, event, experiment_key=experiment["key"])
    return {name: (c, n) for name, (c, n) in counts.items()}


def _posteriors(resp) -> Dict[str, Tuple[float, float]]:
    assert resp.status_code == 200, resp.text
    body = resp.json()
    block = body.get("bayesian_results") or body
    return {
        row["variant_key"]: (row["posterior"]["alpha"], row["posterior"]["beta"])
        for row in block["variant_results"]
    }


def test_the_bayesian_route_is_computed_on_every_request(
    client, db_session, monkeypatch
):
    redis = fake_results_cache(monkeypatch)
    owner = make_user(db_session, UserRole.ADMIN, superuser=True, tag="bayes")
    experiment = _bayesian_experiment(client, owner, {"alpha": 1.0, "beta": 1.0})
    key = api_key(client, owner)
    results_url = f"/api/v1/results/{experiment['id']}"
    counts = _traffic(client, key, experiment, "bayes-fresh")

    cached = _posteriors(client.get(results_url, headers=auth(owner)))
    assert cached == {name: (1.0 + c, 1.0 + n - c) for name, (c, n) in counts.items()}
    writes = len(redis.sets)
    assert writes == 1, redis.sets

    more = _traffic(client, key, experiment, "bayes-later", users=9)
    expected = {
        name: (1.0 + c + more[name][0], 1.0 + (n - c) + (more[name][1] - more[name][0]))
        for name, (c, n) in counts.items()
    }
    assert expected != cached
    fresh = _posteriors(client.get(f"{results_url}/bayesian", headers=auth(owner)))
    assert fresh == expected
    assert len(redis.sets) == writes, "the Bayesian route wrote to the cache"
    # The results themselves still come from the cache.
    assert _posteriors(client.get(results_url, headers=auth(owner))) == cached


@pytest.mark.parametrize("family", ["beta", "normal", "gamma"])
def test_every_prior_family_is_stored_and_analysed_as_beta_binomial(
    client, db_session, family
):
    owner = make_user(db_session, UserRole.ADMIN, superuser=True, tag="prior")
    config = {"prior_family": family, "alpha": 2.0, "beta": 3.0}
    experiment = _bayesian_experiment(client, owner, config)
    stored = client.get(
        f"/api/v1/experiments/{experiment['id']}", headers=auth(owner)
    ).json()
    assert stored["bayesian_config"]["prior_family"] == family
    key = api_key(client, owner)
    counts = _traffic(client, key, experiment, f"prior-{family}")
    posteriors = _posteriors(
        client.get(f"/api/v1/results/{experiment['id']}/bayesian", headers=auth(owner))
    )
    assert posteriors == {
        name: (2.0 + c, 3.0 + n - c) for name, (c, n) in counts.items()
    }


@pytest.mark.parametrize("family", ["poisson", "lognormal", "Beta", ""])
def test_any_other_prior_family_is_refused(client, db_session, family):
    owner = make_user(db_session, UserRole.ADMIN, superuser=True, tag="prior422")
    resp = client.post(
        "/api/v1/experiments/",
        json={
            "name": "Prior family",
            "hypothesis": "only three families",
            "experiment_type": "a_b",
            "variants": [
                {"name": "control", "is_control": True, "traffic_allocation": 50},
                {"name": "treatment", "is_control": False, "traffic_allocation": 50},
            ],
            "metrics": [
                {"name": "Buy", "event_name": "buy", "metric_type": "conversion"}
            ],
            "bayesian_enabled": True,
            "bayesian_config": {"prior_family": family},
        },
        headers=auth(owner),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"][-2:] == ["bayesian_config", "prior_family"]


def test_turning_it_on_changes_neither_the_frequentist_results_nor_the_recommendation(
    client, db_session
):
    owner = make_user(db_session, UserRole.ADMIN, superuser=True, tag="second")
    experiment = create_experiment(client, owner)
    start(client, experiment, owner)
    key = api_key(client, owner)
    counts = _traffic(client, key, experiment, "bayes-second", users=30)
    url = f"/api/v1/results/{experiment['id']}"

    off = client.get(url, params={"use_cache": "false"}, headers=auth(owner))
    assert off.status_code == 200, off.text
    assert off.json()["bayesian_results"] is None
    turned_on = client.put(
        f"/api/v1/experiments/{experiment['id']}",
        json={"bayesian_enabled": True},
        headers=auth(owner),
    )
    assert turned_on.status_code == 200, turned_on.text
    on = client.get(url, params={"use_cache": "false"}, headers=auth(owner))
    assert on.status_code == 200, on.text

    for field in ("metrics", "summary", "sample_size_adequate", "srm"):
        assert on.json()[field] == off.json()[field], field
    assert (
        on.json()["summary"]["recommendation"]
        == off.json()["summary"]["recommendation"]
    )
    # A second analysis of the primary metric, with the default priors Beta(1, 1).
    assert _posteriors(on) == {
        name: (1.0 + c, 1.0 + n - c) for name, (c, n) in counts.items()
    }
    rows = on.json()["bayesian_results"]["variant_results"]
    assert sum(row["probability_to_be_best"] for row in rows) == pytest.approx(1.0)
