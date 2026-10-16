"""The version the package reports is the version it is published as.

``pyproject.toml`` is what the build stamps on the wheel and what
``scripts/check_sdk_version.py`` compares a release tag against.
``controlvar/version.py`` is what the client sends in its User-Agent and
what ``controlvar.__version__`` returns. Nothing derives one from the
other, so this test is what keeps them equal.
"""

import re
from pathlib import Path

from controlvar import __version__
from controlvar.client import _USER_AGENT

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


def test_version_py_matches_pyproject() -> None:
    assert __version__ == _pyproject_version()


def test_user_agent_carries_the_published_version() -> None:
    assert _USER_AGENT == f"controlvar-python/{_pyproject_version()}"
