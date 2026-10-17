import re
import math
from functools import lru_cache

from .config import settings

def terms(text: str) -> set[str]:
    # Character bigrams work for Chinese without a separate tokenizer.
    clean = re.sub(r"\s+", "", _simplified(text).lower())
    return {clean[i:i + 2] for i in range(len(clean) - 1)} | set(re.findall(r"[a-z0-9]+", clean))


@lru_cache(maxsize=4096)
def _simplified(text: str) -> str:
    try:
        return _opencc().convert(text)
    except ImportError:
        return text


@lru_cache(maxsize=1)
def _opencc():
    from opencc import OpenCC
    return OpenCC("t2s")


def search(session, video_id: str, query: str, limit: int = 5) -> list:
    from sqlalchemy import select
    from .db import Evidence
    evidence = list(session.scalars(select(Evidence).where(Evidence.video_id == video_id)))
    query_terms = terms(query)
    if not query_terms:
        return []
    # A single common bigram (for example “是否”) is not enough evidence for
    # a normal-length question. Keep one-term lookups useful while requiring
    # both absolute and proportional overlap for longer queries.
    minimum_overlap = 1 if len(query_terms) <= 3 else max(2, math.ceil(len(query_terms) * 0.15))
    keyword_scores = {}
    for item in evidence:
        document_terms = terms(item.text)
        overlap = len(query_terms & document_terms)
        if overlap >= minimum_overlap:
            keyword_scores[item.id] = overlap / len(query_terms)

    vector_scores = {}
    if any(getattr(item, "embedding", None) is not None for item in evidence):
        try:
            from .embeddings import encode
            query_vector = encode([query], query=True)[0]
            distance = Evidence.embedding.cosine_distance(query_vector)
            rows = session.execute(
                select(Evidence, distance.label("distance"))
                .where(Evidence.video_id == video_id, Evidence.embedding.is_not(None))
                .order_by(distance).limit(max(limit * 4, 20))
            )
            vector_scores = {item.id: max(0.0, 1.0 - float(value)) for item, value in rows}
        except Exception:
            # Model/cache outages must not take down the question endpoint.
            vector_scores = {}

    by_id = {item.id: item for item in evidence}
    scored = []
    for identifier in keyword_scores.keys() | vector_scores.keys():
        keyword = keyword_scores.get(identifier, 0.0)
        vector = vector_scores.get(identifier, 0.0)
        if not keyword and vector < settings.retrieval_vector_threshold:
            continue
        fused = settings.retrieval_keyword_weight * keyword + settings.retrieval_vector_weight * vector
        item = by_id[identifier]
        item.keyword_score, item.vector_score, item.fused_score = keyword, vector, fused
        scored.append((fused, item))
    scored.sort(key=lambda row: (-row[0], row[1].start_ms))
    return [item for _, item in scored[:limit]]


def verify_evidence(video, items: list) -> list:
    return [item for item in items if item.video_id == video.id
            and 0 <= item.start_ms < item.end_ms <= video.duration_ms]
