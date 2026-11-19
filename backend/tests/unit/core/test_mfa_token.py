"""The token between the password step and the code step (#1812).

It is signed with the mfa-token key and carries its own issuer, so it is
refused wherever an access token is read, and an access token is refused
where it is read. Pure functions: no database, no application.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest

from backend.app.core import mfa_token
from backend.app.core.config import settings
from backend.app.core.mfa_token import (
    MFA_TOKEN_ISSUER,
    VERIFY_PURPOSE,
    VERIFY_TTL_SECONDS,
    InvalidMfaToken,
    create_mfa_token,
    decode_mfa_token,
)
from backend.app.core.security import (
    LOCAL_TOKEN_ISSUER,
    InvalidTokenError,
    create_local_access_token,
    decode_local_token,
)
from backend.app.core.two_factor_keys import mfa_token_key

pytestmark = [pytest.mark.unit]


def _user():
    return SimpleNamespace(id=uuid.uuid4(), email="a@example.com", role="ADMIN")


def _claims(**overrides):
    now = int(time.time())
    claims = {
        "sub": str(uuid.uuid4()),
        "iss": MFA_TOKEN_ISSUER,
        "purpose": VERIFY_PURPOSE,
        "iat": now,
        "exp": now + 60,
        "jti": uuid.uuid4().hex,
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def test_a_token_names_its_account():
    user = _user()
    assert decode_mfa_token(create_mfa_token(user)) == user.id


def test_its_claims_are_exactly_these():
    user = _user()
    claims = jwt.decode(
        create_mfa_token(user),
        mfa_token_key(),
        algorithms=["HS256"],
        issuer=MFA_TOKEN_ISSUER,
    )
    assert set(claims) == {"sub", "iss", "purpose", "iat", "exp", "jti"}
    assert claims["sub"] == str(user.id)
    assert claims["purpose"] == "verify"
    assert claims["exp"] - claims["iat"] == VERIFY_TTL_SECONDS == 300
    assert len(claims["jti"]) == 32


def test_it_is_not_signed_with_the_secret_key():
    token = create_mfa_token(_user())
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])


@pytest.mark.regression
def test_an_mfa_token_is_refused_where_access_tokens_are_read():
    """Twice over: the signature (SECRET_KEY) and the issuer."""
    with pytest.raises(InvalidTokenError):
        decode_local_token(create_mfa_token(_user()))
    # Even signed with SECRET_KEY, the mfa issuer is refused.
    forged = jwt.encode(_claims(), settings.SECRET_KEY, algorithm="HS256")
    with pytest.raises(InvalidTokenError):
        decode_local_token(forged)


@pytest.mark.regression
def test_an_access_token_is_refused_where_mfa_tokens_are_read():
    with pytest.raises(InvalidMfaToken):
        decode_mfa_token(create_local_access_token(_user()))
    # Signed with the mfa key but with the access tokens' issuer.
    forged = jwt.encode(
        _claims(iss=LOCAL_TOKEN_ISSUER), mfa_token_key(), algorithm="HS256"
    )
    with pytest.raises(InvalidMfaToken):
        decode_mfa_token(forged)


def test_an_expired_token_is_refused():
    past = datetime.now(timezone.utc) - timedelta(seconds=VERIFY_TTL_SECONDS + 5)
    with pytest.raises(InvalidMfaToken):
        decode_mfa_token(create_mfa_token(_user(), now=past))


def test_a_token_of_another_purpose_is_refused():
    token = create_mfa_token(_user(), purpose="enrol")
    with pytest.raises(InvalidMfaToken):
        decode_mfa_token(token)
    assert decode_mfa_token(token, purpose="enrol")


def test_a_token_signed_with_another_key_is_refused():
    forged = jwt.encode(_claims(), "x" * 32, algorithm="HS256")
    with pytest.raises(InvalidMfaToken):
        decode_mfa_token(forged)


@pytest.mark.parametrize("missing", ["sub", "iss", "purpose", "iat", "exp", "jti"])
def test_every_claim_is_required(missing):
    forged = jwt.encode(_claims(**{missing: None}), mfa_token_key(), algorithm="HS256")
    with pytest.raises(InvalidMfaToken):
        decode_mfa_token(forged)


def test_a_sub_that_is_not_an_account_id_is_refused():
    forged = jwt.encode(_claims(sub="admin"), mfa_token_key(), algorithm="HS256")
    with pytest.raises(InvalidMfaToken):
        decode_mfa_token(forged)


@pytest.mark.parametrize("value", [None, "", 7, "garbage", "a.b.c"])
def test_anything_else_is_refused(value):
    with pytest.raises(InvalidMfaToken):
        decode_mfa_token(value)


def test_the_none_algorithm_is_refused():
    unsigned = jwt.encode(_claims(), None, algorithm="none")
    with pytest.raises(InvalidMfaToken):
        decode_mfa_token(unsigned)


def test_it_follows_a_changed_secret_key(monkeypatch):
    """No setting of its own: a new SECRET_KEY refuses every earlier token."""
    token = create_mfa_token(_user())
    monkeypatch.setattr(settings, "SECRET_KEY", "another-secret-key-of-enough-length")
    with pytest.raises(InvalidMfaToken):
        decode_mfa_token(token)


def test_the_purpose_is_compared_exactly():
    """No prefix match and no case folding."""
    for purpose in ("Verify", "verify ", "verif", "verifyx"):
        token = create_mfa_token(_user(), purpose=purpose)
        with pytest.raises(InvalidMfaToken):
            mfa_token.decode_mfa_token(token)
