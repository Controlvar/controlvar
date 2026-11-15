"""``modules/backend/app/core/credential_crypto.py`` and ``WAREHOUSE_CREDENTIALS_KEYS``.

The helper encrypts stored credentials with operator-provided Fernet keys:
the first key encrypts, every key decrypts, ``rotate`` moves a token onto the
first key.  Without keys it raises ``CredentialKeysUnavailable``; a token no
key opens raises ``CredentialUndecryptable``; nothing falls back to plaintext.
The setting is optional everywhere, and a malformed or placeholder value is
refused when the settings are built in staging or production.
"""

from __future__ import annotations

import base64
import traceback

import pytest
from pydantic import ValidationError

from backend.app.modules_loader import describe_exception
from modules.backend.app import settings as modules_settings
from modules.backend.app.core import credential_crypto
from modules.backend.app.core.credential_crypto import (
    CredentialCryptoError,
    CredentialKeysUnavailable,
    CredentialUndecryptable,
    decrypt,
    encrypt,
    rotate,
)
from modules.backend.app.settings import ModulesSettings

pytestmark = pytest.mark.unit


def _key(fill: bytes) -> str:
    """A fixed, well-formed Fernet key: parametrize ids stay deterministic,
    which pytest-xdist requires of every worker's collection."""
    return base64.urlsafe_b64encode(fill * 32).decode()


KEY_A = _key(b"\xa1")
KEY_B = _key(b"\xb2")
KEY_C = _key(b"\xc3")

#: A strong audit key, so a hardened ModulesSettings fails only on the field
#: under test.
_STRONG = {"AUDIT_HMAC_KEY": "c" * 64}

#: Planted in plaintexts and rejected key values; it must never surface in an
#: exception message, repr or formatted traceback.
SENTINEL = "SENTINEL-7f3a9c-credential"

HARDENED = ["staging", "production"]
RELAXED = ["development", "test"]

#: Values that are not a list of Fernet keys.
MALFORMED = [
    pytest.param("not-a-fernet-key", id="not-base64"),
    pytest.param(base64.urlsafe_b64encode(b"x" * 31).decode(), id="31-bytes"),
    pytest.param(base64.urlsafe_b64encode(b"x" * 33).decode(), id="33-bytes"),
    pytest.param(KEY_A[:-2], id="truncated"),
    pytest.param(f"{KEY_A},", id="trailing-empty-entry"),
    pytest.param(f"{KEY_A},,{KEY_B}", id="empty-entry-between"),
    pytest.param(f"{KEY_A},garbage", id="good-then-bad"),
    # Fernet() itself discards the stray "!"; the setting is stricter.
    pytest.param("!" + KEY_A, id="stray-character"),
    pytest.param(KEY_A[:-1] + "\u00e9", id="non-ascii"),
]

#: Published placeholders (settings_rules.PLACEHOLDER_SECRETS and prefixes).
PLACEHOLDERS = [
    pytest.param("changeme", id="changeme"),
    pytest.param("secret", id="secret"),
    pytest.param("test-credentials-key", id="test-prefix"),
    pytest.param("dev-only-credentials-key", id="dev-only-prefix"),
    pytest.param(f"{KEY_A},changeme", id="good-then-placeholder"),
]


@pytest.fixture
def configured(monkeypatch):
    """Set ``WAREHOUSE_CREDENTIALS_KEYS`` on the process-wide instance."""
    instance = modules_settings.load_modules_settings()

    def set_keys(value):
        monkeypatch.setattr(instance, "WAREHOUSE_CREDENTIALS_KEYS", value)

    return set_keys


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_round_trip_through_the_setting(self, configured):
        configured(KEY_A)
        token = encrypt(b"s3cr3t-password")
        assert token != b"s3cr3t-password"
        assert b"s3cr3t-password" not in token
        assert decrypt(token) == b"s3cr3t-password"

    def test_round_trip_with_explicit_keys(self):
        token = encrypt(b"payload", keys=KEY_A)
        assert decrypt(token, keys=KEY_A) == b"payload"
        assert decrypt(token.decode(), keys=KEY_A) == b"payload"

    def test_each_encryption_is_distinct(self):
        assert encrypt(b"same", keys=KEY_A) != encrypt(b"same", keys=KEY_A)

    def test_empty_and_binary_payloads(self):
        for payload in (b"", bytes(range(256)), b"x" * 100_000):
            assert decrypt(encrypt(payload, keys=KEY_A), keys=KEY_A) == payload

    def test_whitespace_around_keys_is_ignored(self):
        token = encrypt(b"p", keys=f" {KEY_A} , {KEY_B} ")
        assert decrypt(token, keys=KEY_A) == b"p"

    def test_encrypt_refuses_str(self):
        with pytest.raises(TypeError):
            encrypt("not bytes", keys=KEY_A)  # type: ignore[arg-type]


class TestRotation:
    @pytest.mark.regression
    def test_the_rotation_sequence(self, configured):
        """Encrypt under A; configure ``B,A``: old tokens still decrypt and
        ``rotate`` re-encrypts under B; configure ``B``: a token left under A
        is refused with the typed error, the rotated one still opens."""
        configured(KEY_A)
        under_a = encrypt(b"warehouse-password")
        left_behind = encrypt(b"never-rotated")

        configured(f"{KEY_B},{KEY_A}")
        assert decrypt(under_a) == b"warehouse-password"
        rotated = rotate(under_a)
        assert rotated != under_a
        # New encryptions and the rotated token are under B alone.
        assert decrypt(rotated, keys=KEY_B) == b"warehouse-password"
        assert decrypt(encrypt(b"new"), keys=KEY_B) == b"new"
        with pytest.raises(CredentialUndecryptable):
            decrypt(rotated, keys=KEY_A)

        configured(KEY_B)
        assert decrypt(rotated) == b"warehouse-password"
        with pytest.raises(CredentialUndecryptable):
            decrypt(left_behind)
        with pytest.raises(CredentialUndecryptable):
            rotate(left_behind)

    def test_rotate_is_idempotent_on_the_first_key(self):
        token = encrypt(b"p", keys=KEY_B)
        assert decrypt(rotate(token, keys=f"{KEY_B},{KEY_A}"), keys=KEY_B) == b"p"


class TestNoKeys:
    @pytest.mark.parametrize("value", [None, "", "   "])
    @pytest.mark.parametrize("call", ["encrypt", "decrypt", "rotate"])
    def test_absent_raises_the_typed_error(self, configured, value, call):
        configured(value)
        token = encrypt(b"p", keys=KEY_A)
        argument = b"p" if call == "encrypt" else token
        with pytest.raises(
            CredentialKeysUnavailable, match="WAREHOUSE_CREDENTIALS_KEYS"
        ):
            getattr(credential_crypto, call)(argument)

    def test_the_typed_errors_share_a_base(self):
        assert issubclass(CredentialKeysUnavailable, CredentialCryptoError)
        assert issubclass(CredentialUndecryptable, CredentialCryptoError)
        assert not issubclass(CredentialKeysUnavailable, CredentialUndecryptable)

    @pytest.mark.parametrize("value", MALFORMED + PLACEHOLDERS)
    def test_unusable_keys_at_use_raise_the_typed_error(self, configured, value):
        """Development and test do not refuse these at boot; use does."""
        configured(value)
        with pytest.raises(
            CredentialKeysUnavailable, match="WAREHOUSE_CREDENTIALS_KEYS"
        ):
            encrypt(b"p")


class TestNoFallback:
    @pytest.mark.parametrize(
        "token",
        [
            b"plaintext-password",
            base64.urlsafe_b64encode(b"plaintext-password"),
            "plaintext-password",
            "",
            "café",
        ],
    )
    def test_a_plaintext_or_encoded_value_is_never_accepted(self, token):
        with pytest.raises(CredentialUndecryptable):
            decrypt(token, keys=KEY_A)
        with pytest.raises(CredentialUndecryptable):
            rotate(token, keys=KEY_A)

    def test_a_tampered_token_is_refused(self):
        token = bytearray(encrypt(b"p", keys=KEY_A))
        token[-5] = ord("A") if token[-5] != ord("A") else ord("B")
        with pytest.raises(CredentialUndecryptable):
            decrypt(bytes(token), keys=KEY_A)

    def test_a_non_token_type_is_a_type_error(self):
        with pytest.raises(TypeError):
            decrypt(12345, keys=KEY_A)  # type: ignore[arg-type]


def _everything_said(exc: BaseException) -> str:
    return "\n".join([str(exc), repr(exc), "".join(traceback.format_exception(exc))])


class TestNothingSecretInTheErrors:
    @pytest.mark.regression
    def test_undecryptable_names_no_key_token_or_plaintext(self):
        token = encrypt(SENTINEL.encode(), keys=KEY_A)
        for call in (decrypt, rotate):
            with pytest.raises(CredentialUndecryptable) as excinfo:
                call(token, keys=KEY_B)
            said = _everything_said(excinfo.value)
            for secret in (SENTINEL, KEY_A, KEY_B, token.decode()):
                assert secret not in said, call.__name__

    @pytest.mark.regression
    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(SENTINEL, id="bare"),
            pytest.param(f"{KEY_A},{SENTINEL}", id="after-a-good-key"),
            pytest.param(f"test-{SENTINEL}", id="placeholder-prefix"),
            pytest.param(
                base64.urlsafe_b64encode(SENTINEL.encode()).decode(), id="b64"
            ),
        ],
    )
    def test_unusable_keys_are_not_echoed_at_use(self, value):
        with pytest.raises(CredentialKeysUnavailable) as excinfo:
            encrypt(b"p", keys=value)
        said = _everything_said(excinfo.value)
        for secret in (SENTINEL, KEY_A, value):
            assert secret not in said

    @pytest.mark.regression
    @pytest.mark.parametrize("env", HARDENED)
    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(SENTINEL, id="bare"),
            pytest.param(f"{KEY_A},{SENTINEL}", id="after-a-good-key"),
            pytest.param(f"test-{SENTINEL}", id="placeholder-prefix"),
        ],
    )
    def test_the_boot_refusal_does_not_echo_the_value(self, env, value, monkeypatch):
        """The validator's own sentence -- what the loader logs, via
        ``describe_exception`` -- names the setting, never the value.
        (pydantic's own ``str(ValidationError)`` carries ``input_value``;
        that is why the loader never renders it.)"""
        monkeypatch.delenv("TESTING", raising=False)
        with pytest.raises(ValidationError) as excinfo:
            ModulesSettings(
                _env_file=None,
                ENVIRONMENT=env,
                WAREHOUSE_CREDENTIALS_KEYS=value,
                **_STRONG,
            )
        messages = " ".join(e["msg"] for e in excinfo.value.errors())
        described = describe_exception(excinfo.value)
        assert "WAREHOUSE_CREDENTIALS_KEYS" in described
        for secret in (SENTINEL, KEY_A):
            assert secret not in messages
            assert secret not in described


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------


class TestTheSetting:
    def test_it_is_a_module_setting_not_a_core_one(self):
        from backend.app.core.config import Settings

        assert "WAREHOUSE_CREDENTIALS_KEYS" in ModulesSettings.model_fields
        assert "WAREHOUSE_CREDENTIALS_KEYS" not in Settings.model_fields

    @pytest.mark.parametrize("env", HARDENED + RELAXED)
    @pytest.mark.parametrize("value", [None, "", "  "])
    def test_absent_is_allowed_at_boot_everywhere(self, env, value, monkeypatch):
        monkeypatch.delenv("TESTING", raising=False)
        built = ModulesSettings(
            _env_file=None, ENVIRONMENT=env, WAREHOUSE_CREDENTIALS_KEYS=value, **_STRONG
        )
        assert built.WAREHOUSE_CREDENTIALS_KEYS == value

    @pytest.mark.parametrize("env", HARDENED + RELAXED)
    def test_an_empty_value_from_the_environment_reads_as_not_set(
        self, env, monkeypatch
    ):
        """Both compose files pass the setting empty by default (#1253, #256).
        Empty boots everywhere, and using it raises the error an absent
        setting raises, which says to set it."""
        monkeypatch.delenv("TESTING", raising=False)
        monkeypatch.setenv("WAREHOUSE_CREDENTIALS_KEYS", "")
        built = ModulesSettings(_env_file=None, ENVIRONMENT=env, **_STRONG)
        assert built.WAREHOUSE_CREDENTIALS_KEYS == ""
        with pytest.raises(
            CredentialKeysUnavailable, match="^WAREHOUSE_CREDENTIALS_KEYS is not set,"
        ):
            credential_crypto._cipher(built.WAREHOUSE_CREDENTIALS_KEYS)

    @pytest.mark.parametrize("env", HARDENED)
    def test_the_class_default_is_absent_and_boots(self, env, monkeypatch):
        monkeypatch.delenv("TESTING", raising=False)
        monkeypatch.delenv("WAREHOUSE_CREDENTIALS_KEYS", raising=False)
        built = ModulesSettings(_env_file=None, ENVIRONMENT=env, **_STRONG)
        assert built.WAREHOUSE_CREDENTIALS_KEYS is None

    @pytest.mark.parametrize("env", HARDENED)
    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(KEY_A, id="one-key"),
            pytest.param(f"{KEY_B},{KEY_A}", id="two-keys"),
            pytest.param(f"{KEY_C},{KEY_B},{KEY_A}", id="three-keys"),
        ],
    )
    def test_real_keys_are_accepted_in_hardened_environments(
        self, env, value, monkeypatch
    ):
        monkeypatch.delenv("TESTING", raising=False)
        built = ModulesSettings(
            _env_file=None, ENVIRONMENT=env, WAREHOUSE_CREDENTIALS_KEYS=value, **_STRONG
        )
        assert built.WAREHOUSE_CREDENTIALS_KEYS == value

    @pytest.mark.regression
    @pytest.mark.parametrize("env", HARDENED)
    @pytest.mark.parametrize("value", MALFORMED)
    def test_a_malformed_value_is_refused_at_boot(self, env, value, monkeypatch):
        monkeypatch.delenv("TESTING", raising=False)
        with pytest.raises(ValidationError, match="WAREHOUSE_CREDENTIALS_KEYS"):
            ModulesSettings(
                _env_file=None,
                ENVIRONMENT=env,
                WAREHOUSE_CREDENTIALS_KEYS=value,
                **_STRONG,
            )

    @pytest.mark.regression
    @pytest.mark.parametrize("env", HARDENED)
    @pytest.mark.parametrize("value", PLACEHOLDERS)
    def test_a_placeholder_is_refused_at_boot(self, env, value, monkeypatch):
        monkeypatch.delenv("TESTING", raising=False)
        with pytest.raises(ValidationError, match="WAREHOUSE_CREDENTIALS_KEYS"):
            ModulesSettings(
                _env_file=None,
                ENVIRONMENT=env,
                WAREHOUSE_CREDENTIALS_KEYS=value,
                **_STRONG,
            )

    @pytest.mark.parametrize("env", HARDENED)
    def test_a_malformed_value_from_the_process_environment_is_refused(
        self, env, monkeypatch
    ):
        monkeypatch.delenv("TESTING", raising=False)
        monkeypatch.setenv("WAREHOUSE_CREDENTIALS_KEYS", "not-a-fernet-key")
        with pytest.raises(ValidationError, match="WAREHOUSE_CREDENTIALS_KEYS"):
            ModulesSettings(_env_file=None, ENVIRONMENT=env, **_STRONG)

    @pytest.mark.parametrize("env", RELAXED)
    @pytest.mark.parametrize("value", MALFORMED[:2] + PLACEHOLDERS[:1])
    def test_development_and_test_do_not_refuse_at_boot(self, env, value, monkeypatch):
        """Only staging and production refuse at boot; use refuses everywhere
        (``TestNoKeys.test_unusable_keys_at_use_raise_the_typed_error``)."""
        monkeypatch.delenv("TESTING", raising=False)
        built = ModulesSettings(
            _env_file=None, ENVIRONMENT=env, WAREHOUSE_CREDENTIALS_KEYS=value
        )
        assert built.WAREHOUSE_CREDENTIALS_KEYS == value
