"""``backend.app.core.flag_environments``: the fixed set, and what each environment serves (#2011).

* The set is exactly ``production``, ``staging`` and ``development``, the
  default first; any other spelling is not an environment.
* ``flag_in_environment``:

  - production is the flag row itself, unchanged;
  - no side row means off in that environment (``INACTIVE``, no rules, 0%);
  - an archived flag row is ``ARCHIVED`` in every environment, whatever its
    side rows say;
  - otherwise the side row decides, and key, id, value type and values stay
    the flag row's;
  - each test checks that the target changed and the other environment did
    not.

* The registry and the models' checks name the same environments.
* The module reads no settings: the flag environment is never derived from
  the deployment's ``ENVIRONMENT``.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import pytest
import sqlalchemy as sa

from backend.app.core import flag_environments
from backend.app.core.flag_environments import (
    DEFAULT,
    DEVELOPMENT,
    DISPLAY_NAMES,
    ENVIRONMENT_FIELDS,
    FLAG_ENVIRONMENTS,
    NON_PRODUCTION,
    PRODUCTION,
    SETTING_IDENTITY_COLUMNS,
    STAGING,
    FlagEnvironmentName,
    FlagInEnvironment,
    flag_in_environment,
    is_flag_environment,
)
from backend.app.models.api_key import (
    API_KEY_ENVIRONMENT_CHECK,
    API_KEY_ENVIRONMENT_CHECK_SQL,
    APIKey,
)
from backend.app.models.feature_flag import (
    FLAG_ENVIRONMENT_NAME_CHECK,
    FLAG_ENVIRONMENT_NAME_CHECK_SQL,
    FeatureFlag,
    FeatureFlagStatus,
    FlagEnvironmentSetting,
)

pytestmark = [pytest.mark.unit]

MODULE = Path(flag_environments.__file__)

RULES = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [
                {"attribute": "country", "operator": "equals", "value": "US"}
            ],
        }
    ],
}


def _flag(status=FeatureFlagStatus.ACTIVE, **overrides) -> FeatureFlag:
    """A production flag: on at 40% with no rules, a string flag."""
    values = {
        "id": uuid.uuid4(),
        "key": "checkout-v2",
        "name": "Checkout v2",
        "status": status,
        "rollout_percentage": 40,
        "targeting_rules": None,
        "value_type": "string",
        "on_value": "new",
        "off_value": "old",
    }
    values.update(overrides)
    return FeatureFlag(**values)


def _setting(flag, environment, is_active=True, rollout_percentage=75, rules=RULES):
    return FlagEnvironmentSetting(
        id=uuid.uuid4(),
        feature_flag_id=flag.id,
        environment=environment,
        is_active=is_active,
        rollout_percentage=rollout_percentage,
        targeting_rules=rules,
    )


def _production(flag) -> tuple:
    return (flag.status, flag.targeting_rules, flag.rollout_percentage)


# ---------------------------------------------------------------------------
# The set
# ---------------------------------------------------------------------------
def test_the_environments_are_exactly_three_the_default_first():
    assert FLAG_ENVIRONMENTS == ("production", "staging", "development")
    assert (PRODUCTION, STAGING, DEVELOPMENT) == FLAG_ENVIRONMENTS
    assert DEFAULT == "production" == FLAG_ENVIRONMENTS[0]
    assert NON_PRODUCTION == ("staging", "development")
    assert DISPLAY_NAMES == {
        "production": "Production",
        "staging": "Staging",
        "development": "Development",
    }
    assert get_args(FlagEnvironmentName) == FLAG_ENVIRONMENTS


@pytest.mark.parametrize("name", FLAG_ENVIRONMENTS)
def test_each_environment_is_one(name):
    assert is_flag_environment(name)


@pytest.mark.regression
@pytest.mark.parametrize(
    "name",
    [
        "Production",
        "PRODUCTION",
        "prod",
        "dev",
        "qa",
        "",
        " production",
        "staging\n",
        None,
        1,
    ],
)
def test_any_other_spelling_is_not_an_environment(name):
    assert not is_flag_environment(name)
    with pytest.raises(ValueError, match="not a flag environment"):
        flag_in_environment(_flag(), None, name)


def test_a_long_refused_name_is_shown_shortened():
    with pytest.raises(ValueError) as raised:
        flag_in_environment(_flag(), None, "x" * 500)
    assert len(str(raised.value)) < 120


# ---------------------------------------------------------------------------
# Production is the flag row
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_production_is_the_flag_row_itself():
    flag = _flag()
    before = _production(flag)
    assert flag_in_environment(flag, None, PRODUCTION) is flag
    assert _production(flag) == before


def test_production_has_no_side_row():
    flag = _flag()
    with pytest.raises(ValueError, match="production is the flag row"):
        flag_in_environment(flag, _setting(flag, STAGING), PRODUCTION)


@pytest.mark.regression
def test_an_archived_flag_is_archived_in_production():
    flag = _flag(status=FeatureFlagStatus.ARCHIVED)
    view = flag_in_environment(flag, None, PRODUCTION)
    assert view is flag and view.status is FeatureFlagStatus.ARCHIVED


# ---------------------------------------------------------------------------
# No row means off
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("environment", NON_PRODUCTION)
def test_no_side_row_is_off_and_production_is_untouched(environment):
    flag = _flag()
    before = _production(flag)
    view = flag_in_environment(flag, None, environment)
    assert isinstance(view, FlagInEnvironment)
    assert view.status is FeatureFlagStatus.INACTIVE
    assert view.targeting_rules is None
    assert view.rollout_percentage == 0
    assert view.configured is False
    assert view.environment == environment
    # The flag row (production) is still on at 40%.
    assert _production(flag) == before == (FeatureFlagStatus.ACTIVE, None, 40)


@pytest.mark.parametrize("environment", NON_PRODUCTION)
def test_identity_and_values_are_the_flag_rows(environment):
    flag = _flag()
    for setting in (None, _setting(flag, environment)):
        view = flag_in_environment(flag, setting, environment)
        assert view.id == flag.id
        assert view.key == "checkout-v2"
        assert view.name == "Checkout v2"
        assert view.value_type == "string"
        assert (view.on_value, view.off_value) == ("new", "old")


# ---------------------------------------------------------------------------
# A side row decides, except over an archived flag
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_a_staging_row_changes_staging_and_neither_production_nor_development():
    flag = _flag()
    before = _production(flag)
    staging = flag_in_environment(flag, _setting(flag, STAGING), STAGING)
    development = flag_in_environment(flag, None, DEVELOPMENT)

    assert (staging.status, staging.targeting_rules, staging.rollout_percentage) == (
        FeatureFlagStatus.ACTIVE,
        RULES,
        75,
    )
    assert staging.configured is True
    assert (development.status, development.rollout_percentage) == (
        FeatureFlagStatus.INACTIVE,
        0,
    )
    assert _production(flag) == before


@pytest.mark.regression
def test_an_inactive_side_row_is_off_and_keeps_its_percentage():
    flag = _flag()
    view = flag_in_environment(
        flag, _setting(flag, DEVELOPMENT, is_active=False), DEVELOPMENT
    )
    assert view.status is FeatureFlagStatus.INACTIVE
    assert view.rollout_percentage == 75
    assert view.configured is True
    assert flag.status is FeatureFlagStatus.ACTIVE


@pytest.mark.regression
@pytest.mark.parametrize("environment", NON_PRODUCTION)
@pytest.mark.parametrize(
    "archived", [FeatureFlagStatus.ARCHIVED, "ARCHIVED", "archived"]
)
def test_an_archived_flag_row_wins_over_an_active_side_row(environment, archived):
    """The status column loads as the enum, and some writers assign the string."""
    flag = _flag(status=FeatureFlagStatus.ACTIVE)
    flag.status = archived
    view = flag_in_environment(flag, _setting(flag, environment), environment)
    assert view.status is FeatureFlagStatus.ARCHIVED
    # The rules and percentage are still the side row's; ARCHIVED is what
    # turns them off.
    assert view.rollout_percentage == 75
    assert flag.status == archived


@pytest.mark.regression
def test_an_off_production_flag_can_be_on_in_staging():
    flag = _flag(status=FeatureFlagStatus.INACTIVE, rollout_percentage=0)
    view = flag_in_environment(flag, _setting(flag, STAGING), STAGING)
    assert view.status is FeatureFlagStatus.ACTIVE
    assert _production(flag) == (FeatureFlagStatus.INACTIVE, None, 0)


def test_a_side_row_of_another_environment_or_flag_is_refused():
    flag = _flag()
    with pytest.raises(ValueError, match="'development''s, not 'staging''s"):
        flag_in_environment(flag, _setting(flag, DEVELOPMENT), STAGING)
    other = _setting(_flag(), STAGING)
    with pytest.raises(ValueError, match="another flag"):
        flag_in_environment(flag, other, STAGING)


def test_the_view_works_on_duck_typed_rows():
    """The ruleset route loads columns, not models: a namespace with the same
    attributes is a flag here too."""
    flag = SimpleNamespace(
        id=1, key="k", status="ACTIVE", targeting_rules=None, rollout_percentage=10
    )
    setting = SimpleNamespace(
        feature_flag_id=1,
        environment=STAGING,
        is_active=True,
        targeting_rules=None,
        rollout_percentage=60,
    )
    view = flag_in_environment(flag, setting, STAGING)
    assert (view.status, view.rollout_percentage, view.key) == (
        FeatureFlagStatus.ACTIVE,
        60,
        "k",
    )


# ---------------------------------------------------------------------------
# The view is read-only, and serves every column of the side row
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_the_view_cannot_be_written_and_the_flag_row_stays_as_it_was():
    flag = _flag()
    before = _production(flag)
    view = flag_in_environment(flag, _setting(flag, STAGING), STAGING)
    for name, value in (
        ("rollout_percentage", 100),
        ("status", FeatureFlagStatus.INACTIVE),
        ("key", "renamed"),
    ):
        with pytest.raises(AttributeError, match="read-only"):
            setattr(view, name, value)
    with pytest.raises(AttributeError, match="read-only"):
        del view.status
    assert view.rollout_percentage == 75
    assert flag.key == "checkout-v2"
    assert _production(flag) == before


def test_the_per_environment_fields_are_the_side_rows_columns():
    """A column added to ``flag_environment_settings`` that the view does not
    serve fails here: the view maps ``is_active`` to ``status`` and copies the
    rest. Promotion (#2011's write path) reads the same list."""
    columns = [c.name for c in FlagEnvironmentSetting.__table__.columns]
    assert set(SETTING_IDENTITY_COLUMNS) <= set(columns)
    served = [c for c in columns if c not in SETTING_IDENTITY_COLUMNS]
    assert sorted(served) == ["is_active", "rollout_percentage", "targeting_rules"]
    assert ENVIRONMENT_FIELDS == ("status", "targeting_rules", "rollout_percentage")
    assert set(FlagInEnvironment.__slots__) >= set(ENVIRONMENT_FIELDS)
    for column in served:
        if column != "is_active":
            assert column in ENVIRONMENT_FIELDS
    # Each per-environment field is a column of the flag row too.
    flag_columns = {c.name for c in FeatureFlag.__table__.columns}
    assert set(ENVIRONMENT_FIELDS) <= flag_columns


# ---------------------------------------------------------------------------
# The models' checks name the registry's environments
# ---------------------------------------------------------------------------
def _in_list(names) -> str:
    return ", ".join(f"'{name}'" for name in names)


def _checks(model) -> dict:
    return {
        c.name: str(c.sqltext)
        for c in model.__table__.constraints
        if isinstance(c, sa.CheckConstraint)
    }


@pytest.mark.regression
def test_the_models_checks_are_the_registrys_environments():
    assert API_KEY_ENVIRONMENT_CHECK_SQL == (
        f"environment IN ({_in_list(FLAG_ENVIRONMENTS)})"
    )
    assert FLAG_ENVIRONMENT_NAME_CHECK_SQL == (
        f"environment IN ({_in_list(NON_PRODUCTION)})"
    )
    assert _checks(APIKey)[API_KEY_ENVIRONMENT_CHECK] == API_KEY_ENVIRONMENT_CHECK_SQL
    assert (
        _checks(FlagEnvironmentSetting)[FLAG_ENVIRONMENT_NAME_CHECK]
        == FLAG_ENVIRONMENT_NAME_CHECK_SQL
    )


@pytest.mark.regression
def test_a_new_key_is_a_production_key():
    column = APIKey.__table__.columns["environment"]
    assert column.nullable is False
    assert column.default.arg == DEFAULT
    assert str(column.server_default.arg) == f"'{DEFAULT}'"
    assert column.type.length == 32


def test_the_side_row_is_off_with_no_rules_and_zero_percent_by_default():
    table = FlagEnvironmentSetting.__table__
    assert str(table.columns["is_active"].server_default.arg) == "false"
    assert str(table.columns["rollout_percentage"].server_default.arg) == "0"
    assert table.columns["targeting_rules"].nullable is True
    (foreign_key,) = table.columns["feature_flag_id"].foreign_keys
    assert foreign_key.ondelete == "CASCADE"
    assert foreign_key.column.table.name == "feature_flags"
    (unique,) = [c for c in table.constraints if isinstance(c, sa.UniqueConstraint)]
    assert unique.name == "uq_flag_environment"
    assert [c.name for c in unique.columns] == ["feature_flag_id", "environment"]


# ---------------------------------------------------------------------------
# Never the deployment's ENVIRONMENT
# ---------------------------------------------------------------------------
def _reads_settings(source: str) -> list:
    """What in *source* reads the deployment's settings: an import of the
    config module, or any ``.ENVIRONMENT`` / ``"ENVIRONMENT"`` reference."""
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "config" in node.module:
            found.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Import):
            found += [a.name for a in node.names if "config" in a.name]
        elif isinstance(node, ast.Attribute) and node.attr in {
            "ENVIRONMENT",
            "APP_ENV",
        }:
            found.append(f".{node.attr}")
        elif isinstance(node, ast.Constant) and node.value in {
            "ENVIRONMENT",
            "APP_ENV",
        }:
            found.append(repr(node.value))
    return found


@pytest.mark.regression
def test_the_module_never_reads_the_deployment_environment():
    assert _reads_settings(MODULE.read_text(encoding="utf-8")) == []


def test_the_settings_reader_sees_a_settings_read():
    """Positive control for the test above, on a planted source."""
    planted = (
        "from backend.app.core.config import settings\nDEFAULT = settings.ENVIRONMENT\n"
    )
    assert _reads_settings(planted) == [
        "from backend.app.core.config import ...",
        ".ENVIRONMENT",
    ]
