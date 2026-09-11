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


def test_switching_waits_for_stream(base: str, events: Path, run_id: str):
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
    status, body = post_json(base + "/v1/chat/completions", {"model": "fake-beta", "messages": []}, timeout=30)
    switch_elapsed = time.monotonic() - switch_started
    thread.join(timeout=10)

    assert first["error"] is None
    assert first["done"]
    assert first.get("chunks", 0) == 1
    assert status == 200
    assert body["model"] == "fake-beta"
    assert switch_elapsed >= 0.15, "switch must wait for request cleanup"

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
    post_raw(base + "/v1/settings", b'{"smart_scheduling": true, "memory_limit_gb": null, "adapter_policies": {}}', "application/json")


def test_cancel_rejects_non_json_body(base: str, events: Path, run_id: str):
    code, body = post_raw(base + "/v1/cancel", b"request_id=x", "text/plain")
    assert code == 415 and json.loads(body)["error"]["type"] == "invalid_request_error"


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
            if self.path == "/health":
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
                {"http": {"type": "http-managed", "endpoint": endpoint}},
                {
                    "one": {"adapter": "http", "activate_path": "/load", "deactivate_path": "/unload", "activate_payload": {"slot": 1}, "deactivate_payload": {"drop": True}},
                    "two": {"adapter": "http", "activate_path": "/load-two", "deactivate_path": "/unload-two"},
                },
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
                assert not backend.stopped, "native model unload must not stop its reusable server"
                assert managed.current_model is None and managed.state == "unloaded"
            finally:
                managed.shutdown()
    finally:
        server.shutdown()
        server.server_close()


def test_activation_rechecks_model_after_wait():
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
                "adapter-a": {
                    "type": "managed",
                    "endpoint": f"http://127.0.0.1:{free_port()}",
                    "command": [sys.executable, "-c", "pass"],
                },
                "adapter-b": {
                    "type": "managed",
                    "endpoint": f"http://127.0.0.1:{free_port()}",
                    "command": [sys.executable, "-c", "pass"],
                },
            },
            {
                "model-one": {"adapter": "adapter-b", "lifecycle_owner": "model-dispatch"},
                "model-two": {"adapter": "adapter-b", "lifecycle_owner": "model-dispatch"},
                "model-three": {"adapter": "adapter-a", "lifecycle_owner": "model-dispatch"},
            },
        )
        backend = DummyBackend(dispatcher.config.adapters["adapter-b"])
        dispatcher.backend = backend
        dispatcher.current_model = "model-one"
        dispatcher.state = "ready"
        dispatcher.active_requests = 1
        errors = []

        def activate():
            try:
                dispatcher.activate("model-two")
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=activate)
        thread.start()
        time.sleep(0.05)
        with dispatcher.condition:
            dispatcher.current_model = "model-three"
            dispatcher.active_requests = 0
            dispatcher.condition.notify_all()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert not errors, errors
        assert dispatcher.current_model == "model-two"
        assert backend.stopped, "activation must use the model current after waiting"


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
            dispatcher.config = replace(dispatcher.config, load_timeout_seconds=0.1)
            dispatcher._requests["busy-one"] = {
                "request_id": "busy-one",
                "model": "model-one",
                "cancel_event": threading.Event(),
                "metrics": {},
                "started": time.monotonic(),
                "started_wall": time.time(),
                "upstream": None,
            }
            try:
                dispatcher.activate("model-two")
            except model_dispatch.DispatchError as exc:
                assert exc.status == 409 and exc.error_type == "model_busy"
            else:
                raise AssertionError("busy capacity conflict unexpectedly replaced the model")
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
            dispatcher._external_memory_gb = lambda exclude_adapter=None: 4
            try:
                dispatcher._check_memory(dispatcher.config.models["candidate"])
            except model_dispatch.DispatchError as exc:
                assert exc.error_type == "memory_limit_exceeded"
                assert "external 4GB" in exc.message
            else:
                raise AssertionError("loaded external memory was not admitted")

            dispatcher._models["resident"].state = "unloaded"
            dispatcher._external_memory_gb = lambda exclude_adapter=None: 0
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
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            NativeHandler.events.append(self.path)
            self.send_response(200)
            self.end_headers()

    with tempfile.TemporaryDirectory(prefix="model-dispatch-managed-adopt-test-") as tmp:
        root = Path(tmp)
        server = ThreadingHTTPServer(("127.0.0.1", 0), NativeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_port}"
        dispatcher = _dispatcher_for_models(
            root,
            {"managed": {"type": "managed", "endpoint": endpoint, "command": [sys.executable, "-c", "pass"]}},
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
            updated["models"] = {"model-a": {"adapter": "fake"}, "model-b": {"adapter": "fake"}}
            path.write_text(yaml.safe_dump(updated, sort_keys=False), encoding="utf-8")
            result = dispatcher.reload_config()
            assert result["models_added"] == ["model-b"]
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
    tests = [test_unknown_model, test_models_listing, test_alias_routes_to_canonical_model, test_switch_api_and_content_type, test_settings_api, test_cancel_rejects_non_json_body, test_unload, test_request_metrics, test_stream_metrics_complete, test_stream_client_disconnect_finishes, test_cancel_keeps_model_loaded, test_cancel_isolated_by_request_id, test_cross_site_requests_are_rejected, test_same_model_cold_start_is_coalesced, test_switching_waits_for_stream]
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
    print("run test_activation_rechecks_model_after_wait", flush=True)
    test_activation_rechecks_model_after_wait()
    print("ok test_activation_rechecks_model_after_wait", flush=True)
    print("run test_managed_port_and_failure_cleanup", flush=True)
    test_managed_port_and_failure_cleanup()
    print("ok test_managed_port_and_failure_cleanup", flush=True)
    print("run test_managed_native_lifecycle_reuses_healthy_unowned_service", flush=True)
    test_managed_native_lifecycle_reuses_healthy_unowned_service()
    print("ok test_managed_native_lifecycle_reuses_healthy_unowned_service", flush=True)
    print("run test_endpoint_and_port_must_match", flush=True)
    test_endpoint_and_port_must_match()
    print("ok test_endpoint_and_port_must_match", flush=True)
    print("run test_managed_schema_paths_and_shared_port_switch", flush=True)
    test_managed_schema_paths_and_shared_port_switch()
    print("ok test_managed_schema_paths_and_shared_port_switch", flush=True)
    print("run test_idle_unload_and_keep_resident", flush=True)
    test_idle_unload_and_keep_resident()
    print("ok test_idle_unload_and_keep_resident", flush=True)
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
