import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import time
from contextlib import asynccontextmanager, suppress
from datetime import timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError

from .agent import answer_candidates
from .auth import ensure_bootstrap_key, require_scopes
from .config import settings
from .db import Evidence, Session, Submission, Upload, Video, engine, init_db, utcnow
from . import storage
from .retrieval import search, simplified, verify_evidence
from .workflow import VideoWorkflow
from .webhooks import UnsafeWebhook, resolve_public_webhook
from .telemetry import (ACTIVE_JOBS, DB_POOL_CHECKED_IN, DB_POOL_CHECKED_OUT, DB_POOL_OVERFLOW,
    DB_POOL_SIZE, HTTP_REQUESTS, HTTP_SECONDS, JOB_STAGES, METRICS_DB_REFRESH, METRICS_DB_UP,
    RedisSlidingWindowLimiter)


logger = logging.getLogger("video_agent.api")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_metrics_snapshot = None
limiter = RedisSlidingWindowLimiter(settings.redis_url, settings.rate_limit_key_prefix)


def effective_parser_version() -> str:
    return settings.pipeline_version()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ensure_bootstrap_key(settings.api_key, settings.bootstrap_tenant_id)
    storage.ensure_bucket()
    app.state.temporal = await Client.connect(settings.temporal_address)
    app.state.upload_reaper = asyncio.create_task(upload_reaper())
    app.state.dispatcher = asyncio.create_task(dispatch_pending_loop())
    try:
        yield
    finally:
        app.state.upload_reaper.cancel()
        app.state.dispatcher.cancel()
        with suppress(asyncio.CancelledError):
            await app.state.upload_reaper
        with suppress(asyncio.CancelledError):
            await app.state.dispatcher


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
    route = request.scope.get("route")
    route_path = getattr(route, "path", request.url.path)
    HTTP_REQUESTS.labels(request.method, route_path, str(response.status_code)).inc()
    HTTP_SECONDS.labels(request.method, route_path).observe(duration_ms / 1000)
    response.headers["X-Request-Id"] = request_id
    logger.info(json.dumps({"event": "request_complete", "request_id": request_id,
        "method": request.method, "path": request.url.path,
        "status_code": response.status_code, "duration_ms": duration_ms}, separators=(",", ":")))
    return response


class UploadStart(BaseModel):
    filename: str
    size_bytes: int = Field(gt=0)


class UploadComplete(BaseModel):
    parts: list[dict]


UPLOAD_PART_SIZE = 16 * 1024 * 1024


def upload_expiry(upload: Upload):
    created = upload.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created + timedelta(hours=settings.incomplete_upload_retention_hours)


def expire_upload(upload: Upload) -> None:
    if upload.status != "open" or utcnow() < upload_expiry(upload):
        return
    with suppress(Exception):
        storage.abort_multipart(upload.object_key, upload.multipart_id)
    upload.status = "expired"


def expire_stale_uploads() -> None:
    cutoff = utcnow() - timedelta(hours=settings.incomplete_upload_retention_hours)
    with Session.begin() as session:
        uploads = session.scalars(select(Upload).where(
            Upload.status == "open", Upload.created_at <= cutoff)).all()
        for upload in uploads:
            expire_upload(upload)


async def upload_reaper() -> None:
    while True:
        try:
            await asyncio.to_thread(expire_stale_uploads)
        except Exception:
            logger.exception("failed to expire stale multipart uploads")
        await asyncio.sleep(settings.lifecycle_interval_seconds)


async def dispatch_video(video_id: str) -> bool:
    """Idempotently hand a committed job to Temporal."""
    try:
        await app.state.temporal.start_workflow(VideoWorkflow.run,
            args=[video_id, settings.processing_mode], id=f"video-{video_id}",
            task_queue="video-control")
    except WorkflowAlreadyStartedError:
        pass
    except Exception:
        logger.exception("workflow dispatch failed for %s; it remains pending", video_id)
        return False
    with Session.begin() as session:
        video = session.get(Video, video_id)
        if video and video.status == "queued" and video.stage == "dispatch_pending":
            video.stage = "queued"
    return True


async def dispatch_pending_loop() -> None:
    while True:
        try:
            with Session() as session:
                pending = list(session.scalars(select(Video.id).where(
                    Video.status == "queued", Video.stage == "dispatch_pending").limit(100)))
            for video_id in pending:
                await dispatch_video(video_id)
        except Exception:
            logger.exception("pending workflow dispatch pass failed")
        await asyncio.sleep(settings.dispatch_interval_seconds)


def canonical_parts(parts: list[dict], expected: int) -> list[dict]:
    """Keep the latest duplicate and require one contiguous part sequence."""
    by_number = {}
    for part in parts:
        number, etag = part.get("PartNumber"), part.get("ETag")
        if not isinstance(number, int) or not etag:
            raise HTTPException(400, "invalid multipart entry")
        by_number[number] = {"PartNumber": number, "ETag": etag}
    if sorted(by_number) != list(range(1, expected + 1)):
        raise HTTPException(400, "parts must be complete and consecutive")
    return [by_number[number] for number in range(1, expected + 1)]


def validate_part_sizes(parts: list[dict], declared_bytes: int) -> None:
    """Reject truncated chunks before multipart completion becomes irreversible."""
    if not parts or any("Size" not in part for part in parts):
        return  # compatibility path for clients used with stores lacking ListParts
    expected_count = (declared_bytes + UPLOAD_PART_SIZE - 1) // UPLOAD_PART_SIZE
    last_size = declared_bytes - UPLOAD_PART_SIZE * (expected_count - 1)
    for part in parts:
        expected_size = last_size if part["PartNumber"] == expected_count else UPLOAD_PART_SIZE
        if int(part["Size"]) != expected_size:
            raise HTTPException(400, f"uploaded part {part['PartNumber']} has unexpected size")


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
    try:
        limiter.client.ping()
        checks["rate_limiter"] = "ok"
    except Exception:
        checks["rate_limiter"] = "unavailable"
    if any(value != "ok" for value in checks.values()):
        raise HTTPException(503, detail={"status": "not_ready", "checks": checks})
    return {"status": "ready", "checks": checks}


@app.post("/uploads")
def start_upload(request: UploadStart, tenant_id: str = Depends(require_scopes("uploads:write"))):
    limiter.check(tenant_id, "upload", settings.upload_rate_per_minute)
    if request.size_bytes > settings.max_video_bytes:
        raise HTTPException(413, "file too large")
    upload_id = str(uuid4())
    key = f"originals/{tenant_id}/{upload_id}"
    multipart_id = storage.create_multipart(key, "video/mp4")
    created_at = utcnow()
    with Session.begin() as session:
        session.add(Upload(id=upload_id, tenant_id=tenant_id, object_key=key,
                           multipart_id=multipart_id, declared_bytes=request.size_bytes,
                           created_at=created_at))
    expires_at = created_at + timedelta(hours=settings.incomplete_upload_retention_hours)
    return {"upload_id": upload_id, "part_size": UPLOAD_PART_SIZE,
            "expires_at": expires_at.isoformat()}


@app.get("/uploads/{upload_id}")
def get_upload(upload_id: str, tenant_id: str = Depends(require_scopes("uploads:write"))):
    with Session.begin() as session:
        upload = session.get(Upload, upload_id)
        if not upload or upload.tenant_id != tenant_id:
            raise HTTPException(404, "upload not found")
        expire_upload(upload)
        if upload.status == "expired":
            session.commit()
            raise HTTPException(410, "upload session expired")
        parts = storage.list_parts(upload.object_key, upload.multipart_id) if upload.status == "open" else []
        return {"upload_id": upload.id, "status": upload.status,
                "part_size": UPLOAD_PART_SIZE, "declared_bytes": upload.declared_bytes,
                "expires_at": upload_expiry(upload).isoformat(), "parts": parts}


@app.get("/uploads/{upload_id}/parts/{part_number}")
def upload_part(upload_id: str, part_number: int, tenant_id: str = Depends(require_scopes("uploads:write"))):
    with Session.begin() as session:
        upload = session.get(Upload, upload_id)
        if not upload or upload.tenant_id != tenant_id:
            raise HTTPException(404, "upload not found")
        expire_upload(upload)
        if upload.status == "expired":
            session.commit()
            raise HTTPException(410, "upload session expired")
        if upload.status != "open":
            raise HTTPException(409, f"upload is {upload.status}")
        if not 1 <= part_number <= (upload.declared_bytes + UPLOAD_PART_SIZE - 1) // UPLOAD_PART_SIZE:
            raise HTTPException(400, "invalid part number")
        url = storage.presign_part(upload.object_key, upload.multipart_id, part_number)
        return {"url": url}


@app.post("/uploads/{upload_id}/complete")
def complete_upload(upload_id: str, request: UploadComplete,
                    tenant_id: str = Depends(require_scopes("uploads:write"))):
    with Session.begin() as session:
        upload = session.scalar(select(Upload).where(Upload.id == upload_id).with_for_update())
        if not upload or upload.tenant_id != tenant_id:
            raise HTTPException(404, "upload not found")
        if upload.status == "complete":
            return {"upload_id": upload_id, "status": "complete"}
        expire_upload(upload)
        if upload.status == "expired":
            session.commit()
            raise HTTPException(410, "upload session expired")
        if upload.status not in {"open", "completing"}:
            raise HTTPException(409, f"upload is {upload.status}")
        expected = (upload.declared_bytes + UPLOAD_PART_SIZE - 1) // UPLOAD_PART_SIZE
        key, multipart_id, declared = upload.object_key, upload.multipart_id, upload.declared_bytes
        upload.status = "completing"
    try:
        if not storage.object_exists(key):
            listed_parts = storage.list_parts(key, multipart_id)
            validate_part_sizes(listed_parts, declared)
            parts = canonical_parts(listed_parts or request.parts, expected)
            storage.complete_multipart(key, multipart_id, parts)
        actual = storage.object_size(key)
        if actual != declared:
            storage.delete_many([key])
            with Session.begin() as session:
                upload = session.get(Upload, upload_id)
                if upload:
                    upload.status = "invalid"
            raise HTTPException(400, "uploaded size mismatch; object removed")
    except HTTPException:
        raise
    except Exception as exc:
        # Keep `completing`: retrying this endpoint reconciles either the
        # multipart upload or an object completed before a database failure.
        raise HTTPException(503, "upload finalization interrupted; retry safely") from exc
    with Session.begin() as session:
        upload = session.get(Upload, upload_id)
        if not upload or upload.tenant_id != tenant_id:
            raise HTTPException(404, "upload not found")
        upload.status, upload.completed_at = "complete", utcnow()
    return {"upload_id": upload_id, "status": "complete"}


@app.post("/videos", status_code=202)
async def create_video(request: VideoCreate, idempotency_key: str = Header(min_length=1, max_length=160),
                       tenant_id: str = Depends(require_scopes("videos:write"))):
    limiter.check(tenant_id, "submit", settings.submit_rate_per_minute)
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
            # Serialize admission for this tenant across all API replicas. The
            # lock is released automatically with the surrounding transaction.
            session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:tenant, 0))"),
                            {"tenant": tenant_id})
            queued = session.scalar(select(func.count()).select_from(Video).where(
                Video.tenant_id == tenant_id, Video.status == "queued"))
            if queued >= settings.tenant_queue_quota:
                raise HTTPException(429, "tenant queue full", headers={
                    "Retry-After": str(settings.rate_limit_retry_seconds)})
            video = Video(tenant_id=tenant_id, object_key=upload.object_key, stage="dispatch_pending",
                sha256=request.sha256, parser_version=effective_parser_version(),
                webhook_url=request.webhook_url)
            session.add(video)
            session.flush()
        session.add(Submission(tenant_id=tenant_id, idempotency_key=idempotency_key,
            request_hash=request_hash, video_id=video.id))
        job_id, reused = video.id, bool(existing)
    if not reused:
        await dispatch_video(job_id)
    return {"job_id": job_id, "reused": reused}


@app.get("/jobs/{job_id}")
def get_job(job_id: str, tenant_id: str = Depends(require_scopes("videos:read"))):
    with Session() as session:
        video = session.get(Video, job_id)
        if not video or video.tenant_id != tenant_id:
            raise HTTPException(404, "job not found")
        return {"job_id": job_id, "status": video.status, "stage": video.stage,
                "asr_status": video.asr_status, "ocr_status": video.ocr_status,
                "error": video.error, "duration_ms": video.duration_ms}


@app.delete("/videos/{video_id}", status_code=204)
async def delete_video(video_id: str, tenant_id: str = Depends(require_scopes("videos:write"))):
    from .lifecycle import delete_video_data
    with Session() as session:
        video = session.get(Video, video_id)
        if not video or video.tenant_id != tenant_id:
            raise HTTPException(404, "video not found")
        active = video.status in {"queued", "running"}
    if active:
        try:
            await app.state.temporal.get_workflow_handle(f"video-{video_id}").cancel()
        except Exception as exc:
            logger.exception("could not cancel workflow before deletion")
            raise HTTPException(503, "workflow cancellation failed; retry safely") from exc
    try:
        delete_video_data(video_id, tenant_id)
    except Exception as exc:
        logger.exception("video deletion failed")
        raise HTTPException(503, "video deletion is pending; retry safely") from exc
    return Response(status_code=204)


@app.delete("/uploads/{upload_id}", status_code=204)
def delete_upload(upload_id: str, tenant_id: str = Depends(require_scopes("uploads:write"))):
    with Session() as session:
        upload = session.get(Upload, upload_id)
        if not upload or upload.tenant_id != tenant_id:
            raise HTTPException(404, "upload not found")
        if session.scalar(select(Video.id).where(Video.object_key == upload.object_key).limit(1)):
            raise HTTPException(409, "upload is attached to a video; delete the video instead")
        status, key, multipart_id = upload.status, upload.object_key, upload.multipart_id
    try:
        if status in {"open", "completing"}:
            try:
                storage.client().abort_multipart_upload(Bucket=settings.s3_bucket, Key=key,
                                                        UploadId=multipart_id)
            except Exception as exc:
                code = getattr(exc, "response", {}).get("Error", {}).get("Code")
                if code not in {"404", "NoSuchUpload", "NotFound"}:
                    raise
            storage.delete_many([key])
        else:
            storage.delete_many([key])
    except Exception as exc:
        logger.exception("upload deletion failed")
        raise HTTPException(503, "upload deletion failed; retry safely") from exc
    with Session.begin() as session:
        upload = session.get(Upload, upload_id)
        if upload and upload.tenant_id == tenant_id:
            session.delete(upload)
    return Response(status_code=204)


@app.post("/videos/{video_id}/questions")
async def ask(video_id: str, request: Question,
              tenant_id: str = Depends(require_scopes("questions:ask"))):
    limiter.check(tenant_id, "question", settings.question_rate_per_minute)
    with Session() as session:
        video = session.get(Video, video_id)
        if not video or video.tenant_id != tenant_id:
            raise HTTPException(404, "video not found")
        if video.status != "complete":
            raise HTTPException(409, "video not ready")
        candidates = verify_evidence(video, search(session, video.id, request.question))[
            :settings.max_evidence_count]
        object_key, original_deleted = video.object_key, video.original_deleted_at is not None
    # Do not retain a database connection while an external model may take tens of seconds.
    result = await answer_candidates(candidates, request.question)
    result["answer"] = simplified(result["answer"])
    for citation in result["citations"]:
        citation["text"] = simplified(citation["text"])
    result["playback_url"] = None if original_deleted else storage.playback_url(object_key)
    return result


def _metrics_access(request: Request):
    try:
        address = ipaddress.ip_address(request.client.host if request.client else "")
        networks = [ipaddress.ip_network(value.strip()) for value in
                    settings.metrics_allowed_networks.split(",") if value.strip()]
    except ValueError as exc:
        logger.error("invalid metrics network configuration: %s", exc)
        raise HTTPException(503, "metrics access is not configured") from exc
    if not any(address in network for network in networks):
        raise HTTPException(403, "metrics endpoint is restricted")
    if settings.metrics_token:
        supplied = request.headers.get("Authorization", "")
        expected = f"Bearer {settings.metrics_token}"
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(401, "invalid metrics token", headers={"WWW-Authenticate": "Bearer"})


def _refresh_database_metrics():
    global _metrics_snapshot
    with Session() as session:
        jobs = session.execute(select(Video.status, Video.stage, func.count()).group_by(
            Video.status, Video.stage)).all()
    _metrics_snapshot = jobs


def _apply_database_metrics(snapshot):
    jobs = snapshot
    ACTIVE_JOBS.set(sum(count for status, _, count in jobs if status in {"queued", "running"}))
    JOB_STAGES.clear()
    for status, stage, count in jobs:
        JOB_STAGES.labels(status, stage).set(count)


@app.get("/metrics", include_in_schema=False)
def metrics(request: Request):
    _metrics_access(request)
    try:
        _refresh_database_metrics()
    except Exception:
        METRICS_DB_UP.set(0)
        METRICS_DB_REFRESH.labels("failure").inc()
        logger.warning("database metric refresh failed; serving cached/process metrics", exc_info=True)
    else:
        METRICS_DB_UP.set(1)
        METRICS_DB_REFRESH.labels("success").inc()
    if _metrics_snapshot is not None:
        _apply_database_metrics(_metrics_snapshot)
    pool = engine.pool
    for metric, getter in ((DB_POOL_SIZE, "size"), (DB_POOL_CHECKED_OUT, "checkedout"),
                           (DB_POOL_CHECKED_IN, "checkedin"), (DB_POOL_OVERFLOW, "overflow")):
        value = getattr(pool, getter, None)
        if callable(value):
            metric.set(value())
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/", include_in_schema=False)
def workbench():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
