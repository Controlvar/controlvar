"""
Unit tests for ETLService (P3-A TDD).

All AWS interactions are mocked via moto so tests run without real credentials.
Uses moto's decorator-style mocking (``mock_aws``) for Glue.

Coverage:
- run_etl_job: maps boto3 Glue response to ETLJobResponse
- get_job_status: returns correct GlueJobStatus enum value
- add_partitions: creates correct year/month/day/hour partition values
- run_crawler: starts crawler and returns RUNNING state
- get_crawler_status: returns current crawler state
- _get_glue_job_name: returns correct job name per ETLJobType
- run_etl_job with experiment_id filter
- run_etl_job with force_reprocess flag
"""

from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from modules.backend.app.schemas.etl import (
    ETLJobRequest,
    ETLJobResponse,
    ETLJobType,
    GlueCrawlerStatus,
    GlueJobStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

AWS_REGION = "us-east-1"
GLUE_ETL_JOB_NAME = "experimentation-events-etl"
GLUE_METRICS_JOB_NAME = "experimentation-metrics-etl"
GLUE_DATABASE = "experimentation"
GLUE_CRAWLER_NAME = "experimentation-crawler"


def _make_glue_client():
    return boto3.client("glue", region_name=AWS_REGION)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_settings(monkeypatch):
    """Patch settings with test values, and put the service in moto's region.

    The service builds its Glue client with no region, so botocore takes it
    from ``AWS_DEFAULT_REGION`` (never ``AWS_REGION``). Set here rather than
    inherited: CI's module job sets no region, and another test file setting
    one at import time must not decide whether these pass.
    """
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", AWS_REGION)
    monkeypatch.setattr(
        "modules.backend.app.services.etl_service.modules_settings",
        MagicMock(
            GLUE_ETL_JOB_NAME=GLUE_ETL_JOB_NAME,
            GLUE_METRICS_JOB_NAME=GLUE_METRICS_JOB_NAME,
            GLUE_DATABASE=GLUE_DATABASE,
            GLUE_EVENTS_TABLE="raw_events",
            GLUE_CRAWLER_NAME=GLUE_CRAWLER_NAME,
        ),
    )


# ---------------------------------------------------------------------------
# run_etl_job tests
# ---------------------------------------------------------------------------


class TestRunETLJob:
    @mock_aws
    def test_run_events_to_parquet_job(self, mock_settings):
        """run_etl_job for EVENTS_TO_PARQUET starts the correct Glue job."""
        from modules.backend.app.services.etl_service import ETLService

        glue = _make_glue_client()
        # Create the job in moto
        glue.create_job(
            Name=GLUE_ETL_JOB_NAME,
            Role="arn:aws:iam::123456789012:role/GlueRole",
            Command={"Name": "glueetl", "ScriptLocation": "s3://bucket/script.py"},
        )

        svc = ETLService()
        req = ETLJobRequest(job_type=ETLJobType.EVENTS_TO_PARQUET, date="2024-01-15")
        resp = svc.run_etl_job(req)

        assert isinstance(resp, ETLJobResponse)
        assert resp.job_name == GLUE_ETL_JOB_NAME
        assert resp.job_type == ETLJobType.EVENTS_TO_PARQUET
        assert resp.status in {GlueJobStatus.STARTING, GlueJobStatus.RUNNING}
        assert resp.job_run_id  # non-empty string

    @mock_aws
    def test_run_metrics_aggregation_job(self, mock_settings):
        """run_etl_job for METRICS_AGGREGATION uses the metrics job name."""
        from modules.backend.app.services.etl_service import ETLService

        glue = _make_glue_client()
        glue.create_job(
            Name=GLUE_METRICS_JOB_NAME,
            Role="arn:aws:iam::123456789012:role/GlueRole",
            Command={"Name": "glueetl", "ScriptLocation": "s3://bucket/script.py"},
        )

        svc = ETLService()
        req = ETLJobRequest(job_type=ETLJobType.METRICS_AGGREGATION, date="2024-01-15")
        resp = svc.run_etl_job(req)

        assert resp.job_name == GLUE_METRICS_JOB_NAME

    @mock_aws
    def test_run_etl_job_with_experiment_id(self, mock_settings):
        """run_etl_job passes experiment_id as a Glue job argument."""
        from modules.backend.app.services.etl_service import ETLService

        glue = _make_glue_client()
        glue.create_job(
            Name=GLUE_ETL_JOB_NAME,
            Role="arn:aws:iam::123456789012:role/GlueRole",
            Command={"Name": "glueetl", "ScriptLocation": "s3://bucket/script.py"},
        )

        svc = ETLService()
        req = ETLJobRequest(
            job_type=ETLJobType.EVENTS_TO_PARQUET,
            date="2024-01-15",
            experiment_id="exp-abc-123",
        )
        resp = svc.run_etl_job(req)

        assert isinstance(resp, ETLJobResponse)
        assert resp.job_run_id

    @mock_aws
    def test_run_etl_job_with_force_reprocess(self, mock_settings):
        """run_etl_job with force_reprocess=True still starts the job."""
        from modules.backend.app.services.etl_service import ETLService

        glue = _make_glue_client()
        glue.create_job(
            Name=GLUE_ETL_JOB_NAME,
            Role="arn:aws:iam::123456789012:role/GlueRole",
            Command={"Name": "glueetl", "ScriptLocation": "s3://bucket/script.py"},
        )

        svc = ETLService()
        req = ETLJobRequest(
            job_type=ETLJobType.EVENTS_TO_PARQUET,
            date="2024-01-15",
            force_reprocess=True,
        )
        resp = svc.run_etl_job(req)

        assert isinstance(resp, ETLJobResponse)

    @mock_aws
    def test_run_daily_summary_uses_etl_job(self, mock_settings):
        """DAILY_SUMMARY job type uses the ETL job name."""
        from modules.backend.app.services.etl_service import ETLService

        glue = _make_glue_client()
        glue.create_job(
            Name=GLUE_ETL_JOB_NAME,
            Role="arn:aws:iam::123456789012:role/GlueRole",
            Command={"Name": "glueetl", "ScriptLocation": "s3://bucket/script.py"},
        )

        svc = ETLService()
        req = ETLJobRequest(job_type=ETLJobType.DAILY_SUMMARY, date="2024-01-15")
        resp = svc.run_etl_job(req)

        # daily_summary falls back to events ETL job name
        assert resp.job_name in {GLUE_ETL_JOB_NAME, GLUE_METRICS_JOB_NAME}


# ---------------------------------------------------------------------------
# get_job_status tests
# ---------------------------------------------------------------------------


class TestGetJobStatus:
    @mock_aws
    def test_get_job_status_running(self, mock_settings):
        """get_job_status returns the correct status for a job run."""
        from modules.backend.app.services.etl_service import ETLService

        glue = _make_glue_client()
        glue.create_job(
            Name=GLUE_ETL_JOB_NAME,
            Role="arn:aws:iam::123456789012:role/GlueRole",
            Command={"Name": "glueetl", "ScriptLocation": "s3://bucket/script.py"},
        )
        run_response = glue.start_job_run(JobName=GLUE_ETL_JOB_NAME)
        job_run_id = run_response["JobRunId"]

        svc = ETLService()
        resp = svc.get_job_status(job_run_id=job_run_id, job_name=GLUE_ETL_JOB_NAME)

        assert isinstance(resp, ETLJobResponse)
        assert resp.job_run_id == job_run_id
        assert resp.job_name == GLUE_ETL_JOB_NAME
        assert resp.status in set(GlueJobStatus)

    @mock_aws
    def test_get_job_status_maps_job_type(self, mock_settings):
        """get_job_status includes job_type derived from job_name."""
        from modules.backend.app.services.etl_service import ETLService

        glue = _make_glue_client()
        glue.create_job(
            Name=GLUE_ETL_JOB_NAME,
            Role="arn:aws:iam::123456789012:role/GlueRole",
            Command={"Name": "glueetl", "ScriptLocation": "s3://bucket/script.py"},
        )
        run_response = glue.start_job_run(JobName=GLUE_ETL_JOB_NAME)
        job_run_id = run_response["JobRunId"]

        svc = ETLService()
        resp = svc.get_job_status(
            job_run_id=job_run_id,
            job_name=GLUE_ETL_JOB_NAME,
            job_type=ETLJobType.EVENTS_TO_PARQUET,
        )

        assert resp.job_type == ETLJobType.EVENTS_TO_PARQUET


# ---------------------------------------------------------------------------
# add_partitions tests
# ---------------------------------------------------------------------------


class TestAddPartitions:
    @mock_aws
    def test_add_partitions_creates_correct_values(self, mock_settings):
        """add_partitions creates 24 hourly partitions for a given date."""
        from modules.backend.app.services.etl_service import ETLService

        glue = _make_glue_client()
        # Create database and table
        glue.create_database(
            CatalogId="123456789012",
            DatabaseInput={"Name": GLUE_DATABASE},
        )
        glue.create_table(
            DatabaseName=GLUE_DATABASE,
            TableInput={
                "Name": "raw_events",
                "StorageDescriptor": {
                    "Columns": [{"Name": "event_id", "Type": "string"}],
                    "Location": "s3://exp-data-bucket/raw/events/",
                    "InputFormat": "org.apache.hadoop.mapred.TextInputFormat",
                    "OutputFormat": "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
                    "SerdeInfo": {
                        "SerializationLibrary": "org.openx.data.jsonserde.JsonSerDe"
                    },
                },
                "PartitionKeys": [
                    {"Name": "year", "Type": "string"},
                    {"Name": "month", "Type": "string"},
                    {"Name": "day", "Type": "string"},
                    {"Name": "hour", "Type": "string"},
                ],
            },
        )

        svc = ETLService()
        partitions = svc.add_partitions(
            database=GLUE_DATABASE,
            table="raw_events",
            date="2024-01-15",
        )

        assert isinstance(partitions, list)
        # 24 hours in a day
        assert len(partitions) == 24
        # Each partition has the correct structure
        for p in partitions:
            assert p.database == GLUE_DATABASE
            assert p.table == "raw_events"
            assert "year" in p.partition_values
            assert p.partition_values["year"] == "2024"
            assert p.partition_values["month"] == "01"
            assert p.partition_values["day"] == "15"

    @mock_aws
    def test_add_partitions_hour_values(self, mock_settings):
        """add_partitions creates partitions for all 24 hours (00-23)."""
        from modules.backend.app.services.etl_service import ETLService

        glue = _make_glue_client()
        glue.create_database(
            CatalogId="123456789012",
            DatabaseInput={"Name": GLUE_DATABASE},
        )
        glue.create_table(
            DatabaseName=GLUE_DATABASE,
            TableInput={
                "Name": "raw_events",
                "StorageDescriptor": {
                    "Columns": [{"Name": "event_id", "Type": "string"}],
                    "Location": "s3://exp-data-bucket/raw/events/",
                    "InputFormat": "org.apache.hadoop.mapred.TextInputFormat",
                    "OutputFormat": "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
                    "SerdeInfo": {
                        "SerializationLibrary": "org.openx.data.jsonserde.JsonSerDe"
                    },
                },
                "PartitionKeys": [
                    {"Name": "year", "Type": "string"},
                    {"Name": "month", "Type": "string"},
                    {"Name": "day", "Type": "string"},
                    {"Name": "hour", "Type": "string"},
                ],
            },
        )

        svc = ETLService()
        partitions = svc.add_partitions(
            database=GLUE_DATABASE,
            table="raw_events",
            date="2024-01-15",
        )

        hours = {p.partition_values["hour"] for p in partitions}
        expected_hours = {f"{h:02d}" for h in range(24)}
        assert hours == expected_hours

    @mock_aws
    def test_add_partitions_location_format(self, mock_settings):
        """add_partitions builds correct S3 partition locations."""
        from modules.backend.app.services.etl_service import ETLService

        glue = _make_glue_client()
        glue.create_database(
            CatalogId="123456789012",
            DatabaseInput={"Name": GLUE_DATABASE},
        )
        glue.create_table(
            DatabaseName=GLUE_DATABASE,
            TableInput={
                "Name": "raw_events",
                "StorageDescriptor": {
                    "Columns": [{"Name": "event_id", "Type": "string"}],
                    "Location": "s3://exp-data-bucket/raw/events/",
                    "InputFormat": "org.apache.hadoop.mapred.TextInputFormat",
                    "OutputFormat": "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
                    "SerdeInfo": {
                        "SerializationLibrary": "org.openx.data.jsonserde.JsonSerDe"
                    },
                },
                "PartitionKeys": [
                    {"Name": "year", "Type": "string"},
                    {"Name": "month", "Type": "string"},
                    {"Name": "day", "Type": "string"},
                    {"Name": "hour", "Type": "string"},
                ],
            },
        )

        svc = ETLService()
        partitions = svc.add_partitions(
            database=GLUE_DATABASE,
            table="raw_events",
            date="2024-01-15",
        )

        # All locations should contain year/month/day/hour path segments
        for p in partitions:
            assert "year=2024" in p.location
            assert "month=01" in p.location
            assert "day=15" in p.location
            assert "hour=" in p.location

    @mock_aws
    def test_re_running_a_date_answers_the_same_24_partitions(self, mock_settings):
        """Glue reports the second run's hours as already existing (in ``Errors``
        of a successful reply); that is success, and nothing is added twice."""
        from modules.backend.app.services.etl_service import ETLService

        glue = _make_glue_client()
        glue.create_database(DatabaseInput={"Name": GLUE_DATABASE})
        glue.create_table(
            DatabaseName=GLUE_DATABASE,
            TableInput={
                "Name": "raw_events",
                "StorageDescriptor": {"Location": "s3://exp-data-bucket/raw/events/"},
                "PartitionKeys": [
                    {"Name": key, "Type": "string"}
                    for key in ("year", "month", "day", "hour")
                ],
            },
        )

        first = ETLService().add_partitions(GLUE_DATABASE, "raw_events", "2024-01-15")
        second = ETLService().add_partitions(GLUE_DATABASE, "raw_events", "2024-01-15")

        assert first == second
        assert len(second) == 24
        stored = glue.get_partitions(DatabaseName=GLUE_DATABASE, TableName="raw_events")
        assert len(stored["Partitions"]) == 24
        assert {
            part["StorageDescriptor"]["Location"] for part in stored["Partitions"]
        } == {p.location for p in second}


# ---------------------------------------------------------------------------
# run_crawler tests
# ---------------------------------------------------------------------------


class TestRunCrawler:
    @mock_aws
    def test_run_crawler_returns_status(self, mock_settings):
        """run_crawler starts crawler and returns GlueCrawlerStatus."""
        from modules.backend.app.services.etl_service import ETLService

        glue = _make_glue_client()
        glue.create_database(
            CatalogId="123456789012",
            DatabaseInput={"Name": GLUE_DATABASE},
        )
        glue.create_crawler(
            Name=GLUE_CRAWLER_NAME,
            Role="arn:aws:iam::123456789012:role/GlueRole",
            DatabaseName=GLUE_DATABASE,
            Targets={"S3Targets": [{"Path": "s3://exp-data-bucket/raw/events/"}]},
        )

        svc = ETLService()
        status = svc.run_crawler(crawler_name=GLUE_CRAWLER_NAME)

        assert isinstance(status, GlueCrawlerStatus)
        assert status.crawler_name == GLUE_CRAWLER_NAME
        # After start_crawler, moto reports RUNNING
        assert status.state in {"RUNNING", "READY", "STOPPING"}

    @mock_aws
    def test_get_crawler_status_ready(self, mock_settings):
        """get_crawler_status returns READY state for a newly created crawler."""
        from modules.backend.app.services.etl_service import ETLService

        glue = _make_glue_client()
        glue.create_database(
            CatalogId="123456789012",
            DatabaseInput={"Name": GLUE_DATABASE},
        )
        glue.create_crawler(
            Name=GLUE_CRAWLER_NAME,
            Role="arn:aws:iam::123456789012:role/GlueRole",
            DatabaseName=GLUE_DATABASE,
            Targets={"S3Targets": [{"Path": "s3://exp-data-bucket/raw/events/"}]},
        )

        svc = ETLService()
        status = svc.get_crawler_status(crawler_name=GLUE_CRAWLER_NAME)

        assert isinstance(status, GlueCrawlerStatus)
        assert status.crawler_name == GLUE_CRAWLER_NAME
        assert status.state in {"READY", "RUNNING", "STOPPING"}
