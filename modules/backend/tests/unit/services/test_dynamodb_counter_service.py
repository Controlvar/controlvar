"""
Unit tests for DynamoDBCounterService (P2-B TDD).

Uses moto to mock AWS DynamoDB so no real AWS credentials are needed.
All tests are fully isolated — each creates a fresh mocked table.

Coverage:
- increment_counter: atomic ADD, returns new value
- get_experiment_counters: queries all variants, aggregates totals
- bulk_increment: processes list of IncrementRequests, returns processed/failed counts
- reset_counters: sets counter(s) to 0, supports variant_id=None and counter_type=None
"""

from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from modules.backend.app.schemas.realtime_counters import (
    CounterType,
    IncrementRequest,
)
from modules.backend.app.services.dynamodb_counter_service import DynamoDBCounterService

TABLE_NAME = "experiment-counters"
AWS_REGION = "us-east-1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def create_table(dynamodb_resource):
    """Create the experiment-counters DynamoDB table."""
    table = dynamodb_resource.create_table(
        TableName=TABLE_NAME,
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    table.meta.client.get_waiter("table_exists").wait(TableName=TABLE_NAME)
    return table


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def aws_credentials(monkeypatch):
    """Set dummy AWS credentials so moto doesn't complain."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", AWS_REGION)


@pytest.fixture
def dynamodb_resource(aws_credentials):
    """Provide a moto-mocked DynamoDB resource with the counters table created."""
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=AWS_REGION)
        create_table(resource)
        yield resource


@pytest.fixture
def counter_service(dynamodb_resource):
    """Return a DynamoDBCounterService backed by the mocked DynamoDB."""
    return DynamoDBCounterService(
        table_name=TABLE_NAME,
        dynamodb_resource=dynamodb_resource,
    )


# ---------------------------------------------------------------------------
# increment_counter
# ---------------------------------------------------------------------------


class TestIncrementCounter:
    def test_increment_assignment_returns_one_on_first_call(self, counter_service):
        new_val = counter_service.increment_counter(
            experiment_id="exp-1",
            variant_id="v-control",
            counter_type=CounterType.ASSIGNMENT,
        )
        assert new_val == 1

    def test_increment_returns_cumulative_value(self, counter_service):
        counter_service.increment_counter("exp-1", "v-control", CounterType.ASSIGNMENT)
        counter_service.increment_counter("exp-1", "v-control", CounterType.ASSIGNMENT)
        new_val = counter_service.increment_counter(
            "exp-1", "v-control", CounterType.ASSIGNMENT
        )
        assert new_val == 3

    def test_increment_event_counter(self, counter_service):
        val = counter_service.increment_counter("exp-1", "v-control", CounterType.EVENT)
        assert val == 1

    def test_increment_conversion_counter(self, counter_service):
        val = counter_service.increment_counter(
            "exp-1", "v-control", CounterType.CONVERSION
        )
        assert val == 1

    def test_different_variants_are_independent(self, counter_service):
        counter_service.increment_counter("exp-1", "v-control", CounterType.ASSIGNMENT)
        counter_service.increment_counter("exp-1", "v-control", CounterType.ASSIGNMENT)
        treatment_val = counter_service.increment_counter(
            "exp-1", "v-treatment", CounterType.ASSIGNMENT
        )
        # treatment variant has only been incremented once
        assert treatment_val == 1

    def test_different_experiments_are_independent(self, counter_service):
        counter_service.increment_counter("exp-1", "v1", CounterType.ASSIGNMENT)
        val_exp2 = counter_service.increment_counter(
            "exp-2", "v1", CounterType.ASSIGNMENT
        )
        assert val_exp2 == 1

    def test_custom_amount_increment(self, counter_service):
        val = counter_service.increment_counter(
            "exp-1", "v1", CounterType.EVENT, amount=5
        )
        assert val == 5

    def test_second_custom_amount_adds_correctly(self, counter_service):
        counter_service.increment_counter("exp-1", "v1", CounterType.EVENT, amount=5)
        val = counter_service.increment_counter(
            "exp-1", "v1", CounterType.EVENT, amount=3
        )
        assert val == 8

    def test_increment_uses_add_expression_atomically(
        self, counter_service, dynamodb_resource
    ):
        """Verify the item is stored with the correct attribute name."""
        counter_service.increment_counter("exp-1", "v1", CounterType.ASSIGNMENT)
        table = dynamodb_resource.Table(TABLE_NAME)
        item = table.get_item(Key={"pk": "EXPERIMENT#exp-1", "sk": "VARIANT#v1"}).get(
            "Item", {}
        )
        assert "assignments" in item
        assert int(item["assignments"]) == 1


# ---------------------------------------------------------------------------
# get_experiment_counters
# ---------------------------------------------------------------------------


class TestGetExperimentCounters:
    def test_returns_empty_counters_when_no_data(self, counter_service):
        result = counter_service.get_experiment_counters("exp-nonexistent")
        assert result.experiment_id == "exp-nonexistent"
        assert result.total_assignments == 0
        assert result.total_events == 0
        assert result.total_conversions == 0
        assert result.variants == []

    def test_returns_correct_totals_for_single_variant(self, counter_service):
        counter_service.increment_counter("exp-1", "v1", CounterType.ASSIGNMENT, 10)
        counter_service.increment_counter("exp-1", "v1", CounterType.EVENT, 8)
        counter_service.increment_counter("exp-1", "v1", CounterType.CONVERSION, 3)

        result = counter_service.get_experiment_counters("exp-1")
        assert result.total_assignments == 10
        assert result.total_events == 8
        assert result.total_conversions == 3

    def test_aggregates_multiple_variants(self, counter_service):
        counter_service.increment_counter("exp-1", "v1", CounterType.ASSIGNMENT, 100)
        counter_service.increment_counter("exp-1", "v2", CounterType.ASSIGNMENT, 200)

        result = counter_service.get_experiment_counters("exp-1")
        assert result.total_assignments == 300
        assert len(result.variants) == 2

    def test_variant_ids_are_correct(self, counter_service):
        counter_service.increment_counter("exp-1", "v1", CounterType.ASSIGNMENT)
        counter_service.increment_counter("exp-1", "v2", CounterType.ASSIGNMENT)

        result = counter_service.get_experiment_counters("exp-1")
        variant_ids = {v.variant_id for v in result.variants}
        assert variant_ids == {"v1", "v2"}

    def test_last_updated_is_set(self, counter_service):
        counter_service.increment_counter("exp-1", "v1", CounterType.ASSIGNMENT)
        result = counter_service.get_experiment_counters("exp-1")
        assert result.last_updated is not None

    def test_does_not_return_other_experiment_data(self, counter_service):
        counter_service.increment_counter("exp-1", "v1", CounterType.ASSIGNMENT, 5)
        counter_service.increment_counter("exp-2", "v1", CounterType.ASSIGNMENT, 100)

        result = counter_service.get_experiment_counters("exp-1")
        assert result.total_assignments == 5


# ---------------------------------------------------------------------------
# bulk_increment
# ---------------------------------------------------------------------------


class TestBulkIncrement:
    def test_processes_single_increment(self, counter_service):
        increments = [
            IncrementRequest(
                experiment_id="exp-1",
                variant_id="v1",
                counter_type=CounterType.ASSIGNMENT,
            )
        ]
        resp = counter_service.bulk_increment(increments)
        assert resp.processed == 1
        assert resp.failed == 0
        assert len(resp.results) == 1

    def test_processes_multiple_increments(self, counter_service):
        increments = [
            IncrementRequest(
                experiment_id="exp-1",
                variant_id="v1",
                counter_type=CounterType.ASSIGNMENT,
            ),
            IncrementRequest(
                experiment_id="exp-1",
                variant_id="v2",
                counter_type=CounterType.EVENT,
            ),
            IncrementRequest(
                experiment_id="exp-2",
                variant_id="v1",
                counter_type=CounterType.CONVERSION,
            ),
        ]
        resp = counter_service.bulk_increment(increments)
        assert resp.processed == 3
        assert resp.failed == 0

    def test_results_contain_new_values(self, counter_service):
        increments = [
            IncrementRequest(
                experiment_id="exp-1",
                variant_id="v1",
                counter_type=CounterType.ASSIGNMENT,
                amount=5,
            )
        ]
        resp = counter_service.bulk_increment(increments)
        assert resp.results[0].new_value == 5

    def test_partial_failure_reported(self, counter_service, monkeypatch):
        """Simulate one failure in a bulk operation and verify it's reported."""
        call_count = [0]

        original_increment = counter_service.increment_counter

        def patched_increment(experiment_id, variant_id, counter_type, amount=1):
            call_count[0] += 1
            if call_count[0] == 2:
                raise Exception("Simulated DynamoDB failure")
            return original_increment(experiment_id, variant_id, counter_type, amount)

        monkeypatch.setattr(counter_service, "increment_counter", patched_increment)

        increments = [
            IncrementRequest(
                experiment_id="exp-1",
                variant_id="v1",
                counter_type=CounterType.ASSIGNMENT,
            ),
            IncrementRequest(
                experiment_id="exp-1",
                variant_id="v2",
                counter_type=CounterType.ASSIGNMENT,
            ),
            IncrementRequest(
                experiment_id="exp-1",
                variant_id="v3",
                counter_type=CounterType.ASSIGNMENT,
            ),
        ]
        resp = counter_service.bulk_increment(increments)
        assert resp.failed == 1
        assert resp.processed == 2

    def test_all_amounts_applied_correctly(self, counter_service):
        increments = [
            IncrementRequest(
                experiment_id="exp-1",
                variant_id="v1",
                counter_type=CounterType.EVENT,
                amount=10,
            ),
            IncrementRequest(
                experiment_id="exp-1",
                variant_id="v1",
                counter_type=CounterType.EVENT,
                amount=5,
            ),
        ]
        resp = counter_service.bulk_increment(increments)
        # Second call adds to first, resulting new_value should be 15
        assert resp.results[1].new_value == 15


# ---------------------------------------------------------------------------
# reset_counters
# ---------------------------------------------------------------------------


class TestResetCounters:
    def test_reset_all_counters_for_experiment(
        self, counter_service, dynamodb_resource
    ):
        # Populate some counters
        counter_service.increment_counter("exp-1", "v1", CounterType.ASSIGNMENT, 50)
        counter_service.increment_counter("exp-1", "v1", CounterType.EVENT, 40)
        counter_service.increment_counter("exp-1", "v2", CounterType.ASSIGNMENT, 30)

        counter_service.reset_counters("exp-1", reason="Test reset all")

        result = counter_service.get_experiment_counters("exp-1")
        assert result.total_assignments == 0
        assert result.total_events == 0
        assert result.total_conversions == 0

    def test_reset_specific_variant_only(self, counter_service):
        counter_service.increment_counter("exp-1", "v1", CounterType.ASSIGNMENT, 50)
        counter_service.increment_counter("exp-1", "v2", CounterType.ASSIGNMENT, 50)

        counter_service.reset_counters("exp-1", variant_id="v1", reason="Reset v1 only")

        result = counter_service.get_experiment_counters("exp-1")
        # v1 reset, v2 untouched
        assert result.total_assignments == 50

    def test_reset_specific_counter_type_only(self, counter_service):
        counter_service.increment_counter("exp-1", "v1", CounterType.ASSIGNMENT, 100)
        counter_service.increment_counter("exp-1", "v1", CounterType.EVENT, 80)
        counter_service.increment_counter("exp-1", "v1", CounterType.CONVERSION, 20)

        counter_service.reset_counters(
            "exp-1", counter_type=CounterType.CONVERSION, reason="Conversion bug fixed"
        )

        result = counter_service.get_experiment_counters("exp-1")
        assert result.total_assignments == 100
        assert result.total_events == 80
        assert result.total_conversions == 0

    def test_reset_specific_variant_and_counter_type(self, counter_service):
        counter_service.increment_counter("exp-1", "v1", CounterType.ASSIGNMENT, 100)
        counter_service.increment_counter("exp-1", "v1", CounterType.CONVERSION, 20)
        counter_service.increment_counter("exp-1", "v2", CounterType.ASSIGNMENT, 50)
        counter_service.increment_counter("exp-1", "v2", CounterType.CONVERSION, 10)

        counter_service.reset_counters(
            "exp-1",
            variant_id="v1",
            counter_type=CounterType.CONVERSION,
            reason="Fix v1 conversion data",
        )

        result = counter_service.get_experiment_counters("exp-1")
        assert result.total_assignments == 150  # both variants untouched
        assert result.total_conversions == 10  # only v2 conversion remains

    def test_reset_nonexistent_experiment_does_not_raise(self, counter_service):
        # Should not raise an exception
        counter_service.reset_counters("exp-nonexistent", reason="Clean up")

    def test_reset_after_reset_stays_zero(self, counter_service):
        counter_service.increment_counter("exp-1", "v1", CounterType.ASSIGNMENT, 50)
        counter_service.reset_counters("exp-1", reason="First reset")
        counter_service.reset_counters("exp-1", reason="Second reset")

        result = counter_service.get_experiment_counters("exp-1")
        assert result.total_assignments == 0


# ---------------------------------------------------------------------------
# Where the default table name comes from
# ---------------------------------------------------------------------------


class TestDefaultTableName:
    """``ModulesSettings.DYNAMODB_COUNTERS_TABLE`` must actually be read.

    The field was declared in ``modules/backend/app/settings.py`` (manifest
    group 8) while the service read ``os.environ`` directly, so a value in
    ``.env.prod`` was loaded onto the settings object and then ignored -- the
    deployment wrote its counters to ``experiment-counters`` whatever the
    dotenv file said, and the settings docstring's claim that group 8 is read
    from those files was false.
    """

    @staticmethod
    def _stub_resource():
        class _Resource:
            def Table(self, name):  # boto3's own spelling
                return name

        return _Resource()

    @pytest.mark.regression
    def test_the_default_comes_from_the_modules_settings(self, monkeypatch):
        from modules.backend.app import settings as modules_settings

        built = modules_settings.build_modules_settings()
        built.DYNAMODB_COUNTERS_TABLE = "counters-from-the-dotenv-file"
        monkeypatch.setattr(modules_settings, "_instance", built)

        service = DynamoDBCounterService(dynamodb_resource=self._stub_resource())
        assert service.table_name == "counters-from-the-dotenv-file"

    def test_an_explicit_table_name_still_wins(self, monkeypatch):
        from modules.backend.app import settings as modules_settings

        built = modules_settings.build_modules_settings()
        built.DYNAMODB_COUNTERS_TABLE = "counters-from-the-dotenv-file"
        monkeypatch.setattr(modules_settings, "_instance", built)

        service = DynamoDBCounterService(
            table_name="explicit", dynamodb_resource=self._stub_resource()
        )
        assert service.table_name == "explicit"

    def test_the_process_environment_still_reaches_the_service(self, monkeypatch):
        """pydantic-settings reads os.environ, so the documented env var works."""
        from modules.backend.app import settings as modules_settings

        monkeypatch.setenv("DYNAMODB_COUNTERS_TABLE", "counters-from-the-environment")
        monkeypatch.setattr(modules_settings, "_instance", None)

        service = DynamoDBCounterService(dynamodb_resource=self._stub_resource())
        assert service.table_name == "counters-from-the-environment"
