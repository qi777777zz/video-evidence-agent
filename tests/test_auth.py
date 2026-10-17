from video_agent.auth import hash_secret, issue_key, verify_secret
from video_agent.api import app


def test_secret_hash_is_salted_and_verifiable():
    first = hash_secret("same-secret")
    second = hash_secret("same-secret")
    assert first != second
    assert "same-secret" not in first
    assert verify_secret("same-secret", first)
    assert not verify_secret("wrong", first)


def test_issued_key_stores_no_plaintext_and_validates_scopes():
    record, plaintext = issue_key(tenant_id="tenant-1", name="ci",
                                  scopes=["videos:read", "videos:read"])
    secret = plaintext.split(".", 1)[1]
    assert record.scopes == ["videos:read"]
    assert secret not in record.secret_hash
    assert verify_secret(secret, record.secret_hash)


def test_every_business_route_enforces_the_expected_scope():
    expected = {
        ("/uploads", "POST"): "uploads:write",
        ("/uploads/{upload_id}", "GET"): "uploads:write",
        ("/uploads/{upload_id}/parts/{part_number}", "GET"): "uploads:write",
        ("/uploads/{upload_id}/complete", "POST"): "uploads:write",
        ("/videos", "POST"): "videos:write",
        ("/jobs/{job_id}", "GET"): "videos:read",
        ("/videos/{video_id}", "DELETE"): "videos:write",
        ("/videos/{video_id}/questions", "POST"): "questions:ask",
    }
    actual = {}
    for route in app.routes:
        if not hasattr(route, "dependant"):
            continue
        dependencies = route.dependant.dependencies
        for method in route.methods or set():
            key = (route.path, method)
            if key not in expected:
                continue
            scopes = set()
            for dependency in dependencies:
                for cell in dependency.call.__closure__ or ():
                    if isinstance(cell.cell_contents, frozenset):
                        scopes.update(cell.cell_contents)
            actual[key] = scopes
    assert actual == {key: {scope} for key, scope in expected.items()}
