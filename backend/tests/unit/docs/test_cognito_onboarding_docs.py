"""What the docs say about Cognito self sign-up and adding a user (T94).

Pinned without a database or the app (this runs on a docs-only pull request):

* "Adding a user" in ``docs/cognito_integration.md`` keeps its heading and
  anchor, says in its first line that it is Cognito onboarding by an
  administrator, names ``COGNITO_SELF_SIGNUP_ENABLED``, and holds exactly the
  four ``aws cognito-idp`` commands of the procedure -- ``--permanent``
  included -- and the one ``read`` that prompts for the password the third
  command takes from ``$PASSWORD``.  :func:`onboarding_commands` turns the
  four into boto3 calls, and
  ``backend/tests/integration/auth/test_cognito_onboarding.py`` runs those
  calls against moto and signs the user in, so the procedure as written is
  executed, not only read;
* every page under ``docs/`` that mentions the sign-up or confirmation route
  is one of two pinned sets, computed by scanning: a page that describes
  registration names the setting, and two pages only list the route in a
  rate-limit table.  A new page that mentions the routes fails until it is
  classified here;
* every link from any page under ``docs/`` to a section of
  ``cognito_integration.md`` lands on a heading of that page, slugified as
  mkdocs' ``toc`` does.  ``mkdocs build --strict`` does not check anchors
  (mkdocs.yml has no ``validation:`` block), so this is what does.

Walks the filesystem rather than asking git, so it also runs in the
``core_build.sh`` copy, which has no ``.git``.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from backend.tests.unit.docs.test_cognito_integration_docs import section
from backend.tests.unit.docs.test_email_case_runbook import headings, slugify

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
DOCS = REPO_ROOT / "docs"
PAGE = DOCS / "cognito_integration.md"
AUTH_SERVICE = "backend/app/services/auth_service.py"

SETTING = "COGNITO_SELF_SIGNUP_ENABLED"
ADDING = "Adding a user"
POOL_PLACEHOLDER = "$COGNITO_USER_POOL_ID"
#: The password step takes the password from this variable, which the line
#: before it reads from a prompt that does not show it.
PASSWORD_PLACEHOLDER = "$PASSWORD"
PASSWORD_PROMPT = "printf 'Password: '; read -rs PASSWORD; printf '\\n'"

# ---------------------------------------------------------------------------
# The onboarding procedure
# ---------------------------------------------------------------------------

_FENCE_OPEN = re.compile(r"^\s*```\{\.bash\b")
_FENCE_CLOSE = re.compile(r"^\s*```\s*$")


def _flag_key(flag: str) -> str:
    """``--user-pool-id`` -> ``UserPoolId`` (the AWS CLI's own mapping)."""
    return "".join(part.capitalize() for part in flag[2:].split("-"))


def _shorthand(value: str) -> Dict[str, str]:
    """``Name=email,Value=a@b`` -> ``{"Name": "email", "Value": "a@b"}``."""
    pairs = [item.split("=", 1) for item in value.split(",")]
    assert all(len(pair) == 2 for pair in pairs), value
    return dict(pairs)


def to_boto_call(command: str) -> Tuple[str, Dict[str, Any]]:
    """One ``aws cognito-idp`` command line as (boto3 method, parameters).

    Handles exactly the shapes the procedure uses: ``--flag value``, a
    value-less boolean flag (``--permanent``), and ``--user-attributes``
    followed by ``Name=...,Value=...`` items.  Anything else is refused, so
    a new shape cannot be dropped silently.
    """
    words = shlex.split(command)
    assert words[:2] == ["aws", "cognito-idp"], command
    operation = words[2].replace("-", "_")
    params: Dict[str, Any] = {}
    rest = words[3:]
    i = 0
    while i < len(rest):
        flag = rest[i]
        assert flag.startswith("--"), f"unexpected word {flag!r} in {command!r}"
        key = _flag_key(flag)
        values: List[str] = []
        i += 1
        while i < len(rest) and not rest[i].startswith("--"):
            values.append(rest[i])
            i += 1
        assert key not in params, f"{flag} given twice in {command!r}"
        if not values:
            params[key] = True
        elif key == "UserAttributes":
            params[key] = [_shorthand(v) for v in values]
        else:
            assert len(values) == 1, f"{flag} takes one value in {command!r}"
            params[key] = values[0]
    return operation, params


def onboarding_commands(text: Optional[str] = None) -> List[Tuple[str, Dict[str, Any]]]:
    """The commands of "Adding a user", in order, as boto3 calls.

    The one line that is not an ``aws cognito-idp`` command is
    :data:`PASSWORD_PROMPT`, exactly, and it must come straight before the
    command that reads ``$PASSWORD``.
    """
    commands: List[str] = []
    inside = False
    for line in section(ADDING, text).splitlines():
        if not inside and _FENCE_OPEN.match(line):
            inside = True
        elif inside and _FENCE_CLOSE.match(line):
            inside = False
        elif inside and line.strip():
            commands.append(line.strip())
    assert not inside, "an unclosed fence in Adding a user"
    calls: List[Tuple[str, Dict[str, Any]]] = []
    prompted = False
    for command in commands:
        if command == PASSWORD_PROMPT:
            assert not prompted, "the password prompt is given twice"
            prompted = True
            continue
        operation, params = to_boto_call(command)
        if PASSWORD_PLACEHOLDER in params.values():
            assert prompted, f"{command!r} reads $PASSWORD that no prompt set"
        prompted = False
        calls.append((operation, params))
    return calls


#: The procedure, as the boto3 calls the documented commands make.
EXPECTED_PROCEDURE = [
    ("create_group", {"UserPoolId": POOL_PLACEHOLDER, "GroupName": "Developers"}),
    (
        "admin_create_user",
        {
            "UserPoolId": POOL_PLACEHOLDER,
            "Username": "jdoe",
            "UserAttributes": [
                {"Name": "email", "Value": "jane.doe@example.com"},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "given_name", "Value": "Jane"},
                {"Name": "family_name", "Value": "Doe"},
            ],
            "MessageAction": "SUPPRESS",
        },
    ),
    (
        "admin_set_user_password",
        {
            "UserPoolId": POOL_PLACEHOLDER,
            "Username": "jdoe",
            "Password": PASSWORD_PLACEHOLDER,
            "Permanent": True,
        },
    ),
    (
        "admin_add_user_to_group",
        {"UserPoolId": POOL_PLACEHOLDER, "Username": "jdoe", "GroupName": "Developers"},
    ),
]


def test_the_procedure_is_exactly_the_four_commands():
    assert onboarding_commands() == EXPECTED_PROCEDURE


def test_the_section_keeps_its_heading_and_says_what_it_is():
    """The heading is exactly "Adding a user", so ``#adding-a-user`` lands;
    what it is goes in the first line of the body instead."""
    body = section(ADDING).splitlines()[1:]
    first = next(line for line in body if line.strip())
    assert "(Cognito onboarding by an administrator.)" in first
    assert "adding-a-user" in {slugify(t) for _, t in headings(PAGE.read_text())}


def _new_password_detail() -> str:
    """``NEW_PASSWORD_REQUIRED_DETAIL`` from auth_service.py, read with ``ast``
    (this test imports no application code)."""
    tree = ast.parse((REPO_ROOT / AUTH_SERVICE).read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and [getattr(t, "id", None) for t in node.targets]
            == ["NEW_PASSWORD_REQUIRED_DETAIL"]
            and isinstance(node.value, ast.Constant)
        ):
            return node.value.value
    raise AssertionError(f"no NEW_PASSWORD_REQUIRED_DETAIL in {AUTH_SERVICE}")


def test_the_section_explains_the_setting_and_the_temporary_password():
    text = " ".join(section(ADDING).split())
    assert f"`{SETTING}=true`" in text
    assert "`--permanent` is required." in text
    assert "A temporary password cannot sign in through the API:" in text


def test_the_section_quotes_the_401_a_temporary_password_gets():
    """Step 6 quotes what ``POST /auth/token`` answers, word for word."""
    text = " ".join(section(ADDING).split())
    detail = _new_password_detail()
    assert f"`POST /api/v1/auth/token` answers `401` with `{detail}`" in text


def test_the_section_names_no_server_error_status():
    """A sign-in with a temporary password is refused with 401, not 5xx."""
    assert not re.search(r"\b5\d\d\b", section(ADDING))


# ---------------------------------------------------------------------------
# D1: every page that mentions the routes is classified
# ---------------------------------------------------------------------------

#: The sign-up route, or the confirmation route (not confirm-forgot-password).
ROUTE_MENTION = re.compile(r"/signup\b|/auth/confirm(?![-\w])")

#: Pages that describe registration; each must name the setting.
DESCRIBES_REGISTRATION = {
    "docs/api/endpoints.md",
    "docs/auth/auth-user-guide.md",
    "docs/auth/cognito-auth-testing.md",
    "docs/auth/flow.md",
    "docs/cognito_integration.md",
}

#: Pages that only list the sign-up route in a table of rate limits.
RATE_LIMIT_TABLE_ONLY = {
    "docs/api/api-docs-guide.md",
}


def _doc_files() -> List[Path]:
    """Every file under docs/ except the generated OpenAPI snapshots and the
    claims registries (docs/_claims/, which mkdocs does not publish: they quote
    the pages' sentences, so reading them would count each page twice)."""
    found = []
    for dirpath, dirnames, filenames in os.walk(DOCS):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if not d.startswith(".") and not (dirpath == str(DOCS) and d == "_claims")
        )
        for name in sorted(filenames):
            if name.startswith("openapi") and name.endswith(".json"):
                continue
            found.append(Path(dirpath) / name)
    return found


def pages_mentioning_the_routes() -> set:
    return {
        path.relative_to(REPO_ROOT).as_posix()
        for path in _doc_files()
        if ROUTE_MENTION.search(path.read_text(encoding="utf-8", errors="replace"))
    }


def test_every_page_mentioning_the_routes_is_classified():
    assert not DESCRIBES_REGISTRATION & RATE_LIMIT_TABLE_ONLY
    assert (
        pages_mentioning_the_routes() == DESCRIBES_REGISTRATION | RATE_LIMIT_TABLE_ONLY
    )


@pytest.mark.parametrize("page", sorted(DESCRIBES_REGISTRATION))
def test_a_page_that_describes_registration_names_the_setting(page):
    assert SETTING in (REPO_ROOT / page).read_text(encoding="utf-8")


def test_the_settings_page_lists_it_off_by_default():
    text = PAGE.read_text(encoding="utf-8")
    (row,) = [line for line in text.splitlines() if line.startswith(f"| `{SETTING}` |")]
    assert row.endswith("| No | `false` |"), row


# ---------------------------------------------------------------------------
# Anchors into cognito_integration.md, from every page under docs/
# ---------------------------------------------------------------------------

_LINK = re.compile(
    r"\]\((?P<target>[^)\s#]*cognito_integration\.md)#(?P<anchor>[^)\s]+)\)"
)


def links_into_the_page() -> List[Tuple[str, str]]:
    """(linking page, anchor) for every link into a section of the page."""
    links = []
    for path in _doc_files():
        if path.suffix != ".md":
            continue
        for match in _LINK.finditer(path.read_text(encoding="utf-8")):
            target = (path.parent / match.group("target")).resolve()
            if target == PAGE.resolve():
                links.append((path.relative_to(REPO_ROOT).as_posix(), match["anchor"]))
    return links


def test_every_link_into_cognito_integration_lands_on_a_heading():
    slugs = {slugify(title) for _, title in headings(PAGE.read_text(encoding="utf-8"))}
    links = links_into_the_page()
    assert links, "no page links into cognito_integration.md"
    missing = sorted((page, anchor) for page, anchor in links if anchor not in slugs)
    assert not missing, f"no heading for {missing}"


def test_the_link_scan_sees_a_relative_link():
    """Not vacuous: a link one directory down resolves to the page."""
    assert ("docs/api/auth.md", "adding-a-user") in links_into_the_page()


# ---------------------------------------------------------------------------
# S2: the committed stable snapshot documents the 404 on both operations
# ---------------------------------------------------------------------------


def test_the_stable_snapshot_documents_the_404():
    spec = json.loads((DOCS / "api" / "openapi-v1.stable.json").read_text("utf-8"))
    paths = spec["paths"]
    assert sorted(paths["/api/v1/auth/signup"]["post"]["responses"]) == [
        "201",
        "404",
        "422",
    ]
    assert sorted(paths["/api/v1/auth/confirm"]["post"]["responses"]) == [
        "200",
        "404",
        "422",
    ]
    for path in ("/api/v1/auth/signup", "/api/v1/auth/confirm"):
        assert SETTING in paths[path]["post"]["description"]
