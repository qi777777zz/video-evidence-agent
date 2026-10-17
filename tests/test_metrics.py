from evaluation.metrics import iou, score
from video_agent.retrieval import terms


def test_iou():
    assert iou({"start_ms": 0, "end_ms": 10}, {"start_ms": 5, "end_ms": 15}) == 1 / 3


def test_metrics():
    gold = [{"video_id": "v", "question": "q", "answerable": True,
             "evidence": [{"start_ms": 0, "end_ms": 10}]},
            {"video_id": "v", "question": "u", "answerable": False, "evidence": []}]
    predictions = [{"video_id": "v", "question": "q", "abstained": False,
                    "citations": [{"start_ms": 0, "end_ms": 10}]},
                   {"video_id": "v", "question": "u", "abstained": True, "citations": []}]
    assert score(gold, predictions)["recall_at_5"] == 1
    assert score(gold, predictions)["abstention_accuracy"] == 1


def test_chinese_bigrams():
    assert "并发" in terms("并发控制")


def test_terms_normalize_traditional_chinese():
    assert terms("概率密度") & terms("機率密度")
