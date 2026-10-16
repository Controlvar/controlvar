"""A release-candidate tag builds the docs site but does not deploy it.

``.github/workflows/docs.yml`` publishes GitHub Pages from its ``deploy`` job.
That job used to run on any ``refs/tags/v*``, so a pre-release tag pushed to
rehearse a release (``v0.18.0-rc.1``) would have replaced the live site with
the candidate's documentation. The job now runs only on a release tag with no
``-`` in it.

The test does not look for a substring in the ``if:``. It evaluates the
expression the way GitHub does -- ``startsWith`` and ``contains`` compare
case-insensitively, ``==`` on strings too -- against a table of refs, so a
rewrite that keeps the words but changes the logic still fails. The evaluator
knows only the constructs the condition uses and raises on anything else, so a
new construct fails here until it is understood.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from backend.tests.unit.infrastructure.test_docs_only_gate import _load

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
DOCS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docs.yml"
#: This repository's id: a rename or a transfer keeps it.
REPOSITORY_ID = "1367480368"

_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<op>&&|\|\||==|!=|[!(),])"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_.\-]*))"
)


def _tokens(expr: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    pos = 0
    expr = expr.strip()
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m or m.end() == pos:
            raise ValueError(f"cannot tokenise {expr[pos:]!r}")
        kind = m.lastgroup
        assert kind is not None
        out.append((kind, m.group(kind)))
        pos = m.end()
    return out


class _Eval:
    """Recursive descent over ``||`` > ``&&`` > ``==``/``!=`` > ``!`` > atom."""

    def __init__(self, expr: str, context: Dict[str, str]) -> None:
        self.toks = _tokens(expr)
        self.i = 0
        self.context = context

    def run(self) -> Any:
        value = self._or()
        if self.i != len(self.toks):
            raise ValueError(f"trailing tokens: {self.toks[self.i :]}")
        return value

    def _peek(self) -> Tuple[str, str]:
        return self.toks[self.i] if self.i < len(self.toks) else ("", "")

    def _take(self, value: str) -> None:
        if self._peek()[1] != value:
            raise ValueError(f"expected {value!r}, got {self._peek()!r}")
        self.i += 1

    def _or(self) -> Any:
        left = self._and()
        while self._peek() == ("op", "||"):
            self.i += 1
            right = self._and()
            left = left or right
        return left

    def _and(self) -> Any:
        left = self._cmp()
        while self._peek() == ("op", "&&"):
            self.i += 1
            right = self._cmp()
            left = left and right
        return left

    def _cmp(self) -> Any:
        left = self._unary()
        if self._peek() in (("op", "=="), ("op", "!=")):
            op = self._peek()[1]
            self.i += 1
            right = self._unary()
            if not (isinstance(left, str) and isinstance(right, str)):
                raise ValueError("only string comparison is modelled")
            equal = left.lower() == right.lower()
            return equal if op == "==" else not equal
        return left

    def _unary(self) -> Any:
        if self._peek() == ("op", "!"):
            self.i += 1
            return not self._unary()
        return self._atom()

    def _atom(self) -> Any:
        kind, value = self._peek()
        if kind == "str":
            self.i += 1
            return value[1:-1].replace("''", "'")
        if (kind, value) == ("op", "("):
            self.i += 1
            inner = self._or()
            self._take(")")
            return inner
        if kind == "name":
            self.i += 1
            if self._peek() == ("op", "("):
                return self._call(value)
            if value not in self.context:
                raise ValueError(f"unmodelled context {value!r}")
            return self.context[value]
        raise ValueError(f"unexpected token {self._peek()!r}")

    def _call(self, fn: str) -> bool:
        self._take("(")
        a = self._or()
        self._take(",")
        b = self._or()
        self._take(")")
        if not (isinstance(a, str) and isinstance(b, str)):
            raise ValueError(f"{fn} on non-strings")
        if fn == "startsWith":
            return a.lower().startswith(b.lower())
        if fn == "contains":
            return b.lower() in a.lower()
        raise ValueError(f"unmodelled function {fn!r}")


def evaluate(expr: str, context: Dict[str, str]) -> bool:
    expr = expr.strip()
    if expr.startswith("${{") and expr.endswith("}}"):
        expr = expr[3:-2]
    return bool(_Eval(expr, context).run())


@pytest.fixture(scope="module")
def deploy_if() -> str:
    workflow = _load(DOCS_WORKFLOW)
    job = workflow["jobs"]["deploy"]
    assert job.get("environment", {}).get("name") == "github-pages"
    cond = job.get("if")
    assert isinstance(cond, str) and cond.strip(), "the deploy job has no if:"
    return cond


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
@pytest.mark.parametrize(
    "ref, deploys",
    [
        ("refs/tags/v0.17.0", True),
        ("refs/tags/v1.0.0", True),
        ("refs/tags/v0.18.0-rc.1", False),
        ("refs/tags/v1.2.0-beta", False),
        ("refs/heads/main", False),
        ("refs/heads/v0.17.0", False),
        ("refs/tags/sdk/js/v1.2.3", False),
    ],
)
def test_only_a_release_tag_deploys(
    deploy_if: str, ref: str, deploys: bool, event: str
):
    context = {
        "github.ref": ref,
        "github.repository_id": REPOSITORY_ID,
        "github.event_name": event,
    }
    assert evaluate(deploy_if, context) is deploys, (
        f"docs deploy on {ref} ({event}) should be {deploys}: if: {deploy_if}"
    )


def test_a_fork_never_deploys(deploy_if: str):
    context = {
        "github.ref": "refs/tags/v0.17.0",
        "github.repository_id": "1",
        "github.event_name": "push",
    }
    assert evaluate(deploy_if, context) is False


@pytest.mark.parametrize("wrong", ["136748036", "13674803680", ""])
def test_only_this_repository_s_id_deploys(deploy_if: str, wrong: str):
    """The guard is the id, which a rename keeps; a name in the context is not
    modelled, so a guard that went back to the name fails as unmodelled."""
    context = {"github.ref": "refs/tags/v0.17.0", "github.repository_id": wrong}
    assert evaluate(deploy_if, context) is False


def test_the_evaluator_refuses_what_it_does_not_know():
    ctx = {"github.ref": "refs/tags/v1.0.0"}
    with pytest.raises(ValueError):
        evaluate("endsWith(github.ref, '0')", ctx)
    with pytest.raises(ValueError):
        evaluate("github.head_ref == 'x'", ctx)
    assert evaluate("startsWith(github.ref, 'REFS/TAGS/V')", ctx) is True
    assert evaluate("!contains(github.ref, '-') && (github.ref != 'x')", ctx) is True
