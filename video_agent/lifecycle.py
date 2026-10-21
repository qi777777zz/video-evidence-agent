"""Retention enforcement and database/object-store consistency repair."""

import argparse
import logging
import time
from dataclasses import asdict, dataclass
from datetime import timedelta, timezone

from botocore.exceptions import ClientError
from sqlalchemy import delete, select

from . import storage
from .config import settings
from .db import (Conversation, ConversationSummary, Evidence, Message, Session, StageExecution,
                 Submission, Upload, Video, WebhookEvent, init_db, utcnow)


logger = logging.getLogger("video_agent.lifecycle")


@dataclass
class LifecycleResult:
    uploads_removed: int = 0
    videos_removed: int = 0
    originals_removed: int = 0
    orphan_objects_removed: int = 0
    orphan_uploads_aborted: int = 0
    missing_originals_repaired: int = 0
    missing_evidence_repaired: int = 0


def _video_keys(session, video_id: str) -> list[str]:
    video = session.get(Video, video_id)
    if not video:
        return []
    frames = list(session.scalars(select(Evidence.source_key).where(
        Evidence.video_id == video_id, Evidence.source_key.is_not(None))))
    return [video.object_key, video.audio_key, *frames]


def delete_video_data(video_id: str, tenant_id: str | None = None) -> bool:
    """Idempotently delete a video and all dependent records and objects."""
    with Session() as session:
        video = session.get(Video, video_id)
        if not video or (tenant_id is not None and video.tenant_id != tenant_id):
            return False
        keys = _video_keys(session, video_id)
        object_key = video.object_key
    storage.delete_many(keys)
    with Session.begin() as session:
        video = session.get(Video, video_id)
        if not video or (tenant_id is not None and video.tenant_id != tenant_id):
            return False
        session.execute(delete(WebhookEvent).where(WebhookEvent.video_id == video_id))
        session.execute(delete(Submission).where(Submission.video_id == video_id))
        session.execute(delete(StageExecution).where(StageExecution.video_id == video_id))
        session.execute(delete(Evidence).where(Evidence.video_id == video_id))
        conversation_ids = select(Conversation.id).where(Conversation.video_id == video_id)
        session.execute(delete(ConversationSummary).where(
            ConversationSummary.conversation_id.in_(conversation_ids)))
        session.execute(delete(Message).where(Message.conversation_id.in_(conversation_ids)))
        session.execute(delete(Conversation).where(Conversation.video_id == video_id))
        session.delete(video)
        remaining = session.scalar(select(Video.id).where(Video.object_key == object_key).limit(1))
        if not remaining:
            session.execute(delete(Upload).where(Upload.object_key == object_key))
    return True


def cleanup_expired(now=None) -> LifecycleResult:
    now = now or utcnow()
    result = LifecycleResult()
    upload_cutoff = now - timedelta(hours=settings.incomplete_upload_retention_hours)
    with Session() as session:
        stale_uploads = list(session.scalars(select(Upload).where(
            Upload.created_at < upload_cutoff,
            Upload.status.in_(["open", "paused", "complete"]),
            ~Upload.object_key.in_(select(Video.object_key)))))
    for item in stale_uploads:
        if item.status in {"open", "paused"}:
            try:
                storage.client().abort_multipart_upload(Bucket=settings.s3_bucket,
                    Key=item.object_key, UploadId=item.multipart_id)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") not in {"404", "NoSuchUpload", "NotFound"}:
                    raise
        else:
            storage.delete_many([item.object_key])
        with Session.begin() as session:
            row = session.get(Upload, item.id)
            if row and row.created_at < upload_cutoff:
                session.delete(row)
                result.uploads_removed += 1

    cutoffs = {
        "failed": now - timedelta(days=settings.failed_job_retention_days),
        "evidence": now - timedelta(days=settings.evidence_retention_days),
        "original": now - timedelta(days=settings.original_retention_days),
    }
    with Session() as session:
        expired_ids = list(session.scalars(select(Video.id).where(
            ((Video.status == "failed") & (Video.updated_at < cutoffs["failed"])) |
            ((Video.status == "ready") & (Video.created_at < cutoffs["evidence"])))))
        original_rows = list(session.execute(select(Video.id, Video.object_key).where(
            Video.status == "ready", Video.original_deleted_at.is_(None),
            Video.created_at < cutoffs["original"])))
    for video_id, key in original_rows:
        storage.delete_many([key])
        with Session.begin() as session:
            video = session.get(Video, video_id)
            if video and video.original_deleted_at is None:
                video.original_deleted_at = now
                result.originals_removed += 1
    for video_id in expired_ids:
        if delete_video_data(video_id):
            result.videos_removed += 1
    return result


def _objects():
    paginator = storage.client().get_paginator("list_objects_v2")
    for prefix in ("originals/", "frames/", "artifacts/", "temp/"):
        for page in paginator.paginate(Bucket=settings.s3_bucket, Prefix=prefix):
            yield from page.get("Contents", [])


def repair_consistency(now=None) -> LifecycleResult:
    now = now or utcnow()
    result = LifecycleResult()
    with Session() as session:
        originals = {key: video_id for key, video_id in session.execute(
            select(Video.object_key, Video.id).where(Video.original_deleted_at.is_(None)))}
        frames = {key: evidence_id for key, evidence_id in session.execute(
            select(Evidence.source_key, Evidence.id).where(Evidence.source_key.is_not(None)))}
        multipart_ids = set(session.scalars(select(Upload.multipart_id).where(
            Upload.status.in_(["open", "paused"]))))
    existing = {item["Key"]: item for item in _objects()}
    missing_original_ids = [identifier for key, identifier in originals.items() if key not in existing]
    missing_evidence_ids = [identifier for key, identifier in frames.items() if key not in existing]
    with Session.begin() as session:
        for identifier in missing_original_ids:
            video = session.get(Video, identifier)
            if video:
                video.status, video.stage = "failed", "storage_missing"
                video.error = "original object is missing; consistency repair marked this job failed"
                result.missing_originals_repaired += 1
        for identifier in missing_evidence_ids:
            evidence = session.get(Evidence, identifier)
            if evidence:
                evidence.source_key = None
                result.missing_evidence_repaired += 1

    referenced = set(originals) | set(frames)
    grace = now - timedelta(hours=settings.orphan_object_grace_hours)
    orphan_keys = []
    for key, item in existing.items():
        modified = item["LastModified"]
        if modified.tzinfo is None:
            modified = modified.replace(tzinfo=timezone.utc)
        if key not in referenced and modified < grace:
            orphan_keys.append(key)
    storage.delete_many(orphan_keys)
    result.orphan_objects_removed = len(orphan_keys)
    paginator = storage.client().get_paginator("list_multipart_uploads")
    for page in paginator.paginate(Bucket=settings.s3_bucket):
        for item in page.get("Uploads", []):
            initiated = item["Initiated"]
            if initiated.tzinfo is None:
                initiated = initiated.replace(tzinfo=timezone.utc)
            if item["UploadId"] not in multipart_ids and initiated < grace:
                storage.client().abort_multipart_upload(Bucket=settings.s3_bucket,
                    Key=item["Key"], UploadId=item["UploadId"])
                result.orphan_uploads_aborted += 1
    return result


def run_once() -> dict:
    expired = cleanup_expired()
    repaired = repair_consistency()
    values = asdict(expired)
    for key, value in asdict(repaired).items():
        values[key] += value
    logger.info("lifecycle pass complete: %s", values)
    return values


def main():
    parser = argparse.ArgumentParser(description="Enforce retention and repair storage consistency")
    parser.add_argument("--once", action="store_true", help="run one maintenance pass and exit")
    args = parser.parse_args()
    init_db()
    storage.ensure_bucket()
    while True:
        try:
            run_once()
        except Exception:
            logger.exception("lifecycle pass failed")
            if args.once:
                raise
        if args.once:
            return
        time.sleep(settings.lifecycle_interval_seconds)


if __name__ == "__main__":
    main()
