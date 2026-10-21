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


def test_prompt_budget_prioritizes_fresh_evidence_over_history(monkeypatch):
    monkeypatch.setattr(agent.settings, "llm_input_token_budget", 512)
    monkeypatch.setattr(agent.settings, "system_prompt_token_budget", 128)
    monkeypatch.setattr(agent.settings, "current_question_token_budget", 64)
    monkeypatch.setattr(agent.settings, "evidence_token_budget", 220)
    monkeypatch.setattr(agent.settings, "recent_dialogue_token_budget", 220)
    monkeypatch.setattr(agent.settings, "conversation_summary_token_budget", 100)
    evidence = SimpleNamespace(id="fresh", text="本轮检索证据" * 20,
                               start_ms=0, end_ms=1000)
    history = [{"role": "assistant", "content": "历史回答不可作证" * 100}]

    messages = agent.build_messages([evidence], "当前问题", history, "很长的历史摘要" * 100)
    serialized = str(messages)

    assert "fresh" in serialized
    assert "本轮检索证据" in serialized
    assert "历史回答不可作证" not in serialized


def test_prompt_labels_summary_and_history_as_non_evidence():
    evidence = SimpleNamespace(id="e1", text="证据", start_ms=0, end_ms=1000)
    messages = agent.build_messages([evidence], "问题", [], "用户之前询问了定义")

    assert "仅供理解上下文" in messages[1]["content"]
    assert messages[-1]["role"] == "user"
    assert '"evidence"' in messages[-1]["content"]
