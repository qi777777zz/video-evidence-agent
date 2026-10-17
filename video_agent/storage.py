import boto3
from botocore.config import Config

from .config import settings
from .telemetry import observe_storage


def client():
    return boto3.client(
        "s3", endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        config=Config(signature_version="s3v4"),
    )


def ensure_bucket():
    s3 = client()
    try:
        observe_storage("head_bucket", s3.head_bucket, Bucket=settings.s3_bucket)
    except Exception:
        observe_storage("create_bucket", s3.create_bucket, Bucket=settings.s3_bucket)


def download(key: str, path: str):
    observe_storage("download", client().download_file, settings.s3_bucket, key, path)


def upload(path: str, key: str):
    observe_storage("upload", client().upload_file, path, settings.s3_bucket, key)


def create_multipart(key: str, content_type: str) -> str:
    result = observe_storage("create_multipart", client().create_multipart_upload,
                             Bucket=settings.s3_bucket, Key=key, ContentType=content_type)
    return result["UploadId"]


def presign_part(key: str, upload_id: str, part_number: int) -> str:
    return observe_storage("presign_upload_part", client().generate_presigned_url, "upload_part", Params={
        "Bucket": settings.s3_bucket, "Key": key, "UploadId": upload_id, "PartNumber": part_number
    }, ExpiresIn=900)


def complete_multipart(key: str, upload_id: str, parts: list[dict]):
    observe_storage("complete_multipart", client().complete_multipart_upload,
                    Bucket=settings.s3_bucket, Key=key, UploadId=upload_id,
                    MultipartUpload={"Parts": parts})


def list_parts(key: str, upload_id: str) -> list[dict]:
    """Return all uploaded parts in canonical order, including paginated results."""
    paginator = client().get_paginator("list_parts")
    parts: dict[int, dict] = {}
    for page in paginator.paginate(Bucket=settings.s3_bucket, Key=key, UploadId=upload_id):
        for part in page.get("Parts", []):
            number = int(part["PartNumber"])
            parts[number] = {"PartNumber": number, "ETag": part["ETag"],
                             "Size": int(part.get("Size", 0))}
    return [parts[number] for number in sorted(parts)]


def abort_multipart(key: str, upload_id: str) -> None:
    observe_storage("abort_multipart", client().abort_multipart_upload,
                    Bucket=settings.s3_bucket, Key=key, UploadId=upload_id)


def object_size(key: str) -> int:
    result = observe_storage("head_object", client().head_object,
                             Bucket=settings.s3_bucket, Key=key)
    return result["ContentLength"]


def delete_many(keys: list[str]):
    """Delete keys idempotently, in S3's maximum batch size."""
    s3 = client()
    unique = list(dict.fromkeys(key for key in keys if key))
    for offset in range(0, len(unique), 1000):
        batch = unique[offset:offset + 1000]
        response = observe_storage("delete", s3.delete_objects, Bucket=settings.s3_bucket,
                                   Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True})
        if response.get("Errors"):
            raise RuntimeError(f"object deletion failed: {response['Errors']!r}")


def object_exists(key: str) -> bool:
    try:
        client().head_object(Bucket=settings.s3_bucket, Key=key)
        return True
    except client().exceptions.ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def playback_url(key: str) -> str:
    return observe_storage("presign", client().generate_presigned_url,
        "get_object", Params={"Bucket": settings.s3_bucket, "Key": key}, ExpiresIn=900
    )
