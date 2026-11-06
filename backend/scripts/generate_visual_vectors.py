"""
Generate ``tests/sdk-contract/visual-changes-vectors.json``: the corpus the
server's validator of a visual experiment's page changes and page rules
answers, and the browser script that applies them is held to (#1545).

The corpus below is written by hand. Every verdict in the file comes from the
server: each document is run through
``backend.app.schemas.visual.VisualChanges``, and each page-rule case through
``page_matches``. ``backend/tests/unit/schemas/test_visual_changes.py`` holds
the corpus to what it must be on its own -- every REFUSED entry refused, every
ACCEPTED one accepted, exact counts -- and
``backend/tests/unit/schemas/test_visual_vectors.py`` runs ``--check``, so an
edit to the validator that moves an answer fails until the file is
regenerated, and then the script's own contract test fails until it agrees.

Usage (from the repository root, venv active):

    python -m backend.scripts.generate_visual_vectors          # rewrite the file
    python -m backend.scripts.generate_visual_vectors --check  # exit 1 on drift
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError

from backend.app.schemas.visual import (
    ALLOWED_ATTRIBUTES,
    ALLOWED_STYLE_FUNCTIONS,
    ALLOWED_STYLE_PROPERTIES,
    MARGIN_PROPERTIES,
    MAX_ALT_LENGTH,
    MAX_ATTRIBUTE_VALUE_LENGTH,
    MAX_CHANGES,
    MAX_CHANGES_BYTES,
    MAX_SELECTOR_LENGTH,
    MAX_STYLE_VALUE_LENGTH,
    MAX_STYLES,
    MAX_TEXT_LENGTH,
    VISUAL_FORMAT_VERSION,
    VISUAL_OPS,
    VisualChanges,
    page_matches,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
VECTORS_PATH = REPO_ROOT / "tests" / "sdk-contract" / "visual-changes-vectors.json"


def _doc(*changes: Dict[str, Any]) -> Dict[str, Any]:
    return {"version": VISUAL_FORMAT_VERSION, "changes": list(changes)}


def _href(value: str) -> Dict[str, Any]:
    return _doc(
        {"op": "set_attribute", "selector": "a", "name": "href", "value": value}
    )


def _attribute(name: str, value: str = "x") -> Dict[str, Any]:
    return _doc({"op": "set_attribute", "selector": "a", "name": name, "value": value})


def _src(value: str) -> Dict[str, Any]:
    return _doc({"op": "set_image", "selector": "img", "src": value})


def _style(prop: str, value: str) -> Dict[str, Any]:
    return _doc({"op": "set_style", "selector": "p", "styles": {prop: value}})


def _selector(value: str) -> Dict[str, Any]:
    return _doc({"op": "hide", "selector": value})


def _text(value: Any) -> Dict[str, Any]:
    return _doc({"op": "set_text", "selector": "h1", "text": value})


#: (id, the field it aims at, the document). Every one is refused. The field
#: is ``<op>.<field>``, ``selector`` or ``document``: V2 requires an entry
#: for every string field of every operation.
REFUSED: List[Tuple[str, str, Dict[str, Any]]] = [
    # Links: every scheme but http(s), and every other site.
    ("href-javascript", "set_attribute.value", _href("javascript:alert(1)")),
    (
        "href-javascript-case-space",
        "set_attribute.value",
        _href(" JaVaScRiPt:alert(1)"),
    ),
    ("href-javascript-tab", "set_attribute.value", _href("java\tscript:alert(1)")),
    (
        "href-javascript-control",
        "set_attribute.value",
        _href("\u0001javascript:alert(1)"),
    ),
    (
        "href-javascript-entity",
        "set_attribute.value",
        _href("&#106;avascript:alert(1)"),
    ),
    ("href-data", "set_attribute.value", _href("data:text/html,<b>x</b>")),
    ("href-vbscript", "set_attribute.value", _href("vbscript:msgbox(1)")),
    ("href-blob", "set_attribute.value", _href("blob:https://shop.example.com/1")),
    ("href-protocol-relative", "set_attribute.value", _href("//evil.test/x")),
    ("href-backslash-relative", "set_attribute.value", _href("/\\evil.test/x")),
    ("href-user-info", "set_attribute.value", _href("https://u:p@shop.example.com/")),
    ("href-space", "set_attribute.value", _href("https://shop.example.com/a b")),
    ("value-control", "set_attribute.value", _attribute("title", "a\u0007b")),
    ("src-javascript", "set_image.src", _src("javascript:alert(1)")),
    ("src-data", "set_image.src", _src("data:image/svg+xml,<svg onload=alert(1)>")),
    ("src-protocol-relative", "set_image.src", _src("//evil.test/x.png")),
    (
        "alt-control",
        "set_image.alt",
        _doc({"op": "set_image", "selector": "img", "src": "/a.png", "alt": "a\u0007"}),
    ),
    # Attribute names outside the five.
    ("name-onclick", "set_attribute.name", _attribute("onclick")),
    ("name-onclick-case", "set_attribute.name", _attribute("OnClick")),
    ("name-style", "set_attribute.name", _attribute("style")),
    ("name-srcdoc", "set_attribute.name", _attribute("srcdoc")),
    ("name-formaction", "set_attribute.name", _attribute("formaction")),
    ("name-action", "set_attribute.name", _attribute("action")),
    ("name-xlink-href", "set_attribute.name", _attribute("xlink:href")),
    ("name-id", "set_attribute.name", _attribute("id")),
    ("name-name", "set_attribute.name", _attribute("name")),
    ("name-is", "set_attribute.name", _attribute("is")),
    # Style values.
    ("style-url", "set_style.styles", _style("color", "url(https://evil.test/x)")),
    (
        "style-image-set",
        "set_style.styles",
        _style("background-color", "image-set(a 1x)"),
    ),
    ("style-import", "set_style.styles", _style("color", "@import url(x)")),
    ("style-expression", "set_style.styles", _style("width", "expression(alert(1))")),
    ("style-moz-binding", "set_style.styles", _style("color", "-moz-binding")),
    ("style-behavior", "set_style.styles", _style("color", "behavior")),
    ("style-escape", "set_style.styles", _style("color", "u\\72l(x)")),
    ("style-semicolon", "set_style.styles", _style("color", "red;position:fixed")),
    ("style-important", "set_style.styles", _style("color", "red !important")),
    ("style-comment", "set_style.styles", _style("color", "red /* x */")),
    ("style-angle", "set_style.styles", _style("color", "<script>")),
    # Style properties.
    ("prop-position", "set_style.styles", _style("position", "fixed")),
    ("prop-z-index", "set_style.styles", _style("z-index", "10")),
    ("prop-top", "set_style.styles", _style("top", "0")),
    ("prop-left", "set_style.styles", _style("left", "0")),
    ("prop-transform", "set_style.styles", _style("transform", "rotate(1deg)")),
    ("prop-pointer-events", "set_style.styles", _style("pointer-events", "none")),
    ("prop-order", "set_style.styles", _style("order", "1")),
    ("prop-flex-reverse", "set_style.styles", _style("flex-direction", "row-reverse")),
    ("prop-proto", "set_style.styles", _style("__proto__", "x")),
    ("prop-constructor", "set_style.styles", _style("constructor", "x")),
    (
        "styles-empty",
        "set_style.styles",
        _doc({"op": "set_style", "selector": "p", "styles": {}}),
    ),
    # Negative offsets, through margins (V99, C13).
    ("margin-negative", "set_style.styles", _style("margin-top", "-1px")),
    ("margin-second-token", "set_style.styles", _style("margin", "0 -1px")),
    ("margin-inline-start", "set_style.styles", _style("margin-inline-start", "-5px")),
    ("margin-negative-em", "set_style.styles", _style("margin-top", "-0.5em")),
    ("margin-exponent", "set_style.styles", _style("margin-top", "-1e2px")),
    (
        "margin-calc-product",
        "set_style.styles",
        _style("margin-top", "calc(-1 * 10px)"),
    ),
    (
        "margin-calc-difference",
        "set_style.styles",
        _style("margin-top", "calc(0px - 50px)"),
    ),
    ("margin-calc-percent", "set_style.styles", _style("margin-top", "calc(-50%)")),
    ("margin-min", "set_style.styles", _style("margin-top", "min(-10px,0px)")),
    ("margin-max", "set_style.styles", _style("margin-top", "max(-1px,-2px)")),
    (
        "margin-clamp",
        "set_style.styles",
        _style("margin-top", "clamp(-100px,-50px,0px)"),
    ),
    ("margin-var", "set_style.styles", _style("margin-top", "var(--x)")),
    # Text.
    ("text-nul", "set_text.text", _text("a\u0000b")),
    ("text-number", "set_text.text", _text(5)),
    # Selectors: each character S1 refuses on its own, each comment mark on
    # its own, control characters, and the length.
    ("selector-brace", "selector", _selector("div{color:red}")),
    ("selector-open-brace", "selector", _selector("div{")),
    ("selector-close-brace", "selector", _selector("div}")),
    ("selector-semicolon", "selector", _selector("a;b")),
    ("selector-backslash", "selector", _selector("a\\b")),
    ("selector-at", "selector", _selector("@media")),
    ("selector-angle", "selector", _selector("a<b")),
    ("selector-comment", "selector", _selector("a/*x*/")),
    ("selector-comment-open", "selector", _selector('a[href="/*"]')),
    ("selector-comment-close", "selector", _selector("a */")),
    ("selector-nul", "selector", _selector("a\u0000")),
    ("selector-newline", "selector", _selector("a\nb")),
    ("selector-tab", "selector", _selector("a\tb")),
    ("selector-delete", "selector", _selector("a\u007fb")),
    ("selector-c1-control", "selector", _selector("a\u0085b")),
    ("selector-empty", "selector", _selector("")),
    (
        "selector-513-code-points",
        "selector",
        _selector('[title="' + "\U0001f600" * 503 + '"]'),
    ),
    # The document and the operations.
    (
        "op-set-html",
        "document",
        _doc({"op": "set_html", "selector": "a", "html": "<b>"}),
    ),
    ("op-insert-html", "document", _doc({"op": "insert_html", "selector": "a"})),
    ("op-missing", "document", _doc({"selector": "a"})),
    (
        "op-extra-onclick",
        "document",
        _doc({"op": "hide", "selector": "a", "onclick": "x"}),
    ),
    (
        "op-extra-proto",
        "document",
        _doc({"op": "hide", "selector": "a", "__proto__": {}}),
    ),
    (
        "op-extra-constructor",
        "document",
        _doc({"op": "hide", "selector": "a", "constructor": 1}),
    ),
    ("version-2", "document", {"version": 2, "changes": []}),
    ("version-string", "document", {"version": "1", "changes": []}),
    ("version-true", "document", {"version": True, "changes": []}),
    ("version-missing", "document", {"changes": []}),
    ("document-extra", "document", {"version": 1, "changes": [], "script": "x"}),
]

#: (id, the document). Every one is accepted.
ACCEPTED: List[Tuple[str, Dict[str, Any]]] = [
    ("empty", _doc()),
    ("text-markup-is-text", _text("<img src=x onerror=window.__pwned=1>")),
    ("text-newline", _text("Line one\nLine two")),
    ("href-path", _href("/checkout")),
    ("href-https-query", _href("https://shop.example.com/a?ref=1")),
    ("href-http", _href("http://shop.example.com/")),
    ("attribute-title", _attribute("title", "Buy now")),
    ("attribute-aria-label", _attribute("aria-label", "Close")),
    ("attribute-placeholder", _attribute("placeholder", "you@example.com")),
    ("attribute-alt", _attribute("alt", "A photo")),
    (
        "image-path",
        _doc({"op": "set_image", "selector": "img", "src": "/img/a.png", "alt": "A"}),
    ),
    ("image-https", _src("https://cdn.example.com/a.png")),
    ("hide", _selector(".banner")),
    (
        "selector-complex",
        _selector('div.card > a[data-id^="p1"]:nth-of-type(2), #x + .y ~ p'),
    ),
    # Selectors: everything S1 does not refuse is accepted.
    ("selector-slash-in-value", _selector('a[href="/checkout"]')),
    ("selector-query-in-value", _selector("a[href*='?ref=']")),
    ("selector-ampersand-in-value", _selector("[data-x='a&b']")),
    ("selector-non-ascii-value", _selector('[aria-label="Añadir al carrito"]')),
    ("selector-non-ascii-value-2", _selector("[title='Warenkorb öffnen']")),
    ("selector-slash-and-star-apart", _selector('a[href^="/"] > *')),
    # 512 code points, 1014 UTF-16 code units: the limit counts code points.
    (
        "selector-512-code-points",
        _selector('[title="' + "\U0001f600" * 502 + '"]'),
    ),
    (
        "style-colours",
        _doc(
            {
                "op": "set_style",
                "selector": "p",
                "styles": {
                    "color": "rgb(1, 2, 3)",
                    "background-color": "hsla(10, 20%, 30%, 0.5)",
                },
            }
        ),
    ),
    ("style-var-colour", _style("color", "var(--brand)")),
    ("style-calc-width", _style("width", "calc(100% - 2rem)")),
    ("style-clamp-font", _style("font-size", "clamp(1rem, 2vw, 3rem)")),
    ("style-font-family", _style("font-family", '"Helvetica Neue", Arial')),
    ("margin-auto", _style("margin", "0 auto")),
    ("margin-four", _style("margin", "10px 0 5% 1.5em")),
    ("margin-zero", _style("margin-top", "0")),
    ("padding-calc", _style("padding", "calc(1rem + 2px)")),
    ("flex-row", _style("flex-direction", "column")),
    (
        "every-op",
        _doc(
            {"op": "set_text", "selector": "#a", "text": "Hi"},
            {"op": "set_attribute", "selector": "#b", "name": "href", "value": "/x"},
            {"op": "set_image", "selector": "#c", "src": "/c.png"},
            {"op": "hide", "selector": "#d"},
            {"op": "set_style", "selector": "#e", "styles": {"opacity": "0.5"}},
        ),
    ),
]


def _rule(match: str, url: str) -> Dict[str, str]:
    return {"match": match, "url": url}


_EXACT = _rule("exact", "https://shop.example.com/a")
_PREFIX = _rule("prefix", "https://shop.example.com/shop/")
_ROOT = _rule("prefix", "https://shop.example.com/")

#: (id, rule, host, path, whether it matches). As plan 393's vectors: the host
#: is compared in lower case without a port, one trailing / is ignored, the
#: query and fragment are ignored, and the path is compared exactly.
PAGE_RULES: List[Tuple[str, Dict[str, str], str, str, bool]] = [
    ("exact-same", _EXACT, "shop.example.com", "/a", True),
    ("exact-host-case", _EXACT, "SHOP.Example.com", "/a", True),
    ("exact-port-443", _EXACT, "shop.example.com:443", "/a", True),
    ("exact-trailing-slash", _EXACT, "shop.example.com", "/a/", True),
    ("exact-query", _EXACT, "shop.example.com", "/a?x=1", True),
    ("exact-fragment", _EXACT, "shop.example.com", "/a#top", True),
    ("exact-path-case", _EXACT, "shop.example.com", "/A", False),
    ("exact-below", _EXACT, "shop.example.com", "/a/b", False),
    ("exact-other-host", _EXACT, "www.shop.example.com", "/a", False),
    ("exact-suffix-host", _EXACT, "shop.example.com.evil.test", "/a", False),
    ("prefix-itself", _PREFIX, "shop.example.com", "/shop", True),
    ("prefix-below", _PREFIX, "shop.example.com", "/shop/shoes/red", True),
    ("prefix-sibling", _PREFIX, "shop.example.com", "/shopping", False),
    ("prefix-above", _PREFIX, "shop.example.com", "/", False),
    ("root-anything", _ROOT, "shop.example.com", "/any/path", True),
    ("root-empty-path", _ROOT, "shop.example.com", "", True),
    ("root-other-host", _ROOT, "example.com", "/", False),
    (
        "encoded-kept",
        _rule("exact", "https://shop.example.com/a%20b"),
        "shop.example.com",
        "/a%20b",
        True,
    ),
    (
        "encoded-not-decoded",
        _rule("exact", "https://shop.example.com/a%20b"),
        "shop.example.com",
        "/a b",
        False,
    ),
]


def _verdict(document: Dict[str, Any]) -> Dict[str, Any]:
    try:
        VisualChanges.model_validate(document)
    except ValidationError as error:
        return {"accepted": False, "error_type": error.errors()[0]["type"]}
    return {"accepted": True}


def build_vectors() -> Dict[str, Any]:
    """The file's contents, every verdict from the server's own code."""
    changes = []
    for case_id, field, document in REFUSED:
        changes.append(
            {"id": case_id, "field": field, "document": document, **_verdict(document)}
        )
    for case_id, document in ACCEPTED:
        changes.append({"id": case_id, "document": document, **_verdict(document)})
    page_rules = [
        {
            "id": case_id,
            "rule": rule,
            "host": host,
            "path": path,
            "matches": page_matches(host, path, rule),
        }
        for case_id, rule, host, path, _ in PAGE_RULES
    ]
    return {
        "description": (
            "Generated by `python -m backend.scripts.generate_visual_vectors` "
            "from the server's validator (#1545); do not edit by hand."
        ),
        "format_version": VISUAL_FORMAT_VERSION,
        "ops": list(VISUAL_OPS),
        "allowed_attributes": list(ALLOWED_ATTRIBUTES),
        "allowed_style_properties": sorted(ALLOWED_STYLE_PROPERTIES),
        "margin_properties": sorted(MARGIN_PROPERTIES),
        "allowed_style_functions": sorted(ALLOWED_STYLE_FUNCTIONS),
        "limits": {
            "changes": MAX_CHANGES,
            "bytes": MAX_CHANGES_BYTES,
            "selector": MAX_SELECTOR_LENGTH,
            "text": MAX_TEXT_LENGTH,
            "attribute_value": MAX_ATTRIBUTE_VALUE_LENGTH,
            "alt": MAX_ALT_LENGTH,
            "styles": MAX_STYLES,
            "style_value": MAX_STYLE_VALUE_LENGTH,
        },
        "counts": {
            "changes": len(changes),
            "accepted": sum(1 for case in changes if case["accepted"]),
            "page_rules": len(page_rules),
        },
        "changes": changes,
        "page_rules": page_rules,
    }


def render(vectors: Dict[str, Any]) -> str:
    """One case per line, keys sorted, so a diff names the case that moved."""
    lines = ["{"]
    keys = list(vectors)
    for index, key in enumerate(keys):
        comma = "," if index < len(keys) - 1 else ""
        value = vectors[key]
        if key in ("changes", "page_rules"):
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
                "`python -m backend.scripts.generate_visual_vectors`",
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
