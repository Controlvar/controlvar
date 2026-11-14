"""The website details ``POST /api/v1/tracking/visual`` keeps (#1545, S9).

Rows of PLAN-v3.1's VERIFICATION held here, each with the defect that fails it:

* V46  the context fits: every website detail at its longest, in its widest
       characters, plus 4096 bytes of the site's own ``data-attributes``,
       stays within the 8192-byte context limit. Planted defect: a campaign
       cap of 2048 (the worst case is then over 10 kB).
* V57  (server) no raw referrer or query is kept: a referrer URL, a landing
       path with a query, and a click id are dropped; three campaign keys
       stay.
* V108 (server) a landing path of 256 or 300 characters is dropped, never
       cut to 255; 255 is kept.

The cleaning never refuses (a dropped detail is not an error), never touches
a key that does not start with ``web_``, and does not change its input.
"""

from __future__ import annotations

import pytest

from backend.app.core.context_limits import MAX_CONTEXT_BYTES, compact_size
from backend.app.schemas import visual_tracking as vt

pytestmark = pytest.mark.unit

#: The widest character each pattern takes, in bytes of compact UTF-8 JSON:
#: the landing path takes any character but the few it refuses, so a 4-byte
#: one is its worst case; every other pattern is ASCII.
WIDEST_LANDING_CHARACTER = "\U0001f600"

#: Far past any limit a detail may have: the probe below looks this far.
PROBE_LIMIT = 4096


def _longest_kept(key, make):
    """The longest value of *key* the cleaning keeps, ``make(n)`` being the
    value of length n: read from the rule itself, not from a constant."""
    kept = [n for n in range(1, PROBE_LIMIT + 1) if vt.web_detail_is_kept(key, make(n))]
    assert kept, key
    assert kept == list(range(kept[0], kept[-1] + 1)), f"{key}: kept lengths have a gap"
    return make(kept[-1])


def _worst_case_details():
    return {
        "web_device": max(vt.WEB_DEVICES, key=len),
        "web_browser": max(vt.WEB_BROWSERS, key=len),
        "web_language": _longest_kept("web_language", lambda n: "a" * n),
        "web_visitor": max(vt.WEB_VISITORS, key=len),
        "web_referrer": _longest_kept("web_referrer", lambda n: "a" * n),
        **{key: _longest_kept(key, lambda n: "a" * n) for key in vt.UTM_KEYS},
        "web_landing_path": _longest_kept(
            "web_landing_path", lambda n: "/" + WIDEST_LANDING_CHARACTER * (n - 1)
        ),
    }


@pytest.mark.parametrize(
    "key,limit",
    [
        ("web_referrer", vt.MAX_REFERRER_LENGTH),
        ("web_utm_source", vt.MAX_UTM_LENGTH),
        ("web_utm_medium", vt.MAX_UTM_LENGTH),
        ("web_utm_campaign", vt.MAX_UTM_LENGTH),
        ("web_landing_path", vt.MAX_LANDING_PATH_LENGTH),
    ],
)
def test_each_limit_constant_is_the_rules_own(key, limit):
    """The documented limits are where the rules stop: N kept, N + 1 dropped."""
    make = (
        (lambda n: "/" + "a" * (n - 1))
        if key == "web_landing_path"
        else (lambda n: "a" * n)
    )
    assert vt.web_detail_is_kept(key, make(limit)), key
    assert not vt.web_detail_is_kept(key, make(limit + 1)), key
    assert (vt.MAX_REFERRER_LENGTH, vt.MAX_UTM_LENGTH, vt.MAX_LANDING_PATH_LENGTH) == (
        253,
        100,
        255,
    )


def _attributes_of_bytes(size):
    """The site's own attributes: one key whose compact JSON object is
    exactly *size* bytes, as ``data-attributes`` at its 4 KB cap."""
    overhead = compact_size({"site": ""})
    return {"site": "x" * (size - overhead)}


@pytest.mark.regression
def test_the_worst_case_context_fits_the_limit():
    """V46."""
    details = _worst_case_details()
    cleaned, dropped = vt.clean_web_details(details)
    assert dropped == [], "the worst case must be details the server keeps"
    assert set(cleaned) == set(vt.WEB_DETAIL_RULES)
    attributes = _attributes_of_bytes(4096)
    assert compact_size(attributes) == 4096
    worst = {**attributes, **cleaned}
    size = compact_size(worst)
    assert size <= MAX_CONTEXT_BYTES, f"worst case {size:,} > {MAX_CONTEXT_BYTES}"


def test_a_value_in_its_form_is_kept_and_any_other_web_key_is_dropped():
    sent = {
        "web_device": "phone",
        "web_browser": "safari",
        "web_language": "fr",
        "web_visitor": "new",
        "web_referrer": "news.example.org",
        "web_utm_source": "newsletter",
        "web_utm_medium": "email",
        "web_utm_campaign": "spring-sale",
        "web_landing_path": "/shoes/red",
        "web_country": "FR",
        "web_gclid": "XYZ",
        "web_utm_term": "boots",
    }
    cleaned, dropped = vt.clean_web_details(sent)
    assert dropped == ["web_country", "web_gclid", "web_utm_term"]
    assert cleaned == {
        key: value
        for key, value in sent.items()
        if key not in ("web_country", "web_gclid", "web_utm_term")
    }


@pytest.mark.regression
def test_no_raw_referrer_or_query_is_kept():
    """V57 (server)."""
    sent = {
        "web_referrer": "https://www.google.com/search?q=cv-secret-7731",
        "web_landing_path": "/a/b/page?email=a%40b.c&gclid=XYZ",
        "web_gclid": "XYZ",
        "web_utm_source": "news",
        "web_utm_medium": "email",
        "web_utm_campaign": "a@b.c",
    }
    cleaned, _ = vt.clean_web_details(sent)
    assert cleaned == {"web_utm_source": "news", "web_utm_medium": "email"}
    assert "cv-secret-7731" not in repr(cleaned)
    assert "gclid" not in repr(cleaned)


@pytest.mark.regression
def test_a_long_landing_path_is_dropped_never_cut():
    """V108 (server)."""
    for length, kept in ((255, True), (256, False), (300, False)):
        path = "/" + "a" * (length - 1)
        cleaned, dropped = vt.clean_web_details({"web_landing_path": path})
        if kept:
            assert cleaned == {"web_landing_path": path}, length
        else:
            assert cleaned == {} and dropped == ["web_landing_path"], length


def test_other_keys_pass_through_whatever_they_hold():
    sent = {
        "plan": "pro",
        "email": None,
        "nested": {"web_device": "not-a-device"},
        "WEB_DEVICE": "anything",
        "count": 3,
    }
    cleaned, dropped = vt.clean_web_details(sent)
    assert cleaned == sent and dropped == []


def test_the_input_is_not_changed_and_none_stays_none():
    sent = {"web_device": "mobile", "plan": "pro"}
    before = dict(sent)
    cleaned, _ = vt.clean_web_details(sent)
    assert sent == before and cleaned == {"plan": "pro"}
    assert vt.clean_web_details(None) == (None, [])
    assert vt.clean_web_details({}) == ({}, [])


def test_a_value_must_be_a_string_in_its_form():
    for value in (None, "", 1, True, ["phone"], {"v": "phone"}):
        assert not vt.web_detail_is_kept("web_device", value), value
    assert vt.web_detail_is_kept("web_device", "phone")
    # fullmatch: a trailing new line is not accepted where $ would accept it.
    assert not vt.web_detail_is_kept("web_language", "en\n")
    assert not vt.web_detail_is_kept("web_referrer", "example.com\n")
    assert not vt.web_detail_is_kept("web_utm_source", "news\n")


def test_a_landing_path_refuses_what_the_docs_list():
    for path in (
        "/a?b",
        "/a#b",
        "/a@b",
        "/a%40b",
        "/a b",
        "/a b",
        "/a b",
        "/a\x01b",
        "/a\ud800b",
        "a/b",
        "",
    ):
        assert not vt.landing_path_is_kept(path), repr(path)
    for path in ("/", "/a/b", "/%E2%82%AC", "/café", "/a%20b", "/a;b=c"):
        assert vt.landing_path_is_kept(path), repr(path)
