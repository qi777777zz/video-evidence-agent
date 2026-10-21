import asyncio
import hashlib
import importlib
import ipaddress
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import imageio_ffmpeg
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import func, select
from pydantic import BaseModel, HttpUrl

from .. import storage
from ..auth import require_scopes
from ..config import settings
from ..db import Session, Submission, Video
from .video import dispatch_video, effective_parser_version

router = APIRouter(prefix="/imports", tags=["Import"])
ALLOWED_HOSTS = {"bilibili.com", "www.bilibili.com", "b23.tv", "douyin.com", "www.douyin.com",
                 "v.douyin.com"}


class ImportRequest(BaseModel):
    url: HttpUrl


def validate_public_platform_url(value: str) -> str:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or host not in ALLOWED_HOSTS or parsed.username or parsed.password:
        raise ValueError("仅支持公开的 B站或抖音 HTTPS 链接")
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError("无法解析视频网址")
    for item in addresses:
        address = ipaddress.ip_address(item[4][0])
        if not address.is_global:
            raise ValueError("视频网址解析到非公开网络地址")
    return value


def download_remote(video_id: str, url: str) -> tuple[str, dict]:
    try:
        yt_dlp = importlib.import_module("yt_dlp")
    except ImportError as exc:
        raise RuntimeError("远程导入组件未安装") from exc
    with tempfile.TemporaryDirectory() as folder:
        target = str(Path(folder) / "source.%(ext)s")
        options = {"outtmpl": target, "format": "bestvideo+bestaudio/best", "merge_output_format": "mp4",
                   "noplaylist": True, "max_filesize": settings.max_video_bytes,
                   "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
                   "socket_timeout": settings.remote_import_timeout_seconds,
                   "retries": 2, "quiet": True, "no_warnings": True}
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=True)
            if info.get("duration") and info["duration"] > settings.max_video_seconds:
                raise ValueError("视频时长超过限制")
            prepared = Path(downloader.prepare_filename(info))
            candidates = list(Path(folder).glob("source.*"))
            path = next((item for item in candidates if item.suffix == ".mp4"), prepared)
        if not path.is_file() or path.stat().st_size > settings.max_video_bytes:
            raise ValueError("视频文件为空或超过大小限制")
        digest = hashlib.file_digest(path.open("rb"), "sha256").hexdigest()
        key = f"originals/remote/{video_id}"
        storage.upload(str(path), key)
        return digest, {"key": key, "title": str(info.get("title") or "远程视频")[:500],
                        "thumbnail": info.get("thumbnail"), "filename": f"{info.get('id', video_id)}.mp4"}


async def run_import(app_request: Request, video_id: str, url: str):
    try:
        digest, result = await asyncio.to_thread(download_remote, video_id, url)
        with Session.begin() as session:
            video = session.get(Video, video_id)
            if video.status == "cancelled":
                storage.delete_many([result["key"]])
                return
            video.sha256, video.object_key, video.filename = digest, result["key"], result["filename"]
            video.original_title, video.thumbnail_url = result["title"], result["thumbnail"]
            video.status, video.stage, video.progress = "queued", "dispatch_pending", 0
        await dispatch_video(app_request, video_id)
    except Exception as exc:
        with Session.begin() as session:
            video = session.get(Video, video_id)
            if video:
                video.status, video.stage, video.error = "failed", "importing", str(exc)[:1000]


@router.post("", status_code=202)
async def create_import(body: ImportRequest, request: Request,
                        idempotency_key: str | None = Header(default=None, max_length=160),
                        tenant_id: str = Depends(require_scopes("videos:write"))):
    from ..api import limiter
    limiter.check(tenant_id, "submit", settings.submit_rate_per_minute)
    try:
        url = await asyncio.to_thread(validate_public_platform_url, str(body.url))
    except (ValueError, OSError, socket.gaierror) as exc:
        raise HTTPException(400, str(exc)) from exc
    request_hash = hashlib.sha256(url.encode()).hexdigest()
    with Session.begin() as session:
        if idempotency_key:
            prior = session.scalar(select(Submission).where(Submission.tenant_id == tenant_id,
                                                             Submission.idempotency_key == idempotency_key))
            if prior:
                if prior.request_hash != request_hash:
                    raise HTTPException(409, "idempotency key reused with different request")
                return {"job_id": prior.video_id, "status": "reused"}
        queued = session.scalar(select(func.count()).select_from(Video).where(
            Video.tenant_id == tenant_id, Video.status.in_(["uploaded", "queued", "validating", "processing"])))
        if queued >= settings.tenant_queue_quota:
            raise HTTPException(429, "tenant queue full", headers={"Retry-After": str(settings.rate_limit_retry_seconds)})
        identifier = str(uuid4())
        session.add(Video(id=identifier, tenant_id=tenant_id, object_key=f"pending/{identifier}",
            filename="", sha256="0" * 64, parser_version=effective_parser_version(),
            source_type="url", source_url=url, original_title="正在读取视频信息…",
            status="uploaded", stage="importing", progress=1))
        if idempotency_key:
            session.add(Submission(tenant_id=tenant_id, idempotency_key=idempotency_key,
                                   request_hash=request_hash, video_id=identifier))
    asyncio.create_task(run_import(request, identifier, url))
    return {"job_id": identifier, "status": "importing"}
