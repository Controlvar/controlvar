"""The keys two-step sign-in derives from ``SECRET_KEY`` (#1812).

The expected values below were computed outside the module, with RFC 5869's
HKDF written out on ``hmac`` and ``hashlib`` (no salt, SHA-256, 32 bytes), for
the fixed ``SECRET_KEY`` here and the two ``info`` strings of version 1. They
are literals, not the module's own constants: an ``info`` string edited in the
module, a different hash, a salt or a length fails here, because every secret
already stored would stop opening.

``SEALED_RFC_SEED`` is a Fernet token made outside the module with the first
derived key (a fixed IV and time), holding RFC 6238's SHA-1 seed: the module
must open it, and must refuse it under any other ``SECRET_KEY``.
"""

from __future__ import annotations

import pytest

from backend.app.core import two_factor_keys
from backend.app.core.two_factor_keys import (
    SecretUnreadable,
    derive_key,
    mfa_token_key,
    open_secret,
    seal_secret,
)

pytestmark = [pytest.mark.unit]

#: A fixed SECRET_KEY, 50 characters.
FIXED = "known-answer-secret-key-for-two-step-sign-in-tests"
#: HKDF-SHA256(FIXED, info="controlvar-totp-secret-v1"), computed outside.
EXPECTED_SEALING_DERIVATION = (
    "5cd3134d6a7514a61a78fdebdb94808a466b2870a7f97cd17e1050b7f83d9dee"
)
#: HKDF-SHA256(FIXED, info="controlvar-mfa-token-v1"), computed outside.
EXPECTED_MFA_DERIVATION = (
    "53217c3a6c4c9081cfc0cfbfff94c2dcafb49617ba2507f7350546112bc148c4"
)
#: RFC 6238's SHA-1 seed sealed outside the module under the first derivation.
SEALED_RFC_SEED = (
    "gAAAAABlU_EAAAAAAAAAAAAAAAAAAAAAAMlsksHYWvdR4w9c0GtlwTqTkx7sDtGtP5Uazp1ZqMk"
    "200tjER_mhPwufZ6byTHaVa3iN8hcVYV2Aq9dvBAHICw="
)
RFC_SEED = b"12345678901234567890"


@pytest.mark.regression
def test_the_sealing_key_is_the_committed_derivation():
    assert derive_key(b"controlvar-totp-secret-v1", FIXED).hex() == (
        EXPECTED_SEALING_DERIVATION
    )
    assert derive_key(two_factor_keys.TOTP_SECRET_INFO, FIXED).hex() == (
        EXPECTED_SEALING_DERIVATION
    )


@pytest.mark.regression
def test_the_mfa_token_key_is_the_committed_derivation():
    assert mfa_token_key(FIXED).hex() == EXPECTED_MFA_DERIVATION


def test_the_two_keys_differ_and_neither_is_the_secret_key():
    sealing = derive_key(two_factor_keys.TOTP_SECRET_INFO, FIXED)
    signing = mfa_token_key(FIXED)
    assert sealing != signing
    assert FIXED.encode() not in (sealing, signing)


@pytest.mark.regression
def test_a_secret_sealed_outside_the_module_opens():
    assert open_secret(SEALED_RFC_SEED, FIXED) == RFC_SEED


@pytest.mark.regression
def test_a_secret_sealed_under_another_secret_key_is_refused():
    with pytest.raises(SecretUnreadable):
        open_secret(SEALED_RFC_SEED, FIXED + "-rotated")
    sealed = seal_secret(RFC_SEED, FIXED + "-rotated")
    with pytest.raises(SecretUnreadable):
        open_secret(sealed, FIXED)


@pytest.mark.parametrize("damaged", ["", "not a token", "gAAAA", "é"])
def test_a_damaged_value_is_refused_never_returned(damaged):
    with pytest.raises(SecretUnreadable):
        open_secret(damaged, FIXED)


def test_seal_and_open_round_trip_and_the_token_is_never_the_secret():
    sealed = seal_secret(RFC_SEED, FIXED)
    assert open_secret(sealed, FIXED) == RFC_SEED
    assert RFC_SEED.decode() not in sealed
    # A new IV each time: the same secret seals differently.
    assert seal_secret(RFC_SEED, FIXED) != sealed


def test_the_default_is_the_settings_secret_key(monkeypatch):
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "SECRET_KEY", FIXED)
    assert mfa_token_key().hex() == EXPECTED_MFA_DERIVATION
    assert open_secret(SEALED_RFC_SEED) == RFC_SEED
