from types import SimpleNamespace

from video_agent import agent


def run_without_io(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    raise AssertionError("the unit-test path unexpectedly performed async I/O")


def test_answer_abstains_without_evidence(monkeypatch):
    monkeypatch.setattr(agent, "search", lambda *_args: [])
    video = SimpleNamespace(id="video-1", duration_ms=10_000)
    result = run_without_io(agent.answer(object(), video, "不存在的问题"))
    assert result["abstained"] is True
    assert result["citations"] == []
    assert result["answer"] == "无法从视频中确认。"


def test_extract_answer_contains_valid_timed_citation(monkeypatch):
    item = SimpleNamespace(id="evidence-1", video_id="video-1", modality="slide",
                           start_ms=1_000, end_ms=5_000, text="课程目标是可靠交付")
    monkeypatch.setattr(agent, "search", lambda *_args: [item])
    monkeypatch.setattr(agent.settings, "model_api_url", "")
    video = SimpleNamespace(id="video-1", duration_ms=10_000)
    result = run_without_io(agent.answer(object(), video, "课程目标是什么"))
    assert result["abstained"] is False
    assert result["answer"] == item.text
    assert result["citations"] == [{
        "evidence_id": "evidence-1", "modality": "slide",
        "start_ms": 1_000, "end_ms": 5_000, "text": item.text,
    }]
