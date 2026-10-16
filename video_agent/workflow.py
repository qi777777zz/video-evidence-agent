import asyncio
import hashlib
import hmac
import json
import tempfile
from datetime import timedelta
from pathlib import Path

import httpx
from sqlalchemy import select
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from .config import settings
from .db import Evidence, Session, Video, WebhookEvent
from . import media, storage


def state(video_id: str, stage: str, status: str = "running", error: str | None = None):
    with Session.begin() as session:
        video = session.get(Video, video_id)
        video.stage, video.status, video.error = stage, status, error


@activity.defn
def prepare(video_id: str) -> int:
    state(video_id, "prepare")
    with Session() as session:
        video = session.get(Video, video_id)
        key = video.object_key
    with tempfile.TemporaryDirectory() as folder:
        path = str(Path(folder) / "source.mp4")
        storage.download(key, path)
        with open(path, "rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        with Session() as session:
            expected = session.get(Video, video_id).sha256
        if digest != expected:
            raise ValueError("uploaded file hash mismatch")
        duration = media.duration_ms(path)
    with Session.begin() as session:
        session.get(Video, video_id).duration_ms = duration
    return duration


@activity.defn
def transcribe(video_id: str) -> list[dict]:
    state(video_id, "transcribe")
    with Session() as session:
        key = session.get(Video, video_id).object_key
    with tempfile.TemporaryDirectory() as folder:
        path = str(Path(folder) / "source.mp4")
        storage.download(key, path)
        return media.audio_segments(path)


@activity.defn
def inspect_slides(video_id: str, duration: int) -> list[dict]:
    state(video_id, "inspect_slides")
    with Session() as session:
        key = session.get(Video, video_id).object_key
    with tempfile.TemporaryDirectory() as folder:
        path = str(Path(folder) / "source.mp4")
        storage.download(key, path)
        result = media.visual_segments(path, duration)
        for row in result:
            frame = row.pop("frame")
            frame_key = f"frames/{video_id}/{row['start_ms']}.jpg"
            storage.upload(frame, frame_key)
            row["source_key"] = frame_key
        return result


@activity.defn
def persist(video_id: str, speech: list[dict], slides: list[dict]):
    state(video_id, "index")
    with Session.begin() as session:
        video = session.get(Video, video_id)
        existing = session.scalar(select(Evidence.id).where(Evidence.video_id == video_id).limit(1))
        if existing:
            return
        for row in sorted(speech + slides, key=lambda x: x["start_ms"]):
            if not row["text"] or row["end_ms"] <= row["start_ms"]:
                continue
            session.add(Evidence(video_id=video_id, modality=row["modality"],
                start_ms=row["start_ms"], end_ms=min(row["end_ms"], video.duration_ms),
                text=row["text"], confidence=row.get("confidence"), raw=row.get("raw", {}),
                source_key=row.get("source_key"), parser_version=video.parser_version))
        video.status, video.stage = "complete", "complete"


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
    async with httpx.AsyncClient(timeout=10) as http:
        response = await http.post(target, content=body,
            headers={"Content-Type": "application/json", "X-Event-Id": event_id,
                     "X-Signature-SHA256": signature})
        response.raise_for_status()
    with Session.begin() as session:
        session.get(WebhookEvent, event_id).status = "delivered"


@workflow.defn
class VideoWorkflow:
    @workflow.run
    async def run(self, video_id: str, mode: str):
        retry = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=2))
        try:
            duration = await workflow.execute_activity(prepare, video_id,
                start_to_close_timeout=timedelta(minutes=15), retry_policy=retry)
            if mode == "smoke":
                await workflow.execute_activity(persist, args=[video_id, [], []],
                    start_to_close_timeout=timedelta(minutes=10), retry_policy=retry)
                await workflow.execute_activity(notify, video_id,
                    start_to_close_timeout=timedelta(seconds=30), retry_policy=retry)
                return
            speech, slides = await asyncio.gather(
                workflow.execute_activity(transcribe, video_id, start_to_close_timeout=timedelta(hours=3), retry_policy=retry),
                workflow.execute_activity(inspect_slides, args=[video_id, duration],
                    start_to_close_timeout=timedelta(hours=3), retry_policy=retry))
            await workflow.execute_activity(persist, args=[video_id, speech, slides],
                start_to_close_timeout=timedelta(minutes=10), retry_policy=retry)
        except Exception as exc:
            await workflow.execute_activity(fail, args=[video_id, str(exc)],
                start_to_close_timeout=timedelta(seconds=30), retry_policy=retry)
            raise
        await workflow.execute_activity(notify, video_id,
            start_to_close_timeout=timedelta(seconds=30), retry_policy=retry)


@activity.defn
def fail(video_id: str, message: str):
    state(video_id, "failed", "failed", message[:1000])
