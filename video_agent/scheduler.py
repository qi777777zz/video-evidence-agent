"""Singleton operational loops, deliberately separate from the stateless API."""
import asyncio
import logging

from sqlalchemy import select

from .adapters import MinioAdapter, TemporalAdapter
from .config import settings
from .db import Session, Video, init_db

logger = logging.getLogger("video_agent.scheduler")


async def dispatch_pending(workflows: TemporalAdapter) -> None:
    with Session() as session:
        pending = list(session.scalars(select(Video.id).where(
            Video.status == "uploaded", Video.stage == "dispatch_pending").limit(100)))
    for video_id in pending:
        try:
            await workflows.dispatch_video(video_id, settings.processing_mode)
        except Exception:
            logger.exception("workflow dispatch failed for %s", video_id)
            continue
        with Session.begin() as session:
            video = session.get(Video, video_id)
            if video and video.status == "uploaded" and video.stage == "dispatch_pending":
                video.stage = "uploaded"


async def main() -> None:
    init_db()
    MinioAdapter().ensure_bucket()
    workflows = await TemporalAdapter.connect(settings.temporal_address)
    while True:
        try:
            await dispatch_pending(workflows)
        except Exception:
            logger.exception("scheduler pass failed")
        await asyncio.sleep(settings.dispatch_interval_seconds)


if __name__ == "__main__":
    asyncio.run(main())
