import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..auth import require_scopes
from ..db import Session, Video

router = APIRouter(tags=["Analysis"])


@router.get("/jobs/{job_id}")
def get_job(job_id: str, tenant_id: str = Depends(require_scopes("videos:read"))):
    with Session() as session:
        video = session.get(Video, job_id)
        if not video or video.tenant_id != tenant_id:
            raise HTTPException(404, "job not found")
        return {"job_id": job_id, "status": video.status, "stage": video.stage,
                "asr_status": video.asr_status, "ocr_status": video.ocr_status,
                "progress": video.progress, "error": video.error, "duration_ms": video.duration_ms}


@router.get("/jobs/{job_id}/events")
def job_events(job_id: str, tenant_id: str = Depends(require_scopes("videos:read"))):
    async def stream():
        previous = None
        while True:
            with Session() as session:
                video = session.get(Video, job_id)
                if not video or video.tenant_id != tenant_id:
                    yield 'event: error\ndata: {"detail":"job not found"}\n\n'
                    return
                payload = {"job_id": video.id, "status": video.status, "stage": video.stage,
                           "progress": video.progress, "error": video.error}
            current = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if current != previous:
                yield f"data: {current}\n\n"
                previous = current
            if payload["status"] in {"ready", "failed", "cancelled"}:
                return
            await asyncio.sleep(1)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.post("/jobs/{job_id}/cancel", status_code=202)
async def cancel_job(job_id: str, request: Request,
                     tenant_id: str = Depends(require_scopes("videos:write"))):
    with Session.begin() as session:
        video = session.get(Video, job_id)
        if not video or video.tenant_id != tenant_id:
            raise HTTPException(404, "job not found")
        if video.status in {"ready", "failed", "cancelled"}:
            raise HTTPException(409, "job is not active")
        importing = video.stage == "importing"
        video.status, video.stage = ("cancelled", "cancelled") if importing else ("cancelling", "cancelling")
    if importing:
        return {"job_id": job_id, "status": "cancelled"}
    from ..adapters import TemporalAdapter
    await TemporalAdapter(request.app.state.temporal).cancel_video(job_id)
    return {"job_id": job_id, "status": "cancelling"}


@router.post("/jobs/{job_id}/retry", status_code=202)
async def retry_job(job_id: str, request: Request,
                    tenant_id: str = Depends(require_scopes("videos:write"))):
    with Session.begin() as session:
        video = session.get(Video, job_id)
        if not video or video.tenant_id != tenant_id:
            raise HTTPException(404, "job not found")
        if video.status not in {"failed", "cancelled"}:
            raise HTTPException(409, "only failed or cancelled jobs can be retried")
        import_retry = video.source_type == "url" and video.stage == "importing"
        source_url = video.source_url
        video.workflow_run += 1
        video.status, video.stage, video.error, video.progress = "retrying", "retrying", None, 0
    if import_retry:
        from .imports import run_import
        video_stage = "importing"
        with Session.begin() as session:
            session.get(Video, job_id).stage = video_stage
        asyncio.create_task(run_import(request, job_id, source_url))
    else:
        from ..routers.video import dispatch_video
        await dispatch_video(request, job_id)
    return {"job_id": job_id, "status": "retrying"}
