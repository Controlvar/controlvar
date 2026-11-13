"""``core/totp.py``: RFC 6238's own vectors, the window, and the link (#1812).

The six SHA-1 vectors of RFC 6238 Appendix B, as the 6 digits an app
shows (the last six of the RFC's eight; ``005924`` keeps its zeros). Each row
names its vector, so a wrong hash, modulus, mask or padding fails on a named
time.

The window, as an exact table of steps around a fixed second: the step
before and the step after are accepted, two either side are not, and the
29th and 30th second of a step fall in different steps.

No wall clock anywhere: every time is a literal.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from backend.app.core import totp

pytestmark = [pytest.mark.unit]

#: RFC 6238 Appendix B's SHA-1 seed: the ASCII digits 1 to 0, twice.
RFC_KEY = b"12345678901234567890"

#: (Unix time, the RFC's 8-digit TOTP): Appendix B, SHA-1 rows.
RFC_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]


@pytest.mark.regression
@pytest.mark.parametrize(
    "unix_time,rfc", RFC_VECTORS, ids=[str(t) for t, _ in RFC_VECTORS]
)
def test_the_rfc_6238_sha1_vectors_as_six_digits(unix_time, rfc):
    assert totp.totp_at(RFC_KEY, unix_time) == rfc[-6:]


def test_the_eight_digit_vectors_too():
    """The same function with ``digits=8`` gives the RFC's own strings."""
    for unix_time, rfc in RFC_VECTORS:
        assert totp.totp_at(RFC_KEY, unix_time, digits=8) == rfc, unix_time


def test_a_code_keeps_its_leading_zeros():
    assert totp.totp_at(RFC_KEY, 1234567890) == "005924"
    assert totp.totp_at(RFC_KEY, 1111111109) == "081804"


def test_steps_are_thirty_seconds_from_the_epoch():
    assert totp.STEP_SECONDS == 30
    assert totp.step_at(0) == 0
    assert totp.step_at(29) == 0
    assert totp.step_at(29.999) == 0
    assert totp.step_at(30) == 1
    assert totp.step_at(59) == 1
    assert totp.step_at(1234567890) == 41152263


# --- the window -------------------------------------------------------------------

#: A second in the middle of a step, far from the epoch.
NOW = 1234567890
STEP = totp.step_at(NOW)


@pytest.mark.regression
@pytest.mark.parametrize(
    "offset,accepted",
    [(-2, False), (-1, True), (0, True), (1, True), (2, False)],
    ids=["step-2", "step-1", "step", "step+1", "step+2"],
)
def test_the_window_is_one_step_either_side(offset, accepted):
    code = totp.hotp(RFC_KEY, STEP + offset)
    found = totp.matching_step(RFC_KEY, code, NOW)
    if accepted:
        assert found == STEP + offset
    else:
        assert found is None


@pytest.mark.regression
def test_the_29th_and_30th_second_of_a_step_are_different_steps():
    start = 30 * 41152300  # the first second of a step
    code_29 = totp.totp_at(RFC_KEY, start + 29)
    code_30 = totp.totp_at(RFC_KEY, start + 30)
    assert code_29 == totp.hotp(RFC_KEY, 41152300)
    assert code_30 == totp.hotp(RFC_KEY, 41152301)
    assert code_29 != code_30
    # At second 29, the next step's code is one step ahead: accepted.
    assert totp.matching_step(RFC_KEY, code_30, start + 29) == 41152301
    # At second 30 of the step after, step 41152300 is one behind: accepted;
    # two steps on (second 60), it is two behind: refused.
    assert totp.matching_step(RFC_KEY, code_29, start + 30) == 41152300
    assert totp.matching_step(RFC_KEY, code_29, start + 60) is None


@pytest.mark.regression
@pytest.mark.parametrize(
    "offset", [-1, None], ids=["match-at-the-first-step", "no-match"]
)
def test_every_step_of_the_window_is_compared_with_compare_digest(offset, monkeypatch):
    """The three steps are each compared with ``hmac.compare_digest``, all
    three whether the first matches or none does, so the time taken does not
    say which step matched."""
    calls = []
    real = totp.hmac.compare_digest

    def counting(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(totp.hmac, "compare_digest", counting)
    code = "000000" if offset is None else totp.hotp(RFC_KEY, STEP + offset)
    if offset is None:
        assert all(totp.hotp(RFC_KEY, STEP + s) != code for s in (-1, 0, 1))
    found = totp.matching_step(RFC_KEY, code, NOW)
    assert found == (None if offset is None else STEP + offset)
    assert len(calls) == 3


@pytest.mark.parametrize(
    "code",
    ["", "12345", "1234567", "00592a", " 05924", "０５９２４５", "005924\n"],
)
def test_only_six_ascii_digits_can_match(code):
    assert totp.matching_step(RFC_KEY, code, NOW) is None


def test_a_step_before_the_epoch_is_never_compared():
    assert totp.matching_step(RFC_KEY, totp.hotp(RFC_KEY, 0), 0) == 0
    with pytest.raises(ValueError):
        totp.hotp(RFC_KEY, -1)


# --- the secret and the link ----------------------------------------------------


def test_a_new_secret_is_twenty_random_bytes():
    first, second = totp.new_secret(), totp.new_secret()
    assert len(first) == len(second) == 20
    assert first != second


def test_the_secret_text_is_base32_without_padding():
    assert totp.secret_text(RFC_KEY) == "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def test_the_otpauth_link():
    uri = totp.otpauth_uri(RFC_KEY, "ana@example.com")
    assert uri.startswith("otpauth://totp/Controlvar:ana@example.com?")
    query = parse_qs(urlsplit(uri).query)
    assert query == {
        "secret": ["GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"],
        "issuer": ["Controlvar"],
        "algorithm": ["SHA1"],
        "digits": ["6"],
        "period": ["30"],
    }


def test_the_link_label_is_percent_encoded():
    uri = totp.otpauth_uri(RFC_KEY, "a b&c?@example.com")
    parts = urlsplit(uri)
    assert parts.netloc == "totp"
    assert parts.path == "/Controlvar:a%20b%26c%3F@example.com"
