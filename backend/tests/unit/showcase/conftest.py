"""Fixtures for the showcase tests."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _process_environment_is_restored() -> Iterator[None]:
    """Hand ``os.environ`` to the next test as this one found it.

    ``showcase.capture.run.main`` cuts the process's own environment to the
    tool's allow-list when it starts, so a test that calls it in this process
    would otherwise leave the rest of the session without ``APP_ENV``, the
    dummy cloud settings and the rest.
    """
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
