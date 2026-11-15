"""The dedup key's encoding, pinned byte for byte (#1543, SPEC v1.2 U4).

``event_dedup.dedup_key`` is the one function that computes the key a stored
``event_id`` is recognised by. Its value is stored in ``event_dedup_keys``, so
a change to it silently stops recognising every event already stored: these
vectors fail first. Each vector pins the bytes hashed (each part as
``<UTF-8 byte length>:<bytes>``, in the order experiment, flag, event name,
``event_id``) and the resulting digest.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

from backend.app.services.event_dedup import DEDUP_KEY_LENGTH, dedup_key

pytestmark = [pytest.mark.unit]

EXPERIMENT = "3f2b8c1e-5d4a-4b6f-9e7d-0a1b2c3d4e5f"
FLAG = "9c8d7e6f-5a4b-4c3d-8e2f-1a0b9c8d7e6f"

#: (arguments, the bytes hashed, the key).
VECTORS = [
    pytest.param(
        (None, None, "purchase", "order-1001"),
        b"0:0:8:purchase10:order-1001",
        "9f9e77c14972cd70e3e493c3a8b0c9aa865a817b545dfbf037e29a13f2160257",
        id="both-targets-empty",
    ),
    pytest.param(
        (uuid.UUID(EXPERIMENT), None, "purchase\U0001f600", "order-1001"),
        b"36:" + EXPERIMENT.encode() + b"0:12:purchase\xf0\x9f\x98\x8010:order-1001",
        "e2cbe74aa02d46ed1ebfb86c178828d03dc07a3e75912845ed763070700f32a4",
        id="four-byte-character-in-the-name",
    ),
    pytest.param(
        (uuid.UUID(EXPERIMENT), uuid.UUID(FLAG), "checkout_completed", "order-1001"),
        b"36:"
        + EXPERIMENT.encode()
        + b"36:"
        + FLAG.encode()
        + b"18:checkout_completed10:order-1001",
        "8d15ab373075d16a70f839ea0e879b2e68a22ffbe24a6134404399e047f16650",
        id="one-id-first-name",
    ),
    pytest.param(
        (uuid.UUID(EXPERIMENT), uuid.UUID(FLAG), "purchase", "order-1001"),
        b"36:"
        + EXPERIMENT.encode()
        + b"36:"
        + FLAG.encode()
        + b"8:purchase10:order-1001",
        "4a01e83122ab32b02a984ff749856e2114592c38f48d4c2382959ea8f9ce445c",
        id="one-id-second-name",
    ),
]


@pytest.mark.regression
@pytest.mark.parametrize("args, preimage, key", VECTORS)
def test_the_key_is_the_sha256_of_the_length_prefixed_parts(args, preimage, key):
    assert hashlib.sha256(preimage).hexdigest() == key
    assert dedup_key(*args) == key
    assert len(key) == DEDUP_KEY_LENGTH


def test_two_names_with_one_id_are_two_keys():
    """The event name is in the key (PE-GT-3): an order id that keys
    ``checkout_completed`` and ``purchase`` keys two events."""
    exp, flag = uuid.UUID(EXPERIMENT), uuid.UUID(FLAG)
    assert dedup_key(exp, flag, "checkout_completed", "order-1001") != dedup_key(
        exp, flag, "purchase", "order-1001"
    )


def test_an_id_is_read_as_its_canonical_text_whatever_its_form():
    """A UUID, its lowercase text and its uppercase text are one experiment."""
    assert (
        dedup_key(uuid.UUID(EXPERIMENT), None, "purchase", "e")
        == dedup_key(EXPERIMENT, None, "purchase", "e")
        == dedup_key(EXPERIMENT.upper(), None, "purchase", "e")
    )


@pytest.mark.parametrize(
    "first, second",
    [
        (("ab", "c"), ("a", "bc")),
        (("a:b", ""), ("a", ":b")),
        (("1:a", "b"), ("1", ":ab")),
    ],
    ids=["moved-character", "colon", "length-lookalike"],
)
def test_parts_that_concatenate_alike_are_different_keys(first, second):
    """Length prefixes, not a separator: no two different parts encode alike."""
    assert dedup_key(None, None, *first) != dedup_key(None, None, *second)


def test_the_experiment_and_the_flag_are_not_interchangeable():
    exp = uuid.UUID(EXPERIMENT)
    assert dedup_key(exp, None, "purchase", "e") != dedup_key(
        None, exp, "purchase", "e"
    )


def test_the_event_id_is_compared_exactly():
    """Case-sensitive and not trimmed."""
    keys = {
        dedup_key(None, None, "purchase", e) for e in ("Order-1", "order-1", "order-1 ")
    }
    assert len(keys) == 3


def test_the_longest_parts_still_give_a_64_character_key():
    """255 four-byte characters in the name and the id: 64 hex characters."""
    key = dedup_key(
        uuid.UUID(EXPERIMENT), uuid.UUID(FLAG), "\U0001f600" * 255, "\U0001f600" * 255
    )
    assert len(key) == DEDUP_KEY_LENGTH
    assert key == key.lower()
    int(key, 16)
