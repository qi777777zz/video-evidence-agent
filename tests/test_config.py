import pytest
from pydantic import ValidationError

from video_agent.config import Settings


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant_concurrency", 0),
        ("tenant_queue_quota", 0),
        ("tenant_queue_quota", 10_001),
        ("asr_concurrency", 65),
        ("ocr_concurrency", -1),
        ("llm_concurrency", 65),
        ("upload_rate_per_minute", 0),
        ("submit_rate_per_minute", 10_001),
        ("question_rate_per_minute", -1),
        ("rate_limit_retry_seconds", 0),
        ("rate_limit_retry_seconds", 3601),
        ("webhook_timeout_seconds", 0),
        ("llm_timeout_seconds", 300.1),
        ("max_evidence_count", 0),
        ("max_evidence_count", 101),
        ("max_video_seconds", 0),
        ("max_video_seconds", 86_401),
    ],
)
def test_settings_reject_unsafe_resource_limits(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_settings_accept_boundary_values():
    configured = Settings(
        _env_file=None,
        tenant_concurrency=1,
        asr_concurrency=64,
        upload_rate_per_minute=1,
        submit_rate_per_minute=10_000,
        webhook_timeout_seconds=0.1,
        llm_timeout_seconds=300,
        rate_limit_retry_seconds=3600,
        max_evidence_count=100,
        max_video_seconds=86_400,
    )

    assert configured.tenant_concurrency == 1
    assert configured.asr_concurrency == 64
    assert configured.upload_rate_per_minute == 1
    assert configured.submit_rate_per_minute == 10_000
    assert configured.webhook_timeout_seconds == 0.1
    assert configured.llm_timeout_seconds == 300
    assert configured.rate_limit_retry_seconds == 3600
    assert configured.max_evidence_count == 100
    assert configured.max_video_seconds == 86_400


def test_production_rejects_development_secrets():
    with pytest.raises(ValidationError, match="unsafe production configuration"):
        Settings(_env_file=None, app_environment="production")


def test_embedding_dimension_matches_database_schema():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, embedding_dimension=768)
