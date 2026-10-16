"""Generate a five-minute fixture and verify multipart upload -> Temporal job."""

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path

import httpx
import imageio_ffmpeg


PART_SIZE = 16 * 1024 * 1024


def fixture(path: Path):
    if path.exists():
        return
    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-f", "lavfi", "-i",
               "color=c=navy:s=320x180:r=1", "-f", "lavfi", "-i",
               "sine=frequency=440:sample_rate=16000", "-t", "300",
               "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
               "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(command, check=True, capture_output=True, timeout=180)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--key", default="demo-key")
    parser.add_argument("--tenant", default="demo")
    parser.add_argument("--video", type=Path, default=Path("day1-five-minute.mp4"))
    parser.add_argument("--generate", action="store_true")
    args = parser.parse_args()
    if args.generate:
        fixture(args.video)
    if not args.video.is_file():
        parser.error("video missing; pass --generate or --video PATH")
    headers = {"X-API-Key": args.key, "X-Tenant-Id": args.tenant}
    with args.video.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    size = args.video.stat().st_size
    # Local API and MinIO pre-signed URLs must not be sent through shell proxies.
    with httpx.Client(timeout=60, trust_env=False) as http:
        response = http.post(f"{args.api}/uploads", headers=headers,
            json={"filename": args.video.name, "size_bytes": size})
        if response.status_code >= 400:
            raise RuntimeError(f"POST /uploads failed ({response.status_code}): {response.text[:500]}")
        response.raise_for_status()
        upload_id = response.json()["upload_id"]
        parts = []
        with args.video.open("rb") as source:
            for number in range(1, (size + PART_SIZE - 1) // PART_SIZE + 1):
                url = http.get(f"{args.api}/uploads/{upload_id}/parts/{number}", headers=headers)
                url.raise_for_status()
                sent = http.put(url.json()["url"], content=source.read(PART_SIZE))
                sent.raise_for_status()
                parts.append({"PartNumber": number, "ETag": sent.headers["ETag"]})
        complete = http.post(f"{args.api}/uploads/{upload_id}/complete",
            headers=headers, json={"parts": parts})
        complete.raise_for_status()
        submitted = http.post(f"{args.api}/videos",
            headers={**headers, "Idempotency-Key": f"day1-{upload_id}"},
            json={"upload_id": upload_id, "sha256": digest})
        submitted.raise_for_status()
        job_id = submitted.json()["job_id"]
        print(json.dumps({"upload_id": upload_id, "job_id": job_id, "size": size,
                          "sha256": digest}, ensure_ascii=False))
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            status = http.get(f"{args.api}/jobs/{job_id}", headers=headers)
            status.raise_for_status()
            body = status.json()
            print(json.dumps(body, ensure_ascii=False))
            if body["status"] == "complete":
                assert 299000 <= body["duration_ms"] <= 301000
                print("DAY1 SMOKE PASS")
                return
            if body["status"] == "failed":
                raise RuntimeError(f"workflow failed: {body['error']}")
            time.sleep(3)
    raise TimeoutError("workflow did not complete in 180 seconds")


if __name__ == "__main__":
    main()
