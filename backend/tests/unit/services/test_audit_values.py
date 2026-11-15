"""What an audit entry may record, per entity (#221).

``AUDIT_VALUE_FIELDS`` (recorded as values) and ``AUDIT_NAME_ONLY_FIELDS``
(recorded by name when they change) name columns of the entity's model. A
field the model does not have at this base cannot be listed, and no list may
name a secret. ``AUDIT_NAME_ONLY_ROWS`` (an experiment's variants and metrics,
recorded by name when they change) reads relationships of the model, and every
column of the tables it reads is compared or listed here with a reason.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.models.api_key import APIKey
from backend.app.models.audit_log import EntityType
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    Metric,
    MetricType,
    Variant,
)
from backend.app.models.feature_flag import FeatureFlag
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.mutual_exclusion_group import MutualExclusionGroup
from backend.app.models.segment import Segment
from backend.app.models.user import User, UserRole
from backend.app.services.audit_service import (
    AUDIT_NAME_ONLY_FIELDS,
    AUDIT_NAME_ONLY_ROWS,
    AUDIT_REASON_MAX,
    AUDIT_VALUE_FIELDS,
    AUDIT_VALUE_MAX,
    AuditService,
    audit_changes,
    audit_identity,
    audit_snapshot,
    role_value,
)

pytestmark = [pytest.mark.unit]

MODELS = {
    EntityType.FEATURE_FLAG: FeatureFlag,
    EntityType.EXPERIMENT: Experiment,
    EntityType.USER: User,
    EntityType.API_KEY: APIKey,
    EntityType.HOLDOUT: GlobalHoldout,
    EntityType.MUTUAL_EXCLUSION_GROUP: MutualExclusionGroup,
    EntityType.SEGMENT: Segment,
}

#: Columns no audit entry may hold. ``key`` is a public slug on flags and
#: experiments, but on an API key it is the key's hash.
NEVER_RECORDED = {"hashed_password", "password", "token", "secret"}
NEVER_RECORDED_FOR = {EntityType.API_KEY: {"key"}}


def test_every_entity_with_values_has_a_model():
    assert set(AUDIT_VALUE_FIELDS) == set(MODELS)
    assert set(AUDIT_NAME_ONLY_FIELDS) == set(MODELS)


@pytest.mark.parametrize("entity", sorted(MODELS, key=lambda e: e.value))
def test_every_allow_listed_field_is_a_column(entity):
    columns = {c.name for c in MODELS[entity].__table__.columns}
    listed = set(AUDIT_VALUE_FIELDS[entity]) | set(AUDIT_NAME_ONLY_FIELDS[entity])
    assert listed <= columns, sorted(listed - columns)


@pytest.mark.parametrize("entity", sorted(MODELS, key=lambda e: e.value))
def test_no_allow_list_names_a_secret(entity):
    listed = set(AUDIT_VALUE_FIELDS[entity]) | set(AUDIT_NAME_ONLY_FIELDS[entity])
    assert not (listed & (NEVER_RECORDED | NEVER_RECORDED_FOR.get(entity, set())))


def test_an_update_records_changed_values_and_names_only_fields():
    row = SimpleNamespace(
        key="k",
        name="before",
        status=ExperimentStatus.DRAFT,
        start_date=None,
        end_date=None,
        description="secret plan",
        hypothesis="h",
        targeting_rules={"a": 1},
        metrics=None,
    )
    before = audit_snapshot(EntityType.EXPERIMENT, row)
    row.name = "after"
    row.status = ExperimentStatus.ACTIVE
    row.description = "a new secret plan"
    old, new = audit_changes(before, audit_snapshot(EntityType.EXPERIMENT, row))
    assert old == {"name": "before", "status": "draft"}
    assert new == {
        "name": "after",
        "status": "active",
        "changed_fields": ["description"],
    }
    assert "secret" not in json.dumps([old, new])


#: The feature-flag columns no audit entry records, each with its reason.
#: B8 (#1497): every column is a value field, a name-only field or listed
#: here, so a new column cannot be left unclassified.
FLAG_COLUMNS_NOT_AUDITED = {
    "id": "the entry's entity_id",
    "created_at": "the entry's own timestamp records the create",
    "updated_at": "the entry's own timestamp records the change",
    "owner_id": "set once, to the creator, who is the entry's actor",
    "default_value": "always false (D40); deprecated, dropped after launch",
    "tags": "labels, not behaviour",
    "workspace_id": "the workspaces module's column; nothing in the core reads it",
}


@pytest.mark.regression
def test_every_feature_flag_column_is_classified():
    columns = {c.name for c in FeatureFlag.__table__.columns}
    value = set(AUDIT_VALUE_FIELDS[EntityType.FEATURE_FLAG])
    name_only = set(AUDIT_NAME_ONLY_FIELDS[EntityType.FEATURE_FLAG])
    unaudited = set(FLAG_COLUMNS_NOT_AUDITED)
    assert not (value & name_only) and not ((value | name_only) & unaudited)
    assert columns == value | name_only | unaudited, (
        "unclassified feature_flags columns: "
        f"{sorted(columns - value - name_only - unaudited)}; "
        f"listed but not columns: {sorted((value | name_only | unaudited) - columns)}"
    )


#: The variant and metric columns a design save's entry does not compare, each
#: with its reason (#1782). Every column of the two tables is compared by a
#: member of ``AUDIT_NAME_ONLY_ROWS`` or listed here, so a new column cannot be
#: left unclassified.
ROW_COLUMNS_NOT_COMPARED = {
    "id": "every save deletes and re-creates the rows: a new id is no change",
    "created_at": "every save re-creates the rows: a new time is no change",
    "updated_at": "every save re-creates the rows: a new time is no change",
    "experiment_id": "the entry's entity_id",
}


def test_name_only_rows_read_relationships_of_the_entity():
    related = set()
    for entity, members in AUDIT_NAME_ONLY_ROWS.items():
        relationships = MODELS[entity].__mapper__.relationships
        fields = AUDIT_VALUE_FIELDS[entity] + AUDIT_NAME_ONLY_FIELDS[entity]
        for name, (relationship, columns) in members.items():
            assert relationship in relationships, (name, relationship)
            assert name not in fields, (
                f"{name} is also a field: one would hide the other"
            )
            assert len(set(columns)) == len(columns), name
            related.add(relationships[relationship].mapper.class_)
    assert related == {Variant, Metric}


@pytest.mark.parametrize("model", [Variant, Metric], ids=lambda m: m.__tablename__)
def test_every_variant_and_metric_column_is_classified(model):
    relationships = Experiment.__mapper__.relationships
    compared = {
        column
        for relationship, columns in AUDIT_NAME_ONLY_ROWS[
            EntityType.EXPERIMENT
        ].values()
        if relationships[relationship].mapper.class_ is model
        for column in columns
    }
    columns = {c.name for c in model.__table__.columns}
    unaudited = set(ROW_COLUMNS_NOT_COMPARED)
    assert not (compared & unaudited)
    assert columns == compared | unaudited, (
        f"unclassified {model.__tablename__} columns: "
        f"{sorted(columns - compared - unaudited)}; "
        f"listed but not columns: {sorted((compared | unaudited) - columns)}"
    )


def _variant(name, share, **extra):
    """A variant row as a save re-creates it: a new id and a new time."""
    return SimpleNamespace(
        id=uuid4(),
        created_at=datetime.now(timezone.utc),
        experiment_id=None,
        name=name,
        description=None,
        is_control=name == "A",
        traffic_allocation=share,
        configuration=extra.get("configuration"),
    )


def test_variants_and_metrics_are_compared_by_their_columns_not_ids_or_order():
    metric = SimpleNamespace(
        id=uuid4(),
        name="Conversion",
        description="s3cr3t metric",
        event_name="purchase",
        metric_type=MetricType.CONVERSION,
        is_primary=True,
        aggregation_method="average",
        minimum_sample_size=100,
        expected_effect=None,
        event_value_path=None,
        lower_is_better=False,
    )
    row = SimpleNamespace(
        name="exp",
        variants=[_variant("A", 50), _variant("B", 50)],
        metric_definitions=[metric],
    )
    before = audit_snapshot(EntityType.EXPERIMENT, row)

    row.variants = [_variant("B", 50), _variant("A", 50)]
    row.metric_definitions = [SimpleNamespace(**{**vars(metric), "id": uuid4()})]
    assert audit_changes(before, audit_snapshot(EntityType.EXPERIMENT, row)) == (
        None,
        None,
    )

    row.variants = [_variant("A", 50), _variant("B", 50, configuration={"c": 1})]
    old, new = audit_changes(before, audit_snapshot(EntityType.EXPERIMENT, row))
    assert (old, new) == (None, {"changed_fields": ["variants"]})
    assert "s3cr3t" not in json.dumps([old, new])


@pytest.mark.regression
def test_a_flag_records_its_value_type_and_its_values_by_name_only():
    """Plan F ruling 3 (M2): ``value_type`` as a value, the two values by name."""
    row = SimpleNamespace(
        key="checkout-label",
        name="Checkout label",
        status="ACTIVE",
        rollout_percentage=10,
        value_type="string",
        on_value="s3cr3t on copy",
        off_value="s3cr3t off copy",
        description=None,
        targeting_rules=None,
        variants=None,
    )
    identity = audit_identity(audit_snapshot(EntityType.FEATURE_FLAG, row))
    assert identity == {
        "key": "checkout-label",
        "name": "Checkout label",
        "status": "ACTIVE",
        "rollout_percentage": 10,
        "value_type": "string",
    }
    before = audit_snapshot(EntityType.FEATURE_FLAG, row)
    row.on_value = "s3cr3t new copy"
    row.rollout_percentage = 50
    old, new = audit_changes(before, audit_snapshot(EntityType.FEATURE_FLAG, row))
    assert old == {"rollout_percentage": 10}
    assert new == {"rollout_percentage": 50, "changed_fields": ["on_value"]}
    assert "s3cr3t" not in json.dumps([identity, old, new])


def test_an_8_kib_value_does_not_cost_the_percentage_change():
    """Why by name: a recorded 8 KiB value would truncate the whole entry."""
    row = SimpleNamespace(
        key="k",
        name="n",
        status="ACTIVE",
        rollout_percentage=10,
        value_type="json",
        on_value={"a": "x" * 8000},
        off_value=[],
        description=None,
        targeting_rules=None,
        variants=None,
    )
    before = audit_snapshot(EntityType.FEATURE_FLAG, row)
    row.on_value = {"a": "y" * 8000}
    row.rollout_percentage = 20
    _, new = audit_changes(before, audit_snapshot(EntityType.FEATURE_FLAG, row))
    stored = AuditService.record(
        _Session(),
        actor=SimpleNamespace(id=None, email=None, username=None),
        action="feature_flag_update",
        entity_type="feature_flag",
        entity_id=uuid4(),
        entity_name="n",
        before=None,
        after=new,
    )
    assert json.loads(stored.new_value) == {
        "rollout_percentage": 20,
        "changed_fields": ["on_value"],
    }


def test_nothing_changed_records_no_values():
    row = SimpleNamespace(
        name="h", holdout_percentage=5, is_active=False, description=""
    )
    snap = audit_snapshot(EntityType.HOLDOUT, row)
    assert audit_changes(snap, dict(snap)) == (None, None)


def test_identity_is_the_value_fields_only():
    row = SimpleNamespace(
        name="k1", scopes="sdk:ruleset", expires_at=None, user_id=uuid4(), key="hash"
    )
    identity = audit_identity(audit_snapshot(EntityType.API_KEY, row))
    assert set(identity) == {"name", "scopes", "expires_at", "user_id"}
    assert "hash" not in json.dumps(identity)


def test_role_value_shape():
    user = SimpleNamespace(role=UserRole.ANALYST, is_superuser=True)
    assert role_value(user) == {"role": "ANALYST", "is_superuser": True}


class _Session:
    def __init__(self):
        self.added = []

    def add(self, row):
        self.added.append(row)


def test_record_caps_reason_name_and_values():
    db = _Session()
    actor = SimpleNamespace(id=None, email=None, username=None)
    row = AuditService.record(
        db,
        actor=actor,
        action="feature_flag_update",
        entity_type="feature_flag",
        entity_id=uuid4(),
        entity_name="n" * 300,
        before=None,
        after={"name": "x" * (AUDIT_VALUE_MAX + 10)},
        reason="r" * (AUDIT_REASON_MAX + 10),
    )
    assert db.added == [row]
    assert row.user_email == "system"
    assert len(row.entity_name) == 255
    assert len(row.reason) == AUDIT_REASON_MAX
    assert json.loads(row.new_value) == {"truncated": True, "changed_fields": ["name"]}
    assert row.timestamp <= datetime.now(timezone.utc)
