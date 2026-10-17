"""Run the Day 5 security, idempotency, recovery, and evidence acceptance checks."""

import argparse
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx

from day2_e2e import ask, checked, upload


def expect_status(response: httpx.Response, status: int, operation: str):
    if response.status_code != status:
        raise AssertionError(f"{operation}: expected {status}, got {response.status_code}: {response.text[:500]}")


def get_with_retry(http: httpx.Client, url: str, headers: dict | None = None,
                   attempts: int = 3) -> httpx.Response:
    for attempt in range(attempts):
        try:
            return http.get(url, headers=headers)
        except httpx.TransportError:
            if attempt + 1 == attempts:
                raise
            time.sleep(1)
    raise AssertionError("unreachable")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--speech-question", required=True)
    parser.add_argument("--slide-question", required=True)
    parser.add_argument("--unanswerable-question", default="视频是否证明了火星上存在蓝色鲸鱼？")
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--key", default="demo-key")
    parser.add_argument("--tenant", default="day5")
    parser.add_argument("--timeout-minutes", type=int, default=180)
    parser.add_argument("--report", type=Path, default=Path("day5-report.json"))
    args = parser.parse_args()
    if not args.video.is_file():
        parser.error(f"video does not exist: {args.video}")

    api = args.api.rstrip("/")
    headers = {"X-API-Key": args.key, "X-Tenant-Id": args.tenant}
    other_headers = {"X-API-Key": args.key, "X-Tenant-Id": f"{args.tenant}-other"}
    started_at = time.time()
    with httpx.Client(timeout=120, trust_env=False) as http:
        checked(get_with_retry(http, f"{api}/health/live"), "liveness")
        checked(get_with_retry(http, f"{api}/health/ready"), "readiness")
        expect_status(get_with_retry(http, f"{api}/jobs/not-a-job", {"X-API-Key": "wrong"}), 401,
                      "invalid API key")

        upload_id, digest = upload(http, api, headers, args.video)
        malicious = http.post(f"{api}/videos", headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"upload_id": upload_id, "sha256": digest,
                  "webhook_url": "https://127.0.0.1/event"})
        expect_status(malicious, 400, "private webhook rejection")

        key = f"day5-{uuid4()}"
        payload = {"upload_id": upload_id, "sha256": digest}
        first = checked(http.post(f"{api}/videos", headers={**headers, "Idempotency-Key": key},
            json=payload), "submit video")
        duplicate = checked(http.post(f"{api}/videos", headers={**headers, "Idempotency-Key": key},
            json=payload), "repeat idempotent submission")
        if duplicate["job_id"] != first["job_id"] or not duplicate.get("reused"):
            raise AssertionError(f"idempotency failed: first={first}, duplicate={duplicate}")
        video_id = first["job_id"]
        expect_status(get_with_retry(http, f"{api}/jobs/{video_id}", other_headers), 404,
                      "cross-tenant job isolation")

        deadline = time.monotonic() + args.timeout_minutes * 60
        while time.monotonic() < deadline:
            job = checked(get_with_retry(http, f"{api}/jobs/{video_id}", headers), "poll job")
            print(json.dumps(job, ensure_ascii=False))
            if job["status"] == "complete":
                break
            if job["status"] == "failed":
                raise RuntimeError(f"workflow failed at {job['stage']}: {job['error']}")
            time.sleep(5)
        else:
            raise TimeoutError(f"workflow did not complete in {args.timeout_minutes} minutes")

        expect_status(http.post(f"{api}/videos/{video_id}/questions", headers=other_headers,
            json={"question": args.speech_question}), 404, "cross-tenant evidence isolation")
        results = {
            "video_id": video_id,
            "duration_ms": job["duration_ms"],
            "elapsed_seconds": round(time.time() - started_at, 2),
            "idempotency_reused": True,
            "tenant_isolation": True,
            "private_webhook_rejected": True,
            "speech": ask(http, api, headers, video_id, args.speech_question, "speech"),
            "slide": ask(http, api, headers, video_id, args.slide_question, "slide"),
            "unanswerable": ask(http, api, headers, video_id, args.unanswerable_question, None),
        }
    args.report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"DAY5 ACCEPTANCE PASS; report={args.report}")


if __name__ == "__main__":
    main()
