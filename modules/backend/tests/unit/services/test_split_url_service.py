"""
Unit tests for split_url_service.py's pure helpers (#393).

The MD5 helpers (``hash_user``, ``get_url_variant``, ``get_cookie_name``)
are gone: a visitor's variant is ``/tracking/assign``'s, so nothing here
chooses one. What is left is pure:

* ``merge_query``: the visitor's query after the variant's own parameters;
* ``location``: the query goes only to a variant on the control URL's host,
  hosts compared without case or port;
* ``is_bot``: a fixed list of case-insensitive substrings, read in the first
  512 characters of a User-Agent.

The same rules against the API, as exact strings, are in
``modules/backend/tests/integration/split_url/`` (``vectors.json``).
"""

import pytest

from modules.backend.app.services.split_url_service import (
    BOT_INSPECTED_CHARACTERS,
    BOT_SUBSTRINGS,
    is_bot,
    location,
    merge_query,
    same_host,
)

pytestmark = [pytest.mark.unit]


@pytest.mark.parametrize(
    "url, query, expected",
    [
        ("https://e.com/v2", "utm_source=x", "https://e.com/v2?utm_source=x"),
        ("https://e.com/v2?a=1", "utm_source=x", "https://e.com/v2?a=1&utm_source=x"),
        ("https://e.com/v2?", "utm_source=x", "https://e.com/v2?utm_source=x"),
        ("https://e.com/v2", "", "https://e.com/v2"),
        ("https://e.com/v2", None, "https://e.com/v2"),
        ("https://e.com/v2", "q=caf%C3%A9&b", "https://e.com/v2?q=caf%C3%A9&b"),
    ],
    ids=["added", "after-its-own", "bare-question-mark", "empty", "none", "as-sent"],
)
def test_merge_query(url, query, expected):
    assert merge_query(url, query) == expected


@pytest.mark.parametrize(
    "variant, expected",
    [
        ("https://e.com/v2", "https://e.com/v2?utm_source=x"),
        ("https://E.COM/v2", "https://E.COM/v2?utm_source=x"),
        ("https://e.com:8443/v2", "https://e.com:8443/v2?utm_source=x"),
        ("http://e.com/v2", "http://e.com/v2?utm_source=x"),
        ("https://other.example/landing", "https://other.example/landing"),
        ("https://other.example/landing?a=1", "https://other.example/landing?a=1"),
        ("https://shop.e.com/v2", "https://shop.e.com/v2"),
    ],
    ids=[
        "same-host",
        "same-host-in-capitals",
        "same-host-other-port",
        "same-host-other-scheme",
        "another-host",
        "another-host-own-query",
        "a-subdomain-is-another-host",
    ],
)
def test_the_query_goes_only_to_the_control_urls_host(variant, expected):
    assert location(variant, "https://e.com/checkout", "utm_source=x") == expected


def test_same_host_ignores_case_and_port():
    assert same_host("https://E.com:443/a", "http://e.com/b")
    assert same_host("https://[2001:db8::1]:8443/a", "https://[2001:DB8::1]/b")
    assert not same_host("https://e.com/a", "https://www.e.com/a")


@pytest.mark.parametrize("substring", BOT_SUBSTRINGS)
def test_every_listed_substring_is_a_bot_in_any_case(substring):
    assert is_bot(f"Mozilla/5.0 (compatible; {substring}/1.0)")
    assert is_bot(substring.upper())


@pytest.mark.parametrize(
    "user_agent",
    [
        None,
        "",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 Safari/605.1.15",
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) Chrome/126.0 Mobile Safari/537.36",
    ],
    ids=["none", "empty", "safari", "chrome-android"],
)
def test_a_browser_is_not_a_bot(user_agent):
    assert not is_bot(user_agent)


def test_only_the_first_512_characters_are_read():
    assert BOT_INSPECTED_CHARACTERS == 512
    assert is_bot("x" * 503 + "Googlebot")
    assert not is_bot("x" * 512 + "Googlebot")
