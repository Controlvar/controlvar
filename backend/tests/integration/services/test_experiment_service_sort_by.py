"""Both ``ExperimentService`` list methods sort only by a column (#786).

``get_experiments`` (the list route's) and ``get_experiments_by_owner`` (no
route calls it; only tests do) each did
``if sort_by and hasattr(Experiment, sort_by): getattr(Experiment, sort_by)``.
The route now refuses a name that is no column before it calls the service, so
only this test reaches the second method with one. Each method refuses such a
name itself with ``ValueError`` before it builds a query, rather than handing
it to ``getattr``: on main ``metadata`` raised ``AttributeError`` from
``MetaData.desc`` and ``foo`` was ignored. A column sorts as before, and an
absent or empty ``sort_by`` is still no sort.
"""

import uuid

import pytest

from backend.app.services.experiment_service import ExperimentService

pytestmark = pytest.mark.integration

NO_COLUMN = ("metadata", "to_dict", "variants", "__table__", "foo", "Name")


@pytest.fixture
def seeded(db_session, make_experiment, admin_user):
    """Three experiments of admin_user under a unique search term, written in
    an order that is not their name order."""
    marker = f"svc786{uuid.uuid4().hex[:10]}"
    for word in ("b", "c", "a"):
        make_experiment(name=f"{word} {marker}", description=marker)
    return marker, admin_user


def _names(rows):
    return [row["name"].split(" ", 1)[0] for row in rows]


@pytest.mark.regression
@pytest.mark.parametrize("sort_by", NO_COLUMN)
def test_786_get_experiments_by_owner_refuses_a_name_that_is_no_column(
    db_session, seeded, sort_by
):
    marker, owner = seeded
    with pytest.raises(ValueError, match="sort_by"):
        ExperimentService(db_session).get_experiments_by_owner(
            owner_id=owner.id, search=marker, sort_by=sort_by
        )


@pytest.mark.regression
@pytest.mark.parametrize("sort_by", NO_COLUMN)
def test_786_get_experiments_refuses_a_name_that_is_no_column(
    db_session, seeded, sort_by
):
    marker, _owner = seeded
    with pytest.raises(ValueError, match="sort_by"):
        ExperimentService(db_session).get_experiments(search=marker, sort_by=sort_by)


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_both_methods_still_sort_by_a_column(db_session, seeded, order):
    marker, owner = seeded
    service = ExperimentService(db_session)
    expected = ["a", "b", "c"] if order == "asc" else ["c", "b", "a"]
    assert (
        _names(service.get_experiments(search=marker, sort_by="name", sort_order=order))
        == expected
    )
    assert (
        _names(
            service.get_experiments_by_owner(
                owner_id=owner.id, search=marker, sort_by="name", sort_order=order
            )
        )
        == expected
    )


@pytest.mark.parametrize("sort_by", [None, ""])
def test_no_sort_by_is_still_no_sort(db_session, seeded, sort_by):
    marker, owner = seeded
    service = ExperimentService(db_session)
    assert sorted(_names(service.get_experiments(search=marker, sort_by=sort_by))) == [
        "a",
        "b",
        "c",
    ]
    assert sorted(
        _names(
            service.get_experiments_by_owner(
                owner_id=owner.id, search=marker, sort_by=sort_by
            )
        )
    ) == ["a", "b", "c"]
