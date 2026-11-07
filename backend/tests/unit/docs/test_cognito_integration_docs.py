"""What ``docs/cognito_integration.md`` tells an operator about Cognito sign-in.

Pinned without a database (this runs on a docs-only pull request):

* the page's links to its own sections land, slugified as mkdocs' ``toc``
  extension does it;
* "Linking an existing account" holds exactly one SQL statement, an
  ``UPDATE <schema>.users`` that sets ``external_id`` to ``cognito:`` and the
  Cognito user ID, with the placeholders the prose explains;
* the table of refusal reasons lists exactly the reason codes the code writes
  (``auth_service`` and ``cognito_accounts``, read through ``ast``);
* the 60-minute access-token lifetime the page states for the reference app
  client is CDK's default, which holds only while the stack sets no lifetime.

That the statement links an account a sign-in then reaches is run against
PostgreSQL by ``backend/tests/integration/api/test_cognito_link_docs.py``,
which reads it through :func:`link_statement` here.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from backend.tests.unit.docs.test_email_case_runbook import headings, slugify

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
PAGE = REPO_ROOT / "docs" / "cognito_integration.md"
SERVICES = REPO_ROOT / "backend" / "app" / "services"
AUTH_STACK = REPO_ROOT / "infrastructure" / "cdk" / "stacks" / "authentication_stack.py"

LINK_SECTION = "Linking an existing account"
SCHEMA = "<schema>"
SUB = "<the sub printed above>"
EMAIL = "<their email address on the platform>"

_SQL_BLOCK = re.compile(r"^```sql\n(.*?)^```", re.M | re.S)
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def _text() -> str:
    return PAGE.read_text(encoding="utf-8")


def section(title: str, text: str | None = None) -> str:
    """The page from the heading ``title`` to the next heading of its level."""
    lines = (_text() if text is None else text).splitlines(keepends=True)
    start = level = None
    fenced = False
    for i in range(len(lines)):
        line = lines[i]
        if line.startswith("```"):
            fenced = not fenced
            continue
        match = None if fenced else _HEADING.match(line)
        if not match:
            continue
        if start is None and match.group(2) == title:
            start, level = i, len(match.group(1))
        elif start is not None and len(match.group(1)) <= level:
            return "".join(lines[start:i])
    assert start is not None, f"no heading {title!r} in {PAGE.name}"
    return "".join(lines[start:])


def link_statement(text: str | None = None) -> str:
    """The one SQL statement of "Linking an existing account", as written."""
    blocks = _SQL_BLOCK.findall(section(LINK_SECTION, text))
    assert len(blocks) == 1, f"expected one sql block, found {len(blocks)}"
    return blocks[0].strip()


def _reason_codes(path: pathlib.Path) -> set:
    """The values of the module-level ``REASON_*`` string constants."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    codes = set()
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.startswith("REASON_")
            and isinstance(node.value, ast.Constant)
        ):
            codes.add(node.value.value)
    return codes


def test_the_pages_links_to_its_own_sections_land():
    text = _text()
    slugs = {slugify(title) for _, title in headings(text)}
    anchors = set(re.findall(r"\]\(#([a-z0-9-]+)\)", text))
    assert anchors, "the page links to none of its sections"
    assert anchors <= slugs, f"no heading for {sorted(anchors - slugs)}"


def test_the_link_statement_sets_the_prefixed_cognito_user_id():
    sql = link_statement()
    assert sql.startswith(f"UPDATE {SCHEMA}.users\n"), sql
    assert f"SET external_id = 'cognito:{SUB}'" in sql
    assert f"WHERE lower(email) = lower('{EMAIL}')" in sql
    assert sql.endswith("RETURNING id, username, email, external_id;")
    assert sql.count(";") == 1


def test_the_prose_explains_every_placeholder_of_the_statement():
    prose = section(LINK_SECTION)
    assert "`<schema>` is the platform's schema" in prose
    assert "`experimentation` unless you changed it (`POSTGRES_SCHEMA`" in prose
    assert "admin-get-user" in prose
    assert set(re.findall(r"<[^<>]+>", link_statement())) == {SCHEMA, SUB, EMAIL}


def test_the_reason_table_lists_exactly_the_codes_the_code_writes():
    written = _reason_codes(SERVICES / "auth_service.py") | _reason_codes(
        SERVICES / "cognito_accounts.py"
    )
    table = section("Reading a refused sign-in")
    documented = set(re.findall(r"^\| `([a-z_]+)` \|", table, re.M))
    assert written == {
        "not_configured",
        "wrong_issuer",
        "no_sub",
        "local_password",
        "linked_elsewhere",
        "legacy_unlinked",
        "no_email",
        "email_unverified",
        "email_taken",
        "field_too_long",
        "commit_failed",
    }
    assert documented == written


def test_the_stated_token_lifetime_is_the_reference_clients():
    """The page says 60 minutes because the stack sets no lifetime and CDK's
    default is 60 minutes; setting one there must change the page too."""
    assert "access_token_validity" not in AUTH_STACK.read_text(encoding="utf-8")
    assert "CDK's default of 60 minutes" in section("When a role change takes effect")


def test_the_page_says_where_to_check_a_sign_in():
    text = _text()
    assert "`GET /api/v1/users/me`" in text
    assert "Do not create the account on the platform first." in section(
        "Adding a user"
    )
