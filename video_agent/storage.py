import boto3
from botocore.config import Config

from .config import settings


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
        s3.head_bucket(Bucket=settings.s3_bucket)
    except Exception:
        s3.create_bucket(Bucket=settings.s3_bucket)


def download(key: str, path: str):
    client().download_file(settings.s3_bucket, key, path)


def upload(path: str, key: str):
    client().upload_file(path, settings.s3_bucket, key)


def playback_url(key: str) -> str:
    return client().generate_presigned_url(
        "get_object", Params={"Bucket": settings.s3_bucket, "Key": key}, ExpiresIn=900
    )
