import pytest
from fastapi import HTTPException

from video_agent.api import tenant
from video_agent.config import settings


@pytest.mark.parametrize("tenant_id", ["", "/absolute", "../escape", "has/slash", "line\nbreak", "x" * 81])
def test_tenant_rejects_unsafe_identifiers(tenant_id):
    with pytest.raises(HTTPException) as error:
        tenant(settings.api_key, tenant_id)
    assert error.value.status_code == 400


@pytest.mark.parametrize("tenant_id", ["demo", "team-42", "org.example_1"])
def test_tenant_accepts_safe_identifiers(tenant_id):
    assert tenant(settings.api_key, tenant_id) == tenant_id


def test_tenant_rejects_wrong_api_key_first():
    with pytest.raises(HTTPException) as error:
        tenant("wrong", "demo")
    assert error.value.status_code == 401
