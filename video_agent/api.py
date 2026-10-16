import hashlib
import ipaddress
import json
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from temporalio.client import Client

from .agent import answer
from .config import settings
from .db import Session, Submission, Upload, Video, init_db
from . import storage
from .workflow import VideoWorkflow


def effective_parser_version() -> str:
    return f"{settings.parser_version}-smoke" if settings.processing_mode == "smoke" else settings.parser_version


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    storage.ensure_bucket()
    app.state.temporal = await Client.connect(settings.temporal_address)
    yield


app = FastAPI(title="Video Evidence Agent", lifespan=lifespan)


def tenant(x_api_key: str = Header(default=""), x_tenant_id: str = Header(default="demo")):
    if x_api_key != settings.api_key:
        raise HTTPException(401, "invalid API key")
    if not x_tenant_id or len(x_tenant_id) > 80:
        raise HTTPException(400, "invalid tenant")
    return x_tenant_id


class UploadStart(BaseModel):
    filename: str
    size_bytes: int = Field(gt=0)


class UploadComplete(BaseModel):
    parts: list[dict]


class VideoCreate(BaseModel):
    upload_id: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    webhook_url: str | None = None


class Question(BaseModel):
    question: str = Field(min_length=2, max_length=1000)


def checked_webhook(value: str | None):
    if value is None:
        return
    url = urlparse(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise HTTPException(400, "webhook must be an HTTPS URL")
    try:
        address = ipaddress.ip_address(url.hostname)
        if not address.is_global:
            raise HTTPException(400, "webhook host is not public")
    except ValueError:
        pass


@app.post("/uploads")
def start_upload(request: UploadStart, tenant_id: str = Depends(tenant)):
    if request.size_bytes > settings.max_video_bytes:
        raise HTTPException(413, "file too large")
    upload_id = str(uuid4())
    key = f"originals/{tenant_id}/{upload_id}"
    multipart = storage.client().create_multipart_upload(Bucket=settings.s3_bucket, Key=key,
        ContentType="video/mp4")
    with Session.begin() as session:
        session.add(Upload(id=upload_id, tenant_id=tenant_id, object_key=key,
                           multipart_id=multipart["UploadId"], declared_bytes=request.size_bytes))
    return {"upload_id": upload_id, "part_size": 16 * 1024 * 1024}


@app.get("/uploads/{upload_id}/parts/{part_number}")
def upload_part(upload_id: str, part_number: int, tenant_id: str = Depends(tenant)):
    with Session() as session:
        upload = session.get(Upload, upload_id)
        if not upload or upload.tenant_id != tenant_id or upload.status != "open":
            raise HTTPException(404, "upload not found")
        if not 1 <= part_number <= (upload.declared_bytes + 16 * 1024 * 1024 - 1) // (16 * 1024 * 1024):
            raise HTTPException(400, "invalid part number")
        url = storage.client().generate_presigned_url("upload_part", Params={
            "Bucket": settings.s3_bucket, "Key": upload.object_key,
            "UploadId": upload.multipart_id, "PartNumber": part_number}, ExpiresIn=900)
        return {"url": url}


@app.post("/uploads/{upload_id}/complete")
def complete_upload(upload_id: str, request: UploadComplete, tenant_id: str = Depends(tenant)):
    with Session.begin() as session:
        upload = session.get(Upload, upload_id)
        if not upload or upload.tenant_id != tenant_id:
            raise HTTPException(404, "upload not found")
        if upload.status == "complete":
            return {"upload_id": upload_id, "status": "complete"}
        expected = (upload.declared_bytes + 16 * 1024 * 1024 - 1) // (16 * 1024 * 1024)
        numbers = [part.get("PartNumber") for part in request.parts]
        if numbers != list(range(1, expected + 1)) or any(not part.get("ETag") for part in request.parts):
            raise HTTPException(400, "parts must be complete and ordered")
        storage.client().complete_multipart_upload(Bucket=settings.s3_bucket, Key=upload.object_key,
            UploadId=upload.multipart_id, MultipartUpload={"Parts": request.parts})
        actual = storage.client().head_object(Bucket=settings.s3_bucket, Key=upload.object_key)["ContentLength"]
        if actual != upload.declared_bytes:
            raise HTTPException(400, "uploaded size mismatch")
        upload.status = "complete"
    return {"upload_id": upload_id, "status": "complete"}


@app.post("/videos", status_code=202)
async def create_video(request: VideoCreate, idempotency_key: str = Header(min_length=1, max_length=160),
                       tenant_id: str = Depends(tenant)):
    checked_webhook(request.webhook_url)
    request_hash = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    with Session.begin() as session:
        prior = session.scalar(select(Submission).where(Submission.tenant_id == tenant_id,
            Submission.idempotency_key == idempotency_key))
        if prior:
            if prior.request_hash != request_hash:
                raise HTTPException(409, "idempotency key reused with different request")
            return {"job_id": prior.video_id, "reused": True}
        upload = session.get(Upload, request.upload_id)
        if not upload or upload.tenant_id != tenant_id or upload.status != "complete":
            raise HTTPException(400, "upload not complete")
        existing = session.scalar(select(Video).where(Video.tenant_id == tenant_id,
            Video.sha256 == request.sha256, Video.parser_version == effective_parser_version(),
            Video.status == "complete"))
        if existing:
            video = existing
        else:
            active = session.scalar(select(func.count()).select_from(Video).where(
                Video.tenant_id == tenant_id, Video.status.in_(["queued", "running"])))
            if active >= settings.tenant_concurrency:
                raise HTTPException(429, "tenant queue full")
            video = Video(tenant_id=tenant_id, object_key=upload.object_key,
                sha256=request.sha256, parser_version=effective_parser_version(),
                webhook_url=request.webhook_url)
            session.add(video)
            session.flush()
        session.add(Submission(tenant_id=tenant_id, idempotency_key=idempotency_key,
            request_hash=request_hash, video_id=video.id))
        job_id, reused = video.id, bool(existing)
    if not reused:
        try:
            await app.state.temporal.start_workflow(VideoWorkflow.run, args=[job_id, settings.processing_mode],
                id=f"video-{job_id}", task_queue="video-jobs")
        except Exception as exc:
            with Session.begin() as session:
                video = session.get(Video, job_id)
                video.status, video.stage, video.error = "failed", "dispatch", str(exc)[:1000]
            raise HTTPException(503, "workflow dispatch failed") from exc
    return {"job_id": job_id, "reused": reused}


@app.get("/jobs/{job_id}")
def get_job(job_id: str, tenant_id: str = Depends(tenant)):
    with Session() as session:
        video = session.get(Video, job_id)
        if not video or video.tenant_id != tenant_id:
            raise HTTPException(404, "job not found")
        return {"job_id": job_id, "status": video.status, "stage": video.stage,
                "error": video.error, "duration_ms": video.duration_ms}


@app.post("/videos/{video_id}/questions")
async def ask(video_id: str, request: Question, tenant_id: str = Depends(tenant)):
    with Session() as session:
        video = session.get(Video, video_id)
        if not video or video.tenant_id != tenant_id:
            raise HTTPException(404, "video not found")
        if video.status != "complete":
            raise HTTPException(409, "video not ready")
        result = await answer(session, video, request.question)
        result["playback_url"] = storage.playback_url(video.object_key)
        return result
