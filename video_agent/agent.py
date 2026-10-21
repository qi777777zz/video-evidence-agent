import asyncio
import json

import httpx

from .config import settings
from .conversation_store import estimate_tokens, trim_to_tokens
from .retrieval import search, verify_evidence
from .telemetry import LLM_REQUESTS

_llm_gate = asyncio.Semaphore(settings.llm_concurrency)


async def answer(session, video, question: str) -> dict:
    candidates = verify_evidence(video, search(session, video.id, question))[:settings.max_evidence_count]
    return await answer_candidates(candidates, question)


SYSTEM_PROMPT = ("只依据本轮给定证据回答。历史摘要和近期对话仅用于理解指代和上下文，"
                 "不可作为事实证据。输出 JSON: answer, citation_ids。无法确认则 answer 为"
                 "‘无法从视频中确认。’且 citation_ids 为空。")


def _take_recent(messages: list[dict], budget: int) -> list[dict]:
    selected, used = [], 0
    for item in reversed(messages):
        if item.get("role") not in {"user", "assistant"}:
            continue
        content = item.get("content", "")
        cost = estimate_tokens(content) + 4
        if used + cost > budget:
            break
        selected.append({"role": item["role"], "content": content})
        used += cost
    return list(reversed(selected))


def build_messages(candidates, question: str, history: list[dict] | None = None,
                   summary: str | None = None) -> list[dict]:
    """Allocate an explicit context budget in priority order: evidence, recent, summary."""
    total = settings.llm_input_token_budget
    system = trim_to_tokens(SYSTEM_PROMPT, min(settings.system_prompt_token_budget, total))
    total -= estimate_tokens(system) + 4
    question = trim_to_tokens(question, min(settings.current_question_token_budget, max(0, total)))
    total -= estimate_tokens(question) + 4

    evidence_budget = min(settings.evidence_token_budget, max(0, total))
    evidence, evidence_used = [], 0
    for item in candidates:
        text = trim_to_tokens(item.text, max(0, evidence_budget - evidence_used - 20))
        if not text:
            break
        row = {"id": item.id, "text": text, "start_ms": item.start_ms, "end_ms": item.end_ms}
        cost = estimate_tokens(json.dumps(row, ensure_ascii=False)) + 2
        if evidence_used + cost > evidence_budget:
            break
        evidence.append(row)
        evidence_used += cost
    total -= evidence_used

    recent_budget = min(settings.recent_dialogue_token_budget, max(0, total))
    recent = _take_recent(history or [], recent_budget)
    recent_used = sum(estimate_tokens(x["content"]) + 4 for x in recent)
    total -= recent_used
    summary_text = trim_to_tokens(summary or "", min(settings.conversation_summary_token_budget,
                                                        max(0, total)))

    messages = [{"role": "system", "content": system}]
    if summary_text:
        messages.append({"role": "system", "content": "历史摘要（仅供理解上下文）：" + summary_text})
    messages.extend(recent)
    messages.append({"role": "user", "content": json.dumps(
        {"question": question, "evidence": evidence}, ensure_ascii=False)})
    return messages


async def summarize_history(previous_summary: str | None, messages: list[dict]) -> str:
    source = json.dumps({"previous_summary": previous_summary, "messages": messages}, ensure_ascii=False)
    if not settings.model_api_url:
        return trim_to_tokens(source, settings.conversation_summary_token_budget)
    payload = {"model": settings.model_name, "max_tokens": settings.conversation_summary_token_budget,
               "messages": [{"role": "system", "content": "压缩会话历史，保留用户意图、指代和未决事项；不要加入事实。只输出摘要文本。"},
                            {"role": "user", "content": source}]}
    async with _llm_gate:
        async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as http:
            response = await http.post(settings.model_api_url, json=payload,
                headers={"Authorization": f"Bearer {settings.model_api_key}"})
            response.raise_for_status()
    return trim_to_tokens(response.json()["choices"][0]["message"]["content"],
                          settings.conversation_summary_token_budget)


async def answer_candidates(candidates, question: str, history: list[dict] | None = None,
                            summary: str | None = None) -> dict:
    if not candidates:
        return {"answer": "无法从视频中确认。", "citations": [], "abstained": True,
                "trace": ["classify", "retrieve", "verify", "abstain"]}
    # Without a configured model, return an extractive answer with exact evidence.
    if not settings.model_api_url:
        item = candidates[0]
        return {"answer": item.text, "citations": [citation(value) for value in candidates], "abstained": False,
                "trace": ["classify", "retrieve", "verify", "extract"]}
    messages = build_messages(candidates, question, history, summary)
    payload = {"model": settings.model_name, "max_tokens": settings.llm_max_output_tokens,
               "messages": messages, "response_format": {"type": "json_object"}}
    try:
        async with _llm_gate:
            async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as http:
                response = await http.post(settings.model_api_url, json=payload,
                    headers={"Authorization": f"Bearer {settings.model_api_key}"})
                response.raise_for_status()
        result = json.loads(response.json()["choices"][0]["message"]["content"])
        allowed = {x.id: x for x in candidates}
        cited = [allowed[x] for x in result.get("citation_ids", []) if x in allowed]
        if not cited or not str(result.get("answer", "")).strip():
            raise ValueError("no valid citation")
        LLM_REQUESTS.labels("success").inc()
        return {"answer": result["answer"], "citations": [citation(x) for x in cited],
                "abstained": False, "trace": ["classify", "retrieve", "verify", "generate"]}
    except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError):
        LLM_REQUESTS.labels("failure").inc()
        return {"answer": "无法从视频中确认。", "citations": [], "abstained": True,
                "trace": ["classify", "retrieve", "verify", "abstain"]}


def citation(item):
    result = {"evidence_id": item.id, "modality": item.modality,
              "start_ms": item.start_ms, "end_ms": item.end_ms, "text": item.text}
    for name in ("keyword_score", "vector_score", "fused_score"):
        if hasattr(item, name):
            result[name] = round(getattr(item, name), 4)
    return result
