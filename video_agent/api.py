"""FastAPI composition root; capability routes live in ``video_agent.routers``."""
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from temporalio.client import Client

from . import storage
from .auth import ensure_bootstrap_key
from .config import settings
from .db import init_db
from .errors import BusinessError
from .routers.admin import (STATIC_DIR, _metrics_access, favicon, health_live,
                            router as admin_router, workbench)
from .routers.analysis import router as analysis_router
from .routers.conversation import router as conversation_router
from .routers.upload import (UPLOAD_PART_SIZE, canonical_parts, expire_upload,
                             router as upload_router, validate_part_sizes)
from .routers.video import router as video_router
from .routers.artifacts import router as artifacts_router
from .routers.imports import router as imports_router
from .telemetry import HTTP_REQUESTS, HTTP_SECONDS, RedisSlidingWindowLimiter

logger = logging.getLogger("video_agent.api")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
limiter = RedisSlidingWindowLimiter(settings.redis_url, settings.rate_limit_key_prefix)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ensure_bootstrap_key(settings.api_key, settings.bootstrap_tenant_id)
    storage.ensure_bucket()
    app.state.temporal = await Client.connect(settings.temporal_address)
    yield


app = FastAPI(title="Video Evidence Agent", lifespan=lifespan)


@app.exception_handler(BusinessError)
async def business_error_handler(_request: Request, exc: BusinessError):
    return JSONResponse(status_code=exc.status_code, headers=exc.headers,
                        content={"error": {"code": exc.code, "message": exc.message}})


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


# Routers already contain their final prefixes. Extending keeps concrete
# APIRoute objects visible to route-introspection and policy tests.
for capability_router in (upload_router, imports_router, artifacts_router, video_router, conversation_router,
                          analysis_router, admin_router):
    app.router.routes.extend(capability_router.routes)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
