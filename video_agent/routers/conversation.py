import json
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from .. import storage
from ..agent import answer_candidates, summarize_history
from ..auth import require_scopes
from ..config import settings
from ..conversation_store import (conversation_context_cache, estimate_tokens,
                                  split_recent)
from ..db import Conversation, ConversationSummary, Message, Session, Video
from ..retrieval import search, simplified, verify_evidence

router = APIRouter(prefix="/videos", tags=["Conversation"])
logger = logging.getLogger("video_agent.conversation")


class Question(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    conversation_id: str | None = Field(default=None, min_length=36, max_length=36)


@router.post("/{video_id}/questions")
async def ask(video_id: str, request: Question,
              tenant_id: str = Depends(require_scopes("questions:ask"))):
    from ..api import limiter
    limiter.check(tenant_id, "question", settings.question_rate_per_minute)
    with Session.begin() as session:
        video = session.get(Video, video_id)
        if not video or video.tenant_id != tenant_id:
            raise HTTPException(404, "video not found")
        if video.status not in {"ready", "complete"}:
            raise HTTPException(409, "video not ready")
        conversation = session.get(Conversation, request.conversation_id) if request.conversation_id else None
        if conversation and (conversation.tenant_id != tenant_id or conversation.video_id != video_id):
            raise HTTPException(404, "conversation not found")
        if request.conversation_id and not conversation:
            raise HTTPException(404, "conversation not found")
        if not conversation:
            conversation = Conversation(tenant_id=tenant_id, video_id=video_id,
                                        title=request.question[:255])
            session.add(conversation)
            session.flush()
        history = conversation_context_cache.load(session, conversation.id)
        stored_summary = conversation_context_cache.load_summary(session, conversation.id)
        summary_text = stored_summary.content if stored_summary else None
        candidates = verify_evidence(video, search(session, video.id, request.question))[
            :settings.max_evidence_count]
        object_key, original_deleted = video.object_key, video.original_deleted_at is not None
        conversation_id = conversation.id
    result = await answer_candidates(candidates, request.question, history, summary_text)
    result["answer"] = simplified(result["answer"])
    for citation in result["citations"]:
        citation["text"] = simplified(citation["text"])
    persisted = [{"role": "user", "content": request.question},
                 {"role": "assistant", "content": result["answer"]}]
    with Session.begin() as session:
        conversation = session.get(Conversation, conversation_id)
        if not conversation or conversation.tenant_id != tenant_id:
            raise HTTPException(409, "conversation no longer available")
        session.add_all(Message(conversation_id=conversation_id, **item) for item in persisted)
    conversation_context_cache.append(conversation_id, persisted)

    # Compact only after the answer is durable. The cache is replaced only after
    # the summary transaction commits, so a failed MySQL write never drops context.
    complete_history = history + persisted
    if sum(estimate_tokens(x["content"]) + 4 for x in complete_history) > settings.conversation_summary_trigger_tokens:
        old, recent = split_recent(complete_history, settings.recent_dialogue_token_budget)
        if old:
            try:
                new_summary = await summarize_history(summary_text, old)
                with Session.begin() as session:
                    rows = list(session.scalars(select(Message).where(
                        Message.conversation_id == conversation_id
                    ).order_by(Message.created_at.asc(), Message.id.asc())))
                    boundary = rows[len(old) - 1]
                    summary_row = session.scalar(select(ConversationSummary).where(
                        ConversationSummary.conversation_id == conversation_id).with_for_update())
                    if summary_row:
                        summary_row.content = new_summary
                        summary_row.through_message_id = boundary.id
                    else:
                        session.add(ConversationSummary(conversation_id=conversation_id,
                            content=new_summary, through_message_id=boundary.id))
                conversation_context_cache.replace(conversation_id, recent)
            except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError):
                logger.warning("conversation summarization failed; retaining full context", exc_info=True)
    result["conversation_id"] = conversation_id
    result["playback_url"] = None if original_deleted else storage.playback_url(object_key)
    return result
