import re

def terms(text: str) -> set[str]:
    # Character bigrams work for Chinese without a separate tokenizer.
    clean = re.sub(r"\s+", "", text.lower())
    return {clean[i:i + 2] for i in range(len(clean) - 1)} | set(re.findall(r"[a-z0-9]+", clean))


def search(session, video_id: str, query: str, limit: int = 5) -> list:
    from sqlalchemy import select
    from .db import Evidence
    evidence = list(session.scalars(select(Evidence).where(Evidence.video_id == video_id)))
    query_terms = terms(query)
    if not query_terms:
        return []
    scored = []
    for item in evidence:
        document_terms = terms(item.text)
        overlap = len(query_terms & document_terms)
        if overlap:
            scored.append((overlap / len(query_terms), item))
    scored.sort(key=lambda x: (-x[0], x[1].start_ms))
    return [item for _, item in scored[:limit]]


def verify_evidence(video, items: list) -> list:
    return [item for item in items if item.video_id == video.id
            and 0 <= item.start_ms < item.end_ms <= video.duration_ms]
