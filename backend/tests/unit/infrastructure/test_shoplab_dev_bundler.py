"""ShopLab's dev server is webpack, so /products loads on a direct visit (#1172).

Under Next 16.4.0's Turbopack dev server, opening ``/products`` as the first
page (the address typed, or the page reloaded) throws in the browser::

    Module [project]/demo/shoplab/src/data/products.ts [client] (ecmascript)
    was instantiated because it was required from module
    [project]/demo/shoplab/src/pages/products/index.tsx [client] (ecmascript),
    but the module factory is not available.

``src/data/products.ts`` is in none of the page's client chunks. The page loads
when it is reached from another page, every other ShopLab page loads either
way, and ``next dev --webpack`` loads all of them. Moving one import above
another made it load, but nothing about that is stable, so the dev script
asks for webpack instead (``demo/setup-local.sh`` starts it as
``npm run dev``, so it follows the script).

The failure happens in the browser and the server renders only a shell, so a
request to the dev server answers 200 and a Jest run never loads Turbopack.
What can be pinned in the unit job is the choice: the ``dev`` script names
``--webpack`` and does not also name Turbopack. The check is a function of the
parsed package, and each way of getting it wrong is planted below, so it fails
here when it stops firing and not only when the real file regresses.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, List, Mapping

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
SHOPLAB_PACKAGE = REPO_ROOT / "demo" / "shoplab" / "package.json"

WHY = (
    "ShopLab's dev script must run `next dev --webpack`: the Turbopack dev server "
    "leaves src/data/products.ts out of /products' chunks, so opening /products "
    "directly fails with 'the module factory is not available' (#1172)"
)


def dev_bundler_problems(package: Mapping[str, Any]) -> List[str]:
    """What is wrong with ``scripts.dev`` as a choice of bundler."""
    script = (package.get("scripts") or {}).get("dev")
    if not script:
        return ["has no dev script"]
    lexer = shlex.shlex(script, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    words = list(lexer)
    if words[:2] != ["next", "dev"]:
        return [f"dev script is not `next dev`: {script}"]
    if any(set(word) <= set("();<>|&") for word in words):
        return [f"dev script chains another command: {script}"]
    if "--webpack" not in words:
        return [f"dev script does not pass --webpack: {script}"]
    for flag in ("--turbo", "--turbopack"):
        if flag in words:
            return [f"dev script also passes {flag}: {script}"]
    return []


def test_shoplab_dev_script_runs_webpack():
    package = json.loads(SHOPLAB_PACKAGE.read_text(encoding="utf-8"))
    assert dev_bundler_problems(package) == [], WHY


PLANTED = {
    "the script before the fix": (
        {"scripts": {"dev": "next dev -p 3200 -H 127.0.0.1"}},
        "dev script does not pass --webpack",
    ),
    "a flag that only begins --webpack": (
        {"scripts": {"dev": "next dev --webpack-cache -p 3200 -H 127.0.0.1"}},
        "dev script does not pass --webpack",
    ),
    "--webpack named by an earlier command": (
        {"scripts": {"dev": "echo --webpack && next dev -p 3200"}},
        "dev script is not `next dev`",
    ),
    "--webpack named by a later command": (
        {"scripts": {"dev": "next dev -p 3200 && echo --webpack"}},
        "dev script chains another command",
    ),
    "webpack and Turbopack together": (
        {"scripts": {"dev": "next dev --webpack --turbopack -p 3200"}},
        "dev script also passes --turbopack",
    ),
    "webpack and the --turbo spelling": (
        {"scripts": {"dev": "next dev --turbo --webpack -p 3200"}},
        "dev script also passes --turbo",
    ),
    "not next dev at all": (
        {"scripts": {"dev": "node server.js --webpack"}},
        "dev script is not `next dev`",
    ),
    "no dev script": ({"scripts": {"build": "next build"}}, "has no dev script"),
}


@pytest.mark.parametrize("package, expected", PLANTED.values(), ids=list(PLANTED))
def test_a_planted_dev_script_is_named(package, expected):
    problems = dev_bundler_problems(package)
    assert any(expected in p for p in problems), problems


def test_a_webpack_dev_script_passes():
    script = "next dev --webpack -p 3200 -H 127.0.0.1"
    assert dev_bundler_problems({"scripts": {"dev": script}}) == []
