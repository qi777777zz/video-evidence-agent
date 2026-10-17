from typing import Annotated, Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


Concurrency = Annotated[int, Field(ge=1, le=64)]
RatePerMinute = Annotated[int, Field(ge=1, le=10_000)]
TimeoutSeconds = Annotated[float, Field(ge=0.1, le=300)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg://video:video@localhost:5432/video"
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "videos"
    web_origin: str = "http://localhost:8000"
    temporal_address: str = "localhost:7233"
    redis_url: str = "redis://localhost:6379/0"
    rate_limit_key_prefix: str = "video-agent:rate"
    api_key: str = ""  # Deprecated; imported once as a bootstrap key.
    bootstrap_tenant_id: str = "demo"
    metrics_token: str = ""
    metrics_allowed_networks: str = "127.0.0.0/8,::1/128"
    worker_metrics_port: int = 9091
    webhook_secret: str = "change-me"
    webhook_timeout_seconds: TimeoutSeconds = 10
    webhook_max_response_bytes: int = 64 * 1024
    model_api_url: str = ""
    model_api_key: str = ""
    model_name: str = ""
    max_video_bytes: int = 2 * 1024**3
    max_video_seconds: Annotated[int, Field(ge=1, le=24 * 3600)] = 3 * 3600
    tenant_concurrency: Concurrency = 2
    tenant_queue_quota: Annotated[int, Field(ge=1, le=10_000)] = 20
    dispatch_interval_seconds: Annotated[int, Field(ge=1, le=300)] = 5
    asr_concurrency: Concurrency = 1
    ocr_concurrency: Concurrency = 2
    llm_concurrency: Concurrency = 2
    upload_rate_per_minute: RatePerMinute = 30
    submit_rate_per_minute: RatePerMinute = 10
    question_rate_per_minute: RatePerMinute = 60
    rate_limit_retry_seconds: Annotated[int, Field(ge=1, le=3600)] = 60
    max_evidence_count: Annotated[int, Field(ge=1, le=100)] = 20
    llm_timeout_seconds: TimeoutSeconds = 30
    llm_max_output_tokens: int = 800
    parser_version: str = "v1"
    processing_mode: str = "full"  # smoke validates the upload/workflow path without ML downloads
    asr_model: str = "small"
    ocr_model: str = "rapidocr-onnxruntime"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dimension: Literal[512] = 512
    embedding_cache_dir: str = "D:/anew/models"
    media_cache_dir: str = ""
    media_cache_max_bytes: Annotated[int, Field(ge=64 * 1024**2, le=1024**4)] = 8 * 1024**3
    retrieval_keyword_weight: float = 0.55
    retrieval_vector_weight: float = 0.45
    retrieval_vector_threshold: float = 0.58
    lifecycle_interval_seconds: Annotated[int, Field(ge=60, le=86400)] = 3600
    incomplete_upload_retention_hours: Annotated[int, Field(ge=1, le=720)] = 24
    failed_job_retention_days: Annotated[int, Field(ge=1, le=3650)] = 7
    original_retention_days: Annotated[int, Field(ge=1, le=3650)] = 90
    evidence_retention_days: Annotated[int, Field(ge=1, le=3650)] = 90
    orphan_object_grace_hours: Annotated[int, Field(ge=1, le=720)] = 24

    @model_validator(mode="after")
    def reject_unsafe_production_defaults(self):
        if self.app_environment != "production":
            return self
        unsafe = []
        if self.s3_access_key == "minioadmin" or self.s3_secret_key == "minioadmin":
            unsafe.append("S3 credentials")
        if self.webhook_secret in {"", "change-me"}:
            unsafe.append("WEBHOOK_SECRET")
        if not self.metrics_token:
            unsafe.append("METRICS_TOKEN")
        if "dev-redis-password" in self.redis_url or ":6379/0" in self.redis_url and "@" not in self.redis_url:
            unsafe.append("REDIS_URL authentication")
        if unsafe:
            raise ValueError("unsafe production configuration: " + ", ".join(unsafe))
        return self

    def pipeline_version(self) -> str:
        if self.processing_mode == "smoke":
            return f"{self.parser_version}-smoke"
        models = f"asr={self.asr_model};ocr={self.ocr_model};embed={self.embedding_model}"
        return f"{self.parser_version};{models}"


settings = Settings()
