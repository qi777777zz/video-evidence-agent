import socket

import pytest

from video_agent.routers.imports import validate_public_platform_url
from video_agent.routers.video import srt_time


def test_srt_time_uses_millisecond_timestamp():
    assert srt_time(3_723_045) == "01:02:03,045"


@pytest.mark.parametrize("url", [
    "http://www.bilibili.com/video/BV1", "https://example.com/video",
    "https://127.0.0.1/video", "file:///etc/passwd",
])
def test_remote_import_rejects_unsupported_or_unsafe_urls(url):
    with pytest.raises(ValueError):
        validate_public_platform_url(url)


def test_remote_import_rejects_non_public_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ValueError, match="非公开"):
        validate_public_platform_url("https://www.bilibili.com/video/BV1")


def test_remote_import_accepts_allowed_platform_with_public_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
    assert validate_public_platform_url("https://v.douyin.com/example/").startswith("https://")
