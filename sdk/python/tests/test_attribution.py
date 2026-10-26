"""The attributed keys behind the key-less ``track()`` fan-out: per user, at most
``MAX_ATTRIBUTED_USERS`` users, each key for ``ATTRIBUTION_HORIZON_SECONDS``."""

from __future__ import annotations

from unittest import mock

from controlvar.attribution import (
    ATTRIBUTION_HORIZON_SECONDS,
    LIFECYCLE_REASONS,
    MAX_ATTRIBUTED_USERS,
    AttributedKeys,
)


def test_the_bounds_are_10000_users_and_24_hours_and_four_lifecycle_reasons():
    assert MAX_ATTRIBUTED_USERS == 10_000
    assert ATTRIBUTION_HORIZON_SECONDS == 24 * 60 * 60
    assert sorted(LIFECYCLE_REASONS) == ["archived", "completed", "draft", "paused"]


def test_one_user_more_than_the_cap_drops_the_least_recently_answered():
    keys = AttributedKeys()
    for i in range(MAX_ATTRIBUTED_USERS + 1):
        keys.record_experiment(f"u-{i}", "checkout")

    assert len(keys) == MAX_ATTRIBUTED_USERS
    assert keys.keys("u-0") == ([], [])
    assert keys.keys("u-1") == (["checkout"], [])
    assert keys.keys(f"u-{MAX_ATTRIBUTED_USERS}") == (["checkout"], [])


def test_a_new_answer_makes_its_user_the_most_recently_answered():
    keys = AttributedKeys()
    for i in range(MAX_ATTRIBUTED_USERS):
        keys.record_experiment(f"u-{i}", "checkout")
    keys.record_flag("u-0", "new_search")  # u-0 answered again
    keys.record_experiment("u-new", "checkout")

    assert len(keys) == MAX_ATTRIBUTED_USERS
    assert keys.keys("u-0") == (["checkout"], ["new_search"])
    assert keys.keys("u-1") == ([], [])


def test_a_key_is_dropped_once_its_latest_answer_is_more_than_24_hours_old():
    keys = AttributedKeys()
    with mock.patch("controlvar.cache._now") as now:
        now.return_value = 0.0
        keys.record_experiment("u", "old")
        now.return_value = float(ATTRIBUTION_HORIZON_SECONDS)
        keys.record_experiment("u", "new")
        assert keys.keys("u") == (["old", "new"], [])

        now.return_value = ATTRIBUTION_HORIZON_SECONDS + 0.001
        assert keys.keys("u") == (["new"], [])
        now.return_value = 2 * ATTRIBUTION_HORIZON_SECONDS + 0.001
        assert keys.keys("u") == ([], [])
        assert len(keys) == 0


def test_the_24_hours_run_from_the_latest_answer_of_the_key():
    keys = AttributedKeys()
    with mock.patch("controlvar.cache._now") as now:
        now.return_value = 0.0
        keys.record_experiment("u", "checkout")
        now.return_value = 23 * 3600.0
        keys.record_experiment("u", "checkout")
        now.return_value = 25 * 3600.0
        assert keys.keys("u") == (["checkout"], [])


def test_forget_experiment_drops_that_experiment_only_and_the_user_once_nothing_is_left():
    keys = AttributedKeys()
    keys.record_experiment("u", "checkout")
    keys.record_experiment("u", "hero")
    keys.record_flag("u", "new_search")
    keys.forget_experiment("u", "checkout")
    assert keys.keys("u") == (["hero"], ["new_search"])

    keys.record_experiment("v", "checkout")
    keys.forget_experiment("v", "checkout")
    keys.forget_experiment("nobody", "checkout")
    assert len(keys) == 1


def test_one_users_keys_never_come_back_for_another():
    keys = AttributedKeys()
    keys.record_experiment("a", "checkout")
    keys.record_flag("a", "new_search")
    assert keys.keys("b") == ([], [])
