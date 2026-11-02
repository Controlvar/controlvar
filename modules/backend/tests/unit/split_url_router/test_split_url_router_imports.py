"""The router imports only the Python standard library (#393, V16).

Lambda@Edge runs the bundled ``handler.py`` with nothing installed beside it:
a third-party import there is an outage on the customer's site, not a pass
through. Two gates, on the code asset the edge app actually bundles:

* (a) a fresh interpreter, with no site-packages and no current directory on
  its path (``python -I -S``), imports the bundled ``handler``;
* (b) every import in it is a standard-library module of an explicit list,
  and it calls neither ``importlib.import_module`` nor ``__import__``, which
  (a) does not see inside a function.

The interpreter is the tests' (3.11); the function runs python3.12. The list
in (b) holds no module removed in 3.12 or 3.13.
"""

from __future__ import annotations

import ast
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.unit]

#: Every module the router imports. A new one is added here on purpose.
ALLOWED = {
    "hashlib",
    "http.client",
    "json",
    "logging",
    "os",
    "ssl",
    "string",
    "time",
    "urllib.parse",
    "uuid",
}
#: Standard-library modules removed in Python 3.12 and 3.13.
REMOVED_BY_3_13 = {
    "aifc", "asynchat", "asyncore", "audioop", "cgi", "cgitb", "chunk", "crypt",
    "distutils", "imghdr", "imp", "lib2to3", "mailcap", "msilib", "nis",
    "nntplib", "ossaudiodev", "pipes", "smtpd", "sndhdr", "spwd", "sunau",
    "telnetlib", "uu", "xdrlib",
}  # fmt: skip


def test_the_bundled_router_imports_with_no_site_packages(edge_synth):
    """V16(a): the bundled handler imports in an isolated interpreter."""
    asset = edge_synth.function_asset()
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import handler; "
            "print(sorted(m for m in sys.modules if m.split('.')[0] == 'handler'))",
            str(asset),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "['handler']"


def _imports(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield ("." * node.level) + (node.module or "")


def _dynamic_imports(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "__import__":
                yield "__import__"
            elif isinstance(func, ast.Attribute) and func.attr == "import_module":
                yield "import_module"


def test_the_bundled_router_imports_only_the_listed_standard_library(edge_synth):
    """V16(b): the imports, statically, against the list and the standard
    library; no dynamic import."""
    source = (edge_synth.function_asset() / "handler.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set(_imports(tree))
    assert imported - ALLOWED == set()
    assert {name.split(".")[0] for name in imported} <= set(sys.stdlib_module_names)
    assert not imported & REMOVED_BY_3_13
    assert list(_dynamic_imports(tree)) == []
