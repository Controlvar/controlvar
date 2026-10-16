"""The container's start-up check lists every missing setting at once (#237).

``backend/docker-entrypoint.sh`` runs ``python -m backend.app.core.preflight``
first. Before it, a production start refused its settings one at a time --
SECRET_KEY and FIRST_SUPERUSER_PASSWORD, then PUBLIC_BASE_URL, then
AUDIT_HMAC_KEY -- and a DATABASE_URL the image does not read cost a
120-second wait on localhost. Each refusal below is a planted defect: one
setting wrong in an otherwise good environment, and the check must name it.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.app.core import preflight

REPO_ROOT = Path(__file__).resolve().parents[4]
ENTRYPOINT = REPO_ROOT / "backend" / "docker-entrypoint.sh"

_KEY = "a1" * 32  # 64 hex characters, like `openssl rand -hex 32`
_PASSWORD = "Xq7-first-admin-password"

#: A production environment the image starts with (full profile).
GOOD = {
    "ENVIRONMENT": "production",
    "SECRET_KEY": _KEY,
    "FIRST_SUPERUSER_PASSWORD": _PASSWORD,
    "PUBLIC_BASE_URL": "https://controlvar.example.com",
    "AUDIT_HMAC_KEY": _KEY[::-1],
    "POSTGRES_SERVER": "db.internal",
}

pytestmark = pytest.mark.unit


def check(environ, *, full_profile=True):
    """No dotenv file: the directory does not exist."""
    return preflight.check(
        environ, full_profile=full_profile, root=Path("/nonexistent-preflight-root")
    )


def with_(**changes):
    env = dict(GOOD)
    for key, value in changes.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def only_problem(problems, setting):
    assert len(problems) == 1, problems
    assert problems[0].startswith(setting), problems[0]
    return problems[0]


def test_a_good_production_environment_passes():
    assert check(GOOD) == []
    assert check(with_(AUDIT_HMAC_KEY=None), full_profile=False) == []


@pytest.mark.regression
def test_nothing_set_lists_every_required_setting_at_once():
    """The issue: five settings, one list, not five restarts."""
    problems = check({"ENVIRONMENT": "production"})
    named = [p.split(" ", 1)[0] for p in problems]
    assert named == [
        "SECRET_KEY",
        "FIRST_SUPERUSER_PASSWORD",
        "PUBLIC_BASE_URL",
        "AUDIT_HMAC_KEY",
        "POSTGRES_SERVER",
    ]
    text = preflight.render(problems)
    assert text.startswith(
        "[preflight] Controlvar cannot start: 5 settings need attention.\n"
    )
    assert text.count("\n  - ") == 5
    assert "continuing on the core profile" not in text
    assert "<value>" not in text


@pytest.mark.regression
def test_the_core_profile_does_not_ask_for_the_audit_key():
    problems = check({"ENVIRONMENT": "production"}, full_profile=False)
    assert not any(p.startswith("AUDIT_HMAC_KEY") for p in problems)
    assert len(problems) == 4


@pytest.mark.regression
def test_main_exits_78_and_prints_the_list(capsys):
    assert preflight.main({"ENVIRONMENT": "staging"}) == preflight.EX_CONFIG == 78
    err = capsys.readouterr().err
    assert "[preflight] Controlvar cannot start" in err
    assert "SECRET_KEY" in err and "POSTGRES_SERVER" in err


def test_main_exits_0_when_nothing_is_wrong(capsys):
    assert preflight.main({"ENVIRONMENT": "development"}) == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "setting, value, words",
    [
        ("SECRET_KEY", None, "SECRET_KEY is not set"),
        ("SECRET_KEY", "k3y9", "shorter than 32"),
        (
            "SECRET_KEY",
            "dev-only-change-me-9f1c7b2e4a6d8e0f1a2b3c4d5e6f7a8b",
            "placeholder",
        ),
        ("FIRST_SUPERUSER_PASSWORD", None, "FIRST_SUPERUSER_PASSWORD is not set"),
        ("FIRST_SUPERUSER_PASSWORD", "Demo1234!", "well-known default"),
        ("FIRST_SUPERUSER_PASSWORD", "pw7x", "shorter than 8"),
        ("AUDIT_HMAC_KEY", None, "full profile requires it"),
        ("AUDIT_HMAC_KEY", "dev-audit-key-change-in-production", "placeholder"),
    ],
)
@pytest.mark.regression
def test_each_secret_refusal_names_the_setting_and_how_to_generate_it(
    setting, value, words
):
    message = only_problem(check(with_(**{setting: value})), setting)
    assert words in message
    assert "Generate one: openssl rand" in message
    if value:
        assert value not in message, "a secret's value must never be printed"


@pytest.mark.regression
def test_public_base_url_missing_gives_the_dashboard_url_as_the_example():
    message = only_problem(check(with_(PUBLIC_BASE_URL=None)), "PUBLIC_BASE_URL")
    assert "https://controlvar.example.com" in message
    assert "api." not in message


@pytest.mark.parametrize(
    "url",
    ["controlvar.example.com", "ftp://x.example.com", "https://x.example.com/app"],
)
def test_a_malformed_public_base_url_is_refused(url):
    message = only_problem(check(with_(PUBLIC_BASE_URL=url)), "PUBLIC_BASE_URL")
    assert "api." not in message


def test_allowed_hosts_stands_in_for_public_base_url_as_the_settings_allow():
    assert (
        check(with_(PUBLIC_BASE_URL=None, ALLOWED_HOSTS="a.example.com,b.example.com"))
        == []
    )


@pytest.mark.parametrize("hosts", ["*", "a.example.com,*", '["*"]'])
def test_a_wildcard_allowed_hosts_is_refused(hosts):
    only_problem(check(with_(ALLOWED_HOSTS=hosts)), "ALLOWED_HOSTS")


@pytest.mark.regression
def test_postgres_server_missing_is_refused_before_the_wait():
    message = only_problem(check(with_(POSTGRES_SERVER=None)), "POSTGRES_SERVER")
    assert "localhost:5432" in message


def test_postgres_host_is_accepted_as_the_entrypoint_accepts_it():
    assert check(with_(POSTGRES_SERVER=None, POSTGRES_HOST="db.internal")) == []


@pytest.mark.regression
@pytest.mark.parametrize("key", ["DATABASE_URL", "DATABASE_URI"])
def test_a_connection_string_the_image_does_not_read_is_refused(key):
    message = only_problem(check(with_(**{key: "postgresql://u:p@db/x"})), key)
    assert "POSTGRES_SERVER, POSTGRES_PORT, POSTGRES_USER, POSTGRES_PASSWORD" in message
    assert "u:p@db" not in message


@pytest.mark.regression
def test_no_environment_at_all_is_refused_in_one_line():
    assert check({}) == [preflight.ENVIRONMENT_UNSET]
    text = preflight.render(check({"SECRET_KEY": _KEY}))
    assert text == f"[preflight] {preflight.ENVIRONMENT_UNSET}"
    assert "\n" not in text
    assert "ENVIRONMENT" in text


@pytest.mark.parametrize(
    "app_env, hardened",
    [("prod", True), ("staging", True), ("dev", False), ("demo", False)],
)
def test_the_legacy_app_env_names_the_environment(app_env, hardened):
    """What the CDK task definitions set (`"APP_ENV": env_name`)."""
    problems = check({"APP_ENV": app_env})
    assert bool(problems) is hardened
    assert preflight.ENVIRONMENT_UNSET not in problems


@pytest.mark.parametrize("name", ["prodution", " ", "live"])
def test_an_unknown_environment_name_is_refused(name):
    (message,) = check({"ENVIRONMENT": name})
    assert message.startswith("ENVIRONMENT=")
    assert message.endswith("Use one of: development, staging, production.")


def test_development_needs_nothing_else():
    """Docker Smoke and the dev compose: placeholder secrets, SEED=demo."""
    assert (
        check({"ENVIRONMENT": "development", "SECRET_KEY": "ci-only-x", "SEED": "demo"})
        == []
    )


@pytest.mark.regression
@pytest.mark.parametrize(
    "environ, name",
    [
        ({"ENVIRONMENT": "test"}, "ENVIRONMENT"),
        ({"ENVIRONMENT": " Test "}, "ENVIRONMENT"),
        ({"APP_ENV": "test"}, "APP_ENV"),
        # A good production set does not rescue it: the name alone decides.
        (with_(ENVIRONMENT="test"), "ENVIRONMENT"),
    ],
)
def test_the_test_environment_is_refused_in_one_line(environ, name):
    """``test`` is the test runner's environment (pytest, from source). The
    image used to start in it, with the test settings' placeholder secrets
    and first-administrator password."""
    problems = check(environ)
    assert problems == [preflight.environment_is_test(name)]
    text = preflight.render(problems)
    assert text == (
        f"[preflight] {name}=test is for the test runner, not this image: set "
        "ENVIRONMENT=production (or staging; development only for a local trial)."
    )
    assert "\n" not in text


_TESTING_REFUSAL = (
    "TESTING is for the test runner and cannot be combined with "
    "ENVIRONMENT={}. Remove TESTING from this deployment's configuration."
)


@pytest.mark.regression
@pytest.mark.parametrize("environment", ["production", "staging"])
@pytest.mark.parametrize("flag", ["true", "1", "on", "T", "Y", "yes", "enabled"])
def test_testing_with_a_hardened_environment_is_refused_in_one_line(environment, flag):
    """The settings refuse TESTING with staging/production at import. The
    check must refuse it first, before the database wait and the migrations,
    even when every other setting is good."""
    problems = check(with_(ENVIRONMENT=environment, TESTING=flag))
    expected = _TESTING_REFUSAL.format(environment)
    assert problems == [expected]
    assert preflight.render(problems) == f"[preflight] {expected}"


def test_testing_through_the_legacy_app_env_is_refused_too():
    problems = check(with_(ENVIRONMENT=None, APP_ENV="prod", TESTING="true"))
    assert problems == [_TESTING_REFUSAL.format("production")]


@pytest.mark.parametrize("flag", ["", "false", "0", "off", "N"])
def test_an_empty_or_false_testing_flag_is_not_refused(flag):
    assert check(with_(TESTING=flag)) == []


def test_testing_in_development_is_not_refused():
    assert check({"ENVIRONMENT": "development", "TESTING": "true"}) == []


@pytest.mark.regression
def test_main_exits_78_for_testing_in_production(capsys):
    assert preflight.main(with_(TESTING="true")) == preflight.EX_CONFIG
    err = capsys.readouterr().err
    assert err == f"[preflight] {_TESTING_REFUSAL.format('production')}\n"


@pytest.mark.regression
def test_what_the_check_refuses_for_testing_the_settings_refuse_too():
    env = with_(TESTING="on")
    assert check(env)
    result = _settings_build(env)
    assert result.returncode != 0, result.stdout
    assert "TESTING is for the test runner" in result.stderr, result.stderr[-2000:]


@pytest.mark.regression
def test_main_exits_78_for_the_test_environment(capsys):
    assert preflight.main({"ENVIRONMENT": "test"}) == preflight.EX_CONFIG
    assert "ENVIRONMENT=test is for the test runner" in capsys.readouterr().err


def test_values_in_the_settings_dotenv_file_count(tmp_path):
    """The settings read .env.prod from the working directory; so does this."""
    (tmp_path / ".env.prod").write_text(
        f"SECRET_KEY={_KEY}\nFIRST_SUPERUSER_PASSWORD={_PASSWORD}\n"
    )
    env = with_(SECRET_KEY=None, FIRST_SUPERUSER_PASSWORD=None)
    assert preflight.check(env, full_profile=True, root=tmp_path) == []
    # ...and the process environment wins over the file, as in pydantic-settings.
    env["SECRET_KEY"] = "short"
    only_problem(preflight.check(env, full_profile=True, root=tmp_path), "SECRET_KEY")


# ---------------------------------------------------------------------------
# The check agrees with the settings it stands in front of
# ---------------------------------------------------------------------------

_BUILD_SETTINGS = """
import backend.app.core.config as config
print(config.settings.ENVIRONMENT)
"""


def _settings_build(environ) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO_ROOT), **environ}
    return subprocess.run(
        [sys.executable, "-c", _BUILD_SETTINGS],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.regression
def test_what_the_check_accepts_the_core_settings_accept():
    """The check must never be stricter than the settings on what they share."""
    result = _settings_build(GOOD)
    assert result.returncode == 0, result.stderr[-2000:]


@pytest.mark.parametrize(
    "changes",
    [
        {"SECRET_KEY": "short"},
        {"FIRST_SUPERUSER_PASSWORD": "Demo1234!"},
        {"PUBLIC_BASE_URL": None},
        {"ALLOWED_HOSTS": "*"},
    ],
)
def test_what_the_check_refuses_the_core_settings_refuse_too(changes):
    env = with_(**changes)
    assert check(env)
    result = _settings_build(env)
    assert result.returncode != 0, result.stdout
    assert next(iter(changes)) in result.stderr, result.stderr[-2000:]


# ---------------------------------------------------------------------------
# ALLOWED_HOSTS: the check and the settings apply one rule, in every environment
# ---------------------------------------------------------------------------

#: (value, refused). `*example.com` is the silent-outage typo (a literal host
#: with an asterisk: matches nothing). `http://example.com` and a blank entry
#: are what the settings accept today -- the scheme'd one as a literal host
#: that no Host header will equal, the blank one dropped by the parser -- and
#: the check must agree with them, not be stricter.
HOST_CASES = [
    ("*example.com", True),
    ("example.*", True),
    (".example.com", True),
    ('["*example.com"]', True),
    ("[not json", True),
    ("http://example.com", False),
    ("a.example.com,,b.example.com", False),
    ("*.example.com", False),
    ('["a.example.com", "*.b.example.com"]', False),
]


@pytest.mark.regression
@pytest.mark.parametrize("environment", ["production", "development"])
@pytest.mark.parametrize("hosts, refused", HOST_CASES)
def test_allowed_hosts_patterns_are_judged_as_the_settings_judge_them(
    hosts, refused, environment
):
    """#277 review: `*example.com` passed the check, then the process died on
    a pydantic traceback after the database wait."""
    env = with_(ENVIRONMENT=environment, ALLOWED_HOSTS=hosts)
    problems = check(env)
    assert bool(problems) is refused, problems
    if refused:
        assert all(p.startswith("ALLOWED_HOSTS") for p in problems), problems
    result = _settings_build(env)
    assert (result.returncode != 0) is refused, result.stderr[-2000:]
    if refused:
        assert "ALLOWED_HOSTS" in result.stderr or "JSON" in result.stderr


# ---------------------------------------------------------------------------
# AUDIT_HMAC_KEY: the check and the modules' settings builder agree (full)
# ---------------------------------------------------------------------------

_BUILD_MODULE_SETTINGS = (
    _BUILD_SETTINGS
    + """
from modules.backend.app.settings import build_modules_settings
build_modules_settings()
print("modules settings built")
"""
)

_HAS_MODULES = (REPO_ROOT / "modules" / "backend" / "app" / "settings.py").is_file()


@pytest.mark.regression
@pytest.mark.skipif(not _HAS_MODULES, reason="core checkout: no modules package")
@pytest.mark.parametrize(
    "audit_key, refused",
    [
        (_KEY[::-1], False),
        (None, True),
        ("too-short-audit-key", True),
        ("dev-audit-key-change-in-production", True),
        ("dev-audit-" + "x" * 40, True),
    ],
)
def test_the_audit_key_is_judged_as_the_modules_settings_judge_it(audit_key, refused):
    env = with_(AUDIT_HMAC_KEY=audit_key)
    problems = check(env, full_profile=True)
    assert bool(problems) is refused, problems
    result = subprocess.run(
        [sys.executable, "-c", _BUILD_MODULE_SETTINGS],
        cwd=REPO_ROOT,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO_ROOT), **env},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert (result.returncode != 0) is refused, result.stderr[-2000:]
    if refused:
        assert "AUDIT_HMAC_KEY" in result.stderr
    else:
        assert "modules settings built" in result.stdout


# ---------------------------------------------------------------------------
# WAREHOUSE_CREDENTIALS_KEYS: optional, but usable when set (full profile)
# ---------------------------------------------------------------------------

_FERNET_A = base64.urlsafe_b64encode(b"a" * 32).decode()
_FERNET_B = base64.urlsafe_b64encode(b"b" * 32).decode()

_CREDENTIAL_KEYS_CASES = [
    (None, False),
    ("", False),
    (_FERNET_A, False),
    (f"{_FERNET_B},{_FERNET_A}", False),
    ("not-a-fernet-key", True),
    (base64.urlsafe_b64encode(b"a" * 31).decode(), True),
    (f"{_FERNET_A},", True),
    (f"{_FERNET_A},changeme", True),
    ("changeme", True),
    ("test-credentials-key", True),
]


@pytest.mark.parametrize("keys, refused", _CREDENTIAL_KEYS_CASES)
def test_credential_keys_are_checked_in_the_full_profile(keys, refused):
    problems = check(with_(WAREHOUSE_CREDENTIALS_KEYS=keys), full_profile=True)
    if refused:
        only_problem(problems, "WAREHOUSE_CREDENTIALS_KEYS")
        assert keys not in problems[0]
    else:
        assert problems == []


def test_credential_keys_are_not_checked_in_the_core_profile():
    env = with_(AUDIT_HMAC_KEY=None, WAREHOUSE_CREDENTIALS_KEYS="not-a-fernet-key")
    assert check(env, full_profile=False) == []


def test_credential_keys_are_not_checked_in_development():
    env = {"ENVIRONMENT": "development", "WAREHOUSE_CREDENTIALS_KEYS": "junk"}
    assert check(env, full_profile=True) == []


@pytest.mark.regression
@pytest.mark.skipif(not _HAS_MODULES, reason="core checkout: no modules package")
@pytest.mark.parametrize("keys, refused", _CREDENTIAL_KEYS_CASES)
def test_credential_keys_are_judged_as_the_modules_settings_judge_them(keys, refused):
    env = with_(WAREHOUSE_CREDENTIALS_KEYS=keys)
    assert bool(check(env, full_profile=True)) is refused
    result = subprocess.run(
        [sys.executable, "-c", _BUILD_MODULE_SETTINGS],
        cwd=REPO_ROOT,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO_ROOT), **env},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert (result.returncode != 0) is refused, result.stderr[-2000:]
    if refused:
        assert "WAREHOUSE_CREDENTIALS_KEYS" in result.stderr
    else:
        assert "modules settings built" in result.stdout


# ---------------------------------------------------------------------------
# The entrypoint runs it first
# ---------------------------------------------------------------------------


def _executable_lines() -> list[str]:
    return [
        line.strip()
        for line in ENTRYPOINT.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


@pytest.mark.regression
def test_the_entrypoint_runs_the_check_before_the_database_wait():
    lines = _executable_lines()
    call = "python -m backend.app.core.preflight"
    assert call in lines, "docker-entrypoint.sh does not run the preflight"
    at = lines.index(call)
    # Before the POSTGRES_SERVER default is filled in (or the check could not
    # tell "unset" from "localhost"), and before anything waits or migrates.
    for later in (
        ': "${POSTGRES_SERVER:=${POSTGRES_HOST:-localhost}}"',
        "wait_for_db",
        "python -m backend.app.db.bootstrap",
    ):
        first = next(i for i, line in enumerate(lines) if later in line)
        assert at < first, f"the preflight runs after {later!r}"
    # Not tolerated: `|| true` or a pipe would throw its exit status away.
    assert lines[at] == call
