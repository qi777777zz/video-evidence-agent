from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://video:video@localhost:5432/video"
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "videos"
    temporal_address: str = "localhost:7233"
    api_key: str = "demo-key"
    webhook_secret: str = "change-me"
    model_api_url: str = ""
    model_api_key: str = ""
    model_name: str = ""
    max_video_bytes: int = 2 * 1024**3
    max_video_seconds: int = 3 * 3600
    tenant_concurrency: int = 2
    asr_concurrency: int = 1
    ocr_concurrency: int = 2
    llm_concurrency: int = 2
    parser_version: str = "v1"
    processing_mode: str = "full"  # smoke validates the upload/workflow path without ML downloads


settings = Settings()
