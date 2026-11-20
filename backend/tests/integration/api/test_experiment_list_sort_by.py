"""``GET /api/v1/experiments/?sort_by=`` takes a column, in exact case (#786).

The route passed ``sort_by`` to ``hasattr(Experiment, ...)`` and ``getattr``.
Measured on main (PostgreSQL, the real route and the local JWT):

- ``metadata`` (SQLAlchemy's ``MetaData``), ``registry``, ``__table__``,
  ``__init__``, ``to_dict`` and the eight relationships answered 500: the
  attribute exists and is no column, so ``.desc()`` failed;
- ``foo``, ``Name``, ``NAME``, ``Created_At`` and a name with a space answered
  200 and were ignored: no attribute, so no sort and nothing said;
- every one of the 30 columns answered 200 and sorted, the eight JSON ones
  included (PostgreSQL orders ``jsonb``).

Now any non-empty value that is not one of the 30 columns answers 422 at
``["query", "sort_by"]`` with type ``enum`` and a message that names the 30 and
never the value sent, checked after the LIST permission and before the route's
``try`` (inside it, ``except Exception`` would turn the 422 into a 500). Every
column still sorts, in both directions, and an empty ``sort_by`` is still no
sort. The four values the dashboard sends (``frontend/src/services/experiments.ts``)
answer byte for byte what main answered: ``golden/experiment_list_sort_by.json``
was captured on main from the rows ``_golden_rows`` writes. The fields added
to the answer since, #448's ``balance_within`` and #1545's ``visual_config`` and
``design_etag`` (null for those rows), are inserted where the response schema
puts them; every other byte is main's.

Each test reads only its own rows, by a ``search`` term that only they carry,
because the test database is shared by the session.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import String, text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token
from backend.app.main import app
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    ExperimentType,
)
from backend.app.models.user import User

pytestmark = pytest.mark.integration

SCHEMA = "test_experimentation"
LIST = "/api/v1/experiments/"

#: The 30 columns, written out; the unit test pins this against the model.
COLUMNS = (
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

#: The whole 422 body, written out: one error, no ``input``, and a message
#: that is the same whatever was sent, so the value is repeated nowhere.
REFUSED = {
    "detail": [
        {
            "type": "enum",
            "loc": ["query", "sort_by"],
            "msg": "sort_by must be one of: " + ", ".join(COLUMNS),
        }
    ]
}

#: Names that are no column. On main the attributes answered 500 and the rest
#: 200, ignored. The relationships are all eight of ``Experiment``'s.
RELATIONSHIPS = (
    "assignments",
    "bandit_state",
    "events",
    "metric_definitions",
    "mutual_exclusion_group",
    "owner",
    "reports",
    "variants",
)
ATTRIBUTES = (
    "metadata",
    "registry",
    "__table__",
    "__tablename__",
    "__init__",
    "__class__",
    "_sa_class_manager",
    "to_dict",
)
NOT_ATTRIBUTES = (
    "foo",
    "Name",
    "NAME",
    "Created_At",
    "created_at ",
    " name",
    "name,status",
)

#: The values the dashboard's client may send (ExperimentListParams.sort_by).
DASHBOARD_SORTS = ("created_at", "updated_at", "name", "status")

GOLDEN = Path(__file__).parent / "golden" / "experiment_list_sort_by.json"
GOLDEN_SEARCH = "gold786sortby"


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")


@pytest.fixture
def client(db_session):
    engine = db_session.get_bind()
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_get_db():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


def _list(client: TestClient, user: User, params: Dict[str, str]):
    headers = {"Authorization": f"Bearer {create_local_access_token(user)}"}
    return client.get(LIST, params=params, headers=headers)


def _ids(resp) -> List[str]:
    assert resp.status_code == 200, resp.text
    return [item["id"] for item in resp.json()["items"]]


def _assert_refused(resp, sent: str) -> None:
    assert resp.status_code == 422, f"{sent!r}: expected 422, got {resp.status_code}"
    assert resp.json() == REFUSED


# ---------------------------------------------------------------------------
# The regression: a name that is no column
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_786_sort_by_metadata_answers_422_not_500(client, viewer_user):
    """The issue's request, as the least role that may list."""
    resp = _list(client, viewer_user, {"sort_by": "metadata"})
    _assert_refused(resp, "metadata")


@pytest.mark.regression
@pytest.mark.parametrize("sent", ATTRIBUTES + RELATIONSHIPS + NOT_ATTRIBUTES)
def test_786_a_name_that_is_no_column_answers_422(client, viewer_user, sent):
    resp = _list(client, viewer_user, {"sort_by": sent})
    _assert_refused(resp, sent)
    assert "(any case)" not in resp.text


#: Columns that are not sort keys (#448): the beta ``balance_within``, and
#: ``balance_seed``, which is never answered, so never an order either.
NOT_SORT_KEYS = ("balance_within", "balance_seed")


@pytest.mark.regression
@pytest.mark.parametrize("sent", NOT_SORT_KEYS)
def test_448_the_balance_columns_are_not_sort_keys(client, viewer_user, sent):
    resp = _list(client, viewer_user, {"sort_by": sent})
    _assert_refused(resp, sent)


@pytest.mark.regression
def test_786_the_refusal_never_repeats_the_value_sent(client, viewer_user):
    sentinel = "zz786sentinel"
    resp = _list(client, viewer_user, {"sort_by": sentinel})
    _assert_refused(resp, sentinel)
    assert sentinel not in resp.text


def test_a_caller_who_may_not_list_is_refused_before_sort_by_is_read(
    client, viewer_user
):
    """403 first: a caller who may not list learns nothing about the columns."""
    with patch(
        "backend.app.api.v1.endpoints.experiments.check_permission",
        return_value=False,
    ):
        resp = _list(client, viewer_user, {"sort_by": "metadata"})
    assert resp.status_code == 403, resp.text
    assert "sort_by" not in resp.text


# ---------------------------------------------------------------------------
# Every column sorts, in both directions
# ---------------------------------------------------------------------------


def _referenced_rows(db_session, column: str, ids: List[uuid.UUID], marker: str):
    """A row for each of *ids* in the table *column* references, if it
    references one: ``owner_id`` the users, ``mutual_exclusion_group_id`` the
    groups, and ``workspace_id`` a table only when the module that owns it is
    loaded (it attaches the foreign key itself). Each NOT NULL column with no
    default gets a unique text; any other kind fails here, by name."""
    for fk in Experiment.__table__.c[column].foreign_keys:
        table = fk.column.table
        for n, row_id in enumerate(ids):
            values: Dict[str, Any] = {fk.column.key: row_id}
            for c in table.columns:
                if c.key in values or c.nullable or c.primary_key:
                    continue
                if c.default is not None or c.server_default is not None:
                    continue
                assert isinstance(c.type, String), f"{table.name}.{c.key}: no value"
                values[c.key] = f"s786-{marker}-{n}"[: c.type.length or 100]
            db_session.execute(table.insert().values(**values))
    db_session.commit()


def _ascending(db_session, column: str, marker: str) -> List[Any]:
    """Two or three values for *column*, in the order PostgreSQL sorts them."""
    base = datetime(2026, 3, 1, 12, 0, 0)
    times = [base, base + timedelta(hours=1), base + timedelta(days=1)]
    words = ["a", "b", "c"]
    objects = [{"k": 1}, {"k": 2}, {"k": 3}]
    if column in ("id", "owner_id", "mutual_exclusion_group_id", "workspace_id"):
        # PostgreSQL compares uuids byte by byte, as Python's UUID does.
        ids = sorted(uuid.uuid4() for _ in range(3))
        _referenced_rows(db_session, column, ids, marker)
        return ids
    if column in ("created_at", "updated_at", "start_date", "end_date"):
        return times
    if column == "resume_at":
        return [t.replace(tzinfo=timezone.utc) for t in times]
    if column in ("name", "description"):
        return [f"{w} {marker}" for w in words]
    if column == "key":
        return [f"{w}-{marker}" for w in words]
    if column == "hypothesis":
        return words
    if column == "status":
        # A PostgreSQL enum sorts in its declared order.
        return [
            ExperimentStatus.DRAFT,
            ExperimentStatus.ACTIVE,
            ExperimentStatus.COMPLETED,
        ]
    if column == "experiment_type":
        return [
            ExperimentType.A_B,
            ExperimentType.MULTIVARIATE,
            ExperimentType.SPLIT_URL,
        ]
    if column == "optimization_type":
        return ["fixed", "thompson_sampling", "ucb1"]
    if column == "sequential_testing_method":
        return ["msprt", "always_valid"]
    if column in ("sequential_testing_enabled", "bayesian_enabled"):
        return [False, True]
    if column == "tags":
        return [["a"], ["m"], ["z"]]
    if column == "metrics":
        return [[1], [2], [3]]
    if column in (
        "targeting_rules",
        "experiment_metadata",
        "sequential_testing_config",
        "variance_reduction_config",
        "bayesian_config",
        "split_url_config",
    ):
        return objects
    if column == "correction_method":
        return ["benjamini_hochberg", "bonferroni", "none"]
    if column == "confidence_level":
        return [0.8, 0.9, 0.99]
    if column == "bayesian_decision":
        return ["CONTINUE", "STOP_FUTILE", "STOP_WINNER"]
    raise AssertionError(f"no values for {column}")


@pytest.fixture
def rows_for(db_session):
    """Rows whose *column* differs, written out of order; returns the search
    term and their ids in ascending order of the column."""

    def _rows(column: str):
        marker = f"sort786{uuid.uuid4().hex[:10]}"
        values = _ascending(db_session, column, marker)
        n = len(values)
        experiments = []
        for i in range(n):
            # Row i takes the value after its own, so neither the insert order
            # nor its reverse is the column's order.
            fields: Dict[str, Any] = {
                "name": f"row {i} {marker}",
                "description": f"row {i} {marker}",
                "status": ExperimentStatus.DRAFT,
            }
            if column == "resume_at":
                fields["status"] = ExperimentStatus.PAUSED
            fields[column] = values[(i + 1) % n]
            experiment = Experiment(**fields)
            db_session.add(experiment)
            experiments.append(experiment)
        db_session.commit()
        ascending = [str(experiments[(k - 1) % n].id) for k in range(n)]
        return marker, ascending

    return _rows


@pytest.mark.parametrize("column", COLUMNS)
def test_every_column_sorts_both_ways(client, viewer_user, rows_for, column):
    marker, ascending = rows_for(column)
    asc = _list(
        client, viewer_user, {"search": marker, "sort_by": column, "sort_order": "asc"}
    )
    desc = _list(
        client,
        viewer_user,
        {"search": marker, "sort_by": column, "sort_order": "desc"},
    )
    for resp in (asc, desc):
        assert resp.status_code == 200, (
            f"{column}: expected 200, got {resp.status_code}"
        )
    assert _ids(asc) == ascending, f"{column} asc"
    assert _ids(desc) == ascending[::-1], f"{column} desc"


def test_an_empty_sort_by_is_still_no_sort(client, viewer_user, rows_for):
    marker, ascending = rows_for("name")
    resp = _list(client, viewer_user, {"search": marker, "sort_by": ""})
    assert sorted(_ids(resp)) == sorted(ascending)
    assert resp.json()["total"] == len(ascending)


# ---------------------------------------------------------------------------
# The dashboard's sorts answer exactly what main answered
# ---------------------------------------------------------------------------

#: Four rows whose created_at, updated_at, name and status orders all differ.
GOLDEN_ROWS = (
    ("786a0001-0000-4000-8000-000000000001", "Checkout copy", "ACTIVE", 1, 4),
    ("786a0002-0000-4000-8000-000000000002", "Banner colour", "COMPLETED", 2, 1),
    ("786a0003-0000-4000-8000-000000000003", "Pricing page", "DRAFT", 3, 3),
    ("786a0004-0000-4000-8000-000000000004", "Annual plan", "PAUSED", 4, 2),
)


def _golden_rows(db_session) -> None:
    """The golden rows, written once per session (every value fixed)."""
    for row_id, name, status, created_day, updated_day in GOLDEN_ROWS:
        if db_session.get(Experiment, uuid.UUID(row_id)) is not None:
            continue
        db_session.add(
            Experiment(
                id=uuid.UUID(row_id),
                name=name,
                key=f"{GOLDEN_SEARCH}-{name.lower().replace(' ', '-')}",
                description=f"{GOLDEN_SEARCH}: the dashboard's sorts (#786)",
                hypothesis="Each sort answers as it did on main",
                status=ExperimentStatus[status],
                created_at=datetime(2026, 1, created_day, 9, 0, 0),
                updated_at=datetime(2026, 2, updated_day, 9, 0, 0),
            )
        )
    db_session.commit()


#: The fields added to every experiment of the answer since the golden file
#: was captured: #448's ``balance_within``, and #1545's ``visual_config`` and
#: ``design_etag``, all null for these rows (they balance nothing and are not
#: visual), which ``ExperimentResponse`` declares in that order right after
#: ``split_url_config``. The golden file stays main's bytes.
ADDED_AFTER = '"split_url_config":null,'
ADDED_FIELD = '"balance_within":null,"visual_config":null,"design_etag":null,'


def _main_answer_with_the_added_field(main_answer: str) -> str:
    """Main's answer with :data:`ADDED_FIELD` inserted once per experiment,
    where the response schema puts it; every other byte is main's."""
    assert ADDED_FIELD not in main_answer
    assert main_answer.count(ADDED_AFTER) == len(GOLDEN_ROWS)
    return main_answer.replace(ADDED_AFTER, ADDED_AFTER + ADDED_FIELD)


def _golden_requests() -> Dict[str, Dict[str, str]]:
    """Each request the golden file holds, by a readable name."""
    requests = {"default": {"search": GOLDEN_SEARCH}}
    for sort_by in DASHBOARD_SORTS:
        for order in ("asc", "desc"):
            requests[f"{sort_by} {order}"] = {
                "search": GOLDEN_SEARCH,
                "sort_by": sort_by,
                "sort_order": order,
            }
    return requests


@pytest.mark.parametrize("name", sorted(_golden_requests()))
def test_the_dashboards_sorts_answer_byte_for_byte_as_on_main(
    client, db_session, viewer_user, name
):
    _golden_rows(db_session)
    golden = json.loads(GOLDEN.read_text())
    assert sorted(golden) == sorted(_golden_requests())
    resp = _list(client, viewer_user, _golden_requests()[name])
    assert resp.status_code == 200, f"{name}: expected 200, got {resp.status_code}"
    assert resp.text == _main_answer_with_the_added_field(golden[name])
