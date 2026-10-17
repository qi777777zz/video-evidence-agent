from functools import lru_cache

from .config import settings


@lru_cache(maxsize=1)
def model():
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=settings.embedding_model, cache_dir=settings.embedding_cache_dir)


def encode(texts: list[str], *, query: bool = False) -> list[list[float]]:
    if not texts:
        return []
    encoder = model().query_embed if query else model().passage_embed
    vectors = encoder(texts)
    result = [vector.tolist() for vector in vectors]
    if any(len(vector) != settings.embedding_dimension for vector in result):
        raise ValueError("embedding dimension does not match EMBEDDING_DIMENSION")
    return result


def contextual_texts(rows: list, window: int = 2) -> list[str]:
    """Give short ASR fragments enough neighboring context for semantic search."""
    result = []
    for index in range(len(rows)):
        left, right = max(0, index - window), min(len(rows), index + window + 1)
        video_id = getattr(rows[index], "video_id", None)
        result.append(" ".join(row.text for row in rows[left:right]
                               if row.text and getattr(row, "video_id", None) == video_id))
    return result
