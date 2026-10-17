from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://video:video@localhost:5432/video"
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "videos"
    web_origin: str = "http://localhost:8000"
    temporal_address: str = "localhost:7233"
    api_key: str = "demo-key"
    webhook_secret: str = "change-me"
    webhook_timeout_seconds: float = 10
    webhook_max_response_bytes: int = 64 * 1024
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
    asr_model: str = "small"
    ocr_model: str = "rapidocr-onnxruntime"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dimension: int = 512
    embedding_cache_dir: str = "D:/anew/models"
    retrieval_keyword_weight: float = 0.55
    retrieval_vector_weight: float = 0.45
    retrieval_vector_threshold: float = 0.58

    def pipeline_version(self) -> str:
        if self.processing_mode == "smoke":
            return f"{self.parser_version}-smoke"
        models = f"asr={self.asr_model};ocr={self.ocr_model};embed={self.embedding_model}"
        return f"{self.parser_version};{models}"


settings = Settings()
