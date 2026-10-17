from video_agent.api import health_live


def test_liveness_has_no_dependency_or_secret_details():
    assert health_live() == {"status": "ok"}
