import argparse

from sqlalchemy import select

from video_agent.config import settings
from video_agent.db import Evidence, Session, init_db
from video_agent.embeddings import contextual_texts, encode


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill pgvector embeddings for one video or all videos")
    parser.add_argument("--video-id")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--force", action="store_true", help="rebuild existing vectors")
    args = parser.parse_args()
    init_db()
    with Session() as session:
        statement = select(Evidence)
        if not args.force:
            statement = statement.where(Evidence.embedding.is_(None))
        if args.video_id:
            statement = statement.where(Evidence.video_id == args.video_id)
        rows = list(session.scalars(statement.order_by(Evidence.video_id, Evidence.start_ms)))
    texts = contextual_texts(rows)
    indexed = 0
    for offset in range(0, len(rows), args.batch_size):
        batch = rows[offset:offset + args.batch_size]
        vectors = encode(texts[offset:offset + args.batch_size])
        with Session.begin() as session:
            for row, vector in zip(batch, vectors):
                evidence = session.get(Evidence, row.id)
                evidence.embedding = vector
                evidence.embedding_model = settings.embedding_model
                evidence.index_status = "indexed"
        indexed += len(batch)
        print(f"indexed {indexed}/{len(rows)}")
    print({"indexed": indexed})


if __name__ == "__main__":
    main()
