from contextlib import suppress
from datetime import timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from .. import storage
from ..auth import require_scopes
from ..config import settings
from ..db import Session, Upload, Video, utcnow

router = APIRouter(prefix="/uploads", tags=["Upload"])
UPLOAD_PART_SIZE = 16 * 1024 * 1024


class UploadStart(BaseModel):
    filename: str
    size_bytes: int = Field(gt=0)


class UploadComplete(BaseModel):
    parts: list[dict]


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


def canonical_parts(parts: list[dict], expected: int) -> list[dict]:
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
    if not parts or any("Size" not in part for part in parts):
        return
    expected_count = (declared_bytes + UPLOAD_PART_SIZE - 1) // UPLOAD_PART_SIZE
    last_size = declared_bytes - UPLOAD_PART_SIZE * (expected_count - 1)
    for part in parts:
        expected_size = last_size if part["PartNumber"] == expected_count else UPLOAD_PART_SIZE
        if int(part["Size"]) != expected_size:
            raise HTTPException(400, f"uploaded part {part['PartNumber']} has unexpected size")


@router.post("")
def start_upload(request: UploadStart, tenant_id: str = Depends(require_scopes("uploads:write"))):
    from ..api import limiter
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
                           filename=request.filename,
                           created_at=created_at))
    return {"upload_id": upload_id, "part_size": UPLOAD_PART_SIZE,
            "expires_at": (created_at + timedelta(hours=settings.incomplete_upload_retention_hours)).isoformat()}


@router.get("/{upload_id}")
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


@router.get("/{upload_id}/parts/{part_number}")
def upload_part(upload_id: str, part_number: int,
                tenant_id: str = Depends(require_scopes("uploads:write"))):
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
        return {"url": storage.presign_part(upload.object_key, upload.multipart_id, part_number)}


@router.post("/{upload_id}/complete")
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
            storage.complete_multipart(key, multipart_id,
                                       canonical_parts(listed_parts or request.parts, expected))
        if storage.object_size(key) != declared:
            storage.delete_many([key])
            with Session.begin() as session:
                row = session.get(Upload, upload_id)
                if row:
                    row.status = "invalid"
            raise HTTPException(400, "uploaded size mismatch; object removed")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, "upload finalization interrupted; retry safely") from exc
    with Session.begin() as session:
        upload = session.get(Upload, upload_id)
        if not upload or upload.tenant_id != tenant_id:
            raise HTTPException(404, "upload not found")
        upload.status, upload.completed_at = "complete", utcnow()
    return {"upload_id": upload_id, "status": "complete"}


@router.delete("/{upload_id}", status_code=204)
def delete_upload(upload_id: str, tenant_id: str = Depends(require_scopes("uploads:write"))):
    from fastapi.responses import Response
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
                storage.abort_multipart(key, multipart_id)
            except Exception as exc:
                code = getattr(exc, "response", {}).get("Error", {}).get("Code")
                if code not in {"404", "NoSuchUpload", "NotFound"}:
                    raise
        storage.delete_many([key])
    except Exception as exc:
        raise HTTPException(503, "upload deletion failed; retry safely") from exc
    with Session.begin() as session:
        upload = session.get(Upload, upload_id)
        if upload and upload.tenant_id == tenant_id:
            session.delete(upload)
    return Response(status_code=204)
