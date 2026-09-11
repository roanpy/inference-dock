#!/usr/bin/env python3
"""Isolated plugin registry tests: validation, safe scan, redaction."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import plugin_registry

REGISTRY = ROOT / "scripts" / "plugin_registry.py"


def run_cli(*args: str):
    proc = subprocess.run(
        [sys.executable, str(REGISTRY), *args],
        capture_output=True, text=True, timeout=30, cwd=ROOT,
    )
    return proc.returncode, json.loads(proc.stdout), proc.stderr


def write_manifest(directory: Path, plugin_id: str, body: str) -> Path:
    folder = directory / plugin_id
    folder.mkdir(parents=True)
    path = folder / "manifest.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class NotFoundHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


def test_builtin_manifests_check():
    rc, payload, _ = run_cli("check")
    assert rc == 0, payload
    assert payload["ok"] is True
    assert {m["id"] for m in payload["manifests"]} == {"ds4", "mlx-serve", "mtplx", "ollama", "omlx"}


def test_manifest_rejects_unsafe_endpoint():
    with tempfile.TemporaryDirectory() as tmp:
        write_manifest(Path(tmp), "bad", "\n".join([
            "schema_version: 1",
            "id: bad",
            "display_name: Bad",
            "discover: {executables: []}",
            "service: {mode: observe, endpoint: http://example.com:9000}",
            "models_source: none",
        ]))
        rc, payload, _ = run_cli("check", "--plugins-dir", tmp)
        assert rc == 1
        assert any("loopback" in e["error"] for e in payload["errors"])


def test_duplicate_manifest_ids_fail():
    with tempfile.TemporaryDirectory() as tmp:
        body = "\n".join([
            "schema_version: 1",
            "id: duplicate",
            "display_name: Duplicate",
            "discover: {executables: [\"missing\"]}",
            "service: {mode: observe}",
            "models_source: none",
        ])
        write_manifest(Path(tmp), "one", body)
        write_manifest(Path(tmp), "two", body)
        rc, payload, _ = run_cli("check", "--plugins-dir", tmp)
        assert rc == 1
        assert payload["ok"] is False


def test_scan_explicit_path_and_not_found():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        plugins = tmp_path / "plugins"
        write_manifest(plugins, "fixture", "\n".join([
            "schema_version: 1",
            "id: fixture",
            "display_name: Fixture",
            "discover: {executables: [\"definitely-missing-fixture-bin\"]}",
            "service: {mode: managed}",
            "models_source: none",
        ]))
        fake = tmp_path / "fixture-bin"
        fake.write_text("#!/bin/sh\necho fixture 1.0\n", encoding="utf-8")
        fake.chmod(0o755)

        rc, payload, _ = run_cli("scan", "--plugins-dir", str(plugins))
        assert rc == 0
        (item,) = payload["results"]
        assert item["status"] == "not_found"
        assert item["path"] is None and item["version"] is None

        rc, payload, _ = run_cli("scan", "--plugins-dir", str(plugins), "--path", f"fixture={fake}", "--probe-version")
        assert rc == 0
        (item,) = payload["results"]
        assert item["status"] == "installed"
        assert item["path"] == str(fake.resolve())
        # Untrusted (non-builtin) manifests must not execute the binary.
        assert item["version"] is None


def test_probe_version_only_for_trusted():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        manifest_path = write_manifest(tmp_path, "fixture", "\n".join([
            "schema_version: 1",
            "id: fixture",
            "display_name: Fixture",
            "discover: {executables: [\"fixture-bin\"], version_args: [\"--version\"]}",
            "service: {mode: managed}",
            "models_source: none",
        ]))
        fake = tmp_path / "fixture-bin"
        fake.write_text("#!/bin/sh\necho fixture 1.0\n", encoding="utf-8")
        fake.chmod(0o755)
        untrusted = plugin_registry.load_manifest(manifest_path, trusted=False)
        trusted = plugin_registry.load_manifest(manifest_path, trusted=True)
        assert plugin_registry._probe_version(untrusted, fake) is None
        assert plugin_registry._probe_version(trusted, fake) == "fixture 1.0"


def test_endpoint_status():
    port = free_port()
    with tempfile.TemporaryDirectory() as tmp:
        plugins = Path(tmp) / "plugins"
        write_manifest(plugins, "svc", "\n".join([
            "schema_version: 1",
            "id: svc",
            "display_name: Service",
            "discover: {executables: []}",
            f"service: {{mode: observe, endpoint: http://127.0.0.1:{port}, health_path: /health}}",
            "models_source: none",
        ]))
        rc, payload, _ = run_cli("scan", "--plugins-dir", str(plugins))
        assert rc == 0
        (item,) = payload["results"]
        assert item["endpoint_running"] is False
        assert item["status"] == "not_running"

        server = ThreadingHTTPServer(("127.0.0.1", port), NotFoundHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            rc, payload, _ = run_cli("scan", "--plugins-dir", str(plugins))
            (item,) = payload["results"]
            # Any HTTP response, including 404, proves the service is alive.
            assert item["endpoint_running"] is True
            assert item["status"] == "running"
        finally:
            server.shutdown()
            server.server_close()


def test_import_preview_filters_and_redacts():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        config = tmp_path / "settings.json"
        config.write_text(json.dumps({
            "model": "fixture-model",
            "models": {"fixture": {"model": "safe", "note": "drop-nested"}},
            "port": 9000,
            "api_key": "top-secret-value",
            "unrelated": "drop-me",
            "nested": {"token": "nested-secret", "note": "Bearer abc123"},
        }), encoding="utf-8")
        plugins = tmp_path / "plugins"
        write_manifest(plugins, "cfg", "\n".join([
            "schema_version: 1",
            "id: cfg",
            "display_name: Config",
            f"discover: {{executables: [], config_files: [\"{config}\"]}}",
            f"service: {{mode: observe, endpoint: http://127.0.0.1:{free_port()}}}",
            "models_source: none",
        ]))
        rc, payload, _ = run_cli("scan", "--plugins-dir", str(plugins))
        assert rc == 0
        blob = json.dumps(payload)
        assert "top-secret-value" not in blob
        assert "nested-secret" not in blob
        assert "drop-nested" not in blob
        assert "abc123" not in blob
        assert "drop-me" not in blob
        (item,) = payload["results"]
        (preview,) = item["import_preview"]
        assert preview["fields"]["model"] == "fixture-model"
        assert preview["fields"]["port"] == 9000


TESTS = [
    test_builtin_manifests_check,
    test_manifest_rejects_unsafe_endpoint,
    test_duplicate_manifest_ids_fail,
    test_scan_explicit_path_and_not_found,
    test_probe_version_only_for_trusted,
    test_endpoint_status,
    test_import_preview_filters_and_redacts,
]


def main():
    for test in TESTS:
        print(f"run {test.__name__}", flush=True)
        test()
        print(f"ok {test.__name__}", flush=True)


if __name__ == "__main__":
    main()
