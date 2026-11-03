"""What every file of the live replay shares (#1544): one seed, one sign-in,
one API key and one step summary per run.

``test_replay.py`` (the clean scenarios) and ``test_faults.py`` (the fault
scenarios) both use the ``live`` driver below, so a run signs in once and
creates one key, whichever files it collects, and the seed is announced
before any test (``ground_truth.seed`` resolves it once for both files). The
helpers these fixtures call stay in ``test_replay.py``, where the unit gates
(``backend/tests/unit/ground_truth/test_replay_gates.py``) import them.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import driver
import httpx
import pytest
from ground_truth.seed import SEED
from test_replay import (
    API_URL,
    SCALE,
    _mask,
    _say,
    credentials,
    start_session,
    summary_text,
)


@pytest.fixture(scope="session", autouse=True)
def _announce_the_seed(pytestconfig: pytest.Config) -> None:
    _say(
        pytestconfig,
        f"ground-truth replay: seed {SEED}, scale {SCALE}; GROUND_TRUTH_SEED={SEED} reproduces it",
    )


@pytest.fixture(scope="session")
def live(pytestconfig: pytest.Config) -> Iterator[driver.Driver]:
    """The signed-in driver, with its API key; the step summary at the end."""
    email, password = credentials(os.environ)
    with httpx.Client(base_url=API_URL, timeout=driver.TIMEOUT) as client:
        session = driver.Driver(client)
        try:
            start_session(
                session,
                email,
                password,
                mask=lambda value: _mask(pytestconfig, value),
            )
            yield session
        finally:
            text = summary_text(session)
            for line in text.splitlines():
                _say(pytestconfig, line)
            target = os.environ.get("GITHUB_STEP_SUMMARY")
            if target:
                with open(target, "a", encoding="utf-8") as stream:
                    stream.write(text)
