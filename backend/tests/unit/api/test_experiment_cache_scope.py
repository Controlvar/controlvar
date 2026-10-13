"""What the experiment cache keeps, for how long, and what a change clears.

With ``CACHE_ENABLED`` on, an experiment's detail, daily results and segmented
results are written to Redis for ``EXPERIMENT_CACHE_TTL_SECONDS``. A change to
the experiment drops its detail and every ``experiments:*`` key; the daily and
segmented results are keyed otherwise and stay until they expire. These run
the module's own helpers against an in-memory stand-in for the async Redis
client; ``backend/tests/integration/api/test_experiment_cache_redis.py`` holds
that the routes read and write these keys.
"""

from __future__ import annotations

import asyncio
import fnmatch
import uuid
from typing import Dict, List, Tuple

import pytest

from backend.app.api import deps
from backend.app.api.v1.endpoints import experiments
from backend.app.core.config import DevSettings, ProdSettings, TestSettings

pytestmark = [pytest.mark.unit]


class _Redis:
    """The four calls the helpers make, on a dict."""

    def __init__(self) -> None:
        self.store: Dict[str, str] = {}
        self.ttls: List[Tuple[str, int]] = []

    async def get(self, key: str):
        return self.store.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.ttls.append((key, ttl))
        self.store[key] = value

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.store.pop(key, None)

    async def scan_iter(self, match: str):
        for key in list(self.store):
            if fnmatch.fnmatchcase(key, match):
                yield key


def _control(redis: _Redis, enabled: bool = True) -> deps.CacheControl:
    return deps.CacheControl(enabled=enabled, redis=redis)


def test_an_entry_is_kept_for_an_hour():
    assert experiments.EXPERIMENT_CACHE_TTL_SECONDS == 3600
    redis = _Redis()
    asyncio.run(experiments._cache_set(_control(redis), "experiment:x", {"a": 1}))
    assert redis.ttls == [("experiment:x", 3600)]


@pytest.mark.parametrize("settings_class", [DevSettings, TestSettings, ProdSettings])
def test_the_cache_is_off_by_default(settings_class):
    assert settings_class.model_fields["CACHE_ENABLED"].default is False


def test_with_the_cache_off_nothing_is_written_or_read():
    redis = _Redis()
    redis.store["experiment:x"] = '{"a": 1}'
    control = _control(redis, enabled=False)
    asyncio.run(experiments._cache_set(control, "experiment:y", {"a": 1}))
    assert redis.ttls == []
    assert asyncio.run(experiments._cache_get(control, "experiment:x")) is None


def test_a_change_clears_the_detail_and_leaves_the_daily_and_segmented_results():
    experiment_id = uuid.uuid4()
    redis = _Redis()
    detail = f"experiment:{experiment_id}"
    daily = f"experiment_daily_results:{experiment_id}"
    segmented = f"experiment_segmented_results:{experiment_id}:country"
    for key in (detail, daily, segmented):
        redis.store[key] = "{}"
    asyncio.run(
        experiments._invalidate_experiment_cache(_control(redis), experiment_id)
    )
    assert detail not in redis.store
    assert daily in redis.store and segmented in redis.store
