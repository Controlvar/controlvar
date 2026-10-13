"""What the package is: its name, its import name, what it needs, and its shape.

These hold the package's description of itself (``pyproject.toml``) and the
source to the few plain facts a reader acts on before writing any code: that
it installs as ``experimently`` and imports as ``experimentation``, needs
Python 3.9 or later and nothing else, is a 0.x beta, talks HTTP through the
standard library, and is synchronous.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Any, Dict

import experimentation
from experimentation import Assignment, ExperimentationClient, FlagEvaluation, UrllibTransport
from experimentation import transport as transport_module

SDK_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = SDK_ROOT / "pyproject.toml"
PACKAGE = SDK_ROOT / "experimentation"


def project() -> Dict[str, Any]:
    """The ``[project]`` and ``[tool.setuptools]`` tables of pyproject.toml."""
    text = PYPROJECT.read_text(encoding="utf-8")
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:  # pragma: no cover - 3.9/3.10: the stdlib has no TOML reader
        import tomli as tomllib  # type: ignore[no-redef]
    data = tomllib.loads(text)
    return {"project": data["project"], "setuptools": data["tool"]["setuptools"]}


def test_the_package_is_a_0_x_beta():
    meta = project()["project"]
    assert meta["version"].split(".")[0] == "0", meta["version"]
    assert "Development Status :: 4 - Beta" in meta["classifiers"]


def test_it_installs_as_experimently_and_imports_as_experimentation():
    data = project()
    assert data["project"]["name"] == "experimently"
    assert data["setuptools"]["packages"] == ["experimentation"]
    assert Path(experimentation.__file__).resolve().parent == PACKAGE.resolve()


def test_it_needs_python_3_9_and_no_other_package():
    meta = project()["project"]
    assert meta["requires-python"] == ">=3.9"
    assert meta["dependencies"] == []


def test_http_goes_through_the_standard_library():
    client = ExperimentationClient("http://test", "key")
    assert isinstance(client._transport, UrllibTransport)
    imported = set()
    for node in ast.walk(ast.parse(Path(transport_module.__file__).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert "urllib" in imported
    assert imported <= {"__future__", "http", "json", "typing", "urllib"}, imported


def _is_stdlib(name: str) -> bool:
    names = getattr(sys, "stdlib_module_names", None)  # Python 3.10+
    if names is not None:
        return name in names
    spec = importlib.util.find_spec(name)  # pragma: no cover - Python 3.9
    origin = (spec.origin or "") if spec else ""
    return spec is not None and (origin in ("built-in", "frozen") or "site-packages" not in origin)


def test_every_module_imports_only_the_standard_library_and_itself():
    imported = []
    for path in sorted(PACKAGE.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported += [(path.name, alias.name.split(".")[0]) for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.append((path.name, node.module.split(".")[0]))
    assert len(imported) > 10, "found almost no import: the scan is broken"
    third_party = [f"{module}: {name}" for module, name in imported if not _is_stdlib(name)]
    assert not third_party, third_party


def test_the_client_is_synchronous():
    public = [
        (name, member)
        for name, member in inspect.getmembers(ExperimentationClient, inspect.isfunction)
        if not name.startswith("_")
    ]
    assert public, "found no public method: the check is broken"
    asynchronous = [name for name, member in public if inspect.iscoroutinefunction(member)]
    assert not asynchronous, asynchronous


def test_assignment_and_flag_evaluation_are_frozen_dataclasses():
    for cls in (Assignment, FlagEvaluation):
        assert dataclasses.is_dataclass(cls)
        assert cls.__dataclass_params__.frozen, f"{cls.__name__} is not frozen"
