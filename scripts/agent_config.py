#!/usr/bin/env python3
"""Small, reviewable Agent/provider configuration adapter.

The module deliberately separates preview/validation from apply.  It only
writes a provider file when :func:`apply_config` is called explicitly and
always leaves a sibling backup before replacing it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import argparse
import re
from pathlib import Path
from typing import Any, Mapping

import yaml


AGENTS = ("pi", "hermes", "opencode", "opencodex", "zcode", "dsh")
# Fixed candidates only: callers may present existing files for review, but no
# directory walk or broad machine scan is performed.
DEFAULT_CONFIG_PATHS = {
    "pi": ("~/.pi/agent/models.json", "~/.pi/agent/settings.json"),
    "hermes": ("~/.hermes/config.yaml", "~/.hermes/config.json"),
    "opencode": ("~/.config/opencode/opencode.json",),
    "opencodex": ("~/.opencodex/config.json", "~/.config/opencodex/config.json"),
    "zcode": ("~/.zcode/v2/config.json", "~/.config/zcode/config.json"),
    "dsh": ("~/.dsh/settings.yaml", "~/.config/dsh/config.json"),
}
_REASONING = {"off", "low", "medium", "high", "xhigh", "max", "ultra"}
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_ENV_REF = re.compile(r"\$(?:\{([A-Z][A-Z0-9_]*)\}|([A-Z][A-Z0-9_]*))")


class AgentConfigError(ValueError):
    pass


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentConfigError(f"{name} must be a mapping")
    return dict(value)


def _load(path: Path) -> Any:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentConfigError(f"cannot read config: {path}") from exc
    try:
        return json.loads(raw) if path.suffix.lower() == ".json" else yaml.safe_load(raw)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise AgentConfigError(f"invalid config syntax: {path}") from exc


def load_config(path: str | Path) -> Any:
    return _load(Path(path).expanduser())


def default_config_candidates(agent: str) -> list[Path]:
    """Return existing files from the fixed per-agent candidate list."""
    if agent not in DEFAULT_CONFIG_PATHS:
        raise AgentConfigError(f"unsupported agent: {agent}")
    return [path for raw in DEFAULT_CONFIG_PATHS[agent] if (path := Path(raw).expanduser()).is_file()]


def _provider_entries(agent: str, config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return provider dictionaries from common Agent config layouts."""
    if agent == "dsh":
        root = config.get("llm-pi-ai", config)
        providers = root.get("providers", {}) if isinstance(root, Mapping) else {}
        return [dict(v, _name=k) for k, v in providers.items() if isinstance(v, Mapping)]
    providers = config.get("providers") or config.get("custom_providers") or config.get("models") or config.get("provider")
    if isinstance(providers, Mapping):
        return [dict(v, _name=k) for k, v in providers.items() if isinstance(v, Mapping)]
    if isinstance(providers, list):
        return [dict(v) for v in providers if isinstance(v, Mapping)]
    return [dict(config)] if isinstance(config, Mapping) else []


def _model_entries(provider: Mapping[str, Any]) -> list[dict[str, Any]]:
    context_map = provider.get("modelContextWindows") if isinstance(provider.get("modelContextWindows"), Mapping) else {}
    output_map = provider.get("modelMaxOutputTokens") if isinstance(provider.get("modelMaxOutputTokens"), Mapping) else {}

    def decorate(model_id: Any, entry: dict[str, Any]) -> dict[str, Any]:
        entry.setdefault("id", model_id)
        if model_id in context_map:
            entry.setdefault("contextWindow", context_map[model_id])
        if model_id in output_map:
            entry.setdefault("maxTokens", output_map[model_id])
        return entry

    models = provider.get("models", provider.get("model"))
    if isinstance(models, Mapping):
        model_fields = {"id", "name", "contextWindow", "context_window", "maxTokens", "max_output_tokens", "input", "input_modalities", "reasoning", "reasoningEfforts", "reasoning_levels", "reasoningEffort", "reasoning_effort", "default_reasoning", "supports_vision"}
        if model_fields.intersection(models):
            return [dict(models)]
        entries = []
        for model_id, value in models.items():
            if isinstance(value, Mapping):
                entries.append(decorate(model_id, dict(value)))
            elif isinstance(value, str):
                entries.append(decorate(model_id, {"name": value}))
            else:
                entries.append(decorate(model_id, {}))
        return entries
    if isinstance(models, list):
        return [decorate(v.get("id", v.get("name")), dict(v)) if isinstance(v, Mapping) else decorate(v, {}) for v in models if isinstance(v, (Mapping, str))]
    return []


def _is_loopback_provider(provider: Mapping[str, Any]) -> bool:
    base = provider.get("baseURL", provider.get("baseUrl", provider.get("base_url", provider.get("endpoint"))))
    if not isinstance(base, str) or not base.strip():
        return False
    from urllib.parse import urlsplit

    try:
        return urlsplit(base).hostname in _LOOPBACK
    except ValueError:
        return False


def _effective_capabilities(model: Mapping[str, Any]) -> dict[str, Any]:
    caps = model.get("capabilities")
    if isinstance(caps, Mapping):
        return dict(caps)
    modalities = model.get("modalities")
    inputs = model.get("input") or model.get("input_modalities")
    if inputs is None and isinstance(modalities, Mapping):
        inputs = modalities.get("input")
    if inputs is None:
        inputs = ["text", "image"] if model.get("supports_vision") is True else ["text"]
    return {"text": "text" in inputs, "vision": "image" in inputs, "stream": bool(model.get("stream", True)), "chat": True}


def _validate_reasoning(model: Mapping[str, Any], where: str) -> tuple[list[str], str | None]:
    default = model.get("default_reasoning", model.get("reasoning_effort", model.get("reasoningEffort")))
    native = model.get("reasoningEfforts")
    if native is False:
        declared = ["off"]
    elif native is not None:
        if not isinstance(native, Mapping) or not native:
            raise AgentConfigError(f"{where}.reasoningEfforts must be false or a non-empty effort map")
        invalid = [
            key for key, value in native.items()
            if key not in _REASONING
            or (key == "off" and value is not None and not isinstance(value, str))
            or (key != "off" and (not isinstance(value, str) or not value))
        ]
        if invalid or not any(key != "off" for key in native):
            raise AgentConfigError(f"{where}.reasoningEfforts must use {_REASONING}, with a wire value for every level except off")
        declared = list(native)
    else:
        declared = model.get("reasoning_levels")
    if declared is None:
        declared = model.get("reasoning")
    if isinstance(declared, Mapping):
        declared = declared.get("levels", declared.get("supported", declared.get("efforts")))
        if default is None and isinstance(model.get("reasoning"), Mapping):
            default = model["reasoning"].get("default")
    if isinstance(declared, bool):
        # Boolean fields in Pi/OpenCode mean that reasoning is supported, not
        # which server-specific effort values are accepted. A default cannot
        # invent the accepted range; require an explicit list in that case.
        if default is not None:
            raise AgentConfigError(f"{where}.reasoning_levels must be explicitly declared when a default is set")
        declared = ["off"]
    if declared is None:
        if default is not None:
            raise AgentConfigError(f"{where}.reasoning_levels must be explicitly declared when a default is set")
        declared = ["off"]
    if isinstance(declared, str):
        declared = [declared]
    if not isinstance(declared, list) or not all(isinstance(x, str) and x in _REASONING for x in declared):
        raise AgentConfigError(f"{where}.reasoning_levels must use {_REASONING}")
    declared = list(dict.fromkeys(declared))
    if default is not None and (not isinstance(default, str) or default not in declared):
        raise AgentConfigError(f"{where}.default_reasoning must be one of declared reasoning_levels")
    return declared, default


def _environment_references(value: Any) -> set[str]:
    """Collect environment references without reading or returning values."""
    refs: set[str] = set()
    if isinstance(value, str):
        refs.update(match.group(1) or match.group(2) for match in _ENV_REF.finditer(value))
    elif isinstance(value, Mapping):
        for key, item in value.items():
            refs.update(_environment_references(key))
            refs.update(_environment_references(item))
    elif isinstance(value, list):
        for item in value:
            refs.update(_environment_references(item))
    return refs


def validate_config(agent: str, config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize capabilities without changing the input."""
    if agent not in AGENTS:
        raise AgentConfigError(f"unsupported agent: {agent}")
    root = _mapping(config, "config")
    findings: list[dict[str, Any]] = []
    normalized: list[dict[str, Any]] = []
    skipped: list[str] = []
    providers = _provider_entries(agent, root)
    if not providers:
        raise AgentConfigError("config declares no providers")
    for provider in providers:
        name = str(provider.get("_name", provider.get("name", "provider")))
        for key in provider.get("env", []) if isinstance(provider.get("env"), list) else []:
            if not isinstance(key, str) or not key.strip():
                raise AgentConfigError(f"provider {name} contains an unset environment key")
            if not os.environ.get(key):
                raise AgentConfigError(f"provider {name} references unset environment key: {key}")
        # Shell interpolation in online provider definitions is owned by the
        # client and is often intentionally unavailable to a GUI process. We
        # only validate such references for loopback providers we dispatch.
        if _is_loopback_provider(provider):
            missing_refs = sorted(name for name in _environment_references(provider) if not os.environ.get(name))
            if missing_refs:
                raise AgentConfigError(f"provider {name} references unset environment key(s): {', '.join(missing_refs)}")
        if agent == "dsh" and _is_loopback_provider(provider):
            headers = provider.get("headers")
            if not isinstance(headers, Mapping):
                raise AgentConfigError(f"provider {name} must declare headers")
            auth = headers.get("Authorization", headers.get("authorization"))
            # DSH 0.1.2-rc.1 rejects an empty/missing auth value for local APIs.
            # Online/OAuth providers (no loopback baseURL) stay untouched.
            if not isinstance(auth, str) or not auth.strip():
                raise AgentConfigError(f"provider {name} must declare non-empty headers.Authorization")
        models = _model_entries(provider)
        if not models:
            skipped.append(name)
            continue
        normalized_models: list[dict[str, Any]] = []
        for index, model in enumerate(models):
            where = f"provider {name}.models[{index}]"
            levels, default = _validate_reasoning(model, where)
            caps = _effective_capabilities(model)
            inputs = model.get("input") or model.get("input_modalities")
            modalities = model.get("modalities")
            if inputs is None and isinstance(modalities, Mapping):
                inputs = modalities.get("input")
            if inputs is not None and (not isinstance(inputs, list) or not all(isinstance(x, str) and x.strip() for x in inputs)):
                raise AgentConfigError(f"{where}.input must be a string array")
            if agent == "dsh" and inputs is not None and any(x not in {"text", "image"} for x in inputs):
                raise AgentConfigError(f"{where}.input supports only text and image")
            limits = model.get("limit") if isinstance(model.get("limit"), Mapping) else {}
            ctx = model.get("contextWindow", model.get("context_window", model.get("context_length", limits.get("context"))))
            out = model.get("maxTokens", model.get("max_output_tokens", limits.get("output")))
            if ctx is not None and (isinstance(ctx, bool) or not isinstance(ctx, int) or ctx <= 0):
                raise AgentConfigError(f"{where}.contextWindow must be a positive integer")
            if out is not None and (isinstance(out, bool) or not isinstance(out, int) or out <= 0):
                raise AgentConfigError(f"{where}.maxTokens must be a positive integer")
            if default is None and levels:
                findings.append({"provider": name, "model": model.get("id", model.get("name", index)), "default_reasoning": levels[0]})
            normalized_models.append({
                "id": model.get("id", model.get("name", index)),
                "reasoning_levels": levels,
                "default_reasoning": default if default is not None else levels[0],
                "effective_capabilities": caps,
            })
        normalized.append({"provider": name, "models": normalized_models})
    if not normalized:
        raise AgentConfigError("config declares no providers with models")
    return {"agent": agent, "valid": True, "providers": len(providers), "normalized": normalized, "skipped_providers": skipped, "default_reasoning_filled": findings}


def preview_config(agent: str, config_or_path: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    config = load_config(config_or_path) if isinstance(config_or_path, (str, Path)) else config_or_path
    report = validate_config(agent, config)
    return {"agent": agent, "valid": True, "config": config, "validation": report, "changes": [], "apply_required": True}


def _dump(path: Path, data: Any) -> bytes:
    if path.suffix.lower() == ".json":
        return (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False).encode()


def dsh_running() -> bool:
    try:
        result = subprocess.run(["pgrep", "-f", r"(^|/)dsh(\s|$)|dsh --profile"], capture_output=True, text=True, check=False)
        return result.returncode == 0
    except OSError:
        return False


def apply_config(agent: str, path: str | Path, config: Mapping[str, Any], *, confirm: bool = False) -> dict[str, Any]:
    """Validate, back up and atomically replace *path*; never restarts a client."""
    if not confirm:
        raise AgentConfigError("apply requires explicit confirmation")
    destination = Path(path).expanduser()
    validate_config(agent, config)
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = destination.with_name(destination.name + ".bak-inferencedock")
    if destination.exists():
        shutil.copy2(destination, backup)
    payload = _dump(destination, config)
    fd, temp_name = tempfile.mkstemp(prefix=destination.name + ".", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, destination)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    restart_required = agent == "dsh" and dsh_running()
    return {"agent": agent, "path": str(destination), "backup": str(backup) if backup.exists() else None, "applied": True, "restart_required": restart_required, "note": "新会话才会读取配置；不会自动终止正在运行的 DSH" if restart_required else None}


def restore_config(agent: str, path: str | Path, backup: str | Path | None = None) -> dict[str, Any]:
    """Atomically restore the reviewed sibling backup without exposing secrets."""
    destination = Path(path).expanduser()
    expected = destination.with_name(destination.name + ".bak-inferencedock")
    source = Path(backup).expanduser() if backup is not None else expected
    if source.resolve() != expected.resolve():
        raise AgentConfigError("restore only accepts the sibling .bak-inferencedock backup")
    if not source.is_file():
        raise AgentConfigError(f"backup not found: {source}")
    validate_config(agent, load_config(source))
    destination.parent.mkdir(parents=True, exist_ok=True)
    rollback = destination.with_name(destination.name + ".before-restore")
    if destination.exists():
        shutil.copy2(destination, rollback)
    fd, temp_name = tempfile.mkstemp(prefix=destination.name + ".restore.", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(source.read_bytes())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, destination)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return {"agent": agent, "path": str(destination), "restored_from": str(source), "rollback": str(rollback) if rollback.exists() else None, "restored": True}


# Explicit aliases make the API easy to discover from callers and tests.
validate_agent_config = validate_config
preview_agent_config = preview_config
apply_agent_config = apply_config
restore_agent_config = restore_config


def main(argv: list[str] | None = None) -> int:
    raw = list(argv if argv is not None else __import__("sys").argv[1:])
    command = raw[0] if raw and raw[0] in {"preview", "validate", "apply", "restore"} else "validate"
    if command == "validate" and (not raw or raw[0] not in {"preview", "validate", "apply", "restore"}):
        # Backward-compatible form: agent path
        parser = argparse.ArgumentParser(description="Validate an InferenceDock Agent configuration")
        parser.add_argument("agent", choices=AGENTS)
        parser.add_argument("path", type=Path)
        args = parser.parse_args(raw)
    else:
        parser = argparse.ArgumentParser(description="InferenceDock Agent configuration tool")
        sub = parser.add_subparsers(dest="command", required=True)
        for name in ("preview", "validate"):
            p = sub.add_parser(name)
            p.add_argument("agent", choices=AGENTS)
            p.add_argument("path", type=Path)
        apply_parser = sub.add_parser("apply")
        apply_parser.add_argument("agent", choices=AGENTS)
        apply_parser.add_argument("path", type=Path)
        apply_parser.add_argument("candidate", type=Path)
        apply_parser.add_argument("--yes", action="store_true")
        restore_parser = sub.add_parser("restore")
        restore_parser.add_argument("agent", choices=AGENTS)
        restore_parser.add_argument("path", type=Path)
        restore_parser.add_argument("--yes", action="store_true")
        args = parser.parse_args(raw)
    try:
        if getattr(args, "command", "validate") == "apply":
            if not args.yes:
                raise AgentConfigError("apply requires --yes confirmation")
            result = apply_config(args.agent, args.path, load_config(args.candidate), confirm=True)
        elif getattr(args, "command", "validate") == "restore":
            if not args.yes:
                raise AgentConfigError("restore requires --yes confirmation")
            result = restore_config(args.agent, args.path)
        else:
            result = validate_config(args.agent, load_config(args.path))
    except (AgentConfigError, OSError) as exc:
        print(json.dumps({"agent": args.agent, "valid": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
