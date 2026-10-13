"""What the provider package needs, and where its tests import it from.

``pyproject.toml`` is what pip reads when the provider is installed from a
clone; these hold it to the requirements the README states.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import experimentation
import experimentation_openfeature

PROVIDER_ROOT = Path(__file__).resolve().parents[1]
SDK_ROOT = PROVIDER_ROOT.parent / "python"


def project(root: Path) -> Dict[str, Any]:
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:  # pragma: no cover - 3.9/3.10: the stdlib has no TOML reader
        import tomli as tomllib  # type: ignore[no-redef]
    return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_the_package_is_a_0_x_beta():
    meta = project(PROVIDER_ROOT)
    assert meta["version"].split(".")[0] == "0", meta["version"]
    assert "Development Status :: 4 - Beta" in meta["classifiers"]


def test_it_needs_python_3_9_and_openfeature_sdk_0_10():
    meta = project(PROVIDER_ROOT)
    assert meta["requires-python"] == ">=3.9"
    assert "openfeature-sdk>=0.10.0" in meta["dependencies"]


def test_its_only_other_dependency_is_the_python_sdk_which_has_none():
    names = [d.split(">")[0].split("=")[0].split("<")[0].strip() for d in project(PROVIDER_ROOT)["dependencies"]]
    assert sorted(names) == ["experimently", "openfeature-sdk"]
    assert project(SDK_ROOT)["dependencies"] == []


def test_the_tests_import_both_packages_from_the_source_tree():
    assert Path(experimentation.__file__).resolve().is_relative_to(SDK_ROOT.resolve())
    assert Path(experimentation_openfeature.__file__).resolve().is_relative_to(
        (PROVIDER_ROOT / "src").resolve()
    )
