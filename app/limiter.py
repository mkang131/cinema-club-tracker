"""Process-local sliding-window limiter for the documented single-worker deployment."""

from collections import deque
from threading import Lock
from time import monotonic


class RateLimiter:
    def __init__(self):
        self._buckets: dict[str, tuple[float, deque]] = {}
        self._lock = Lock()
        self._last_cleanup = monotonic()

    def check(self, key: str, limit: int, window: float) -> bool:
        now = monotonic()
        with self._lock:
            if now - self._last_cleanup > 60:
                self._buckets = {
                    key: (period, timestamps)
                    for key, (period, timestamps) in self._buckets.items()
                    if timestamps and timestamps[-1] > now - period
                }
                self._last_cleanup = now
            _, timestamps = self._buckets.setdefault(key, (window, deque()))
            while timestamps and timestamps[0] <= now - window:
                timestamps.popleft()
            if len(timestamps) >= limit:
                return False
            timestamps.append(now)
            return True
