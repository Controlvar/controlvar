"""The founder-run real-account check, checked offline.

``modules/backend/tests/live_warehouse/`` calls BigQuery and Snowflake and is
never collected by CI.  What it depends on that needs no network is checked
here, and this file IS collected (Module Tests, full-build):

* the directory is not collected by the commands CI runs, and is collected
  under ``-m warehouse_live``;
* without credentials it refuses to start, naming what to set, and runs nothing;
* the spend caps cannot be raised from the environment, and the session byte
  budget stops a query before it is sent;
* the evidence writer writes nothing when a credential -- or an encoding or
  shape of one -- is anywhere in what it would write (sentinels generated at
  runtime; none is committed);
* the parity and runner comparisons it makes fail when a number is wrong.
"""

from __future__ import annotations

import ast
import base64
import dataclasses
import json
import os
import re
import secrets
import subprocess  # nosec B404 - fixed argv, no shell
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import jwt
import numpy as np
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from modules.backend.app.services import warehouse_runner
from modules.backend.app.services.warehouse_query_builder import (
    AnalysisWindow,
    AssignmentMapping,
    BuiltQuery,
    MetricMapping,
    build_metric_query,
)
from modules.backend.app.services.warehouse_sufficient_stats import (
    parse_float,
    parse_metric_rows,
)
from modules.backend.app.warehouse.bigquery import (
    AccessTokenCache,
    BigQueryAdapter,
    BigQueryConnection,
    parse_service_account_json,
)
from modules.backend.app.warehouse.deadlines import Deadline
from modules.backend.app.warehouse.egress import OutboundResponse
from modules.backend.app.warehouse.redshift import RedshiftAdapter
from modules.backend.app.warehouse.snowflake import SnowflakeAdapter
from modules.backend.tests.live_warehouse import harness, selection
from modules.backend.tests.unit.warehouse import numeric_fixtures as nf
from modules.backend.tests.unit.warehouse.duckdb_adapter import DUCKDB_SQL

pytestmark = pytest.mark.unit

REPO = harness.REPO_ROOT
LIVE = "modules/backend/tests/live_warehouse"
LIVE_ENV = re.compile(r"^WAREHOUSE_LIVE_")


# -- collection --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("markexpr", "collected"),
    [
        (None, False),
        ("", False),
        ("unit", False),
        ("not unit", False),
        ("integration or not slow", False),
        ("not warehouse_live", False),
        ("warehouse_live or not unit", False),
        ("warehouse_live and (", False),  # unparsable: fails closed
        ("warehouse_live", True),
        ("warehouse_live and not slow", True),
        ("warehouse_live or unit", True),
    ],
)
def test_the_live_directory_is_collected_only_on_purpose(markexpr, collected):
    assert selection.selects_live_marker(markexpr) is collected


def test_the_ignore_hook_never_forces_collection():
    """``False`` from pytest_ignore_collect would override ``--ignore``; it returns None."""
    live = selection.LIVE_DIRECTORY / "test_bigquery_live.py"
    other = Path(__file__)
    assert selection.ignore_live_directory(live, None) is True
    assert selection.ignore_live_directory(selection.LIVE_DIRECTORY, "unit") is True
    assert selection.ignore_live_directory(live, "warehouse_live") is None
    assert selection.ignore_live_directory(other, None) is None
    assert selection.ignore_live_directory(other, "warehouse_live") is None


def _pytest(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    clean = {k: v for k, v in os.environ.items() if not LIVE_ENV.match(k)}
    clean.update(env or {})
    return subprocess.run(  # nosec B603 - our own interpreter and fixed arguments
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cov",
            "-p",
            "no:cacheprovider",
            *args,
        ],
        cwd=REPO,
        env=clean,
        capture_output=True,
        text=True,
        timeout=600,
    )


def _node_ids(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if "::" in line]


def test_the_module_tests_command_does_not_collect_the_live_check():
    """What Module Tests and full-build run (``pytest modules/backend/tests``)."""
    run = _pytest("--collect-only", "-q", "modules/backend/tests")
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-3000:]
    ids = _node_ids(run.stdout)
    assert any("unit/warehouse/" in i for i in ids), "collected nothing to compare with"
    assert [i for i in ids if "live_warehouse" in i] == []


def test_naming_the_directory_without_the_marker_collects_nothing():
    run = _pytest("--collect-only", "-q", LIVE)
    assert run.returncode == 5, run.stdout[-3000:] + run.stderr[-3000:]  # no tests
    assert _node_ids(run.stdout) == []


def test_the_marker_collects_every_check():
    run = _pytest("--collect-only", "-q", "-m", selection.MARKER, LIVE)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-3000:]
    names = [i.rsplit("::", 1)[-1] for i in _node_ids(run.stdout)]
    # The files in name order: test_bigquery_live, test_redshift_live,
    # test_snowflake_live.
    expected = [
        "test_" + c
        for target in ("bigquery", "redshift", "snowflake")
        for c in harness.EXPECTED_CHECKS[target]
    ]
    assert names == expected


def _pytest_commands(text: str) -> list[str]:
    """The text after ``pytest`` on each line that runs it."""
    found = []
    for line in text.splitlines():
        match = re.search(r"\bpytest\b(.*)$", line)
        if match and not line.lstrip().startswith("#"):
            found.append(match.group(1))
    return found


def test_no_ci_command_selects_the_live_marker():
    """No workflow, Makefile target or build script passes ``-m`` that selects it."""
    sources = sorted((REPO / ".github" / "workflows").glob("*.yml"))
    assert sources, "no workflows found: this check would pass having read nothing"
    sources += [REPO / "Makefile", REPO / "scripts" / "core_build.sh"]
    selecting = []
    for path in sources:
        for rest in _pytest_commands(path.read_text(encoding="utf-8")):
            for expr in re.findall(r"(?:^|\s)-m\s+(\"[^\"]*\"|'[^']*'|\S+)", rest):
                if selection.selects_live_marker(expr.strip("'\"")):
                    selecting.append(f"{path.name}: -m {expr}")
    assert selecting == []
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    addopts = re.search(r'^addopts\s*=\s*"([^"]*)"', pyproject, re.M)
    assert addopts is not None and selection.MARKER not in addopts.group(1)
    assert f'"{selection.MARKER}:' in pyproject  # the marker is declared


# -- refusing to start --------------------------------------------------------------


def test_without_credentials_the_check_refuses_and_runs_nothing():
    run = _pytest("-q", "-m", selection.MARKER, LIVE)
    out = run.stdout + run.stderr
    assert run.returncode == 4, out[-3000:]
    assert "refused to start" in out and harness.ENV_TARGET in out
    assert not re.search(r"\d+ (passed|failed|skipped|error)", out), out[-3000:]


def test_a_target_without_its_variables_names_every_one_missing(tmp_path):
    run = _pytest(
        "-q",
        "-m",
        selection.MARKER,
        LIVE,
        env={harness.ENV_TARGET: "bigquery", harness.ENV_EVIDENCE_DIR: str(tmp_path)},
    )
    out = run.stdout + run.stderr
    assert run.returncode == 4, out[-3000:]
    for name in harness.BQ_REQUIRED:
        assert name in out
    assert list(tmp_path.iterdir()) == []  # no evidence for a run that never started


@pytest.mark.parametrize(
    "target",
    [
        f"{LIVE}/test_bigquery_live.py",
        f"{LIVE}/test_bigquery_live.py::test_bq_sign_in",
    ],
    ids=["file", "node-id"],
)
@pytest.mark.parametrize("markexpr", [None, "unit", "not warehouse_live"])
def test_a_named_live_file_without_the_marker_refuses_even_with_credentials(
    tmp_path, target, markexpr
):
    """pytest_ignore_collect does not see named paths; the second gate must.

    Complete (fake) credentials are exported, as they are mid-check: without
    the gate this would reach the adapters.
    """
    key_file = tmp_path / "sa.json"
    key_file.write_text(_sa_json())
    evidence = tmp_path / "evidence"
    env = {
        **_bq_env(key_file),
        harness.ENV_EVIDENCE_DIR: str(evidence),
        harness.ENV_BQ_LOCATION: "US",
        # A billing project BigQueryConnection refuses before any request is
        # built: if this gate ever regresses, the session fails offline
        # instead of sending the fake key to Google.
        harness.ENV_BQ_PROJECT: "NOT_A_PROJECT",
    }
    args = ["-q", target] if markexpr is None else ["-q", "-m", markexpr, target]
    run = _pytest(*args, env=env)
    out = run.stdout + run.stderr
    assert run.returncode == 4, out[-3000:]
    assert f"without -m {selection.MARKER}" in out, out[-3000:]
    assert not re.search(r"\d+ (passed|failed|error)", out), out[-3000:]
    assert not evidence.exists()  # no session was started


def test_a_named_live_file_with_the_marker_passes_the_second_gate(tmp_path):
    """The gate is not a blanket refusal: with -m it goes on to read credentials."""
    run = _pytest(
        "-q",
        "-m",
        selection.MARKER,
        f"{LIVE}/test_bigquery_live.py::test_bq_sign_in",
        env={harness.ENV_EVIDENCE_DIR: str(tmp_path)},
    )
    out = run.stdout + run.stderr
    assert run.returncode == 4, out[-3000:]
    assert harness.ENV_TARGET in out and "without -m" not in out, out[-3000:]


def test_load_config_refusals(tmp_path):
    with pytest.raises(harness.LiveConfigError, match=harness.ENV_TARGET):
        harness.load_config({})
    with pytest.raises(harness.LiveConfigError, match="athena"):
        harness.load_config({harness.ENV_TARGET: "athena"})
    with pytest.raises(harness.LiveConfigError) as missing:
        harness.load_config({harness.ENV_TARGET: "snowflake"})
    for name in harness.SF_REQUIRED:
        assert name in str(missing.value)
    assert harness.ENV_SF_KEY_PASSPHRASE not in str(missing.value)  # optional
    with pytest.raises(harness.LiveConfigError) as missing:
        harness.load_config({harness.ENV_TARGET: "redshift"})
    for name in harness.RS_REQUIRED:
        assert name in str(missing.value)
    assert harness.ENV_RS_SESSION_TOKEN not in str(missing.value)  # optional
    assert harness.ENV_RS_TIMEOUT not in str(missing.value)  # has a default


#: A complete (fake) Redshift configuration; the identity is a dummy.
def _rs_env(**extra: str) -> dict:
    env = {
        harness.ENV_TARGET: "redshift",
        harness.ENV_RS_REGION: "eu-west-1",
        harness.ENV_RS_ROLE_ARN: (
            "arn:aws:iam::123456789012:role/controlvar-warehouse/live-check"
        ),
        harness.ENV_RS_EXTERNAL_ID: "exp-" + "0" * 32,
        harness.ENV_RS_DATABASE: "dev",
        harness.ENV_RS_SCHEMA: "live_check",
        harness.ENV_RS_WORKGROUP: "live-check",
        harness.ENV_RS_CLUSTER: "live-check-cluster",
        harness.ENV_RS_DB_USER: "live_reader",
        harness.ENV_RS_USAGE_LIMIT: "wg-limit-1, lmt-cluster-1",
        harness.ENV_RS_ACCESS_KEY_ID: "AKIDUMMYDUMMYDUMMY00",
        harness.ENV_RS_SECRET_ACCESS_KEY: "dummy-secret-" + secrets.token_hex(8),
    }
    env.update(extra)
    return env


def test_the_redshift_check_requires_a_usage_limit_and_caps_its_time():
    config = harness.load_config(_rs_env())
    assert config.usage_limits == ("wg-limit-1", "lmt-cluster-1")
    assert config.query_timeout_seconds == harness.RS_DEFAULT_TIMEOUT_SECONDS
    assert harness.ENV_RS_USAGE_LIMIT in harness.RS_REQUIRED
    env = _rs_env()
    del env[harness.ENV_RS_USAGE_LIMIT]
    with pytest.raises(harness.LiveConfigError) as missing:
        harness.load_config(env)
    assert harness.ENV_RS_USAGE_LIMIT in str(missing.value)
    for bad in (",", " , ", "limit one", "lmt;1"):
        with pytest.raises(harness.LiveConfigError, match=harness.ENV_RS_USAGE_LIMIT):
            harness.load_config(_rs_env(**{harness.ENV_RS_USAGE_LIMIT: bad}))
    with pytest.raises(harness.LiveConfigError, match="hard cap"):
        harness.load_config(
            _rs_env(
                **{harness.ENV_RS_TIMEOUT: str(harness.RS_HARD_MAX_TIMEOUT_SECONDS + 1)}
            )
        )
    # The identity's keys are searched for in the evidence; none is in public().
    assert config.secret_access_key in harness.config_secrets(config)
    assert config.access_key_id in harness.config_secrets(config)
    assert config.secret_access_key not in json.dumps(config.public())
    assert config.secret_access_key not in repr(config)


def test_a_redshift_session_without_a_usage_limit_stops_before_any_test(tmp_path):
    """With every other variable set, as mid-check: no usage limit, no session.

    Every AWS client the session could build is pointed at a closed local
    port, so if the refusal ever broke, the session would fail here without
    a request leaving the machine (the connector's own clients refuse an
    endpoint override outright).
    """
    env = _rs_env(
        **{
            harness.ENV_EVIDENCE_DIR: str(tmp_path / "evidence"),
            "AWS_ENDPOINT_URL": "https://127.0.0.1:1",
        }
    )
    del env[harness.ENV_RS_USAGE_LIMIT]
    run = _pytest("-q", "-m", selection.MARKER, LIVE, env=env)
    out = run.stdout + run.stderr
    assert run.returncode == 4, out[-3000:]
    assert "refused to start" in out and harness.ENV_RS_USAGE_LIMIT in out, out[-3000:]
    assert not re.search(r"\d+ (passed|failed|skipped|error)", out), out[-3000:]
    assert not (tmp_path / "evidence").exists()


def _sa_json(key: rsa.RSAPrivateKey | None = None) -> str:
    key = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    return json.dumps(
        {
            "type": "service_account",
            "project_id": "experimently-bq-sandbox",
            "private_key_id": secrets.token_hex(20),
            "private_key": pem,
            "client_email": "experimently-ci@experimently-bq-sandbox.iam.gserviceaccount.com",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )


def _bq_env(key_file: Path, **extra: str) -> dict:
    return {
        harness.ENV_TARGET: "bigquery",
        harness.ENV_BQ_KEY_FILE: str(key_file),
        harness.ENV_BQ_PROJECT: "experimently-bq-sandbox",
        harness.ENV_BQ_DATASET: "experimently_live_check",
        **extra,
    }


def test_spend_caps_cannot_be_raised_from_the_environment(tmp_path):
    key_file = tmp_path / "sa.json"
    key_file.write_text(_sa_json())
    config = harness.load_config(_bq_env(key_file))
    assert config.max_bytes_per_query == harness.BQ_DEFAULT_MAX_BYTES
    assert (
        harness.load_config(
            _bq_env(
                key_file, **{harness.ENV_BQ_MAX_BYTES: str(harness.BQ_HARD_MAX_BYTES)}
            )
        ).max_bytes_per_query
        == harness.BQ_HARD_MAX_BYTES
    )
    for bad in (str(harness.BQ_HARD_MAX_BYTES + 1), "0", "-5", "1e9", "lots"):
        with pytest.raises(harness.LiveConfigError):
            harness.load_config(_bq_env(key_file, **{harness.ENV_BQ_MAX_BYTES: bad}))
    with pytest.raises(harness.LiveConfigError, match="hard cap"):
        harness._int_setting(
            {harness.ENV_SF_TIMEOUT: str(harness.SF_HARD_MAX_TIMEOUT_SECONDS + 1)},
            harness.ENV_SF_TIMEOUT,
            harness.SF_DEFAULT_TIMEOUT_SECONDS,
            harness.SF_HARD_MAX_TIMEOUT_SECONDS,
        )


def test_keys_and_evidence_stay_outside_the_repository(tmp_path):
    inside = REPO / "modules" / "backend" / "tests" / "live_warehouse" / "__init__.py"
    with pytest.raises(harness.LiveConfigError, match="inside the repository"):
        harness.load_config(_bq_env(inside))
    with pytest.raises(harness.LiveConfigError, match="not a file"):
        harness.load_config(_bq_env(tmp_path / "absent.json"))
    with pytest.raises(harness.LiveConfigError, match="inside the repository"):
        harness.evidence_root({harness.ENV_EVIDENCE_DIR: str(REPO / "evidence")})
    assert harness.evidence_root({harness.ENV_EVIDENCE_DIR: str(tmp_path)}) == tmp_path
    assert not harness.is_inside(harness.evidence_root({}), REPO)
    assert harness.main(["--out", str(REPO / "fixture-out")]) == 2
    assert not (REPO / "fixture-out").exists()


# -- the evidence writer never writes a credential ----------------------------------


@pytest.fixture(scope="module")
def sentinels():
    """Credentials made at runtime: a key, its SA JSON, a signed token, an access token."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sa_json = _sa_json(key)
    data = json.loads(sa_json)
    token = jwt.encode(
        {"iss": data["client_email"], "n": secrets.token_hex(8)}, key, algorithm="RS256"
    )
    access = "ya29." + secrets.token_urlsafe(48)
    return SimpleNamespace(
        sa_json=sa_json,
        pem=data["private_key"],
        key_id=data["private_key_id"],
        jwt=token,
        access=access,
    )


def _recorder(tmp_path, known) -> harness.EvidenceRecorder:
    return harness.EvidenceRecorder(
        target="bigquery",
        run_id="wl-bigquery-test",
        root=tmp_path,
        connection={"billing_project": "experimently-bq-sandbox"},
        secrets=list(known),
    )


def _written(tmp_path) -> list[Path]:
    return [p for p in tmp_path.rglob("*") if p.is_file()]


@pytest.mark.parametrize(
    "plant",
    [
        "the key id in a check",
        "the key id base64-encoded",
        "the key id urlsafe-base64-encoded",
        "the whole SA JSON in a failure",
        "one PEM body line in an answer body",
        "the PEM body joined",
        "a signed token in a recorded answer",
        "an access token in a nested list",
        "a PEM block, unregistered",
        "a signed token, unregistered",
        "an access token, unregistered",
    ],
)
def test_the_evidence_refuses_every_form_of_a_credential(tmp_path, sentinels, plant):
    registered = [sentinels.sa_json, sentinels.pem, sentinels.key_id]
    body_lines = [
        line
        for line in sentinels.pem.splitlines()
        if line and not line.startswith("-----")
    ]
    recorder = _recorder(tmp_path, registered)
    recorder.record("bq_sign_in", signed_in=True)
    planted = {
        "the key id in a check": lambda: recorder.record(
            "bq_sign_in", note=sentinels.key_id
        ),
        "the key id base64-encoded": lambda: recorder.record(
            "bq_sign_in", note=base64.b64encode(sentinels.key_id.encode()).decode()
        ),
        "the key id urlsafe-base64-encoded": lambda: recorder.record(
            "bq_sign_in",
            note=base64.urlsafe_b64encode(sentinels.key_id.encode())
            .decode()
            .rstrip("="),
        ),
        "the whole SA JSON in a failure": lambda: recorder.record(
            "bq_wire_probe", failure="AssertionError: " + sentinels.sa_json
        ),
        "one PEM body line in an answer body": lambda: recorder.add_exchange(
            {"check": "x", "request": {}, "status": 200, "body": {"v": body_lines[3]}}
        ),
        "the PEM body joined": lambda: recorder.record("x", v="".join(body_lines)),
        "a signed token in a recorded answer": lambda: (
            recorder.register_secret(sentinels.jwt),
            recorder.add_exchange(
                {
                    "check": "x",
                    "request": {},
                    "status": 200,
                    "body": {"t": sentinels.jwt},
                }
            ),
        ),
        "an access token in a nested list": lambda: (
            recorder.register_secret(sentinels.access),
            recorder.record("x", deep=[{"a": [sentinels.access]}]),
        ),
        "a PEM block, unregistered": lambda: recorder.record(
            "x",
            v=serialization.load_pem_private_key(sentinels.pem.encode(), None)
            .private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
            .decode(),
        ),
        "a signed token, unregistered": lambda: recorder.record("x", v=sentinels.jwt),
        "an access token, unregistered": lambda: recorder.record(
            "x", v=sentinels.access
        ),
    }
    if plant.endswith("unregistered"):
        recorder.secrets = []
    planted[plant]()
    with pytest.raises(harness.EvidenceRefused) as refused:
        recorder.write()
    assert _written(tmp_path) == []
    message = str(refused.value)
    for secret in (sentinels.key_id, sentinels.jwt, sentinels.access, *body_lines):
        assert secret not in message


def test_clean_evidence_is_written_in_the_recorded_fixture_format(tmp_path, sentinels):
    recorder = _recorder(tmp_path, [sentinels.sa_json, sentinels.pem, sentinels.key_id])
    recorder.record(
        "bq_wire_probe", text="0.30000000000000004", job_id="experimently_abc"
    )
    recorder.outcome("bq_wire_probe", "passed")
    recorder.add_exchange(
        {
            "check": "bq_wire_probe",
            "request": {
                "method": "GET",
                "host": "bigquery.googleapis.com",
                "path": "/x",
            },
            "status": 200,
            "label": "get_queries_x",
            "body": {"jobComplete": True},
        }
    )
    path = recorder.write()
    document = json.loads(path.read_text())
    assert document["run_id"] == "wl-bigquery-test"
    assert document["outcome"] == "failed"  # most expected checks did not run
    assert "bq_sign_in" in document["expected_checks_not_run"]
    assert document["not_verified_rows"]["7"]["outcomes"]["bq_wire_probe"] == "passed"
    assert document["not_verified_rows"]["5"]["settled_by_this_run"] is None  # Athena
    recordings = list((path.parent / "recordings" / "bigquery").glob("*.json"))
    assert len(recordings) == 1
    fixture = json.loads(recordings[0].read_text())
    # What test_recorded_fixtures_declare_provenance accepts for a real recording.
    assert set(fixture) == {"_provenance", "status", "body"}
    assert fixture["_provenance"]["real_run_id"] == "wl-bigquery-test"
    for written in _written(tmp_path):
        text = written.read_text()
        assert sentinels.key_id not in text and "PRIVATE KEY" not in text


class _StubClient:
    """Answers each request from a list, as an OutboundClient would."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def request(
        self, method, url, *, headers=None, params=None, content=None, json_body=None
    ):
        self.requests.append({"method": method, "url": url, "json_body": json_body})
        status, body = self.answers.pop(0)
        return OutboundResponse(status, {}, json.dumps(body).encode())

    def close(self):
        return None


def test_recording_keeps_no_token_and_no_request_headers(tmp_path, sentinels):
    recorder = _recorder(tmp_path, [])
    inner = _StubClient(
        [
            (
                200,
                {
                    "access_token": sentinels.access,
                    "token_type": "Bearer",
                    "expires_in": 3599,
                },
            ),
            (200, {"kind": "bigquery#job", "jobReference": {"jobId": "j1"}}),
        ]
    )
    client = harness.RecordingClient(inner, recorder, lambda: "bq_sign_in")
    client.request(
        "POST",
        "https://oauth2.googleapis.com/token",
        content=b"assertion=" + sentinels.jwt.encode(),
    )
    client.request(
        "POST",
        "https://bigquery.googleapis.com/bigquery/v2/projects/p/jobs",
        headers={"Authorization": "Bearer " + sentinels.access},
        json_body={
            "configuration": {"dryRun": True, "query": {"maximumBytesBilled": "10"}}
        },
    )
    token_exchange, job_exchange = recorder.exchanges
    assert "body" not in token_exchange
    assert token_exchange["body_keys"] == ["access_token", "expires_in", "token_type"]
    assert sentinels.access in recorder.secrets  # the search will look for it
    assert job_exchange["request"]["dry_run"] is True
    assert job_exchange["request"]["maximum_bytes_billed"] == "10"
    path = recorder.write()
    for written in _written(tmp_path):
        text = written.read_text()
        assert sentinels.access not in text and sentinels.jwt not in text
    assert (
        json.loads(path.read_text())["exchanges"][1]["body"]["kind"] == "bigquery#job"
    )


# -- the BigQuery session budget -----------------------------------------------------


def _dry_run_answer(nbytes: int):
    return (
        200,
        {
            "statistics": {
                "query": {"statementType": "SELECT", "totalBytesProcessed": str(nbytes)}
            }
        },
    )


def _capped(sentinels, answers, budget):
    key = parse_service_account_json(sentinels.sa_json)
    tokens = AccessTokenCache()
    tokens.put(key.cache_key, "stub-token", 3600)
    stub = _StubClient(answers)
    adapter = harness.capped_bigquery_adapter(
        BigQueryConnection("experimently-bq-sandbox", "US", 100 * harness.MIB, 60),
        key,
        client_factory=lambda deadline: stub,
        token_cache=tokens,
        budget=budget,
    )
    return adapter, stub


def test_the_session_budget_stops_a_query_before_it_is_sent(sentinels):
    budget = harness.ByteBudget(limit=5 * harness.MIB)
    adapter, stub = _capped(sentinels, [_dry_run_answer(6 * harness.MIB)], budget)
    built = BuiltQuery(kind="preview", dialect="bigquery", sql="SELECT 1 AS v")
    with pytest.raises(harness.BudgetExceeded):
        adapter.run_query(built, Deadline(60))
    assert len(stub.requests) == 1  # the dry run; no job was inserted
    assert stub.requests[0]["json_body"]["configuration"]["dryRun"] is True
    assert budget.spent == 0


def test_the_session_budget_is_charged_by_every_dry_run(sentinels):
    budget = harness.ByteBudget(limit=10 * harness.MIB)
    adapter, stub = _capped(
        sentinels,
        [_dry_run_answer(4 * harness.MIB), _dry_run_answer(4 * harness.MIB)],
        budget,
    )
    adapter.dry_run(stub, "SELECT 1")
    adapter.dry_run(stub, "SELECT 1")
    assert budget.spent == 8 * harness.MIB
    assert [d.total_bytes_processed for d in adapter.dry_runs] == [4 * harness.MIB] * 2
    with pytest.raises(harness.BudgetExceeded):
        budget.charge(3 * harness.MIB)


# -- what the live checks compare, against DuckDB --------------------------------------


def test_the_comparison_ignores_only_the_session_offset():
    base = {"diagnostics": {"units": 10, "session_offset": None}, "p": 0.25}
    snowflake = {"diagnostics": {"units": 10, "session_offset": "+00:00"}, "p": 0.25}
    assert harness.compare_results(snowflake, base) == []
    assert harness.compare_results({**snowflake, "p": 0.26}, base) != []
    wrong_units = {"diagnostics": {"units": 11, "session_offset": "+00:00"}, "p": 0.25}
    assert harness.compare_results(wrong_units, base) != []


def test_the_live_data_is_f_mean_1():
    control, treatment = harness.live_fixture()
    expected_c, expected_t = nf.f_mean_1()
    assert np.array_equal(control, expected_c) and np.array_equal(treatment, expected_t)


def test_the_fixture_csvs_hold_exact_values(tmp_path):
    manifest = harness.write_fixture_csvs(tmp_path, n_per_arm=50)
    control, treatment = harness.live_fixture(50)
    assert manifest["events.csv"]["rows"] == manifest["exposures.csv"]["rows"] == 100
    lines = (tmp_path / "events.csv").read_text().splitlines()
    assert lines[0] == ",".join(harness.EVENT_COLUMNS)
    amounts = [float(line.split(",")[2]) for line in lines[1:]]
    assert amounts == control.tolist() + treatment.tolist()  # exact, not approximate


def test_the_wire_probe_statement_reads_back_exactly():
    from modules.backend.tests.unit.warehouse.duckdb_adapter import DuckDBWarehouse

    text = (
        DuckDBWarehouse().con.execute(harness.wire_probe_sql(DUCKDB_SQL)).fetchone()[0]
    )
    assert text == harness.WIRE_PROBE_TEXT
    assert parse_float(text, "v")[0] == harness.WIRE_PROBE_VALUE


def _small():
    return harness.live_fixture(400)


def test_mean_parity_passes_on_the_oracle_and_fails_on_a_wrong_sum():
    control, treatment = _small()
    client, exposures, events, columns = harness.duckdb_oracle(control, treatment)
    a, e = columns["exposures"], columns["events"]
    built = build_metric_query(
        DUCKDB_SQL,
        AssignmentMapping(
            exposures, a["user_id"], a["experiment_key"], a["variant"], a["exposed_at"]
        ),
        MetricMapping(events, e["user_id"], e["event_at"], "mean", e["amount"]),
        harness.EXPERIMENT_KEY,
        AnalysisWindow(harness.WINDOW_START, harness.WINDOW_END),
    )
    rows = list(client.adapter.run_query(built, None).rows)
    reference = harness.exact_reference(control, treatment)
    errors = harness.mean_parity_errors(parse_metric_rows(rows), reference)
    assert max(errors.values()) <= harness.F_MEAN_1_TOLERANCE, errors
    # One ulp-scale slip in one sum is caught.
    rows[0] = {**rows[0], "sum_d": repr(float(rows[0]["sum_d"]) * (1 + 1e-9))}
    errors = harness.mean_parity_errors(parse_metric_rows(rows), reference)
    assert max(errors.values()) > harness.F_MEAN_1_TOLERANCE


def test_runner_parity_passes_on_the_oracle_and_fails_on_a_wrong_count():
    control, treatment = _small()
    first = harness.duckdb_oracle(control, treatment)
    second = harness.duckdb_oracle(control, treatment)
    results = [
        harness.execute_plan(harness.run_plan(c, x, v, cols), Deadline(60))["results"]
        for c, x, v, cols in (first, second)
    ]
    assert results[0]["metrics"][0]["computed"] is True
    assert harness.compare_results(*results) == []

    class OffByOne:
        def __init__(self, inner):
            self.inner = inner

        def run_query(self, built, deadline):
            answer = self.inner.run_query(built, deadline)
            rows = [dict(r) for r in answer.rows]
            if built.kind == "metric":
                rows[0]["n_converted"] = str(int(rows[0]["n_converted"]) - 1)
            return SimpleNamespace(rows=tuple(rows))

    client, exposures, events, cols = harness.duckdb_oracle(control, treatment)
    wrong = dataclasses.replace(client, adapter=OffByOne(client.adapter))
    shifted = harness.execute_plan(
        harness.run_plan(wrong, exposures, events, cols), Deadline(60)
    )
    assert harness.compare_results(shifted["results"], results[0]) != []


# -- what the live files rely on -------------------------------------------------------


LIVE_FILES = {
    "bigquery": "test_bigquery_live.py",
    "redshift": "test_redshift_live.py",
    "snowflake": "test_snowflake_live.py",
}


def test_every_expected_check_is_a_live_test_in_order():
    assert set(LIVE_FILES) == set(harness.TARGETS) == set(harness.EXPECTED_CHECKS)
    assert sorted(p.name for p in selection.LIVE_DIRECTORY.glob("test_*.py")) == sorted(
        LIVE_FILES.values()
    )
    rows = {c for row in harness.NOT_VERIFIED_ROWS.values() for c in row["checks"]}
    for target, filename in LIVE_FILES.items():
        tree = ast.parse(
            (selection.LIVE_DIRECTORY / filename).read_text(encoding="utf-8")
        )
        tests = [
            node.name[len("test_") :]
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        ]
        assert tuple(tests) == harness.EXPECTED_CHECKS[target]
        prefix = harness.CHECK_PREFIXES[target]
        assert all(t.startswith(prefix) for t in tests), tests
        assert {c for c in rows if c.startswith(prefix)} <= set(tests)
    assert all(
        any(c.startswith(p) for p in harness.CHECK_PREFIXES.values()) for c in rows
    )


def test_one_prefix_table_decides_which_checks_a_target_runs():
    """The session's deselection (conftest) and the evidence's settled rows
    (EvidenceRecorder.document) both read CHECK_PREFIXES; neither spells a
    prefix of its own."""
    prefixes = harness.CHECK_PREFIXES
    assert set(prefixes) == set(harness.TARGETS)
    assert len(set(prefixes.values())) == len(prefixes)
    conftest = (selection.LIVE_DIRECTORY / "conftest.py").read_text(encoding="utf-8")
    source = Path(harness.__file__).read_text(encoding="utf-8")
    for prefix in prefixes.values():
        assert f'"{prefix}"' not in conftest
        assert source.count(f'"{prefix}"') == 1, prefix  # the table's own entry
    assert "harness.CHECK_PREFIXES[" in conftest


def test_the_private_calls_the_live_check_makes_still_exist():
    """The live check calls a few non-public steps; a rename must fail here, not there."""
    for owner, names in (
        (
            BigQueryAdapter,
            (
                "dry_run",
                "_insert",
                "_wait",
                "_new_job_id",
                "run_query",
                "table_columns",
                "check_connection",
            ),
        ),
        (
            SnowflakeAdapter,
            ("_execute", "run_query", "table_columns", "check_connection"),
        ),
        (
            RedshiftAdapter,
            (
                "_execute",
                "_role_session",
                "_submit",
                "_wait",
                "_result",
                "run_query",
                "table_columns",
                "check_connection",
            ),
        ),
        (warehouse_runner, ("_execute", "RunPlan", "MetricPlan", "VariantInfo")),
    ):
        for name in names:
            assert callable(getattr(owner, name, None)), f"{owner.__name__}.{name}"


@pytest.mark.regression
def test_a_warehouse_without_a_visible_monitor_names_the_monitor_grant():
    """The refusal names the grant that makes the monitor visible to the role.

    On the first real run MONITOR on the warehouse was granted and the role
    still saw ``resource_monitor`` as null; MONITOR on the resource monitor
    itself is what shows it.
    """
    from modules.backend.tests.live_warehouse.test_snowflake_live import (
        _verify_spend_cap,
    )

    seen = []

    class Adapter:
        def _execute(self, sql, deadline):
            seen.append(sql)
            return "h", [{"name": "WH_A", "resource_monitor": None}], 0

    class Recorder:
        def record(self, *args, **kwargs):
            pass

    config = SimpleNamespace(warehouse="WH_A", role="ROLE_A", resource_monitor="MON_A")
    with pytest.raises(AssertionError) as refused:
        _verify_spend_cap(Adapter(), config, Recorder())
    message = str(refused.value)
    assert "GRANT MONITOR ON RESOURCE MONITOR MON_A TO ROLE ROLE_A;" in message
    assert "Nothing else has run." in message
    assert seen == ["SHOW WAREHOUSES LIKE 'WH_A'"]


class _StubbedPlatform:
    """A boto3 session whose clients answer from a Stubber (nothing is sent)."""

    def __init__(self, answers):
        self.answers = answers
        self.stubbers = []

    def client(self, service, region_name=None):
        import boto3
        from botocore.stub import Stubber

        client = boto3.session.Session(
            aws_access_key_id="AKIDUMMYDUMMYDUMMY00",
            aws_secret_access_key="dummy-secret",
        ).client(service, region_name=region_name)
        stubber = Stubber(client)
        for operation, expected, answer in self.answers[service]:
            stubber.add_response(operation, answer, expected)
        stubber.activate()
        self.stubbers.append(stubber)
        return client


def _usage_limit_answers(on_workgroup, on_cluster):
    arn = "arn:aws:redshift-serverless:eu-west-1:123456789012:workgroup/abc"
    return {
        "redshift-serverless": [
            (
                "get_workgroup",
                {"workgroupName": "live-check"},
                {"workgroup": {"workgroupArn": arn, "workgroupName": "live-check"}},
            ),
            (
                "list_usage_limits",
                {"resourceArn": arn},
                # The model's list has at least one entry: none is no key.
                (
                    {"usageLimits": [{"usageLimitId": i} for i in on_workgroup]}
                    if on_workgroup
                    else {}
                ),
            ),
        ],
        "redshift": [
            (
                "describe_usage_limits",
                {"ClusterIdentifier": "live-check-cluster"},
                {"UsageLimits": [{"UsageLimitId": i} for i in on_cluster]},
            )
        ],
    }


@pytest.mark.parametrize(
    ("on_workgroup", "on_cluster", "refused"),
    [
        (["wg-limit-1"], ["lmt-cluster-1"], None),
        ([], ["lmt-cluster-1"], "workgroup"),
        (["wg-limit-1"], ["someone-elses"], "cluster"),
    ],
    ids=["both-set", "no-workgroup-limit", "cluster-limit-not-named"],
)
def test_the_redshift_check_reads_the_usage_limits_back_before_anything_runs(
    on_workgroup, on_cluster, refused
):
    from modules.backend.tests.live_warehouse.test_redshift_live import (
        _verify_usage_limits,
    )

    config = harness.load_config(_rs_env())
    platform = _StubbedPlatform(_usage_limit_answers(on_workgroup, on_cluster))
    recorded = {}

    class Recorder:
        def record(self, check, **data):
            recorded[check] = data

    if refused is None:
        _verify_usage_limits(platform, config, Recorder())
    else:
        with pytest.raises(AssertionError) as stopped:
            _verify_usage_limits(platform, config, Recorder())
        message = str(stopped.value)
        assert f"on the {refused}" in message and "Nothing else has run." in message
        assert harness.ENV_RS_USAGE_LIMIT in message
    for stubber in platform.stubbers:
        stubber.assert_no_pending_responses()
    assert recorded["rs_spend_cap"] == {
        "declared": ["wg-limit-1", "lmt-cluster-1"],
        "workgroup": on_workgroup,
        "cluster": on_cluster,
    }


def test_aws_calls_are_recorded_and_sts_credentials_are_kept_out(tmp_path):
    """Through a real client: a Data API answer is recorded with its body; an
    STS answer only by its keys, its credentials registered for the search."""
    import boto3
    from botocore.stub import Stubber

    from modules.backend.app.warehouse import aws

    sentinel = "LIVETEMPCRED" + secrets.token_hex(8).upper()
    recorder = harness.EvidenceRecorder(
        target="redshift", run_id="wl-redshift-test", root=tmp_path
    )
    recording = harness.AwsRecording(recorder, lambda: "rs_sign_in")
    session = boto3.session.Session(
        aws_access_key_id="AKIDUMMYDUMMYDUMMY00", aws_secret_access_key="dummy-secret"
    )
    recording.install()
    try:
        sts = aws.make_client(session, "sts", "eu-west-1", warehouse="redshift")
        data = aws.make_client(
            session, "redshift-data", "eu-west-1", warehouse="redshift"
        )
    finally:
        recording.uninstall()
    assert aws.make_client.__name__ == "make_client"  # put back
    with Stubber(sts) as sts_stub, Stubber(data) as data_stub:
        sts_stub.add_response(
            "assume_role",
            {
                "Credentials": {
                    "AccessKeyId": "ASIA" + sentinel,
                    "SecretAccessKey": "secret-" + sentinel,
                    "SessionToken": "token-" + sentinel,
                    "Expiration": "2026-11-13T19:00:00Z",
                }
            },
        )
        data_stub.add_response("batch_execute_statement", {"Id": "abc"})
        sts.assume_role(
            RoleArn="arn:aws:iam::123456789012:role/x", RoleSessionName="s1"
        )
        data.batch_execute_statement(Sqls=["SELECT 1"], Database="dev")
    by_operation = {x["request"]["operation"]: x for x in recorder.exchanges}
    assert "body" not in by_operation["AssumeRole"]
    assert by_operation["AssumeRole"]["body_keys"] == ["Credentials"]
    assert by_operation["BatchExecuteStatement"]["body"] == {"Id": "abc"}
    assert by_operation["BatchExecuteStatement"]["check"] == "rs_sign_in"
    assert "secret-" + sentinel in recorder.secrets
    recorder.record("rs_sign_in", note="token-" + sentinel)
    with pytest.raises(harness.EvidenceRefused):
        recorder.write()
    assert _written(tmp_path) == []


def test_new_run_ids_are_distinct_and_name_the_target():
    ids = {harness.new_run_id("snowflake") for _ in range(20)}
    assert len(ids) == 20
    assert all(re.fullmatch(r"wl-snowflake-\d{8}T\d{6}Z-[0-9a-f]{8}", i) for i in ids)
    assert uuid.UUID(int=0)  # the runner's run id placeholder is a valid UUID
