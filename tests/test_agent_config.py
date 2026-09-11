#!/usr/bin/env python3
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import agent_config


def _dsh_config():
    return {
        "llm-pi-ai": {
            "providers": {
                "ds4-local": {
                    "api": "openai-completions",
                    "baseURL": "http://127.0.0.1:18800/v1",
                    "headers": {"Authorization": "Bearer local"},
                    "models": [
                        {"id": "ds4new", "contextWindow": 524288, "maxTokens": 32768, "input": ["text", "image"], "reasoningEfforts": {"off": None, "high": "high", "max": "max"}},
                        {"id": "qwen38", "contextWindow": 262144, "maxTokens": 32768, "input": ["text"]},
                    ],
                }
            }
        }
    }


def test_dsh_valid_config_normalizes_capabilities():
    config = _dsh_config()
    report = agent_config.validate_config("dsh", config)
    assert report["valid"] is True and report["providers"] == 1
    models = report["normalized"][0]["models"]
    assert models[0]["effective_capabilities"] == {"text": True, "vision": True, "stream": True, "chat": True}
    assert models[1]["effective_capabilities"]["vision"] is False
    # Missing reasoning metadata defaults to off, reported not guessed silently.
    assert models[1]["reasoning_levels"] == ["off"]
    assert models[1]["default_reasoning"] == "off"
    assert report["default_reasoning_filled"] == [
        {"provider": "ds4-local", "model": "ds4new", "default_reasoning": "off"},
        {"provider": "ds4-local", "model": "qwen38", "default_reasoning": "off"},
    ]
    # The caller's config is never mutated by validation.
    assert "effective_capabilities" not in config["llm-pi-ai"]["providers"]["ds4-local"]["models"][0]


def test_dsh_requires_nonempty_authorization():
    for headers in ({}, {"Authorization": ""}, {"Authorization": "   "}):
        config = _dsh_config()
        config["llm-pi-ai"]["providers"]["ds4-local"]["headers"] = headers
        try:
            agent_config.validate_config("dsh", config)
        except agent_config.AgentConfigError as exc:
            assert "Authorization" in str(exc)
        else:
            raise AssertionError(f"empty local credential accepted: {headers!r}")


def test_dsh_online_providers_are_not_touched():
    config = _dsh_config()
    providers = config["llm-pi-ai"]["providers"]
    providers["openai-codex"] = {"api": None, "models": [{"id": "gpt-codex"}]}
    providers["opencode-go"] = {"baseURL": "https://opencode.ai/zen/go/v1", "models": [{"id": "go-1"}]}
    providers["online-template"] = {"baseURL": "https://example.invalid/v1?token=${NOT_IN_GUI_ENV}", "models": [{"id": "remote-1"}]}
    report = agent_config.validate_config("dsh", config)
    assert report["valid"] is True and report["providers"] == 4


def test_reasoning_and_capability_validation():
    config = _dsh_config()
    model = config["llm-pi-ai"]["providers"]["ds4-local"]["models"][0]
    model["reasoningEfforts"] = {"off": None, "sometimes": "sometimes"}
    try:
        agent_config.validate_config("dsh", config)
    except agent_config.AgentConfigError as exc:
        assert "reasoningEfforts" in str(exc)
    else:
        raise AssertionError("unsupported reasoning level accepted")

    config = _dsh_config()
    model = config["llm-pi-ai"]["providers"]["ds4-local"]["models"][0]
    model["default_reasoning"] = "ultra"
    try:
        agent_config.validate_config("dsh", config)
    except agent_config.AgentConfigError as exc:
        assert "default_reasoning" in str(exc)
    else:
        raise AssertionError("default reasoning outside declared levels accepted")

    config = _dsh_config()
    model = config["llm-pi-ai"]["providers"]["ds4-local"]["models"][0]
    model["reasoningEfforts"] = {"off": None}
    try:
        agent_config.validate_config("dsh", config)
    except agent_config.AgentConfigError as exc:
        assert "reasoningEfforts" in str(exc)
    else:
        raise AssertionError("off-only native reasoning map accepted")

    config = _dsh_config()
    model = config["llm-pi-ai"]["providers"]["ds4-local"]["models"][0]
    model["input"] = ["text", "audio"]
    try:
        agent_config.validate_config("dsh", config)
    except agent_config.AgentConfigError as exc:
        assert "input" in str(exc)
    else:
        raise AssertionError("unsupported input modality accepted")

    config = _dsh_config()
    model = config["llm-pi-ai"]["providers"]["ds4-local"]["models"][0]
    model.pop("reasoningEfforts")
    model["default_reasoning"] = "high"
    try:
        agent_config.validate_config("dsh", config)
    except agent_config.AgentConfigError as exc:
        assert "explicitly declared" in str(exc)
    else:
        raise AssertionError("default reasoning invented an accepted range")


def test_environment_references_must_exist_without_exposing_values():
    config = _dsh_config()
    provider = config["llm-pi-ai"]["providers"]["ds4-local"]
    provider["baseURL"] = "http://127.0.0.1:18800/v1?token=${INFERENCE_DOCK_MISSING}"
    try:
        agent_config.validate_config("dsh", config)
    except agent_config.AgentConfigError as exc:
        message = str(exc)
        assert "INFERENCE_DOCK_MISSING" in message
        assert "secret-value" not in message
    else:
        raise AssertionError("unset environment reference accepted")

    provider.pop("baseURL")
    provider["env"] = ["INFERENCE_DOCK_MISSING"]
    try:
        agent_config.validate_config("dsh", config)
    except agent_config.AgentConfigError as exc:
        assert "INFERENCE_DOCK_MISSING" in str(exc)
    else:
        raise AssertionError("unset provider env accepted")


def test_preview_is_read_only():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.yaml"
        original = _dsh_config()
        path.write_text(json.dumps(original), encoding="utf-8")
        before = path.read_bytes()
        preview = agent_config.preview_config("dsh", path)
        assert preview["valid"] is True and preview["apply_required"] is True
        assert path.read_bytes() == before, "preview must not modify the provider file"
        assert not list(Path(tmp).glob("*.bak*")), "preview must not create backups"


def test_apply_backs_up_and_replaces_atomically():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.yaml"
        config = _dsh_config()
        path.write_text("old: true\n", encoding="utf-8")
        original_check = agent_config.dsh_running
        try:
            agent_config.dsh_running = lambda: False
            result = agent_config.apply_config("dsh", path, config, confirm=True)
            assert result["applied"] is True and result["restart_required"] is False
            backup = Path(result["backup"])
            assert backup.read_text(encoding="utf-8") == "old: true\n"
            reloaded = agent_config.load_config(path)
            assert reloaded["llm-pi-ai"]["providers"]["ds4-local"]["headers"]["Authorization"] == "Bearer local"
            # No temp files left behind.
            assert [p.name for p in Path(tmp).iterdir()] == ["settings.yaml", backup.name]

            # A running DSH is reported as needing a new session; never killed.
            agent_config.dsh_running = lambda: True
            result = agent_config.apply_config("dsh", path, config, confirm=True)
            assert result["restart_required"] is True and "不会自动终止" in result["note"]
        finally:
            agent_config.dsh_running = original_check


def test_apply_rejects_invalid_before_writing():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.yaml"
        path.write_text("old: true\n", encoding="utf-8")
        config = _dsh_config()
        config["llm-pi-ai"]["providers"]["ds4-local"]["headers"] = {}
        try:
            agent_config.apply_config("dsh", path, config, confirm=True)
        except agent_config.AgentConfigError:
            pass
        else:
            raise AssertionError("invalid config applied")
        assert path.read_text(encoding="utf-8") == "old: true\n"
        assert not list(Path(tmp).glob("*.bak*")), "failed apply must not create a backup"


def test_all_six_agents_supported_and_generic_layout():
    for agent in agent_config.AGENTS:
        config = {"providers": {"local": {"baseURL": "http://127.0.0.1:18800/v1", "models": [{"id": "m", "reasoning_levels": ["off", "high"]}]}}}
        if agent == "dsh":
            config["providers"]["local"]["headers"] = {"Authorization": "Bearer local"}
        report = agent_config.validate_config(agent, config)
        assert report["valid"] is True
    try:
        agent_config.validate_config("unknown-agent", {})
    except agent_config.AgentConfigError as exc:
        assert "unsupported agent" in str(exc)
    else:
        raise AssertionError("unknown agent accepted")


def test_common_dict_models_and_baseurl_aliases():
    config = {
        "providers": {
            "local": {
                "baseUrl": "http://127.0.0.1:18800/v1",
                "models": {
                    "qwen38": {
                        "limit": {"context": 262144, "output": 32768},
                        "modalities": {"input": ["text", "image"]},
                        "reasoning": True,
                        "reasoning_levels": ["off", "high"],
                        "reasoningEffort": "high",
                    }
                },
            }
        }
    }
    report = agent_config.validate_config("opencodex", config)
    model = report["normalized"][0]["models"][0]
    assert model["id"] == "qwen38"
    assert model["reasoning_levels"] == ["off", "high"]
    assert model["default_reasoning"] == "high"
    assert model["effective_capabilities"]["vision"] is True


def test_default_config_candidates_match_documented_locations():
    assert agent_config.DEFAULT_CONFIG_PATHS["pi"][0] == "~/.pi/agent/models.json"
    assert agent_config.DEFAULT_CONFIG_PATHS["hermes"][0] == "~/.hermes/config.yaml"
    assert agent_config.DEFAULT_CONFIG_PATHS["opencodex"][0] == "~/.opencodex/config.json"
    assert agent_config.DEFAULT_CONFIG_PATHS["zcode"][0] == "~/.zcode/v2/config.json"
    assert agent_config.DEFAULT_CONFIG_PATHS["dsh"][0] == "~/.dsh/settings.yaml"


def test_validate_cli_exit_code_and_no_config_dump():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(_dsh_config()), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(Path(agent_config.__file__)), "dsh", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert '"valid": true' in result.stdout
        assert "Bearer local" not in result.stdout
        path.write_text('{"llm-pi-ai": {"providers": {"local": {"models": []}}}}', encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(Path(agent_config.__file__)), "dsh", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert '"valid": false' in result.stdout


def test_agent_cli_preview_apply_restore_confirmation():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "agent.json"
        candidate = root / "candidate.json"
        config = _dsh_config()
        target.write_text(json.dumps(config), encoding="utf-8")
        candidate.write_text(json.dumps(config), encoding="utf-8")
        preview = subprocess.run([sys.executable, str(Path(agent_config.__file__)), "preview", "dsh", str(candidate)], capture_output=True, text=True)
        assert preview.returncode == 0 and '"valid": true' in preview.stdout
        denied = subprocess.run([sys.executable, str(Path(agent_config.__file__)), "apply", "dsh", str(target), str(candidate)], capture_output=True, text=True)
        assert denied.returncode == 2 and "--yes" in denied.stdout
        applied = subprocess.run([sys.executable, str(Path(agent_config.__file__)), "apply", "dsh", str(target), str(candidate), "--yes"], capture_output=True, text=True)
        assert applied.returncode == 0 and '"applied": true' in applied.stdout
        restore = subprocess.run([sys.executable, str(Path(agent_config.__file__)), "restore", "dsh", str(target), "--yes"], capture_output=True, text=True)
        assert restore.returncode == 0 and '"restored": true' in restore.stdout


def test_restore_is_safe_and_atomic():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "agent.json"
        original = _dsh_config()
        path.write_text(json.dumps(original), encoding="utf-8")
        changed = dict(original)
        changed["llm-pi-ai"] = dict(original["llm-pi-ai"])
        changed["llm-pi-ai"]["providers"] = {}
        agent_config.apply_config("dsh", path, original, confirm=True)
        path.write_text(json.dumps(changed), encoding="utf-8")
        restored = agent_config.restore_config("dsh", path)
        assert restored["restored"] is True
        assert agent_config.load_config(path) == original
        cli = subprocess.run(
            [sys.executable, str(Path(agent_config.__file__)), "restore", "dsh", str(path), "--yes"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert cli.returncode == 0
        assert '"restored": true' in cli.stdout
        try:
            agent_config.restore_config("dsh", path, Path(tmp) / "other.json")
        except agent_config.AgentConfigError as exc:
            assert "sibling" in str(exc)
        else:
            raise AssertionError("restore accepted a non-sibling backup")



TESTS = [
    test_dsh_valid_config_normalizes_capabilities,
    test_dsh_requires_nonempty_authorization,
    test_dsh_online_providers_are_not_touched,
    test_reasoning_and_capability_validation,
    test_environment_references_must_exist_without_exposing_values,
    test_preview_is_read_only,
    test_apply_backs_up_and_replaces_atomically,
    test_apply_rejects_invalid_before_writing,
    test_all_six_agents_supported_and_generic_layout,
    test_common_dict_models_and_baseurl_aliases,
    test_validate_cli_exit_code_and_no_config_dump,
    test_agent_cli_preview_apply_restore_confirmation,
    test_restore_is_safe_and_atomic,
]


def main():
    for test in TESTS:
        print(f"run {test.__name__}", flush=True)
        test()
        print(f"ok {test.__name__}", flush=True)


if __name__ == "__main__":
    main()
