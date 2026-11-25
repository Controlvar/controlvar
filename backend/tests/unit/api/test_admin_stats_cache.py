"""Regression: GET /api/v1/admin/stats raised NameError when Redis caching was on.

`admin.py` used `json.loads`/`json.dumps` on the cache path without importing
`json`, so the endpoint worked only when caching was disabled — which is what
every existing test did. ruff's F821 found it.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.regression
@pytest.mark.asyncio
async def test_admin_stats_cache_hit_returns_cached_payload():
    from backend.app.api.deps import CacheControl
    from backend.app.api.v1.endpoints import admin

    payload = {"total_users": 7, "total_experiments": 3}
    redis = MagicMock()
    redis.get = AsyncMock(return_value=json.dumps(payload))
    cache = CacheControl(enabled=True, skip=False, redis=redis)

    result = await admin.get_system_stats(
        db=MagicMock(), current_user=MagicMock(), cache_control=cache
    )
    assert result == payload


@pytest.mark.regression
@pytest.mark.asyncio
async def test_admin_stats_cache_miss_writes_the_cache():
    assert False, "planted: a failing unit test in shard 4"
    from backend.app.api.deps import CacheControl
    from backend.app.api.v1.endpoints import admin

    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    db = MagicMock()
    db.query.return_value.filter.return_value.count.return_value = 1
    db.query.return_value.count.return_value = 2
    cache = CacheControl(enabled=True, skip=False, redis=redis)

    stats = await admin.get_system_stats(
        db=db, current_user=MagicMock(), cache_control=cache
    )

    assert isinstance(stats, dict) and stats
    redis.setex.assert_awaited_once()
    # The cached value must be JSON the read path can load back.
    cached = redis.setex.await_args.args[-1]
    assert json.loads(cached) == stats


@pytest.mark.regression
@pytest.mark.asyncio
async def test_clear_cache_scans_the_namespaces_the_app_actually_writes():
    """`settings.REDIS_PREFIX` does not exist: the endpoint used to raise."""
    from backend.app.api.deps import CacheControl
    from backend.app.api.v1.endpoints import admin

    scanned: list[str] = []
    deleted: list[str] = []

    class _Redis:
        def scan_iter(self, match: str):
            scanned.append(match)

            async def _gen():
                # One key per namespace, so the count is checkable.
                yield match.replace("*", "one")

            return _gen()

        async def delete(self, key):
            deleted.append(key)

    cache = CacheControl(enabled=True, skip=False, redis=_Redis())
    result = await admin.clear_cache(current_user=MagicMock(), cache_control=cache)

    assert result["keys_deleted"] == len(admin.CACHE_NAMESPACES)
    assert scanned == [f"{ns}:*" for ns in admin.CACHE_NAMESPACES]
    # The namespaces have to match what the app writes, or a cleared cache
    # is not cleared.
    assert "admin:*" in scanned and "experiment:*" in scanned
    # The flag routes no longer cache (#630), so nothing writes flag keys.
    assert "feature_flag:*" not in scanned and "feature_flags:*" not in scanned


@pytest.mark.asyncio
async def test_clear_cache_is_a_no_op_when_caching_is_off():
    from backend.app.api.deps import CacheControl
    from backend.app.api.v1.endpoints import admin

    result = await admin.clear_cache(
        current_user=MagicMock(),
        cache_control=CacheControl(enabled=False, skip=False, redis=None),
    )
    assert result == {"message": "Caching is not enabled"}
