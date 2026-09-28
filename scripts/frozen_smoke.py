"""Start a frozen app against a private temp data directory and verify local assets."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def request_json(url: str, *, headers: dict[str, str] | None = None, data: bytes | None = None):
    request = Request(url, headers=headers or {}, data=data)
    with urlopen(request, timeout=2) as response:
        return response.status, response.read()


def tree_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


def run(executable: Path, data_dir: Path, timeout: float) -> None:
    executable = executable.resolve(strict=True)
    data_dir = data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    bundle_dir = executable.parent
    before = tree_snapshot(bundle_dir)
    env = os.environ.copy()
    env["ARXIV_FILTER_DATA_DIR"] = str(data_dir)
    process = subprocess.Popen(
        [str(executable), "--no-browser", "--port", "0", "--data-dir", str(data_dir)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    try:
        runtime_path = data_dir / "runtime.json"
        deadline = time.monotonic() + timeout
        record = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Frozen app exited during startup with code {process.returncode}")
            try:
                record = json.loads(runtime_path.read_text(encoding="utf-8"))
                if type(record.get("port")) is int and isinstance(record.get("challenge"), str):
                    break
            except (OSError, UnicodeError, json.JSONDecodeError):
                time.sleep(0.05)
                continue
        if record is None:
            raise TimeoutError(f"Frozen app did not write runtime metadata within {timeout}s")

        base = f"http://127.0.0.1:{record['port']}"
        challenge_query = urlencode({"challenge": record["challenge"]})
        status, body = request_json(f"{base}/api/v1/system/health?{challenge_query}")
        if status != 200:
            raise RuntimeError(f"Frozen health endpoint returned HTTP {status}")
        health = json.loads(body)
        token = health.get("session_token")
        if health.get("status") != "ok" or not isinstance(token, str) or len(token) < 32:
            raise RuntimeError(f"Frozen health handshake failed: {health!r}")

        status, html = request_json(f"{base}/")
        if status != 200 or b"arXiv Filter" not in html:
            raise RuntimeError("Frozen UI index was not served")
        status, app_js = request_json(f"{base}/static/js/app.js")
        if status != 200 or b"bootstrap" not in app_js:
            raise RuntimeError("Frozen JavaScript assets were not collected")
        status, categories_body = request_json(
            f"{base}/api/v1/categories", headers={"X-App-Token": token}
        )
        if status != 200 or not json.loads(categories_body).get("groups"):
            raise RuntimeError("Frozen taxonomy resources were not collected")

        page_headers = {"X-App-Token": token, "Content-Type": "application/json"}
        page_id = "f" * 32
        page_body = json.dumps({"client_id": page_id, "active": True}).encode("utf-8")
        status, _ = request_json(
            f"{base}/api/v1/system/heartbeat", headers=page_headers, data=page_body
        )
        if status != 200:
            raise RuntimeError(f"Frozen browser heartbeat returned HTTP {status}")
        close_body = json.dumps({"client_id": page_id, "active": False}).encode("utf-8")
        status, _ = request_json(
            f"{base}/api/v1/system/heartbeat", headers=page_headers, data=close_body
        )
        if status != 200:
            raise RuntimeError(f"Frozen page-close notification returned HTTP {status}")
        process.wait(timeout=25)
        try:
            request_json(f"{base}/health")
        except URLError:
            pass
        else:
            raise RuntimeError("Frozen app process exited but its loopback port is still open")
        after = tree_snapshot(bundle_dir)
        if after != before:
            changed = sorted(set(before) ^ set(after))
            changed.extend(
                name for name in before.keys() & after.keys() if before[name] != after[name]
            )
            raise RuntimeError(
                "Frozen app wrote or changed files in the bundle directory: "
                + ", ".join(changed)
            )
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    run(args.executable, args.data_dir, args.timeout)
    print(f"Frozen smoke passed: {args.executable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
