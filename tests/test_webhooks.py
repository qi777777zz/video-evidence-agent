import socket

import pytest

from video_agent.webhooks import UnsafeWebhook, resolve_public_webhook


@pytest.mark.parametrize("target", [
    "http://example.com/hook",
    "https://user:pass@example.com/hook",
    "https://example.com:8443/hook",
    "https://127.0.0.1/hook",
    "https://[::1]/hook",
    "https://10.1.2.3/hook",
])
def test_rejects_unsafe_webhook_urls(target):
    with pytest.raises(UnsafeWebhook):
        resolve_public_webhook(target)


def test_rejects_hostname_if_any_answer_is_private(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ])
    with pytest.raises(UnsafeWebhook, match="only to public"):
        resolve_public_webhook("https://hooks.example.test/event")


def test_accepts_hostname_with_only_public_answers(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
    ])
    assert resolve_public_webhook("https://hooks.example.test/event") == {"93.184.216.34"}
