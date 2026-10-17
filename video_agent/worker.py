import asyncio
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import UnsandboxedWorkflowRunner, Worker
from prometheus_client import start_http_server

from .config import settings
from .db import init_db
from .workflow import (VideoWorkflow, acquire_tenant_slot, fail, index_evidence,
                       inspect_slides, notify, persist, prepare, transcribe)


async def main():
    init_db()
    start_http_server(settings.worker_metrics_port, addr="0.0.0.0")
    client = await Client.connect(settings.temporal_address)
    with (ThreadPoolExecutor(max_workers=4) as control_pool,
          ThreadPoolExecutor(max_workers=settings.asr_concurrency) as asr_pool,
          ThreadPoolExecutor(max_workers=settings.ocr_concurrency) as ocr_pool):
        control = Worker(client, task_queue="video-control", workflows=[VideoWorkflow],
            activities=[acquire_tenant_slot, prepare, persist, index_evidence, notify, fail],
            workflow_runner=UnsandboxedWorkflowRunner(), activity_executor=control_pool,
            max_concurrent_activities=4)
        asr = Worker(client, task_queue="video-asr", activities=[transcribe],
            activity_executor=asr_pool, max_concurrent_activities=settings.asr_concurrency)
        ocr = Worker(client, task_queue="video-ocr", activities=[inspect_slides],
            activity_executor=ocr_pool, max_concurrent_activities=settings.ocr_concurrency)
        # Keep the previous queue alive while pre-upgrade workflow histories drain.
        legacy = Worker(client, task_queue="video-jobs", workflows=[VideoWorkflow],
            activities=[acquire_tenant_slot, prepare, transcribe, inspect_slides, persist,
                        index_evidence, notify, fail],
            workflow_runner=UnsandboxedWorkflowRunner(), activity_executor=control_pool,
            max_concurrent_activities=4)
        await asyncio.gather(control.run(), asr.run(), ocr.run(), legacy.run())


if __name__ == "__main__":
    asyncio.run(main())
