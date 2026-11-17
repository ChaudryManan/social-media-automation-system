"""Run a real Uvicorn process and check its output (B1-11 "Done when").

The API's stdout/stderr must contain only JSON lines, and each HTTP request must
produce exactly one access line that includes its ``request_id``.
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("uvicorn")

REPO_ROOT = Path(__file__).resolve().parents[2]
STARTUP_TIMEOUT_SECONDS = 20


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(url: str, headers: dict[str, str] | None = None) -> int:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)


def _run_server_and_collect_output(extra_flags: list[str]) -> list[str]:
    port = _free_port()
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "LOG_LEVEL": "INFO"}
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            *extra_flags,
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while True:
            try:
                if _get(f"{base}/health") == 200:
                    break
            except OSError:
                pass
            if process.poll() is not None or time.monotonic() > deadline:
                pytest.fail("uvicorn did not start; output:\n" + _drain(process))
            time.sleep(0.2)

        assert _get(f"{base}/health", {"X-Request-ID": "trace-b111"}) == 200
        assert _get(f"{base}/no-such-route", {"X-Request-ID": "trace-404"}) == 404
    finally:
        process.terminate()
    return _drain(process).splitlines()


def _drain(process: subprocess.Popen[str]) -> str:
    try:
        output, _ = process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        output, _ = process.communicate()
    return output or ""


@pytest.fixture(
    scope="module", params=[["--no-access-log"], []], ids=["flag", "noflag"]
)
def output_lines(request: pytest.FixtureRequest) -> list[str]:
    return _run_server_and_collect_output(request.param)


def _json_lines(lines: list[str]) -> list[dict]:
    return [json.loads(line) for line in lines if line.strip()]


def test_every_output_line_is_json(output_lines: list[str]) -> None:
    """Includes Uvicorn's own startup lines, not only the request lines."""
    for line in output_lines:
        if line.strip():
            json.loads(line)  # raises (test fails) on any plain-text line
    assert any("Application startup complete" in line for line in output_lines)


def test_each_request_has_one_access_line_with_its_request_id(
    output_lines: list[str],
) -> None:
    access = [r for r in _json_lines(output_lines) if r.get("logger") == "app.access"]

    traced = [r for r in access if r["request_id"] == "trace-b111"]
    missing = [r for r in access if r["request_id"] == "trace-404"]
    assert len(traced) == 1 and len(missing) == 1
    assert traced[0]["method"] == "GET"
    assert traced[0]["path"] == "/health"
    assert traced[0]["status_code"] == 200
    assert missing[0]["status_code"] == 404


def test_uvicorns_plain_text_access_log_is_gone(output_lines: list[str]) -> None:
    assert not [line for line in output_lines if re.search(r'"GET [^"]* HTTP/1', line)]


def test_compose_runs_the_api_with_uvicorn_access_log_disabled() -> None:
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    api_service = compose.split("\n  worker:")[0]
    assert "uvicorn app.main:app" in api_service
    assert "--no-access-log" in api_service
