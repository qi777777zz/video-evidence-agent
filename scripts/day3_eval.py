import argparse
import asyncio
import json
from pathlib import Path

from evaluation.metrics import read_jsonl, score
from video_agent.agent import answer
from video_agent.db import Session, Video, init_db


async def run(dataset: Path, output: Path) -> dict:
    init_db()
    predictions = []
    with Session() as session:
        for row in read_jsonl(str(dataset)):
            video = session.get(Video, row["video_id"])
            if video is None:
                raise RuntimeError(f"video not found: {row['video_id']}")
            result = await answer(session, video, row["question"])
            predictions.append({"video_id": video.id, "question": row["question"], **result})
    output.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in predictions) + "\n", encoding="utf-8")
    return score(read_jsonl(str(dataset)), predictions)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Day 3 retrieval acceptance set")
    parser.add_argument("--dataset", type=Path, default=Path("evaluation/day3_questions.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("evaluation/day3_predictions.jsonl"))
    args = parser.parse_args()
    metrics = asyncio.run(run(args.dataset, args.output))
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    if metrics["recall_at_5"] < 0.8 or metrics["abstention_accuracy"] < 0.9:
        raise SystemExit("DAY3 EVAL FAIL")
    print("DAY3 EVAL PASS")


if __name__ == "__main__":
    main()
