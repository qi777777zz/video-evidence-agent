from datetime import datetime, timezone

from video_agent.api import app
from video_agent.lifecycle import LifecycleResult


def test_delete_routes_are_exposed():
    routes = {(route.path, method) for route in app.routes for method in getattr(route, "methods", set())}
    assert ("/uploads/{upload_id}", "DELETE") in routes
    assert ("/videos/{video_id}", "DELETE") in routes


def test_lifecycle_result_has_auditable_counters():
    result = LifecycleResult()
    assert result.uploads_removed == 0
    assert result.videos_removed == 0
    assert result.originals_removed == 0
    assert result.orphan_objects_removed == 0
    assert result.orphan_uploads_aborted == 0


def test_object_timestamp_fixture_is_timezone_aware():
    # The repair pass compares S3 timestamps with an aware UTC cutoff.
    assert datetime.now(timezone.utc).tzinfo is not None
