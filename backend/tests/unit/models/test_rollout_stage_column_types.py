"""``rollout_stages`` binds its three times as UTC, and nothing else moved (#1462).

The behaviour on a session that is not in UTC is
``backend/tests/integration/api/test_rollout_stage_dates_utc.py``. These are the
pins that need no database:

- the three columns stay ``TIMESTAMP WITHOUT TIME ZONE`` (no migration). A type
  change made only in the model is invisible to an autogenerate run against a
  database built from the models, so the compiled DDL is asserted instead;
- ``UTCDateTime`` is on exactly the columns named below: ``BaseModel`` is not
  changed, so no other table's ``updated_at`` binds differently;
- the dashboard's two comments about stage dates say what the API does.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql

from backend.app.models import register_core_models
from backend.app.models.column_types import UTCDateTime
from backend.app.models.rollout_schedule import RolloutStage

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]

STAGE_TIMES = ("start_date", "completed_date", "updated_at")

#: Every (table, column) bound through UTCDateTime. A new one is added here on
#: purpose; one that appears without being listed (UTCDateTime put on
#: BaseModel, say) fails with the tables it reached.
UTC_DATETIME_COLUMNS = {
    ("experiments", "start_date"),
    ("experiments", "end_date"),
    ("rollout_stages", "start_date"),
    ("rollout_stages", "completed_date"),
    ("rollout_stages", "updated_at"),
}


@pytest.mark.parametrize("column", STAGE_TIMES)
def test_a_stage_time_is_still_timestamp_without_time_zone(column):
    ddl = (
        RolloutStage.__table__.c[column]
        .type.compile(dialect=postgresql.dialect())
        .upper()
    )
    assert ddl == "TIMESTAMP WITHOUT TIME ZONE", (
        f"rollout_stages.{column} compiles to {ddl}: the database type must not "
        "change (no migration ships with #1462)"
    )


def test_utc_datetime_is_on_exactly_the_listed_columns():
    base = register_core_models()
    found = {
        (table.name, column.name)
        for table in base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, UTCDateTime)
    }
    assert found == UTC_DATETIME_COLUMNS, (
        f"UTCDateTime also on {sorted(found - UTC_DATETIME_COLUMNS)}, "
        f"missing from {sorted(UTC_DATETIME_COLUMNS - found)}"
    )


def test_a_stage_created_at_is_left_as_basemodel_has_it():
    assert type(RolloutStage.__table__.c.created_at.type).__name__ == "DateTime"


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("2026-11-05T17:00:00Z", datetime(2026, 11, 5, 17, 0)),
        ("2026-11-05T09:00:00-08:00", datetime(2026, 11, 5, 17, 0)),
        ("2026-11-05T19:00:00+02:00", datetime(2026, 11, 5, 17, 0)),
        ("2026-11-05T17:00:00", datetime(2026, 11, 5, 17, 0)),
        ("2026-07-05T17:00:00Z", datetime(2026, 7, 5, 17, 0)),
        (
            datetime(2026, 11, 5, 9, 0, tzinfo=timezone(timedelta(hours=-8))),
            datetime(2026, 11, 5, 17, 0),
        ),
        (datetime(2026, 11, 5, 17, 0), datetime(2026, 11, 5, 17, 0)),
        (None, None),
    ],
)
def test_utc_datetime_binds_every_shape_as_naive_utc(given, stored):
    bound = UTCDateTime().process_bind_param(given, postgresql.dialect())
    assert bound == stored
    assert bound is None or bound.tzinfo is None


@pytest.mark.parametrize(
    "path",
    [
        "frontend/src/services/rolloutSchedules.ts",
        "frontend/src/components/feature-flags/RolloutScheduleManager.tsx",
    ],
)
def test_the_dashboard_comments_say_the_api_takes_a_naive_date_as_utc(path):
    # The comment text with its `*` margins and line breaks removed.
    text = " ".join(
        line.strip().lstrip("/*").strip()
        for line in (REPO_ROOT / path).read_text(encoding="utf-8").splitlines()
    )
    # The old reason, false since #1462: the API converts an aware value
    # itself, so the session's zone moves nothing.
    assert "converted by the database session's zone" not in text, path
    assert "the API takes a value with no zone as UTC (UTCDateTime)" in text.replace(
        "`", ""
    ), path
