"""Redis-backed fixed-window rate limiting for production API requests."""

from functools import lru_cache
from hashlib import sha256

from redis import Redis
from redis.exceptions import RedisError


class RateLimiterUnavailable(Exception):
    """The rate limiter could not reach Redis."""


class RedisRateLimiter:
    _INCREMENT_SCRIPT = """
    local count = redis.call('INCR', KEYS[1])
    if count == 1 then
        redis.call('EXPIRE', KEYS[1], ARGV[1])
    end
    return {count, redis.call('TTL', KEYS[1])}
    """

    def __init__(self, redis_url: str) -> None:
        self._redis = Redis.from_url(redis_url, decode_responses=False)

    def allow(self, client_key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        key_digest = sha256(client_key.encode("utf-8")).hexdigest()
        try:
            count, ttl = self._redis.eval(
                self._INCREMENT_SCRIPT, 1, f"rate-limit:{key_digest}", window_seconds
            )
        except RedisError as error:
            raise RateLimiterUnavailable("The rate limiter is unavailable.") from error
        retry_after = max(int(ttl), 1)
        return int(count) <= limit, retry_after


@lru_cache
def get_rate_limiter(redis_url: str) -> RedisRateLimiter:
    return RedisRateLimiter(redis_url)
