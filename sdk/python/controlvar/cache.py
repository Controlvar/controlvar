"""Thread-safe per-user, per-key TTL cache. Only successful results are ever stored."""

from __future__ import annotations

import threading
import time
from typing import Dict, Generic, List, Optional, Tuple, TypeVar

T = TypeVar("T")

# Indirection so tests can patch ``controlvar.cache._now``.
_now = time.monotonic


class UserKeyCache(Generic[T]):
    """``user_id -> key -> (expires_at, value)`` guarded by a single lock."""

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = float(ttl_seconds)
        self._lock = threading.Lock()
        self._users: Dict[str, Dict[str, Tuple[float, T]]] = {}

    def get(self, user_id: str, key: str) -> Optional[T]:
        with self._lock:
            by_key = self._users.get(user_id)
            if not by_key:
                return None
            entry = by_key.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if _now() >= expires_at:
                del by_key[key]
                return None
            return value

    def set(self, user_id: str, key: str, value: T) -> None:
        with self._lock:
            self._users.setdefault(user_id, {})[key] = (_now() + self._ttl, value)

    def entries(self, user_id: str) -> List[Tuple[str, T]]:
        """All live (unexpired) entries for a user, in insertion order."""
        with self._lock:
            by_key = self._users.get(user_id)
            if not by_key:
                return []
            now = _now()
            live: List[Tuple[str, T]] = []
            for key, (expires_at, value) in list(by_key.items()):
                if now >= expires_at:
                    del by_key[key]
                else:
                    live.append((key, value))
            return live

    def clear(self) -> None:
        with self._lock:
            self._users.clear()
