
from collections import deque
from math import ceil
from threading import Lock
from time import monotonic


class RateLimiter:
    def __init__(self, limit=20, window_seconds=60):
        self.limit = limit
        self.window_seconds = window_seconds
        self.requests = {}
        self.lock = Lock()

    def check(self, client_id: str) -> int | None:
        now = monotonic()

        with self.lock:
            timestamps = self.requests.setdefault(
                client_id, deque()
            )

            # Remove requests outside the time window.
            while timestamps and (
                now - timestamps[0] >= self.window_seconds
            ):
                timestamps.popleft()

            # Reject if the client exceeded its quota.
            if len(timestamps) >= self.limit:
                remaining = (
                    self.window_seconds
                    - (now - timestamps[0])
                )
                return max(1, ceil(remaining))

            timestamps.append(now)
            return None
