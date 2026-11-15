"""The experiment detail's cache key, and dropping it after a change.

With ``CACHE_ENABLED`` on, ``GET /api/v1/experiments/{experiment_id}`` keeps
its answer in Redis for an hour (``backend/app/api/v1/endpoints/experiments.py``).
A writer that changes what that answer shows drops the entry once its change
is committed, so the next read shows the change: the experiment routes, adding
an experiment to an exclusion group or removing it from one, and the experiment
scheduler starting, resuming or completing an experiment.

The route that reads and writes the entry, and every writer, build the key with
``experiment_detail_cache_key``, called through this module, never a copy of
the string: a key typed out anywhere else would drop an entry nobody reads,
and the page would show the old state for an hour.
"""

from __future__ import annotations

from typing import Any, Iterable

from backend.app.api import deps
from backend.app.core.logging import get_logger

logger = get_logger(__name__)


def experiment_detail_cache_key(experiment_id: Any) -> str:
    """The Redis key of ``GET /api/v1/experiments/{experiment_id}``'s answer."""
    return f"experiment:{experiment_id}"


async def drop_cached_experiment_details(experiment_ids: Iterable[Any]) -> None:
    """Drop the cached detail of each of ``experiment_ids``.

    For a writer that has no cache dependency of its own: the exclusion-group
    routes and the experiment scheduler. It asks for the cache as a request
    does (``deps.get_cache_control``), so with ``CACHE_ENABLED`` off (the
    default) it sends nothing to Redis, and with it on it uses the shared pool.
    Called after the change is committed, so a Redis error is logged rather
    than raised, as the experiment routes do: the change was made and must
    not be reported as a failure.
    """
    keys = [
        experiment_detail_cache_key(experiment_id) for experiment_id in experiment_ids
    ]
    if not keys:
        return
    try:
        cache_control = await deps.get_cache_control()
        if not (cache_control.enabled and cache_control.redis):
            return
        await cache_control.redis.delete(*keys)
    except Exception as cache_error:
        logger.warning("Experiment cache invalidation failed: %s", cache_error)
