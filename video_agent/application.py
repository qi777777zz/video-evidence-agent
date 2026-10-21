"""Dependency-injected application container."""
from dataclasses import dataclass
from typing import Callable

from .adapters import MinioAdapter, ModelAdapter, QdrantAdapter, RedisAdapter, TemporalAdapter
from .config import Settings
from .ports import AnswerModel, ObjectStorage, RateLimiter, VectorIndex, WorkflowDispatcher
from .repositories import SqlAlchemyUnitOfWork, UnitOfWork


@dataclass(slots=True)
class Services:
    settings: Settings
    uow_factory: Callable[[], UnitOfWork]
    objects: ObjectStorage
    workflows: WorkflowDispatcher
    rate_limiter: RateLimiter
    model: AnswerModel
    vectors: VectorIndex


async def build_services(settings: Settings) -> Services:
    """Production composition root; tests pass ``Services`` containing fakes."""
    return Services(
        settings=settings,
        uow_factory=SqlAlchemyUnitOfWork,
        objects=MinioAdapter(),
        workflows=await TemporalAdapter.connect(settings.temporal_address),
        rate_limiter=RedisAdapter(settings.redis_url, settings.rate_limit_key_prefix),
        model=ModelAdapter(),
        vectors=QdrantAdapter(),
    )
