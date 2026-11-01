"""A split URL configuration that can never split is refused (#393).

``backend/app/schemas/split_url_config.py`` holds three layers of rules, and
this file holds each of them without a database:

* per field, wherever a configuration is parsed: unique URL variant names, and
  each ``url`` an absolute http(s) URL of at most 2048 characters with an ASCII
  host and without user information, a fragment, a backslash, whitespace or
  control characters, stored exactly as sent;
* across fields, for a ``split_url`` experiment (``ExperimentCreate`` and
  ``split_url_config_problem``): the URL variants name the experiment's
  variants one to one, with the same allocations, and no other URL variant has
  the control URL's host and path;
* across experiments (``split_url_conflict``): the same entry, and the chain
  rule in either direction.

Every refusal is the exact fixed text at its exact ``loc``. The HTTP answers,
update and start are ``backend/tests/integration/api/test_split_url_config_rules_api.py``.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from pydantic import ValidationError

from backend.app.schemas.experiment import ExperimentCreate
from backend.app.schemas.split_url_config import (
    CHAIN,
    SAME_ENTRY,
    SPLIT_URL_CONFIG_ERROR,
    SplitUrlConfig,
    SplitUrlConfigProblem,
    entry,
    find_split_url_config_problem,
    normalize_host,
    normalize_path,
    split_url_config_problem,
    split_url_conflict,
    split_url_entries,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

VARIANTS = [
    {"name": "Control", "is_control": True, "traffic_allocation": 50},
    {"name": "Treatment", "is_control": False, "traffic_allocation": 50},
]


def _uv(name: str, url: str, allocation: float = 50) -> Dict[str, Any]:
    return {"name": name, "url": url, "traffic_allocation": allocation}


def _good() -> List[Dict[str, Any]]:
    return [
        _uv("Control", "https://shop.example/checkout"),
        _uv("Treatment", "https://shop.example/checkout-v2"),
    ]


def _create(url_variants, variants=VARIANTS, experiment_type="split_url"):
    return ExperimentCreate(
        name="split url rules",
        experiment_type=experiment_type,
        variants=variants,
        metrics=[{"name": "Purchase", "event_name": "purchase"}],
        split_url_config={"variants": url_variants},
    )


def _refusal(url_variants, variants=VARIANTS) -> Dict[str, Any]:
    """The one error a create with these URL variants earns."""
    with pytest.raises(ValidationError) as caught:
        _create(url_variants, variants)
    errors = caught.value.errors(include_url=False, include_input=False)
    assert len(errors) == 1, errors
    return {"type": errors[0]["type"], "loc": errors[0]["loc"], "msg": errors[0]["msg"]}


def _with_control_url(url: str) -> List[Dict[str, Any]]:
    variants = _good()
    variants[0]["url"] = url
    return variants


URL_AT_0 = ("split_url_config", "variants", 0, "url")

NOT_ABSOLUTE = (
    "The URL for variant 'Control' must be a full URL that starts with http:// "
    "or https:// and has a host name."
)
TOO_LONG = (
    "The URL for variant 'Control' is longer than 2048 characters. Use a shorter URL."
)
USER_INFO = "The URL for variant 'Control' must not contain a user name or password."
FORBIDDEN = (
    "The URL for variant 'Control' must not contain a # fragment, a backslash, a "
    "space or a control character."
)
NOT_ASCII = (
    "The host name in the URL for variant 'Control' must be ASCII, without a % "
    "sign. Write an internationalised name in its xn-- form."
)
DOT_SEGMENT = (
    "The path in the URL for variant 'Control' must not contain a . or .. "
    "segment, written as is or as %2e. A browser removes these segments, so "
    "write the path the browser requests."
)
ENCODED = (
    "The path in the URL for variant 'Control' must not contain a non-ASCII "
    "character, a double quote, <, >, ^, a backquote, {, | or }. Write each one "
    "percent-encoded (é as %C3%A9, | as %7C), as a browser sends it."
)

#: (id, the control URL, the text). Each is refused at the URL's own loc.
URL_REFUSED = [
    ("relative", "/checkout", NOT_ABSOLUTE),
    ("scheme-relative", "//shop.example/checkout", NOT_ABSOLUTE),
    ("javascript", "javascript:alert(1)", NOT_ABSOLUTE),
    ("ftp", "ftp://shop.example/checkout", NOT_ABSOLUTE),
    ("data", "data:text/html,hello", NOT_ABSOLUTE),
    ("no-host", "https:///checkout", NOT_ABSOLUTE),
    ("no-slashes", "https:shop.example/checkout", NOT_ABSOLUTE),
    ("empty", "", NOT_ABSOLUTE),
    ("port-not-a-number", "https://shop.example:port/checkout", NOT_ABSOLUTE),
    ("2049-characters", "https://shop.example/" + "a" * 2028, TOO_LONG),
    ("user-and-password", "https://user:secret@shop.example/checkout", USER_INFO),
    ("user-only", "https://user@shop.example/checkout", USER_INFO),
    (
        "backslash-before-at",
        "https://evil.example\\@shop.example/checkout-v2",
        FORBIDDEN,
    ),
    ("backslash", "https://shop.example\\checkout", FORBIDDEN),
    ("fragment", "https://shop.example/checkout#top", FORBIDDEN),
    ("space", "https://shop.example/check out", FORBIDDEN),
    ("tab", "https://shop.example/checkout\t", FORBIDDEN),
    ("newline", "https://shop.example/checkout\n", FORBIDDEN),
    ("leading-space", " https://shop.example/checkout", FORBIDDEN),
    ("nul", "https://shop.example/check\x00out", FORBIDDEN),
    ("delete", "https://shop.example/check\x7fout", FORBIDDEN),
    ("c1-control", "https://shop.example/check\x85out", FORBIDDEN),
    ("no-break-space", "https://shop.example/check out", FORBIDDEN),
    ("unicode-host", "https://bücher.example/checkout", NOT_ASCII),
    ("percent-in-host", "https://shop%2Eexample/checkout", NOT_ASCII),
    ("percent-encoded-host", "https://caf%C3%A9.example/checkout", NOT_ASCII),
    ("percent-escape-after-host", "https://shop.example%00/checkout", NOT_ASCII),
    # Measured on 2026-11-01 with Node 22.14.0's WHATWG URL and Chromium 153's
    # URL API (headless): both resolve each of these paths without the segment,
    # so the browser requests another path than the one written.
    ("dot-dot", "https://shop.example/x/../checkout", DOT_SEGMENT),
    ("dot", "https://shop.example/./checkout", DOT_SEGMENT),
    ("dot-dot-at-the-root", "https://shop.example/../checkout", DOT_SEGMENT),
    ("encoded-dot-dot", "https://shop.example/x/%2e%2e/checkout", DOT_SEGMENT),
    ("encoded-dot-dot-upper", "https://shop.example/x/%2E%2E/checkout", DOT_SEGMENT),
    ("dot-encoded-dot", "https://shop.example/x/.%2E/checkout", DOT_SEGMENT),
    ("encoded-dot-dot-mixed", "https://shop.example/x/%2e./checkout", DOT_SEGMENT),
    ("encoded-dot", "https://shop.example/%2e/checkout", DOT_SEGMENT),
    ("trailing-dot", "https://shop.example/checkout/.", DOT_SEGMENT),
    ("trailing-dot-dot", "https://shop.example/checkout/x/..", DOT_SEGMENT),
    ("trailing-encoded-dot", "https://shop.example/checkout/%2E?a=1", DOT_SEGMENT),
    ("only-dot-dot", "https://shop.example/..", DOT_SEGMENT),
    # Measured the same way: Node percent-encodes " < > ` { } and every
    # non-ASCII character in a path, and Chromium also ^ and |.
    ("non-ascii-path", "https://shop.example/café", ENCODED),
    ("double-quote", 'https://shop.example/a"b', ENCODED),
    ("less-than", "https://shop.example/a<b", ENCODED),
    ("greater-than", "https://shop.example/a>b", ENCODED),
    ("caret", "https://shop.example/a^b", ENCODED),
    ("backquote", "https://shop.example/a`b", ENCODED),
    ("open-brace", "https://shop.example/a{b", ENCODED),
    ("pipe", "https://shop.example/a|b", ENCODED),
    ("close-brace", "https://shop.example/a}b", ENCODED),
]


@pytest.mark.parametrize(
    "url, text", [r[1:] for r in URL_REFUSED], ids=[r[0] for r in URL_REFUSED]
)
def test_a_url_no_visitor_could_be_sent_to_is_refused_at_its_loc(url, text):
    assert _refusal(_with_control_url(url)) == {
        "type": SPLIT_URL_CONFIG_ERROR,
        "loc": URL_AT_0,
        "msg": text,
    }


#: Accepted, and stored exactly as sent.
URL_ACCEPTED = [
    "https://shop.example/checkout",
    "http://shop.example/checkout",
    "HTTPS://Shop.Example:8443/Checkout/?utm_source=x&b=%20",
    "https://xn--bcher-kva.example/checkout",
    "https://shop.example",
    "https://shop.example/" + "a" * 2027,  # exactly 2048 characters
    "https://shop.example/a@b",  # an @ in the path is not user information
    "https://[2001:db8::1]:8443/checkout",
    # Measured on 2026-11-01 with Node 22.14.0's WHATWG URL and Chromium 153's
    # URL API: neither rewrites these paths, and a dot in the query is not in
    # the path.
    "https://shop.example/.well-known/x",
    "https://shop.example/a..b",
    "https://shop.example/...",
    "https://shop.example/%2e%2e%2e",
    "https://shop.example/x/..%2Fcheckout",
    "https://shop.example/landing?next=/../checkout",
    "https://shop.example/caf%C3%A9",
    "https://shop.example/a[b]",
    "https://shop.example/50%off",
    "https://shop.example/a'b",
]


@pytest.mark.parametrize("url", URL_ACCEPTED)
def test_a_usable_url_is_accepted_and_stored_as_sent(url):
    created = _create(_with_control_url(url))
    assert created.split_url_config.variants[0].url == url
    assert len(url) <= 2048


def test_url_variant_names_are_unique():
    assert _refusal(
        [
            _uv("Control", "https://shop.example/a"),
            _uv("Control", "https://shop.example/b"),
        ]
    ) == {
        "type": SPLIT_URL_CONFIG_ERROR,
        "loc": ("split_url_config", "variants"),
        "msg": "URL variant names must be unique, but 'Control' is used more than once.",
    }


def test_the_per_field_rules_hold_on_an_experiment_of_another_type():
    """A configuration on an experiment that is not split_url is checked only
    by the per-field rules: names need not match, and a destination may equal
    the entry."""
    created = _create(
        [_uv("A", "https://shop.example/a"), _uv("B", "https://shop.example/a")],
        experiment_type="a_b",
    )
    assert [v.name for v in created.split_url_config.variants] == ["A", "B"]
    with pytest.raises(ValidationError) as caught:
        _create(
            [_uv("A", "/a"), _uv("B", "https://shop.example/b")], experiment_type="a_b"
        )
    assert caught.value.errors()[0]["msg"] == NOT_ABSOLUTE.replace("Control", "A")


# ---------------------------------------------------------------------------
# Across fields, on create
# ---------------------------------------------------------------------------


THREE = VARIANTS + [{"name": "Third", "is_control": False, "traffic_allocation": 0}]
TWO_CONTROLS_NAMED_ALIKE = [
    VARIANTS[0],
    {"name": "Control", "is_control": False, "traffic_allocation": 50},
]

#: (id, URL variants, experiment variants, loc, text)
CROSS_REFUSED = [
    (
        "url-variant-not-an-experiment-variant",
        [_uv("Control", "https://shop.example/a"), _uv("B", "https://shop.example/b")],
        VARIANTS,
        ("split_url_config", "variants", 1, "name"),
        "URL variant 'B' is not one of this experiment's variants: Control, Treatment.",
    ),
    (
        "experiment-variant-without-a-url",
        [
            _uv("Control", "https://shop.example/a"),
            _uv("Treatment", "https://shop.example/b"),
        ],
        THREE,
        ("split_url_config", "variants"),
        "Experiment variant 'Third' has no URL variant. Add a URL variant named 'Third'.",
    ),
    (
        "experiment-variant-names-repeat",
        [
            _uv("Control", "https://shop.example/a"),
            _uv("Treatment", "https://shop.example/b"),
        ],
        TWO_CONTROLS_NAMED_ALIKE,
        ("split_url_config",),
        "This experiment's variant names must be unique, but 'Control' is used "
        "more than once.",
    ),
    (
        "allocation-differs",
        [
            _uv("Control", "https://shop.example/a", 40),
            _uv("Treatment", "https://shop.example/b", 60),
        ],
        VARIANTS,
        ("split_url_config", "variants", 0, "traffic_allocation"),
        "URL variant 'Control' has a different traffic_allocation from the "
        "experiment variant of the same name. Make them equal; the experiment's "
        "variants decide the split.",
    ),
    (
        "allocation-differs-by-more-than-0.01",
        [
            _uv("Control", "https://shop.example/a", 49.98),
            _uv("Treatment", "https://shop.example/b", 50.02),
        ],
        VARIANTS,
        ("split_url_config", "variants", 0, "traffic_allocation"),
        "URL variant 'Control' has a different traffic_allocation from the "
        "experiment variant of the same name. Make them equal; the experiment's "
        "variants decide the split.",
    ),
    (
        "destination-is-the-entry",
        [
            _uv("Control", "https://shop.example/checkout/"),
            _uv("Treatment", "https://SHOP.example:443/checkout?v=2"),
        ],
        VARIANTS,
        ("split_url_config", "variants", 1, "url"),
        "The URL for variant 'Treatment' has the same host and path as the control "
        "URL, so a visitor sent there would be sent again. Give it a different path "
        "or host; a different query string is not enough.",
    ),
]


@pytest.mark.parametrize(
    "url_variants, variants, loc, text",
    [r[1:] for r in CROSS_REFUSED],
    ids=[r[0] for r in CROSS_REFUSED],
)
def test_a_config_that_does_not_match_its_variants_is_refused_at_its_loc(
    url_variants, variants, loc, text
):
    assert _refusal(url_variants, variants) == {
        "type": SPLIT_URL_CONFIG_ERROR,
        "loc": loc,
        "msg": text,
    }


def test_the_url_variants_may_be_listed_in_any_order():
    created = _create(list(reversed(_good())))
    assert [v.name for v in created.split_url_config.variants] == [
        "Treatment",
        "Control",
    ]


def test_an_allocation_within_0_01_matches():
    created = _create(
        [
            _uv("Control", "https://shop.example/a", 50.01),
            _uv("Treatment", "https://shop.example/b", 49.99),
        ]
    )
    assert created.split_url_config is not None


def test_a_destination_with_the_entrys_path_on_another_host_is_accepted():
    created = _create(
        [
            _uv("Control", "https://shop.example/checkout"),
            _uv("Treatment", "https://landing.example/checkout"),
        ]
    )
    assert created.split_url_config is not None


def test_the_cross_field_rules_run_after_the_name_rules():
    """A config that names an unknown variant AND has a wrong allocation
    reports the name first."""
    assert _refusal(
        [
            _uv("Control", "https://shop.example/a", 30),
            _uv("B", "https://shop.example/b", 70),
        ]
    )["loc"] == ("split_url_config", "variants", 1, "name")


def test_create_is_idempotent_as_the_route_needs():
    """The create route builds ExperimentCreate again from the first one's
    dump; the second build accepts what the first did."""
    first = _create(_good())
    again = ExperimentCreate(**first.model_dump())
    assert again.split_url_config == first.split_url_config


def test_a_split_url_experiment_may_still_be_created_without_a_config():
    created = ExperimentCreate(
        name="no config",
        experiment_type="split_url",
        variants=VARIANTS,
        metrics=[{"name": "Purchase", "event_name": "purchase"}],
    )
    assert created.split_url_config is None


# ---------------------------------------------------------------------------
# split_url_config_problem, on stored configurations and rows
# ---------------------------------------------------------------------------


class _Row:
    """An experiment variant as a database row presents it."""

    def __init__(self, name, is_control, traffic_allocation):
        self.name = name
        self.is_control = is_control
        self.traffic_allocation = traffic_allocation


ROWS = [_Row("Control", True, 50), _Row("Treatment", False, 50)]


def test_a_stored_config_is_held_to_the_per_field_rules_first():
    stored = {"variants": _with_control_url("/checkout")}
    assert find_split_url_config_problem(stored, ROWS) == SplitUrlConfigProblem(
        ("variants", 0, "url"), NOT_ABSOLUTE
    )


def test_a_stored_config_failing_an_older_rule_names_where():
    stored = {"variants": [_uv("Control", "https://shop.example/a", 95)]}
    assert split_url_config_problem(stored, ROWS) == (
        "variants: At least 2 URL variants required."
    )


def test_a_stored_config_that_is_not_an_object_is_a_problem_not_an_error():
    assert split_url_config_problem(["not", "a", "config"], ROWS) is not None


def test_the_answer_for_rows_and_for_a_parsed_config_is_the_same():
    stored = {
        "variants": [
            _uv("Control", "https://shop.example/a"),
            _uv("B", "https://shop.example/b"),
        ]
    }
    expected = (
        "URL variant 'B' is not one of this experiment's variants: Control, Treatment."
    )
    assert split_url_config_problem(stored, ROWS) == expected
    assert split_url_config_problem(SplitUrlConfig(**stored), VARIANTS) == expected


def test_a_valid_config_has_no_problem():
    assert split_url_config_problem({"variants": _good()}, ROWS) is None


# ---------------------------------------------------------------------------
# Entries, and the rules across experiments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host, expected",
    [
        ("Shop.Example", "shop.example"),
        ("shop.example:8443", "shop.example"),
        ("SHOP.EXAMPLE:443", "shop.example"),
        ("[2001:DB8::1]:8443", "[2001:db8::1]"),
        ("[2001:db8::1]", "[2001:db8::1]"),
    ],
)
def test_normalize_host(host, expected):
    assert normalize_host(host) == expected


@pytest.mark.parametrize(
    "path, expected",
    [
        ("", "/"),
        ("/", "/"),
        ("/checkout/", "/checkout"),
        ("/checkout//", "/checkout/"),
        ("/Checkout", "/Checkout"),
        ("/caf%C3%A9", "/caf%C3%A9"),
    ],
)
def test_normalize_path(path, expected):
    assert normalize_path(path) == expected


def test_an_entry_is_the_host_and_path_without_the_query():
    assert entry("https://Shop.Example:443/checkout/?utm_source=x") == (
        "shop.example",
        "/checkout",
    )


def _entries(control: str, *others: str):
    url_variants = [_uv("Control", control, 100)]
    rows = [_Row("Control", True, 100)]
    for i, other in enumerate(others):
        url_variants.append(_uv(f"V{i}", other, 0))
        rows.append(_Row(f"V{i}", False, 0))
    found = split_url_entries({"variants": url_variants}, rows)
    assert found is not None
    return found


def test_two_experiments_with_the_same_entry_conflict():
    mine = _entries("https://shop.example/a", "https://shop.example/b")
    theirs = _entries("https://SHOP.example/a/", "https://shop.example/c")
    assert split_url_conflict(mine, theirs) == SAME_ENTRY


def test_the_same_path_on_another_host_does_not_conflict():
    mine = _entries("https://shop.example/a", "https://shop.example/b")
    theirs = _entries("https://other.example/a", "https://other.example/b")
    assert split_url_conflict(mine, theirs) is None


@pytest.mark.parametrize("which", ["mine-into-theirs", "theirs-into-mine"])
def test_a_chain_either_way_conflicts(which):
    a_to_b = _entries("https://shop.example/a", "https://shop.example/b")
    b_to_c = _entries("https://shop.example/b", "https://shop.example/c")
    if which == "mine-into-theirs":
        assert split_url_conflict(a_to_b, b_to_c) == CHAIN
    else:
        assert split_url_conflict(b_to_c, a_to_b) == CHAIN


def test_a_two_way_crossing_is_a_chain():
    a_to_b = _entries("https://shop.example/a", "https://shop.example/b")
    b_to_a = _entries("https://shop.example/b", "https://shop.example/a")
    assert split_url_conflict(a_to_b, b_to_a) == CHAIN


def test_two_experiments_sharing_only_a_destination_do_not_conflict():
    a_to_c = _entries("https://shop.example/a", "https://shop.example/c")
    b_to_c = _entries("https://shop.example/b", "https://shop.example/c")
    assert split_url_conflict(a_to_c, b_to_c) is None


def test_an_invalid_stored_config_has_no_entries():
    assert split_url_entries({"variants": _with_control_url("/a")}, ROWS) is None
    assert split_url_entries(None, ROWS) is None
    assert split_url_entries({}, ROWS) is None
