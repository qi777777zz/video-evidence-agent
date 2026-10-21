"""Durable conversation messages with a disposable Redis hot-context cache."""

import json
import logging

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import or_, select

from .config import settings
from .db import ConversationSummary, Message

logger = logging.getLogger("video_agent.conversations")


def estimate_tokens(value: str) -> int:
    """Conservative tokenizer-independent estimate suitable for hard budgets."""
    return max(1, (len(value.encode("utf-8")) + 2) // 3)


def trim_to_tokens(value: str, budget: int) -> str:
    if budget <= 0:
        return ""
    if estimate_tokens(value) <= budget:
        return value
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(value[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    return value[:low]


def split_recent(messages: list[dict], token_budget: int) -> tuple[list[dict], list[dict]]:
    """Return (old messages to summarize, newest messages kept verbatim)."""
    used, split_at = 0, len(messages)
    for index in range(len(messages) - 1, -1, -1):
        cost = estimate_tokens(messages[index].get("content", "")) + 4
        if used + cost > token_budget:
            break
        used += cost
        split_at = index
    return messages[:split_at], messages[split_at:]


class ConversationContextCache:
    def __init__(self, url: str = settings.redis_url):
        self.client = Redis.from_url(url, decode_responses=True)

    def _key(self, conversation_id: str) -> str:
        return f"{settings.conversation_cache_key_prefix}:{conversation_id}"

    def load(self, session, conversation_id: str,
             summary: ConversationSummary | None = None) -> list[dict]:
        key = self._key(conversation_id)
        try:
            cached = self.client.lrange(key, 0, -1)
            if cached:
                self.client.expire(key, settings.conversation_cache_ttl_seconds)
                return [json.loads(item) for item in cached]
        except (RedisError, json.JSONDecodeError):
            logger.warning("conversation cache read failed", exc_info=True)

        limit = settings.conversation_context_messages
        statement = select(Message).where(Message.conversation_id == conversation_id)
        if summary:
            boundary = session.get(Message, summary.through_message_id)
            if boundary:
                statement = statement.where(or_(
                    Message.created_at > boundary.created_at,
                    (Message.created_at == boundary.created_at) & (Message.id > boundary.id),
                ))
        rows = list(session.scalars(statement.order_by(
            Message.created_at.desc(), Message.id.desc()).limit(limit)))
        context = [{"role": row.role, "content": row.content} for row in reversed(rows)]
        self.replace(conversation_id, context)
        return context

    def load_summary(self, session, conversation_id: str) -> ConversationSummary | None:
        return session.scalar(select(ConversationSummary).where(
            ConversationSummary.conversation_id == conversation_id))

    def replace(self, conversation_id: str, messages: list[dict]) -> None:
        key = self._key(conversation_id)
        try:
            pipeline = self.client.pipeline(transaction=True)
            pipeline.delete(key)
            if messages:
                pipeline.rpush(key, *(json.dumps(item, ensure_ascii=False) for item in messages))
                pipeline.ltrim(key, -settings.conversation_context_messages, -1)
                pipeline.expire(key, settings.conversation_cache_ttl_seconds)
            pipeline.execute()
        except RedisError:
            logger.warning("conversation cache write failed", exc_info=True)

    def append(self, conversation_id: str, messages: list[dict]) -> None:
        if not messages:
            return
        key = self._key(conversation_id)
        try:
            pipeline = self.client.pipeline(transaction=True)
            pipeline.rpush(key, *(json.dumps(item, ensure_ascii=False) for item in messages))
            pipeline.ltrim(key, -settings.conversation_context_messages, -1)
            pipeline.expire(key, settings.conversation_cache_ttl_seconds)
            pipeline.execute()
        except RedisError:
            logger.warning("conversation cache append failed", exc_info=True)


conversation_context_cache = ConversationContextCache()
