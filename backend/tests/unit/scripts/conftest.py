"""Fixtures for the tests of ``scripts/``.

``scripts/qa_render.py`` accepts a run link only of the repository named in
``GITHUB_REPOSITORY``, which Actions sets in every step. These tests write
their run links with :data:`RUN_REPOSITORY`, so it is set here for every test:
without it the links would pass in CI and fail on a laptop, and after a rename
of the repository they would fail in CI instead.
"""

import pytest

#: The repository the tests' run links name.
RUN_REPOSITORY = "Controlvar/controlvar"


@pytest.fixture(autouse=True)
def run_repository(monkeypatch):
    """``GITHUB_REPOSITORY`` as an Actions run of :data:`RUN_REPOSITORY` sets it."""
    monkeypatch.setenv("GITHUB_REPOSITORY", RUN_REPOSITORY)
    return RUN_REPOSITORY
