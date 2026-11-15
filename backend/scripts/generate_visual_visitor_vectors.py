"""
Generate ``tests/sdk-contract/visual-visitor-vectors.json``: the server's
answers about the website details a visual experiment's browser script sends
(#1545, S9), which the script is held to.

Three corpora, written by hand below; every verdict in the file comes from the
server:

* ``details``: one ``web_*`` key and value each, and whether
  ``POST /api/v1/tracking/visual`` keeps it
  (``backend.app.schemas.visual_tracking.web_detail_is_kept``).
* ``utm_queries``: a landing page's query, the value ``URLSearchParams.get``
  reads from it (computed here with ``urllib.parse``, which decodes ``+`` and
  ``%XX`` as the WHATWG URL standard does), and whether that value is kept.
  The script reads the value in the browser and must get the same one: that
  is what the script's half of this file measures.
* ``audiences``: a context as the script sends it and a rule as the
  dashboard's builder stores it, and whether the server assigns the visitor:
  the context is cleaned as the route cleans it, then matched by experiment
  targeting itself (``AssignmentService``), as ``/tracking/assign`` matches
  it. Every website detail, absent, against every negative operator, is one
  of them: a missing key fails closed.

``backend/tests/unit/schemas/test_visual_visitor_vectors.py`` holds each
corpus entry to the verdict it was written for and runs ``--check``, so a
change to the cleaning or to targeting that moves an answer fails until the
file is regenerated, and then the script's contract test fails until it
agrees.

Usage (from the repository root, venv active):

    python -m backend.scripts.generate_visual_visitor_vectors          # rewrite
    python -m backend.scripts.generate_visual_visitor_vectors --check  # exit 1 on drift
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs

from backend.app.schemas.visual_tracking import (
    MAX_LANDING_PATH_LENGTH,
    MAX_REFERRER_LENGTH,
    MAX_UTM_LENGTH,
    UTM_KEYS,
    WEB_BROWSERS,
    WEB_DETAIL_RULES,
    WEB_DEVICES,
    WEB_VISITORS,
    clean_web_details,
    web_detail_is_kept,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
VECTORS_PATH = REPO_ROOT / "tests" / "sdk-contract" / "visual-visitor-vectors.json"

#: The operators that match when a value is NOT something. With the key
#: absent, each must not match (a missing attribute fails closed).
NEGATIVE_OPERATORS = ("not_equals", "not_contains", "not_in")

_FULL_WIDTH = "ｓｐｒｉｎｇ"  # "spring" in full-width letters

#: (id, key, value, kept): what the route keeps of one website detail.
DETAILS: List[Tuple[str, str, Any, bool]] = [
    *[(f"device-{v}", "web_device", v, True) for v in WEB_DEVICES],
    ("device-mobile", "web_device", "mobile", False),
    ("device-capital", "web_device", "Phone", False),
    ("device-null", "web_device", None, False),
    ("device-empty", "web_device", "", False),
    ("device-number", "web_device", 1, False),
    *[(f"browser-{v}", "web_browser", v, True) for v in WEB_BROWSERS],
    ("browser-unknown", "web_browser", "brave", False),
    ("language-2", "web_language", "en", True),
    ("language-3", "web_language", "fil", True),
    ("language-capital", "web_language", "EN", False),
    ("language-region", "web_language", "en-US", False),
    ("language-1", "web_language", "e", False),
    ("language-4", "web_language", "engl", False),
    ("language-newline", "web_language", "en\n", False),
    *[(f"visitor-{v}", "web_visitor", v, True) for v in WEB_VISITORS],
    ("visitor-unknown", "web_visitor", "first", False),
    ("referrer-host", "web_referrer", "www.google.com", True),
    ("referrer-max", "web_referrer", "a" * MAX_REFERRER_LENGTH, True),
    ("referrer-over", "web_referrer", "a" * (MAX_REFERRER_LENGTH + 1), False),
    (
        "referrer-url",
        "web_referrer",
        "https://www.google.com/search?q=cv-secret-7731",
        False,
    ),
    ("referrer-path", "web_referrer", "google.com/search", False),
    ("referrer-capital", "web_referrer", "WWW.GOOGLE.COM", False),
    ("referrer-empty", "web_referrer", "", False),
    ("utm-plain", "web_utm_source", "spring-sale", True),
    ("utm-unreserved", "web_utm_medium", "a.b_c~d-e", True),
    ("utm-100", "web_utm_campaign", "a" * MAX_UTM_LENGTH, True),
    ("utm-101", "web_utm_campaign", "a" * (MAX_UTM_LENGTH + 1), False),
    ("utm-email", "web_utm_source", "a@b.c", False),
    ("utm-encoded-at", "web_utm_source", "a%40b.c", False),
    ("utm-space", "web_utm_campaign", "spring sale", False),
    ("utm-plus", "web_utm_campaign", "a+b", False),
    ("utm-full-width", "web_utm_campaign", _FULL_WIDTH, False),
    ("utm-empty", "web_utm_source", "", False),
    ("landing-root", "web_landing_path", "/", True),
    ("landing-path", "web_landing_path", "/a/b/page", True),
    (
        "landing-255",
        "web_landing_path",
        "/" + "a" * (MAX_LANDING_PATH_LENGTH - 1),
        True,
    ),
    ("landing-256", "web_landing_path", "/" + "a" * MAX_LANDING_PATH_LENGTH, False),
    ("landing-300", "web_landing_path", "/" + "a" * 299, False),
    ("landing-query", "web_landing_path", "/a?email=a%40b.c", False),
    ("landing-fragment", "web_landing_path", "/a#x", False),
    ("landing-at", "web_landing_path", "/u/a@b.c", False),
    ("landing-encoded-at", "web_landing_path", "/u/a%40b.c", False),
    ("landing-space", "web_landing_path", "/a b", False),
    ("landing-tab", "web_landing_path", "/a\tb", False),
    ("landing-relative", "web_landing_path", "a/b", False),
    ("landing-empty", "web_landing_path", "", False),
    ("country-from-page", "web_country", "FR", False),
    ("unknown-gclid", "web_gclid", "XYZ", False),
    ("unknown-utm-term", "web_utm_term", "x", False),
    ("unknown-utm-content", "web_utm_content", "x", False),
]

#: (id, query, param, kept): a landing page's query, the campaign parameter
#: read from it, and whether the value read is kept.
UTM_QUERIES: List[Tuple[str, str, str, bool]] = [
    ("q-plain", "utm_source=spring-sale", "utm_source", True),
    ("q-100", "utm_campaign=" + "a" * MAX_UTM_LENGTH, "utm_campaign", True),
    ("q-101", "utm_campaign=" + "a" * (MAX_UTM_LENGTH + 1), "utm_campaign", False),
    ("q-email", "utm_source=a@b.c", "utm_source", False),
    ("q-encoded-at", "utm_source=a%40b.c", "utm_source", False),
    ("q-space", "utm_campaign=spring%20sale", "utm_campaign", False),
    ("q-plus", "utm_campaign=spring+sale", "utm_campaign", False),
    ("q-encoded-plus", "utm_campaign=a%2Bb", "utm_campaign", False),
    (
        "q-full-width",
        "utm_campaign=%EF%BD%93%EF%BD%90%EF%BD%92%EF%BD%89%EF%BD%8E%EF%BD%87",
        "utm_campaign",
        False,
    ),
    ("q-other-params", "gclid=XYZ&utm_medium=email&email=a%40b.c", "utm_medium", True),
]


def _rule(attribute: str, operator: str, value: Any) -> Dict[str, Any]:
    """One condition, as the dashboard's builder stores it."""
    return {
        "logical_operator": "AND",
        "groups": [
            {
                "logical_operator": "AND",
                "conditions": [
                    {"attribute": attribute, "operator": operator, "value": value}
                ],
            }
        ],
    }


def _negative_value(key: str, operator: str) -> Any:
    return ["x"] if operator == "not_in" else "x"


#: (id, context, rule, matches): a context as the script sends it, a rule as
#: the builder stores it, and whether the server assigns the visitor.
AUDIENCES: List[Tuple[str, Dict[str, Any], Dict[str, Any], bool]] = [
    ("aud-01", {"web_device": "phone"}, _rule("web_device", "equals", "phone"), True),
    (
        "aud-02",
        {"web_device": "desktop"},
        _rule("web_device", "equals", "phone"),
        False,
    ),
    (
        "aud-03",
        {"web_device": "phone"},
        _rule("web_device", "not_equals", "phone"),
        False,
    ),
    (
        "aud-04",
        {"web_device": "tablet"},
        _rule("web_device", "not_equals", "phone"),
        True,
    ),
    ("aud-05", {}, _rule("web_device", "not_equals", "phone"), False),
    ("aud-06", {"web_device": "Phone"}, _rule("web_device", "equals", "phone"), False),
    (
        "aud-07",
        {"web_browser": "edge"},
        _rule("web_browser", "in", ["chrome", "edge"]),
        True,
    ),
    ("aud-08", {"web_language": "en"}, _rule("web_language", "equals", "en"), True),
    (
        "aud-09",
        {"web_utm_campaign": "spring-sale"},
        _rule("web_utm_campaign", "starts_with", "spring"),
        True,
    ),
    (
        "aud-10",
        {"web_utm_campaign": "spring-sale"},
        _rule("web_utm_campaign", "contains", "sale"),
        True,
    ),
    (
        "aud-11",
        {"web_utm_campaign": "Spring-Sale"},
        _rule("web_utm_campaign", "equals", "spring-sale"),
        False,
    ),
    (
        "aud-12",
        {"web_referrer": "www.google.com"},
        _rule("web_referrer", "equals", "www.google.com"),
        True,
    ),
    (
        "aud-13",
        {"web_visitor": "returning"},
        _rule("web_visitor", "equals", "new"),
        False,
    ),
    # A null the script must never send: the server drops it, so "not phone"
    # does not match a visitor whose device is unknown.
    ("aud-14", {"web_device": None}, _rule("web_device", "not_equals", "phone"), False),
    ("aud-15", {"web_device": ""}, _rule("web_device", "not_equals", "phone"), False),
    (
        "aud-16",
        {"web_device": "mobile"},
        _rule("web_device", "not_equals", "phone"),
        False,
    ),
    # A country the page sends is never used: the server sets it, or nothing.
    ("aud-17", {"web_country": "FR"}, _rule("web_country", "equals", "FR"), False),
    (
        "aud-18",
        {"plan": "pro", "web_device": "phone"},
        _rule("plan", "equals", "pro"),
        True,
    ),
    *[
        (
            f"absent-{key[len('web_') :]}-{operator}",
            {},
            _rule(key, operator, _negative_value(key, operator)),
            False,
        )
        for key in WEB_DETAIL_RULES
        for operator in NEGATIVE_OPERATORS
    ],
]


def utm_value(query: str, param: str) -> Optional[str]:
    """The first value of *param* in *query*, decoded as ``URLSearchParams``
    decodes it (``+`` is a space, ``%XX`` a UTF-8 byte)."""
    values = parse_qs(query, keep_blank_values=True).get(param)
    return values[0] if values else None


def server_matches(context: Dict[str, Any], rule: Dict[str, Any], case_id: str) -> bool:
    """Whether the server assigns a visitor with *context* to an experiment
    whose targeting is *rule*: the route's cleaning, then experiment
    targeting as ``AssignmentService`` applies it to a new visitor."""
    # Imported here: the services package imports the application's models.
    from backend.app.services.assignment_service import AssignmentService

    cleaned, _ = clean_web_details(context)
    service = AssignmentService(None)
    targeting_context = AssignmentService._build_targeting_context(
        "vector-visitor", cleaned
    )
    experiment = SimpleNamespace(id=f"visual-audience-{case_id}", targeting_rules=rule)
    answer = service._evaluate_experiment_targeting(
        experiment, targeting_context, user_id="vector-visitor"
    )
    return bool(answer["eligible"])


def build_vectors() -> Dict[str, Any]:
    details = [
        {
            "id": case_id,
            "key": key,
            "value": value,
            "kept": web_detail_is_kept(key, value),
        }
        for case_id, key, value, _ in DETAILS
    ]
    utm_queries = []
    for case_id, query, param, _ in UTM_QUERIES:
        value = utm_value(query, param)
        utm_queries.append(
            {
                "id": case_id,
                "query": query,
                "param": param,
                "value": value,
                "kept": value is not None and web_detail_is_kept(f"web_{param}", value),
            }
        )
    audiences = [
        {
            "id": case_id,
            "context": context,
            "rule": rule,
            "matches": server_matches(context, rule, case_id),
        }
        for case_id, context, rule, _ in AUDIENCES
    ]
    return {
        "description": (
            "Generated by `python -m backend.scripts.generate_visual_visitor_vectors` "
            "from the server (#1545); do not edit by hand."
        ),
        "keys": sorted(WEB_DETAIL_RULES),
        "utm_keys": list(UTM_KEYS),
        "devices": list(WEB_DEVICES),
        "browsers": list(WEB_BROWSERS),
        "visitors": list(WEB_VISITORS),
        "negative_operators": list(NEGATIVE_OPERATORS),
        "limits": {
            "referrer": MAX_REFERRER_LENGTH,
            "utm": MAX_UTM_LENGTH,
            "landing_path": MAX_LANDING_PATH_LENGTH,
        },
        "counts": {
            "details": len(details),
            "kept": sum(1 for case in details if case["kept"]),
            "utm_queries": len(utm_queries),
            "audiences": len(audiences),
        },
        "details": details,
        "utm_queries": utm_queries,
        "audiences": audiences,
    }


_LISTS = ("details", "utm_queries", "audiences")


def render(vectors: Dict[str, Any]) -> str:
    """One case per line, keys sorted, so a diff names the case that moved."""
    lines = ["{"]
    keys = list(vectors)
    for index, key in enumerate(keys):
        comma = "," if index < len(keys) - 1 else ""
        value = vectors[key]
        if key in _LISTS:
            lines.append(f"{json.dumps(key)}: [")
            for position, item in enumerate(value):
                tail = "," if position < len(value) - 1 else ""
                lines.append(
                    "  " + json.dumps(item, sort_keys=True, ensure_ascii=True) + tail
                )
            lines.append("]" + comma)
        else:
            lines.append(
                f"{json.dumps(key)}: {json.dumps(value, sort_keys=True)}{comma}"
            )
    lines.append("}")
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare with the committed file and exit 1 if it differs",
    )
    parser.add_argument("--output", type=Path, default=VECTORS_PATH)
    args = parser.parse_args(argv)

    text = render(build_vectors())
    if args.check:
        current = (
            args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        )
        if current != text:
            print(
                f"{args.output} is out of date: run "
                "`python -m backend.scripts.generate_visual_visitor_vectors`",
                file=sys.stderr,
            )
            return 1
        print(f"{args.output} is current")
        return 0
    args.output.write_text(text, encoding="utf-8")
    print(f"wrote {args.output}: {json.loads(text)['counts']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
