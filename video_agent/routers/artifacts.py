import asyncio
import json
import tempfile
from pathlib import Path

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from sqlalchemy import select

from .. import media, storage
from ..auth import require_scopes
from ..config import settings
from ..db import Evidence, Session, Video
from ..workflow import cached_source

router = APIRouter(prefix="/videos", tags=["Artifacts"])


def owned_video(session, video_id: str, tenant_id: str) -> Video:
    video = session.get(Video, video_id)
    if not video or video.tenant_id != tenant_id:
        raise HTTPException(404, "video not found")
    return video


def build_audio(video_id: str) -> None:
    try:
        with Session() as session:
            video = session.get(Video, video_id)
            source, version = video.object_key, video.parser_version
        path = cached_source(video_id, source)
        with tempfile.TemporaryDirectory() as folder:
            output = str(Path(folder) / "audio.mp3")
            media.run("ffmpeg", "-nostdin", "-y", "-i", path, "-vn", "-codec:a",
                      "libmp3lame", "-q:a", "3", output)
            key = f"artifacts/{video_id}/{version}/audio.mp3"
            storage.upload(output, key)
        with Session.begin() as session:
            video = session.get(Video, video_id)
            video.audio_key, video.audio_version, video.audio_status = key, version, "ready"
    except Exception as exc:
        with Session.begin() as session:
            video = session.get(Video, video_id)
            if video:
                video.audio_status = "failed"
                video.error = f"音频生成失败：{str(exc)[:800]}"


@router.post("/{video_id}/audio", status_code=202)
def create_audio(video_id: str, tasks: BackgroundTasks,
                 tenant_id: str = Depends(require_scopes("videos:write"))):
    with Session.begin() as session:
        video = owned_video(session, video_id, tenant_id)
        if video.status not in {"ready", "complete"}:
            raise HTTPException(409, "video is not ready")
        if video.audio_status == "ready" and video.audio_version == video.parser_version:
            return {"status": "ready"}
        if video.audio_status != "processing":
            video.audio_status = "processing"
            tasks.add_task(build_audio, video_id)
        return {"status": video.audio_status}


@router.get("/{video_id}/audio")
def get_audio(video_id: str, tenant_id: str = Depends(require_scopes("videos:read"))):
    with Session() as session:
        video = owned_video(session, video_id, tenant_id)
        if video.audio_status != "ready" or not video.audio_key:
            return {"status": video.audio_status}
        return {"status": "ready", "download_url": storage.playback_url(video.audio_key)}


async def request_summary(title: str, rows: list[Evidence]) -> dict:
    transcript = [{"start_ms": row.start_ms, "end_ms": row.end_ms, "text": row.text}
                  for row in rows]
    payload = {"model": settings.model_name, "max_tokens": settings.llm_max_output_tokens,
        "messages": [
            {"role": "system", "content": "仅依据文字稿生成中文学习总结。输出 JSON，字段为 chapters、key_points、conclusion；chapters 每项含 title、start_ms、summary。不得添加文字稿之外的信息。"},
            {"role": "user", "content": json.dumps({"title": title, "transcript": transcript}, ensure_ascii=False)}],
        "response_format": {"type": "json_object"}}
    async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
        response = await client.post(settings.model_api_url, json=payload,
            headers={"Authorization": f"Bearer {settings.model_api_key}"})
        response.raise_for_status()
    result = json.loads(response.json()["choices"][0]["message"]["content"])
    if not isinstance(result.get("chapters"), list) or not isinstance(result.get("key_points"), list):
        raise ValueError("model returned an invalid summary")
    return result


@router.post("/{video_id}/summary")
async def create_summary(video_id: str, regenerate: bool = False,
                         tenant_id: str = Depends(require_scopes("videos:write"))):
    if not settings.model_api_url or not settings.model_api_key:
        raise HTTPException(503, "AI 总结未配置，请先配置模型服务")
    with Session.begin() as session:
        video = owned_video(session, video_id, tenant_id)
        if video.status not in {"ready", "complete"}:
            raise HTTPException(409, "video is not ready")
        if not regenerate and video.summary_status == "ready" and video.summary_version == video.parser_version:
            return video.summary_data
        rows = list(session.scalars(select(Evidence).where(
            Evidence.video_id == video_id, Evidence.modality == "speech").order_by(Evidence.start_ms)))
        if not rows:
            raise HTTPException(409, "transcript is empty")
        title, version = video.original_title or video.filename, video.parser_version
        video.summary_status = "processing"
    try:
        result = await request_summary(title, rows)
    except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError) as exc:
        with Session.begin() as session:
            owned_video(session, video_id, tenant_id).summary_status = "failed"
        raise HTTPException(502, "AI 总结生成失败，请稍后重试") from exc
    with Session.begin() as session:
        video = owned_video(session, video_id, tenant_id)
        video.summary_data, video.summary_version, video.summary_status = result, version, "ready"
    return result


@router.get("/{video_id}/summary")
def get_summary(video_id: str, tenant_id: str = Depends(require_scopes("videos:read"))):
    with Session() as session:
        video = owned_video(session, video_id, tenant_id)
        return {"status": video.summary_status, "summary": video.summary_data}
