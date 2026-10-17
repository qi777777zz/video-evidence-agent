import hashlib
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from temporalio.client import Client

from .agent import answer
from .config import settings
from .db import Session, Submission, Upload, Video, init_db
from . import storage
from .retrieval import simplified
from .workflow import VideoWorkflow
from .webhooks import UnsafeWebhook, resolve_public_webhook


logger = logging.getLogger("video_agent.api")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def effective_parser_version() -> str:
    return settings.pipeline_version()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    storage.ensure_bucket()
    app.state.temporal = await Client.connect(settings.temporal_address)
    yield


app = FastAPI(title="Video Evidence Agent", lifespan=lifespan)
STATIC_DIR = Path(__file__).with_name("static")


@app.middleware("http")
async def request_trace(request: Request, call_next):
    supplied_id = request.headers.get("X-Request-Id", "").strip()
    request_id = supplied_id if SAFE_ID.fullmatch(supplied_id) else str(uuid4())
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(json.dumps({"event": "request_failed", "request_id": request_id,
            "method": request.method, "path": request.url.path}, separators=(",", ":")))
        raise
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    response.headers["X-Request-Id"] = request_id
    logger.info(json.dumps({"event": "request_complete", "request_id": request_id,
        "method": request.method, "path": request.url.path,
        "status_code": response.status_code, "duration_ms": duration_ms}, separators=(",", ":")))
    return response


def tenant(x_api_key: str = Header(default=""), x_tenant_id: str = Header(default="demo")):
    if x_api_key != settings.api_key:
        raise HTTPException(401, "invalid API key")
    if not SAFE_ID.fullmatch(x_tenant_id):
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
    try:
        resolve_public_webhook(value)
    except UnsafeWebhook as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/health/live", include_in_schema=False)
def health_live():
    return {"status": "ok"}


@app.get("/health/ready", include_in_schema=False)
async def health_ready():
    checks = {}
    try:
        with Session() as session:
            session.execute(select(1))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "unavailable"
    try:
        storage.client().head_bucket(Bucket=settings.s3_bucket)
        checks["object_storage"] = "ok"
    except Exception:
        checks["object_storage"] = "unavailable"
    try:
        healthy = await app.state.temporal.service_client.check_health()
        checks["workflow"] = "ok" if healthy else "unavailable"
    except Exception:
        checks["workflow"] = "unavailable"
    if any(value != "ok" for value in checks.values()):
        raise HTTPException(503, detail={"status": "not_ready", "checks": checks})
    return {"status": "ready", "checks": checks}


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
        result["answer"] = simplified(result["answer"])
        for citation in result["citations"]:
            citation["text"] = simplified(citation["text"])
        result["playback_url"] = storage.playback_url(video.object_key)
        return result


@app.get("/", include_in_schema=False)
def workbench():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
