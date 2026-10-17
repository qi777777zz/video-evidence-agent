from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from video_agent.api import _metrics_access
from video_agent.config import settings
from video_agent.telemetry import observe_storage


def request(host="127.0.0.1", authorization=""):
    return SimpleNamespace(client=SimpleNamespace(host=host), headers={"Authorization": authorization})


def test_metrics_rejects_non_monitoring_network(monkeypatch):
    monkeypatch.setattr(settings, "metrics_allowed_networks", "127.0.0.0/8")
    monkeypatch.setattr(settings, "metrics_token", "")
    with pytest.raises(HTTPException) as error:
        _metrics_access(request("10.0.0.2"))
    assert error.value.status_code == 403


def test_metrics_requires_configured_bearer_token(monkeypatch):
    monkeypatch.setattr(settings, "metrics_allowed_networks", "127.0.0.0/8")
    monkeypatch.setattr(settings, "metrics_token", "secret")
    with pytest.raises(HTTPException) as error:
        _metrics_access(request())
    assert error.value.status_code == 401
    _metrics_access(request(authorization="Bearer secret"))


def test_storage_instrumentation_preserves_result_and_errors():
    assert observe_storage("test", lambda: 42) == 42
    with pytest.raises(RuntimeError):
        observe_storage("test", lambda: (_ for _ in ()).throw(RuntimeError("failed")))
