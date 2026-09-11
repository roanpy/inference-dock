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
    assert {m["id"] for m in payload["manifests"]} == {"ds4", "fastmlx", "llama-cpp", "lm-studio", "mlx-lm", "mlx-serve", "mlx-vlm", "mtplx", "ollama", "omlx", "vllm-mlx"}


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


def test_manifest_validates_runtime_probe_paths():
    with tempfile.TemporaryDirectory() as tmp:
        path = write_manifest(Path(tmp), "probe", "\n".join([
            "schema_version: 1",
            "id: probe",
            "display_name: Probe",
            "discover: {executables: []}",
            "service: {mode: observe, endpoint: http://127.0.0.1:19000, active_requests_path: /activity, model_state_path: /loaded}",
            "models_source: none",
        ]))
        manifest = plugin_registry.load_manifest(path)
        assert manifest.service["active_requests_path"] == "/activity"
        path.write_text(path.read_text().replace("/loaded", "/../loaded"), encoding="utf-8")
        try:
            plugin_registry.load_manifest(path)
        except plugin_registry.ManifestError as exc:
            assert "model_state_path" in str(exc)
        else:
            raise AssertionError("unsafe model state path accepted")


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
            # A 404 proves only that the port is reachable; it cannot identify
            # the declared service.
            assert item["endpoint_running"] is True
            assert item["endpoint_reachable"] is True
            assert item["identity_verified"] is False
            assert item["status"] == "reachable_unverified"
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


def test_scan_reports_candidate_level_and_chinese_label():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        plugins = tmp_path / "plugins"
        write_manifest(plugins, "fixture", "\n".join([
            "schema_version: 1",
            "id: fixture",
            "display_name: Fixture",
            "discover: {executables: [\"definitely-missing-fixture-bin\"]}",
            f"service: {{mode: observe, endpoint: http://127.0.0.1:{free_port()}, health_path: /health}}",
            "models_source: none",
        ]))
        rc, payload, _ = run_cli("scan", "--plugins-dir", str(plugins))
        assert rc == 0
        (item,) = payload["results"]
        assert item["status"] == "not_running"
        assert item["candidate_level"] == "registered"
        assert item["status_label"] == "已登记，服务未运行"
        assert item["platform"] == {"id": "fixture", "display_name": "Fixture"}
        assert item["installation"]["source"] == "manifest"
        assert item["instance"]["id"].startswith("fixture:http://127.0.0.1:")
        # Discovery never claims configured/tested levels.
        assert item["candidate_level"] in plugin_registry.CANDIDATE_LEVELS[:3]


def test_import_instance_explicit_paths():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        plugins = tmp_path / "plugins"
        manifest_path = write_manifest(plugins, "fixture", "\n".join([
            "schema_version: 1",
            "id: fixture",
            "display_name: Fixture",
            "discover: {executables: [\"fixture-bin\"]}",
            f"service: {{mode: external, endpoint: http://127.0.0.1:{free_port()}, health_path: /health}}",
            "models_source: none",
        ]))
        manifest = plugin_registry.load_manifest(manifest_path)
        fake = tmp_path / "fixture-bin"
        fake.write_text("#!/bin/sh\necho fixture\n", encoding="utf-8")
        fake.chmod(0o755)

        # A valid explicit import never scans PATH and reports a candidate only.
        item = plugin_registry.import_instance(manifest, executable=fake)
        assert item["path"] == str(fake.resolve())
        assert item["candidate_level"] == "registered"
        assert item["status_label"].startswith("已登记")
        assert item["platform"]["id"] == "fixture"
        assert item["installation"]["source"] == "explicit"
        assert item["instance"]["mode"] == "external"

        # Missing executable notes the partial registration instead of scanning.
        item = plugin_registry.import_instance(manifest, endpoint=f"http://127.0.0.1:{free_port()}")
        assert item["status"] == "not_running"
        assert any("未提供可执行文件路径" in note for note in item["notes_zh"])
        assert any("端点" in note for note in item["notes_zh"])

        for bad_call in (
            lambda: plugin_registry.import_instance(manifest, executable=Path("relative/bin")),
            lambda: plugin_registry.import_instance(manifest, executable=tmp_path / "missing-bin"),
            lambda: plugin_registry.import_instance(manifest, endpoint="http://192.0.2.1:9000"),
            lambda: plugin_registry.import_instance(manifest),
        ):
            try:
                bad_call()
            except plugin_registry.ManifestError:
                pass
            else:
                raise AssertionError("unsafe import accepted")

        # Config import is filtered and redacted like scan previews.
        config = tmp_path / "fixture.json"
        config.write_text(json.dumps({"model": "fixture-model", "api_key": "do-not-leak"}), encoding="utf-8")
        item = plugin_registry.import_instance(manifest, executable=fake, config_path=config)
        assert item["config_preview"]["fields"] == {"model": "fixture-model"}
        assert "do-not-leak" not in json.dumps(item)
        assert str(fake.resolve()) not in json.dumps(item["installation"])
        assert str(fake.resolve()) not in item["instance"]["id"]

        # A source/build directory is an explicit candidate only; no recursive
        # scan or command execution is performed.
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        item = plugin_registry.import_instance(manifest, source_dir=source_dir)
        assert item["source_dir"] == str(source_dir.resolve())
        assert item["status"] == "installed"
        assert item["installation"]["source"] == "explicit"

        # The CLI exposes the same explicit import.
        rc, payload, _ = run_cli("import-instance", "--plugins-dir", str(plugins), "--id", "fixture", "--executable", str(fake))
        assert rc == 0
        assert payload["instance"]["path"] == str(fake.resolve())
        rc, payload, _ = run_cli("import-instance", "--plugins-dir", str(plugins), "--id", "fixture", "--source-dir", str(source_dir))
        assert rc == 0
        assert payload["instance"]["source_dir"] == str(source_dir.resolve())
        rc, payload, _ = run_cli("import-instance", "--plugins-dir", str(plugins), "--id", "fixture", "--endpoint", "http://example.com:9000")
        assert rc == 1
        assert payload["instance"] is None


def test_builtin_probe_and_residency_claims_stay_conservative():
    manifests, errors = plugin_registry.load_manifests()
    assert not errors
    by_id = {m.id: m for m in manifests}

    # Ollama: /api/ps is a verified residency probe; /v1/models-style catalogs
    # must never be registered as state probes, and no activation is claimed.
    ollama = by_id["ollama"]
    assert ollama.service.get("model_state_path") == "/api/ps"
    assert ollama.service.get("active_requests_path") is None
    assert ollama.service.get("activation") is None
    assert ollama.service.get("mode") == "observe"

    # oMLX: resident RAG service, observe-only, no unverified probe claims.
    omlx = by_id["omlx"]
    assert omlx.keep_resident is True
    assert omlx.service.get("mode") == "observe"
    assert omlx.service.get("active_requests_path") is None
    assert omlx.service.get("model_state_path") is None
    assert omlx.service.get("activation") is None and omlx.service.get("deactivation") is None

    # MTPLX: no HTTP state/activity probe was verifiable; the gap is pinned so
    # adding one later is a deliberate, reviewed change.
    mtplx = by_id["mtplx"]
    assert mtplx.service.get("active_requests_path") is None
    assert mtplx.service.get("model_state_path") is None
    assert mtplx.service.get("activation") is None and mtplx.service.get("deactivation") is None

    # LM Studio: observe-only until fixed-version probes verify load routes.
    lm_studio = by_id["lm-studio"]
    assert lm_studio.service.get("mode") == "observe"
    assert lm_studio.service.get("activation") is None and lm_studio.service.get("deactivation") is None

    # mlx-lm / mlx-vlm / vllm-mlx: CLI discovery plus verified upstream
    # repositories only; no endpoint, probe, or lifecycle route is claimed.
    expected_repos = {
        "mlx-lm": "https://github.com/ml-explore/mlx-lm",
        "mlx-vlm": "https://github.com/Blaizzy/mlx-vlm",
        "vllm-mlx": "https://github.com/waybarrios/vllm-mlx",
    }
    for plugin_id, remote in expected_repos.items():
        manifest = by_id[plugin_id]
        assert manifest.service.get("mode") == "managed"
        assert manifest.service.get("endpoint") is None
        assert manifest.service.get("activation") is None and manifest.service.get("deactivation") is None
        assert manifest.service.get("active_requests_path") is None and manifest.service.get("model_state_path") is None
        assert manifest.models_source == "none"
        assert manifest.discover.get("executables"), plugin_id
        assert [repo.get("remote") for repo in manifest.repositories] == [remote]

    # FastMLX exposes an OpenAI catalog, but lifecycle remains observe-only
    # until its query-parameter load/unload routes are tested at a fixed version.
    fastmlx = by_id["fastmlx"]
    assert fastmlx.service.get("mode") == "observe"
    assert fastmlx.service.get("models_path") == "/v1/models"
    assert fastmlx.service.get("activation") is None and fastmlx.service.get("deactivation") is None
    assert fastmlx.models_source == "service"


TESTS = [
    test_builtin_manifests_check,
    test_manifest_rejects_unsafe_endpoint,
    test_manifest_validates_runtime_probe_paths,
    test_duplicate_manifest_ids_fail,
    test_scan_explicit_path_and_not_found,
    test_probe_version_only_for_trusted,
    test_endpoint_status,
    test_import_preview_filters_and_redacts,
    test_scan_reports_candidate_level_and_chinese_label,
    test_import_instance_explicit_paths,
    test_builtin_probe_and_residency_claims_stay_conservative,
]


def main():
    for test in TESTS:
        print(f"run {test.__name__}", flush=True)
        test()
        print(f"ok {test.__name__}", flush=True)


if __name__ == "__main__":
    main()
