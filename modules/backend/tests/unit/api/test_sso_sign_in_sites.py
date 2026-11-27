"""Every SSO session and hand-off code is minted for an account ``provision_user`` returned.

``sso_service.provision_user`` finds or creates the account a sign-in is for,
and refuses an account single sign-on does not sign in (a superuser's). So
each place under ``modules/backend/app`` that mints a session
(``create_local_access_token``, or the SAML sign-in's ``_issue_jwt``) or a
dashboard hand-off code (``issue_handoff_code``) is pinned here, keyed on its
enclosing function, with the way its account was reached:

* ``provision_user``: the function mentions it, or a function of its own file
  that does, on a line above the mint;
* ``redeem_handoff_code``: every caller of the function redeems a hand-off code
  on a line above the call. Codes are minted only in a place pinned here, and
  signed with a key only ``issue_handoff_code`` and ``redeem_handoff_code`` use.

Any mention of a minter counts, not only a call, so one passed as a callback or
imported under another name is a place too. A new place fails until it is added
here. So does a new function that calls ``jwt.encode`` (under any name it is
imported as): the five there are pinned in ``ENCODERS``. The refusal itself must
stay in ``provision_user``, above the first line that reads or sets the
account's role. Which functions may sign with ``SECRET_KEY`` at all is the core's
``backend/tests/unit/core/test_local_token_mint_sites.py``.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[5]
APP = REPO_ROOT / "modules" / "backend" / "app"
SSO = "modules/backend/app/api/v1/endpoints/sso.py"
SERVICE = "modules/backend/app/services/sso_service.py"

MINTERS = frozenset({"create_local_access_token", "issue_handoff_code", "_issue_jwt"})

Function = Tuple[str, str]
Site = Tuple[str, str, str]
References = Dict[Function, List[Tuple[str, int]]]

#: (file, function, minter): how the account it mints for was reached.
SITES: Dict[Site, str] = {
    (SSO, "_oidc_sign_in", "create_local_access_token"): "provision_user",
    (SSO, "_exchange_session", "create_local_access_token"): "redeem_handoff_code",
    (SSO, "_redirect_mode_callback", "issue_handoff_code"): "provision_user",
    (SSO, "saml_acs", "_issue_jwt"): "provision_user",
}

#: Every function under modules/backend/app that calls ``jwt.encode``: the
#: SAML sign-in's token, the hand-off code, the OIDC state cookie, and the two
#: warehouses' own service credentials.
ENCODERS: Set[Function] = {
    (SSO, "_issue_jwt"),
    (SERVICE, "issue_handoff_code"),
    (SERVICE, "start_oidc_login"),
    ("modules/backend/app/warehouse/bigquery.py", "_assertion"),
    ("modules/backend/app/warehouse/snowflake.py", "sign"),
}


class _Finder(ast.NodeVisitor):
    """Each name a function mentions, as a name or an attribute, with its line.

    A name bound by ``from ... import x as y`` is recorded as ``x``.
    """

    def __init__(self, path: str, found: References, aliases: Dict[str, str]) -> None:
        self.path = path
        self.found = found
        self.aliases = aliases
        self.stack: List[str] = []

    def _function(self, node) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def _add(self, name: str, line: int) -> None:
        where = self.stack[-1] if self.stack else "<module>"
        self.found[(self.path, where)].append((name, line))

    def visit_Name(self, node: ast.Name) -> None:
        self._add(self.aliases.get(node.id, node.id), node.lineno)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._add(node.attr, node.lineno)
        self.generic_visit(node)


def references(sources: Iterable[Tuple[str, str]]) -> References:
    found: References = defaultdict(list)
    for path, source in sources:
        tree = ast.parse(source, filename=path)
        aliases = {
            alias.asname: alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.asname
        }
        _Finder(path, found, aliases).visit(tree)
    return found


def sites(found: References) -> Set[Site]:
    return {
        (path, function, name)
        for (path, function), refs in found.items()
        for name, _ in refs
        if name in MINTERS
    }


def _above(refs: List[Tuple[str, int]], gates: Set[str], name: str) -> bool:
    """A mention of one of *gates* on a line above the first mention of *name*."""
    gate_lines = [line for n, line in refs if n in gates]
    name_lines = [line for n, line in refs if n == name]
    return bool(gate_lines and name_lines) and min(gate_lines) < min(name_lines)


def _reaching(found: References, path: str, gate: str) -> Set[str]:
    """*gate*, and the functions of *path* that mention it or one that does."""
    names = {
        function: {n for n, _ in refs}
        for (p, function), refs in found.items()
        if p == path
    }
    reach = {gate}
    while True:
        more = {f for f, ns in names.items() if f not in reach and ns & reach}
        if not more:
            return reach
        reach |= more


def unreached(found: References, pinned: Dict[Site, str]) -> List[Site]:
    """The pinned places whose account was not reached the way they say."""
    bad = []
    for site, gate in sorted(pinned.items()):
        path, function, minter = site
        if gate == "provision_user":
            gates = _reaching(found, path, "provision_user")
            ok = _above(found.get((path, function), []), gates, minter)
        elif gate == "redeem_handoff_code":
            callers = [
                refs
                for where, refs in found.items()
                if where != (path, function) and any(n == function for n, _ in refs)
            ]
            ok = bool(callers) and all(
                _above(refs, {"redeem_handoff_code"}, function) for refs in callers
            )
        else:
            ok = False
        if not ok:
            bad.append(site)
    return bad


def _leftmost(node: ast.AST) -> str:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


class _Encoders(ast.NodeVisitor):
    """The functions that call ``jwt.encode``, under any name it is imported as."""

    def __init__(self, path: str, modules: Set[str], functions: Set[str]) -> None:
        self.path = path
        self.modules = modules  # names the jwt package is bound to
        self.functions = functions  # names jwt's encode is bound to
        self.stack: List[str] = []
        self.found: Set[Function] = set()

    def _function(self, node) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "encode"
            and _leftmost(func.value) in self.modules
        ) or (isinstance(func, ast.Name) and func.id in self.functions):
            self.found.add((self.path, self.stack[-1] if self.stack else "<module>"))
        self.generic_visit(node)


def encoders(sources: Iterable[Tuple[str, str]]) -> Set[Function]:
    found: Set[Function] = set()
    for path, source in sources:
        tree = ast.parse(source, filename=path)
        modules: Set[str] = set()
        functions: Set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "jwt" or alias.name.startswith("jwt."):
                        modules.add((alias.asname or alias.name).split(".")[0])
            elif (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").split(".")[0] == "jwt"
            ):
                for alias in node.names:
                    if alias.name == "encode":
                        functions.add(alias.asname or alias.name)
                    else:
                        modules.add(alias.asname or alias.name)
        finder = _Encoders(path, modules, functions)
        finder.visit(tree)
        found |= finder.found
    return found


def _sources() -> List[Tuple[str, str]]:
    return [
        (path.relative_to(REPO_ROOT).as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted(APP.rglob("*.py"))
    ]


def test_these_are_the_places_that_mint_a_session_or_a_hand_off_code():
    found = sites(references(_sources()))
    assert found == set(SITES), (
        "A place that mints a session or a hand-off code was added or removed: "
        "pin it in SITES with how its account is reached (through provision_user). "
        f"Found: {sorted(found)}"
    )


def test_each_reaches_its_account_the_way_it_is_pinned():
    assert unreached(references(_sources()), SITES) == []


def test_these_are_the_functions_that_call_jwt_encode():
    found = encoders(_sources())
    assert found == ENCODERS, (
        "A function under modules/backend/app that calls jwt.encode was added "
        "or removed: if it mints a session or a hand-off code, mint it with "
        "create_local_access_token or issue_handoff_code and pin it in SITES. "
        f"Found: {sorted(found)}"
    )


def test_provision_user_refuses_a_superuser_before_it_changes_the_account():
    """The refusal is in the one function every place above reaches, on a line
    above the first one that reads or sets the account's role."""
    refs = references(_sources())[(SERVICE, "provision_user")]
    where = "provision_user refuses a superuser account above its first .role"
    assert _above(refs, {"is_superuser"}, "role"), where
    assert _above(refs, {"SUPERUSER_ACCOUNT_DETAIL"}, "role"), where


def test_hand_off_codes_are_signed_with_a_key_only_they_use():
    found = references(_sources())
    users = {
        where
        for where, refs in found.items()
        if any(name == "_handoff_key" for name, _ in refs)
    }
    assert users == {(SERVICE, "issue_handoff_code"), (SERVICE, "redeem_handoff_code")}


# ---------------------------------------------------------------------------
# The scan finds what it is for
# ---------------------------------------------------------------------------

PLANTED = """
from backend.app.core.security import create_local_access_token
from backend.app.core.security import create_local_access_token as _session_for
from modules.backend.app.services import sso_service


def under_another_name(user):
    return _session_for(user)


def minted_first(db, info, config):
    token = create_local_access_token(lookup(db, info))
    sso_service.provision_user(db, info, config)
    return token


def as_a_callback(run):
    return run(create_local_access_token)


def through_a_helper(db, info, config, hh):
    user = _found(db, info, config)
    return sso_service.issue_handoff_code(user.id, hh)


def _found(db, info, config):
    return sso_service.provision_user(db, info, config)


def exchanged(db, user_id):
    return create_local_access_token(db.get(user_id))


def route(code, secret):
    user_id = sso_service.redeem_handoff_code(code, secret)
    return exchanged(None, user_id)


def unredeemed(user_id):
    return exchanged(None, user_id)
"""


def test_the_scan_finds_each_planted_place_and_how_it_was_reached():
    found = references([("planted.py", PLANTED)])
    assert sites(found) == {
        ("planted.py", "under_another_name", "create_local_access_token"),
        ("planted.py", "minted_first", "create_local_access_token"),
        ("planted.py", "as_a_callback", "create_local_access_token"),
        ("planted.py", "through_a_helper", "issue_handoff_code"),
        ("planted.py", "exchanged", "create_local_access_token"),
    }
    pinned = {
        ("planted.py", "minted_first", "create_local_access_token"): "provision_user",
        ("planted.py", "as_a_callback", "create_local_access_token"): "provision_user",
        ("planted.py", "through_a_helper", "issue_handoff_code"): "provision_user",
        ("planted.py", "exchanged", "create_local_access_token"): "redeem_handoff_code",
    }
    # `unredeemed` calls `exchanged` without redeeming a code first.
    assert unreached(found, pinned) == [
        ("planted.py", "as_a_callback", "create_local_access_token"),
        ("planted.py", "exchanged", "create_local_access_token"),
        ("planted.py", "minted_first", "create_local_access_token"),
    ]


PLANTED_ENCODERS = {
    "plain.py": """
import jwt
from backend.app.core.config import settings


def raw_session(user):
    return jwt.encode({"sub": str(user.id)}, settings.SECRET_KEY, algorithm="HS256")


def text_only(value):
    return value.encode("utf-8")
""",
    "aliased.py": """
import jwt as pyjwt
from jwt import encode
from jwt import encode as sign_it


def by_alias(claims, key):
    return pyjwt.encode(claims, key)


def by_name(claims, key):
    return encode(claims, key)


def by_renamed(claims, key):
    return sign_it(claims, key)


def local_import(claims, key):
    import jwt

    return jwt.api_jwt.encode(claims, key)
""",
}


def test_the_scan_finds_each_planted_jwt_encode():
    assert encoders(PLANTED_ENCODERS.items()) == {
        ("plain.py", "raw_session"),
        ("aliased.py", "by_alias"),
        ("aliased.py", "by_name"),
        ("aliased.py", "by_renamed"),
        ("aliased.py", "local_import"),
    }
