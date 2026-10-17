"""Concurrent Day 6 capacity check for upload, submission, completion, and Q&A."""

import argparse
import concurrent.futures
import json
import statistics
import time
from pathlib import Path
from uuid import uuid4

import httpx

from day2_e2e import checked, upload


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))], 3)


def submit_one(api, key, tenant, video):
    headers = {"X-API-Key": key, "X-Tenant-Id": tenant}
    started = time.perf_counter()
    with httpx.Client(timeout=180, trust_env=False) as http:
        upload_id, digest = upload(http, api, headers, video)
        uploaded = time.perf_counter()
        response = http.post(f"{api}/videos", headers={**headers, "Idempotency-Key": str(uuid4())},
                             json={"upload_id": upload_id, "sha256": digest})
        body = checked(response, "submit")
    return {"tenant": tenant, "job_id": body["job_id"],
            "upload_seconds": uploaded - started, "submit_seconds": time.perf_counter() - uploaded}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--key", default="demo-key")
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--tenants", type=int, default=5)
    parser.add_argument("--timeout-minutes", type=int, default=180)
    parser.add_argument("--report", type=Path, default=Path("day6-performance-report.json"))
    args = parser.parse_args()
    if not args.video.is_file():
        parser.error(f"video does not exist: {args.video}")
    if args.concurrency < 1 or args.tenants < 1:
        parser.error("concurrency and tenants must be positive")
    api = args.api.rstrip("/")
    tenants = [f"day6-{index % args.tenants}" for index in range(args.concurrency)]
    failures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(submit_one, api, args.key, tenant, args.video) for tenant in tenants]
        jobs = []
        for future in concurrent.futures.as_completed(futures):
            try:
                jobs.append(future.result())
            except Exception as exc:
                failures.append(str(exc))
    deadline = time.monotonic() + args.timeout_minutes * 60
    question_seconds = []
    pending = {item["job_id"]: item for item in jobs}
    with httpx.Client(timeout=120, trust_env=False) as http:
        while pending and time.monotonic() < deadline:
            for job_id, item in list(pending.items()):
                headers = {"X-API-Key": args.key, "X-Tenant-Id": item["tenant"]}
                job = checked(http.get(f"{api}/jobs/{job_id}", headers=headers), "poll")
                if job["status"] == "failed":
                    failures.append(f"{job_id}: {job['error']}")
                    pending.pop(job_id)
                elif job["status"] == "complete":
                    started = time.perf_counter()
                    checked(http.post(f"{api}/videos/{job_id}/questions", headers=headers,
                                      json={"question": args.question}), "question")
                    question_seconds.append(time.perf_counter() - started)
                    pending.pop(job_id)
            if pending:
                time.sleep(5)
    failures.extend(f"{job_id}: timeout" for job_id in pending)
    report = {"submitted": len(jobs), "completed": len(question_seconds), "failures": failures,
              "latency_seconds": {
                  "upload_p50": percentile([x["upload_seconds"] for x in jobs], .5),
                  "upload_p95": percentile([x["upload_seconds"] for x in jobs], .95),
                  "submit_p95": percentile([x["submit_seconds"] for x in jobs], .95),
                  "question_p50": percentile(question_seconds, .5),
                  "question_p95": percentile(question_seconds, .95),
                  "question_mean": round(statistics.mean(question_seconds), 3) if question_seconds else None}}
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(1)
    print(f"DAY6 LOAD PASS; report={args.report}")


if __name__ == "__main__":
    main()
