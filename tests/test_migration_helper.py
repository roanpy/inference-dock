#!/usr/bin/env python3
"""Fixture tests for read-only and explicitly applied dispatcher migration."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "migration_helper.py"
sys.path.insert(0, str(ROOT / "scripts"))
import migration_helper


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_cli(*args: str):
    return subprocess.run([sys.executable, str(HELPER), *args], capture_output=True, text=True, timeout=30, cwd=ROOT)


def write_target_config(path: Path, listen_port: int) -> None:
    content = f"""listen_host: 127.0.0.1
listen_port: {listen_port}
adapters:
  ds4-router:
    type: external
    endpoint: http://127.0.0.1:8888
  mlx-serve:
    type: external
    endpoint: http://127.0.0.1:11234
  mtplx:
    type: external
    endpoint: http://127.0.0.1:18992
resource_groups:
  large-local:
    capacity: 1
models:
  ds4-high:
    adapter: ds4-router
    backend_model: ds4high
    resource_group: large-local
    lifecycle_owner: ds4-router
  mlx-flash:
    adapter: mlx-serve
    backend_model: old-mlx-flash
    resource_group: large-local
    lifecycle_owner: mlx-serve
"""
    path.write_text(content, encoding="utf-8")


def test_plan_and_apply_with_backup():
    with tempfile.TemporaryDirectory(prefix="migration-helper-test-") as tmp:
        root = Path(tmp)
        config = root / "target.yaml"
        write_target_config(config, free_port())
        target = root / "agent.env"
        secret = "fixture-secret-value"
        original_content = (
            'export OPENAI_BASE_URL="http://127.0.0.1:8888/v1"\n'
            "export OPENAI_MODEL=ds4high\n"
            "curl http://localhost:11234/v1/models\n"
            "mtplx serve --port 18992\n"
            f"export API_KEY={secret}\n"
        )
        target.write_text(original_content, encoding="utf-8")

        proc = run_cli("--config", str(config), str(target))
        assert proc.returncode == 0, proc.stderr
        plan = json.loads(proc.stdout)
        assert plan["mode"] == "plan"
        assert plan["summary"]["changed"] == 1
        assert secret not in proc.stdout
        (file_plan,) = plan["files"]
        action_types = [action["type"] for action in file_plan["actions"]]
        assert action_types.count("rewrite_endpoint") == 2
        assert action_types.count("rewrite_model") == 1
        assert action_types.count("manual_review") == 1

        proc = run_cli("--config", str(config), "--apply", str(target))
        assert proc.returncode == 0, proc.stderr
        applied = json.loads(proc.stdout)
        (file_result,) = applied["files"]
        backup = Path(file_result["backup"])
        assert backup.is_file()
        assert secret in backup.read_text(encoding="utf-8")
        migrated = target.read_text(encoding="utf-8")
        dispatcher = plan["dispatcher"]
        assert f'export OPENAI_BASE_URL="{dispatcher}/v1"' in migrated
        assert "export OPENAI_MODEL=ds4-high" in migrated
        assert f"curl {dispatcher}/v1/models" in migrated
        assert "mtplx serve --port 18992" in migrated

        proc = run_cli("--config", str(config), str(target))
        assert proc.returncode == 0
        rerun = json.loads(proc.stdout)
        assert rerun["summary"]["changed"] == 0
        assert rerun["summary"]["manual_review"] == 1

        shutil.copy2(backup, target)
        assert target.read_text(encoding="utf-8") == original_content


def test_apply_requires_explicit_files_and_valid_config():
    with tempfile.TemporaryDirectory(prefix="migration-helper-test-") as tmp:
        root = Path(tmp)
        config = root / "target.yaml"
        write_target_config(config, free_port())

        proc = run_cli("--config", str(config), "--apply")
        assert proc.returncode == 2
        assert "explicit file" in proc.stderr

        missing = root / "missing.yaml"
        target = root / "agent.env"
        target.write_text("export OPENAI_BASE_URL=http://127.0.0.1:8888/v1\n", encoding="utf-8")
        proc = run_cli("--config", str(missing), "--apply", str(target))
        assert proc.returncode == 2
        assert "config not found" in proc.stderr
        assert target.read_text(encoding="utf-8").endswith(":8888/v1\n")

        plan = migration_helper.build_plan([target], config)
        target.write_text("changed concurrently\n", encoding="utf-8")
        try:
            migration_helper.apply_file(target, plan["files"][0])
        except migration_helper.MigrationError as exc:
            assert "changed after migration plan" in str(exc)
        else:
            raise AssertionError("stale migration plan was applied")


def test_ambiguous_model_alias_does_not_block_endpoint_migration():
    with tempfile.TemporaryDirectory(prefix="migration-helper-test-") as tmp:
        root = Path(tmp)
        config = root / "target.yaml"
        port = free_port()
        config.write_text(f"""listen_host: 127.0.0.1
listen_port: {port}
adapters:
  one: {{type: external, endpoint: 'http://127.0.0.1:8888'}}
  two: {{type: external, endpoint: 'http://127.0.0.1:11234'}}
models:
  first: {{adapter: one, backend_model: shared}}
  second: {{adapter: two, backend_model: shared}}
""", encoding="utf-8")
        target = root / "agent.env"
        target.write_text("OPENAI_BASE_URL=http://127.0.0.1:8888/v1\nOPENAI_MODEL=shared\n", encoding="utf-8")
        plan = migration_helper.build_plan([target], config)
        assert plan["summary"]["changed"] == 1
        actions = plan["files"][0]["actions"]
        assert any(action["type"] == "rewrite_endpoint" for action in actions)
        assert any(action["type"] == "manual_review" for action in actions)


def test_legacy_port_allowlist_limits_rewrites():
    with tempfile.TemporaryDirectory(prefix="migration-helper-test-") as tmp:
        root = Path(tmp)
        config = root / "target.yaml"
        write_target_config(config, free_port())
        target = root / "agent.env"
        target.write_text("A=http://127.0.0.1:8888/v1\nB=http://127.0.0.1:11234/v1\n", encoding="utf-8")
        plan = migration_helper.build_plan([target], config, {8888})
        content = plan["files"][0]["content"]
        assert ":8888" not in content
        assert ":11234" in content


TESTS = [test_plan_and_apply_with_backup, test_apply_requires_explicit_files_and_valid_config, test_ambiguous_model_alias_does_not_block_endpoint_migration, test_legacy_port_allowlist_limits_rewrites]


def main():
    for test in TESTS:
        print(f"run {test.__name__}", flush=True)
        test()
        print(f"ok {test.__name__}", flush=True)


if __name__ == "__main__":
    main()
