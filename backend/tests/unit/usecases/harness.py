"""Shared by the use-case harness's unit gates: the harness on ``sys.path``, a
fake upstream to put behind the proxy, and a fake clock.

The harness (``tests/usecases/usecases``) and the ground-truth driver it
signs in with (``tests/ground_truth/driver.py``) are imported by putting their
roots on ``sys.path``, as ``backend/tests/unit/ground_truth/library.py`` does,
and the tests take the modules from here, so the path is set before anything
imports them.

The fake upstream is the harness's own (``usecases/fake_upstream.py``): the JS
driver's gates, which need Node and so run beside the cases, use the same one.

Every server here listens on 127.0.0.1, on a port the system picks, and is
stopped by the fixture that made it. No database, no network beyond loopback.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
USECASES_ROOT = REPO_ROOT / "tests" / "usecases"
GROUND_TRUTH_ROOT = REPO_ROOT / "tests" / "ground_truth"
PYTHON_DRIVER = USECASES_ROOT / "drivers" / "python_driver.py"
SDK_ROOT = REPO_ROOT / "sdk" / "python"

for _root in (USECASES_ROOT, GROUND_TRUTH_ROOT):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

from usecases import admin, gates, proxy, runner, sdks
from usecases.fake_upstream import FLAG_KEY, Received, Upstream, closed_port

__all__ = [
    "FLAG_KEY",
    "GROUND_TRUTH_ROOT",
    "PYTHON_DRIVER",
    "REPO_ROOT",
    "SDK_ROOT",
    "USECASES_ROOT",
    "Clock",
    "Received",
    "Upstream",
    "admin",
    "closed_port",
    "gates",
    "proxy",
    "runner",
    "sdks",
]


class Clock:
    """A clock that moves only when slept on."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
