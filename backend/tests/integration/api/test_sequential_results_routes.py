"""The sequential analysis through the API, against a real database.

* any signed-in user reads ``GET /results/{id}/sequential``;
* it counts the users who sent the primary metric's event, once each, whatever
  the metric's ``metric_type``;
* the block embedded in ``GET /results/{id}`` is computed at the stored alpha,
  and that route's ``confidence_level`` and ``correction_method`` do not move it;
* that block is cached with the rest of the results (5 minutes while the
  experiment runs, 24 hours otherwise), so a change to the sequential settings
  shows there only once the cache is cleared, while ``/sequential`` is not
  cached;
* once the experiment is running, the ``PUT`` that changes those settings
  answers 400 for everyone but a superuser.
"""

from __future__ import annotations

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


def _running(client, db_session, *, metric_type="conversion", config=None):
    """A running experiment with sequential testing on, owned by a superuser."""
    owner = make_user(db_session, UserRole.ADMIN, superuser=True, tag="seq")
    experiment = create_experiment(client, owner, metric_type=metric_type)
    _enable(client, experiment, owner, config or {})
    start(client, experiment, owner)
    return owner, experiment


def _traffic(client, owner, experiment, prefix: str) -> Tuple[int, int, int, int]:
    """Assign 24 users; some convert, a few of them more than once.

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
        if i % 3 == 0:
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
def test_any_signed_in_user_reads_the_sequential_analysis(
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


def test_a_settings_change_reaches_the_cached_results_only_once_cleared(
    client, db_session, monkeypatch
):
    redis = fake_results_cache(monkeypatch)
    owner, experiment = _running(client, db_session)
    exp_url = f"/api/v1/experiments/{experiment['id']}"
    url = f"/api/v1/results/{experiment['id']}"

    assert _boundary(client.get(url, headers=auth(owner))) == pytest.approx(20.0)
    written = [
        ttl for key, ttl in redis.sets if key.startswith(f"results:{experiment['id']}:")
    ]
    assert written == [300], redis.sets

    accepted = client.put(
        exp_url,
        json={"sequential_testing_config": {"alpha": 0.1}},
        headers=auth(owner),
    )
    assert accepted.status_code == 200, accepted.text

    # /sequential is computed on every request; the results come from the cache.
    assert _boundary(client.get(f"{url}/sequential", headers=auth(owner))) == 10.0
    assert _boundary(client.get(url, headers=auth(owner))) == 20.0
    cleared = client.post(f"{url}/invalidate-cache", headers=auth(owner))
    assert cleared.status_code == 200, cleared.text
    assert _boundary(client.get(url, headers=auth(owner))) == pytest.approx(10.0)


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
