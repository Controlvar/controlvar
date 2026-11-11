"""Experiment create/update schemas: numbers fit where they are stored (#559).

The sibling of ``test_experiment_field_lengths.py`` (#551, string lengths):

* a schema integer field stored in an integer column carries bounds inside
  the column's range, derived here from the column type (``Integer`` is 32
  bits, ``SmallInteger`` 16, ``BigInteger`` 64). Without them a larger value
  reached the database: create answered a generic 400, update a 500;
* a float inside a config stored as JSONB must be finite. JSONB cannot hold
  NaN or an infinity, so those reached the database the same way.

The request-level answers are pinned by
``backend/tests/integration/api/test_experiment_numeric_bounds_api.py``.
"""

import math
import typing

import annotated_types
import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import BigInteger, Float, Integer, Numeric, SmallInteger
from sqlalchemy.dialects.postgresql import JSONB

from backend.app.models.experiment import Experiment, Metric, Variant
from backend.app.schemas.experiment import (
    INT32_MAX,
    ExperimentCreate,
    ExperimentUpdate,
    MetricBase,
    VariantBase,
)

pytestmark = [pytest.mark.unit]

#: Schema -> the table its fields are stored in.
PAIRS = [
    (ExperimentCreate, Experiment),
    (ExperimentUpdate, Experiment),
    (VariantBase, Variant),
    (MetricBase, Metric),
]


def _column_range(column_type) -> tuple[int, int] | None:
    """The values an integer column holds, from its type."""
    for kind, bits in ((SmallInteger, 16), (BigInteger, 64), (Integer, 32)):
        if isinstance(column_type, kind):
            return -(2 ** (bits - 1)), 2 ** (bits - 1) - 1
    return None


def _schema_range(schema: type[BaseModel], field: str) -> tuple[float, float]:
    low, high = -math.inf, math.inf
    for item in schema.model_fields[field].metadata:
        if isinstance(item, annotated_types.Ge):
            low = max(low, item.ge)
        elif isinstance(item, annotated_types.Gt):
            low = max(low, item.gt + 1)
        elif isinstance(item, annotated_types.Le):
            high = min(high, item.le)
        elif isinstance(item, annotated_types.Lt):
            high = min(high, item.lt - 1)
    return low, high


def _columns(model, kinds) -> dict:
    return {
        column.name: column.type
        for column in model.__table__.columns
        if isinstance(column.type, kinds) and not isinstance(column.type, Float)
    }


@pytest.mark.regression
@pytest.mark.parametrize(
    "schema, model", PAIRS, ids=[schema.__name__ for schema, _ in PAIRS]
)
def test_every_integer_column_field_is_bounded_within_the_column(schema, model):
    columns = _columns(model, Integer)
    out_of_range = {}
    for field, column_type in columns.items():
        if field not in schema.model_fields:
            continue
        col_low, col_high = _column_range(column_type)
        low, high = _schema_range(schema, field)
        if low < col_low or high > col_high:
            out_of_range[field] = ((col_low, col_high), (low, high))
    assert out_of_range == {}, "field: (column range, schema range)"


def test_no_schema_field_is_stored_in_a_numeric_column():
    """A ``Numeric(p, s)`` column would need its own bound; there is none yet."""
    stored = {
        field
        for schema, model in PAIRS
        for field in _columns(model, Numeric)
        if field in schema.model_fields
    }
    assert stored == set()


def test_the_pin_sees_the_integer_columns():
    """Guard the guard: the pin above would pass vacuously on no columns."""
    assert set(_columns(Metric, Integer)) >= {"minimum_sample_size"}
    assert set(_columns(Variant, Integer)) >= {"traffic_allocation"}
    assert _column_range(Metric.__table__.c.minimum_sample_size.type) == (
        -(2**31),
        INT32_MAX,
    )


@pytest.mark.regression
def test_minimum_sample_size_is_limited_to_the_integer_column():
    base = {"name": "m", "event_name": "e"}
    assert MetricBase(**base, minimum_sample_size=INT32_MAX).minimum_sample_size == (
        INT32_MAX
    )
    with pytest.raises(ValidationError) as caught:
        MetricBase(**base, minimum_sample_size=INT32_MAX + 1)
    assert caught.value.errors()[0]["type"] == "less_than_equal"


# ---------------------------------------------------------------------------
# Floats in configs stored as JSONB are finite
# ---------------------------------------------------------------------------


def _model_of(annotation):
    """The BaseModel inside ``Optional[Model]``, if any (not ``List[Model]``)."""
    if typing.get_origin(annotation) is list:
        return None
    for arg in (annotation, *typing.get_args(annotation)):
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return arg
    return None


def _float_paths(model: type[BaseModel], prefix=()):
    """(path, sample) for every float a config holds, nested models included.

    ``sample`` builds the value placed at the path: a list of floats gets the
    probe as one of its items.
    """
    for name, info in model.model_fields.items():
        annotation = info.annotation
        args = typing.get_args(annotation)
        nested = _model_of(annotation)
        if annotation is float or float in args:
            yield (*prefix, name), lambda v: v
        elif any(
            typing.get_origin(a) is list and typing.get_args(a) == (float,)
            for a in (annotation, *args)
        ):
            # Ordered, so a [lower, upper] check (``rope``) does not refuse
            # the list for a reason other than the probe.
            yield (*prefix, name), lambda v: [v, 0.0] if v == -math.inf else [0.0, v]
        elif typing.get_origin(annotation) is list or any(
            typing.get_origin(a) is list for a in args
        ):
            inner = [
                _model_of(x)
                for a in (annotation, *args)
                for x in typing.get_args(a)
                if _model_of(x)
            ]
            for item in inner:
                for path, sample in _float_paths(item, (*prefix, name, 0)):
                    yield path, sample
        elif nested is not None:
            yield from _float_paths(nested, (*prefix, name))


def _jsonb_config_models():
    """ExperimentCreate fields stored in a JSONB column and typed as a model."""
    jsonb = _columns(Experiment, JSONB)
    out = {}
    for schema in (ExperimentCreate, ExperimentUpdate):
        for field, info in schema.model_fields.items():
            model = _model_of(info.annotation)
            if field in jsonb and model is not None:
                out[field] = model
    return out


CONFIGS = _jsonb_config_models()
FLOAT_CASES = [
    (field, model, path, sample, probe)
    for field, model in sorted(CONFIGS.items())
    for path, sample in _float_paths(model)
    for probe in (math.inf, -math.inf, math.nan)
]


def test_the_jsonb_pin_sees_every_config():
    assert set(CONFIGS) == {
        "sequential_testing_config",
        "variance_reduction_config",
        "bayesian_config",
        "split_url_config",
        # #448: strings only (an attribute name and values), no float.
        "balance_within",
    }
    paths = {(field, path) for field, _, path, _, _ in FLOAT_CASES}
    assert ("bayesian_config", ("rope",)) in paths
    assert ("split_url_config", ("variants", 0, "traffic_allocation")) in paths


def _valid(model: type[BaseModel]) -> dict:
    if model.__name__ == "SplitUrlConfig":
        return {
            "variants": [
                {"name": "a", "url": "https://a.example.com", "traffic_allocation": 50},
                {"name": "b", "url": "https://b.example.com", "traffic_allocation": 50},
            ]
        }
    return {}


def _place(data: dict, path: tuple, value) -> dict:
    target = data
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value
    return data


@pytest.mark.regression
@pytest.mark.parametrize(
    "field, model, path, sample, probe",
    FLOAT_CASES,
    ids=[f"{c[0]}.{'.'.join(map(str, c[2]))}-{c[4]}" for c in FLOAT_CASES],
)
def test_a_float_stored_as_jsonb_must_be_finite(field, model, path, sample, probe):
    model.model_validate(_valid(model))  # the base value is accepted
    with pytest.raises(ValidationError):
        model.model_validate(_place(_valid(model), path, sample(probe)))
