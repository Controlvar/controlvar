"""A metric's conversion window is written, read back, cloned and audited (#1934).

``conversion_window_hours`` on an experiment's metric is a whole number of hours
from 1 to ``MAX_CONVERSION_WINDOW_HOURS`` (8760), or null for none. Create and a
draft's PUT take it (``MetricBase``), and refuse anything else with Pydantic's
own 422 at the metric's field. Every place that copies a metric row by naming
its columns carries it: the row create and PUT write, the response
(``MetricResponse``) and the service's dict that GET, list, create, PUT and
clone answer with, the clone, the audit entry's comparison and the design etag.

Each of those lists is held here against ``Metric.__table__.columns``, so a
column added to the table and left out of one fails with ``missing [...]``
naming it, rather than being dropped in silence. Everything here is DB-free;
the same through the API, on PostgreSQL, is
``backend/tests/integration/api/test_metric_window_writes.py``.
"""

from __future__ import annotations

import fnmatch
import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from backend.app.core.stats_engine import ENGINE_VERSION
from backend.app.models import experiment as experiment_models
from backend.app.models.audit_log import EntityType
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    ExperimentType,
    Metric,
    MetricType,
)
from backend.app.schemas.experiment import (
    ExperimentCreate,
    ExperimentResponse,
    ExperimentUpdate,
    MetricBase,
    MetricResponse,
)
from backend.app.services import visual
from backend.app.services.audit_service import (
    AUDIT_NAME_ONLY_ROWS,
    audit_changes,
    audit_snapshot,
)
from backend.app.services.experiment_service import ExperimentService

pytestmark = pytest.mark.unit

FIELD = "conversion_window_hours"
#: A metric row's columns that are not part of its definition: a save re-creates
#: them, or they are the experiment's.
NOT_A_DEFINITION = {"id", "experiment_id", "created_at", "updated_at"}
COLUMNS = sorted(
    c.name for c in Metric.__table__.columns if c.name not in NOT_A_DEFINITION
)


def _definition() -> Dict[str, Any]:
    """A value for every definition column, none of them its default."""
    return {
        "name": "Revenue in a week",
        "description": "what each user spends in their first week",
        "event_name": "purchase",
        "metric_type": "revenue",
        "is_primary": True,
        "aggregation_method": "sum",
        "minimum_sample_size": 250,
        "expected_effect": 0.07,
        "event_value_path": "value",
        "lower_is_better": True,
        FIELD: 168,
    }


def _plain(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _missing(found: Dict[str, Any]) -> List[str]:
    """The columns whose value did not arrive in *found* as sent."""
    sent = _definition()
    return [c for c in COLUMNS if c not in found or _plain(found[c]) != sent[c]]


def _row(**values) -> Metric:
    definition = {**_definition(), **values}
    definition["metric_type"] = MetricType(definition["metric_type"])
    now = datetime.now(timezone.utc)
    return Metric(
        id=uuid.uuid4(),
        experiment_id=uuid.uuid4(),
        created_at=now,
        updated_at=now,
        **definition,
    )


def _variants() -> List[Dict[str, Any]]:
    return [
        {"name": "Control", "is_control": True, "traffic_allocation": 50},
        {"name": "Treatment", "is_control": False, "traffic_allocation": 50},
    ]


def _create_body(*metrics: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": "Checkout window",
        "experiment_type": "a_b",
        "variants": _variants(),
        "metrics": list(metrics),
    }


def _plain_metric(**fields) -> Dict[str, Any]:
    return {
        "name": "Signup",
        "event_name": "signup",
        "metric_type": "conversion",
        **fields,
    }


def _added_metrics(db: MagicMock) -> List[Metric]:
    return [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], Metric)]


def _source() -> Experiment:
    experiment = Experiment(
        id=uuid.uuid4(),
        name="Checkout window",
        experiment_type=ExperimentType.A_B,
        status=ExperimentStatus.COMPLETED,
        owner_id=uuid.uuid4(),
    )
    experiment.variants = []
    experiment.metric_definitions = [_row()]
    return experiment


# --- the bound ------------------------------------------------------------------


def test_the_bound_is_the_core_model_s():
    """The schema reads the bound the table's check is written with, from the
    core model, so a tree without ``modules/`` has it."""
    assert experiment_models.MAX_CONVERSION_WINDOW_HOURS == 8760
    bounds = {
        type(m).__name__: getattr(m, "ge", getattr(m, "le", None))
        for m in MetricBase.model_fields[FIELD].metadata
    }
    assert bounds == {"Ge": 1, "Le": experiment_models.MAX_CONVERSION_WINDOW_HOURS}


def test_the_description_says_beta_first_and_names_the_rule():
    for schema in (MetricBase, MetricResponse):
        description = schema.model_fields[FIELD].description
        assert description.startswith("Beta.  The metric's conversion window, in hours")
        for words in (
            "from 1 to 8760",
            "elapsed hours from their assignment",
            "up to and not including",
            "null, or left out, means no window",
        ):
            assert words in description, (schema.__name__, words)


# --- what create and PUT refuse and take -------------------------------------------

#: (value sent, Pydantic's type, its message, its ctx)
REFUSED = [
    (0, "greater_than_equal", "Input should be greater than or equal to 1", {"ge": 1}),
    (-1, "greater_than_equal", "Input should be greater than or equal to 1", {"ge": 1}),
    (
        8761,
        "less_than_equal",
        "Input should be less than or equal to 8760",
        {"le": 8760},
    ),
    (1.5, "int_type", "Input should be a valid integer", None),
    (True, "int_type", "Input should be a valid integer", None),
    ("24", "int_type", "Input should be a valid integer", None),
    ("168", "int_type", "Input should be a valid integer", None),
    (24.0, "int_type", "Input should be a valid integer", None),
]


def _body(schema, value) -> Dict[str, Any]:
    metrics = [
        _plain_metric(is_primary=True),
        _plain_metric(name="Purchase", event_name="purchase", **{FIELD: value}),
    ]
    if schema is ExperimentCreate:
        return _create_body(*metrics)
    return {"metrics": metrics}


@pytest.mark.parametrize("schema", [ExperimentCreate, ExperimentUpdate])
@pytest.mark.parametrize(
    "value, error_type, message, ctx", REFUSED, ids=[repr(r[0]) for r in REFUSED]
)
def test_create_and_put_refuse_anything_but_a_whole_number_of_hours(
    schema, value, error_type, message, ctx
):
    with pytest.raises(ValidationError) as raised:
        schema.model_validate(_body(schema, value))
    (error,) = raised.value.errors(include_url=False, include_input=False)
    expected = {"type": error_type, "loc": ("metrics", 1, FIELD), "msg": message}
    if ctx is not None:
        expected["ctx"] = ctx
    assert error == expected


@pytest.mark.parametrize("schema", [ExperimentCreate, ExperimentUpdate])
@pytest.mark.parametrize("value", [1, 168, 8760, None])
def test_create_and_put_take_1_to_8760_hours_or_null(schema, value):
    parsed = schema.model_validate(_body(schema, value))
    assert parsed.metrics[1].conversion_window_hours == value
    assert parsed.metrics[0].conversion_window_hours is None


# --- the six lists, against the table's columns -----------------------------------


def test_the_definition_covers_every_column():
    """A new column fails here first: give it a value in ``_definition``,
    then each list below says whether it carries it."""
    assert sorted(_definition()) == COLUMNS
    assert FIELD in COLUMNS


def test_create_writes_every_column():
    db = MagicMock()
    service = ExperimentService(db)
    body = ExperimentCreate.model_validate(_create_body(_definition()))
    with patch.object(service, "_experiment_to_dict", return_value={}):
        service.create_experiment(body, uuid.uuid4())
    (row,) = _added_metrics(db)
    found = {c: getattr(row, c) for c in COLUMNS}
    assert _missing(found) == [], f"missing {_missing(found)}"


def test_put_writes_every_column():
    db = MagicMock()
    service = ExperimentService(db)
    experiment = _source()
    experiment.status = ExperimentStatus.DRAFT
    experiment.metric_definitions = []
    body = ExperimentUpdate.model_validate({"metrics": [_definition()]})
    with patch.object(service, "_experiment_to_dict", return_value={}):
        service.update_experiment(experiment, body)
    (row,) = _added_metrics(db)
    found = {c: getattr(row, c) for c in COLUMNS}
    assert _missing(found) == [], f"missing {_missing(found)}"


def test_a_put_that_leaves_the_window_out_writes_none():
    """A PUT replaces the metrics: one sent without a window has none, as one
    sent without ``lower_is_better`` is not lower-is-better."""
    db = MagicMock()
    service = ExperimentService(db)
    experiment = _source()
    experiment.status = ExperimentStatus.DRAFT
    without = {k: v for k, v in _definition().items() if k != FIELD}
    body = ExperimentUpdate.model_validate({"metrics": [without]})
    with patch.object(service, "_experiment_to_dict", return_value={}):
        service.update_experiment(experiment, body)
    (row,) = _added_metrics(db)
    assert row.conversion_window_hours is None
    db.delete.assert_called_once_with(experiment.metric_definitions[0])


def test_the_response_and_the_service_dict_carry_every_column():
    """GET and list answer ``MetricResponse``; create, PUT, clone and the
    detail read build it from the service's dict."""
    row = _row()
    from_row = MetricResponse.model_validate(row).model_dump()
    assert _missing(from_row) == [], f"missing {_missing(from_row)}"

    experiment = _source()
    experiment.metric_definitions = [row]
    (as_dict,) = ExperimentService(MagicMock())._experiment_to_dict(experiment)[
        "metrics"
    ]
    assert _missing(as_dict) == [], f"missing {_missing(as_dict)}"
    from_dict = MetricResponse.model_validate(as_dict).model_dump()
    assert _missing(from_dict) == [], f"missing {_missing(from_dict)}"
    assert "metrics" in ExperimentResponse.model_fields


def test_a_clone_copies_every_column():
    db = MagicMock()
    service = ExperimentService(db)
    with patch.object(service, "_experiment_to_dict", return_value={}):
        service.clone_experiment(_source(), uuid.uuid4())
    (row,) = _added_metrics(db)
    found = {c: getattr(row, c) for c in COLUMNS}
    assert _missing(found) == [], f"missing {_missing(found)}"


def test_the_audit_compares_every_column():
    _, columns = AUDIT_NAME_ONLY_ROWS[EntityType.EXPERIMENT]["metrics"]
    missing = [c for c in COLUMNS if c not in columns]
    assert missing == [], f"missing {missing}"


def test_a_save_that_changes_only_a_window_names_the_metrics():
    def experiment(hours):
        return SimpleNamespace(
            name="exp",
            variants=[],
            metric_definitions=[SimpleNamespace(**{**_definition(), FIELD: hours})],
        )

    before = audit_snapshot(EntityType.EXPERIMENT, experiment(168))
    same = audit_snapshot(EntityType.EXPERIMENT, experiment(168))
    assert audit_changes(before, same) == (None, None)
    for hours in (24, None):
        after = audit_snapshot(EntityType.EXPERIMENT, experiment(hours))
        assert audit_changes(before, after) == (None, {"changed_fields": ["metrics"]})


async def test_a_change_to_the_metrics_drops_every_cached_result(monkeypatch):
    """The results route's cache is cleared through its own client, and the
    daily and segmented results' keys of this experiment, and only those, are
    deleted from the experiment cache."""
    from backend.app.api import deps
    from backend.app.api.v1.endpoints import experiments as endpoint

    experiment_id, other = uuid.uuid4(), uuid.uuid4()
    keys = {
        f"experiment_daily_results:{experiment_id}:{ENGINE_VERSION}",
        f"experiment_daily_results:{experiment_id}:{ENGINE_VERSION}:{uuid.uuid4()}",
        f"experiment_segmented_results:{experiment_id}:{ENGINE_VERSION}:country",
        f"experiment_daily_results:{other}:{ENGINE_VERSION}",
        f"experiment_segmented_results:{other}:{ENGINE_VERSION}:country",
        f"experiment:{experiment_id}",
    }

    class Redis:
        async def scan_iter(self, match):
            for key in sorted(keys):
                if fnmatch.fnmatchcase(key, match):
                    yield key

        async def delete(self, *names):
            keys.difference_update(names)

    cleared = []

    async def clear(experiment_id):
        cleared.append(experiment_id)

    monkeypatch.setattr(endpoint.results_endpoints, "clear_cached_results", clear)
    await endpoint._invalidate_computed_results(
        deps.CacheControl(enabled=True, redis=Redis()), experiment_id
    )
    assert cleared == [experiment_id]
    assert keys == {
        f"experiment_daily_results:{other}:{ENGINE_VERSION}",
        f"experiment_segmented_results:{other}:{ENGINE_VERSION}:country",
        f"experiment:{experiment_id}",
    }


async def test_the_results_cache_is_cleared_on_the_event_loop_at_most_a_second(
    monkeypatch,
):
    """The results route's own cache is cleared with an async client of the
    same settings: against a Redis that refuses, it makes one connect attempt
    with 1 s connect and socket timeouts, and raises nothing."""
    from redis.asyncio.connection import Connection

    from backend.app.api.v1.endpoints import results as results_endpoint
    from backend.app.core.config import settings

    for name, value in {
        "REDIS_HOST": "127.0.0.1",
        "REDIS_PORT": 1,
        "REDIS_PASSWORD": None,
        "REDIS_DB": 0,
        "REDIS_SSL": False,
    }.items():
        monkeypatch.setattr(settings, name, value)
    attempts = []
    real_connect = Connection._connect

    async def counting_connect(conn):
        attempts.append((conn.port, conn.socket_connect_timeout, conn.socket_timeout))
        return await real_connect(conn)

    monkeypatch.setattr(Connection, "_connect", counting_connect)
    assert await results_endpoint.clear_cached_results(uuid.uuid4()) is None
    assert attempts == [(1, 1, 1)]


async def test_a_clear_that_does_not_reach_the_cache_is_logged(monkeypatch, caplog):
    """A clear that fails leaves the cached answers in place until they
    expire, so it says so in the log, naming the experiment."""
    from backend.app.api.v1.endpoints import results as results_endpoint

    closed = []

    class Paused:
        async def scan_iter(self, match):
            raise ConnectionError("Redis is paused")
            yield  # an async generator

        async def aclose(self):
            closed.append(True)

    experiment_id = uuid.uuid4()
    monkeypatch.setattr(results_endpoint, "_async_cache_client", lambda: Paused())
    with caplog.at_level(logging.WARNING, logger=results_endpoint.logger.name):
        assert await results_endpoint.clear_cached_results(experiment_id) is None
    (record,) = [
        r for r in caplog.records if r.getMessage().startswith("Results cache clear")
    ]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        f"Results cache clear failed for {experiment_id}: Redis is paused"
    )
    assert closed == [True]


def test_the_design_etag_covers_every_column():
    hashed = visual.DESIGN_METRIC_COLUMNS + visual.DESIGN_METRIC_COLUMNS_WHEN_SET
    missing = [c for c in COLUMNS if c not in hashed]
    assert missing == [], f"missing {missing}"
    assert FIELD in visual.DESIGN_METRIC_COLUMNS_WHEN_SET
