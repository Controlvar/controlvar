"""A split URL variant's traffic_allocation is from 0 to 100, over HTTP (#1194).

The schema is ``backend/tests/unit/schemas/test_split_url_allocation_range.py``.
These tests send real requests and read what was stored:

* create and update refuse an allocation outside 0 to 100 with 422, naming it
  at ``["body", "split_url_config", "variants", <i>, "traffic_allocation"]``,
  and write nothing;
* a configuration stored BEFORE the range was checked is still read as it was
  stored, and a PUT that does not mention ``split_url_config`` still works and
  leaves it byte for byte unchanged. Only a PUT that sends it back answers 422
  (the rule for every stored value a schema later refuses): the client must
  correct it, not resubmit it;
* the split URL preview of such a stored configuration answers 409, with a
  fixed ``detail`` and none of the stored values. Before the range was
  checked it answered 200 and served the stored allocations as they were
  (``traffic_allocation`` 150.0), and a create of -50 and 150 answered 201.
"""

from __future__ import annotations

import copy
import uuid

import pytest
from sqlalchemy import Text, cast

from backend.app.models.experiment import Experiment, ExperimentStatus

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

EXPERIMENTS = "/api/v1/experiments"


def _split_url(*allocations: float) -> dict:
    return {
        "variants": [
            {
                "name": f"variant {i}",
                "url": f"https://example.com/{i}",
                "traffic_allocation": allocation,
            }
            for i, allocation in enumerate(allocations)
        ],
        "cookie_ttl_days": 30,
    }


#: Stored before the range was checked: the sum is right, the values are not.
STORED_OUT_OF_RANGE = _split_url(-50, 150)

#: (id, allocations, the indexes of the allocations the 422 names)
REFUSED = [
    ("minus-50-and-150", (-50, 150), [0, 1]),
    ("just-under-0-and-just-over-100", (-0.01, 100.01), [0, 1]),
    ("the-last-of-three", (60, 50, -10), [2]),
]


def _create_body(name: str, **extra) -> dict:
    return {
        "name": name,
        "experiment_type": "a_b",
        "variants": [
            {"name": "Control", "is_control": True, "traffic_allocation": 50},
            {"name": "Treatment", "is_control": False, "traffic_allocation": 50},
        ],
        "metrics": [
            {
                "name": "Conversion",
                "event_name": "purchase",
                "metric_type": "conversion",
                "is_primary": True,
            }
        ],
    } | extra


def _name() -> str:
    return f"split url range {uuid.uuid4().hex[:8]}"


def _locs(response) -> list:
    detail = response.json()["detail"]
    assert isinstance(detail, list), detail
    return [e["loc"] for e in detail]


def _loc(i: int) -> list:
    return ["body", "split_url_config", "variants", i, "traffic_allocation"]


def _stored_text(db_session, experiment_id) -> str:
    """The stored JSONB as the database prints it."""
    db_session.expire_all()
    return (
        db_session.query(cast(Experiment.split_url_config, Text))
        .filter(Experiment.id == experiment_id)
        .scalar()
    )


def _stored_experiment(make_experiment, **extra):
    """An experiment whose stored configuration is out of range, as one saved
    before the check would be: written directly, never through the API."""
    return make_experiment(
        name=_name(), split_url_config=copy.deepcopy(STORED_OUT_OF_RANGE), **extra
    )


# ---------------------------------------------------------------------------
# Create and update refuse it
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "allocations, indexes", [r[1:] for r in REFUSED], ids=[r[0] for r in REFUSED]
)
def test_create_with_an_allocation_outside_0_to_100_answers_422(
    admin_client, db_session, allocations, indexes
):
    name = _name()
    body = _create_body(
        name, experiment_type="split_url", split_url_config=_split_url(*allocations)
    )

    response = admin_client.post(f"{EXPERIMENTS}/", json=body)

    assert response.status_code == 422, response.text
    assert sorted(_locs(response)) == sorted(_loc(i) for i in indexes)
    assert db_session.query(Experiment).filter_by(name=name).count() == 0


@pytest.mark.regression
@pytest.mark.parametrize(
    "allocations, indexes", [r[1:] for r in REFUSED], ids=[r[0] for r in REFUSED]
)
def test_update_with_an_allocation_outside_0_to_100_answers_422_and_writes_nothing(
    admin_client, db_session, make_experiment, allocations, indexes
):
    experiment = make_experiment(name=_name(), split_url_config=_split_url(60, 40))
    before = _stored_text(db_session, experiment.id)

    response = admin_client.put(
        f"{EXPERIMENTS}/{experiment.id}",
        json={"split_url_config": _split_url(*allocations)},
    )

    assert response.status_code == 422, response.text
    assert sorted(_locs(response)) == sorted(_loc(i) for i in indexes)
    assert _stored_text(db_session, experiment.id) == before


@pytest.mark.parametrize(
    "allocations", [(0, 100), (100, 0), (0.0, 100.0)], ids=["0-100", "100-0", "floats"]
)
def test_the_limits_themselves_are_accepted_on_create_and_update(
    admin_client, db_session, make_experiment, allocations
):
    # An a_b experiment may carry a configuration, so this holds in the core
    # profile too, where creating a split_url experiment answers 501.
    created = admin_client.post(
        f"{EXPERIMENTS}/",
        json=_create_body(_name(), split_url_config=_split_url(*allocations)),
    )
    assert created.status_code == 201, created.text
    assert [
        v["traffic_allocation"] for v in created.json()["split_url_config"]["variants"]
    ] == list(allocations)

    experiment = make_experiment(name=_name())
    updated = admin_client.put(
        f"{EXPERIMENTS}/{experiment.id}",
        json={"split_url_config": _split_url(*allocations)},
    )
    assert updated.status_code == 200, updated.text
    assert [
        v["traffic_allocation"] for v in updated.json()["split_url_config"]["variants"]
    ] == list(allocations)


# ---------------------------------------------------------------------------
# D41: a stored value the schema now refuses
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "status", [ExperimentStatus.DRAFT, ExperimentStatus.ACTIVE], ids=["draft", "active"]
)
def test_a_put_resending_the_stored_out_of_range_config_answers_422(
    admin_client, db_session, make_experiment, status
):
    experiment = _stored_experiment(make_experiment, status=status)
    before = _stored_text(db_session, experiment.id)

    response = admin_client.put(
        f"{EXPERIMENTS}/{experiment.id}",
        json={"split_url_config": copy.deepcopy(STORED_OUT_OF_RANGE)},
    )

    assert response.status_code == 422, response.text
    assert sorted(_locs(response)) == [_loc(0), _loc(1)]
    assert _stored_text(db_session, experiment.id) == before


def test_a_put_that_omits_split_url_config_works_and_leaves_it_unchanged(
    admin_client, db_session, make_experiment
):
    experiment = _stored_experiment(make_experiment)
    before = _stored_text(db_session, experiment.id)
    assert before is not None

    response = admin_client.put(
        f"{EXPERIMENTS}/{experiment.id}", json={"description": "edited"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["description"] == "edited"
    assert _stored_text(db_session, experiment.id) == before


def test_a_put_can_correct_the_stored_config(admin_client, db_session, make_experiment):
    experiment = _stored_experiment(make_experiment)

    response = admin_client.put(
        f"{EXPERIMENTS}/{experiment.id}", json={"split_url_config": _split_url(50, 50)}
    )

    assert response.status_code == 200, response.text
    db_session.expire_all()
    stored = db_session.get(Experiment, experiment.id).split_url_config
    assert [v["traffic_allocation"] for v in stored["variants"]] == [50, 50]


# ---------------------------------------------------------------------------
# The preview of a stored config that is out of range
# ---------------------------------------------------------------------------

DETAIL = "split_url_config is not valid; update it with PUT /api/v1/experiments/{id}"


@pytest.mark.modules
@pytest.mark.regression
def test_the_preview_of_a_stored_out_of_range_config_answers_409(
    admin_client, db_session
):
    created = admin_client.post(
        f"{EXPERIMENTS}/",
        json=_create_body(
            _name(), experiment_type="split_url", split_url_config=_split_url(50, 50)
        ),
    )
    assert created.status_code == 201, created.text
    experiment_id = created.json()["id"]
    previewed = admin_client.get(
        f"{EXPERIMENTS}/{experiment_id}/split-url/preview", params={"user_id": "u1"}
    )
    assert previewed.status_code == 200, previewed.text

    # Saved before the range was checked.
    db_session.query(Experiment).filter(
        Experiment.id == uuid.UUID(experiment_id)
    ).update({"split_url_config": copy.deepcopy(STORED_OUT_OF_RANGE)})
    db_session.commit()

    response = admin_client.get(
        f"{EXPERIMENTS}/{experiment_id}/split-url/preview", params={"user_id": "u1"}
    )

    assert response.status_code == 409, response.text
    assert response.json() == {"detail": DETAIL}
    for stored_value in ("-50", "150", "example.com"):
        assert stored_value not in response.text
