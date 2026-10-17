"""Validate an external, immutable evaluation set without committing its answers."""
import argparse
import hashlib
import json
from pathlib import Path


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--min-videos", type=int, default=5)
    parser.add_argument("--min-questions", type=int, default=40)
    args = parser.parse_args()
    actual = file_hash(args.dataset)
    if actual.lower() != args.expected_sha256.lower():
        raise SystemExit(f"sealed dataset hash mismatch: {actual}")
    rows = [json.loads(line) for line in args.dataset.read_text("utf-8").splitlines() if line.strip()]
    required = {"video_id", "question", "answerable", "evidence"}
    for number, row in enumerate(rows, 1):
        missing = required - set(row)
        if missing:
            raise SystemExit(f"line {number} missing fields: {sorted(missing)}")
        if row["answerable"] and not row["evidence"]:
            raise SystemExit(f"line {number}: answerable item has no evidence")
        for evidence in row["evidence"]:
            if evidence.get("end_ms", 0) <= evidence.get("start_ms", -1):
                raise SystemExit(f"line {number}: invalid evidence interval")
    videos = {row["video_id"] for row in rows}
    if len(videos) < args.min_videos or len(rows) < args.min_questions:
        raise SystemExit(f"dataset too small: {len(videos)} videos, {len(rows)} questions")
    if not any(row["answerable"] for row in rows) or not any(not row["answerable"] for row in rows):
        raise SystemExit("dataset must contain answerable and unanswerable questions")
    print(json.dumps({"sha256": actual, "videos": len(videos), "questions": len(rows),
                      "answerable": sum(bool(row["answerable"]) for row in rows)}, indent=2))
    print("SEALED EVALUATION SET VALID")


if __name__ == "__main__":
    main()
