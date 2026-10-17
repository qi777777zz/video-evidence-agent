import pytest
from fastapi import HTTPException
from redis.exceptions import ConnectionError

from video_agent.telemetry import RedisSlidingWindowLimiter, SlidingWindowLimiter


def test_limiter_rejects_and_sets_retry_after():
    limiter = SlidingWindowLimiter()
    limiter.check("tenant", "question", 1)
    with pytest.raises(HTTPException) as error:
        limiter.check("tenant", "question", 1)
    assert error.value.status_code == 429
    assert int(error.value.headers["Retry-After"]) >= 1


def test_disabled_limiter_allows_requests():
    limiter = SlidingWindowLimiter()
    for _ in range(100):
        limiter.check("tenant", "upload", 0)


def test_redis_limiter_uses_tenant_and_scope_key():
    limiter = RedisSlidingWindowLimiter("redis://localhost:6379/15", "test-rate")
    called = {}

    def check(*, keys, args):
        called["keys"], called["args"] = keys, args
        return [1, 0]

    limiter._check = check
    limiter.check("tenant-a", "question", 12, window_seconds=30)

    assert called["keys"] == ["test-rate:question:tenant-a"]
    assert called["args"][:2] == [30, 12]


def test_redis_limiter_rejects_with_shared_retry_after():
    limiter = RedisSlidingWindowLimiter("redis://localhost:6379/15")
    limiter._check = lambda **_: [0, 17]

    with pytest.raises(HTTPException) as error:
        limiter.check("tenant", "submit", 10)

    assert error.value.status_code == 429
    assert error.value.headers["Retry-After"] == "17"


def test_redis_limiter_fails_closed_when_redis_is_unavailable():
    limiter = RedisSlidingWindowLimiter("redis://localhost:6379/15")

    def unavailable(**_):
        raise ConnectionError("offline")

    limiter._check = unavailable
    with pytest.raises(HTTPException) as error:
        limiter.check("tenant", "upload", 30)

    assert error.value.status_code == 503
