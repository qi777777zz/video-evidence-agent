import hmac
import ipaddress
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import func, select

from .. import storage
from ..config import settings
from ..db import Session, Video, engine
from ..telemetry import (ACTIVE_JOBS, DB_POOL_CHECKED_IN, DB_POOL_CHECKED_OUT,
    DB_POOL_OVERFLOW, DB_POOL_SIZE, JOB_STAGES, METRICS_DB_REFRESH, METRICS_DB_UP)

router = APIRouter(tags=["Admin"])
logger = logging.getLogger("video_agent.routers.admin")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_metrics_snapshot = None


@router.get("/health/live", include_in_schema=False)
def health_live():
    return {"status": "ok"}


@router.get("/health/ready", include_in_schema=False)
async def health_ready(request: Request):
    from ..api import limiter
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
        healthy = await request.app.state.temporal.service_client.check_health()
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


def _metrics_access(request: Request):
    try:
        address = ipaddress.ip_address(request.client.host if request.client else "")
        networks = [ipaddress.ip_network(value.strip()) for value in
                    settings.metrics_allowed_networks.split(",") if value.strip()]
    except ValueError as exc:
        raise HTTPException(503, "metrics access is not configured") from exc
    if not any(address in network for network in networks):
        raise HTTPException(403, "metrics endpoint is restricted")
    if settings.metrics_token:
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied, f"Bearer {settings.metrics_token}"):
            raise HTTPException(401, "invalid metrics token", headers={"WWW-Authenticate": "Bearer"})


@router.get("/metrics", include_in_schema=False)
def metrics(request: Request):
    global _metrics_snapshot
    _metrics_access(request)
    try:
        with Session() as session:
            _metrics_snapshot = session.execute(select(Video.status, Video.stage, func.count()).group_by(
                Video.status, Video.stage)).all()
    except Exception:
        METRICS_DB_UP.set(0)
        METRICS_DB_REFRESH.labels("failure").inc()
        logger.warning("database metric refresh failed", exc_info=True)
    else:
        METRICS_DB_UP.set(1)
        METRICS_DB_REFRESH.labels("success").inc()
    if _metrics_snapshot is not None:
        ACTIVE_JOBS.set(sum(c for status, _, c in _metrics_snapshot if status in {"queued", "running"}))
        JOB_STAGES.clear()
        for status, stage, count in _metrics_snapshot:
            JOB_STAGES.labels(status, stage).set(count)
    pool = engine.pool
    for metric, getter in ((DB_POOL_SIZE, "size"), (DB_POOL_CHECKED_OUT, "checkedout"),
                           (DB_POOL_CHECKED_IN, "checkedin"), (DB_POOL_OVERFLOW, "overflow")):
        value = getattr(pool, getter, None)
        if callable(value):
            metric.set(value())
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/", include_in_schema=False)
def workbench():
    return FileResponse(STATIC_DIR / "index.html")


@router.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")
