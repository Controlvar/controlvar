"""The documented dashboard build must build the documented image.

``frontend/Dockerfile``'s default profile flipped from ``full`` to ``core`` so
that a plain ``docker build`` of the API and of the dashboard produce a
matching pair.  Two places still told the reader the opposite -- and gave the
command for it, with no ``--build-arg`` -- so following the documentation
produced a core dashboard tagged ``:full``: a bundle with no module route in
it, serving the module nav against 404s.

The Dockerfile is the source of truth here; these checks read the default out
of it and require the prose to agree.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
DOCKERFILE = REPO_ROOT / "frontend" / "Dockerfile"
DOCS = (
    REPO_ROOT / "docs" / "getting-started" / "modules.md",
    REPO_ROOT / "frontend" / "README.md",
)

pytestmark = pytest.mark.skipif(
    not DOCKERFILE.is_file(), reason="this tree has no frontend/Dockerfile"
)


def _dockerfile_default_profile() -> str:
    """The value of the first top-level ``ARG EXPERIMENTLY_PROFILE=<x>``."""
    match = re.search(
        r"^ARG\s+EXPERIMENTLY_PROFILE=(\w+)", DOCKERFILE.read_text(), re.MULTILINE
    )
    assert match, "frontend/Dockerfile no longer declares a default profile"
    return match.group(1)


def _build_commands(document: Path) -> list[str]:
    """Every ``docker build -f frontend/Dockerfile …`` line in *document*."""
    return [
        line.strip()
        for line in document.read_text().splitlines()
        if "docker build" in line and "frontend/Dockerfile" in line
    ]


@pytest.mark.parametrize("document", DOCS, ids=lambda p: p.name)
class TestTheDocumentedDashboardBuild:
    def test_the_document_shows_both_builds(self, document):
        if not document.is_file():
            pytest.skip(f"this tree has no {document.name}")
        assert len(_build_commands(document)) >= 2

    @pytest.mark.regression
    def test_the_bare_build_is_tagged_with_the_dockerfile_default(self, document):
        """`docker build … .` with no --build-arg produces the default."""
        if not document.is_file():
            pytest.skip(f"this tree has no {document.name}")
        default = _dockerfile_default_profile()
        bare = [
            command
            for command in _build_commands(document)
            if "EXPERIMENTLY_PROFILE" not in command
        ]
        assert bare, "no unqualified docker build line to check"
        for command in bare:
            assert f"experimently-web:{default}" in command, (
                f"{document.name} tags a bare build differently from "
                f"frontend/Dockerfile's ARG EXPERIMENTLY_PROFILE={default}: "
                f"{command}"
            )

    @pytest.mark.regression
    def test_the_other_profile_is_built_with_a_build_arg(self, document):
        if not document.is_file():
            pytest.skip(f"this tree has no {document.name}")
        default = _dockerfile_default_profile()
        other = "full" if default == "core" else "core"
        qualified = [
            command
            for command in _build_commands(document)
            if f"experimently-web:{other}" in command
        ]
        assert qualified, f"{document.name} never builds the {other} dashboard"
        for command in qualified:
            assert f"--build-arg EXPERIMENTLY_PROFILE={other}" in command, (
                f"{document.name} builds experimently-web:{other} without "
                f"asking for it: {command}"
            )

    @pytest.mark.regression
    def test_the_prose_does_not_claim_the_wrong_default(self, document):
        """The line above the commands said "full is the default"."""
        if not document.is_file():
            pytest.skip(f"this tree has no {document.name}")
        default = _dockerfile_default_profile()
        other = "full" if default == "core" else "core"
        # Backticks are markup: `EXPERIMENTLY_PROFILE=core` is the default
        # says the same as the bare words.
        text = document.read_text().replace("`", "")
        assert f"EXPERIMENTLY_PROFILE={other} is the default" not in text
        assert f"the {other} profile by default" not in text


@pytest.mark.regression
def test_the_modules_page_names_the_dockerfile_default():
    """The modules page says which profile a plain dashboard build gives."""
    page = DOCS[0]
    if not page.is_file():
        pytest.skip(f"this tree has no {page.name}")
    text = page.read_text().replace("`", "")
    default = _dockerfile_default_profile()
    assert f"For the dashboard, EXPERIMENTLY_PROFILE={default} is the default" in text
