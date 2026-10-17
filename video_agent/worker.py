import asyncio
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from .config import settings
from .db import init_db
from .workflow import VideoWorkflow, fail, index_evidence, inspect_slides, notify, persist, prepare, transcribe


async def main():
    init_db()
    client = await Client.connect(settings.temporal_address)
    with ThreadPoolExecutor(max_workers=settings.asr_concurrency + settings.ocr_concurrency + 2) as pool:
        worker = Worker(client, task_queue="video-jobs", workflows=[VideoWorkflow],
            activities=[prepare, transcribe, inspect_slides, persist, index_evidence, notify, fail],
            workflow_runner=UnsandboxedWorkflowRunner(),
            activity_executor=pool, max_concurrent_activities=settings.asr_concurrency + settings.ocr_concurrency + 2)
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
