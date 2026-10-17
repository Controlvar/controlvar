# SPDX-FileCopyrightText: 2026 The Controlvar authors
# SPDX-License-Identifier: Apache-2.0
"""The seeds are development-only.

``seed_demo_data.py``, ``seed_shoplab.py`` and ``seed_streampulse.py`` fill a
database with sample users, experiments and flags for the demo apps, and
``seed_sdk_contract.py`` creates the demo accounts, the fixtures and the API
keys the SDK contract suites assert on. They run when ``ENVIRONMENT`` is
``development`` or ``test`` and stop at once, before touching the database,
anywhere else. ``backend/docker-entrypoint.sh`` makes the same check on
``SEED=`` before it waits for the database; this is the same rule for someone
who runs a script by hand.
"""

from __future__ import annotations

import os
import sys

from backend.app.core.config import (
    BYPASS_ALLOWED_ENVIRONMENTS,
    resolve_environment_from_process_env,
)

#: Exit status for a refused seed (EX_CONFIG): a configuration problem, not a crash.
REFUSED_EXIT_CODE = 78


def refusal_message(seed: str, environment: str) -> str:
    """The one line printed when *seed* is refused under *environment*."""
    return (
        f"[seed] the '{seed}' seed is for development and is refused when "
        f"ENVIRONMENT={environment}. Run it against a development or test environment."
    )


def require_development_environment(seed: str) -> None:
    """Exit with :data:`REFUSED_EXIT_CODE` unless the environment is development or test.

    The environment is resolved the way the application resolves it
    (``ENVIRONMENT`` wins, then the legacy ``APP_ENV``, then ``development``),
    so an unrecognised name is refused rather than treated as development.
    """
    environment = resolve_environment_from_process_env()
    if environment in BYPASS_ALLOWED_ENVIRONMENTS:
        return
    shown = os.environ.get("ENVIRONMENT") or os.environ.get("APP_ENV") or environment
    print(refusal_message(seed, shown), file=sys.stderr)
    raise SystemExit(REFUSED_EXIT_CODE)
