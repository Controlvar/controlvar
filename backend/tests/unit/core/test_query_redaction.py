"""The rule for query values that are not logged (backend/app/core/query_redaction.py)."""

from __future__ import annotations

import logging

import pytest

from backend.app.core.query_redaction import (
    MAX_QUERY_CHARS,
    QUERY_VALUE_REDACTOR,
    REDACTED,
    TRUNCATED,
    QueryValueRedactor,
    is_oidc_callback,
    redact_query,
    redact_query_params,
    redact_target,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

CALLBACK = "/api/v1/auth/sso/oidc/okta/callback"


@pytest.mark.parametrize(
    "name",
    [
        "code",
        "state",
        "token",
        "access_token",
        "id_token",
        "refresh_token",
        "client_secret",
        "api_key",
        "apikey",
        "password",
        "secret",
        "jwt",
        "CODE",
        "API_KEY",
        "ApiKey",
        "Password",
        "JWT",
        "%73ecret",  # percent-encoded
        "Access_Token",
        "%63ode",  # "code", percent-encoded
        "client%5Fsecret",
        "ID_TOKEN",
        # The end user's attributes on the flag-evaluation routes (#1552).
        pytest.param("context", marks=pytest.mark.regression),
        pytest.param("Context", marks=pytest.mark.regression),
        pytest.param("%63ontext", marks=pytest.mark.regression),
    ],
)
def test_a_listed_name_has_its_value_replaced_on_any_route(name):
    assert redact_query("/api/v1/anything", f"{name}=v4lue-1&page=2") == (
        f"{name}={REDACTED}&page=2"
    )


def test_other_parameters_are_kept_as_sent():
    query = "page=2&sort=-created_at&q=a%20b&codename=x&statex=y&key=k&api=a"
    assert redact_query("/api/v1/experiments", query) == query


def test_every_value_on_the_oidc_callback_is_replaced_and_every_name_kept():
    query = "code=c0de-v4lue&state=st4te-v4lue&session_state=s3ss&iss=https%3A%2F%2Fidp"
    assert redact_query(CALLBACK, query) == (
        f"code={REDACTED}&state={REDACTED}&session_state={REDACTED}&iss={REDACTED}"
    )


@pytest.mark.parametrize(
    "path",
    [
        CALLBACK,
        "/api/v1/auth/sso/oidc/google/callback/",
        "/API/V1/AUTH/SSO/OIDC/okta/CALLBACK",
        "http%3A//host/api/v1/auth/sso/oidc/okta/callback",  # absolute-form target
    ],
)
def test_the_callback_is_recognised(path):
    assert is_oidc_callback(path)


@pytest.mark.parametrize(
    "path",
    ["/api/v1/auth/sso/oidc/okta/login", "/api/v1/auth/sso/login", "/callback"],
)
def test_other_routes_are_not_the_callback(path):
    assert not is_oidc_callback(path)


def test_a_pair_without_a_value_is_kept():
    assert redact_query("/x", "code&token=&debug") == f"code&token={REDACTED}&debug"


def test_an_empty_query_stays_empty():
    assert redact_query("/x", "") == ""


def test_a_target_without_a_query_is_unchanged():
    assert redact_target("/api/v1/auth/sso/oidc/okta/callback") == CALLBACK


def test_a_target_has_its_query_rewritten():
    assert redact_target("/metrics?token=t0ken-v4lue") == f"/metrics?token={REDACTED}"


def test_a_long_query_is_cut_before_it_is_read():
    query = "page=1&" + "a" * (MAX_QUERY_CHARS * 50) + "&code=c0de-v4lue"
    out = redact_query("/x", query)
    assert out.endswith(TRUNCATED)
    assert len(out) <= MAX_QUERY_CHARS + len(TRUNCATED)
    assert "c0de-v4lue" not in out


def test_a_value_cut_at_the_limit_is_still_replaced():
    query = "page=1&code=" + "c" * (MAX_QUERY_CHARS * 2)
    out = redact_query("/x", query)
    assert out == f"page=1&code={REDACTED}{TRUNCATED}"


def test_parsed_parameters_follow_the_same_rule():
    assert redact_query_params(
        "/api/v1/experiments", {"Code": "c", "page": "2", "token": "t"}
    ) == {"Code": REDACTED, "page": "2", "token": REDACTED}
    assert redact_query_params(CALLBACK, {"code": "c", "iss": "i"}) == {
        "code": REDACTED,
        "iss": REDACTED,
    }


def _record(msg, args, name="uvicorn.access"):
    return logging.LogRecord(name, logging.INFO, __file__, 1, msg, args, None)


def test_the_filter_rewrites_uvicorns_access_line():
    record = _record(
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", f"{CALLBACK}?code=c0de&state=st4te", "1.1", 302),
    )
    assert QueryValueRedactor().filter(record) is True
    assert record.getMessage() == (
        f'127.0.0.1:5000 - "GET {CALLBACK}?code={REDACTED}&state={REDACTED} '
        'HTTP/1.1" 302'
    )


def test_the_filter_leaves_other_records_alone():
    record = _record("Started server process [%d]", (42,), name="uvicorn.error")
    assert QueryValueRedactor().filter(record) is True
    assert record.getMessage() == "Started server process [42]"
    plain = _record("no arguments", ())
    assert QueryValueRedactor().filter(plain) is True
    assert plain.getMessage() == "no arguments"


def test_the_filter_drops_the_whole_query_if_the_rule_fails(monkeypatch):
    def broken(target):
        raise RuntimeError("boom")

    monkeypatch.setattr("backend.app.core.query_redaction.redact_target", broken)
    record = _record("%s", ("/x?code=c0de-v4lue",))
    assert QueryValueRedactor().filter(record) is True
    assert record.getMessage() == f"/x?{REDACTED}"


def test_configure_logging_installs_one_filter_however_often_it_runs():
    import io

    import structlog

    from backend.app.core.logger import configure_logging

    root = logging.root
    handlers, root_level = list(root.handlers), root.level
    saved = structlog.get_config()
    try:
        for _ in range(3):
            configure_logging(json_logs=True, stream=io.StringIO())
        for name in ("uvicorn.access", "uvicorn.error"):
            filters = logging.getLogger(name).filters
            assert filters.count(QUERY_VALUE_REDACTOR) == 1, (name, filters)
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in handlers:
            root.addHandler(handler)
        root.setLevel(root_level)
        structlog.configure(**saved)
