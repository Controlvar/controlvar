"""
Shared identity for the statistics engine: version and seeded randomness.

Every Monte Carlo computation on the platform (Bayesian PtBB / expected loss /
ROPE, Thompson-sampling bandit weights) draws from
``numpy.random.default_rng(seed)`` where ``seed`` is a deterministic function
of the inputs, so two calls with identical inputs return byte-identical
sample arrays and can be audited later.

Seed derivation
---------------
``seed = blake2b(f"{experiment_id}|{as_of_bucket}|{n_samples}", digest_size=8)``
interpreted as a big-endian unsigned integer with the top bit cleared
(63 bits), so it is stable across processes/machines, fits a signed
PostgreSQL ``BIGINT`` and never overflows ``numpy`` seeding.

``as_of_bucket`` is the UTC calendar day (``YYYY-MM-DD``) of the analysis.
The day is the coarsest bucket that is still meaningful for a daily-updated
posterior: every call made on the same day for the same experiment and
sample count reuses the same seed, so dashboards, API consumers and the
persisted ``analysis_snapshots`` agree.

This module lives in ``core`` (not ``services``) so that Pydantic schemas can
import ``ENGINE_VERSION`` without creating an import cycle through
``backend.app.services``.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from typing import Optional, Union

import numpy as np

#: Bumped whenever a change alters numbers a client could have persisted
#: (sampling strategy, estimator, default priors, thresholds).  1.5.0: the
#: winner, the recommendation and the Bayesian decision read the metric's
#: ``lower_is_better``, and ``/results`` analyses a revenue metric as its mean
#: per assigned user.
#: 1.6.0 (#1385): a bandit whose primary metric is lower-is-better counts a
#: user who did not convert as the success, so its weights, regret estimate
#: and history alpha/beta favour the variant with fewer conversions; the
#: stored and reported counts stay the converting users.
#:
#: The rule: an engine-changing PR bumps ``ENGINE_VERSION`` to the next minor
#: and appends it to ``RELEASED_ENGINE_VERSIONS`` ("frozen on main", in
#: ``backend/tests/unit/services/test_sufficient_stats_fingerprint.py``) in the
#: same change.  A frozen version's entries are never edited, and no key is
#: ever added under one.  A PR is engine-changing when, for the same database
#: rows and request, it adds, removes or changes any number, decision (winner,
#: recommendation, Bayesian decision, ``can_stop``) or allocation weight that a
#: path stamping ``engine_version`` returns, caches or persists: results and
#: its snapshots, Bayesian, CUPED, sequential, and the bandit's state, history
#: and status.  A revert of an engine-changing PR bumps again.
ENGINE_VERSION: str = "1.6.0"

#: Mask that keeps the derived seed inside the signed 64-bit range.
_SEED_MASK = (1 << 63) - 1


def as_of_bucket(as_of: Optional[Union[datetime, date]] = None) -> str:
    """Return the reproducibility bucket for an analysis time.

    Args:
        as_of: Time the analysis is "as of".  Naive datetimes are assumed
            UTC; aware datetimes are converted to UTC.  ``None`` means now.

    Returns:
        ISO calendar day, e.g. ``"2026-09-11"``.
    """
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    if isinstance(as_of, datetime):
        if as_of.tzinfo is not None:
            as_of = as_of.astimezone(timezone.utc)
        return as_of.date().isoformat()
    return as_of.isoformat()


def derive_seed(experiment_id: object, bucket: str, n_samples: int) -> int:
    """Derive the deterministic RNG seed for a Monte Carlo computation.

    Args:
        experiment_id: Experiment identifier (UUID or string); ``str()`` is used.
        bucket: Reproducibility bucket from :func:`as_of_bucket`.
        n_samples: Number of Monte Carlo samples the computation will draw.

    Returns:
        A non-negative integer below 2**63.
    """
    key = f"{experiment_id}|{bucket}|{int(n_samples)}".encode("utf-8")
    digest = hashlib.blake2b(key, digest_size=8).digest()
    return int.from_bytes(digest, "big") & _SEED_MASK


def make_rng(seed: int) -> np.random.Generator:
    """Return the platform's RNG for a seed (always ``default_rng``)."""
    return np.random.default_rng(int(seed))
