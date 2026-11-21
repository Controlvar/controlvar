"""Only one function mints local access tokens (#501).

A local access token carries the account's password stamp (``pwv``), and a
token without one is accepted until it expires (tokens from before the stamp
existed). So a second place that minted tokens the API accepts as its own,
without going through ``security.create_local_access_token``, would mint
tokens no password change could end. Two static sets pin that, read from the
source with ``ast`` and keyed on the enclosing function, never on a line:

* (i) the ``*.encode(...)`` calls one of whose arguments names
  ``SECRET_KEY`` are exactly ``create_local_access_token`` in the core, and,
  when the modules are present, one function of theirs, the SAML sign-in's
  ``_issue_jwt`` (its tokens carry no ``iss``, so ``decode_local_token``
  refuses them; a test of the modules' own pins that);
* (ii) the dict literals (and ``dict(iss=...)`` calls and ``x["iss"] = ...``
  assignments) whose ``"iss"`` names ``LOCAL_TOKEN_ISSUER`` or spells out its
  value are exactly ``create_local_access_token``.

It scans ``backend/app``, and the modules' application package when it is
present, so the core build's copy (no ``modules/``) runs it too. The modules'
side is named by its function only, so this core test names no module file.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator, List, Optional, Set, Tuple

import pytest

from backend.app.core.security import LOCAL_TOKEN_ISSUER

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
CORE = REPO_ROOT / "backend" / "app"
MODULES = REPO_ROOT / "modules" / "backend" / "app"

MINTER = ("backend/app/core/security.py", "create_local_access_token")
#: The one function of the modules that signs with SECRET_KEY.
MODULE_ENCODER = "_issue_jwt"

SECRET_NAME = "SECRET_KEY"
ISSUER_NAME = "LOCAL_TOKEN_ISSUER"
#: The issuer's value, read from the module rather than spelled here.
ISSUER_VALUE = LOCAL_TOKEN_ISSUER

Site = Tuple[str, str]


def _names(node: Optional[ast.AST], name: str) -> bool:
    """Whether ``node`` mentions ``name`` as a name or an attribute."""
    if node is None:
        return False
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id == name:
            return True
        if isinstance(child, ast.Attribute) and child.attr == name:
            return True
    return False


def _is_local_issuer(node: Optional[ast.AST]) -> bool:
    if _names(node, ISSUER_NAME):
        return True
    return node is not None and any(
        isinstance(child, ast.Constant) and child.value == ISSUER_VALUE
        for child in ast.walk(node)
    )


def _is_iss(key: Optional[ast.AST]) -> bool:
    return isinstance(key, ast.Constant) and key.value == "iss"


class _Finder(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.stack: List[str] = []
        self.encoders: Set[Site] = set()
        self.issuers: Set[Site] = set()

    def _site(self) -> Site:
        return (self.path, self.stack[-1] if self.stack else "<module>")

    def _function(self, node) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "encode":
            arguments = list(node.args) + [k.value for k in node.keywords]
            if any(_names(argument, SECRET_NAME) for argument in arguments):
                self.encoders.add(self._site())
        if isinstance(func, ast.Name) and func.id == "dict":
            for keyword in node.keywords:
                if keyword.arg == "iss" and _is_local_issuer(keyword.value):
                    self.issuers.add(self._site())
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values):
            if _is_iss(key) and _is_local_issuer(value):
                self.issuers.add(self._site())
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if (
                isinstance(target, ast.Subscript)
                and _is_iss(target.slice)
                and _is_local_issuer(node.value)
            ):
                self.issuers.add(self._site())
        self.generic_visit(node)


def _sources(roots: List[Path]) -> Iterator[Tuple[str, str]]:
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            yield (
                path.relative_to(REPO_ROOT).as_posix(),
                path.read_text(encoding="utf-8"),
            )


def scan(sources) -> Tuple[Set[Site], Set[Site]]:
    """The functions that encode with SECRET_KEY, and those that write the
    local issuer, over ``(path, source)`` pairs."""
    encoders: Set[Site] = set()
    issuers: Set[Site] = set()
    for path, source in sources:
        finder = _Finder(path)
        finder.visit(ast.parse(source, filename=path))
        encoders |= finder.encoders
        issuers |= finder.issuers
    return encoders, issuers


def _roots() -> List[Path]:
    return [CORE] + ([MODULES] if MODULES.is_dir() else [])


def test_only_create_local_access_token_signs_with_the_secret_key():
    encoders, _ = scan(_sources(_roots()))
    core = {site for site in encoders if site[0].startswith("backend/")}
    module_functions = sorted(name for path, name in encoders - core)
    expected_modules = [MODULE_ENCODER] if MODULES.is_dir() else []
    assert (core, module_functions) == ({MINTER}, expected_modules), (
        "A function other than security.create_local_access_token encodes "
        "with SECRET_KEY: mint local access tokens through it, so they carry "
        f"the password stamp. Found: {sorted(encoders)}"
    )


def test_only_create_local_access_token_writes_the_local_issuer():
    _, issuers = scan(_sources(_roots()))
    assert issuers == {MINTER}, (
        "A function other than security.create_local_access_token writes the "
        f"local tokens' issuer. Found: {sorted(issuers - {MINTER})}"
    )


# ---------------------------------------------------------------------------
# The scan finds what it is for
# ---------------------------------------------------------------------------

PLANTED_ENCODER = """
import jwt
from backend.app.core.config import settings

def side_door(user):
    return jwt.encode({"sub": str(user.id)}, settings.SECRET_KEY, algorithm="HS256")

def keyword_door(user):
    return jwt.encode({"sub": str(user.id)}, key=settings.SECRET_KEY)

def not_a_signer():
    return settings.SECRET_KEY.encode("utf-8")
"""

PLANTED_ISSUERS = f"""
from backend.app.core.security import LOCAL_TOKEN_ISSUER

def by_constant(user):
    return {{"sub": str(user.id), "iss": LOCAL_TOKEN_ISSUER}}

def by_literal(user):
    return {{"sub": str(user.id), "iss": {ISSUER_VALUE!r}}}

def by_keyword(user):
    return dict(sub=str(user.id), iss=LOCAL_TOKEN_ISSUER)

def by_assignment(claims):
    claims["iss"] = {ISSUER_VALUE!r}

def another_issuer():
    return {{"iss": "controlvar-local-mfa"}}
"""


def test_the_scan_finds_a_planted_encoder():
    encoders, _ = scan([("planted.py", PLANTED_ENCODER)])
    assert encoders == {("planted.py", "side_door"), ("planted.py", "keyword_door")}


def test_the_scan_finds_every_planted_issuer():
    _, issuers = scan([("planted.py", PLANTED_ISSUERS)])
    assert issuers == {
        ("planted.py", "by_constant"),
        ("planted.py", "by_literal"),
        ("planted.py", "by_keyword"),
        ("planted.py", "by_assignment"),
    }
