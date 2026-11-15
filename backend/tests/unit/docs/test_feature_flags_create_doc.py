"""`docs/feature-flags/create.md` — what can be checked without a stack.

The page's examples once failed at every step while each `curl` exited 0: a
login sent `username` (422), the create call had no trailing slash (a 307 curl
does not follow, so nothing was created), later steps used literal placeholders
(`flag-uuid-here`, `your-api-key`), and create and activate sent a `"status"`
field the API silently ignores -- there is no DRAFT status.  The documentation
runner will execute the page; until then these pin the shape of each example:

* every ```json sample parses;
* no placeholder a reader cannot fill in is left in a shell example;
* every body sent to the create or update endpoint uses only fields the request
  schema writes: the API refuses any other field with 422, and the read-only
  fields it accepts (``id``, ``status``, ...) are ignored, so an example that
  sends one teaches a field that does nothing;
* the collection URL is written with its trailing slash, a single flag's without;
* the page never mentions a draft state.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from backend.app.schemas.feature_flag import (
    READ_ONLY_FIELDS,
    FeatureFlagCreate,
    FeatureFlagUpdate,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

PAGE = (
    pathlib.Path(__file__).resolve().parents[4] / "docs" / "feature-flags" / "create.md"
)

# ```bash, or the tagged form the documentation runner requires (```{.bash exec}).
_FENCE = re.compile(r"^```(?:\{\.)?(\w*)[^`]*$")
PLACEHOLDERS = (
    "your-username",
    "your-password",
    "your-api-key",
    "flag-uuid-here",
    "(unchanged:",
)


def _blocks() -> list[tuple[str, int, str]]:
    """(language, first line number, text) of every fenced block."""
    blocks, lang, start, lines = [], None, 0, []
    for number, line in enumerate(PAGE.read_text(encoding="utf-8").splitlines(), 1):
        match = _FENCE.match(line)
        if lang is None and match:
            lang, start, lines = match.group(1), number + 1, []
        elif lang is not None and line.strip() == "```":
            blocks.append((lang, start, "\n".join(lines)))
            lang = None
        elif lang is not None:
            lines.append(line)
    assert lang is None, f"unclosed fence starting at line {start}"
    return blocks


def _shell() -> list[tuple[int, str]]:
    return [(start, text) for lang, start, text in _blocks() if lang == "bash"]


def _flag_requests():
    """(line, method, url, body) of each shell call to the flag endpoints."""
    for start, text in _shell():
        for call in re.split(r"\n(?=\S)", text):
            url = re.search(r"localhost:8000(/api/v1/feature-flags[^\s\\\"']*)", call)
            body = re.search(r"-d '(\{.*?\})'", call, re.S)
            if not url or not body or "/evaluate/" in url.group(1):
                continue
            method = re.search(r"-X (\w+)", call)
            yield (
                start,
                method.group(1) if method else "GET",
                url.group(1),
                body.group(1),
            )


def test_the_page_has_its_examples():
    assert len(_shell()) == 15
    # create, rules, rules refused (#535), turn on, a string flag created and
    # a number flag refused (#1497)
    assert len(list(_flag_requests())) == 6


def test_the_value_examples_are_what_the_api_answers():
    """#1497: the string flag the page creates is a valid create, and the
    number flag it sends text answers exactly the promised message, at
    ``on_value``."""
    from pydantic import ValidationError

    text = PAGE.read_text(encoding="utf-8")
    promised = re.search(r'<!-- expect: "msg":"(on_value [^"]+)" -->', text)
    assert promised, "the value 422 example's expectation is missing"
    created = [
        json.loads(body)
        for _, method, _, body in _flag_requests()
        if method == "POST" and '"value_type"' in body
    ]
    assert len(created) == 2
    label, refused = created
    assert FeatureFlagCreate(**label).value_type == "string"
    with pytest.raises(ValidationError) as caught:
        FeatureFlagCreate(**refused)
    (error,) = caught.value.errors()
    assert error["loc"] == ("on_value",)
    assert error["msg"] == promised.group(1)


def test_the_refused_rules_example_is_what_the_api_answers():
    """#535: the page's 422 example promises a message; the update schema
    gives exactly that message for the body the example sends."""
    from pydantic import ValidationError

    text = PAGE.read_text(encoding="utf-8")
    promised = re.search(r'<!-- expect: "msg":"(Value error, [^"]+)" -->', text)
    assert promised, "the 422 example's expectation is missing"
    refused = [
        json.loads(body)
        for _, method, _, body in _flag_requests()
        if method == "PUT" and '"equal"' in body
    ]
    assert len(refused) == 1
    with pytest.raises(ValidationError) as caught:
        FeatureFlagUpdate(**refused[0])
    assert caught.value.errors()[0]["msg"] == promised.group(1)


def test_every_json_sample_parses():
    for lang, start, text in _blocks():
        if lang == "json":
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                pytest.fail(f"create.md:{start}: {exc}")


def test_no_placeholder_is_left_in_a_shell_example():
    found = [
        f"create.md:{start}: {placeholder}"
        for start, text in _shell()
        for placeholder in PLACEHOLDERS
        if placeholder in text
    ]
    assert not found, found


def test_the_page_never_mentions_a_draft_state():
    assert "draft" not in PAGE.read_text(encoding="utf-8").lower()


def test_login_sends_an_email_not_a_username():
    login = [t for _, t in _shell() if "/auth/login" in t]
    assert login and all('"email"' in t and '"username"' not in t for t in login)


def _writable(schema) -> set[str]:
    """The fields a request writes: declared, and not read-only (#94)."""
    return set(schema.model_fields) - READ_ONLY_FIELDS


def test_the_writable_set_excludes_every_read_only_field():
    for schema in (FeatureFlagCreate, FeatureFlagUpdate):
        assert READ_ONLY_FIELDS <= set(schema.model_fields)
        assert not (_writable(schema) & READ_ONLY_FIELDS)
        assert {"key", "name", "is_active", "default_value"} <= _writable(schema)


def test_every_body_uses_only_fields_the_api_writes():
    for start, method, url, body in _flag_requests():
        schema = FeatureFlagCreate if method == "POST" else FeatureFlagUpdate
        unwritten = set(json.loads(body)) - _writable(schema)
        assert not unwritten, (
            f"create.md:{start}: {method} {url} sends {sorted(unwritten)}, which the "
            f"API refuses (422) or accepts and ignores"
        )


def test_urls_are_written_the_way_the_api_serves_them():
    for start, method, url, _ in _flag_requests():
        if method == "POST":
            assert url == "/api/v1/feature-flags/", f"create.md:{start}: {url}"
        else:
            assert not url.endswith("/"), f"create.md:{start}: {url} answers 307"
