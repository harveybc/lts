"""
rate_limiter.py

Provides a minimal RateLimiter class for API rate limiting simulation.

:author: Your Name
:copyright: (c) 2025 Your Organization
:license: MIT
"""
from collections import defaultdict
import time

class RateLimiter:
    """
    Simulates API rate limiting and backoff.
    """
    def __init__(self, max_calls: int, period: float = 60.0):
        """
        Initialize the rate limiter.

        :param max_calls: Maximum allowed calls per user per period.
        :type max_calls: int
        :param period: Time window in seconds.
        :type period: float
        """
        self.max_calls = max_calls
        self.period = period
        self.calls = defaultdict(list)

    def is_allowed(self, user: str) -> bool:
        """
        Check if a user is allowed to make a call.

        :param user: User identifier.
        :type user: str
        :return: True if allowed, False if rate limited.
        :rtype: bool
        """
        now = time.time()
        calls = self.calls[user]
        # Remove calls outside the period
        self.calls[user] = [t for t in calls if now - t < self.period]
        if len(self.calls[user]) >= self.max_calls:
            return False
        self.calls[user].append(now)
        return True
