import pytest
from video_agent.auth import issue_key


@pytest.mark.parametrize("tenant_id", ["", "/absolute", "../escape", "has/slash", "line\nbreak", "x" * 81])
def test_tenant_rejects_unsafe_identifiers(tenant_id):
    with pytest.raises(ValueError, match="invalid tenant"):
        issue_key(tenant_id=tenant_id, name="test", scopes=[])


@pytest.mark.parametrize("tenant_id", ["demo", "team-42", "org.example_1"])
def test_tenant_accepts_safe_identifiers(tenant_id):
    record, plaintext = issue_key(tenant_id=tenant_id, name="test", scopes=[])
    assert record.tenant_id == tenant_id
    assert plaintext.startswith(record.prefix + ".")
