"""Small, dependency-light instrumentation shared by the API and worker."""

import threading
import time
from uuid import uuid4
from collections import defaultdict, deque

from fastapi import HTTPException
from prometheus_client import Counter, Gauge, Histogram
from redis import Redis
from redis.exceptions import RedisError

HTTP_REQUESTS = Counter("video_agent_http_requests_total", "HTTP requests", ["method", "path", "status"])
HTTP_SECONDS = Histogram("video_agent_http_request_duration_seconds", "HTTP request latency", ["method", "path"])
RATE_LIMITED = Counter("video_agent_rate_limited_total", "Rejected requests", ["scope"])
LLM_REQUESTS = Counter("video_agent_llm_requests_total", "LLM calls", ["outcome"])
ACTIVE_JOBS = Gauge("video_agent_active_jobs", "Queued and running jobs")
JOB_STAGES = Gauge("video_agent_jobs", "Jobs by state", ["status", "stage"])
METRICS_DB_REFRESH = Counter("video_agent_metrics_db_refresh_total", "Database metric refreshes", ["outcome"])
METRICS_DB_UP = Gauge("video_agent_metrics_database_up", "Whether the last metric database refresh succeeded")
QUEUE_WAIT_SECONDS = Histogram("video_agent_queue_wait_seconds", "Time from submission to processing start")
STAGE_SECONDS = Histogram("video_agent_parsing_stage_duration_seconds", "Parsing activity latency", ["stage", "outcome"])
PARSER_ATTEMPTS = Counter("video_agent_parser_attempts_total", "ASR/OCR activity attempts", ["parser"])
PARSER_FAILURES = Counter("video_agent_parser_failures_total", "ASR/OCR failed attempts", ["parser"])
PARSER_RETRIES = Counter("video_agent_parser_retries_total", "ASR/OCR retry attempts", ["parser"])
WEBHOOK_ATTEMPTS = Counter("video_agent_webhook_attempts_total", "Webhook delivery attempts", ["outcome"])
STORAGE_REQUESTS = Counter("video_agent_object_storage_requests_total", "Object storage operations", ["operation", "outcome"])
STORAGE_SECONDS = Histogram("video_agent_object_storage_request_duration_seconds", "Object storage operation latency", ["operation"])
DB_POOL_SIZE = Gauge("video_agent_database_pool_size", "Configured database connection pool size")
DB_POOL_CHECKED_OUT = Gauge("video_agent_database_pool_checked_out", "Checked out database connections")
DB_POOL_CHECKED_IN = Gauge("video_agent_database_pool_checked_in", "Idle database connections")
DB_POOL_OVERFLOW = Gauge("video_agent_database_pool_overflow", "Database pool overflow connections")


def observe_storage(operation: str, function, *args, **kwargs):
    started = time.perf_counter()
    try:
        result = function(*args, **kwargs)
    except Exception:
        STORAGE_REQUESTS.labels(operation, "failure").inc()
        raise
    else:
        STORAGE_REQUESTS.labels(operation, "success").inc()
        return result
    finally:
        STORAGE_SECONDS.labels(operation).observe(time.perf_counter() - started)


class SlidingWindowLimiter:
    """In-process implementation retained for isolated unit tests only."""
    def __init__(self):
        self._events = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, tenant: str, scope: str, limit: int, window_seconds: int = 60):
        if limit <= 0:
            return
        now = time.monotonic()
        key = (tenant, scope)
        with self._lock:
            events = self._events[key]
            while events and events[0] <= now - window_seconds:
                events.popleft()
            if len(events) >= limit:
                retry = max(1, int(events[0] + window_seconds - now) + 1)
                RATE_LIMITED.labels(scope).inc()
                raise HTTPException(429, f"{scope} rate limit exceeded",
                                    headers={"Retry-After": str(retry)})
            events.append(now)


class RedisSlidingWindowLimiter:
    """Atomic sliding-window limiter shared by every API process and replica."""

    _SCRIPT = """
local now = redis.call('TIME')
local now_us = now[1] * 1000000 + now[2]
local cutoff = now_us - (tonumber(ARGV[1]) * 1000000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', cutoff)
local count = redis.call('ZCARD', KEYS[1])
if count >= tonumber(ARGV[2]) then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  local retry_us = tonumber(oldest[2]) + tonumber(ARGV[1]) * 1000000 - now_us
  return {0, math.max(1, math.ceil(retry_us / 1000000))}
end
redis.call('ZADD', KEYS[1], now_us, ARGV[3])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]) + 1)
return {1, 0}
"""

    def __init__(self, url: str, key_prefix: str = "video-agent:rate"):
        self.client = Redis.from_url(url, decode_responses=True, socket_timeout=2,
                                     socket_connect_timeout=2)
        self.key_prefix = key_prefix
        self._check = self.client.register_script(self._SCRIPT)

    def check(self, tenant: str, scope: str, limit: int, window_seconds: int = 60):
        key = f"{self.key_prefix}:{scope}:{tenant}"
        try:
            allowed, retry = self._check(keys=[key], args=[window_seconds, limit, str(uuid4())])
        except RedisError as exc:
            # Failing open silently would turn a Redis outage into unlimited traffic.
            raise HTTPException(503, "rate limiter unavailable", headers={"Retry-After": "1"}) from exc
        if not allowed:
            RATE_LIMITED.labels(scope).inc()
            raise HTTPException(429, f"{scope} rate limit exceeded",
                                headers={"Retry-After": str(retry)})
