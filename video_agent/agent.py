import asyncio
import json

import httpx

from .config import settings
from .retrieval import search, verify_evidence

_llm_gate = asyncio.Semaphore(settings.llm_concurrency)


async def answer(session, video, question: str) -> dict:
    candidates = verify_evidence(video, search(session, video.id, question))
    if not candidates:
        return {"answer": "无法从视频中确认。", "citations": [], "abstained": True,
                "trace": ["classify", "retrieve", "verify", "abstain"]}
    # Without a configured model, return an extractive answer with exact evidence.
    if not settings.model_api_url:
        item = candidates[0]
        return {"answer": item.text, "citations": [citation(value) for value in candidates], "abstained": False,
                "trace": ["classify", "retrieve", "verify", "extract"]}
    payload = {"model": settings.model_name, "messages": [
        {"role": "system", "content": "只依据给定证据回答。输出 JSON: answer, citation_ids。无法确认则 answer 为‘无法从视频中确认。’且 citation_ids 为空。"},
        {"role": "user", "content": json.dumps({"question": question, "evidence": [
            {"id": x.id, "text": x.text, "start_ms": x.start_ms, "end_ms": x.end_ms}
            for x in candidates]}, ensure_ascii=False)}], "response_format": {"type": "json_object"}}
    try:
        async with _llm_gate:
            async with httpx.AsyncClient(timeout=30) as http:
                response = await http.post(settings.model_api_url, json=payload,
                    headers={"Authorization": f"Bearer {settings.model_api_key}"})
                response.raise_for_status()
        result = json.loads(response.json()["choices"][0]["message"]["content"])
        allowed = {x.id: x for x in candidates}
        cited = [allowed[x] for x in result.get("citation_ids", []) if x in allowed]
        if not cited or not str(result.get("answer", "")).strip():
            raise ValueError("no valid citation")
        return {"answer": result["answer"], "citations": [citation(x) for x in cited],
                "abstained": False, "trace": ["classify", "retrieve", "verify", "generate"]}
    except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError):
        return {"answer": "无法从视频中确认。", "citations": [], "abstained": True,
                "trace": ["classify", "retrieve", "verify", "abstain"]}


def citation(item):
    result = {"evidence_id": item.id, "modality": item.modality,
              "start_ms": item.start_ms, "end_ms": item.end_ms, "text": item.text}
    for name in ("keyword_score", "vector_score", "fused_score"):
        if hasattr(item, name):
            result[name] = round(getattr(item, name), 4)
    return result
