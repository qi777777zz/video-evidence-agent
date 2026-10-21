import json
from types import SimpleNamespace

from redis.exceptions import ConnectionError

from video_agent.conversation_store import ConversationContextCache


class Pipeline:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*args):
            self.calls.append((name, args))
            return self
        return call


def test_hot_context_is_read_from_redis_without_mysql(monkeypatch):
    cache = ConversationContextCache("redis://localhost:6379/15")
    monkeypatch.setattr(cache.client, "lrange", lambda *_: [json.dumps({"role": "user", "content": "上一个问题"})])
    monkeypatch.setattr(cache.client, "expire", lambda *_: True)

    class NoDatabase:
        def scalars(self, _statement):
            raise AssertionError("hot cache should avoid a database read")

    assert cache.load(NoDatabase(), "conversation-1") == [{"role": "user", "content": "上一个问题"}]


def test_cache_failure_falls_back_to_durable_messages(monkeypatch):
    cache = ConversationContextCache("redis://localhost:6379/15")
    monkeypatch.setattr(cache.client, "lrange", lambda *_: (_ for _ in ()).throw(ConnectionError()))
    monkeypatch.setattr(cache, "replace", lambda *_: None)
    rows = [SimpleNamespace(role="assistant", content="较新的回答"),
            SimpleNamespace(role="user", content="较早的问题")]

    class Database:
        def scalars(self, _statement):
            return rows

    assert cache.load(Database(), "conversation-1") == [
        {"role": "user", "content": "较早的问题"},
        {"role": "assistant", "content": "较新的回答"},
    ]


def test_append_bounds_context_and_refreshes_ttl(monkeypatch):
    cache = ConversationContextCache("redis://localhost:6379/15")
    pipeline = Pipeline()
    monkeypatch.setattr(cache.client, "pipeline", lambda **_: pipeline)
    cache.append("conversation-1", [{"role": "user", "content": "问题"}])

    names = [name for name, _args in pipeline.calls]
    assert names == ["rpush", "ltrim", "expire", "execute"]
