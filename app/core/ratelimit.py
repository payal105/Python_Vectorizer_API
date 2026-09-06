"""In-process sliding-window rate limiter.

Deliberately simple and process-local: it protects a single worker from a
runaway client. Swap :class:`RateLimiter` for a Redis-backed implementation
before running more than one worker behind a load balancer.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, limit_per_minute: int, window_seconds: float = 60.0) -> None:
        self.limit = limit_per_minute
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def check(self, key: str) -> tuple[bool, int, float]:
        """Record a hit for *key*.

        Returns ``(allowed, remaining, retry_after_seconds)``.
        """
        if not self.enabled:
            return True, -1, 0.0

        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            bucket = self._hits[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                retry_after = max(0.0, bucket[0] + self.window - now)
                return False, 0, retry_after
            bucket.append(now)
            return True, self.limit - len(bucket), 0.0

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)
