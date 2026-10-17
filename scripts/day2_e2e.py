"""Verify full upload -> ASR/OCR -> evidence-backed Q&A against a real video."""

import argparse
import hashlib
import json
import time
from pathlib import Path

import httpx


PART_SIZE = 16 * 1024 * 1024


def checked(response: httpx.Response, operation: str) -> dict:
    if response.status_code >= 400:
        raise RuntimeError(f"{operation} failed ({response.status_code}): {response.text[:500]}")
    return response.json()


def upload(http: httpx.Client, api: str, headers: dict, video: Path) -> tuple[str, str]:
    size = video.stat().st_size
    with video.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    started = checked(http.post(f"{api}/uploads", headers=headers,
        json={"filename": video.name, "size_bytes": size}), "start upload")
    upload_id = started["upload_id"]
    part_size = started["part_size"]
    parts = []
    with video.open("rb") as source:
        number = 1
        while chunk := source.read(part_size):
            target = checked(http.get(f"{api}/uploads/{upload_id}/parts/{number}",
                headers=headers), f"get part {number} URL")["url"]
            sent = http.put(target, content=chunk)
            sent.raise_for_status()
            parts.append({"PartNumber": number, "ETag": sent.headers["ETag"]})
            number += 1
    checked(http.post(f"{api}/uploads/{upload_id}/complete", headers=headers,
        json={"parts": parts}), "complete upload")
    return upload_id, digest


def ask(http: httpx.Client, api: str, headers: dict, video_id: str,
        question: str, expected_modality: str | None) -> dict:
    result = checked(http.post(f"{api}/videos/{video_id}/questions", headers=headers,
        json={"question": question}), f"ask {expected_modality or 'unanswerable'} question")
    if expected_modality is None:
        if not result.get("abstained") or result.get("citations"):
            raise AssertionError(f"unanswerable question was not rejected: {result}")
        return result
    citations = result.get("citations", [])
    if result.get("abstained") or not any(row.get("modality") == expected_modality for row in citations):
        raise AssertionError(f"no {expected_modality} evidence returned: {result}")
    for row in citations:
        if not 0 <= row["start_ms"] < row["end_ms"]:
            raise AssertionError(f"invalid evidence bounds: {row}")
    if not result.get("playback_url"):
        raise AssertionError("response has no playback URL")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True,
                        help="3-10 minute Chinese video containing speech and slides")
    parser.add_argument("--speech-question", required=True)
    parser.add_argument("--slide-question", required=True)
    parser.add_argument("--unanswerable-question", default="视频是否证明了火星上存在蓝色鲸鱼？")
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--key", default="demo-key")
    parser.add_argument("--tenant", default="day2")
    parser.add_argument("--timeout-minutes", type=int, default=180)
    parser.add_argument("--report", type=Path, default=Path("day2-report.json"))
    args = parser.parse_args()
    if not args.video.is_file():
        parser.error(f"video does not exist: {args.video}")

    headers = {"X-API-Key": args.key, "X-Tenant-Id": args.tenant}
    with httpx.Client(timeout=120, trust_env=False) as http:
        upload_id, digest = upload(http, args.api.rstrip("/"), headers, args.video)
        submitted = checked(http.post(f"{args.api}/videos",
            headers={**headers, "Idempotency-Key": f"day2-{upload_id}"},
            json={"upload_id": upload_id, "sha256": digest}), "submit video")
        video_id = submitted["job_id"]
        deadline = time.monotonic() + args.timeout_minutes * 60
        while time.monotonic() < deadline:
            job = checked(http.get(f"{args.api}/jobs/{video_id}", headers=headers), "poll job")
            print(json.dumps(job, ensure_ascii=False))
            if job["status"] == "complete":
                break
            if job["status"] == "failed":
                raise RuntimeError(f"workflow failed at {job['stage']}: {job['error']}")
            time.sleep(5)
        else:
            raise TimeoutError(f"workflow did not complete in {args.timeout_minutes} minutes")

        results = {
            "video_id": video_id,
            "duration_ms": job["duration_ms"],
            "speech": ask(http, args.api, headers, video_id, args.speech_question, "speech"),
            "slide": ask(http, args.api, headers, video_id, args.slide_question, "slide"),
            "unanswerable": ask(http, args.api, headers, video_id,
                                 args.unanswerable_question, None),
        }
    args.report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"DAY2 E2E PASS; report={args.report}")


if __name__ == "__main__":
    main()
