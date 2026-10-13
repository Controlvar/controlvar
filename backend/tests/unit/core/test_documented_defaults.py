"""The defaults the getting-started pages quote, read from the settings class.

The quick start, the architecture overview, the FAQ and the Limitations page
quote these numbers. Each test reads the class default (not a ``Settings()``
instance, which the environment can override), so it holds what a deployment
gets when it sets nothing. A page sentence quoting one is tied to the test by
its registry under ``docs/_claims/``.
"""

from __future__ import annotations

import pytest

from backend.app.core.config import Settings

pytestmark = [pytest.mark.unit, pytest.mark.regression]


def _default(name: str):
    return Settings.model_fields[name].default


def test_sign_in_is_local_by_default():
    assert _default("AUTH_PROVIDER") == "local"


def test_a_local_token_lasts_12_hours():
    assert _default("LOCAL_AUTH_TOKEN_TTL_MINUTES") == 12 * 60


def test_ten_failed_sign_ins_lock_an_account_for_15_minutes():
    assert _default("LOCAL_AUTH_MAX_FAILED_ATTEMPTS") == 10
    assert _default("LOCAL_AUTH_LOCKOUT_MINUTES") == 15


def test_two_settings_purge_the_platforms_own_history():
    assert _default("ANALYSIS_HISTORY_RETENTION_DAYS") == 90
    assert _default("SCHEDULER_HISTORY_RETENTION_DAYS") == 30


def test_the_compliance_reports_cover_365_and_730_days_by_default():
    assert _default("AUDIT_RETENTION_DAYS_SOC2") == 365
    assert _default("AUDIT_RETENTION_DAYS_ISO27001") == 730


def test_the_scheduler_cycles():
    assert _default("BANDIT_UPDATE_INTERVAL_MINUTES") == 5
    assert _default("ROLLOUT_CHECK_INTERVAL_MINUTES") == 15
    assert _default("EXPERIMENT_SCHEDULER_INTERVAL_MINUTES") == 15


def test_experiments_are_cached_for_an_hour():
    from backend.app.api.v1.endpoints.experiments import EXPERIMENT_CACHE_TTL_SECONDS

    assert EXPERIMENT_CACHE_TTL_SECONDS == 3600
