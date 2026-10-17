"""One-shot dependency bootstrap used by Docker Compose."""

import time

from botocore.exceptions import BotoCoreError, ClientError

from . import storage
from .config import settings


def main(attempts: int = 60, delay: float = 2.0) -> None:
    """Wait for object storage and create the configured bucket idempotently."""
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            storage.ensure_bucket()
            storage.client().head_bucket(Bucket=settings.s3_bucket)
            print(f"Object storage is ready; bucket {settings.s3_bucket!r} exists.")
            return
        except (BotoCoreError, ClientError, OSError) as exc:
            last_error = exc
            time.sleep(delay)
    raise RuntimeError("object storage did not become ready") from last_error


if __name__ == "__main__":
    main()
