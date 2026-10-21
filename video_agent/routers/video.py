import hashlib
import io
import json
import zipfile
from pathlib import Path
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, func, select, text

from .. import storage
from ..auth import require_scopes
from ..config import settings
from ..db import Evidence, Session, Submission, Upload, Video
from ..lifecycle import delete_video_data
from ..webhooks import UnsafeWebhook, resolve_public_webhook

router = APIRouter(prefix="/videos", tags=["Video"])
logger = logging.getLogger("video_agent.routers.video")


class VideoCreate(BaseModel):
    upload_id: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    webhook_url: str | None = None


class VideoUpdate(BaseModel):
    favorite: bool | None = None
    tags: list[str] | None = Field(default=None, max_length=30)


def video_payload(video: Video) -> dict:
    return {"id": video.id, "title": video.original_title or video.filename or "未命名视频",
            "filename": video.filename, "source_type": video.source_type,
            "source_url": video.source_url, "thumbnail_url": video.thumbnail_url,
            "status": video.status, "stage": video.stage, "progress": video.progress,
            "asr_status": video.asr_status, "ocr_status": video.ocr_status,
            "audio_status": video.audio_status, "summary_status": video.summary_status,
            "duration_ms": video.duration_ms, "error": video.error,
            "tags": video.tags or [], "favorite": video.favorite,
            "created_at": video.created_at.isoformat()}


@router.get("")
def list_videos(page: int = 1, page_size: int = 30, q: str = "", status: str = "",
                tenant_id: str = Depends(require_scopes("videos:read"))):
    page, page_size = max(1, page), min(100, max(1, page_size))
    with Session() as session:
        query = select(Video).where(Video.tenant_id == tenant_id)
        if status:
            query = query.where(Video.status == status)
        if q.strip():
            term = f"%{q.strip()}%"
            matching = select(Evidence.video_id).where(Evidence.text.ilike(term))
            query = query.where((Video.filename.ilike(term)) | (Video.original_title.ilike(term)) |
                                cast(Video.summary_data, String).ilike(term) | Video.id.in_(matching))
        total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = session.scalars(query.order_by(Video.created_at.desc()).offset(
            (page - 1) * page_size).limit(page_size)).all()
        return {"items": [video_payload(row) for row in rows], "page": page,
                "page_size": page_size, "total": total}


@router.get("/{video_id}")
def get_video(video_id: str, tenant_id: str = Depends(require_scopes("videos:read"))):
    with Session() as session:
        video = session.get(Video, video_id)
        if not video or video.tenant_id != tenant_id:
            raise HTTPException(404, "video not found")
        payload = video_payload(video)
        payload["playback_url"] = None if video.original_deleted_at else storage.playback_url(video.object_key)
        return payload


@router.patch("/{video_id}")
def update_video(video_id: str, body: VideoUpdate,
                 tenant_id: str = Depends(require_scopes("videos:write"))):
    with Session.begin() as session:
        video = session.get(Video, video_id)
        if not video or video.tenant_id != tenant_id:
            raise HTTPException(404, "video not found")
        if body.favorite is not None:
            video.favorite = body.favorite
        if body.tags is not None:
            video.tags = list(dict.fromkeys(tag.strip()[:40] for tag in body.tags if tag.strip()))
        session.flush()
        return video_payload(video)


def effective_parser_version() -> str:
    return settings.pipeline_version()


def checked_webhook(value: str | None):
    if value is None:
        return
    try:
        resolve_public_webhook(value)
    except UnsafeWebhook as exc:
        raise HTTPException(400, str(exc)) from exc


async def dispatch_video(request: Request, video_id: str) -> bool:
    try:
        from ..adapters import TemporalAdapter
        await TemporalAdapter(request.app.state.temporal).dispatch_video(video_id, settings.processing_mode)
    except Exception:
        logger.exception("workflow dispatch failed for %s; it remains pending", video_id)
        return False
    with Session.begin() as session:
        video = session.get(Video, video_id)
        if video and video.status == "queued" and video.stage == "dispatch_pending":
            video.stage = "queued"
    return True


@router.post("", status_code=202)
async def create_video(request_body: VideoCreate, request: Request,
                       idempotency_key: str = Header(min_length=1, max_length=160),
                       tenant_id: str = Depends(require_scopes("videos:write"))):
    from ..api import limiter
    limiter.check(tenant_id, "submit", settings.submit_rate_per_minute)
    checked_webhook(request_body.webhook_url)
    request_hash = hashlib.sha256(request_body.model_dump_json().encode()).hexdigest()
    with Session.begin() as session:
        prior = session.scalar(select(Submission).where(Submission.tenant_id == tenant_id,
            Submission.idempotency_key == idempotency_key))
        if prior:
            if prior.request_hash != request_hash:
                raise HTTPException(409, "idempotency key reused with different request")
            return {"job_id": prior.video_id, "reused": True}
        upload = session.get(Upload, request_body.upload_id)
        if not upload or upload.tenant_id != tenant_id or upload.status != "complete":
            raise HTTPException(400, "upload not complete")
        existing = session.scalar(select(Video).where(Video.tenant_id == tenant_id,
            Video.sha256 == request_body.sha256, Video.parser_version == effective_parser_version(),
            Video.status.in_(["ready", "complete"])))
        if existing:
            video = existing
        else:
            session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:tenant, 0))"),
                            {"tenant": tenant_id})
            queued = session.scalar(select(func.count()).select_from(Video).where(
                Video.tenant_id == tenant_id, Video.status == "queued"))
            if queued >= settings.tenant_queue_quota:
                raise HTTPException(429, "tenant queue full", headers={
                    "Retry-After": str(settings.rate_limit_retry_seconds)})
            video = Video(tenant_id=tenant_id, object_key=upload.object_key, stage="dispatch_pending",
                filename=upload.filename, original_title=upload.filename, source_type="local",
                sha256=request_body.sha256, parser_version=effective_parser_version(),
                webhook_url=request_body.webhook_url)
            session.add(video)
            session.flush()
        session.add(Submission(tenant_id=tenant_id, idempotency_key=idempotency_key,
            request_hash=request_hash, video_id=video.id))
        job_id, reused = video.id, bool(existing)
    if not reused:
        await dispatch_video(request, job_id)
    return {"job_id": job_id, "reused": reused}


def transcript_rows(session, video_id: str):
    return list(session.scalars(select(Evidence).where(
        Evidence.video_id == video_id, Evidence.modality == "speech").order_by(Evidence.start_ms)))


def srt_time(milliseconds: int) -> str:
    hours, remainder = divmod(max(0, milliseconds), 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


@router.get("/{video_id}/transcript")
def transcript(video_id: str, format: str = "json",
               tenant_id: str = Depends(require_scopes("videos:read"))):
    if format not in {"json", "txt", "srt"}:
        raise HTTPException(400, "format must be json, txt, or srt")
    with Session() as session:
        video = session.get(Video, video_id)
        if not video or video.tenant_id != tenant_id:
            raise HTTPException(404, "video not found")
        rows = transcript_rows(session, video_id)
        if format == "json":
            return {"items": [{"start_ms": x.start_ms, "end_ms": x.end_ms,
                                "text": x.text} for x in rows]}
        if format == "txt":
            content = "\n".join(x.text for x in rows)
        else:
            content = "\n\n".join(f"{i}\n{srt_time(x.start_ms)} --> {srt_time(x.end_ms)}\n{x.text}"
                                    for i, x in enumerate(rows, 1))
        media_type = "text/plain" if format == "txt" else "application/x-subrip"
        name = Path(video.filename or "transcript").stem
        return Response(content, media_type=f"{media_type}; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="{name}.{format}"'})


@router.post("/batch-export")
def batch_export(video_ids: list[str], tenant_id: str = Depends(require_scopes("videos:read"))):
    if not video_ids or len(video_ids) > 50:
        raise HTTPException(400, "select between 1 and 50 videos")
    output = io.BytesIO()
    with Session() as session, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        videos = list(session.scalars(select(Video).where(Video.tenant_id == tenant_id,
                                                          Video.id.in_(video_ids))))
        for video in videos:
            name = Path(video.filename or video.id).stem
            rows = transcript_rows(session, video.id)
            archive.writestr(f"{name}/transcript.txt", "\n".join(x.text for x in rows))
            archive.writestr(f"{name}/transcript.srt", "\n\n".join(
                f"{i}\n{srt_time(x.start_ms)} --> {srt_time(x.end_ms)}\n{x.text}"
                for i, x in enumerate(rows, 1)))
            if video.summary_data:
                archive.writestr(f"{name}/summary.json", json.dumps(video.summary_data,
                                 ensure_ascii=False, indent=2))
    output.seek(0)
    return StreamingResponse(output, media_type="application/zip", headers={
        "Content-Disposition": 'attachment; filename="video-evidence-export.zip"'})


@router.delete("/{video_id}", status_code=204)
async def delete_video(video_id: str, request: Request,
                       tenant_id: str = Depends(require_scopes("videos:write"))):
    with Session() as session:
        video = session.get(Video, video_id)
        if not video or video.tenant_id != tenant_id:
            raise HTTPException(404, "video not found")
        active = video.status in {"queued", "running"}
    if active:
        try:
            await request.app.state.temporal.get_workflow_handle(f"video-{video_id}").cancel()
        except Exception as exc:
            raise HTTPException(503, "workflow cancellation failed; retry safely") from exc
    try:
        delete_video_data(video_id, tenant_id)
    except Exception as exc:
        raise HTTPException(503, "video deletion is pending; retry safely") from exc
    return Response(status_code=204)
