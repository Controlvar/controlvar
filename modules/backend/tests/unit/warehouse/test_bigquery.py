"""The BigQuery connector, against recorded responses on a fake network.

Every test drives the real client (destination check, address check,
deadline, body limit) over :class:`GoogleFake`; nothing reaches Google.
"""

from __future__ import annotations

import json
import logging
import traceback
from datetime import datetime, timezone

import httpcore
import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from modules.backend.app.core.warehouse_identifiers import WarehouseQueryRefused
from modules.backend.app.models.warehouse_connection import WarehouseConnection
from modules.backend.app.services.warehouse_query_builder import (
    BIGQUERY_SQL,
    AnalysisWindow,
    AssignmentMapping,
    BuiltQuery,
    MetricMapping,
    build_diagnostics_query,
    build_metric_query,
)
from modules.backend.app.services.warehouse_sufficient_stats import (
    parse_diagnostics_rows,
    parse_metric_rows,
)
from modules.backend.app.warehouse import bigquery as bq
from modules.backend.app.warehouse import egress
from modules.backend.app.warehouse.bigquery import (
    API_ROOT,
    CANCEL_RESERVE_SECONDS,
    JOB_LABELS,
    TOKEN_URL,
    AccessTokenCache,
    BigQueryAdapter,
    BigQueryConfigRefused,
    BigQueryConnection,
    ServiceAccountKey,
    parse_service_account_json,
)
from modules.backend.app.warehouse.deadlines import Deadline
from modules.backend.app.warehouse.egress import GuardedTransport, OutboundClient
from modules.backend.app.warehouse.errors import WarehouseError, WarehouseErrorCode
from modules.backend.tests.unit.warehouse.conftest import FakeClock
from modules.backend.tests.unit.warehouse.google_fake import (
    SENTINEL,
    GoogleFake,
    Script,
    client_factory,
    recorded,
)

pytestmark = pytest.mark.unit

C = WarehouseErrorCode
EMAIL = "analysis-reader@acme-analytics.iam.gserviceaccount.com"
KEY_ID = "0123456789abcdef0123456789abcdef01234567"
PROJECT = "acme-analytics"


# -- keys -------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def pem(rsa_key) -> str:
    return rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def sa_json(pem: str, **overrides) -> str:
    data = {
        # Built from the connector's constant, so no key-shaped JSON is committed.
        "type": bq.SERVICE_ACCOUNT_TYPE,
        "project_id": PROJECT,
        "private_key_id": KEY_ID,
        "private_key": pem,
        "client_email": EMAIL,
        "client_id": "123456789012345678901",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": TOKEN_URL,
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/x",
        "universe_domain": "googleapis.com",
    }
    for name, value in overrides.items():
        if value is _DROP:
            data.pop(name, None)
        else:
            data[name] = value
    return json.dumps(data)


_DROP = object()


@pytest.fixture
def key(pem) -> ServiceAccountKey:
    return parse_service_account_json(sa_json(pem))


CONNECTION = BigQueryConnection(
    billing_project=PROJECT,
    location="US",
    max_bytes_per_query=50_000_000,
    query_timeout_seconds=300,
)


def adapter(key, fake, **kwargs) -> BigQueryAdapter:
    kwargs.setdefault("token_cache", AccessTokenCache())
    kwargs.setdefault("wall_clock", lambda: 1_759_000_000.0)
    return BigQueryAdapter(
        kwargs.pop("connection", CONNECTION),
        key,
        client_factory=client_factory(fake),
        **kwargs,
    )


def metric_query() -> BuiltQuery:
    return build_metric_query(
        BIGQUERY_SQL,
        AssignmentMapping(
            table=("acme-data", "events", "exposures"),
            unit_id="user_id",
            experiment_key="experiment",
            variant="variant",
            exposed_at="exposed_at",
        ),
        MetricMapping(
            table=("acme-data", "events", "purchases"),
            unit_id="user_id",
            event_at="purchased_at",
            metric_type="proportion",
        ),
        "checkout-test",
        AnalysisWindow(
            start=datetime(2026, 9, 1, tzinfo=timezone.utc),
            end=datetime(2026, 9, 15, tzinfo=timezone.utc),
        ),
    )


HAPPY = {
    "token": ["token_ok"],
    "dry_run": ["dry_run_select"],
    "insert": ["insert_running"],
    "results": ["query_results_incomplete", "query_results_metric"],
    "job": ["job_done"],
    "cancel": ["cancel_ok"],
    "table": ["table_get"],
}


def happy(**changes):
    routes = {k: list(v) for k, v in HAPPY.items()}
    routes.update(changes)
    return Script(routes)


def jobs_posts(fake: GoogleFake):
    return [r for r in fake.requests if r.method == "POST" and r.path.endswith("/jobs")]


# -- the pasted key ---------------------------------------------------------


def test_only_the_four_fields_are_kept(pem):
    """The stored blob is exactly client_email, private_key_id, private_key, project_id."""
    key = parse_service_account_json(sa_json(pem))
    blob = json.loads(key.to_blob())
    assert set(blob) == {"client_email", "private_key_id", "private_key", "project_id"}
    assert blob["client_email"] == EMAIL
    assert blob["project_id"] == PROJECT
    for dropped in ("token_uri", "universe_domain", "auth_uri", "client_x509_cert_url"):
        assert dropped not in key.to_blob().decode()
    assert ServiceAccountKey.from_blob(key.to_blob()) == key


def test_the_stored_credential_holds_exactly_the_four_fields(pem):
    """Through the model: encrypted with the operator key, decrypted to four fields."""
    keys = Fernet.generate_key().decode()
    row = WarehouseConnection(warehouse_type="bigquery")
    row.set_credentials(parse_service_account_json(sa_json(pem)).to_blob(), keys=keys)
    assert b"private_key" not in bytes(row.credentials_ciphertext)
    stored = json.loads(row.get_credentials(keys=keys))
    assert sorted(stored) == [
        "client_email",
        "private_key",
        "private_key_id",
        "project_id",
    ]


def test_a_blob_with_any_other_field_is_refused(key):
    blob = json.loads(key.to_blob())
    blob["token_uri"] = "https://example.com/token"
    with pytest.raises(WarehouseError) as err:
        ServiceAccountKey.from_blob(json.dumps(blob).encode())
    assert err.value.code is C.INTERNAL


@pytest.mark.parametrize(
    "token_uri",
    [
        "http://127.0.0.1:1/token",
        "https://oauth2.googleapis.com/token/",
        "https://oauth2.googleapis.com.example.com/token",
        "https://OAUTH2.googleapis.com/token",
        "https://oauth2.googleapis.com/token\n",
        "https://oauth2.googleapis.com:443/token",
        "https://sts.googleapis.com/v1/token",
        "",
        None,
        123,
    ],
)
def test_token_uri_other_than_google_refused(pem, token_uri):
    with pytest.raises(BigQueryConfigRefused) as err:
        parse_service_account_json(sa_json(pem, token_uri=token_uri))
    assert err.value.code == "token_endpoint_not_allowed"
    assert err.value.to_body()["field"] == "service_account_json"


@pytest.mark.parametrize("universe", ["example.com", "googleapis.com.", "", None])
def test_universe_other_than_google_refused(pem, universe):
    with pytest.raises(BigQueryConfigRefused) as err:
        parse_service_account_json(sa_json(pem, universe_domain=universe))
    assert err.value.code == "token_endpoint_not_allowed"


def test_a_key_without_token_uri_or_universe_is_accepted(pem):
    key = parse_service_account_json(
        sa_json(pem, token_uri=_DROP, universe_domain=_DROP)
    )
    assert key.client_email == EMAIL


class _ProbeOrGoogle(httpcore.NetworkBackend):
    """Loopback goes, for real, to the probe; every other address to the fake Google."""

    def __init__(self, probe, google: GoogleFake) -> None:
        self._probe = probe
        self._google = google
        self._real = httpcore.SyncBackend()

    def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None
    ):
        if host == "127.0.0.1":
            return self._real.connect_tcp(host, self._probe.port, timeout=1.0)
        return self._google.connect_tcp(host, port)

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("no")

    def sleep(self, seconds):
        pass


def test_token_uri_other_than_google_refused_and_probe_receives_nothing(
    pem, probe, monkeypatch
):
    """Even a key naming a reachable endpoint signs in only at the constant URL.

    The layers in front are opened so the probe *can* be reached: the key's
    endpoint check is switched off, the destination check allows any URL and
    the address check allows loopback.  A key whose ``token_uri`` is the
    probe then goes through parsing, and the connector must still exchange
    it at ``oauth2.googleapis.com`` -- the probe accepts nothing.
    """
    from modules.backend.tests.unit.warehouse.test_egress import _wait_for_probe

    monkeypatch.setattr(bq, "check_token_endpoint", lambda data: None)
    original = egress.refused_address
    monkeypatch.setattr(
        egress,
        "refused_address",
        lambda value: value != "127.0.0.1" and original(value),
    )
    probe_url = f"http://127.0.0.1:{probe.port}/token"
    key = parse_service_account_json(sa_json(pem, token_uri=probe_url))

    google = GoogleFake(happy())

    def resolve(host, port):
        if host == "127.0.0.1":
            return [("127.0.0.1", probe.port)]
        return [("93.184.216.34", port)]

    def make(deadline):
        return OutboundClient(
            deadline,
            warehouse="bigquery",
            transport=GuardedTransport(
                deadline,
                resolver=resolve,
                network_backend=_ProbeOrGoogle(probe, google),
                destination_check=lambda url: None,
            ),
        )

    connector = BigQueryAdapter(
        CONNECTION, key, client_factory=make, token_cache=AccessTokenCache()
    )
    outcome = None
    try:
        connector.check_connection(Deadline(30))
    except WarehouseError as exc:
        outcome = exc.code
    _wait_for_probe(probe)
    assert probe.accepted == 0
    token_requests = [r for r in google.requests if r.host == "oauth2.googleapis.com"]
    assert len(token_requests) == 1 and token_requests[0].path == "/token"
    assert outcome is None


@pytest.mark.parametrize(
    "kind", ["authorized_user", "external_account", "impersonated_service_account"]
)
def test_other_credential_types_refused(pem, kind):
    with pytest.raises(BigQueryConfigRefused) as err:
        parse_service_account_json(sa_json(pem, type=kind))
    assert err.value.code == "unsupported_credential_type"


def _ec_pem() -> str:
    return (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )


def _pkcs1_pem(rsa_key) -> str:
    return rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()


@pytest.mark.parametrize(
    "case",
    [
        "not json",
        "list",
        "wrong type",
        "email host",
        "email newline",
        "email missing",
        "key id quote",
        "key id missing",
        "key missing",
        "key garbage",
        "key ec",
        "key pkcs1",
        "key small",
        "project bad",
        "duplicate token_uri",
        "too large",
    ],
)
def test_malformed_service_account_refused_without_repeating_it(pem, rsa_key, case):
    small = (
        rsa.generate_private_key(public_exponent=65537, key_size=1024)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    text = {
        "not json": "{" + SENTINEL,
        "list": "[]",
        "wrong type": sa_json(pem, type="service-account"),
        "email host": sa_json(pem, client_email="a-reader@example.com"),
        "email newline": sa_json(pem, client_email=EMAIL + "\n"),
        "email missing": sa_json(pem, client_email=_DROP),
        "key id quote": sa_json(pem, private_key_id='abc"def'),
        "key id missing": sa_json(pem, private_key_id=_DROP),
        "key missing": sa_json(pem, private_key=_DROP),
        "key garbage": sa_json(pem, private_key=bq._PKCS8_BEGIN + "\n" + SENTINEL),
        "key ec": sa_json(pem, private_key=_ec_pem()),
        "key pkcs1": sa_json(pem, private_key=_pkcs1_pem(rsa_key)),
        "key small": sa_json(pem, private_key=small),
        "project bad": sa_json(pem, project_id="Acme_Project"),
        "duplicate token_uri": sa_json(pem)[:-1]
        + ', "token_uri": "http://127.0.0.1:1/x"}',
        "too large": sa_json(pem, padding="x" * 17000),
    }[case]
    with pytest.raises(BigQueryConfigRefused) as err:
        parse_service_account_json(text)
    assert err.value.code == "invalid_service_account"
    rendered = str(err.value) + json.dumps(err.value.to_body())
    assert SENTINEL not in rendered and "BEGIN" not in rendered
    assert err.value.__cause__ is None


@pytest.mark.parametrize(
    "field, value",
    [
        ("billing_project", "acme-analytics\n"),
        ("billing_project", "Acme-Analytics"),
        ("billing_project", "acme"),
        ("billing_project", "acme/analytics"),
        ("location", "US\n"),
        ("location", "us east"),
        ("location", "US/../x"),
        ("location", ""),
    ],
)
def test_connection_fields_fully_match(field, value):
    params = {
        "billing_project": PROJECT,
        "location": "EU",
        "max_bytes_per_query": 10_000_000,
        "query_timeout_seconds": 300,
    }
    params[field] = value
    with pytest.raises(BigQueryConfigRefused) as err:
        BigQueryConnection(**params)
    assert err.value.field == field


@pytest.mark.parametrize("location", ["US", "EU", "europe-west2", "asia-northeast1"])
def test_connection_locations_accepted(location):
    assert BigQueryConnection(PROJECT, location, 10_000_000, 300).location == location


def test_repr_never_shows_the_key(key):
    assert "PRIVATE" not in repr(key) and KEY_ID not in repr(key)


# -- sign-in ----------------------------------------------------------------


def test_assertion_is_signed_for_the_constant_endpoint(key, rsa_key):
    fake = GoogleFake(happy())
    adapter(key, fake).check_connection(Deadline(30))
    token = fake.requests[0]
    assert (token.method, token.host, token.path) == (
        "POST",
        "oauth2.googleapis.com",
        "/token",
    )
    assert token.headers["content-type"] == "application/x-www-form-urlencoded"
    form = token.form()
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    assertion = form["assertion"][0]
    header = jwt.get_unverified_header(assertion)
    assert header == {"alg": "RS256", "typ": "JWT", "kid": KEY_ID}
    claims = jwt.decode(
        assertion,
        rsa_key.public_key(),
        algorithms=["RS256"],
        audience=TOKEN_URL,
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims == {
        "iss": EMAIL,
        "scope": "https://www.googleapis.com/auth/bigquery",
        "aud": TOKEN_URL,
        "iat": 1_759_000_000,
        "exp": 1_759_003_600,
    }
    api = fake.requests[1]
    assert api.headers["authorization"] == "Bearer synthetic-access-token-for-tests"


def test_token_is_cached_until_five_minutes_before_expiry(key):
    clock = FakeClock()
    cache = AccessTokenCache(clock=clock)
    fake = GoogleFake(happy())
    connector = adapter(key, fake, token_cache=cache)

    def token_posts():
        return sum(1 for r in fake.requests if r.host == "oauth2.googleapis.com")

    connector.check_connection(Deadline(30))
    connector.check_connection(Deadline(30))
    assert token_posts() == 1
    clock.advance(3599 - 300 - 1)
    connector.check_connection(Deadline(30))
    assert token_posts() == 1
    clock.advance(1)
    connector.check_connection(Deadline(30))
    assert token_posts() == 2


def _formatted(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


@pytest.mark.parametrize(
    "answer, code",
    [
        ("token_error_invalid_grant", C.AUTH_FAILED),
        ("token_error_disabled_client", C.KEY_REVOKED),
        (
            (401, {"error": "deleted_client", "error_description": SENTINEL}),
            C.KEY_REVOKED,
        ),
        (
            (400, {"error": "invalid_client", "error_description": SENTINEL}),
            C.AUTH_FAILED,
        ),
        ((500, b"<html>" + SENTINEL.encode() + b"</html>"), C.AUTH_FAILED),
    ],
)
def test_token_error_body_never_returned_or_logged(key, caplog, answer, code):
    fake = GoogleFake(happy(token=[answer]))
    caplog.set_level(logging.DEBUG)
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake).check_connection(Deadline(30))
    assert err.value.code is code
    surfaces = [
        str(err.value),
        repr(err.value),
        err.value.message,
        json.dumps(err.value.to_body()),
        json.dumps(err.value.log_fields()),
        _formatted(err.value),
        caplog.text,
        "".join(_formatted(r.exc_info[1]) for r in caplog.records if r.exc_info),
    ]
    for surface in surfaces:
        assert SENTINEL not in surface
        assert "Invalid JWT" not in surface
    # Only the token request was made: nothing ran.
    assert [r.host for r in fake.requests] == ["oauth2.googleapis.com"]


@pytest.mark.parametrize(
    "body",
    [
        {"access_token": "a b", "token_type": "Bearer", "expires_in": 3599},
        {
            "access_token": "tok.x\r\nX-Evil: 1",
            "token_type": "Bearer",
            "expires_in": 3599,
        },
        {"access_token": "tok.x", "token_type": "MAC", "expires_in": 3599},
        {"access_token": "tok.x", "token_type": "Bearer", "expires_in": "3599"},
        {"access_token": "tok.x", "token_type": "Bearer", "expires_in": 0},
        {"token_type": "Bearer", "expires_in": 3599},
    ],
)
def test_malformed_token_answer_refused(key, body):
    fake = GoogleFake(happy(token=[(200, body)]))
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake).check_connection(Deadline(30))
    assert err.value.code is C.RESULT_INVALID


# -- queries ----------------------------------------------------------------


def test_dry_run_uses_jobs_insert_and_requires_select(key):
    """The statement type comes from a jobs.insert dry run; not SELECT -> nothing runs."""
    fake = GoogleFake(happy(dry_run=["dry_run_script"]))
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake).run_query(metric_query(), Deadline(600))
    assert err.value.code is C.NOT_A_SELECT
    posts = jobs_posts(fake)
    assert len(posts) == 1
    assert posts[0].host == "bigquery.googleapis.com"
    assert posts[0].path == f"/bigquery/v2/projects/{PROJECT}/jobs"
    body = posts[0].json()
    assert body["configuration"]["dryRun"] is True
    assert body["configuration"]["query"]["useLegacySql"] is False
    assert body["jobReference"] == {"projectId": PROJECT, "location": "US"}
    # jobs.query (POST .../queries) is never used.
    assert not [r for r in fake.requests if r.path.endswith("/queries")]


def test_bytes_over_cap_refused_before_run(key):
    fake = GoogleFake(happy())
    small = BigQueryConnection(PROJECT, "US", 30_755_559, 300)
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake, connection=small).run_query(metric_query(), Deadline(600))
    assert err.value.code is C.BYTES_LIMIT
    assert len(jobs_posts(fake)) == 1
    assert jobs_posts(fake)[0].json()["configuration"]["dryRun"] is True


def test_bytes_at_the_cap_run(key):
    fake = GoogleFake(happy())
    exact = BigQueryConnection(PROJECT, "US", 30_755_560, 300)
    adapter(key, fake, connection=exact, sleeper=lambda s: None).run_query(
        metric_query(), Deadline(600)
    )
    assert len(jobs_posts(fake)) == 2


def test_no_secret_reaches_a_log_record(key, pem, caplog):
    """At DEBUG, with httpx and httpcore tracing, no key, assertion or token is logged."""
    caplog.set_level(logging.DEBUG)
    # The application holds these two at WARNING (configure_logging); the test
    # turns their tracing back on, which is what it exists to read.
    caplog.set_level(logging.DEBUG, logger="httpx")
    caplog.set_level(logging.DEBUG, logger="httpcore")
    fake = GoogleFake(happy())
    adapter(key, fake, sleeper=lambda s: None).run_query(metric_query(), Deadline(600))
    assertion = fake.requests[0].form()["assertion"][0]
    text = caplog.text + "".join(str(r.__dict__) for r in caplog.records)
    assert any(r.name.split(".")[0] in ("httpx", "httpcore") for r in caplog.records)
    for secret in (
        "synthetic-access-token-for-tests",
        assertion,
        assertion.split(".")[2],
        pem.splitlines()[1],
        "PRIVATE KEY",
    ):
        assert secret not in text


def test_run_query_sends_the_limits_and_returns_the_rows(key):
    fake = GoogleFake(happy())
    query = metric_query()
    result = adapter(
        key, fake, job_id_factory=lambda: "experimently_job1", sleeper=lambda s: None
    ).run_query(query, Deadline(600))

    dry, real = jobs_posts(fake)
    assert dry.json()["configuration"]["query"]["query"] == query.sql
    config = real.json()["configuration"]
    assert "dryRun" not in config
    assert config["query"] == {
        "query": query.sql,
        "useLegacySql": False,
        "maximumBytesBilled": "50000000",
    }
    assert config["jobTimeoutMs"] == "300000"
    assert config["labels"] == JOB_LABELS == {"experimently": "analysis"}
    assert real.json()["jobReference"] == {
        "projectId": PROJECT,
        "jobId": "experimently_job1",
        "location": "US",
    }
    polls = [r for r in fake.requests if "/queries/" in r.path]
    assert len(polls) == 2
    for poll in polls:
        assert poll.path == f"/bigquery/v2/projects/{PROJECT}/queries/experimently_job1"
        assert poll.query["location"] == ["US"]
        assert 0 < int(poll.query["timeoutMs"][0]) <= 10_000
    assert not [r for r in fake.requests if r.path.endswith("/cancel")]

    assert result.job_id == "experimently_job1"
    # The real job's statistics (run wl-bigquery-20261005T150837Z-7782e712).
    assert result.total_bytes_billed == 31_457_280
    assert result.total_bytes_processed == 30_755_560
    assert result.elapsed_ms == 2949
    stats = parse_metric_rows(list(result.rows))
    assert [v.variant for v in stats.variants] == ["control", "treatment"]
    assert stats.k_text == "0.10500000000000001"
    assert {r.host for r in fake.requests} == {
        "oauth2.googleapis.com",
        "bigquery.googleapis.com",
    }
    assert set(fake.tls_hostnames) == {
        "oauth2.googleapis.com",
        "bigquery.googleapis.com",
    }


def test_job_time_limit_never_exceeds_the_time_left(key):
    fake = GoogleFake(happy())
    adapter(key, fake, sleeper=lambda s: None).run_query(metric_query(), Deadline(42))
    assert jobs_posts(fake)[1].json()["configuration"]["jobTimeoutMs"] in (
        "41000",
        "42000",
    )


def test_cancel_on_deadline(key):
    """A job still running at the deadline is cancelled, and the query is time_limit."""
    clock = FakeClock()
    fake = GoogleFake(
        happy(results=["query_results_incomplete"]),
        clock=clock,
        seconds_per_request=7.0,
    )
    connector = adapter(
        key,
        fake,
        job_id_factory=lambda: "experimently_slow",
        sleeper=lambda seconds: clock.advance(seconds),
    )
    end = clock() + 60
    with pytest.raises(WarehouseError) as err:
        connector.run_query(metric_query(), Deadline(60, clock=clock))
    assert err.value.code is C.TIME_LIMIT
    cancels = [r for r in fake.requests if r.path.endswith("/cancel")]
    assert len(cancels) == 1
    # Polls stop early enough for the cancel to go out within the total.
    assert cancels[0].at is not None and cancels[0].at <= end
    assert cancels[0].method == "POST"
    assert (
        cancels[0].path
        == f"/bigquery/v2/projects/{PROJECT}/jobs/experimently_slow/cancel"
    )
    assert cancels[0].query["location"] == ["US"]
    # Every poll left the reserve: the last one ended before the deadline.
    polls = [r for r in fake.requests if "/queries/" in r.path]
    assert polls
    for poll in polls:
        assert int(poll.query["timeoutMs"][0]) <= 10_000


def test_cancel_is_sent_even_when_a_slow_answer_used_the_reserve(key):
    """A poll that ends past the deadline still leads to a cancel, on its own budget."""
    clock = FakeClock()
    script = happy(results=["query_results_incomplete"])
    fake = GoogleFake(script, clock=clock, seconds_per_request=0.0)

    def handler(request):
        if "/queries/" in request.path:
            clock.advance(100.0)  # the answer arrives long after the deadline
        return script(request)

    fake.handler = handler
    connector = adapter(key, fake, sleeper=lambda s: clock.advance(s))
    with pytest.raises(WarehouseError) as err:
        connector.run_query(metric_query(), Deadline(60, clock=clock))
    assert err.value.code is C.TIME_LIMIT
    assert len([r for r in fake.requests if r.path.endswith("/cancel")]) == 1


def test_a_job_whose_insert_answer_is_unreadable_is_cancelled(key):
    """The job id is ours before the insert, so a lost answer still gets a cancel."""
    fake = GoogleFake(happy(insert=[(200, b"<html>not json</html>")]))
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake, job_id_factory=lambda: "experimently_lost").run_query(
            metric_query(), Deadline(600)
        )
    assert err.value.code is C.RESULT_INVALID
    cancels = [r for r in fake.requests if r.path.endswith("/cancel")]
    assert [c.path for c in cancels] == [
        f"/bigquery/v2/projects/{PROJECT}/jobs/experimently_lost/cancel"
    ]


def test_a_refused_insert_is_not_cancelled(key):
    fake = GoogleFake(happy(insert=["error_access_denied"]))
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake).run_query(metric_query(), Deadline(600))
    assert err.value.code is C.PERMISSION_DENIED
    assert not [r for r in fake.requests if r.path.endswith("/cancel")]


def test_a_token_google_rejects_is_not_reused(key):
    rejected = (401, {"error": {"code": 401, "errors": [{"reason": "authError"}]}})
    fake = GoogleFake(happy(dry_run=[rejected, "dry_run_select"]))
    connector = adapter(key, fake)
    with pytest.raises(WarehouseError) as err:
        connector.check_connection(Deadline(30))
    assert err.value.code is C.AUTH_FAILED
    connector.check_connection(Deadline(30))
    assert sum(1 for r in fake.requests if r.host == "oauth2.googleapis.com") == 2


def test_a_failed_cancel_still_reports_time_limit(key, caplog):
    clock = FakeClock()
    fake = GoogleFake(
        happy(
            results=["query_results_incomplete"],
            cancel=["error_access_denied"],
        ),
        clock=clock,
        seconds_per_request=7.0,
    )
    caplog.set_level(logging.DEBUG)
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake, sleeper=lambda s: clock.advance(s)).run_query(
            metric_query(), Deadline(60, clock=clock)
        )
    assert err.value.code is C.TIME_LIMIT
    assert SENTINEL not in caplog.text


@pytest.mark.parametrize(
    "answer, code",
    [
        ("error_access_denied", C.PERMISSION_DENIED),
        ("error_not_found", C.OBJECT_NOT_FOUND),
        ("error_bytes_billed_limit", C.BYTES_LIMIT),
        ("error_timeout", C.TIME_LIMIT),
        ("error_unknown_reason", C.UNRECOGNISED_WAREHOUSE_ERROR),
    ],
)
@pytest.mark.parametrize("stage", ["dry_run", "insert", "results"])
def test_google_errors_map_to_codes_without_their_text(
    key, caplog, answer, code, stage
):
    fake = GoogleFake(happy(**{stage: [answer]}))
    caplog.set_level(logging.DEBUG)
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake, sleeper=lambda s: None).run_query(
            metric_query(), Deadline(600)
        )
    assert err.value.code is code
    for surface in (
        str(err.value),
        repr(err.value),
        err.value.message,
        json.dumps(err.value.to_body()),
        _formatted(err.value),
        caplog.text,
    ):
        assert SENTINEL not in surface
    if code is C.UNRECOGNISED_WAREHOUSE_ERROR:
        assert err.value.vendor_code == "invalidQuery"
        assert "(code invalidQuery)" in err.value.message


def test_the_real_avg_of_a_row_refusal_is_reported_by_its_reason(key, caplog):
    """BigQuery's real dry-run answer to the statement it refused (#312):
    invalidQuery has no code of ours, so it is unrecognised, with the reason
    as the vendor code and none of BigQuery's text."""
    fake = GoogleFake(happy(dry_run=["real_dry_run_avg_of_row"]))
    caplog.set_level(logging.DEBUG)
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake, sleeper=lambda s: None).run_query(
            metric_query(), Deadline(600)
        )
    assert err.value.code is C.UNRECOGNISED_WAREHOUSE_ERROR
    assert err.value.vendor_code == "invalidQuery"
    assert err.value.http_status == 400
    for surface in (
        str(err.value),
        repr(err.value),
        err.value.message,
        json.dumps(err.value.to_body()),
        _formatted(err.value),
        caplog.text,
    ):
        assert "No matching signature" not in surface
        assert "STRUCT" not in surface


def test_a_job_that_failed_is_reported_by_its_reason(key):
    fake = GoogleFake(happy(job=["job_done_error"]))
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake, sleeper=lambda s: None).run_query(
            metric_query(), Deadline(600)
        )
    assert err.value.code is C.PERMISSION_DENIED


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["schema"]["fields"][0].update(type="RECORD"),
        lambda b: b["schema"]["fields"][0].update(mode="REPEATED"),
        lambda b: b["rows"][0]["f"].pop(),
        lambda b: b["rows"][0]["f"][1].update(v=1000),
        lambda b: b["rows"][0]["f"][1].pop("v"),
        lambda b: b["schema"]["fields"][1].update(name="variant"),
        lambda b: b["schema"].pop("fields"),
    ],
)
def test_result_rows_of_an_unexpected_shape_refused(key, mutate):
    status, body = recorded("query_results_metric")
    mutate(body)
    fake = GoogleFake(happy(results=[(status, body)]))
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake).run_query(metric_query(), Deadline(600))
    assert err.value.code is C.RESULT_INVALID


def test_only_generated_bigquery_statements_run(key):
    fake = GoogleFake(happy())
    other = BuiltQuery(kind="metric", dialect="snowflake", sql="SELECT 1")
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake).run_query(other, Deadline(600))
    assert err.value.code is C.INTERNAL
    with pytest.raises(WarehouseError):
        adapter(key, fake).run_query("SELECT 1", Deadline(600))  # type: ignore[arg-type]
    assert fake.requests == []


def test_every_request_is_to_a_published_google_endpoint(key):
    fake = GoogleFake(happy())
    connector = adapter(key, fake, sleeper=lambda s: None)
    connector.run_query(metric_query(), Deadline(600))
    connector.table_columns(("acme-data", "events", "exposures"), Deadline(30))
    assert fake.connects and {c[0] for c in fake.connects} == {"93.184.216.34"}
    assert {r.host for r in fake.requests} <= {
        "oauth2.googleapis.com",
        "bigquery.googleapis.com",
    }
    assert API_ROOT == "https://bigquery.googleapis.com/bigquery/v2"


def test_connection_test_is_a_dry_run_only(key):
    fake = GoogleFake(happy())
    dry = adapter(key, fake).check_connection(Deadline(30))
    assert dry.statement_type == "SELECT"
    posts = jobs_posts(fake)
    assert len(posts) == 1 and posts[0].json()["configuration"]["dryRun"] is True
    assert not [r for r in fake.requests if "/queries/" in r.path]


# -- metadata ---------------------------------------------------------------


def test_table_columns_from_tables_get(key):
    fake = GoogleFake(happy())
    columns = adapter(key, fake).table_columns(
        ("acme-data", "events", "exposures"), Deadline(30)
    )
    assert columns == (
        ("user_id", "STRING"),
        ("experiment", "STRING"),
        ("variant", "STRING"),
        ("exposed_at", "TIMESTAMP"),
        ("amount", "FLOAT"),
    )
    get = fake.requests[-1]
    assert (get.method, get.path) == (
        "GET",
        "/bigquery/v2/projects/acme-data/datasets/events/tables/exposures",
    )


@pytest.mark.parametrize(
    "table",
    [
        ("acme-data", "events"),
        ("acme-data", "events", "exposures", "x"),
        ("acme-data", "events/../x", "exposures"),
        ("acme-data", "events", "exposures\n"),
        ("acme-data", "events", "a`b"),
        ("ACME", "events", "exposures"),
    ],
)
def test_table_reference_checked_before_anything_is_sent(key, table):
    fake = GoogleFake(happy())
    with pytest.raises(WarehouseQueryRefused) as err:
        adapter(key, fake).table_columns(table, Deadline(30))
    assert err.value.code == "invalid_identifier"
    assert fake.requests == [] and fake.connects == []


def test_the_deadline_before_any_request_sends_nothing(key):
    clock = FakeClock()
    deadline = Deadline(1, clock=clock)
    clock.advance(2)
    fake = GoogleFake(happy())
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake).check_connection(deadline)
    assert err.value.code is C.TIME_LIMIT
    assert fake.requests == []
    assert CANCEL_RESERVE_SECONDS > 0


# -- answers recorded on a real account (run wl-bigquery-20261005T150837Z-7782e712)


def diagnostics_query() -> BuiltQuery:
    return build_diagnostics_query(
        BIGQUERY_SQL,
        AssignmentMapping(
            table=("acme-data", "events", "exposures"),
            unit_id="user_id",
            experiment_key="experiment",
            variant="variant",
            exposed_at="exposed_at",
        ),
        "checkout-test",
        AnalysisWindow(
            start=datetime(2026, 9, 1, tzinfo=timezone.utc),
            end=datetime(2026, 9, 15, tzinfo=timezone.utc),
        ),
    )


def test_real_table_columns_are_read(key):
    fake = GoogleFake(happy(table=["real_table_get"]))
    columns = adapter(key, fake).table_columns(
        ("acme-data", "events", "exposures"), Deadline(30)
    )
    assert columns == (
        ("user_id", "STRING"),
        ("experiment_key", "STRING"),
        ("variant", "STRING"),
        ("exposed_at", "TIMESTAMP"),
    )


def test_real_metric_results_are_read_and_parsed(key):
    fake = GoogleFake(happy(results=["real_query_results_metric"]))
    result = adapter(key, fake, sleeper=lambda s: None).run_query(
        metric_query(), Deadline(600)
    )
    stats = parse_metric_rows(list(result.rows))
    assert [v.variant for v in stats.variants] == ["control", "treatment"]
    assert [v.n for v in stats.variants] == [200_000, 200_000]
    assert [v.n_converted for v in stats.variants] == [100_514, 100_260]
    assert stats.k_text == "0.50193499999999193"


def test_real_diagnostics_results_are_read_and_parsed(key):
    fake = GoogleFake(happy(results=["real_query_results_diagnostics"]))
    result = adapter(key, fake, sleeper=lambda s: None).run_query(
        diagnostics_query(), Deadline(600)
    )
    diagnostics = parse_diagnostics_rows(list(result.rows))
    assert diagnostics.units == 400_000
    assert diagnostics.multi_variant_units == 0


@pytest.mark.parametrize(
    "answer, code",
    [
        ("real_job_access_denied", C.PERMISSION_DENIED),
        ("real_job_bytes_billed_limit", C.BYTES_LIMIT),
    ],
)
def test_a_real_job_that_failed_on_insert_is_reported_by_its_reason(
    key, caplog, answer, code
):
    """BigQuery answered both refusals as a finished job with an ``errorResult``."""
    caplog.set_level(logging.DEBUG)
    fake = GoogleFake(happy(insert=[answer]))
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake, sleeper=lambda s: None).run_query(
            metric_query(), Deadline(600)
        )
    assert err.value.code is code
    for surface in (
        str(err.value),
        err.value.message,
        _formatted(err.value),
        caplog.text,
    ):
        assert SENTINEL not in surface
