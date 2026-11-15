"""``sort_by`` on ``GET /api/v1/experiments/`` is 30 table columns (#786).

The list passed ``sort_by`` to ``hasattr(Experiment, ...)`` and ``getattr``, so
any attribute of the class was a sort key: ``metadata`` (SQLAlchemy's
``MetaData``), ``registry``, ``to_dict``, a dunder or a relationship answered
500, and a name that is no attribute (``foo``, ``Name``) answered 200 and was
ignored. The allow-list is now the columns of the experiments table, in exact
case, the JSON columns included, except the two #448 added and the one #1545
added, which are not sort keys (``NOT_SORT_KEYS``).

These tests need no database. They pin the list three ways, so none of them can
move alone:

- ``EXPECTED`` below, written out, with ``NOT_SORT_KEYS``, equals
  ``Experiment.__table__.columns`` at run time: a column added, dropped or
  renamed fails here, with the names, until it is listed in one of the two;
- the service's ``SORT_BY_COLUMNS`` equals ``EXPECTED``, in order;
- the route's 422 message and the served OpenAPI description name exactly those
  columns, and the parameter is still a plain string (no ``enum``).

The behaviour through the route, on PostgreSQL, is
``backend/tests/integration/api/test_experiment_list_sort_by.py``'s; the two
service methods' is ``backend/tests/integration/services/test_experiment_service_sort_by.py``'s.
"""

import pytest
from sqlalchemy.dialects.postgresql import JSONB

from backend.app.models.experiment import Experiment

pytestmark = pytest.mark.unit

#: The 30 sort keys, written out (PLAN v1.2 D3): the 27 columns
#: models/experiment.py had before #448 and the 3 of models/base.py's BaseModel.
EXPECTED = (
    "id",
    "created_at",
    "updated_at",
    "name",
    "key",
    "description",
    "hypothesis",
    "status",
    "experiment_type",
    "owner_id",
    "start_date",
    "end_date",
    "resume_at",
    "targeting_rules",
    "metrics",
    "tags",
    "experiment_metadata",
    "optimization_type",
    "sequential_testing_enabled",
    "sequential_testing_method",
    "sequential_testing_config",
    "variance_reduction_config",
    "correction_method",
    "confidence_level",
    "mutual_exclusion_group_id",
    "bayesian_enabled",
    "bayesian_config",
    "bayesian_decision",
    "split_url_config",
    "workspace_id",
)

#: The eight JSON columns among them: each sorts, as PostgreSQL orders jsonb.
EXPECTED_JSONB = {
    "targeting_rules",
    "metrics",
    "tags",
    "experiment_metadata",
    "sequential_testing_config",
    "variance_reduction_config",
    "bayesian_config",
    "split_url_config",
}

#: The columns that are not sort keys, and why: each answers 422 like any
#: other name that is not in ``EXPECTED``.
NOT_SORT_KEYS = {
    # Beta (#448): not part of the stable sort_by list while it is beta.
    "balance_within": "beta",
    # The block-order key: server-side only, never answered, so never an
    # order of experiments either.
    "balance_seed": "server-side only",
    # #1545: a visual experiment's pages and goals, null on every other
    # experiment; sorting by it would change the stable list and its 422.
    "visual_config": "not an order of experiments",
}

#: The 422's message, written out from the list above.
EXPECTED_MESSAGE = "sort_by must be one of: " + ", ".join(EXPECTED)


def test_the_written_list_is_exactly_the_experiment_tables_columns():
    columns = list(Experiment.__table__.columns.keys())
    assert len(EXPECTED) == len(set(EXPECTED)) == 30
    assert not set(EXPECTED) & set(NOT_SORT_KEYS)
    listed = set(EXPECTED) | set(NOT_SORT_KEYS)
    assert set(columns) == listed, (
        f"not listed: {sorted(set(columns) - listed)}; "
        f"not a column: {sorted(listed - set(columns))}"
    )
    assert len(columns) == 33
    jsonb = {c.key for c in Experiment.__table__.columns if isinstance(c.type, JSONB)}
    assert jsonb == EXPECTED_JSONB | {"balance_within", "visual_config"}


@pytest.mark.regression
def test_786_sort_by_accepts_exactly_the_tables_columns():
    from backend.app.services.experiment_service import SORT_BY_COLUMNS

    assert SORT_BY_COLUMNS == EXPECTED


@pytest.mark.regression
def test_786_the_refusal_names_every_column_in_exact_case():
    from backend.app.api.v1.endpoints.experiments import SORT_BY_MESSAGE

    assert SORT_BY_MESSAGE == EXPECTED_MESSAGE
    assert "(any case)" not in SORT_BY_MESSAGE


@pytest.fixture(scope="module")
def sort_by_parameter():
    from backend.app.main import app

    operation = app.openapi()["paths"]["/api/v1/experiments/"]["get"]
    (parameter,) = [
        p
        for p in operation["parameters"]
        if p["in"] == "query" and p["name"] == "sort_by"
    ]
    return parameter


@pytest.mark.regression
def test_786_the_parameter_stays_a_string_whose_description_lists_the_columns(
    sort_by_parameter,
):
    description = (
        "Column to sort by, in exact case: "
        + ", ".join(EXPECTED)
        + ". An empty value applies no sort; any other value answers 422."
    )
    assert sort_by_parameter == {
        "description": description,
        "in": "query",
        "name": "sort_by",
        "required": False,
        "schema": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "default": "created_at",
            "description": description,
            "title": "Sort By",
        },
    }
