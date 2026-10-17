"""Day 7 release gate: reproducible offline checks plus optional live-service checks."""

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = (
    "README.md",
    ".env.example",
    "docker-compose.yml",
    "docs/architecture.md",
    "video_agent/static/index.html",
    "video_agent/static/app.js",
    "video_agent/static/app.css",
)


def run(command: list[str], label: str) -> dict:
    started = time.perf_counter()
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    record = {
        "name": label,
        "passed": result.returncode == 0,
        "seconds": round(time.perf_counter() - started, 3),
    }
    if result.returncode:
        record["detail"] = (result.stdout + result.stderr)[-4000:]
    return record


def live_checks(api: str, key: str, tenant: str) -> list[dict]:
    checks = []
    headers = {"X-API-Key": key, "X-Tenant-Id": tenant}
    with httpx.Client(base_url=api, timeout=10, trust_env=False) as client:
        for name, path, expected, request_headers in (
            ("liveness", "/health/live", 200, None),
            ("readiness", "/health/ready", 200, None),
            ("workbench", "/", 200, None),
            ("metrics", "/metrics", 200, None),
            ("authentication rejection", "/uploads", 401, {
                "X-API-Key": "day7-invalid-key", "X-Tenant-Id": tenant}),
        ):
            try:
                if path == "/uploads":
                    response = client.post(path, headers=request_headers, json={
                        "filename": "probe.mp4", "size_bytes": 1})
                else:
                    response = client.get(path, headers=request_headers or headers)
                checks.append({"name": name, "passed": response.status_code == expected,
                               "status_code": response.status_code, "expected": expected})
            except httpx.HTTPError as exc:
                checks.append({"name": name, "passed": False, "detail": str(exc)})
    return checks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="also verify a running API and its dependencies")
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--key", default="demo-key")
    parser.add_argument("--tenant", default="day7-release-check")
    parser.add_argument("--report", type=Path, default=Path("day7-release-report.json"))
    args = parser.parse_args()

    checks = [{"name": f"required file: {path}", "passed": (ROOT / path).is_file()}
              for path in REQUIRED_FILES]
    checks.append({"name": "Python 3.12+", "passed": sys.version_info >= (3, 12),
                   "detail": sys.version.split()[0]})
    checks.append(run([sys.executable, "-m", "pytest", "-q", "--basetemp",
                       str(ROOT / ".tmp" / "pytest-day7")], "unit and component tests"))

    docker = shutil.which("docker")
    if docker:
        checks.append(run([docker, "compose", "config", "--quiet"], "Docker Compose configuration"))
    else:
        checks.append({"name": "Docker CLI available", "passed": False,
                       "detail": "Install/start Docker Desktop before the live demonstration."})

    if args.live:
        checks.extend(live_checks(args.api.rstrip("/"), args.key, args.tenant))

    report = {
        "passed": all(item["passed"] for item in checks),
        "mode": "live" if args.live else "offline",
        "checks": checks,
    }
    report_path = args.report if args.report.is_absolute() else ROOT / args.report
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)
    print(f"DAY7 RELEASE PASS; report={report_path}")


if __name__ == "__main__":
    main()
