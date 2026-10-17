"""The version the provider reports is the version it is published as.

``pyproject.toml`` is what the build stamps on the wheel and what
``scripts/check_sdk_version.py`` compares a release tag against.
``controlvar_openfeature.__version__`` is a second, hand-written copy.
Nothing derives one from the other, so this test is what keeps them equal
(the same pin ``sdk/python/tests/test_version.py`` holds for the SDK, #688).
"""

import re
from pathlib import Path

from controlvar_openfeature import __version__

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _pyproject_version() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:  # pragma: no cover - 3.9/3.10
        # The [project] table's static ``version = "..."`` line. Anchored to
        # the start of a line so a dependency pin cannot match it.
        project = text.split("[project]", 1)[1].split("\n[", 1)[0]
        match = re.search(r'^version\s*=\s*"([^"]+)"', project, re.MULTILINE)
        assert match, "pyproject.toml [project] declares no static version"
        return match.group(1)
    return tomllib.loads(text)["project"]["version"]


def test_dunder_version_matches_pyproject() -> None:
    assert __version__ == _pyproject_version()
