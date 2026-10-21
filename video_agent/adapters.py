"""Infrastructure adapters. Vendor SDKs are confined to this module."""
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError

from . import storage
from .agent import answer_candidates
from .retrieval import search
from .telemetry import RedisSlidingWindowLimiter
from .workflow import VideoWorkflow


class MinioAdapter:
    ensure_bucket = staticmethod(storage.ensure_bucket)
    create_multipart = staticmethod(storage.create_multipart)
    presign_part = staticmethod(storage.presign_part)
    list_parts = staticmethod(storage.list_parts)
    complete_multipart = staticmethod(storage.complete_multipart)
    abort_multipart = staticmethod(storage.abort_multipart)
    object_exists = staticmethod(storage.object_exists)
    object_size = staticmethod(storage.object_size)
    delete_many = staticmethod(storage.delete_many)
    playback_url = staticmethod(storage.playback_url)

    def health(self) -> bool:
        storage.client().head_bucket(Bucket=storage.settings.s3_bucket)
        return True


class TemporalAdapter:
    def __init__(self, client: Client):
        self.client = client

    @classmethod
    async def connect(cls, address: str):
        return cls(await Client.connect(address))

    async def dispatch_video(self, video_id: str, mode: str) -> None:
        from .db import Session, Video
        with Session() as session:
            run = session.get(Video, video_id).workflow_run
        try:
            await self.client.start_workflow(VideoWorkflow.run, args=[video_id, mode],
            id=f"video-{video_id}-r{run}", task_queue="video-control")
        except WorkflowAlreadyStartedError:
            return

    async def cancel_video(self, video_id: str) -> None:
        from .db import Session, Video
        with Session() as session:
            run = session.get(Video, video_id).workflow_run
        await self.client.get_workflow_handle(f"video-{video_id}-r{run}").cancel()

    async def health(self) -> bool:
        return bool(await self.client.service_client.check_health())


class RedisAdapter:
    def __init__(self, url: str, prefix: str):
        self._limiter = RedisSlidingWindowLimiter(url, prefix)

    def check(self, tenant: str, operation: str, limit: int) -> None:
        self._limiter.check(tenant, operation, limit)

    def health(self) -> bool:
        return bool(self._limiter.client.ping())


class ModelAdapter:
    async def answer(self, candidates, question: str) -> dict:
        return await answer_candidates(candidates, question)


class QdrantAdapter:
    """Vector-search adapter backed by MySQL JSON vectors and application-side scoring."""
    def search(self, session, video_id: str, query: str):
        return search(session, video_id, query)

