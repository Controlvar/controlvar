"""The password stamp in every local token (#501).

``security.password_stamp`` is a keyed digest of the account id and the
stored ``hashed_password``; a token carries it in ``pwv``, and a token whose
stamp no longer matches the account is refused. Its format is part of the
token format, so it is pinned here by known answers computed outside the
module (RFC 5869's HKDF written out on ``hmac`` and ``hashlib``, as
``test_two_factor_keys.py`` does): a changed ``info`` string, length or
encoding fails here, because it would sign everyone out once.

Pure functions, no database, no application.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.core import security
from backend.app.core.config import settings
from backend.app.core.security import (
    PASSWORD_STAMP_CLAIM,
    PASSWORD_STAMP_INFO,
    PASSWORD_STAMP_LENGTH,
    get_password_hash,
    password_stamp,
    stamp_is_current,
)
from backend.app.core.two_factor_keys import (
    MFA_TOKEN_INFO,
    TOTP_SECRET_INFO,
    derive_key,
)

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]

#: A fixed SECRET_KEY, 50 characters.
FIXED = "known-answer-secret-key-for-the-password-stamp-501"
#: A fixed account and a fixed stored value (not a real hash: any text works).
ACCOUNT = uuid.UUID("0b7c4f2e-5a1d-4c3b-9e8f-6a2d1c0b9e8f")
STORED = "stored-hash-for-the-known-answer-vector"
#: HKDF-SHA256(FIXED, info="controlvar-access-token-pwv-v1"), computed outside.
EXPECTED_KEY = "8ccd7d692e524857140f76e3f8c7d93e29353d3c73efc6e3e7b072a0012cf3f2"
#: The stamp of ACCOUNT with STORED, and with no password, computed outside.
EXPECTED_STAMP = "yVMwjLSkolO4rWCNp5hX6w"
EXPECTED_STAMP_NO_PASSWORD = "GZuVi_u0vEzb7a-WxBY4xA"


@pytest.fixture
def fixed_secret(monkeypatch):
    monkeypatch.setattr(settings, "SECRET_KEY", FIXED)


def _account(**fields):
    values = {"id": ACCOUNT, "hashed_password": STORED}
    values.update(fields)
    return SimpleNamespace(**values)


# ---------------------------------------------------------------------------
# The format
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_the_key_is_the_committed_derivation():
    assert derive_key(b"controlvar-access-token-pwv-v1", FIXED).hex() == EXPECTED_KEY
    assert derive_key(PASSWORD_STAMP_INFO, FIXED).hex() == EXPECTED_KEY


@pytest.mark.regression
def test_the_stamp_is_the_committed_value(fixed_secret):
    assert password_stamp(_account()) == EXPECTED_STAMP
    assert password_stamp(_account(hashed_password=None)) == (
        EXPECTED_STAMP_NO_PASSWORD
    )
    # The id may arrive as a string (a token's ``sub``); the bytes are the same.
    assert password_stamp(_account(id=str(ACCOUNT))) == EXPECTED_STAMP


def test_the_claim_and_its_length():
    assert PASSWORD_STAMP_CLAIM == "pwv"
    assert PASSWORD_STAMP_LENGTH == 22
    stamp = password_stamp(_account(id=uuid.uuid4()))
    assert len(stamp) == PASSWORD_STAMP_LENGTH
    assert stamp.isascii() and "=" not in stamp


def test_each_key_has_its_own_info():
    infos = {TOTP_SECRET_INFO, MFA_TOKEN_INFO, PASSWORD_STAMP_INFO}
    assert len(infos) == 3
    assert len({derive_key(info, FIXED) for info in infos}) == 3


def test_every_process_derives_the_same_stamp(fixed_secret):
    """No per-process key: two API processes agree on every stamp."""
    program = (
        "import uuid\n"
        "from types import SimpleNamespace\n"
        "from backend.app.core.security import password_stamp\n"
        f"user = SimpleNamespace(id=uuid.UUID({str(ACCOUNT)!r}), "
        f"hashed_password={STORED!r})\n"
        "print('STAMP=' + password_stamp(user))\n"
    )
    env = {**os.environ, "SECRET_KEY": FIXED}
    seen = []
    for _ in range(2):
        done = subprocess.run(
            [sys.executable, "-c", program],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert done.returncode == 0, done.stderr[-2000:]
        (line,) = [x for x in done.stdout.splitlines() if x.startswith("STAMP=")]
        seen.append(line.removeprefix("STAMP="))
    assert seen == [EXPECTED_STAMP, EXPECTED_STAMP]
    assert password_stamp(_account()) == EXPECTED_STAMP


def test_it_follows_a_changed_secret_key(fixed_secret, monkeypatch):
    before = password_stamp(_account())
    monkeypatch.setattr(settings, "SECRET_KEY", FIXED + "-rotated")
    assert password_stamp(_account()) != before


# ---------------------------------------------------------------------------
# What changes it
# ---------------------------------------------------------------------------


def test_a_new_hash_of_the_same_password_changes_it():
    """bcrypt salts every hash: setting the same password again is a change."""
    first = get_password_hash("Same-Passw0rd-501")
    second = get_password_hash("Same-Passw0rd-501")
    assert first != second
    assert password_stamp(_account(hashed_password=first)) != password_stamp(
        _account(hashed_password=second)
    )


def test_another_account_with_the_same_hash_has_another_stamp():
    assert password_stamp(_account(id=uuid.uuid4())) != password_stamp(
        _account(id=uuid.uuid4())
    )


@pytest.mark.parametrize(
    "field, value",
    [
        ("email", "renamed@example.com"),
        ("username", "renamed"),
        ("full_name", "Renamed Person"),
        ("role", "ADMIN"),
        ("is_superuser", True),
        ("totp_enabled", True),
        ("is_active", False),
        ("updated_at", "2030-01-01T00:00:00"),
    ],
)
def test_only_the_password_moves_it(field, value):
    """Changing the address, the name, the role, superuser, two-step sign-in
    or the last-updated time leaves every session as it was."""
    user = _account(
        id=uuid.uuid4(),
        email="before@example.com",
        username="before",
        full_name="Before",
        role="VIEWER",
        is_superuser=False,
        totp_enabled=False,
        is_active=True,
        updated_at="2026-01-01T00:00:00",
    )
    claims = {PASSWORD_STAMP_CLAIM: password_stamp(user)}
    setattr(user, field, value)
    assert stamp_is_current(claims, user)


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


def test_a_matching_stamp_is_current():
    user = _account(id=uuid.uuid4())
    assert stamp_is_current({PASSWORD_STAMP_CLAIM: password_stamp(user)}, user)


def test_a_stamp_from_before_the_change_is_not():
    user = _account(id=uuid.uuid4())
    claims = {PASSWORD_STAMP_CLAIM: password_stamp(user)}
    user.hashed_password = STORED + "-changed"
    assert not stamp_is_current(claims, user)


def test_no_stamp_is_accepted():
    """Tokens from before the stamp existed: accepted until they expire."""
    assert stamp_is_current({"sub": str(ACCOUNT)}, _account())


@pytest.mark.parametrize(
    "value",
    [12345, None, "", "x" * 23, "x" * 21, "\ud800" * 22, "é" * 22, ["x"], {"a": 1}],
    ids=[
        "int",
        "null",
        "empty",
        "23-characters",
        "21-characters",
        "lone-surrogates",
        "non-ascii",
        "list",
        "object",
    ],
)
def test_a_malformed_stamp_is_not_current(value):
    assert stamp_is_current({PASSWORD_STAMP_CLAIM: value}, _account()) is False


def test_an_account_whose_stamp_cannot_be_computed_is_not_current():
    """An id that is not a UUID: refused, never raised."""
    claims = {PASSWORD_STAMP_CLAIM: "x" * PASSWORD_STAMP_LENGTH}
    assert stamp_is_current(claims, _account(id="not-a-uuid")) is False
    assert stamp_is_current(claims, _account(hashed_password="\ud800")) is False


def test_the_comparison_takes_constant_time(monkeypatch):
    """``hmac.compare_digest`` decides, on bytes."""
    calls = []
    real = security.hmac.compare_digest

    def spy(a, b):
        calls.append((type(a), type(b)))
        return real(a, b)

    monkeypatch.setattr(security.hmac, "compare_digest", spy)
    user = _account(id=uuid.uuid4())
    assert stamp_is_current({PASSWORD_STAMP_CLAIM: password_stamp(user)}, user)
    assert calls == [(bytes, bytes)]
