"""
Simple in-memory TTL cache for Odoo tool results.

Thread-safe via GIL for single-process deployment.
No external dependencies.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional


class TTLCache:
    """Dict-based TTL cache with prefix-based invalidation."""

    def __init__(self) -> None:
        self._store: Dict[str, tuple[Any, float]] = {}

    def get(self, key: str) -> Optional[Any]:
        """Get value if not expired. Returns None if missing or expired."""
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if time.time() > expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        """Set value with TTL in seconds."""
        self._store[key] = (value, time.time() + ttl_seconds)

    def invalidate(self, prefix: str) -> int:
        """Remove all entries whose key starts with prefix. Returns count removed."""
        keys_to_remove = [k for k in self._store if k.startswith(prefix)]
        for k in keys_to_remove:
            del self._store[k]
        return len(keys_to_remove)

    def clear(self) -> None:
        """Remove all entries."""
        self._store.clear()

    def cleanup_expired(self) -> int:
        """Remove all expired entries. Returns count removed."""
        now = time.time()
        keys_to_remove = [k for k, (_, exp) in self._store.items() if now > exp]
        for k in keys_to_remove:
            del self._store[k]
        return len(keys_to_remove)
