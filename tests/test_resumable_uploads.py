from datetime import timedelta

import pytest
from fastapi import HTTPException

from video_agent import storage
from video_agent.api import UPLOAD_PART_SIZE, canonical_parts, expire_upload, validate_part_sizes
from video_agent.db import Upload, utcnow


class FakePaginator:
    def paginate(self, **_kwargs):
        return [
            {"Parts": [{"PartNumber": 2, "ETag": "second", "Size": 4}]},
            {"Parts": [{"PartNumber": 1, "ETag": "first", "Size": 5},
                       {"PartNumber": 2, "ETag": "new-second", "Size": 4}]},
        ]


class FakeMultipartClient:
    def get_paginator(self, name):
        assert name == "list_parts"
        return FakePaginator()


def test_list_parts_paginates_sorts_and_deduplicates(monkeypatch):
    monkeypatch.setattr(storage, "client", lambda: FakeMultipartClient())
    assert storage.list_parts("key", "upload") == [
        {"PartNumber": 1, "ETag": "first", "Size": 5},
        {"PartNumber": 2, "ETag": "new-second", "Size": 4},
    ]


def test_canonical_parts_keeps_latest_retransmission():
    parts = canonical_parts([
        {"PartNumber": 1, "ETag": "old"}, {"PartNumber": 1, "ETag": "new"},
        {"PartNumber": 2, "ETag": "two"},
    ], 2)
    assert parts == [{"PartNumber": 1, "ETag": "new"},
                     {"PartNumber": 2, "ETag": "two"}]


def test_canonical_parts_rejects_gaps():
    with pytest.raises(HTTPException) as error:
        canonical_parts([{"PartNumber": 2, "ETag": "two"}], 2)
    assert error.value.status_code == 400


def test_part_sizes_are_checked_before_completion():
    parts = [{"PartNumber": 1, "ETag": "one", "Size": UPLOAD_PART_SIZE},
             {"PartNumber": 2, "ETag": "two", "Size": 3}]
    validate_part_sizes(parts, UPLOAD_PART_SIZE + 3)
    parts[0]["Size"] -= 1
    with pytest.raises(HTTPException) as error:
        validate_part_sizes(parts, UPLOAD_PART_SIZE + 3)
    assert error.value.status_code == 400


def test_expired_upload_is_aborted(monkeypatch):
    aborted = []
    monkeypatch.setattr(storage, "abort_multipart", lambda key, upload_id: aborted.append((key, upload_id)))
    upload = Upload(status="open", object_key="key", multipart_id="multipart", declared_bytes=1,
                    created_at=utcnow() - timedelta(days=2))
    expire_upload(upload)
    assert upload.status == "expired"
    assert aborted == [("key", "multipart")]
