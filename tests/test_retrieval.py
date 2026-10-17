from types import SimpleNamespace

from video_agent.retrieval import search, simplified, terms, verify_evidence


class FakeScalars:
    def __init__(self, values):
        self.values = values

    def __iter__(self):
        return iter(self.values)


class FakeSession:
    def __init__(self, values):
        self.values = values

    def scalars(self, _statement):
        return FakeScalars(self.values)


def evidence(identifier, text, start_ms, end_ms, video_id="video-1"):
    return SimpleNamespace(id=identifier, text=text, start_ms=start_ms,
                           end_ms=end_ms, video_id=video_id)


def test_terms_support_chinese_bigrams_and_ascii_tokens():
    value = terms("并发 Control 123")
    assert "并发" in value
    assert "control123" in value


def test_simplified_converts_traditional_chinese_for_display():
    assert simplified("我們介紹具體的分子運動途徑") == "我们介绍具体的分子运动途径"


def test_search_ranks_overlap_then_timestamp():
    values = [
        evidence("later", "这里解释并发控制", 20_000, 25_000),
        evidence("early", "并发控制的基本概念", 5_000, 9_000),
        evidence("irrelevant", "课程结束", 1_000, 2_000),
    ]
    found = search(FakeSession(values), "video-1", "并发控制", limit=5)
    assert [item.id for item in found] == ["early", "later"]


def test_search_returns_empty_for_unmatched_question():
    values = [evidence("one", "事务隔离级别", 0, 5_000)]
    assert search(FakeSession(values), "video-1", "量子引力") == []


def test_search_rejects_single_common_bigram_false_positive():
    values = [evidence("one", "它是不是存在的", 0, 5_000)]
    assert search(FakeSession(values), "video-1", "视频是否介绍量子纠缠实验") == []


def test_verify_evidence_rejects_wrong_video_and_invalid_bounds():
    video = SimpleNamespace(id="video-1", duration_ms=30_000)
    values = [
        evidence("valid", "内容", 0, 1_000),
        evidence("wrong-video", "内容", 0, 1_000, video_id="video-2"),
        evidence("negative", "内容", -1, 1_000),
        evidence("empty", "内容", 2_000, 2_000),
        evidence("overflow", "内容", 29_000, 31_000),
    ]
    assert [item.id for item in verify_evidence(video, values)] == ["valid"]
