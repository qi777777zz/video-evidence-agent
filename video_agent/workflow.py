import asyncio
import hashlib
import hmac
import json
import tempfile
import os
import threading
import time
from datetime import timedelta
from functools import wraps
from pathlib import Path

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError, CancelledError

from .config import settings
from .db import Evidence, Session, StageExecution, Video, WebhookEvent
from .job_state import stage_snapshot, successful_stage, transition
from . import media, storage
from .webhooks import UnsafeWebhook, pinned_webhook_targets
from .telemetry import (PARSER_ATTEMPTS, PARSER_FAILURES, PARSER_RETRIES,
                        QUEUE_WAIT_SECONDS, STAGE_SECONDS, WEBHOOK_ATTEMPTS)


_cache_locks: dict[str, threading.Lock] = {}
_cache_locks_guard = threading.Lock()


def cached_source(video_id: str, key: str) -> str:
    """Keep a bounded per-worker source cache shared by prepare, ASR, and OCR."""
    root = Path(settings.media_cache_dir or tempfile.gettempdir()) / "video-agent-media"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{video_id}.mp4"
    with _cache_locks_guard:
        lock = _cache_locks.setdefault(video_id, threading.Lock())
    with lock:
        if not path.is_file():
            temporary = root / f".{video_id}-{os.getpid()}-{threading.get_ident()}.part"
            try:
                storage.download(key, str(temporary))
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        path.touch()
        files = sorted((item for item in root.glob("*.mp4") if item != path),
                       key=lambda item: item.stat().st_mtime)
        total = sum(item.stat().st_size for item in root.glob("*.mp4"))
        for item in files:
            if total <= settings.media_cache_max_bytes:
                break
            size = item.stat().st_size
            try:
                item.unlink(missing_ok=True)
            except OSError:
                continue
            total -= size
    return str(path)


def instrument_stage(stage: str, parser: str | None = None):
    """Measure each Activity attempt, including attempts that Temporal retries."""
    def decorate(function):
        @wraps(function)
        def measured(*args, **kwargs):
            started = time.perf_counter()
            if parser:
                PARSER_ATTEMPTS.labels(parser).inc()
                try:
                    if activity.info().attempt > 1:
                        PARSER_RETRIES.labels(parser).inc()
                except RuntimeError:  # Allows direct unit tests outside an Activity context.
                    pass
            try:
                result = function(*args, **kwargs)
            except Exception:
                STAGE_SECONDS.labels(stage, "failure").observe(time.perf_counter() - started)
                if parser:
                    PARSER_FAILURES.labels(parser).inc()
                    if args:
                        branch_state(args[0], parser, "failed")
                if args:
                    try:
                        with Session() as session:
                            video = session.get(Video, args[0])
                            public_stage = {"asr": "transcribing", "ocr": "ocr_processing",
                                            "prepare": "validating", "persist": "summarizing",
                                            "embedding": "indexing"}.get(stage, stage)
                            version = _input_version(video, public_stage)
                        stage_snapshot(args[0], public_stage, version, "failed", error="activity failed")
                    except Exception:
                        pass
                raise
            STAGE_SECONDS.labels(stage, "success").observe(time.perf_counter() - started)
            return result
        return measured
    return decorate


def failure_message(exc: BaseException) -> str:
    """Return the deepest useful Temporal/activity error instead of its wrapper."""
    current = exc
    seen = set()
    while current.__cause__ is not None and id(current.__cause__) not in seen:
        seen.add(id(current))
        current = current.__cause__
    message = str(current).strip() or str(exc).strip() or type(current).__name__
    return f"{type(current).__name__}: {message}"


def state(video_id: str, stage: str, status: str = "running", error: str | None = None):
    with Session.begin() as session:
        video = session.get(Video, video_id)
        # Compatibility mapper for callers while exposing the public state model.
        public = {"running": "processing", "complete": "ready"}.get(status, status)
        public_stage = {"prepare": "validating", "persist": "summarizing",
                        "embedding": "indexing", "complete": "ready"}.get(stage, stage)
        transition(video, public, stage=public_stage, error=error)


def _input_version(video: Video, stage: str) -> str:
    return hashlib.sha256(f"{video.sha256}:{video.parser_version}:{stage}".encode()).hexdigest()


def _stage_start(video_id: str, stage: str) -> tuple[str, StageExecution | None]:
    with Session() as session:
        video = session.get(Video, video_id)
        version = _input_version(video, stage)
    prior = successful_stage(video_id, stage, version)
    if prior is None:
        stage_snapshot(video_id, stage, version, "running", increment_attempt=True)
    return version, prior


def _stage_progress(video_id: str, stage: str, version: str, detail) -> None:
    progress = detail.get("progress", detail.get("percent", 0)) if isinstance(detail, dict) else 0
    stage_snapshot(video_id, stage, version, "running", progress=int(progress or 0))
    with Session.begin() as session:
        video = session.get(Video, video_id)
        video.progress = max(video.progress, min(95, int(progress or 0)))
    activity.heartbeat(detail)


def branch_state(video_id: str, branch: str, status: str) -> None:
    with Session.begin() as session:
        video = session.get(Video, video_id)
        setattr(video, f"{branch}_status", status)


@activity.defn
def acquire_tenant_slot(video_id: str) -> None:
    """Atomically admit no more than the configured jobs per tenant."""
    with Session.begin() as session:
        video = session.get(Video, video_id)
        if video.status in {"validating", "processing"}:
            return
        session.execute(text("INSERT IGNORE INTO tenant_locks (tenant_id) VALUES (:tenant)"),
                        {"tenant": video.tenant_id})
        session.execute(text("SELECT tenant_id FROM tenant_locks WHERE tenant_id=:tenant FOR UPDATE"),
                        {"tenant": video.tenant_id})
        active = session.scalar(select(func.count()).select_from(Video).where(
            Video.tenant_id == video.tenant_id,
            Video.status.in_(["validating", "processing"]), Video.id != video_id))
        if active >= settings.tenant_concurrency:
            raise RuntimeError("tenant concurrency slot unavailable")
        transition(video, "validating", progress=2)


@activity.defn
@instrument_stage("prepare")
def prepare(video_id: str) -> int:
    version, prior = _stage_start(video_id, "validating")
    if prior is not None and prior.output_data:
        return int(prior.output_data["duration_ms"])
    state(video_id, "prepare")
    with Session() as session:
        video = session.get(Video, video_id)
        key = video.object_key
        # utcnow avoids relying on the worker host's local timezone.
        from .db import utcnow
        queue_seconds = max(0.0, (utcnow() - video.created_at).total_seconds())
    QUEUE_WAIT_SECONDS.observe(queue_seconds)
    path = cached_source(video_id, key)
    with open(path, "rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    with Session() as session:
        expected = session.get(Video, video_id).sha256
    if digest != expected:
        Path(path).unlink(missing_ok=True)
        raise ApplicationError("uploaded file hash mismatch", non_retryable=True,
                               type="ValidationError")
    duration = media.duration_ms(path)
    with Session.begin() as session:
        session.get(Video, video_id).duration_ms = duration
    stage_snapshot(video_id, "validating", version, "succeeded", output_version=version,
                   output_data={"duration_ms": duration}, progress=100)
    return duration


@activity.defn
@instrument_stage("asr", "asr")
def transcribe(video_id: str) -> list[dict]:
    version, prior = _stage_start(video_id, "transcribing")
    if prior is not None:
        return list(prior.output_data or [])
    state(video_id, "transcribing")
    branch_state(video_id, "asr", "running")
    with Session() as session:
        key = session.get(Video, video_id).object_key
    path = cached_source(video_id, key)
    result = media.audio_segments(path, progress=lambda detail: _stage_progress(
        video_id, "transcribing", version, detail))
    branch_state(video_id, "asr", "complete")
    stage_snapshot(video_id, "transcribing", version, "succeeded", output_version=version,
                   output_data=result, progress=100)
    return result


@activity.defn
@instrument_stage("ocr", "ocr")
def inspect_slides(video_id: str, duration: int) -> list[dict]:
    version, prior = _stage_start(video_id, "ocr_processing")
    if prior is not None:
        return list(prior.output_data or [])
    state(video_id, "ocr_processing")
    branch_state(video_id, "ocr", "running")
    with Session() as session:
        key = session.get(Video, video_id).object_key
    path = cached_source(video_id, key)
    with tempfile.TemporaryDirectory() as folder:
        result = media.visual_segments(path, duration, frame_dir=folder,
            progress=lambda detail: _stage_progress(video_id, "ocr_processing", version, detail))
        for row in result:
            frame = row.pop("frame")
            frame_key = f"frames/{video_id}/{row['start_ms']}.jpg"
            storage.upload(frame, frame_key)
            row["source_key"] = frame_key
    branch_state(video_id, "ocr", "complete")
    stage_snapshot(video_id, "ocr_processing", version, "succeeded", output_version=version,
                   output_data=result, progress=100)
    return result


@activity.defn
@instrument_stage("persist")
def persist(video_id: str, speech: list[dict], slides: list[dict]):
    version, prior = _stage_start(video_id, "summarizing")
    if prior is not None:
        return
    state(video_id, "persist")
    with Session.begin() as session:
        video = session.get(Video, video_id)
        for row in sorted(speech + slides, key=lambda x: x["start_ms"]):
            if not row["text"] or row["end_ms"] <= row["start_ms"]:
                continue
            key_data = f'{video_id}:{row["modality"]}:{row["start_ms"]}:{row["end_ms"]}:{version}'
            business_key = hashlib.sha256(key_data.encode()).hexdigest()
            if session.scalar(select(Evidence.id).where(Evidence.video_id == video_id,
                                                        Evidence.business_key == business_key)):
                continue
            session.add(Evidence(video_id=video_id, business_key=business_key, modality=row["modality"],
                evidence_source=row["modality"],
                start_ms=row["start_ms"], end_ms=min(row["end_ms"], video.duration_ms),
                text=row["text"], confidence=row.get("confidence"), raw=row.get("raw", {}),
                source_key=row.get("source_key"), parser_version=video.parser_version))
    stage_snapshot(video_id, "summarizing", version, "succeeded", output_version=version,
                   output_data={"count": len(speech) + len(slides)}, progress=100)


@activity.defn
@instrument_stage("embedding")
def index_evidence(video_id: str):
    version, prior = _stage_start(video_id, "indexing")
    if prior is not None:
        return prior.output_data
    state(video_id, "embedding")
    with Session() as session:
        rows = list(session.scalars(select(Evidence).where(
            Evidence.video_id == video_id, Evidence.embedding.is_(None))))
        identifiers = [row.id for row in rows]
        texts = [row.text for row in rows]
    status = "indexed"
    try:
        from .embeddings import contextual_texts, encode
        vectors = encode(contextual_texts(rows))
    except Exception:
        # Keep lexical retrieval available; the reindex command can retry later.
        vectors, status = [], "deferred"
    with Session.begin() as session:
        for identifier, vector in zip(identifiers, vectors):
            row = session.get(Evidence, identifier)
            row.embedding = vector
            row.embedding_model = settings.embedding_model
            row.index_status = "indexed"
        if status == "deferred":
            for identifier in identifiers:
                session.get(Evidence, identifier).index_status = "deferred"
        video = session.get(Video, video_id)
        transition(video, "ready", progress=100)
    result = {"indexed": len(vectors), "deferred": status == "deferred"}
    stage_snapshot(video_id, "indexing", version, "succeeded", output_version=version,
                   output_data=result, progress=100)
    return result


@activity.defn
async def notify(video_id: str):
    with Session.begin() as session:
        video = session.get(Video, video_id)
        if not video.webhook_url:
            return
        event = session.scalar(select(WebhookEvent).where(WebhookEvent.video_id == video_id))
        if event is None:
            event = WebhookEvent(video_id=video_id)
            session.add(event)
            session.flush()
        if event.status == "delivered":
            return
        event.attempts += 1
        event_id, target, status = event.id, video.webhook_url, video.status
    body = json.dumps({"event_id": event_id, "video_id": video_id, "status": status}, separators=(",", ":"))
    signature = hmac.new(settings.webhook_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    started = time.perf_counter()
    try:
        try:
            targets = pinned_webhook_targets(target)
        except UnsafeWebhook as exc:
            raise ValueError(f"unsafe webhook target: {exc}") from exc
        timeout = httpx.Timeout(settings.webhook_timeout_seconds)
        last_error = None
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as http:
            for pinned_url, host_header, sni_hostname in targets:
                try:
                    request = http.build_request("POST", pinned_url, content=body,
                        headers={"Host": host_header, "Content-Type": "application/json",
                                 "X-Event-Id": event_id, "X-Signature-SHA256": signature})
                    request.extensions["sni_hostname"] = sni_hostname
                    response = await http.send(request, stream=True)
                    try:
                        response.raise_for_status()
                        received = 0
                        async for chunk in response.aiter_bytes():
                            received += len(chunk)
                            if received > settings.webhook_max_response_bytes:
                                raise ValueError("webhook response is too large")
                    finally:
                        await response.aclose()
                    last_error = None
                    break
                except (httpx.HTTPError, OSError) as exc:
                    last_error = exc
            if last_error is not None:
                raise last_error
    except Exception:
        WEBHOOK_ATTEMPTS.labels("failure").inc()
        STAGE_SECONDS.labels("webhook", "failure").observe(time.perf_counter() - started)
        raise
    WEBHOOK_ATTEMPTS.labels("success").inc()
    STAGE_SECONDS.labels("webhook", "success").observe(time.perf_counter() - started)
    with Session.begin() as session:
        session.get(WebhookEvent, event_id).status = "delivered"


@workflow.defn
class VideoWorkflow:
    @workflow.run
    async def run(self, video_id: str, mode: str):
        retry = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=2))
        resource_controls = workflow.patched("resource-controls-v1")
        try:
            if resource_controls:
                await workflow.execute_activity(acquire_tenant_slot, video_id,
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=0,
                        initial_interval=timedelta(seconds=2), maximum_interval=timedelta(seconds=30)))
            duration = await workflow.execute_activity(prepare, video_id,
                start_to_close_timeout=timedelta(minutes=15), retry_policy=retry)
            if mode == "smoke":
                await workflow.execute_activity(persist, args=[video_id, [], []],
                    start_to_close_timeout=timedelta(minutes=10), retry_policy=retry)
                await workflow.execute_activity(index_evidence, video_id,
                    start_to_close_timeout=timedelta(minutes=10), retry_policy=retry)
                await workflow.execute_activity(notify, video_id,
                    start_to_close_timeout=timedelta(seconds=30), retry_policy=retry)
                return
            common = {"start_to_close_timeout": timedelta(hours=3), "retry_policy": retry}
            if resource_controls:
                speech_call = workflow.execute_activity(transcribe, video_id,
                    task_queue="video-asr", heartbeat_timeout=timedelta(minutes=2), **common)
                slides_call = workflow.execute_activity(inspect_slides, args=[video_id, duration],
                    task_queue="video-ocr", heartbeat_timeout=timedelta(minutes=2), **common)
            else:
                speech_call = workflow.execute_activity(transcribe, video_id, **common)
                slides_call = workflow.execute_activity(inspect_slides, args=[video_id, duration], **common)
            speech, slides = await asyncio.gather(speech_call, slides_call)
            await workflow.execute_activity(persist, args=[video_id, speech, slides],
                start_to_close_timeout=timedelta(minutes=10), retry_policy=retry)
            await workflow.execute_activity(index_evidence, video_id,
                start_to_close_timeout=timedelta(minutes=30), retry_policy=retry)
        except CancelledError:
            await workflow.execute_activity(mark_cancelled, video_id,
                start_to_close_timeout=timedelta(seconds=30), retry_policy=retry)
            raise
        except Exception as exc:
            await workflow.execute_activity(fail, args=[video_id, failure_message(exc)],
                start_to_close_timeout=timedelta(seconds=30), retry_policy=retry)
            raise
        await workflow.execute_activity(notify, video_id,
            start_to_close_timeout=timedelta(seconds=30), retry_policy=retry)


@activity.defn
def fail(video_id: str, message: str):
    state(video_id, "failed", "failed", message[:1000])


@activity.defn
def mark_cancelled(video_id: str):
    with Session.begin() as session:
        video = session.get(Video, video_id)
        if video.status != "cancelling":
            transition(video, "cancelling")
        transition(video, "cancelled")
