"""Fixtures for the showcase tests."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _process_environment_is_restored() -> Iterator[None]:
    """Hand ``os.environ`` to the next test as this one found it.

    ``showcase.capture.run.main`` and ``showcase.voice.step.main`` cut the
    process's own environment to the tool's allow-list when they start, so a
    test that calls one in this process would otherwise leave the rest of the
    session without ``APP_ENV``, the dummy cloud settings and the rest.
    """
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def _home_is_a_scratch_directory(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every showcase test runs with ``HOME`` in pytest's own temporary directory.

    The voice step's cache lives under ``HOME`` by default, and an entry there
    is never overwritten: a test run on a developer's machine must not be able
    to write one (``test_showcase_voice.py`` asserts where the default root
    resolves). Voice tests point ``HOME`` under their own ``tmp_path`` as well.
    """
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
