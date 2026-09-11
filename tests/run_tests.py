#!/usr/bin/env python3
"""Isolated dispatcher smoke tests using only loopback fake backends."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import model_dispatch

# Keep direct unit tests deterministic on a memory-pressured developer host;
# the dispatcher subprocess still exercises the real platform probe.
model_dispatch.memory_pressure_level = lambda: 100

DISPATCHER = ROOT / "scripts" / "model_dispatch.py"
PROTECTED = {8000, 8888, 11234, 18888, 18889, 18953}


def free_port() -> int:
    while True:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port not in PROTECTED:
            return port


def wait_health(url: str, timeout: float = 15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url + "/health", timeout=0.5) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise AssertionError(f"dispatcher did not become healthy at {url}")


def post_json(url: str, payload: dict, timeout: float = 30):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def post_raw(url: str, body: bytes, content_type: str | None):
    headers = {}
    if content_type is not None:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def get_json(url: str):
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def dispatcher_run(base: str) -> str:
    return get_json(base + "/health")["dispatcher_run"]


def read_events(path: Path, run_id: str):
    if path.is_dir():
        records = []
        for item in sorted(path.glob("fake-*.events.jsonl")):
            if item.name != path.name:
                continue
            for line in item.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                record["source"] = item.name
                records.append(record)
        return records
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@contextlib.contextmanager
def dispatcher_process(request_timeout=120, sse_delay=0.2):
    with tempfile.TemporaryDirectory(prefix="model-dispatch-test-") as tmp:
        tmp_path = Path(tmp)
        listen = free_port()
        backend = free_port()
        events_path = tmp_path / "fake.events.jsonl"
        config = tmp_path / "engines.yaml"
        config.write_text(
            "\n".join(
                [
                    "listen_host: 127.0.0.1",
                    f"listen_port: {listen}",
                    "load_timeout_seconds: 10",
                    f"request_timeout_seconds: {request_timeout}",
                    "request_poll_seconds: 0.02",
                    "adapters:",
                    "  fake:",
                    "    type: fake",
                    f"    python: {sys.executable}",
                    "    script: scripts/fake_backend.py",
                    f"    port: {backend}",
                    f"    events_path: {events_path}",
                    "    start_delay_seconds: 0.2",
                    "    sse_chunks: 5",
                    f"    sse_delay_seconds: {sse_delay}",
                    "    env: {}",
                    "resource_groups:",
                    "  large-local:",
                    "    capacity: 1",
                    "models:",
                    "  fake-alpha:",
                    "    adapter: fake",
                    "    backend_model: fake-alpha",
                    "    aliases: [fake-alpha-alias]",
                    "    resource_group: large-local",
                    "    lifecycle_owner: model-dispatch",
                    "    capabilities:",
                    "      chat: true",
                    "      stream: true",
                    "      vision: false",
                    "  fake-beta:",
                    "    adapter: fake",
                    "    backend_model: fake-beta",
                    "    resource_group: large-local",
                    "    lifecycle_owner: model-dispatch",
                    "    capabilities:",
                    "      chat: true",
                    "      stream: true",
                    "      vision: false",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        base = f"http://127.0.0.1:{listen}"
        log = tmp_path / "dispatcher.log"
        with log.open("wb") as log_file:
            process = subprocess.Popen(
                [sys.executable, str(DISPATCHER), "--config", str(config)],
                cwd=ROOT,
                env={**os.environ, "INFERENCEDOCK_SETTINGS_PATH": str(tmp_path / "settings.json")},
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                wait_health(base)
                yield base, events_path, log, dispatcher_run(base)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                if process.poll() is None:
                    raise AssertionError("dispatcher did not stop")


def test_unknown_model(base: str, events: Path, run_id: str):
    try:
        post_json(base + "/v1/chat/completions", {"model": "nope", "messages": []})
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
        assert "nope" in exc.read().decode("utf-8")
    else:
        raise AssertionError("unknown model unexpectedly succeeded")
    assert read_events(events, run_id) == []


def test_responses_api_uses_same_dispatch_path(base: str, events: Path, run_id: str):
    status, body = post_json(base + "/v1/responses", {"model": "fake-alpha", "input": "hello"})
    assert status == 200
    assert body["object"] == "response" and body["model"] == "fake-alpha"
    latest = get_json(base + "/v1/status")["latest_request"]
    assert latest["model"] == "fake-alpha" and latest["protocol"] == "responses"


def test_switching_rejects_busy_stream_then_retries(base: str, events: Path, run_id: str):
    first = {"done": False, "error": None}

    def run_stream():
        try:
            request = urllib.request.Request(
                base + "/v1/chat/completions",
                data=json.dumps({"model": "fake-alpha", "stream": True, "messages": []}).encode("utf-8"),
                headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
                method="POST",
            )
            response = urllib.request.urlopen(request, timeout=20)
            try:
                line = response.readline()
                first["chunks"] = 1 if line.startswith(b"data:") else 0
            finally:
                response.close()
        except Exception as exc:
            first["error"] = repr(exc)
        finally:
            first["done"] = True

    thread = threading.Thread(target=run_stream)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if get_json(base + "/v1/status")["active_requests"] == 1:
            break
        time.sleep(0.02)
    assert get_json(base + "/v1/status")["active_requests"] == 1

    switch_started = time.monotonic()
    status, raw = post_raw(
        base + "/v1/chat/completions",
        json.dumps({"model": "fake-beta", "messages": []}).encode("utf-8"),
        "application/json",
    )
    switch_elapsed = time.monotonic() - switch_started
    thread.join(timeout=10)

    assert first["error"] is None
    assert first["done"]
    assert first.get("chunks", 0) == 1
    assert status == 409 and json.loads(raw)["error"]["type"] == "model_busy"
    assert switch_elapsed < 0.5, "busy switch should fail immediately"
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and get_json(base + "/v1/status")["active_requests"]:
        time.sleep(0.02)
    status, body = post_json(base + "/v1/chat/completions", {"model": "fake-beta", "messages": []}, timeout=30)
    assert status == 200 and body["model"] == "fake-beta"

    event_names = [event["event"] for event in read_events(events, run_id)]
    assert "process_start" in event_names
    assert "request_end" in event_names


def test_unload(base: str, events: Path, run_id: str):
    status, body = post_json(base + "/v1/chat/completions", {"model": "fake-alpha", "messages": []})
    assert status == 200
    assert get_json(base + "/v1/status")["active_model"] == "fake-alpha"
    post_json(base + "/v1/unload", {})
    status_payload = get_json(base + "/v1/status")
    assert status_payload["active_model"] is None
    assert status_payload["state"] == "unloaded"


def test_models_listing(base: str, events: Path, run_id: str):
    payload = get_json(base + "/v1/models")
    ids = {item["id"] for item in payload["data"]}
    assert ids == {"fake-alpha", "fake-beta"}


def test_alias_routes_to_canonical_model(base: str, events: Path, run_id: str):
    status, _ = post_json(base + "/v1/chat/completions", {"model": "fake-alpha-alias", "messages": []})
    assert status == 200
    assert get_json(base + "/v1/status")["latest_request"]["model"] == "fake-alpha"


def test_switch_api_and_content_type(base: str, events: Path, run_id: str):
    status, payload = post_json(base + "/v1/switch", {"model": "fake-alpha"})
    assert status == 200
    assert payload["active_model"] == "fake-alpha"
    code, _ = post_raw(base + "/v1/switch", b"{}", None)
    assert code == 415
    code, _ = post_raw(base + "/v1/switch", b"", "application/json")
    assert code == 400


def test_settings_api(base: str, events: Path, run_id: str):
    current = get_json(base + "/v1/settings")
    assert current["smart_scheduling"] is False
    code, body = post_raw(
        base + "/v1/settings",
        json.dumps({"smart_scheduling": True, "memory_limit_gb": 4, "adapter_policies": {"fake": {"exclusive_groups": ["large-local"], "keep_resident": True}}}).encode(),
        "application/json",
    )
    assert code == 200
    updated = json.loads(body)
    assert updated["smart_scheduling"] is True and updated["memory_limit_gb"] == 4.0
    persisted = get_json(base + "/v1/settings")
    assert persisted["adapter_policies"]["fake"]["keep_resident"] is True
    code, _ = post_raw(base + "/v1/settings", b'{"unknown": true}', "application/json")
    assert code == 400
    post_raw(base + "/v1/settings", b'{"smart_scheduling": false, "memory_limit_gb": null, "adapter_policies": {}}', "application/json")
    code, body = post_raw(
        base + "/v1/chat/completions",
        json.dumps({"model": "fake-beta", "messages": []}).encode(),
        "application/json",
    )
    assert code == 409 and json.loads(body)["error"]["type"] == "smart_scheduling_disabled"
    assert post_json(base + "/v1/switch", {"model": "fake-beta"})[0] == 200
    code, body = post_raw(
        base + "/v1/settings",
        b'{"smart_scheduling": true, "memory_limit_gb": null, "idle_unload_seconds": null, "adapter_policies": {}}',
        "application/json",
    )
    assert code == 200
    cleared = json.loads(body)
    assert cleared["memory_limit_gb"] is None and cleared["idle_unload_seconds"] is None


def test_cancel_rejects_non_json_body(base: str, events: Path, run_id: str):
    code, body = post_raw(base + "/v1/cancel", b"request_id=x", "text/plain")
    assert code == 415 and json.loads(body)["error"]["type"] == "invalid_request_error"

    post_json(base + "/v1/switch", {"model": "fake-alpha"})
    url = urllib.request.urlsplit(base)
    for path in ("/v1/cancel", "/v1/unload"):
        for length in ("-1", "invalid"):
            request = (
                f"POST {path} HTTP/1.1\r\nHost: {url.hostname}:{url.port}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {length}\r\nConnection: close\r\n\r\n"
            ).encode("ascii")
            with socket.create_connection((url.hostname, url.port), timeout=3) as sock:
                sock.sendall(request)
                response = b""
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk
            assert b" 400 " in response.split(b"\r\n", 1)[0]
    assert get_json(base + "/v1/status")["active_model"] == "fake-alpha"


def test_request_metrics(base: str, events: Path, run_id: str):
    sentinel = "DO_NOT_LOG_PROMPT_7f9c"
    status, body = post_json(
        base + "/v1/chat/completions",
        {"model": "fake-alpha", "messages": [{"role": "user", "content": sentinel}]},
    )
    assert status == 200
    status_payload = get_json(base + "/v1/status")
    latest = status_payload["latest_request"]
    assert latest["model"] == "fake-alpha"
    assert latest["adapter"] == "fake"
    assert latest["stream"] is False
    assert latest["response_bytes"] > 0
    assert "duration_ms" in latest
    assert latest["usage"]["prompt_tokens"] == 1
    assert latest["usage"]["completion_tokens"] == 1
    assert latest["finish_reason"] == "stop"
    metrics_path = ROOT / "logs" / "request-metrics.jsonl"
    assert sentinel not in metrics_path.read_text(encoding="utf-8")

    request = urllib.request.Request(
        base + "/v1/chat/completions",
        data=json.dumps({"model": "fake-beta", "stream": True, "messages": []}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    response = urllib.request.urlopen(request, timeout=20)
    try:
        line = response.readline()
        assert line.startswith(b"data:")
    finally:
        response.close()

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        status_payload = get_json(base + "/v1/status")
        latest = status_payload["latest_request"]
        if latest and latest["model"] == "fake-alpha":
            break
        time.sleep(0.05)
    assert status_payload["latest_request"]["model"] == "fake-alpha"


def test_stream_metrics_complete(base: str, events: Path, run_id: str):
    post_json(base + "/v1/switch", {"model": "fake-beta"})
    url = urllib.request.urlsplit(base)
    body = json.dumps({"model": "fake-beta", "stream": True, "messages": []}).encode("utf-8")
    request = (
        b"POST /v1/chat/completions HTTP/1.1\r\n"
        + f"Host: {url.hostname}:{url.port}\r\n".encode("ascii")
        + b"Content-Type: application/json\r\n"
        + b"Accept: text/event-stream\r\n"
        + f"Content-Length: {len(body)}\r\n".encode("ascii")
        + b"Connection: close\r\n\r\n"
        + body
    )
    data = b""
    with socket.create_connection((url.hostname, url.port), timeout=20) as sock:
        sock.sendall(request)
        while b"[DONE]" not in data:
            chunk = sock.recv(8192)
            if not chunk:
                break
            data += chunk
    assert b"[DONE]" in data

    metrics_path = ROOT / "logs" / "request-metrics.jsonl"
    deadline = time.monotonic() + 3
    latest = None
    while time.monotonic() < deadline:
        if metrics_path.exists():
            rows = [json.loads(line) for line in metrics_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            if rows:
                latest = rows[-1]
                if (
                    latest.get("stream") is True
                    and latest.get("model") == "fake-beta"
                    and latest.get("finish_reason") == "stop"
                ):
                    break
        time.sleep(0.05)
    assert latest is not None
    assert latest["model"] == "fake-beta"
    assert latest["stream"] is True
    assert latest["sse_chunks"] >= 5
    assert latest["finish_reason"] == "stop"
    assert latest["response_bytes"] > 0


def test_responses_stream_metrics_complete(base: str, events: Path, run_id: str):
    body = json.dumps({"model": "fake-beta", "stream": True, "input": "hello"}).encode("utf-8")
    request = urllib.request.Request(base + "/v1/responses", data=body, headers={"Content-Type": "application/json", "Accept": "text/event-stream"}, method="POST")
    with urllib.request.urlopen(request, timeout=20) as response:
        assert b"response.completed" in response.read()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        latest = get_json(base + "/v1/status").get("latest_request") or {}
        if latest.get("protocol") == "responses" and latest.get("finish_reason") == "stop":
            break
        time.sleep(0.05)
    assert latest["finish_reason"] == "stop"
    assert latest["ttft_ms"] >= 0 and latest["cached_tokens"] == 0
    assert latest["tokens_per_second"] == 12.5 and latest["prefill_tokens_per_second"] == 100.0


def test_responses_background_is_rejected(base: str, events: Path, run_id: str):
    status, body = post_raw(
        base + "/v1/responses",
        json.dumps({"model": "fake-beta", "input": "hello", "background": True}).encode(),
        "application/json",
    )
    assert status == 400
    assert b"background" in body.lower()


def test_stream_client_disconnect_finishes(base: str, events: Path, run_id: str):
    post_json(base + "/v1/switch", {"model": "fake-alpha"})
    url = urllib.request.urlsplit(base)
    body = json.dumps({"model": "fake-alpha", "stream": True, "messages": []}).encode("utf-8")
    request = (
        b"POST /v1/chat/completions HTTP/1.1\r\n"
        + f"Host: {url.hostname}:{url.port}\r\n".encode("ascii")
        + b"Content-Type: application/json\r\n"
        + b"Accept: text/event-stream\r\n"
        + f"Content-Length: {len(body)}\r\n".encode("ascii")
        + b"Connection: close\r\n\r\n"
        + body
    )
    with socket.create_connection((url.hostname, url.port), timeout=20) as sock:
        sock.sendall(request)
        data = b""
        while b"data:" not in data:
            chunk = sock.recv(8192)
            assert chunk
            data += chunk
        sock.shutdown(socket.SHUT_RDWR)

    deadline = time.monotonic() + 5
    latest = None
    while time.monotonic() < deadline:
        status = get_json(base + "/v1/status")
        latest = status.get("latest_request")
        if status["active_requests"] == 0 and latest and latest.get("model") == "fake-alpha":
            break
        time.sleep(0.05)
    assert latest is not None
    assert latest["model"] == "fake-alpha"
    assert latest["finish_reason"] == "client_disconnect"
    assert get_json(base + "/v1/status")["active_requests"] == 0


def test_cancel_keeps_model_loaded(base: str, events: Path, run_id: str):
    post_json(base + "/v1/switch", {"model": "fake-alpha"})
    url = urllib.request.urlsplit(base)
    body = json.dumps({"model": "fake-alpha", "stream": True, "messages": []}).encode("utf-8")
    request = (
        b"POST /v1/chat/completions HTTP/1.1\r\n"
        + f"Host: {url.hostname}:{url.port}\r\n".encode("ascii")
        + b"Content-Type: application/json\r\nAccept: text/event-stream\r\n"
        + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
        + body
    )
    with socket.create_connection((url.hostname, url.port), timeout=20) as sock:
        sock.sendall(request)
        initial = b""
        while b"data:" not in initial:
            chunk = sock.recv(8192)
            assert chunk
            initial += chunk
        status, payload = post_json(base + "/v1/cancel", {})
        assert status == 200 and payload["cancelled"] is True
        # Cancellation closes the upstream; downstream EOF timing is server/socket dependent.
        # Close the client side here and assert cancellation through the dispatcher state.
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status_payload = get_json(base + "/v1/status")
        if status_payload["active_requests"] == 0:
            break
        time.sleep(0.05)
    assert status_payload["active_requests"] == 0
    assert status_payload["active_model"] == "fake-alpha"
    assert status_payload["state"] == "ready"
    assert status_payload["latest_request"]["finish_reason"] == "request_cancelled"


def test_cross_site_requests_are_rejected(base: str, events: Path, run_id: str):
    def request_with_headers(method: str, path: str, headers: dict):
        req = urllib.request.Request(base + path, method=method, headers=headers)
        if method == "POST":
            req.data = b"{}"
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    assert request_with_headers("POST", "/v1/unload", {"Origin": "http://evil.example"}) == 403
    assert request_with_headers("POST", "/v1/cancel", {"Origin": "https://127.0.0.1.evil.example"}) == 403
    assert request_with_headers("GET", "/v1/status", {"Origin": "http://evil.example"}) == 403
    assert request_with_headers("POST", "/v1/unload", {"Host": "rebind.example"}) == 403
    assert request_with_headers("GET", "/v1/models", {"Origin": "http://127.0.0.1:3000"}) == 200
    assert request_with_headers("POST", "/v1/unload", {}) == 200


def test_cancel_isolated_by_request_id(base: str, events: Path, run_id: str):
    def open_stream(request_id: str):
        request = urllib.request.Request(
            base + "/v1/chat/completions",
            data=json.dumps({"model": "fake-alpha", "stream": True, "messages": []}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "X-InferenceDock-Request-ID": request_id,
            },
            method="POST",
        )
        response = urllib.request.urlopen(request, timeout=20)
        assert response.headers["X-InferenceDock-Request-ID"] == request_id
        assert response.readline().startswith(b"data:")
        return response

    first = open_stream("cancel-me")
    second = open_stream("keep-me")
    try:
        status, payload = post_json(base + "/v1/cancel", {"request_id": "cancel-me"})
        assert status == 200 and payload["cancelled"] is True and payload["cancelled_count"] == 1
        first.close()
        assert b"data: [DONE]" in second.read(), "cancelling one request interrupted its same-model peer"
    finally:
        first.close()
        second.close()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and get_json(base + "/v1/status")["active_requests"]:
        time.sleep(0.05)
    assert get_json(base + "/v1/status")["active_requests"] == 0


def test_same_model_cold_start_is_coalesced(base: str, events: Path, run_id: str):
    post_json(base + "/v1/unload", {})
    barrier = threading.Barrier(3)
    results = []

    def invoke():
        barrier.wait()
        results.append(post_json(base + "/v1/chat/completions", {"model": "fake-alpha", "messages": []}, timeout=20))

    threads = [threading.Thread(target=invoke) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=25)
    assert len(results) == 2 and all(status == 200 for status, _ in results)
    starts = sum(event["event"] == "process_start" for event in read_events(events, run_id))
    assert starts == 1, "concurrent first requests started the same backend more than once"


def test_sse_ttft_requires_content():
    handler = object.__new__(model_dispatch.DispatchHandler)
    metrics = {"_started_at": time.monotonic() - 0.01}
    handler._consume_sse_line(
        b'data: {"choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}',
        metrics,
    )
    handler._consume_sse_line(b'data: {"usage":{"prompt_tokens":1}}', metrics)
    assert "ttft_ms" not in metrics
    handler._consume_sse_line(
        b'data: {"choices":[{"delta":{"content":"hello"},"finish_reason":null}]}',
        metrics,
    )
    assert isinstance(metrics.get("ttft_ms"), (int, float))


def test_sse_non_object_metrics_are_ignored():
    handler = object.__new__(model_dispatch.DispatchHandler)
    metrics = {"_started_at": time.monotonic()}
    # A backend may emit a JSON array/null keep-alive frame. It must not raise
    # or affect the response forwarding loop.
    handler._consume_sse_line(b"data: []", metrics)
    handler._consume_sse_line(b"data: null", metrics)
    assert "sse_chunks" not in metrics


def test_stream_timeout_returns_structured_end_without_traceback():
    with dispatcher_process(request_timeout=0.01, sse_delay=1.0) as (base, _events, log, _run_id):
        request = urllib.request.Request(
            base + "/v1/chat/completions",
            data=json.dumps({"model": "fake-alpha", "stream": True, "messages": []}).encode(),
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read()
        assert b'"type": "upstream_timeout"' in body
        assert b"data: [DONE]" in body
        time.sleep(0.05)
        assert "Traceback" not in log.read_text(encoding="utf-8")


def _dispatcher_for_models(tmp_path: Path, adapters: dict, models: dict, idle_unload_seconds: float | None = None, resource_groups: dict | None = None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_data = {
        "listen_host": "127.0.0.1",
        "listen_port": free_port(),
        "adapters": adapters,
        "models": models,
    }
    if idle_unload_seconds is not None:
        config_data["idle_unload_seconds"] = idle_unload_seconds
    if resource_groups is not None:
        config_data["resource_groups"] = resource_groups
    config_path = tmp_path / "engines.yaml"
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    config = model_dispatch.load_config(config_path)
    old_settings = os.environ.get("INFERENCEDOCK_SETTINGS_PATH")
    os.environ["INFERENCEDOCK_SETTINGS_PATH"] = str(tmp_path / "settings.json")
    try:
        return model_dispatch.ModelDispatcher(config, tmp_path)
    finally:
        if old_settings is None:
            os.environ.pop("INFERENCEDOCK_SETTINGS_PATH", None)
        else:
            os.environ["INFERENCEDOCK_SETTINGS_PATH"] = old_settings


def test_cancel_during_load_rolls_back_orphan_model():
    class SlowBackend:
        def __init__(self, adapter):
            self.adapter = adapter
            self.stopped = False
            self.process = object()

        def ensure_started(self, timeout):
            time.sleep(0.4)

        def healthy(self):
            return True

        def stop(self):
            self.stopped = True

    with tempfile.TemporaryDirectory(prefix="model-dispatch-cancel-load-test-") as tmp:
        dispatcher = _dispatcher_for_models(
            Path(tmp),
            {
                "managed": {
                    "type": "managed",
                    "endpoint": f"http://127.0.0.1:{free_port()}",
                    "command": [sys.executable, "-c", "pass"],
                }
            },
            {"model": {"adapter": "managed"}},
        )
        backend = SlowBackend(dispatcher.config.adapters["managed"])
        dispatcher._backends["managed"] = backend
        old_listener = model_dispatch.listener_rss_gb
        model_dispatch.listener_rss_gb = lambda port: 99.0
        try:
            # Case 1: the only waiter cancels mid-load; the orphan load is rolled back.
            cancel_event = threading.Event()
            meta = {"request_id": "solo", "cancel_event": cancel_event, "metrics": {}}
            timer = threading.Timer(0.05, cancel_event.set)
            timer.start()
            try:
                dispatcher.begin_request("model", False, meta)
            except model_dispatch.DispatchError as exc:
                assert exc.status == 409
                assert exc.error_type == "request_cancelled"
            else:
                raise AssertionError("cancelled load unexpectedly succeeded")
            finally:
                timer.cancel()
            assert dispatcher._models["model"].state == "unloaded"
            assert dispatcher._models["model"].observed_memory_gb is None, "cancelled load must not publish memory evidence"
            assert backend.stopped, "orphan load must stop the managed service"

            # Case 2: another waiter exists, so the model stays loaded for them.
            backend.stopped = False
            cancel_a = threading.Event()
            meta_a = {"request_id": "a", "cancel_event": cancel_a, "metrics": {}}
            meta_b = {"request_id": "b", "cancel_event": threading.Event(), "metrics": {}}
            errors = []

            def run(meta):
                try:
                    dispatcher.begin_request("model", False, meta)
                except Exception as exc:
                    errors.append(exc)

            thread_a = threading.Thread(target=run, args=(meta_a,))
            thread_a.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and dispatcher._models["model"].state != "loading":
                time.sleep(0.01)
            assert dispatcher._models["model"].state == "loading"
            thread_b = threading.Thread(target=run, args=(meta_b,))
            thread_b.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and "b" not in dispatcher._requests:
                time.sleep(0.01)
            assert "b" in dispatcher._requests
            cancel_a.set()
            thread_a.join(timeout=5)
            thread_b.join(timeout=10)
            assert not thread_a.is_alive() and not thread_b.is_alive()
            assert len(errors) == 1 and errors[0].error_type == "request_cancelled"
            assert dispatcher._models["model"].state == "ready", "model must stay loaded for the surviving waiter"
            assert not backend.stopped
            dispatcher.end_request("b")
            dispatcher.unload("model")
            assert backend.stopped
        finally:
            model_dispatch.listener_rss_gb = old_listener
            dispatcher.shutdown()


def test_external_switch_and_unload_are_rejected():
    with tempfile.TemporaryDirectory(prefix="model-dispatch-external-test-") as tmp:
        root = Path(tmp)
        dispatcher = _dispatcher_for_models(
            root,
            {
                "one": {"type": "external", "endpoint": f"http://127.0.0.1:{free_port()}", "exclusive_group": "shared"},
                "two": {"type": "external", "endpoint": f"http://127.0.0.1:{free_port()}", "exclusive_group": "shared"},
            },
            {
                "model-one": {"adapter": "one", "lifecycle_owner": "service-one", "exclusive_groups": ["shared"]},
                "model-two": {"adapter": "two", "lifecycle_owner": "service-two", "exclusive_groups": ["shared"]},
            },
        )
        dispatcher.current_model = "model-one"
        dispatcher.state = "ready"
        try:
            dispatcher.activate("model-two")
        except model_dispatch.DispatchError as exc:
            assert exc.status == 409
            assert exc.error_type == "external_backend_conflict"
        else:
            raise AssertionError("cross-external switch unexpectedly succeeded")
        assert dispatcher.current_model == "model-one"
        try:
            dispatcher.unload()
        except model_dispatch.DispatchError as exc:
            assert exc.status == 409
            assert exc.error_type == "external_backend_not_owned"
        else:
            raise AssertionError("external unload unexpectedly succeeded")


def test_http_managed_model_actions_and_observe_noop():
    class ActionHandler(BaseHTTPRequestHandler):
        events = []
        fail_deactivate = False
        action_delay = 0.0

        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            if self.path == "/activity":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"active_requests": 0}')
            elif self.path == "/health":
                self.send_response(200)
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            ActionHandler.events.append((self.path, payload))
            time.sleep(ActionHandler.action_delay)
            status = 500 if self.fail_deactivate and self.path.startswith("/unload") else 200
            self.send_response(status)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), ActionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    try:
        with tempfile.TemporaryDirectory(prefix="model-dispatch-http-managed-test-") as tmp:
            root = Path(tmp)
            dispatcher = _dispatcher_for_models(
                root,
                {"http": {"type": "http-managed", "endpoint": endpoint, "active_requests_path": "/activity"}},
                {
                    "one": {"adapter": "http", "resource_group": "shared", "activate_path": "/load", "deactivate_path": "/unload", "activate_payload": {"slot": 1}, "deactivate_payload": {"drop": True}},
                    "two": {"adapter": "http", "resource_group": "shared", "activate_path": "/load-two", "deactivate_path": "/unload-two"},
                },
                resource_groups={"shared": {"capacity": 1}},
            )
            dispatcher.config = replace(dispatcher.config, connect_timeout_seconds=0.01, load_timeout_seconds=0.5)
            try:
                ActionHandler.action_delay = 0.05
                dispatcher.activate("one")
                ActionHandler.action_delay = 0.0
                ActionHandler.fail_deactivate = True
                try:
                    dispatcher.activate("two")
                except model_dispatch.DispatchError as exc:
                    assert exc.error_type == "model_deactivation_failed"
                    assert dispatcher.current_model == "one" and dispatcher.state == "ready"
                else:
                    raise AssertionError("deactivation failure unexpectedly switched model")
                ActionHandler.fail_deactivate = False
                ActionHandler.events.clear()
                dispatcher.activate("two")
                assert ActionHandler.events[:2] == [
                    ("/unload", {"drop": True, "model": "one"}),
                    ("/load-two", {"model": "two"}),
                ]
                dispatcher.unload()
                assert ActionHandler.events[2] == ("/unload-two", {"model": "two"})
                assert dispatcher.backend is not None and dispatcher.backend.process is None
            finally:
                dispatcher.shutdown()

            ActionHandler.events.clear()
            observed = _dispatcher_for_models(
                root / "observed",
                {"observe": {"type": "observe", "endpoint": endpoint}},
                {"model": {"adapter": "observe", "activate_path": "/load", "deactivate_path": "/unload"}},
            )
            try:
                observed.activate("model")
                try:
                    observed.unload()
                except model_dispatch.DispatchError as exc:
                    assert exc.error_type == "external_backend_not_owned"
                else:
                    raise AssertionError("observe backend unexpectedly unloaded")
                assert ActionHandler.events == []
            finally:
                observed.shutdown()

            class ManagedBackendStub:
                def __init__(self):
                    self.stopped = False

                def stop(self):
                    self.stopped = True

            managed = _dispatcher_for_models(
                root / "managed-native-unload",
                {"managed": {"type": "managed", "endpoint": endpoint, "command": [sys.executable, "-c", "pass"]}},
                {"model": {"adapter": "managed", "deactivate_path": "/unload"}},
            )
            try:
                backend = ManagedBackendStub()
                managed.backend = backend
                managed.current_model = "model"
                managed.state = "ready"
                managed.unload()
                assert backend.stopped, "non-resident native model unload must stop its owned idle server"
                assert managed.current_model is None and managed.state == "unloaded"
            finally:
                managed.shutdown()
    finally:
        server.shutdown()
        server.server_close()


def test_activation_rejects_busy_model_without_stale_switch():
    class DummyBackend:
        def __init__(self, adapter):
            self.adapter = adapter
            self.stopped = False

        def stop(self):
            self.stopped = True

        def ensure_started(self, timeout):
            return None

    with tempfile.TemporaryDirectory(prefix="model-dispatch-race-test-") as tmp:
        dispatcher = _dispatcher_for_models(
            Path(tmp),
            {
                "adapter-b": {
                    "type": "managed",
                    "endpoint": f"http://127.0.0.1:{free_port()}",
                    "command": [sys.executable, "-c", "pass"],
                },
            },
            {
                "model-one": {"adapter": "adapter-b", "lifecycle_owner": "model-dispatch"},
                "model-two": {"adapter": "adapter-b", "lifecycle_owner": "model-dispatch"},
            },
        )
        backend = DummyBackend(dispatcher.config.adapters["adapter-b"])
        dispatcher.backend = backend
        dispatcher.current_model = "model-one"
        dispatcher.state = "ready"
        dispatcher.active_requests = 1
        try:
            dispatcher.activate("model-two")
        except model_dispatch.DispatchError as exc:
            assert exc.error_type == "model_busy"
        else:
            raise AssertionError("activation waited for and replaced a busy model")
        assert dispatcher.current_model == "model-one"
        assert not backend.stopped, "busy activation must not touch the current backend"


def test_managed_port_and_failure_cleanup():
    with tempfile.TemporaryDirectory(prefix="model-dispatch-managed-test-") as tmp:
        root = Path(tmp)
        occupied_port = free_port()
        listener = socket.socket()
        listener.bind(("127.0.0.1", occupied_port))
        listener.listen()
        try:
            adapter = model_dispatch.AdapterConfig(
                name="managed",
                type="managed",
                command=(sys.executable, "-c", "import time; time.sleep(5)"),
                endpoint=f"http://127.0.0.1:{occupied_port}",
            )
            backend = model_dispatch.ProcessBackend(adapter, root)
            try:
                backend.ensure_started(0.2)
            except model_dispatch.DispatchError as exc:
                assert exc.error_type == "managed_port_in_use"
            else:
                raise AssertionError("occupied managed port unexpectedly accepted")
            assert backend.process is None
        finally:
            listener.close()

        failed = model_dispatch.AdapterConfig(
            name="failed",
            type="managed",
            command=(sys.executable, "-c", "import sys; sys.exit(3)"),
            endpoint=f"http://127.0.0.1:{free_port()}",
        )
        backend = model_dispatch.ProcessBackend(failed, root)
        try:
            backend.ensure_started(1)
        except model_dispatch.DispatchError as exc:
            assert exc.error_type == "backend_start_failed"
        else:
            raise AssertionError("failed managed process unexpectedly succeeded")
        assert backend.process is None


def test_packaged_backend_logs_stay_outside_bundle():
    bundle_root = Path("/tmp/InferenceDock.app/Contents")
    adapter = model_dispatch.AdapterConfig(name="managed", type="managed", port=12345, command=(sys.executable, "-c", "pass"))
    backend = model_dispatch.ProcessBackend(adapter, bundle_root)
    runtime_root = Path.home() / "Library" / "Logs" / "InferenceDock"
    assert backend.log_path == runtime_root / "managed.log"
    assert backend.events_path.parent == runtime_root
    assert backend.stop_file.parent == runtime_root / ".tmp"
    assert all(not str(path).startswith(str(bundle_root) + "/") for path in (backend.log_path, backend.events_path, backend.stop_file))

    old_root = model_dispatch.ROOT
    model_dispatch.ROOT = bundle_root
    try:
        with tempfile.TemporaryDirectory(prefix="model-dispatch-bundle-config-test-") as tmp:
            config_path = Path(tmp) / "engines.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "listen_port": free_port(),
                        "adapters": {"external": {"type": "external", "endpoint": f"http://127.0.0.1:{free_port()}"}},
                        "models": {"model": {"adapter": "external"}},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            config = model_dispatch.load_config(config_path)
            assert config.metrics_path == runtime_root / "request-metrics.jsonl"
            assert not str(config.metrics_path).startswith(str(bundle_root) + "/")
    finally:
        model_dispatch.ROOT = old_root


def test_endpoint_and_port_must_match():
    with tempfile.TemporaryDirectory(prefix="model-dispatch-config-test-") as tmp:
        path = Path(tmp) / "engines.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "listen_port": free_port(),
                    "adapters": {
                        "managed": {
                            "type": "managed",
                            "endpoint": "http://127.0.0.1:19001",
                            "port": 19002,
                            "command": [sys.executable, "-c", "pass"],
                        }
                    },
                    "models": {"m": {"adapter": "managed"}},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        try:
            model_dispatch.load_config(path)
        except model_dispatch.ConfigError as exc:
            assert "match the endpoint port" in str(exc)
        else:
            raise AssertionError("mismatched adapter endpoint and port accepted")


def test_managed_schema_paths_and_shared_port_switch():
    with tempfile.TemporaryDirectory(prefix="model-dispatch-shared-port-test-") as tmp:
        root = Path(tmp)
        port = free_port()
        fake = str(ROOT / "scripts" / "fake_backend.py")
        config_data = {
            "listen_port": free_port(),
            "load_timeout_seconds": 3,
            "adapters": {
                "managed-a": {
                    "type": "managed",
                    "endpoint": f"http://127.0.0.1:{port}",
                    "exclusive_group": "shared",
                    "working_directory": "~",
                    "log_path": "~/Library/Logs/model-dispatch-test-a.log",
                    "stop_timeout_seconds": 0.5,
                    "command": [sys.executable, fake, "--port", str(port), "--events", str(root / "a.events"), "--stop-file", str(root / "a.stop"), "--start-delay", "0"],
                },
                "managed-b": {
                    "type": "managed",
                    "endpoint": f"http://127.0.0.1:{port}",
                    "exclusive_group": "shared",
                    "command": [sys.executable, fake, "--port", str(port), "--events", str(root / "b.events"), "--stop-file", str(root / "b.stop"), "--start-delay", "0"],
                },
            },
            "resource_groups": {"shared": {"capacity": 1}},
            "models": {
                "model-a": {"adapter": "managed-a", "resource_group": "shared"},
                "model-b": {"adapter": "managed-b", "resource_group": "shared"},
            },
        }
        path = root / "engines.yaml"
        path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
        config = model_dispatch.load_config(path)
        assert config.adapters["managed-a"].working_directory == Path.home()
        expanded_log = Path.home() / "Library/Logs/model-dispatch-test-a.log"
        assert config.adapters["managed-a"].log_path == expanded_log
        assert config.adapters["managed-a"].stop_timeout_seconds == 0.5

        dispatcher = model_dispatch.ModelDispatcher(config, root)
        try:
            dispatcher.activate("model-a")
            first = dispatcher.backend.process
            assert first is not None
            dispatcher.activate("model-b")
            second = dispatcher.backend.process
            assert second is not None and second.pid != first.pid
            assert first.poll() is not None, "shared-port switch must stop the old managed process"
        finally:
            dispatcher.shutdown()
            expanded_log.unlink(missing_ok=True)

        for field, value in (("working_directory", 1), ("log_path", {}), ("stop_timeout_seconds", 0)):
            invalid = dict(config_data)
            invalid["adapters"] = {name: dict(adapter) for name, adapter in config_data["adapters"].items()}
            invalid["adapters"]["managed-a"][field] = value
            invalid_path = root / f"invalid-{field}.yaml"
            invalid_path.write_text(yaml.safe_dump(invalid, sort_keys=False), encoding="utf-8")
            try:
                model_dispatch.load_config(invalid_path)
            except model_dispatch.ConfigError:
                pass
            else:
                raise AssertionError(f"invalid {field} accepted")

        different_group = dict(config_data)
        different_group["adapters"] = {name: dict(adapter) for name, adapter in config_data["adapters"].items()}
        different_group["adapters"]["managed-b"]["exclusive_group"] = "other"
        different_group["resource_groups"] = {}
        different_group["models"] = {name: {key: value for key, value in model.items() if key != "resource_group"} for name, model in config_data["models"].items()}
        different_path = root / "different-group.yaml"
        different_path.write_text(yaml.safe_dump(different_group, sort_keys=False), encoding="utf-8")
        try:
            model_dispatch.load_config(different_path)
        except model_dispatch.ConfigError as exc:
            assert "shared without" in str(exc)
        else:
            raise AssertionError("managed adapters from different groups shared a port")


def test_startup_idle_unload_uses_persisted_settings():
    """A timeout set from the menu bar is persisted in settings.json while the
    config file keeps no idle_unload_seconds at all. The idle loop reads the
    config copy, so a core restart must merge the persisted value before it
    decides whether automatic unload is enabled."""
    with tempfile.TemporaryDirectory(prefix="model-dispatch-idle-startup-test-") as tmp:
        root = Path(tmp)
        root.mkdir(parents=True, exist_ok=True)
        (root / "settings.json").write_text(json.dumps({"idle_unload_seconds": 0.05}), encoding="utf-8")
        dispatcher = _dispatcher_for_models(
            root,
            {
                "managed": {
                    "type": "managed",
                    "endpoint": f"http://127.0.0.1:{free_port()}",
                    "command": [sys.executable, "-c", "pass"],
                }
            },
            {"model": {"adapter": "managed", "keep_resident": False}},
        )
        try:
            assert dispatcher.config.idle_unload_seconds == 0.05, (
                "a persisted idle timeout must reach the idle loop at startup"
            )
            assert dispatcher._idle_thread is not None and dispatcher._idle_thread.is_alive()
        finally:
            dispatcher.shutdown()


def test_activation_restarts_idle_clock():
    """A model switched in long after startup must not inherit the runtime
    object's creation-time idle clock, or the idle loop unloads it the moment
    it finishes loading."""
    with tempfile.TemporaryDirectory(prefix="model-dispatch-idle-clock-test-") as tmp:
        root = Path(tmp)
        root.mkdir(parents=True, exist_ok=True)
        (root / "settings.json").write_text(json.dumps({"idle_unload_seconds": 600}), encoding="utf-8")
        dispatcher = _dispatcher_for_models(
            root,
            {
                "managed": {
                    "type": "managed",
                    "endpoint": f"http://127.0.0.1:{free_port()}",
                    "command": [sys.executable, "-c", "pass"],
                }
            },
            {"model": {"adapter": "managed", "keep_resident": False}},
        )

        class StubBackend:
            stopped = False

            def healthy(self):
                return True

            def ensure_started(self, timeout):
                return None

            def stop(self):
                self.stopped = True

        dispatcher._backends["managed"] = StubBackend()
        try:
            with dispatcher.condition:
                dispatcher._models["model"].last_request_finished = time.monotonic() - 3600
            dispatcher.activate("model")
            assert dispatcher._models["model"].state == "ready", "activation must finish"
            idle_for = time.monotonic() - dispatcher._models["model"].last_request_finished
            assert idle_for < 5, f"activation must restart the idle clock, saw {idle_for:.1f}s"
        finally:
            dispatcher.shutdown()


def test_idle_unload_and_keep_resident():
    with tempfile.TemporaryDirectory(prefix="model-dispatch-idle-test-") as tmp:
        root = Path(tmp)
        dispatcher = _dispatcher_for_models(
            root,
            {
                "managed": {
                    "type": "managed",
                    "endpoint": f"http://127.0.0.1:{free_port()}",
                    "command": [sys.executable, "-c", "pass"],
                }
            },
            {"model": {"adapter": "managed", "keep_resident": False}},
            idle_unload_seconds=0.05,
        )
        backend = type("Backend", (), {"stop": lambda self: setattr(self, "stopped", True)})()
        backend.stopped = False
        dispatcher.backend = backend
        dispatcher.current_model = "model"
        dispatcher.state = "ready"
        dispatcher.last_request_finished = time.monotonic() - 1
        with dispatcher.condition:
            dispatcher.condition.notify_all()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and dispatcher.state != "unloaded":
            time.sleep(0.02)
        assert dispatcher.state == "unloaded"
        assert backend.stopped
        dispatcher.shutdown()

        resident = _dispatcher_for_models(
            root / "resident",
            {
                "managed": {
                    "type": "managed",
                    "endpoint": f"http://127.0.0.1:{free_port()}",
                    "command": [sys.executable, "-c", "pass"],
                    "keep_resident": True,
                }
            },
            {"model": {"adapter": "managed"}},
            idle_unload_seconds=0.05,
        )
        resident.backend = backend = type("Backend", (), {"stop": lambda self: setattr(self, "stopped", True)})()
        backend.stopped = False
        resident.current_model = "model"
        resident.state = "ready"
        resident.last_request_finished = time.monotonic() - 1
        with resident.condition:
            resident.condition.notify_all()
        time.sleep(0.15)
        assert resident.state == "ready"
        assert not backend.stopped
        resident.shutdown()

        observed = _dispatcher_for_models(
            root / "observed",
            {
                "external": {"type": "observe", "endpoint": f"http://127.0.0.1:{free_port()}"}
            },
            {"model": {"adapter": "external", "keep_resident": False}},
            idle_unload_seconds=0.05,
        )
        observed.backend = backend = type("Backend", (), {"stop": lambda self: setattr(self, "stopped", True)})()
        backend.stopped = False
        observed.current_model = "model"
        observed.state = "ready"
        observed.last_request_finished = time.monotonic() - 1
        with observed.condition:
            observed.condition.notify_all()
        time.sleep(0.15)
        assert observed.state == "ready"
        assert not backend.stopped
        observed.shutdown()


def test_external_different_groups_can_coexist():
    with tempfile.TemporaryDirectory(prefix="model-dispatch-groups-test-") as tmp:
        dispatcher = _dispatcher_for_models(
            Path(tmp),
            {
                "one": {"type": "external", "endpoint": f"http://127.0.0.1:{free_port()}"},
                "two": {"type": "external", "endpoint": f"http://127.0.0.1:{free_port()}"},
            },
            {
                "model-one": {"adapter": "one", "exclusive_group": "group-one", "lifecycle_owner": "one"},
                "model-two": {"adapter": "two", "exclusive_group": "group-two", "lifecycle_owner": "two"},
            },
        )
        backend = model_dispatch.ProcessBackend(dispatcher.config.adapters["two"], Path(tmp))
        backend.ensure_started = lambda timeout: None
        dispatcher.backend = backend
        dispatcher.current_model = "model-one"
        dispatcher.state = "ready"
        dispatcher.activate("model-two")
        assert dispatcher.current_model == "model-two"


def test_fake_adapters_in_different_groups_can_coexist():
    with tempfile.TemporaryDirectory(prefix="model-dispatch-multi-instance-test-") as tmp:
        root = Path(tmp)
        port_one = free_port()
        port_two = free_port()
        fake = str(ROOT / "scripts" / "fake_backend.py")
        dispatcher = _dispatcher_for_models(
            root,
            {
                "one": {"type": "fake", "python": sys.executable, "script": fake, "port": port_one, "events_path": str(root / "one.events"), "start_delay_seconds": 0},
                "two": {"type": "fake", "python": sys.executable, "script": fake, "port": port_two, "events_path": str(root / "two.events"), "start_delay_seconds": 0},
            },
            {
                "model-one": {"adapter": "one", "resource_group": "group-one"},
                "model-two": {"adapter": "two", "resource_group": "group-two"},
            },
            resource_groups={"group-one": {"capacity": 1}, "group-two": {"capacity": 1}},
        )
        try:
            dispatcher.activate("model-one")
            dispatcher.settings["smart_scheduling"] = True
            dispatcher._requests["active-one"] = {
                "request_id": "active-one", "model": "model-one", "cancel_event": threading.Event(),
                "metrics": {}, "started": time.monotonic(), "started_wall": time.time(), "upstream": None,
            }
            dispatcher.activate("model-two")
            one = dispatcher._backends["one"].process
            two = dispatcher._backends["two"].process
            assert one is not None and two is not None and one.pid != two.pid
            assert one.poll() is None and two.poll() is None
            status = dispatcher.status()
            assert status["active_models"] == ["model-one", "model-two"]
            assert status["resource_groups"]["group-one"]["used"] == 1
            assert status["resource_groups"]["group-two"]["used"] == 1
            dispatcher.end_request("active-one")
        finally:
            dispatcher.shutdown()


def test_capacity_busy_model_waits_then_replaces():
    with tempfile.TemporaryDirectory(prefix="model-dispatch-capacity-test-") as tmp:
        root = Path(tmp)
        port_one = free_port()
        port_two = free_port()
        fake = str(ROOT / "scripts" / "fake_backend.py")
        dispatcher = _dispatcher_for_models(
            root,
            {
                "one": {"type": "fake", "python": sys.executable, "script": fake, "port": port_one, "events_path": str(root / "one.events"), "start_delay_seconds": 0},
                "two": {"type": "fake", "python": sys.executable, "script": fake, "port": port_two, "events_path": str(root / "two.events"), "start_delay_seconds": 0},
            },
            {
                "model-one": {"adapter": "one", "resource_group": "shared"},
                "model-two": {"adapter": "two", "resource_group": "shared"},
            },
            resource_groups={"shared": {"capacity": 1}},
        )
        try:
            dispatcher.activate("model-one")
            first_process = dispatcher._backends["one"].process
            dispatcher.update_settings({"memory_limit_gb": 8})
            try:
                dispatcher.activate("model-two")
            except model_dispatch.DispatchError as exc:
                assert exc.error_type == "memory_estimate_required"
            else:
                raise AssertionError("switch without a target memory estimate was admitted")
            assert dispatcher._models["model-one"].state == "ready", "failed admission unloaded the current model"
            dispatcher.update_settings({"memory_limit_gb": None})
            dispatcher.config = replace(dispatcher.config, load_timeout_seconds=1)
            dispatcher._requests["busy-one"] = {
                "request_id": "busy-one",
                "model": "model-one",
                "cancel_event": threading.Event(),
                "metrics": {},
                "started": time.monotonic(),
                "started_wall": time.time(),
                "upstream": None,
            }
            busy_started = time.monotonic()
            try:
                dispatcher.activate("model-two")
            except model_dispatch.DispatchError as exc:
                assert exc.status == 409 and exc.error_type == "model_busy"
            else:
                raise AssertionError("busy capacity conflict unexpectedly replaced the model")
            assert time.monotonic() - busy_started < 0.5, "busy switch should fail immediately"
            assert dispatcher._models["model-one"].state == "ready"
            assert dispatcher._backends["one"].process.poll() is None
            assert "two" not in dispatcher._backends
            dispatcher.end_request("busy-one")
            dispatcher.config = replace(dispatcher.config, load_timeout_seconds=3)
            dispatcher.activate("model-two")
            assert dispatcher._models["model-one"].state == "unloaded"
            assert dispatcher._models["model-two"].state == "ready"
            assert first_process is not None and first_process.poll() is not None
            assert dispatcher._backends["two"].process.poll() is None
        finally:
            dispatcher.shutdown()


def test_capacity_two_evicts_only_one_idle_model():
    with tempfile.TemporaryDirectory(prefix="model-dispatch-capacity-two-test-") as tmp:
        root = Path(tmp)
        fake = str(ROOT / "scripts" / "fake_backend.py")
        ports = [free_port() for _ in range(3)]
        dispatcher = _dispatcher_for_models(
            root,
            {
                name: {"type": "fake", "python": sys.executable, "script": fake, "port": port, "events_path": str(root / f"{name}.events"), "start_delay_seconds": 0}
                for name, port in zip(("one", "two", "three"), ports)
            },
            {f"model-{name}": {"adapter": name, "resource_group": "shared"} for name in ("one", "two", "three")},
            resource_groups={"shared": {"capacity": 2}},
        )
        try:
            dispatcher.activate("model-one")
            dispatcher.activate("model-two")
            dispatcher.activate("model-three")
            ready = {name for name, runtime in dispatcher._models.items() if runtime.state == "ready"}
            assert len(ready) == 2 and "model-three" in ready
            assert dispatcher.status()["resource_groups"]["shared"]["used"] == 2
        finally:
            dispatcher.shutdown()


def test_targeted_unload_preserves_other_service_and_rejects_busy():
    with tempfile.TemporaryDirectory(prefix="model-dispatch-target-unload-test-") as tmp:
        root = Path(tmp)
        fake = str(ROOT / "scripts" / "fake_backend.py")
        dispatcher = _dispatcher_for_models(
            root,
            {
                "one": {"type": "fake", "python": sys.executable, "script": fake, "port": free_port(), "events_path": str(root / "one.events"), "start_delay_seconds": 0},
                "two": {"type": "fake", "python": sys.executable, "script": fake, "port": free_port(), "events_path": str(root / "two.events"), "start_delay_seconds": 0},
            },
            {"model-one": {"adapter": "one"}, "model-two": {"adapter": "two"}},
        )
        try:
            dispatcher.activate("model-one")
            dispatcher.activate("model-two")
            dispatcher._requests["busy"] = {
                "request_id": "busy", "model": "model-one", "cancel_event": threading.Event(),
                "metrics": {}, "started": time.monotonic(), "started_wall": time.time(), "upstream": None,
            }
            try:
                dispatcher.unload("model-one")
            except model_dispatch.DispatchError as exc:
                assert exc.error_type == "model_busy"
            else:
                raise AssertionError("busy model was unloaded")
            dispatcher.end_request("busy")
            dispatcher.unload("model-one")
            assert dispatcher._models["model-one"].state == "unloaded"
            assert dispatcher._models["model-two"].state == "ready"
            assert dispatcher._backends["two"].process.poll() is None
        finally:
            dispatcher.shutdown()


def test_memory_admission_counts_loaded_reserved_external_and_pressure():
    with tempfile.TemporaryDirectory(prefix="model-dispatch-memory-test-") as tmp:
        root = Path(tmp)
        dispatcher = _dispatcher_for_models(
            root,
            {
                "external": {"type": "external", "endpoint": f"http://127.0.0.1:{free_port()}"},
                "managed": {"type": "managed", "endpoint": f"http://127.0.0.1:{free_port()}", "command": [sys.executable, "-c", "pass"]},
            },
            {
                "resident": {"adapter": "external", "estimated_memory_gb": 4},
                "candidate": {"adapter": "managed", "estimated_memory_gb": 5},
            },
        )
        old_pressure = model_dispatch.memory_pressure_level
        model_dispatch.memory_pressure_level = lambda: 100
        try:
            dispatcher.update_settings({"memory_limit_gb": 8})
            dispatcher._models["resident"].state = "ready"
            dispatcher._external_memory_gb = lambda exclude_adapters=None: 4
            try:
                dispatcher._check_memory(dispatcher.config.models["candidate"])
            except model_dispatch.DispatchError as exc:
                assert exc.error_type == "memory_limit_exceeded"
                assert "external 4GB" in exc.message
            else:
                raise AssertionError("loaded external memory was not admitted")

            dispatcher._models["resident"].state = "unloaded"
            dispatcher._external_memory_gb = lambda exclude_adapters=None: 0
            extra = model_dispatch.ModelConfig(
                name="loading", adapter="managed", backend_model="loading",
                resource_group=None, capabilities={"chat": True}, lifecycle_owner="model-dispatch",
                estimated_memory_gb=4,
            )
            dispatcher.config.models["loading"] = extra
            dispatcher._models["loading"] = model_dispatch.ModelRuntime(state="loading", loading_reservation_gb=4)
            try:
                dispatcher._check_memory(dispatcher.config.models["candidate"])
            except model_dispatch.DispatchError as exc:
                assert exc.error_type == "memory_limit_exceeded"
                assert "loading 4GB" in exc.message
            else:
                raise AssertionError("loading reservation was not admitted")

            dispatcher._models["resident"].state = "unloaded"
            dispatcher._models["resident"].loading_reservation_gb = None
            dispatcher.update_settings({"memory_limit_gb": None})
            model_dispatch.memory_pressure_level = lambda: 49
            try:
                dispatcher._check_memory(dispatcher.config.models["candidate"])
            except model_dispatch.DispatchError as exc:
                assert exc.error_type == "memory_pressure"
            else:
                raise AssertionError("memory pressure did not reject a new load")
        finally:
            model_dispatch.memory_pressure_level = old_pressure
            dispatcher.shutdown()


def test_fake_backend_crash_recovery():
    with tempfile.TemporaryDirectory(prefix="model-dispatch-crash-test-") as tmp:
        root = Path(tmp)
        port = free_port()
        adapter = model_dispatch.AdapterConfig(
            name="fake",
            type="fake",
            python=sys.executable,
            script=str(ROOT / "scripts" / "fake_backend.py"),
            port=port,
            events_path=str(root / "events.jsonl"),
        )
        backend = model_dispatch.ProcessBackend(adapter, root)
        backend.ensure_started(3)
        first_pid = backend.process.pid
        os.kill(first_pid, signal.SIGKILL)
        backend.process.wait(timeout=3)
        backend.ensure_started(3)
        assert backend.process is not None and backend.process.pid != first_pid
        backend.stop()


def test_managed_native_lifecycle_reuses_healthy_unowned_service():
    class NativeHandler(BaseHTTPRequestHandler):
        events = []

        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            if self.path == "/activity":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"active_requests": 0}')
                return
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            NativeHandler.events.append(self.path)
            self.send_response(200)
            self.end_headers()

    old_pressure = model_dispatch.memory_pressure_level
    model_dispatch.memory_pressure_level = lambda: 100
    try:
        with tempfile.TemporaryDirectory(prefix="model-dispatch-managed-adopt-test-") as tmp:
            root = Path(tmp)
            server = ThreadingHTTPServer(("127.0.0.1", 0), NativeHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            endpoint = f"http://127.0.0.1:{server.server_port}"
            dispatcher = _dispatcher_for_models(
                root,
                {"managed": {"type": "managed", "endpoint": endpoint, "command": [sys.executable, "-c", "pass"], "active_requests_path": "/activity"}},
                {"model": {"adapter": "managed", "activate_path": "/load", "deactivate_path": "/unload"}},
            )
            try:
                dispatcher.activate("model")
                assert dispatcher._models["model"].state == "ready"
                assert dispatcher._backends["managed"].process is None
                assert NativeHandler.events == ["/load"]
                dispatcher.unload("model")
                assert NativeHandler.events == ["/load", "/unload"]
            finally:
                dispatcher.shutdown()
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
    finally:
        model_dispatch.memory_pressure_level = old_pressure


def test_shared_native_routes_and_external_activity_fail_closed():
    with tempfile.TemporaryDirectory(prefix="model-dispatch-native-route-test-") as tmp:
        dispatcher = _dispatcher_for_models(
            Path(tmp),
            {"external": {"type": "http-managed", "endpoint": f"http://127.0.0.1:{free_port()}"}},
            {
                "one": {"adapter": "external", "activate_path": "/load", "deactivate_path": "/unload"},
                "two": {"adapter": "external", "activate_path": "/load", "deactivate_path": "/unload"},
            },
        )
        old_http_json = model_dispatch.http_json
        try:
            dispatcher._models["one"].state = "ready"
            assert dispatcher._capacity_blockers(dispatcher.config.models["two"]) == []
            assert dispatcher._external_activity(dispatcher.config.models["one"]) is True

            dispatcher.config.adapters["external"] = replace(
                dispatcher.config.adapters["external"], active_requests_path="/activity", model_state_path="/models"
            )
            model_dispatch.http_json = lambda url, timeout, headers=None: (200, {"active_requests": 0})
            assert dispatcher._external_activity(dispatcher.config.models["one"]) is False
            model_dispatch.http_json = lambda url, timeout, headers=None: (200, {"gauges": {"requests_running": 1, "requests_waiting": 0}})
            assert dispatcher._external_activity(dispatcher.config.models["one"]) is True
            model_dispatch.http_json = lambda url, timeout, headers=None: (503, {})
            assert dispatcher._external_activity(dispatcher.config.models["one"]) is True
            model_dispatch.http_json = lambda url, timeout, headers=None: (200, {"data": []})
            dispatcher._ensure_deactivated(dispatcher.config.adapters["external"], dispatcher.config.models["one"])
            try:
                dispatcher._ensure_activated(dispatcher.config.adapters["external"], dispatcher.config.models["one"])
            except model_dispatch.DispatchError as exc:
                assert exc.error_type == "model_activation_unverified"
            else:
                raise AssertionError("backend-reported unloaded model passed activation verification")
            model_dispatch.http_json = lambda url, timeout, headers=None: (200, {"data": [{"id": "one"}]})
            dispatcher._ensure_activated(dispatcher.config.adapters["external"], dispatcher.config.models["one"])
            try:
                dispatcher._ensure_deactivated(dispatcher.config.adapters["external"], dispatcher.config.models["one"])
            except model_dispatch.DispatchError as exc:
                assert exc.error_type == "model_deactivation_unverified"
            else:
                raise AssertionError("backend-reported loaded model passed unload verification")
            model_dispatch.http_json = lambda url, timeout, headers=None: (200, {"data": [{"id": "one", "loaded": False}]})
            dispatcher._ensure_deactivated(dispatcher.config.adapters["external"], dispatcher.config.models["one"])
            dispatcher._refresh_observed_states()
            assert dispatcher._models["one"].state == "unloaded"
        finally:
            model_dispatch.http_json = old_http_json
            dispatcher.shutdown()


def test_owned_managed_activity_probe_blocks_direct_unload():
    """A direct backend request must protect an owned managed process too."""
    class ActivityHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                self.send_response(200)
                self.end_headers()
                return
            if self.path == "/activity":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"active_requests":1}')
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, fmt, *args):
            return

    class AliveProcess:
        def poll(self):
            return None

    class Backend:
        def __init__(self):
            self.process = AliveProcess()

        def stop(self):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), ActivityHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory(prefix="model-dispatch-managed-activity-test-") as tmp:
        endpoint = f"http://127.0.0.1:{server.server_port}"
        dispatcher = _dispatcher_for_models(
            Path(tmp),
            {"managed": {"type": "managed", "endpoint": endpoint, "command": [sys.executable, "-c", "pass"], "active_requests_path": "/activity"}},
            {"model": {"adapter": "managed"}},
        )
        try:
            dispatcher._backends["managed"] = Backend()
            dispatcher._models["model"].state = "ready"
            try:
                dispatcher.unload("model")
            except model_dispatch.DispatchError as exc:
                assert exc.status == 409 and exc.error_type == "model_busy"
            else:
                raise AssertionError("direct activity did not block owned managed unload")
        finally:
            dispatcher.shutdown()
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def test_dead_owned_process_clears_stale_ready_state():
    class DeadProcess:
        def poll(self):
            return 1

    class Backend:
        process = DeadProcess()

        def stop(self):
            pass

    with tempfile.TemporaryDirectory(prefix="model-dispatch-dead-process-test-") as tmp:
        dispatcher = _dispatcher_for_models(
            Path(tmp),
            {"managed": {"type": "managed", "endpoint": f"http://127.0.0.1:{free_port()}", "command": [sys.executable, "-c", "pass"]}},
            {"model": {"adapter": "managed"}},
        )
        try:
            dispatcher._backends["managed"] = Backend()
            dispatcher._models["model"].state = "ready"
            assert dispatcher.model_entries()[0]["state"] == "unloaded"
        finally:
            dispatcher.shutdown()


def test_unverified_probe_is_visible_and_fail_closed():
    with tempfile.TemporaryDirectory(prefix="model-dispatch-unknown-state-test-") as tmp:
        dispatcher = _dispatcher_for_models(
            Path(tmp),
            {"external": {"type": "external", "endpoint": f"http://127.0.0.1:{free_port()}", "model_state_path": "/models"}},
            {"model": {"adapter": "external", "deactivate_path": "/unload"}},
        )
        old_http_json = model_dispatch.http_json
        try:
            dispatcher._models["model"].state = "ready"
            model_dispatch.http_json = lambda url, timeout, headers=None: (503, {})
            entry = dispatcher.model_entries()[0]
            assert entry["state"] == "unknown"
            assert entry["observed_state"] == "unknown"
            assert dispatcher.state == "unknown"
            try:
                dispatcher.unload("model")
            except model_dispatch.DispatchError as exc:
                assert exc.error_type == "model_state_unverified"
            else:
                raise AssertionError("unknown backend state was unloaded")
            model_dispatch.http_json = lambda url, timeout, headers=None: (200, {"data": [{"id": "backend", "loaded": True}]})
            dispatcher.config.models["model"] = replace(dispatcher.config.models["model"], backend_model="backend")
            assert dispatcher.model_entries()[0]["state"] == "ready"
        finally:
            model_dispatch.http_json = old_http_json
            dispatcher.shutdown()


def test_persistent_metric_aggregates_are_content_free():
    with tempfile.TemporaryDirectory(prefix="model-dispatch-metrics-test-") as tmp:
        recorder = model_dispatch.RequestRecorder(Path(tmp) / "metrics.jsonl")
        recorder.record({"ts": 1, "model": "m", "cold_load_ms": 100, "tokens_per_second": 10, "cached_tokens": 0, "finish_reason": "stop", "prompt": "secret"})
        recorder.record({"ts": 2, "model": "m", "tokens_per_second": 20, "cached_tokens": 4, "finish_reason": "stop"})
        payload = recorder.snapshot()
        aggregate = payload["persistent_summaries"]["models"]["m"]
        assert aggregate["cold_start"]["metrics"]["tokens_per_second"]["median"] == 10.0
        assert aggregate["cache_hit"]["metrics"]["cached_tokens"]["mean"] == 4.0
        assert "prompt" not in json.dumps(payload)


def test_responses_metrics_status_and_unknown_cache():
    with tempfile.TemporaryDirectory(prefix="model-dispatch-responses-metrics-test-") as tmp:
        recorder = model_dispatch.RequestRecorder(Path(tmp) / "metrics.jsonl")
        handler = object.__new__(model_dispatch.DispatchHandler)
        metrics = {"protocol": "responses", "_started_at": time.monotonic() - 0.01}
        handler._consume_json_metrics(
            json.dumps({
                "status": "completed",
                "usage": {"input_tokens": 3, "input_tokens_details": {"cached_tokens": 2}},
                "timings": {"predicted_per_second": 11.0, "prompt_per_second": 99.0},
            }).encode(),
            metrics,
        )
        assert metrics["response_status"] == "completed"
        assert metrics["finish_reason"] == "stop"
        assert metrics["cached_tokens"] == 2
        assert metrics["tokens_per_second"] == 11.0 and metrics["prefill_tokens_per_second"] == 99.0
        recorder.record({"ts": 1, "model": "m", "finish_reason": "failed", "tokens_per_second": 999})
        recorder.record({"ts": 2, "model": "m", "finish_reason": "completed", "tokens_per_second": 10})
        recorder.record({"ts": 3, "model": "m", "finish_reason": "completed", "tokens_per_second": 12})
        summary = recorder.snapshot()["persistent_summaries"]["models"]["m"]
        assert "cache_miss" not in summary and summary["cache_unknown"]["count"] == 2
        assert summary["warm"]["count"] == 2


def test_observed_memory_evidence_is_audit_only():
    with tempfile.TemporaryDirectory(prefix="model-dispatch-memory-evidence-test-") as tmp:
        dispatcher = _dispatcher_for_models(
            Path(tmp),
            {"managed": {"type": "managed", "endpoint": f"http://127.0.0.1:{free_port()}", "command": [sys.executable, "-c", "pass"]}},
            {"model": {"adapter": "managed", "estimated_memory_gb": 8}},
        )
        class Backend:
            process = object()
            def ensure_started(self, timeout):
                return None
            def stop(self):
                return None
        old_listener = model_dispatch.listener_rss_gb
        model_dispatch.listener_rss_gb = lambda port: 12.5
        dispatcher._backends["managed"] = Backend()
        try:
            dispatcher.activate("model")
            entry = dispatcher.model_entries()[0]
            assert entry["configured_memory_gb"] == 8.0
            assert entry["observed_memory_gb"] == 12.5
            assert entry["observed_memory_source"] == "listener_rss_post_load"
            assert dispatcher._effective_estimate(dispatcher.config.models["model"]) == 8
        finally:
            model_dispatch.listener_rss_gb = old_listener
            dispatcher.shutdown()


def test_mlx_metal_timeout_recovers_once_before_response():
    class UpstreamHandler(BaseHTTPRequestHandler):
        attempts = 0
        saw_auth = False

        def do_POST(self):
            UpstreamHandler.attempts += 1
            UpstreamHandler.saw_auth = UpstreamHandler.saw_auth or self.headers.get("Authorization") == "Bearer test-secret"
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(500 if UpstreamHandler.attempts == 1 else 200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            body = b'{"error":"Metal GPU timeout in command buffer"}' if UpstreamHandler.attempts == 1 else b'{"choices":[{"finish_reason":"stop"}]}'
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    class AliveProcess:
        def poll(self):
            return None

    class ManagedBackend:
        def __init__(self, adapter):
            self.adapter = adapter
            self.process = AliveProcess()
            self.stop_count = 0
            self.start_count = 0

        def ensure_started(self, timeout):
            self.start_count += 1

        def stop(self):
            self.stop_count += 1

    upstream_server = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    upstream_thread = threading.Thread(target=upstream_server.serve_forever, daemon=True)
    upstream_thread.start()
    with tempfile.TemporaryDirectory(prefix="model-dispatch-metal-recovery-test-") as tmp:
        dispatcher = _dispatcher_for_models(
            Path(tmp),
            {"mlx-serve": {"type": "managed", "plugin_id": "mlx-serve", "endpoint": f"http://127.0.0.1:{upstream_server.server_port}", "command": [sys.executable, "-c", "pass"], "api_key_env": "TEST_BACKEND_KEY"}},
            {"model": {"adapter": "mlx-serve"}},
        )
        backend = ManagedBackend(dispatcher.config.adapters["mlx-serve"])
        dispatcher._backends["mlx-serve"] = backend
        server = model_dispatch.DispatchHTTPServer(("127.0.0.1", 0), model_dispatch.DispatchHandler, dispatcher)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        old_key = os.environ.get("TEST_BACKEND_KEY")
        os.environ["TEST_BACKEND_KEY"] = "test-secret"
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                data=json.dumps({"model": "model", "messages": [{"role": "user", "content": "hi"}]}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                assert response.status == 200
            assert UpstreamHandler.attempts == 2
            assert UpstreamHandler.saw_auth is True
            assert backend.stop_count == 1 and backend.start_count >= 2
            deadline = time.monotonic() + 1
            while dispatcher.recorder.latest() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert dispatcher.recorder.latest()["recovery_succeeded"] is True
            class DeadProcess:
                def poll(self):
                    return 1
            backend.process = DeadProcess()
            dispatcher._requests["retry-check"] = {"model": "model"}
            checker = object.__new__(model_dispatch.DispatchHandler)
            checker.server = server
            assert checker._can_recover_metal_timeout(dispatcher.config.models["model"], "retry-check") is True
            dispatcher._requests.pop("retry-check", None)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            dispatcher.shutdown()
            if old_key is None:
                os.environ.pop("TEST_BACKEND_KEY", None)
            else:
                os.environ["TEST_BACKEND_KEY"] = old_key
    upstream_server.shutdown()
    upstream_server.server_close()
    upstream_thread.join(timeout=3)


def test_mlx_generic_error_recovers_during_model_load():
    class LoadHandler(BaseHTTPRequestHandler):
        loads = 0
        saw_auth = False

        def do_GET(self):
            LoadHandler.saw_auth = LoadHandler.saw_auth or self.headers.get("Authorization") == "Bearer load-secret"
            self.send_response(200)
            self.end_headers()

        def do_POST(self):
            LoadHandler.loads += 1
            LoadHandler.saw_auth = LoadHandler.saw_auth or self.headers.get("Authorization") == "Bearer load-secret"
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(500 if LoadHandler.loads == 1 else 200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if LoadHandler.loads == 1:
                self.wfile.write(b'{"error":"MlxError"}')
            else:
                self.wfile.write(b'{"ok":true}')

        def log_message(self, fmt, *args):
            return

    class Process:
        def __init__(self):
            self.exit_code = None

        def poll(self):
            return self.exit_code

    class Backend:
        def __init__(self, adapter):
            self.adapter = adapter
            self.process = Process()
            self.starts = 0
            self.stops = 0

        def healthy(self):
            return True

        def ensure_started(self, timeout):
            self.starts += 1
            self.process = Process()

        def stop(self):
            self.stops += 1
            self.process.exit_code = 1

    upstream_server = ThreadingHTTPServer(("127.0.0.1", 0), LoadHandler)
    upstream_thread = threading.Thread(target=upstream_server.serve_forever, daemon=True)
    upstream_thread.start()
    with tempfile.TemporaryDirectory(prefix="model-dispatch-metal-load-test-") as tmp:
        dispatcher = _dispatcher_for_models(
            Path(tmp),
            {"mlx-serve": {"type": "managed", "plugin_id": "mlx-serve", "endpoint": f"http://127.0.0.1:{upstream_server.server_port}", "command": [sys.executable, "-c", "pass"], "api_key_env": "TEST_LOAD_BACKEND_KEY"}},
            {"model": {"adapter": "mlx-serve", "activate_path": "/load"}},
        )
        backend = Backend(dispatcher.config.adapters["mlx-serve"])
        dispatcher._backends["mlx-serve"] = backend
        metrics = {}
        meta = {
            "request_id": "load-retry",
            "cancel_event": threading.Event(),
            "metrics": metrics,
            "recovery_state": {"used": False, "metrics": metrics},
        }
        old_key = os.environ.get("TEST_LOAD_BACKEND_KEY")
        os.environ["TEST_LOAD_BACKEND_KEY"] = "load-secret"
        try:
            dispatcher.begin_request("model", False, meta)
            assert LoadHandler.loads == 2
            assert LoadHandler.saw_auth is True
            assert backend.stops == 1 and backend.starts == 2
            assert dispatcher._models["model"].state == "ready"
            assert meta["metrics"]["recovery_succeeded"] is True
            dispatcher.end_request("load-retry")
        finally:
            dispatcher.shutdown()
            if old_key is None:
                os.environ.pop("TEST_LOAD_BACKEND_KEY", None)
            else:
                os.environ["TEST_LOAD_BACKEND_KEY"] = old_key
    upstream_server.shutdown()
    upstream_server.server_close()
    upstream_thread.join(timeout=3)


def test_backend_auth_header_covers_state_and_activity_probes():
    with tempfile.TemporaryDirectory(prefix="model-dispatch-auth-probe-test-") as tmp:
        dispatcher = _dispatcher_for_models(
            Path(tmp),
            {
                "external": {
                    "type": "external",
                    "endpoint": "http://127.0.0.1:19999",
                    "api_key_env": "TEST_PROBE_BACKEND_KEY",
                    "active_requests_path": "/activity",
                    "model_state_path": "/models",
                    "models_path": "/v1/models",
                }
            },
            {"model": {"adapter": "external", "backend_model": "backend"}},
        )
        captured = []
        old_http_json = model_dispatch.http_json
        old_key = os.environ.get("TEST_PROBE_BACKEND_KEY")
        os.environ["TEST_PROBE_BACKEND_KEY"] = "probe-secret"
        try:
            def fake_http_json(url, timeout, headers=None):
                captured.append((url, headers or {}))
                if url.endswith("/activity"):
                    return 200, {"active_requests": 0}
                if url.endswith("/models"):
                    return 200, {"data": [{"id": "backend", "loaded": True}]}
                if url.endswith("/v1/models"):
                    return 200, {"data": [{"id": "backend"}]}
                return 200, {}

            model_dispatch.http_json = fake_http_json
            assert dispatcher._external_activity(dispatcher.config.models["model"]) is False
            assert dispatcher._reported_model_loaded(
                dispatcher.config.adapters["external"], dispatcher.config.models["model"]
            ) is True
            result = dispatcher.reconcile_models()
            assert result["results"][0]["status"] == "ok"
            assert captured and all(item[1].get("Authorization") == "Bearer probe-secret" for item in captured)
        finally:
            model_dispatch.http_json = old_http_json
            dispatcher.shutdown()
            if old_key is None:
                os.environ.pop("TEST_PROBE_BACKEND_KEY", None)
            else:
                os.environ["TEST_PROBE_BACKEND_KEY"] = old_key


def test_backend_auth_missing_env_is_typed_and_secret_free():
    adapter = model_dispatch.AdapterConfig(name="external", type="external", api_key_env="TEST_MISSING_BACKEND_KEY")
    old_key = os.environ.pop("TEST_MISSING_BACKEND_KEY", None)
    try:
        try:
            model_dispatch.backend_headers(adapter)
        except model_dispatch.DispatchError as exc:
            assert exc.error_type == "backend_credentials_missing"
            assert "TEST_MISSING_BACKEND_KEY" in exc.message
            assert "secret" not in exc.message.lower()
        else:
            raise AssertionError("missing backend credentials were accepted")
    finally:
        if old_key is not None:
            os.environ["TEST_MISSING_BACKEND_KEY"] = old_key


def test_identity_validation_and_diagnostics():
    with tempfile.TemporaryDirectory(prefix="model-dispatch-identity-test-") as tmp:
        root = Path(tmp)

        def write(models: dict, name: str) -> Path:
            path = root / name
            path.write_text(
                yaml.safe_dump(
                    {
                        "listen_port": free_port(),
                        "adapters": {"ext": {"type": "external", "endpoint": f"http://127.0.0.1:{free_port()}"}},
                        "models": models,
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            return path

        duplicate = write(
            {
                "m1": {"adapter": "ext", "backend_model": "same"},
                "m2": {"adapter": "ext", "backend_model": "same"},
            },
            "duplicate.yaml",
        )
        try:
            model_dispatch.load_config(duplicate)
        except model_dispatch.ConfigError as exc:
            assert "aliases" in str(exc)
        else:
            raise AssertionError("duplicate backend identity accepted")

        shadowing = write(
            {
                "m1": {"adapter": "ext", "backend_model": "real"},
                "m2": {"adapter": "ext", "backend_model": "other", "aliases": ["real"]},
            },
            "shadowing.yaml",
        )
        try:
            model_dispatch.load_config(shadowing)
        except model_dispatch.ConfigError as exc:
            assert "shadows" in str(exc)
        else:
            raise AssertionError("alias shadowing a backend id accepted")

        hidden_target = write(
            {
                "m1": {"adapter": "ext", "backend_model": "real", "advertise": False, "aliases": ["legacy"]},
            },
            "hidden.yaml",
        )
        hidden_config = model_dispatch.load_config(hidden_target)
        assert hidden_config.model_aliases["legacy"] == "m1", "hidden canonical keeps its legacy alias"
        assert not hidden_config.models["m1"].advertise

        disabled_target = write(
            {
                "m1": {"adapter": "ext", "backend_model": "real", "enabled": False, "aliases": ["legacy"]},
            },
            "disabled.yaml",
        )
        try:
            model_dispatch.load_config(disabled_target)
        except model_dispatch.ConfigError as exc:
            assert "disabled" in str(exc)
        else:
            raise AssertionError("alias pointing at a disabled model accepted")

        # A disabled duplicate is allowed at load time but reported as drift.
        dispatcher = _dispatcher_for_models(
            root / "ok",
            {"ext": {"type": "external", "endpoint": f"http://127.0.0.1:{free_port()}"}},
            {
                "m1": {"adapter": "ext", "backend_model": "same", "lifecycle_owner": "ext"},
                "m2": {"adapter": "ext", "backend_model": "same", "lifecycle_owner": "ext", "enabled": False},
            },
        )
        try:
            result = dispatcher.reconcile_models()
            duplicates = [d for d in result["diagnostics"] if d["type"] == "duplicate_backend_identity"]
            assert duplicates == [{"type": "duplicate_backend_identity", "adapter": "ext", "backend_model": "same", "models": ["m1", "m2"]}]
        finally:
            dispatcher.shutdown()


def test_user_config_preferred_over_bundled():
    with tempfile.TemporaryDirectory(prefix="model-dispatch-user-config-test-") as tmp:
        root = Path(tmp)
        bundled_dir = root / "InferenceDock.app" / "Contents" / "Resources" / "model-dispatch-config"
        bundled_dir.mkdir(parents=True)
        bundled = bundled_dir / "engines.yaml"
        bundled.write_text(
            yaml.safe_dump(
                {
                    "listen_port": free_port(),
                    "adapters": {"fake": {"type": "fake", "port": free_port()}},
                    "models": {"bundled-model": {"adapter": "fake"}},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        user_dir = root / "support"
        user_dir.mkdir()
        user_config = user_dir / "engines.yaml"
        user_config.write_text(
            yaml.safe_dump(
                {
                    "listen_port": free_port(),
                    "adapters": {"fake": {"type": "fake", "port": free_port()}},
                    "models": {"user-model": {"adapter": "fake"}},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        old_settings = os.environ.get("INFERENCEDOCK_SETTINGS_PATH")
        os.environ["INFERENCEDOCK_SETTINGS_PATH"] = str(user_dir / "settings.json")
        try:
            assert model_dispatch.resolve_config_path(bundled) == user_config
            explicit = root / "elsewhere.yaml"
            assert model_dispatch.resolve_config_path(explicit) == explicit, "explicit non-bundle config must win"
            user_config.write_text("models: [", encoding="utf-8")
            assert model_dispatch.resolve_config_path(bundled) == bundled, "invalid user config falls back to the bundle"
        finally:
            if old_settings is None:
                os.environ.pop("INFERENCEDOCK_SETTINGS_PATH", None)
            else:
                os.environ["INFERENCEDOCK_SETTINGS_PATH"] = old_settings


def test_config_reload_applies_when_idle():
    with tempfile.TemporaryDirectory(prefix="model-dispatch-reload-test-") as tmp:
        root = Path(tmp)
        port = free_port()
        config_data = {
            "listen_port": free_port(),
            "adapters": {"fake": {"type": "fake", "port": port}},
            "models": {"model-a": {"adapter": "fake"}},
        }
        path = root / "engines.yaml"
        path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
        old_settings = os.environ.get("INFERENCEDOCK_SETTINGS_PATH")
        os.environ["INFERENCEDOCK_SETTINGS_PATH"] = str(root / "settings.json")
        try:
            dispatcher = model_dispatch.ModelDispatcher(model_dispatch.load_config(path), root)
        finally:
            if old_settings is None:
                os.environ.pop("INFERENCEDOCK_SETTINGS_PATH", None)
            else:
                os.environ["INFERENCEDOCK_SETTINGS_PATH"] = old_settings
        try:
            original = dispatcher.config
            path.write_text("models: [", encoding="utf-8")
            try:
                dispatcher.reload_config()
            except model_dispatch.DispatchError as exc:
                assert exc.status == 400 and exc.error_type == "invalid_config"
            else:
                raise AssertionError("invalid config unexpectedly reloaded")
            assert dispatcher.config is original, "failed reload must keep the running config"

            updated = dict(config_data)
            updated["adapters"] = {
                **config_data["adapters"],
                "fake-b": {"type": "fake", "port": free_port()},
            }
            updated["models"] = {"model-a": {"adapter": "fake"}, "model-b": {"adapter": "fake-b"}}
            path.write_text(yaml.safe_dump(updated, sort_keys=False), encoding="utf-8")
            result = dispatcher.reload_config()
            assert result["models_added"] == ["model-b"]
            assert result["adapters_added"] == ["fake-b"]
            assert "adapter:fake-b" in dispatcher._transition_locks
            assert {entry["id"] for entry in dispatcher.model_entries()} == {"model-a", "model-b"}

            changed_port = dict(updated)
            changed_port["listen_port"] = free_port()
            path.write_text(yaml.safe_dump(changed_port, sort_keys=False), encoding="utf-8")
            try:
                dispatcher.reload_config()
            except model_dispatch.DispatchError as exc:
                assert exc.error_type == "reload_requires_restart"
            else:
                raise AssertionError("listen port change accepted via reload")

            path.write_text(yaml.safe_dump(updated, sort_keys=False), encoding="utf-8")
            dispatcher._models["model-a"].state = "ready"
            shrunk = dict(updated)
            shrunk["models"] = {"model-b": {"adapter": "fake"}}
            path.write_text(yaml.safe_dump(shrunk, sort_keys=False), encoding="utf-8")
            try:
                dispatcher.reload_config()
            except model_dispatch.DispatchError as exc:
                assert exc.error_type == "reload_removes_loaded_model"
            else:
                raise AssertionError("reload removed a loaded model")
            dispatcher._models["model-a"].state = "unloaded"

            with dispatcher.condition:
                dispatcher.active_requests = 1
            try:
                dispatcher.reload_config()
            except model_dispatch.DispatchError as exc:
                assert exc.status == 409 and exc.error_type == "dispatcher_busy"
            else:
                raise AssertionError("reload accepted a busy dispatcher")
            with dispatcher.condition:
                dispatcher.active_requests = 0

            with dispatcher.condition:
                dispatcher._transitions_active = 1
            try:
                dispatcher.reload_config()
            except model_dispatch.DispatchError as exc:
                assert exc.status == 409 and exc.error_type == "dispatcher_busy"
            else:
                raise AssertionError("reload accepted an active model transition")
            with dispatcher.condition:
                dispatcher._transitions_active = 0

            result = dispatcher.reload_config()
            assert result["reloaded"] is True
            assert dispatcher.config.idle_unload_seconds is None

            dispatcher.update_settings({"idle_unload_seconds": 42})
            result = dispatcher.reload_config()
            assert dispatcher.config.idle_unload_seconds == 42, "persisted idle override must survive a reload"
        finally:
            dispatcher.shutdown()


def test_reconcile_models():
    with tempfile.TemporaryDirectory(prefix="model-dispatch-reconcile-test-") as tmp:
        root = Path(tmp)
        port = free_port()
        dead = free_port()
        fake = str(ROOT / "scripts" / "fake_backend.py")
        config_data = {
            "listen_port": free_port(),
            "load_timeout_seconds": 5,
            "connect_timeout_seconds": 0.5,
            "adapters": {
                "managed-a": {
                    "type": "managed",
                    "endpoint": f"http://127.0.0.1:{port}",
                    "models_path": "/v1/models",
                    "command": [sys.executable, fake, "--port", str(port), "--events", str(root / "a.events"), "--stop-file", str(root / "a.stop"), "--start-delay", "0"],
                    "working_directory": "~",
                    "log_path": str(root / "a.log"),
                    "stop_timeout_seconds": 0.5,
                },
                "ext-dead": {
                    "type": "external",
                    "endpoint": f"http://127.0.0.1:{dead}",
                    "models_path": "/v1/models",
                },
                "ext-skip": {
                    "type": "external",
                    "endpoint": f"http://127.0.0.1:{free_port()}",
                },
            },
            "models": {
                "model-a": {"adapter": "managed-a"},
                "model-x": {"adapter": "ext-dead"},
                "model-y": {"adapter": "ext-skip"},
                "model-z": {"adapter": "ext-skip", "backend_model": "fake-model"},
            },
        }
        path = root / "engines.yaml"
        path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
        config = model_dispatch.load_config(path)
        dispatcher = model_dispatch.ModelDispatcher(config, root)
        try:
            result = dispatcher.reconcile_models()
            assert result["mutated"] is False
            by_adapter = {item["adapter"]: item for item in result["results"]}
            assert set(by_adapter) == {"managed-a", "ext-dead"}, "adapters without models_path must be skipped"
            assert by_adapter["managed-a"]["status"] == "unreachable", "reconcile must not start a stopped managed service"
            assert by_adapter["ext-dead"]["status"] == "unreachable"
            assert model_dispatch._free(port), "read-only reconcile must not bind the managed port"
            dispatcher.activate("model-a")
            pid = dispatcher.backend.process.pid
            result = dispatcher.reconcile_models()
            by_adapter = {item["adapter"]: item for item in result["results"]}
            managed = by_adapter["managed-a"]
            assert managed["status"] == "ok"
            assert managed["discovered"] == ["fake-model"]
            assert managed["new_candidates"] == [{"id": "fake-model", "source": "service-discovered"}]
            assert managed["missing_candidates"] == [{"id": "model-a", "model": "model-a", "enabled": True, "advertise": True}]
            assert dispatcher.backend.process.pid == pid, "reconcile must not restart the resident service"
            shadows = [d for d in result["diagnostics"] if d["type"] == "candidate_shadows_other_adapter"]
            assert shadows == [{"type": "candidate_shadows_other_adapter", "adapter": "managed-a", "backend_model": "fake-model", "configured_adapter": "ext-skip"}]
            with dispatcher.condition:
                dispatcher.active_requests = 1
            try:
                dispatcher.reconcile_models()
            except model_dispatch.DispatchError as exc:
                assert exc.status == 409
            else:
                raise AssertionError("reconcile accepted a busy dispatcher")
            with dispatcher.condition:
                dispatcher.active_requests = 0
        finally:
            dispatcher.shutdown()


def test_local_catalog_refresh():
    with tempfile.TemporaryDirectory(prefix="model-dispatch-local-catalog-test-") as tmp:
        root = Path(tmp)
        catalog_script = "import json; print(json.dumps({'models': [{'repo_id': 'repo-present'}]}))"
        config_data = {
            "listen_port": free_port(),
            "connect_timeout_seconds": 1,
            "adapters": {
                "mtplx": {
                    "type": "managed",
                    "port": free_port(),
                    "command": [sys.executable, "serve", "--model", "repo-present", "--model-id", "present"],
                    "catalog_args": ["-c", catalog_script],
                },
            },
            "models": {
                "present": {"adapter": "mtplx", "backend_model": "present"},
                "missing": {"adapter": "mtplx", "backend_model": "missing"},
            },
        }
        path = root / "engines.yaml"
        path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
        dispatcher = model_dispatch.ModelDispatcher(model_dispatch.load_config(path), root)
        try:
            listed = {entry["id"] for entry in dispatcher.model_entries()}
            assert listed == {"present"}, listed
            hidden = {entry["id"]: entry for entry in dispatcher.model_entries(include_hidden=True)}
            assert hidden["present"]["available"] is True
            assert hidden["missing"]["available"] is False
            result = dispatcher.reconcile_models()
            item = next(entry for entry in result["results"] if entry["adapter"] == "mtplx")
            assert item["source"] == "local-catalog"
            assert item["discovered"] == ["repo-present"]
            assert item["missing_candidates"] == [{"id": "missing", "model": "missing", "enabled": True, "advertise": True}]
            try:
                dispatcher.activate("missing")
            except model_dispatch.DispatchError as exc:
                assert exc.error_type == "model_unavailable" and exc.status == 409
            else:
                raise AssertionError("missing local model was activated")
        finally:
            dispatcher.shutdown()


def test_generic_model_availability_evidence():
    with tempfile.TemporaryDirectory(prefix="model-dispatch-availability-test-") as tmp:
        root = Path(tmp)
        marker = root / "present"
        catalog_script = (
            "import json, pathlib; p=pathlib.Path(" + repr(str(marker)) + "); "
            "print(json.dumps({'data': [{'model': 'catalog-model'}] if p.exists() else []}))"
        )
        asset = root / "asset.bin"
        asset.write_text("ok", encoding="utf-8")
        config_data = {
            "listen_port": free_port(),
            "connect_timeout_seconds": 0.2,
            "adapters": {
                "cli": {"type": "managed", "port": free_port(), "command": [sys.executable], "catalog_args": ["-c", catalog_script]},
                "http": {"type": "external", "endpoint": "http://127.0.0.1:19998", "models_path": "/v1/models"},
            },
            "models": {
                "cli-model": {"adapter": "cli", "backend_model": "backend", "catalog_id": "catalog-model"},
                "http-model": {"adapter": "http", "backend_model": "http-id", "runtime_model_id": "runtime-id"},
                "asset-model": {"adapter": "http", "backend_model": "asset-id", "asset_paths": [asset.name]},
            },
        }
        path = root / "engines.yaml"
        path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
        dispatcher = model_dispatch.ModelDispatcher(model_dispatch.load_config(path), root)
        old_healthy, old_http_json = model_dispatch.ProcessBackend.healthy, model_dispatch.http_json
        http_state = {"healthy": True, "models": {"http-id", "asset-id"}}
        try:
            model_dispatch.ProcessBackend.healthy = lambda self: http_state["healthy"]
            model_dispatch.http_json = lambda *args, **kwargs: (200, {"models": [{"name": item} for item in http_state["models"]] + [{"name": "runtime-id", "context_length": 524288, "meta": {"model_max_tokens": 262144, "kv_quant": "8", "mtp_loaded": True}}]})
            marker.write_text("present", encoding="utf-8")
            listed = {entry["id"] for entry in dispatcher.model_entries()}
            assert listed == {"cli-model", "http-model", "asset-model"}
            http_model = next(entry for entry in dispatcher.model_entries() if entry["id"] == "http-model")
            assert http_model["reported_context_window"] == 524288
            assert http_model["reported_max_output_tokens"] == 262144
            assert http_model["reported_runtime_summary"] == ["KV 8-bit", "MTP on"]
            marker.unlink()
            dispatcher._refresh_local_catalogs(force=True)
            hidden = {entry["id"]: entry for entry in dispatcher.model_entries(include_hidden=True)}
            assert hidden["cli-model"]["available"] is False
            assert hidden["cli-model"]["catalog_model"] == "catalog-model"
            try:
                dispatcher.activate("cli-model")
            except model_dispatch.DispatchError as exc:
                assert exc.status == 409 and exc.error_type == "model_unavailable"
            else:
                raise AssertionError("missing catalog model was activated")
            marker.write_text("restored", encoding="utf-8")
            dispatcher._refresh_local_catalogs(force=True)
            assert "cli-model" in {entry["id"] for entry in dispatcher.model_entries()}
            http_state["models"] = {"asset-id"}
            dispatcher._refresh_models_paths(force=True)
            hidden = {entry["id"]: entry for entry in dispatcher.model_entries(include_hidden=True)}
            assert hidden["http-model"]["available"] is False
            http_state["healthy"] = False
            dispatcher._refresh_models_paths(force=True)
            assert {entry["id"] for entry in dispatcher.model_entries()} == {"cli-model", "http-model", "asset-model"}
            assert all(entry["available"] is None for entry in dispatcher.model_entries(include_hidden=True) if entry["adapter"] == "http")
            asset.unlink()
            http_state["healthy"] = True
            http_state["models"] = {"http-id", "asset-id"}
            dispatcher._refresh_models_paths(force=True)
            hidden = {entry["id"]: entry for entry in dispatcher.model_entries(include_hidden=True)}
            assert hidden["asset-model"]["available"] is False
            dispatcher._models["cli-model"].state = "ready"
            marker.unlink()
            dispatcher._refresh_local_catalogs(force=True)
            assert "cli-model" not in {entry["id"] for entry in dispatcher.model_entries()}
            assert any(entry["id"] == "cli-model" and entry["state"] == "ready" for entry in dispatcher.model_entries(include_hidden=True))
        finally:
            model_dispatch.ProcessBackend.healthy, model_dispatch.http_json = old_healthy, old_http_json
            dispatcher.shutdown()


def test_slow_load_does_not_freeze_status_ready_models_or_cancel():
    class SlowBackend:
        barrier = None

        def __init__(self):
            self.entered = threading.Event()

        def healthy(self):
            return False

        def ensure_started(self, timeout):
            self.entered.set()
            if self.barrier is not None:
                self.barrier.wait(timeout=1)
            time.sleep(0.4)

        def stop(self):
            pass

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config_path = root / "engines.yaml"
        config_path.write_text(
            f"""listen_port: {free_port()}
load_timeout_seconds: 2
request_poll_seconds: 0.01
adapters:
  slow: {{type: fake, port: {free_port()}}}
  fast: {{type: fake, port: {free_port()}}}
resource_groups:
  shared-group: {{capacity: 2}}
models:
  slow-model:
    adapter: slow
    backend_model: slow-model
    resource_group: shared-group
    lifecycle_owner: model-dispatch
    capabilities: {{chat: true, stream: true}}
  fast-model:
    adapter: fast
    backend_model: fast-model
    resource_group: shared-group
    lifecycle_owner: model-dispatch
    capabilities: {{chat: true, stream: true}}
""",
            encoding="utf-8",
        )
        old_pressure = model_dispatch.memory_pressure_level
        model_dispatch.memory_pressure_level = lambda: 100
        dispatcher = model_dispatch.ModelDispatcher(model_dispatch.load_config(config_path), root)
        slow = SlowBackend()
        dispatcher._backends["slow"] = slow
        dispatcher._models["fast-model"].state = "ready"
        dispatcher.settings["smart_scheduling"] = True
        errors = []
        meta = {"request_id": "loading-request", "cancel_event": threading.Event(), "metrics": {"stream": False}}

        def load():
            try:
                dispatcher.begin_request("slow-model", False, meta)
            except model_dispatch.DispatchError as exc:
                errors.append(exc)

        try:
            thread = threading.Thread(target=load)
            thread.start()
            assert slow.entered.wait(1)
            started = time.monotonic()
            assert dispatcher.status()["models"]
            assert time.monotonic() - started < 0.15
            started = time.monotonic()
            dispatcher.begin_request("fast-model", False)
            assert time.monotonic() - started < 0.15
            dispatcher.end_request()
            assert dispatcher.cancel("loading-request") == 1
            thread.join(2)
            assert errors and errors[0].error_type == "request_cancelled"
            assert not thread.is_alive()

            dispatcher._models["slow-model"].state = "unloaded"
            dispatcher._models["fast-model"].state = "unloaded"
            dispatcher._backends["fast"] = SlowBackend()
            SlowBackend.barrier = threading.Barrier(2)
            parallel_errors = []

            def cold_load(model_name):
                request_id = f"parallel-{model_name}"
                request = {"request_id": request_id, "cancel_event": threading.Event(), "metrics": {"stream": False}}
                try:
                    dispatcher.begin_request(model_name, False, request)
                    dispatcher.end_request(request_id)
                except Exception as exc:
                    parallel_errors.append(exc)

            started = time.monotonic()
            threads = [threading.Thread(target=cold_load, args=(name,)) for name in ("slow-model", "fast-model")]
            for item in threads:
                item.start()
            for item in threads:
                item.join(2)
            assert not parallel_errors and not any(item.is_alive() for item in threads)
            assert time.monotonic() - started < 0.7, "independent resource groups loaded serially"
        finally:
            dispatcher.shutdown()
            model_dispatch.memory_pressure_level = old_pressure


def test_config_validation():
    base = ROOT / "config" / "engines.example.yaml"
    original = base.read_text(encoding="utf-8")
    try:
        model_dispatch.load_config(base)
        bad_port = original.replace("listen_port: 18800", "listen_port: 8000", 1)
        assert bad_port != original
        bad_path = base.with_name("engines.bad-port.yaml")
        bad_path.write_text(bad_port, encoding="utf-8")
        try:
            model_dispatch.load_config(bad_path)
        except model_dispatch.ConfigError:
            pass
        else:
            raise AssertionError("protected port accepted")

        bad_endpoint = original.replace("type: fake", "type: external\n    endpoint: http://example.com:9000", 1)
        bad_path.write_text(bad_endpoint, encoding="utf-8")
        try:
            model_dispatch.load_config(bad_path)
        except model_dispatch.ConfigError:
            pass
        else:
            raise AssertionError("non-loopback endpoint accepted")
    finally:
        bad_path = base.with_name("engines.bad-port.yaml")
        bad_path.unlink(missing_ok=True)


def main():
    tests = [test_unknown_model, test_responses_api_uses_same_dispatch_path, test_models_listing, test_alias_routes_to_canonical_model, test_switch_api_and_content_type, test_settings_api, test_cancel_rejects_non_json_body, test_unload, test_request_metrics, test_stream_metrics_complete, test_responses_stream_metrics_complete, test_responses_background_is_rejected, test_stream_client_disconnect_finishes, test_cancel_keeps_model_loaded, test_cancel_isolated_by_request_id, test_cross_site_requests_are_rejected, test_same_model_cold_start_is_coalesced, test_switching_rejects_busy_stream_then_retries]
    with dispatcher_process() as (base, events, log, run_id):
        print(f"dispatcher={base}", flush=True)
        for test in tests:
            print(f"run {test.__name__}", flush=True)
            test(base, events, run_id)
            print(f"ok {test.__name__}", flush=True)
    print("run test_config_validation", flush=True)
    test_config_validation()
    print("ok test_config_validation", flush=True)
    print("run test_slow_load_does_not_freeze_status_ready_models_or_cancel", flush=True)
    test_slow_load_does_not_freeze_status_ready_models_or_cancel()
    print("ok test_slow_load_does_not_freeze_status_ready_models_or_cancel", flush=True)
    print("run test_cancel_during_load_rolls_back_orphan_model", flush=True)
    test_cancel_during_load_rolls_back_orphan_model()
    print("ok test_cancel_during_load_rolls_back_orphan_model", flush=True)
    print("run test_external_switch_and_unload_are_rejected", flush=True)
    test_external_switch_and_unload_are_rejected()
    print("ok test_external_switch_and_unload_are_rejected", flush=True)
    print("run test_http_managed_model_actions_and_observe_noop", flush=True)
    test_http_managed_model_actions_and_observe_noop()
    print("ok test_http_managed_model_actions_and_observe_noop", flush=True)
    print("run test_activation_rejects_busy_model_without_stale_switch", flush=True)
    test_activation_rejects_busy_model_without_stale_switch()
    print("ok test_activation_rejects_busy_model_without_stale_switch", flush=True)
    print("run test_managed_port_and_failure_cleanup", flush=True)
    test_managed_port_and_failure_cleanup()
    print("ok test_managed_port_and_failure_cleanup", flush=True)
    print("run test_packaged_backend_logs_stay_outside_bundle", flush=True)
    test_packaged_backend_logs_stay_outside_bundle()
    print("ok test_packaged_backend_logs_stay_outside_bundle", flush=True)
    print("run test_managed_native_lifecycle_reuses_healthy_unowned_service", flush=True)
    test_managed_native_lifecycle_reuses_healthy_unowned_service()
    print("ok test_managed_native_lifecycle_reuses_healthy_unowned_service", flush=True)
    print("run test_shared_native_routes_and_external_activity_fail_closed", flush=True)
    test_shared_native_routes_and_external_activity_fail_closed()
    print("ok test_shared_native_routes_and_external_activity_fail_closed", flush=True)
    print("run test_owned_managed_activity_probe_blocks_direct_unload", flush=True)
    test_owned_managed_activity_probe_blocks_direct_unload()
    print("ok test_owned_managed_activity_probe_blocks_direct_unload", flush=True)
    print("run test_dead_owned_process_clears_stale_ready_state", flush=True)
    test_dead_owned_process_clears_stale_ready_state()
    print("ok test_dead_owned_process_clears_stale_ready_state", flush=True)
    print("run test_unverified_probe_is_visible_and_fail_closed", flush=True)
    test_unverified_probe_is_visible_and_fail_closed()
    print("ok test_unverified_probe_is_visible_and_fail_closed", flush=True)
    print("run test_persistent_metric_aggregates_are_content_free", flush=True)
    test_persistent_metric_aggregates_are_content_free()
    print("ok test_persistent_metric_aggregates_are_content_free", flush=True)
    print("run test_observed_memory_evidence_is_audit_only", flush=True)
    test_observed_memory_evidence_is_audit_only()
    print("ok test_observed_memory_evidence_is_audit_only", flush=True)
    print("run test_responses_metrics_status_and_unknown_cache", flush=True)
    test_responses_metrics_status_and_unknown_cache()
    print("ok test_responses_metrics_status_and_unknown_cache", flush=True)
    print("run test_mlx_metal_timeout_recovers_once_before_response", flush=True)
    test_mlx_metal_timeout_recovers_once_before_response()
    print("ok test_mlx_metal_timeout_recovers_once_before_response", flush=True)
    print("run test_mlx_generic_error_recovers_during_model_load", flush=True)
    test_mlx_generic_error_recovers_during_model_load()
    print("ok test_mlx_generic_error_recovers_during_model_load", flush=True)
    print("run test_backend_auth_header_covers_state_and_activity_probes", flush=True)
    test_backend_auth_header_covers_state_and_activity_probes()
    print("ok test_backend_auth_header_covers_state_and_activity_probes", flush=True)
    print("run test_backend_auth_missing_env_is_typed_and_secret_free", flush=True)
    test_backend_auth_missing_env_is_typed_and_secret_free()
    print("ok test_backend_auth_missing_env_is_typed_and_secret_free", flush=True)
    print("run test_endpoint_and_port_must_match", flush=True)
    test_endpoint_and_port_must_match()
    print("ok test_endpoint_and_port_must_match", flush=True)
    print("run test_managed_schema_paths_and_shared_port_switch", flush=True)
    test_managed_schema_paths_and_shared_port_switch()
    print("ok test_managed_schema_paths_and_shared_port_switch", flush=True)
    print("run test_idle_unload_and_keep_resident", flush=True)
    test_idle_unload_and_keep_resident()
    print("ok test_idle_unload_and_keep_resident", flush=True)
    print("run test_startup_idle_unload_uses_persisted_settings", flush=True)
    test_startup_idle_unload_uses_persisted_settings()
    print("ok test_startup_idle_unload_uses_persisted_settings", flush=True)
    print("run test_activation_restarts_idle_clock", flush=True)
    test_activation_restarts_idle_clock()
    print("ok test_activation_restarts_idle_clock", flush=True)
    print("run test_external_different_groups_can_coexist", flush=True)
    test_external_different_groups_can_coexist()
    print("ok test_external_different_groups_can_coexist", flush=True)
    print("run test_fake_adapters_in_different_groups_can_coexist", flush=True)
    test_fake_adapters_in_different_groups_can_coexist()
    print("ok test_fake_adapters_in_different_groups_can_coexist", flush=True)
    print("run test_capacity_busy_model_waits_then_replaces", flush=True)
    test_capacity_busy_model_waits_then_replaces()
    print("ok test_capacity_busy_model_waits_then_replaces", flush=True)
    print("run test_capacity_two_evicts_only_one_idle_model", flush=True)
    test_capacity_two_evicts_only_one_idle_model()
    print("ok test_capacity_two_evicts_only_one_idle_model", flush=True)
    print("run test_targeted_unload_preserves_other_service_and_rejects_busy", flush=True)
    test_targeted_unload_preserves_other_service_and_rejects_busy()
    print("ok test_targeted_unload_preserves_other_service_and_rejects_busy", flush=True)
    print("run test_memory_admission_counts_loaded_reserved_external_and_pressure", flush=True)
    test_memory_admission_counts_loaded_reserved_external_and_pressure()
    print("ok test_memory_admission_counts_loaded_reserved_external_and_pressure", flush=True)
    print("run test_fake_backend_crash_recovery", flush=True)
    test_fake_backend_crash_recovery()
    print("ok test_fake_backend_crash_recovery", flush=True)
    print("run test_reconcile_models", flush=True)
    test_reconcile_models()
    print("ok test_reconcile_models", flush=True)
    print("run test_local_catalog_refresh", flush=True)
    test_local_catalog_refresh()
    print("ok test_local_catalog_refresh", flush=True)
    print("run test_generic_model_availability_evidence", flush=True)
    test_generic_model_availability_evidence()
    print("ok test_generic_model_availability_evidence", flush=True)
    print("run test_identity_validation_and_diagnostics", flush=True)
    test_identity_validation_and_diagnostics()
    print("ok test_identity_validation_and_diagnostics", flush=True)
    print("run test_config_reload_applies_when_idle", flush=True)
    test_config_reload_applies_when_idle()
    print("ok test_config_reload_applies_when_idle", flush=True)
    print("run test_user_config_preferred_over_bundled", flush=True)
    test_user_config_preferred_over_bundled()
    print("ok test_user_config_preferred_over_bundled", flush=True)
    print("run test_sse_ttft_requires_content", flush=True)
    test_sse_ttft_requires_content()
    print("ok test_sse_ttft_requires_content", flush=True)
    print("run test_sse_non_object_metrics_are_ignored", flush=True)
    test_sse_non_object_metrics_are_ignored()
    print("ok test_sse_non_object_metrics_are_ignored", flush=True)
    print("run test_stream_timeout_returns_structured_end_without_traceback", flush=True)
    test_stream_timeout_returns_structured_end_without_traceback()
    print("ok test_stream_timeout_returns_structured_end_without_traceback", flush=True)
    import test_plugin_registry
    test_plugin_registry.main()
    import test_plugin_runtime
    test_plugin_runtime.main()
    import test_migration_helper
    test_migration_helper.main()
    import test_update_monitor
    test_update_monitor.main()
    print("all dispatcher smoke tests passed")


if __name__ == "__main__":
    main()
