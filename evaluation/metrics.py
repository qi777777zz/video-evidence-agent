import json
import sys
from pathlib import Path


def iou(left: dict, right: dict) -> float:
    intersection = max(0, min(left["end_ms"], right["end_ms"]) - max(left["start_ms"], right["start_ms"]))
    union = max(left["end_ms"], right["end_ms"]) - min(left["start_ms"], right["start_ms"])
    return intersection / union if union else 0.0


def score(gold: list[dict], predictions: list[dict]) -> dict:
    lookup = {(x["video_id"], x["question"]): x for x in predictions}
    recall, rr, localization, refusal, support = [], [], [], [], []
    for row in gold:
        key = row["video_id"], row["question"]
        if key not in lookup:
            raise ValueError(f"missing prediction: {key}")
        found = lookup[key]
        refusal.append(bool(found["abstained"]) == (not row["answerable"]))
        if not row["answerable"]:
            continue
        citations = found.get("citations", [])[:5]
        matching = [max((iou(candidate, target) for target in row["evidence"]), default=0)
                    for candidate in citations]
        hit = next((index for index, value in enumerate(matching) if value >= 0.5), None)
        recall.append(hit is not None)
        rr.append(1 / (hit + 1) if hit is not None else 0)
        localization.append(max(matching, default=0))
        support.append(not found["abstained"] and hit is not None)
    average = lambda values: round(sum(values) / len(values), 4) if values else None
    return {"count": len(gold), "recall_at_5": average(recall), "mrr": average(rr),
            "mean_best_temporal_iou": average(localization), "abstention_accuracy": average(refusal),
            "evidence_support_rate": average(support)}


def read_jsonl(path: str):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    print(json.dumps(score(read_jsonl(sys.argv[1]), read_jsonl(sys.argv[2])), ensure_ascii=False, indent=2))
