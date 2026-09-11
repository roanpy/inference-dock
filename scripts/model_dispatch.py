#!/usr/bin/env python3
"""Minimal loopback model dispatcher core."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import signal
import socket
import statistics
import subprocess
import sys
import threading
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import parse_qsl, urlsplit
from dataclasses import dataclass, field, replace as dataclass_replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "engines.yaml"
SETTINGS_PATH = Path.home() / "Library" / "Application Support" / "InferenceDock" / "settings.json"
PROTECTED_PORTS = {8000, 8888, 11234, 18888, 18889, 18953}
LOCAL_CATALOG_TTL_SECONDS = 10.0


def _runtime_data_root(root: Path | None = None) -> Path:
    """Return the writable runtime-data root for source and bundled runs."""
    root = ROOT if root is None else root
    if root.name == "Contents" and root.parent.suffix == ".app":
        return Path.home() / "Library" / "Logs" / "InferenceDock"
    return root / "logs"


class ConfigError(ValueError):
    pass


class DispatchError(RuntimeError):
    def __init__(self, message: str, status: int = 500, error_type: str = "dispatch_error"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.error_type = error_type


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be an object")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value.strip()


def _path(value: Any, name: str) -> Path:
    """Parse a user path without silently accepting non-string config values."""
    raw = _string(value, name)
    if "\x00" in raw:
        raise ConfigError(f"{name} is not a valid path")
    try:
        return Path(raw).expanduser()
    except (RuntimeError, ValueError) as exc:
        raise ConfigError(f"{name} is not a valid path") from exc


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be a boolean")
    return value


def _port(value: Any, name: str) -> int:
    if not isinstance(value, int) or value < 1024 or value > 65535:
        raise ConfigError(f"{name} must be an integer from 1024 to 65535")
    if value in PROTECTED_PORTS:
        raise ConfigError(f"{name} {value} is reserved for an existing service")
    return value


def _free(port: int) -> bool:
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _argv(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        try:
            parts = shlex.split(value)
        except ValueError as exc:
            raise ConfigError(f"{name} is invalid: {exc}") from exc
    elif isinstance(value, list) and all(isinstance(item, str) and item for item in value):
        parts = list(value)
    else:
        raise ConfigError(f"{name} must be a command string or argv array")
    if not parts:
        raise ConfigError(f"{name} must not be empty")
    return tuple(parts)


def _action_path(value: Any, name: str) -> str | None:
    if value is None:
        return None
    path = _string(value, name)
    parsed = urlsplit(path)
    if (
        not path.startswith("/")
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or ".." in Path(parsed.path).parts
    ):
        raise ConfigError(f"{name} must be a safe relative URL path")
    return path


def _group_list(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
        raise ConfigError(f"{name} must be a non-empty string array")
    return tuple(dict.fromkeys(item.strip() for item in value))


@dataclass(frozen=True)
class AdapterConfig:
    name: str
    type: str
    command: tuple[str, ...] = ()
    # Optional read-only argv appended to command[0] for a local model catalog.
    catalog_args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    api_key_env: str | None = None
    endpoint: str | None = None
    port: int | None = None
    health_path: str = "/health"
    models_path: str | None = None
    python: str = sys.executable
    script: str = "scripts/fake_backend.py"
    events_path: str | None = None
    start_delay_seconds: float = 0.2
    sse_chunks: int = 6
    sse_delay_seconds: float = 0.25
    keep_resident: bool = False
    exclusive_group: str | None = None
    exclusive_groups: tuple[str, ...] = ()
    working_directory: Path | None = None
    log_path: Path | None = None
    stop_timeout_seconds: float = 5.0
    plugin_id: str | None = None
    service_key: str | None = None
    service_version: str | None = None
    display_name: str | None = None
    menu_group: str | None = None
    active_requests_path: str | None = None
    model_state_path: str | None = None


@dataclass(frozen=True)
class ModelConfig:
    name: str
    adapter: str
    backend_model: str
    resource_group: str | None
    capabilities: dict[str, bool]
    lifecycle_owner: str
    display_name: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    reasoning_levels: tuple[str, ...] = ()
    runtime_summary: tuple[str, ...] = ()
    activate_path: str | None = None
    activate_payload: dict[str, Any] = field(default_factory=dict)
    deactivate_path: str | None = None
    deactivate_payload: dict[str, Any] = field(default_factory=dict)
    keep_resident: bool = False
    exclusive_group: str | None = None
    exclusive_groups: tuple[str, ...] = ()
    estimated_memory_gb: float | None = None
    advertise: bool = True
    enabled: bool = True
    canonical: str | None = None
    aliases: tuple[str, ...] = ()
    service_variant: str | None = None
    catalog_id: str | None = None
    asset_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ResourceGroupConfig:
    name: str
    capacity: int


@dataclass(frozen=True)
class DispatchConfig:
    path: Path
    listen_host: str
    listen_port: int
    load_timeout_seconds: float
    connect_timeout_seconds: float
    request_timeout_seconds: float
    max_body_bytes: int
    request_poll_seconds: float
    metrics_path: Path
    adapters: dict[str, AdapterConfig]
    models: dict[str, ModelConfig]
    model_aliases: dict[str, str]
    resource_groups: dict[str, ResourceGroupConfig]
    idle_unload_seconds: float | None = None


def load_config(path: Path) -> DispatchConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"config is not valid YAML: {exc}") from exc

    root = _mapping(raw, "root")
    listen_host = _string(root.get("listen_host", "127.0.0.1"), "listen_host")
    if listen_host not in {"127.0.0.1", "localhost"}:
        raise ConfigError("listen_host must stay on loopback")
    listen_port = _port(root.get("listen_port"), "listen_port")

    load_timeout = root.get("load_timeout_seconds", 60)
    if not isinstance(load_timeout, (int, float)) or load_timeout <= 0:
        raise ConfigError("load_timeout_seconds must be positive")
    connect_timeout = root.get("connect_timeout_seconds", 3)
    request_timeout = root.get("request_timeout_seconds", 120)
    max_body = root.get("max_body_bytes", 4 * 1024 * 1024)
    if not isinstance(connect_timeout, (int, float)) or connect_timeout <= 0:
        raise ConfigError("connect_timeout_seconds must be positive")
    if not isinstance(request_timeout, (int, float)) or request_timeout <= 0:
        raise ConfigError("request_timeout_seconds must be positive")
    if not isinstance(max_body, int) or max_body < 1024 or max_body > 64 * 1024 * 1024:
        raise ConfigError("max_body_bytes must be between 1024 and 67108864")
    poll = root.get("request_poll_seconds", 0.05)
    if not isinstance(poll, (int, float)) or poll <= 0 or poll > 1:
        raise ConfigError("request_poll_seconds must be greater than 0 and at most 1")
    metrics_path = Path(_string(root.get("metrics_path", str(_runtime_data_root() / "request-metrics.jsonl")), "metrics_path")).expanduser()
    idle_unload = root.get("idle_unload_seconds")
    if idle_unload is not None and (not isinstance(idle_unload, (int, float)) or idle_unload <= 0):
        raise ConfigError("idle_unload_seconds must be positive when set")

    adapters: dict[str, AdapterConfig] = {}
    for name, item in _mapping(root.get("adapters", {}), "adapters").items():
        item = _mapping(item, f"adapters.{name}")
        adapter_type = _string(item.get("type"), f"adapters.{name}.type")
        env = item.get("env", {})
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ConfigError(f"adapters.{name}.env must be a string map")
        api_key_env = item.get("api_key_env")
        if api_key_env is not None and (
            not isinstance(api_key_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", api_key_env)
        ):
            raise ConfigError(f"adapters.{name}.api_key_env must be an uppercase environment variable name")
        port = item.get("port")
        if port is not None:
            port = _port(port, f"adapters.{name}.port")
        endpoint = item.get("endpoint")
        endpoint_port_value = None
        if endpoint is not None:
            endpoint = _string(endpoint, f"adapters.{name}.endpoint").rstrip("/")
            parsed = urlsplit(endpoint)
            if parsed.scheme != "http" or parsed.username or parsed.password or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                raise ConfigError(f"adapters.{name}.endpoint must be loopback HTTP")
            try:
                if parsed.port is None:
                    raise ValueError
                if parsed.port < 1024 or parsed.port > 65535:
                    raise ValueError
                endpoint_port_value = parsed.port
            except ValueError as exc:
                raise ConfigError(f"adapters.{name}.endpoint must include a valid port") from exc
            if port is not None and port != endpoint_port_value:
                raise ConfigError(f"adapters.{name}.port must match the endpoint port")
        command = _argv(item.get("command"), f"adapters.{name}.command")
        catalog_args = _argv(item.get("catalog_args"), f"adapters.{name}.catalog_args")
        working_directory = _path(item["working_directory"], f"adapters.{name}.working_directory") if item.get("working_directory") is not None else None
        log_path = _path(item["log_path"], f"adapters.{name}.log_path") if item.get("log_path") is not None else None
        stop_timeout = item.get("stop_timeout_seconds", 5)
        try:
            stop_timeout_value = float(stop_timeout)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ConfigError(f"adapters.{name}.stop_timeout_seconds must be positive") from exc
        if (
            isinstance(stop_timeout, bool)
            or not isinstance(stop_timeout, (int, float))
            or not math.isfinite(stop_timeout_value)
            or stop_timeout <= 0
        ):
            raise ConfigError(f"adapters.{name}.stop_timeout_seconds must be positive")
        legacy_group = _string(item["exclusive_group"], f"adapters.{name}.exclusive_group") if item.get("exclusive_group") is not None else None
        groups_value = _group_list(item.get("exclusive_groups"), f"adapters.{name}.exclusive_groups")
        adapter_groups = tuple(dict.fromkeys(([legacy_group] if legacy_group else []) + list(groups_value)))
        adapter_display_name = item.get("display_name")
        if adapter_display_name is not None:
            adapter_display_name = _string(adapter_display_name, f"adapters.{name}.display_name")
        menu_group_value = item.get("menu_group", item.get("group_name"))
        if menu_group_value is not None:
            menu_group_value = _string(menu_group_value, f"adapters.{name}.menu_group")
        if item.get("menu_group") is not None and item.get("group_name") is not None and item["menu_group"] != item["group_name"]:
            raise ConfigError(f"adapters.{name}.menu_group and group_name must match")
        adapters[name] = AdapterConfig(
            name=name,
            type=adapter_type,
            command=command,
            catalog_args=catalog_args,
            env=dict(env),
            api_key_env=api_key_env,
            endpoint=endpoint,
            port=port,
            health_path=_string(item.get("health_path", "/health"), f"adapters.{name}.health_path"),
            models_path=_action_path(item.get("models_path"), f"adapters.{name}.models_path"),
            python=_string(item.get("python", sys.executable), f"adapters.{name}.python"),
            script=_string(item.get("script", "scripts/fake_backend.py"), f"adapters.{name}.script"),
            events_path=item.get("events_path"),
            start_delay_seconds=float(item.get("start_delay_seconds", 0.2)),
            sse_chunks=int(item.get("sse_chunks", 6)),
            sse_delay_seconds=float(item.get("sse_delay_seconds", 0.25)),
            keep_resident=_bool(item.get("keep_resident", False), f"adapters.{name}.keep_resident"),
            exclusive_group=adapter_groups[0] if adapter_groups else None,
            exclusive_groups=adapter_groups,
            working_directory=working_directory,
            log_path=log_path,
            stop_timeout_seconds=stop_timeout_value,
            plugin_id=_string(item["plugin_id"], f"adapters.{name}.plugin_id") if item.get("plugin_id") is not None else None,
            service_key=_string(item["service_key"], f"adapters.{name}.service_key") if item.get("service_key") is not None else None,
            service_version=_string(item["service_version"], f"adapters.{name}.service_version") if item.get("service_version") is not None else None,
            display_name=adapter_display_name,
            menu_group=menu_group_value,
            active_requests_path=_action_path(item.get("active_requests_path"), f"adapters.{name}.active_requests_path"),
            model_state_path=_action_path(item.get("model_state_path"), f"adapters.{name}.model_state_path"),
        )

    groups: dict[str, ResourceGroupConfig] = {}
    for name, item in _mapping(root.get("resource_groups", {}), "resource_groups").items():
        item = _mapping(item, f"resource_groups.{name}")
        capacity = item.get("capacity", 1)
        if not isinstance(capacity, int) or capacity < 1:
            raise ConfigError(f"resource_groups.{name}.capacity must be a positive integer")
        groups[name] = ResourceGroupConfig(name, capacity)

    models: dict[str, ModelConfig] = {}
    for name, item in _mapping(root.get("models", {}), "models").items():
        item = _mapping(item, f"models.{name}")
        adapter = _string(item.get("adapter"), f"models.{name}.adapter")
        if adapter not in adapters:
            raise ConfigError(f"models.{name}.adapter references unknown adapter {adapter}")
        group = item.get("resource_group")
        if group is not None:
            group = _string(group, f"models.{name}.resource_group")
            if group not in groups:
                raise ConfigError(f"models.{name}.resource_group references unknown group {group}")
        caps = _mapping(item.get("capabilities", {}), f"models.{name}.capabilities")
        display_name = item.get("display_name")
        if display_name is not None:
            display_name = _string(display_name, f"models.{name}.display_name")
        context_window = item.get("context_window")
        if context_window is not None and (not isinstance(context_window, int) or context_window < 1):
            raise ConfigError(f"models.{name}.context_window must be a positive integer")
        max_output_tokens = item.get("max_output_tokens")
        if max_output_tokens is not None and (not isinstance(max_output_tokens, int) or max_output_tokens < 1):
            raise ConfigError(f"models.{name}.max_output_tokens must be a positive integer")
        reasoning_levels = item.get("reasoning_levels", [])
        if not isinstance(reasoning_levels, list) or not all(isinstance(level, str) and level for level in reasoning_levels):
            raise ConfigError(f"models.{name}.reasoning_levels must be a string array")
        runtime_summary = item.get("runtime_summary", [])
        if not isinstance(runtime_summary, list) or not all(isinstance(value, str) and value for value in runtime_summary):
            raise ConfigError(f"models.{name}.runtime_summary must be a string array")
        activate_path = _action_path(item.get("activate_path"), f"models.{name}.activate_path")
        activate_payload = item.get("activate_payload", {})
        if not isinstance(activate_payload, dict):
            raise ConfigError(f"models.{name}.activate_payload must be an object")
        deactivate_path = _action_path(item.get("deactivate_path"), f"models.{name}.deactivate_path")
        deactivate_payload = item.get("deactivate_payload", {})
        if not isinstance(deactivate_payload, dict):
            raise ConfigError(f"models.{name}.deactivate_payload must be an object")
        legacy_group = _string(item["exclusive_group"], f"models.{name}.exclusive_group") if item.get("exclusive_group") is not None else None
        groups_value = _group_list(item.get("exclusive_groups"), f"models.{name}.exclusive_groups")
        model_groups = tuple(dict.fromkeys(([legacy_group] if legacy_group else []) + list(groups_value)))
        if not model_groups:
            model_groups = tuple(adapters[adapter].exclusive_groups) or ((group,) if group else ())
        estimated_memory = item.get("estimated_memory_gb")
        if estimated_memory is not None:
            if isinstance(estimated_memory, bool):
                raise ConfigError(f"models.{name}.estimated_memory_gb must be positive")
            try:
                estimated_memory = float(estimated_memory)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ConfigError(f"models.{name}.estimated_memory_gb must be positive") from exc
            if not math.isfinite(estimated_memory) or estimated_memory <= 0:
                raise ConfigError(f"models.{name}.estimated_memory_gb must be positive")
        advertise = _bool(item.get("advertise", True), f"models.{name}.advertise")
        enabled = _bool(item.get("enabled", True), f"models.{name}.enabled")
        canonical = item.get("canonical", name)
        canonical = _string(canonical, f"models.{name}.canonical")
        aliases_value = item.get("aliases", [])
        if not isinstance(aliases_value, list) or not all(isinstance(alias, str) and alias.strip() for alias in aliases_value):
            raise ConfigError(f"models.{name}.aliases must be a string array")
        aliases = tuple(dict.fromkeys(alias.strip() for alias in aliases_value))
        service_variant = item.get("service_variant")
        if service_variant is not None:
            service_variant = _string(service_variant, f"models.{name}.service_variant")
        catalog_id = item.get("catalog_id")
        if catalog_id is not None:
            catalog_id = _string(catalog_id, f"models.{name}.catalog_id")
        asset_values = item.get("asset_paths")
        if asset_values is None:
            asset_paths = ()
        elif not isinstance(asset_values, list) or not asset_values or not all(isinstance(value, str) and value.strip() for value in asset_values):
            raise ConfigError(f"models.{name}.asset_paths must be a non-empty string array")
        else:
            parsed_assets = tuple(_path(value, f"models.{name}.asset_paths[{index}]") for index, value in enumerate(asset_values))
            asset_paths = tuple(asset.resolve() if asset.is_absolute() else (path.parent / asset).resolve() for asset in parsed_assets)
        models[name] = ModelConfig(
            name=name,
            adapter=adapter,
            backend_model=_string(item.get("backend_model", name), f"models.{name}.backend_model"),
            resource_group=group,
            capabilities={
                "chat": _bool(caps.get("chat", True), f"models.{name}.capabilities.chat"),
                "stream": _bool(caps.get("stream", True), f"models.{name}.capabilities.stream"),
                "vision": _bool(caps.get("vision", False), f"models.{name}.capabilities.vision"),
            },
            lifecycle_owner=_string(item.get("lifecycle_owner", "model-dispatch"), f"models.{name}.lifecycle_owner"),
            display_name=display_name,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            reasoning_levels=tuple(reasoning_levels),
            runtime_summary=tuple(runtime_summary),
            activate_path=activate_path,
            activate_payload=dict(activate_payload),
            deactivate_path=deactivate_path,
            deactivate_payload=dict(deactivate_payload),
            keep_resident=_bool(item.get("keep_resident", adapters[adapter].keep_resident), f"models.{name}.keep_resident"),
            exclusive_group=model_groups[0] if model_groups else None,
            exclusive_groups=model_groups,
            estimated_memory_gb=estimated_memory,
            advertise=advertise,
            enabled=enabled,
            canonical=canonical,
            aliases=aliases,
            service_variant=service_variant,
            catalog_id=catalog_id,
            asset_paths=asset_paths,
        )

    if not models:
        raise ConfigError("at least one model is required")

    # Canonical IDs must resolve to a configured model; aliases are unique.
    aliases: dict[str, str] = {}
    for model in models.values():
        if model.canonical not in models:
            raise ConfigError(f"models.{model.name}.canonical references unknown model {model.canonical}")
        if model.canonical != model.name and model.aliases:
            raise ConfigError(f"models.{model.name}.aliases is only allowed on canonical models")
        for alias in model.aliases:
            if alias in models and alias != model.name:
                raise ConfigError(f"models.{model.name}.aliases conflicts with model id {alias}")
            owner = aliases.get(alias)
            if owner and owner != model.name:
                raise ConfigError(f"alias {alias!r} is declared by multiple models")
            aliases[alias] = model.name

    # Runtime identity: one visible model per (adapter, backend_model); an alias
    # may never shadow a real backend id, and aliases must point at usable models.
    backend_identity: dict[tuple[str, str], str] = {}
    for model in models.values():
        if not model.enabled or not model.advertise or model.canonical != model.name:
            continue
        key = (model.adapter, model.backend_model)
        other = backend_identity.get(key)
        if other is not None:
            raise ConfigError(
                f"models {other!r} and {model.name!r} share adapter {model.adapter} backend id {model.backend_model!r}; "
                "keep one canonical entry and move the other id into aliases"
            )
        backend_identity[key] = model.name
    for alias, target in aliases.items():
        canonical = models[target]
        if not canonical.enabled:
            raise ConfigError(f"alias {alias!r} points at disabled model {target}")
        backend_owner = backend_identity.get((canonical.adapter, alias))
        if backend_owner is not None and backend_owner != target:
            raise ConfigError(f"alias {alias!r} shadows the backend id of model {backend_owner} on adapter {canonical.adapter}")

    adapter_ports: dict[int, list[AdapterConfig]] = {}
    for adapter in adapters.values():
        if adapter.port is not None or adapter.endpoint is not None:
            adapter_ports.setdefault(endpoint_port(adapter), []).append(adapter)
    if listen_port in adapter_ports:
        raise ConfigError("dispatcher port conflicts with adapter port")
    for port, owners in adapter_ports.items():
        if len(owners) < 2:
            continue
        if all(adapter.type in {"external", "observe", "http-managed"} for adapter in owners):
            continue
        shared_groups = set(owners[0].exclusive_groups)
        if (
            not shared_groups
            or any(adapter.type != "managed" for adapter in owners)
            or not any(
                all(group in adapter.exclusive_groups for adapter in owners)
                and group in groups
                and groups[group].capacity == 1
                for group in shared_groups
            )
        ):
            raise ConfigError(f"adapter port {port} is shared without a single exclusive managed group")

    for adapter in adapters.values():
        if adapter.type == "fake":
            if adapter.port is None:
                raise ConfigError(f"adapters.{adapter.name}.port is required")
            if not _free(adapter.port):
                raise ConfigError(f"fake adapter port {adapter.port} is already in use")
        elif adapter.type == "managed":
            if not adapter.command:
                raise ConfigError(f"adapters.{adapter.name}.command is required")
            if adapter.endpoint is None and adapter.port is None:
                raise ConfigError(f"adapters.{adapter.name} needs endpoint or port")
        elif adapter.type in {"external", "observe", "http-managed"}:
            if adapter.endpoint is None:
                raise ConfigError(f"adapters.{adapter.name}.endpoint is required")
        else:
            raise ConfigError(f"adapters.{adapter.name}.type {adapter.type!r} is not supported")

    for model in models.values():
        if adapters[model.adapter].type == "managed" and model.lifecycle_owner != "model-dispatch":
            raise ConfigError(f"models.{model.name}.lifecycle_owner must be model-dispatch for a managed adapter")
        if adapters[model.adapter].type == "http-managed" and not model.activate_path:
            raise ConfigError(f"models.{model.name}.activate_path is required for an http-managed adapter")

    return DispatchConfig(
        path=path,
        listen_host=listen_host,
        listen_port=listen_port,
        load_timeout_seconds=float(load_timeout),
        connect_timeout_seconds=float(connect_timeout),
        request_timeout_seconds=float(request_timeout),
        max_body_bytes=max_body,
        request_poll_seconds=float(poll),
        idle_unload_seconds=float(idle_unload) if idle_unload is not None else None,
        metrics_path=metrics_path,
        adapters=adapters,
        models=models,
        model_aliases=aliases,
        resource_groups=groups,
    )


def backend_headers(adapter: AdapterConfig) -> dict[str, str]:
    """Build backend-only auth headers from an environment reference."""
    if not adapter.api_key_env:
        return {}
    value = os.environ.get(adapter.api_key_env)
    if not value:
        raise DispatchError(
            f"backend credentials are unavailable ({adapter.api_key_env} is not set)",
            503,
            "backend_credentials_missing",
        )
    return {"Authorization": f"Bearer {value}"}


def http_json(url: str, timeout: float = 0.5, headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    try:
        request = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            return exc.code, {}
    except Exception:
        return 0, {}


def endpoint_for(adapter: AdapterConfig) -> str:
    if adapter.endpoint:
        return adapter.endpoint
    if adapter.port is None:
        raise ConfigError(f"adapter {adapter.name} has no endpoint or port")
    return f"http://127.0.0.1:{adapter.port}"


def adapter_metadata(adapter: AdapterConfig, *, status: str = "configured") -> dict[str, Any]:
    """Expose platform/install/instance identity without changing config shape."""
    endpoint = None
    try:
        endpoint = endpoint_for(adapter)
    except ConfigError:
        pass
    return {
        "platform": {"id": adapter.plugin_id or adapter.name, "display_name": adapter.display_name or adapter.name},
        "installation": {
            "id": adapter.plugin_id or adapter.name,
            "version": adapter.service_version,
            "source": "configured",
        },
        "instance": {
            "id": adapter.service_key or adapter.name,
            "endpoint": endpoint,
            "mode": adapter.type,
            "status": status,
        },
    }


def local_catalog_reference(adapter: AdapterConfig, backend_model: str) -> str:
    """Resolve a configured model to the local catalog's repository ID.

    MTPLX embeds both values in its managed argv (``--model`` and
    ``--model-id``). Other adapters may simply expose the backend ID itself.
    """
    command = adapter.command
    for index, token in enumerate(command[:-1]):
        if token != "--model-id" or command[index + 1] != backend_model:
            continue
        for previous in range(index - 1, -1, -1):
            if command[previous] == "--model" and previous + 1 < len(command):
                return command[previous + 1]
        break
    return backend_model


def parse_model_list(payload: Any) -> set[str]:
    """Read model IDs from a standard ``data``/``models`` list response."""
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        entries = payload.get("data")
        if entries is None:
            entries = payload.get("models")
    else:
        entries = None
    if not isinstance(entries, list):
        raise ValueError("model response has no models list")
    discovered: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("model list contains a non-object entry")
        found = False
        for key in ("repo_id", "id", "name", "model"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                discovered.add(value.strip())
                found = True
                break
        if not found:
            raise ValueError("model list entry has no id")
    return discovered


def parse_local_catalog(stdout: str) -> set[str]:
    """Read repository IDs from a trusted local catalog command's JSON."""
    return parse_model_list(json.loads(stdout or "{}"))


def endpoint_port(adapter: AdapterConfig) -> int:
    """Return the loopback TCP port used by an adapter."""
    parsed = urlsplit(adapter.endpoint) if adapter.endpoint else None
    if parsed is None:
        if adapter.port is None:
            raise ConfigError(f"adapter {adapter.name} has no endpoint or port")
        return adapter.port
    if parsed.port is None:
        raise ConfigError(f"adapter {adapter.name} endpoint has no port")
    return parsed.port


def activate_model_if_needed(adapter: AdapterConfig, model: ModelConfig, timeout: float):
    if adapter.type == "observe" or not model.activate_path:
        return
    _post_model_action(adapter, model, model.activate_path, model.activate_payload, timeout, "activation")


def deactivate_model_if_needed(adapter: AdapterConfig, model: ModelConfig, timeout: float):
    if adapter.type == "observe" or not model.deactivate_path:
        return
    _post_model_action(adapter, model, model.deactivate_path, model.deactivate_payload, timeout, "deactivation")


def _post_model_action(
    adapter: AdapterConfig,
    model: ModelConfig,
    path: str,
    payload_template: dict[str, Any],
    timeout: float,
    action: str,
):
    payload = dict(payload_template)
    payload["model"] = model.backend_model
    request = urllib.request.Request(
        endpoint_for(adapter) + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **backend_headers(adapter)},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status >= 400:
                raise DispatchError(f"model {action} failed with HTTP {response.status}", 502, f"model_{action}_failed")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(8192)
        except OSError:
            body = b""
        if DispatchHandler._is_metal_gpu_timeout(body):
            raise DispatchError(f"model {action} failed with Metal GPU timeout", 502, "metal_gpu_timeout") from exc
        if (
            action == "activation"
            and (adapter.plugin_id == "mlx-serve" or adapter.name == "mlx-serve")
            and b"MlxError" in body
        ):
            # mlx-serve currently flattens Metal command-buffer failures to
            # MlxError at this boundary. Retry once through the same guarded
            # recovery path; a persistent failure is returned after the retry.
            raise DispatchError(f"model {action} failed with MlxError", 502, "mlx_activation_retryable") from exc
        raise DispatchError(f"model {action} failed with HTTP {exc.code}", 502, f"model_{action}_failed") from exc
    except OSError as exc:
        raise DispatchError(f"model {action} request failed: {exc}", 502, f"model_{action}_failed") from exc


class ProcessBackend:
    def __init__(self, adapter: AdapterConfig, root: Path):
        self.adapter = adapter
        self.root = root
        self.process: subprocess.Popen | None = None
        self.endpoint = endpoint_for(adapter)
        self.working_directory = Path(adapter.working_directory or root).expanduser()
        if not self.working_directory.is_absolute():
            self.working_directory = root / self.working_directory
        default_log_root = _runtime_data_root(root)
        self.log_path = Path(adapter.log_path or default_log_root / f"{adapter.name}.log").expanduser()
        if not self.log_path.is_absolute():
            self.log_path = root / self.log_path
        self.run_id = f"{time.time_ns()}-{os.getpid()}"
        self.events_path = Path(adapter.events_path) if adapter.events_path else default_log_root / f"fake-{adapter.port}-{self.run_id}.events.jsonl"
        self.stop_file = default_log_root / ".tmp" / f"fake-{adapter.port}-{self.run_id}.stop"

    def healthy(self) -> bool:
        status, _ = http_json(
            self.endpoint + self.adapter.health_path,
            timeout=0.4,
            headers=backend_headers(self.adapter),
        )
        return status == 200

    def ensure_started(self, timeout: float):
        if self.adapter.type in {"external", "observe", "http-managed"}:
            if not self.healthy():
                raise DispatchError(
                    f"external backend {self.adapter.name!r} is not healthy at {self.endpoint}",
                    503,
                    "backend_unavailable",
                )
            return
        if self.process and self.process.poll() is None and self.healthy():
            return
        if self.process and self.process.poll() is None:
            self.stop()
        if self.adapter.type == "managed":
            self._ensure_managed(timeout)
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DispatchError(f"fake backend log path is unavailable: {exc}", 502, "backend_start_failed") from exc
        self.stop_file.parent.mkdir(parents=True, exist_ok=True)
        self.events_path.unlink(missing_ok=True)
        self.stop_file.unlink(missing_ok=True)
        args = shlex.split(self.adapter.python) + [
            str(self.root / self.adapter.script),
            "--port", str(self.adapter.port),
            "--events", str(self.events_path),
            "--stop-file", str(self.stop_file),
            "--start-delay", str(self.adapter.start_delay_seconds),
            "--sse-chunks", str(self.adapter.sse_chunks),
            "--sse-delay", str(self.adapter.sse_delay_seconds),
        ]
        try:
            with self.log_path.open("ab") as log:
                self.process = subprocess.Popen(
                    args,
                    cwd=self.working_directory,
                    env={**os.environ, **self.adapter.env},
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except OSError as exc:
            raise DispatchError(f"fake backend could not start: {exc}", 502, "backend_start_failed") from exc
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise DispatchError(f"fake backend exited with {self.process.returncode}; see {self.log_path}", 502, "backend_start_failed")
                if self.healthy():
                    return
                time.sleep(0.05)
            raise DispatchError(f"fake backend did not become healthy within {timeout}s", 504, "backend_start_timeout")
        except Exception:
            try:
                self.stop()
            except Exception:
                pass
            raise

    def _ensure_managed(self, timeout: float):
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DispatchError(f"managed backend log path is unavailable: {exc}", 502, "backend_start_failed") from exc
        port = endpoint_port(self.adapter)
        if not _free(port):
            raise DispatchError(
                f"managed backend port {port} is already in use; refusing to treat an existing service as managed",
                409,
                "managed_port_in_use",
            )
        try:
            with self.log_path.open("ab") as log:
                self.process = subprocess.Popen(
                    list(self.adapter.command),
                    cwd=self.working_directory,
                    env={**os.environ, **self.adapter.env},
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except OSError as exc:
            raise DispatchError(f"managed backend could not start: {exc}", 502, "backend_start_failed") from exc
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise DispatchError(
                        f"managed backend exited with {self.process.returncode}; see {self.log_path}",
                        502,
                        "backend_start_failed",
                    )
                if self.healthy():
                    return
                time.sleep(0.05)
            raise DispatchError(f"managed backend did not become healthy within {timeout}s", 504, "backend_start_timeout")
        except Exception:
            try:
                self.stop()
            except Exception:
                pass
            raise

    def stop(self):
        if self.adapter.type in {"external", "observe", "http-managed"}:
            return
        if not self.process:
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.process.wait(timeout=self.adapter.stop_timeout_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=self.adapter.stop_timeout_seconds)
        self.process = None


@dataclass
class ModelRuntime:
    state: str = "unloaded"
    loading_reservation_gb: float | None = None
    last_request_finished: float = field(default_factory=time.monotonic)
    observed_state: str | None = None
    observed_at: float | None = None
    last_confirmed_state: str | None = None
    last_confirmed_at: float | None = None
    observed_memory_gb: float | None = None
    observed_memory_source: str | None = None
    observed_memory_at: float | None = None


def memory_pressure_level() -> int | None:
    """Return macOS memorystatus level when available; None means unknown."""
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["sysctl", "-n", "kern.memorystatus_level"],
            check=False,
            capture_output=True,
            text=True,
            timeout=0.2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def listener_rss_gb(port: int) -> float | None:
    """Return RSS for loopback listeners on macOS; None means unobservable."""
    if sys.platform != "darwin":
        return None
    try:
        listeners = subprocess.run(
            ["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            check=False, capture_output=True, text=True, timeout=0.5,
        )
        pids = sorted({pid for pid in listeners.stdout.split() if pid.isdigit()})
        if listeners.returncode != 0 or not pids:
            return None
        rss = subprocess.run(
            ["/bin/ps", "-o", "rss=", "-p", ",".join(pids)],
            check=False, capture_output=True, text=True, timeout=0.5,
        )
        if rss.returncode != 0:
            return None
        return sum(int(value) for value in rss.stdout.split()) / (1024 * 1024)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


class ModelDispatcher:
    def __init__(self, config: DispatchConfig, root: Path):
        self.config = config
        self.root = root
        self.condition = threading.Condition()
        self._transition_locks = self._build_transition_locks(config)
        self._transitions_active = 0
        self._memory_lock = threading.Lock()
        self._catalog_lock = threading.Lock()
        self._models_path_lock = threading.Lock()
        self._catalog_cache: dict[str, dict[str, Any]] = {}
        self._models_path_cache: dict[str, dict[str, Any]] = {}
        self._models = {name: ModelRuntime() for name in config.models}
        self._backends: dict[str, ProcessBackend] = {}
        self._legacy_backend: Any = None
        self._legacy_current_model: str | None = None
        self._legacy_active_requests = 0
        self.last_error: str | None = None
        self.recorder = RequestRecorder(config.metrics_path)
        # In-flight requests are tracked per request id so one client disconnect
        # can never cancel another client of the same model.
        self._requests: dict[str, dict[str, Any]] = {}
        self.settings_path = Path(os.environ.get("INFERENCEDOCK_SETTINGS_PATH", str(SETTINGS_PATH))).expanduser()
        self.settings = self._load_settings()
        # The idle loop reads the config copy, so a timeout persisted in the
        # settings file has to be merged before the thread starts. Without this
        # a menu-bar timeout leaves config.idle_unload_seconds at None and every
        # model is skipped as if automatic unload were switched off.
        persisted_idle = self.settings.get("idle_unload_seconds")
        if persisted_idle is not None:
            self.config = dataclass_replace(self.config, idle_unload_seconds=persisted_idle)
        self.last_request_finished = time.monotonic()
        self._idle_stop = threading.Event()
        self._idle_thread = None
        if self.settings.get("idle_unload_seconds") is not None:
            self._idle_thread = threading.Thread(target=self._idle_loop, name="model-dispatch-idle", daemon=True)
            self._idle_thread.start()

    @staticmethod
    def _build_transition_locks(config: DispatchConfig) -> dict[str, Any]:
        group_names = set(config.resource_groups)
        group_names.update(group for adapter in config.adapters.values() for group in adapter.exclusive_groups)
        group_names.update(group for model in config.models.values() for group in model.exclusive_groups)
        return {
            **{f"adapter:{name}": threading.Lock() for name in config.adapters},
            **{
                f"group:{name}": threading.BoundedSemaphore(
                    config.resource_groups[name].capacity if name in config.resource_groups else 1
                )
                for name in group_names
            },
        }

    def _load_settings(self) -> dict[str, Any]:
        defaults = {"smart_scheduling": False, "memory_limit_gb": None, "idle_unload_seconds": self.config.idle_unload_seconds, "adapter_policies": {}, "model_policies": {}}
        try:
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
            return self._validate_settings(payload, defaults)
        except (OSError, ValueError, ConfigError):
            return defaults

    def _validate_settings(self, payload: Any, base: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ConfigError("settings must be an object")
        allowed = {"smart_scheduling", "memory_limit_gb", "idle_unload_seconds", "adapter_policies", "model_policies", "adapter_policy"}
        unknown = set(payload) - allowed
        if unknown:
            raise ConfigError(f"unknown settings field(s): {', '.join(sorted(unknown))}")
        result = dict(base or {})
        if "smart_scheduling" in payload:
            if not isinstance(payload["smart_scheduling"], bool):
                raise ConfigError("smart_scheduling must be a boolean")
            result["smart_scheduling"] = payload["smart_scheduling"]
        for key in ("memory_limit_gb", "idle_unload_seconds"):
            if key not in payload:
                continue
            value = payload[key]
            if value is not None:
                if isinstance(value, bool):
                    raise ConfigError(f"{key} must be positive or null")
                try:
                    value = float(value)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ConfigError(f"{key} must be positive or null") from exc
                if not math.isfinite(value) or value <= 0:
                    raise ConfigError(f"{key} must be positive or null")
            result[key] = value
        if "adapter_policy" in payload:
            legacy = payload["adapter_policy"]
            if not isinstance(legacy, dict):
                raise ConfigError("adapter_policy must be an object")
            legacy = dict(legacy)
            if "exclusive_group" in legacy:
                legacy["exclusive_groups"] = [legacy.pop("exclusive_group")]
            payload = dict(payload)
            payload["adapter_policies"] = {name: legacy for name in self.config.adapters}
        for key, known in (("adapter_policies", self.config.adapters), ("model_policies", self.config.models)):
            if key not in payload:
                continue
            policies = payload[key]
            if not isinstance(policies, dict) or any(name not in known for name in policies):
                raise ConfigError(f"{key} must map known ids to policy objects")
            normalized_policies = {}
            for name, policy in policies.items():
                if not isinstance(policy, dict) or set(policy) - {"exclusive_groups", "keep_resident", "estimated_memory_gb"}:
                    raise ConfigError(f"{key}.{name} contains unsupported fields")
                normalized = {}
                if "exclusive_groups" in policy:
                    normalized["exclusive_groups"] = list(_group_list(policy["exclusive_groups"], f"{key}.{name}.exclusive_groups"))
                    if not set(normalized["exclusive_groups"]).issubset(set(self.config.resource_groups)):
                        raise ConfigError(f"{key}.{name}.exclusive_groups must reference configured resource groups")
                if "keep_resident" in policy:
                    normalized["keep_resident"] = _bool(policy["keep_resident"], f"{key}.{name}.keep_resident")
                if "estimated_memory_gb" in policy:
                    if isinstance(policy["estimated_memory_gb"], bool):
                        raise ConfigError(f"{key}.{name}.estimated_memory_gb must be positive")
                    try:
                        value = float(policy["estimated_memory_gb"])
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise ConfigError(f"{key}.{name}.estimated_memory_gb must be positive") from exc
                    if not math.isfinite(value) or value <= 0:
                        raise ConfigError(f"{key}.{name}.estimated_memory_gb must be positive")
                    normalized["estimated_memory_gb"] = value
                normalized_policies[name] = normalized
            result[key] = normalized_policies
        self._validate_policy_ports(result.get("adapter_policies", {}))
        return result

    def _validate_policy_ports(self, policies: dict[str, Any]):
        ports: dict[int, list[AdapterConfig]] = {}
        for adapter in self.config.adapters.values():
            if adapter.port is not None or adapter.endpoint is not None:
                ports.setdefault(endpoint_port(adapter), []).append(adapter)
        for port, owners in ports.items():
            if len(owners) < 2 or all(a.type in {"external", "observe", "http-managed"} for a in owners):
                continue
            groups = []
            for adapter in owners:
                policy = policies.get(adapter.name, {})
                groups.append(set(policy.get("exclusive_groups", adapter.exclusive_groups)))
            if any(a.type != "managed" for a in owners) or not (set.intersection(*groups) & {name for name, cfg in self.config.resource_groups.items() if cfg.capacity == 1}):
                raise ConfigError(f"adapter port {port} policy removes its shared exclusive group")

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.condition:
            updated = self._validate_settings(payload, self.settings)
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.settings_path.parent, delete=False) as handle:
                    json.dump(updated, handle, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                    temp_path = Path(handle.name)
                os.replace(temp_path, self.settings_path)
            except OSError:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
                raise
            self.settings = updated
            self.config = dataclass_replace(self.config, idle_unload_seconds=updated.get("idle_unload_seconds"))
            if updated.get("idle_unload_seconds") is not None and self._idle_thread is None:
                self._idle_thread = threading.Thread(target=self._idle_loop, name="model-dispatch-idle", daemon=True)
                self._idle_thread.start()
            self.condition.notify_all()
            return updated

    def _idle_loop(self):
        while not self._idle_stop.is_set():
            due = []
            with self.condition:
                candidates = [
                    self.config.models[name]
                    for name, runtime in self._models.items()
                    if runtime.state == "ready" and self._request_count(name) == 0
                ]
                if not candidates:
                    self.condition.wait(timeout=self.config.request_poll_seconds)
                    continue
                next_wait = self.config.request_poll_seconds
                for model in candidates:
                    adapter = self.config.adapters[model.adapter]
                    if adapter.type == "observe" or (adapter.type in {"external", "http-managed"} and not model.deactivate_path) or self._keep_resident(model) or self.config.idle_unload_seconds is None:
                        continue
                    runtime = self._models[model.name]
                    remaining = self.config.idle_unload_seconds - (time.monotonic() - runtime.last_request_finished)
                    if remaining > 0:
                        next_wait = min(next_wait, remaining)
                        continue
                    due.append(model.name)
            for model_name in due:
                try:
                    self.unload(model_name)
                except DispatchError as exc:
                    with self.condition:
                        self.last_error = str(exc)
                        self._models[model_name].last_request_finished = time.monotonic()
            self._idle_stop.wait(max(0.01, next_wait))

    def resolve_model_name(self, model_name: str) -> str:
        return self.config.model_aliases.get(model_name, model_name)

    def reload_config(self) -> dict[str, Any]:
        """Re-read the current config file; reject destructive changes while busy.

        The running listener, in-flight requests and managed processes never
        restart here. Removal or re-typing of a busy model's adapter is refused
        with 409 so an edit can never orphan live state.
        """
        with self.condition:
            if self._busy() or self.active_requests or self._transitions_active:
                raise DispatchError("cannot reload configuration while the dispatcher is busy", 409, "dispatcher_busy")
            previous = self.config
            try:
                new_config = load_config(previous.path)
            except ConfigError as exc:
                raise DispatchError(f"config reload failed: {exc}", 400, "invalid_config") from exc
            if (new_config.listen_host, new_config.listen_port) != (previous.listen_host, previous.listen_port):
                raise DispatchError("listen_host/listen_port cannot change via reload; restart the core instead", 409, "reload_requires_restart")
            for model_name, runtime in self._models.items():
                if runtime.state not in {"loading", "ready"}:
                    continue
                if model_name not in new_config.models:
                    raise DispatchError(f"config reload cannot remove loaded model {model_name!r}", 409, "reload_removes_loaded_model")
                old_adapter = previous.models[model_name].adapter
                new_adapter = new_config.models[model_name].adapter
                if old_adapter != new_adapter:
                    raise DispatchError(f"config reload cannot move loaded model {model_name!r} to adapter {new_adapter!r}", 409, "reload_removes_loaded_model")
            for adapter_name, backend in self._backends.items():
                if adapter_name not in new_config.adapters:
                    raise DispatchError(f"config reload cannot remove adapter {adapter_name!r} while it owns a process", 409, "reload_removes_loaded_model")
                if new_config.adapters[adapter_name] != previous.adapters[adapter_name]:
                    raise DispatchError(f"config reload cannot change adapter {adapter_name!r} while it owns a process", 409, "reload_removes_loaded_model")
            # Any settings file value (null disables, a number sets it) keeps
            # winning after a reload; without a settings file the fresh config
            # value applies.
            idle = self.settings.get("idle_unload_seconds") if self.settings_path.exists() else new_config.idle_unload_seconds
            self.config = dataclass_replace(new_config, idle_unload_seconds=idle)
            with self._catalog_lock:
                self._catalog_cache.clear()
            with self._models_path_lock:
                self._models_path_cache.clear()
            self._transition_locks = self._build_transition_locks(self.config)
            self._models = {
                name: self._models.get(name, ModelRuntime())
                for name in new_config.models
            }
            self.condition.notify_all()
            return {
                "reloaded": True,
                "config_path": str(new_config.path),
                "models_added": sorted(set(new_config.models) - set(previous.models)),
                "models_removed": sorted(set(previous.models) - set(new_config.models)),
                "adapters_added": sorted(set(new_config.adapters) - set(previous.adapters)),
                "adapters_removed": sorted(set(previous.adapters) - set(new_config.adapters)),
            }

    def config_report(self) -> dict[str, Any]:
        config = self.config
        return {
            "path": str(config.path),
            "models_configured": len(config.models),
            "models_visible": sum(1 for model in config.models.values() if model.advertise and model.enabled),
            "aliases": len(config.model_aliases),
            "adapters": [
                {
                    "name": adapter.name,
                    "type": adapter.type,
                    "plugin_id": adapter.plugin_id,
                    "service_key": adapter.service_key,
                    "service_version": adapter.service_version,
                    "endpoint": endpoint_for(adapter) if adapter.endpoint or adapter.port is not None else None,
                }
                for adapter in config.adapters.values()
            ],
        }

    def _transition_keys(self, model: ModelConfig) -> set[str]:
        keys = {f"adapter:{model.adapter}"}
        keys.update(f"group:{name}" for name in self._effective_groups(model))
        return keys

    def _acquire_transitions(self, models: list[ModelConfig], cancel_event=None, deadline=None):
        acquired = []
        with self.condition:
            self._transitions_active += 1
        try:
            locks = [self._transition_locks[key] for key in sorted(set().union(*(self._transition_keys(model) for model in models)))]
            for lock in locks:
                while not lock.acquire(timeout=self.config.request_poll_seconds):
                    if cancel_event is not None and cancel_event.is_set():
                        raise DispatchError("request cancelled while waiting for the model", 409, "request_cancelled")
                    if deadline is not None and time.monotonic() >= deadline:
                        raise DispatchError("timed out waiting for a model transition", 409, "model_busy")
                acquired.append(lock)
            return acquired
        except Exception:
            for lock in reversed(acquired):
                lock.release()
            with self.condition:
                self._transitions_active -= 1
                self.condition.notify_all()
            raise

    def _release_transitions(self, locks):
        for lock in reversed(locks):
            lock.release()
        with self.condition:
            self._transitions_active -= 1
            self.condition.notify_all()

    def _refresh_observed_states(self):
        """Drop stale ready state only when the owned process is gone or the
        backend explicitly reports that the model is not resident.  A failed
        probe is surfaced as ``unknown`` and blocks eviction until it recovers."""
        with self.condition:
            observed = [
                model for model in self.config.models.values()
                if self._models[model.name].state in {"ready", "unknown"}
            ]
        for model in observed:
            adapter = self.config.adapters[model.adapter]
            backend = self._backends.get(adapter.name)
            process = getattr(backend, "process", None)
            gone = process is not None and getattr(process, "poll", lambda: None)() is not None
            reported = None
            probe_error = None
            if not gone and adapter.model_state_path:
                try:
                    reported = self._reported_model_loaded(adapter, model)
                except DispatchError as exc:
                    probe_error = exc
            now = time.time()
            if not gone and probe_error is not None:
                with self.condition:
                    runtime = self._models[model.name]
                    if runtime.state in {"ready", "unknown"}:
                        runtime.state = "unknown"
                        runtime.observed_state = "unknown"
                        runtime.observed_at = now
                        self.condition.notify_all()
                continue
            with self.condition:
                runtime = self._models[model.name]
                runtime.observed_at = now
                if gone:
                    runtime.observed_state = "unloaded"
                    runtime.last_confirmed_state = "unloaded"
                    runtime.last_confirmed_at = now
                elif reported is True:
                    runtime.observed_state = "ready"
                    runtime.last_confirmed_state = "ready"
                    runtime.last_confirmed_at = now
                elif reported is False:
                    runtime.observed_state = "unloaded"
                    runtime.last_confirmed_state = "unloaded"
                    runtime.last_confirmed_at = now
                if (gone or reported is False) and runtime.state in {"ready", "unknown"} and self._request_count(model.name) == 0:
                    runtime.state = "unloaded"
                    runtime.loading_reservation_gb = None
                    if self._legacy_current_model == model.name:
                        self._legacy_current_model = None
                    self.condition.notify_all()
                elif (gone or reported is False) and self._request_count(model.name) > 0:
                    runtime.state = "unknown"
                    runtime.observed_state = "unknown"
                    self.condition.notify_all()
                elif reported is True and runtime.state == "unknown":
                    runtime.state = "ready"
                    self.condition.notify_all()

    def _run_local_catalog(self, adapter: AdapterConfig) -> dict[str, Any]:
        """Run an explicitly configured, read-only catalog argv.

        The command is always assembled as argv and never passed through a
        shell. A failed probe is kept as ``unknown`` so a transient CLI error
        cannot hide a previously configured model.
        """
        if not adapter.catalog_args or not adapter.command:
            return {"status": "unconfigured", "models": set()}
        try:
            result = subprocess.run(
                [adapter.command[0], *adapter.catalog_args],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.config.connect_timeout_seconds,
                cwd=adapter.working_directory,
                env={**os.environ, **adapter.env},
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            return {"status": "unknown", "error": "catalog_command_failed", "models": set()}
        if result.returncode != 0:
            return {"status": "unknown", "error": "catalog_command_failed", "models": set()}
        try:
            return {"status": "ok", "models": parse_local_catalog(result.stdout)}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"status": "unknown", "error": "catalog_response_invalid", "models": set()}

    def _refresh_local_catalogs(self, *, force: bool = False):
        """Refresh configured local catalogs with a short, shared TTL."""
        with self.condition:
            adapters = [adapter for adapter in self.config.adapters.values() if adapter.catalog_args]
        if not adapters:
            return
        now = time.monotonic()
        groups: dict[tuple[Any, ...], list[AdapterConfig]] = {}
        for adapter in adapters:
            key = (
                adapter.command[0] if adapter.command else None,
                adapter.catalog_args,
                tuple(sorted(adapter.env.items())),
                str(adapter.working_directory) if adapter.working_directory else None,
            )
            groups.setdefault(key, []).append(adapter)
        # ponytail: one global probe lock keeps the five MTPLX aliases from
        # spawning duplicate catalog processes; split locks only if this shows
        # up in status latency.
        with self._catalog_lock:
            pending = []
            for group in groups.values():
                cached = [self._catalog_cache.get(adapter.name) for adapter in group]
                if not force and cached and all(
                    item is not None and now - item.get("monotonic", 0) < LOCAL_CATALOG_TTL_SECONDS
                    for item in cached
                ):
                    continue
                pending.append(group)
            for group in pending:
                result = self._run_local_catalog(group[0])
                checked = {**result, "checked_at": time.time(), "monotonic": time.monotonic()}
                for adapter in group:
                    self._catalog_cache[adapter.name] = checked

    def _catalog_snapshot(self) -> dict[str, dict[str, Any]]:
        with self._catalog_lock:
            return {name: dict(value) for name, value in self._catalog_cache.items()}

    @staticmethod
    def _model_availability(
        model: ModelConfig,
        adapters: dict[str, AdapterConfig],
        catalogs: dict[str, dict[str, Any]],
        models_paths: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        models_paths = models_paths or {}
        adapter = adapters[model.adapter]
        sources: list[dict[str, Any]] = []
        catalog = catalogs.get(adapter.name)
        catalog_model = model.catalog_id or (local_catalog_reference(adapter, model.backend_model) if adapter.catalog_args else None)
        catalog_checked_at = catalog.get("checked_at") if catalog else None
        if adapter.catalog_args:
            status = "unknown"
            if catalog and catalog.get("status") == "ok":
                status = "present" if (model.catalog_id or catalog_model) in catalog.get("models", set()) else "missing"
            sources.append({"source": "catalog_args", "status": status})
        models_path = models_paths.get(adapter.name)
        models_checked_at = models_path.get("checked_at") if models_path else None
        if adapter.models_path:
            status = "unknown"
            if models_path and models_path.get("status") == "ok":
                target = model.backend_model
                status = "present" if target in models_path.get("models", set()) else "missing"
            sources.append({"source": "models_path", "status": status})
        asset_checked_at = None
        if model.asset_paths:
            status = "present"
            for path in model.asset_paths:
                try:
                    if not path.is_file():
                        status = "missing"
                        break
                except OSError:
                    status = "unknown"
                    break
            asset_checked_at = time.time()
            sources.append({"source": "asset_paths", "status": status})
        discovery_statuses = [source["status"] for source in sources if source["source"] != "asset_paths"]
        asset_status = next((source["status"] for source in sources if source["source"] == "asset_paths"), None)
        if asset_status == "missing":
            available, aggregate = False, "missing"
        elif asset_status == "unknown":
            available, aggregate = None, "unknown"
        elif not discovery_statuses:
            available, aggregate = True, "not_configured"
        elif "missing" in discovery_statuses and "present" in discovery_statuses:
            available, aggregate = None, "conflict"
        elif "missing" in discovery_statuses:
            available, aggregate = False, "missing"
        elif "unknown" in discovery_statuses:
            available, aggregate = None, "unknown"
        else:
            available, aggregate = True, "present"
        catalog_status = "not_configured"
        if adapter.catalog_args:
            source_status = sources[0]["status"]
            catalog_status = "available" if source_status == "present" else source_status
        checked_values = [value for value in (catalog_checked_at, models_checked_at, asset_checked_at) if value is not None]
        return {
            "available": available,
            "status": aggregate,
            "availability_status": aggregate,
            "availability_checked_at": max(checked_values) if checked_values else None,
            "availability_sources": sources,
            "catalog_status": catalog_status,
            "catalog_model": catalog_model,
            "catalog_checked_at": catalog_checked_at,
            "checked_at": catalog_checked_at,
        }

    def _refresh_models_paths(self, *, force: bool = False):
        """Probe only healthy, already-running HTTP services; never start one."""
        with self.condition:
            adapters = [adapter for adapter in self.config.adapters.values() if adapter.models_path]
        if not adapters:
            return
        with self._models_path_lock:
            now = time.monotonic()
            pending = [
                adapter for adapter in adapters
                if force
                or self._models_path_cache.get(adapter.name) is None
                or now - self._models_path_cache[adapter.name].get("monotonic", 0) >= LOCAL_CATALOG_TTL_SECONDS
            ]
            for adapter in pending:
                result: dict[str, Any] = {"status": "unknown", "models": set(), "checked_at": time.time()}
                try:
                    if not ProcessBackend(adapter, self.root).healthy():
                        result["error"] = "service_unreachable"
                    else:
                        status, payload = http_json(
                            endpoint_for(adapter) + adapter.models_path,
                            timeout=self.config.connect_timeout_seconds,
                            headers=backend_headers(adapter),
                        )
                        if status == 200:
                            result["models"] = parse_model_list(payload)
                            result["status"] = "ok"
                        else:
                            result["error"] = "models_probe_failed"
                except (ConfigError, DispatchError, OSError, TypeError, ValueError, json.JSONDecodeError):
                    result["error"] = "models_response_invalid"
                result["monotonic"] = time.monotonic()
                self._models_path_cache[adapter.name] = result

    def _models_path_snapshot(self) -> dict[str, dict[str, Any]]:
        with self._models_path_lock:
            return {name: dict(value) for name, value in self._models_path_cache.items()}

    def _ensure_catalog_available(self, model: ModelConfig, runtime: ModelRuntime):
        self._refresh_local_catalogs()
        self._refresh_models_paths()
        availability = self._model_availability(model, self.config.adapters, self._catalog_snapshot(), self._models_path_snapshot())
        if availability["available"] is False and runtime.state != "ready":
            raise DispatchError(
                f"model {model.name!r} is unavailable",
                409,
                "model_unavailable",
            )

    def model_entries(self, include_hidden: bool = False, refresh: bool = True) -> list[dict[str, Any]]:
        if refresh:
            self._refresh_observed_states()
            self._refresh_local_catalogs()
            self._refresh_models_paths()
        catalogs = self._catalog_snapshot()
        models_paths = self._models_path_snapshot()
        with self.condition:
            states = {name: runtime.state for name, runtime in self._models.items()}
            request_counts = {name: self._request_count(name) for name in self.config.models}
            server_states = {name: self._service_state(adapter) for name, adapter in self.config.adapters.items()}
        entries = []
        for model in self.config.models.values():
            availability = self._model_availability(model, self.config.adapters, catalogs, models_paths)
            if not include_hidden and not (model.advertise and model.enabled and availability["available"] is not False):
                continue
            entries.append({
                **adapter_metadata(self.config.adapters[model.adapter], status=server_states[model.adapter]),
                "id": model.name,
                "object": "model",
                "created": 0,
                "owned_by": "model-dispatch",
                "backend_model": model.backend_model,
                "adapter": model.adapter,
                "resource_group": model.resource_group,
                "exclusive_group": model.exclusive_group,
                "exclusive_groups": sorted(self._effective_groups(model)),
                "keep_resident": self._keep_resident(model),
                "estimated_memory_gb": self._effective_estimate(model),
                "configured_memory_gb": self._effective_estimate(model),
                "observed_memory_gb": self._models[model.name].observed_memory_gb,
                "observed_memory_source": self._models[model.name].observed_memory_source,
                "observed_memory_at": self._models[model.name].observed_memory_at,
                "capabilities": model.capabilities,
                "display_name": model.display_name or model.name,
                "context_window": model.context_window,
                "max_output_tokens": model.max_output_tokens,
                "reasoning_levels": list(model.reasoning_levels),
                "runtime_summary": list(model.runtime_summary),
                "state": states[model.name],
                "observed_state": self._models[model.name].observed_state,
                "observed_at": self._models[model.name].observed_at,
                "last_confirmed_state": self._models[model.name].last_confirmed_state,
                "last_confirmed_at": self._models[model.name].last_confirmed_at,
                "advertise": model.advertise,
                "enabled": model.enabled,
                "canonical": model.canonical,
                "aliases": list(model.aliases),
                "plugin_id": self.config.adapters[model.adapter].plugin_id,
                "service_key": self.config.adapters[model.adapter].service_key,
                "service_version": self.config.adapters[model.adapter].service_version,
                "server_id": model.adapter,
                "server_type": self.config.adapters[model.adapter].type,
                "server_name": self.config.adapters[model.adapter].display_name or model.adapter,
                "menu_group": self.config.adapters[model.adapter].menu_group,
                "service_variant": model.service_variant,
                "loaded": states[model.name] == "ready",
                "active_requests": request_counts[model.name],
                "server_status": server_states[model.adapter],
                "available": availability["available"],
                "catalog_status": availability["catalog_status"],
                "catalog_model": availability["catalog_model"],
                "catalog_checked_at": availability["catalog_checked_at"],
                "availability_status": availability["availability_status"],
                "availability_checked_at": availability["availability_checked_at"],
                "availability_sources": availability["availability_sources"],
            })
        return entries

    def status(self) -> dict[str, Any]:
        self._refresh_observed_states()
        self._refresh_local_catalogs()
        self._refresh_models_paths()
        with self.condition:
            in_flight = [
                {
                    "request_id": meta["request_id"],
                    "model": meta["model"],
                    "stream": bool(meta["metrics"].get("stream")),
                    "started_ts": meta["started_wall"],
                    "elapsed_ms": round((time.monotonic() - meta["started"]) * 1000, 1),
                    "response_bytes": meta["metrics"].get("response_bytes", 0),
                }
                for meta in sorted(self._requests.values(), key=lambda m: m["started"])
            ]
            active_models = sorted(name for name, runtime in self._models.items() if runtime.state == "ready")
            return {
                "state": self.state,
                "active_model": self.current_model,
                "active_models": active_models,
                "active_requests": self.active_requests,
                "last_error": self.last_error,
                "latest_request": self.recorder.latest(),
                "requests": in_flight,
                "models": self.model_entries(include_hidden=True, refresh=False),
                "resource_groups": {
                    name: {
                        "capacity": group.capacity,
                        "used": sum(
                            1
                            for model in self.config.models.values()
                            if name in self._effective_groups(model) and self._models[model.name].state in {"loading", "ready"}
                        ),
                    }
                    for name, group in self.config.resource_groups.items()
                },
            }

    def reconcile_models(self) -> dict[str, Any]:
        with self.condition:
            if self.active_requests or self._busy():
                raise DispatchError("cannot refresh models while the dispatcher is busy", 409, "dispatcher_busy")
        self._refresh_local_catalogs(force=True)
        self._refresh_models_paths(force=True)
        catalogs = self._catalog_snapshot()
        models_paths = self._models_path_snapshot()
        results = []
        for adapter in self.config.adapters.values():
            configured_models = [m for m in self.config.models.values() if m.adapter == adapter.name]
            if not adapter.models_path and not adapter.catalog_args and not any(m.asset_paths for m in configured_models):
                continue
            configured_entries = [
                {"id": m.backend_model, "model": m.name, "enabled": m.enabled, "advertise": m.advertise}
                for m in configured_models
            ]
            configured_entries = sorted(configured_entries, key=lambda entry: entry["id"])
            configured = [entry["id"] for entry in configured_entries]
            result = {"adapter": adapter.name, "plugin_id": adapter.plugin_id, "status": "ok", "configured": configured_entries}
            discovered: set[str] = set()
            source_names = []
            if adapter.catalog_args:
                source_names.append("local-catalog")
                catalog = catalogs.get(adapter.name)
                if catalog and catalog.get("status") == "ok":
                    discovered.update(catalog.get("models", set()))
                else:
                    result.update({"status": "error", "error": (catalog or {}).get("error", "catalog_command_failed")})
            if adapter.models_path:
                source_names.append("service-discovered")
                probe = models_paths.get(adapter.name)
                if probe and probe.get("status") == "ok":
                    discovered.update(probe.get("models", set()))
                elif result["status"] == "ok":
                    result.update({"status": "unreachable" if (probe or {}).get("error") == "service_unreachable" else "invalid_response", "error": (probe or {}).get("error")})
            if source_names:
                result["source"] = source_names[0]
            result["discovered"] = sorted(discovered)
            targets = {m.catalog_id or (local_catalog_reference(adapter, m.backend_model) if adapter.catalog_args else m.backend_model) for m in configured_models}
            result["new_candidates"] = [
                {"id": candidate, "source": source_names[0] if source_names else "asset-paths"}
                for candidate in sorted(discovered - targets)
            ]
            missing_models = [
                model for model in configured_models
                if self._model_availability(model, self.config.adapters, catalogs, models_paths)["available"] is False
            ]
            result["missing_candidates"] = [
                {"id": model.backend_model, "model": model.name, "enabled": model.enabled, "advertise": model.advertise}
                for model in sorted(missing_models, key=lambda item: item.backend_model)
            ]
            result["availability"] = [
                {"model": model.name, "status": self._model_availability(model, self.config.adapters, catalogs, models_paths)["availability_status"]}
                for model in sorted(configured_models, key=lambda item: item.name)
            ]
            results.append(result)
        diagnostics = self._identity_diagnostics(results)
        return {"results": results, "diagnostics": diagnostics, "mutated": False}

    def _identity_diagnostics(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Read-only duplicate/drift signals: same backend id twice, conflicting
        capability claims, candidates colliding with another adapter's model,
        and hidden or disabled entries that still own a backend id."""
        diagnostics: list[dict[str, Any]] = []
        by_identity: dict[tuple[str, str], list[ModelConfig]] = {}
        for model in self.config.models.values():
            by_identity.setdefault((model.adapter, model.backend_model), []).append(model)
        for (adapter_name, backend_model), entries in sorted(by_identity.items()):
            if len(entries) < 2:
                continue
            names = sorted(model.name for model in entries)
            diagnostics.append({"type": "duplicate_backend_identity", "adapter": adapter_name, "backend_model": backend_model, "models": names})
            visions = {model.capabilities.get("vision", False) for model in entries}
            if len(visions) > 1:
                diagnostics.append({"type": "conflicting_capability_claims", "adapter": adapter_name, "backend_model": backend_model, "field": "vision", "models": names})
        for alias, target in sorted(self.config.model_aliases.items()):
            canonical = self.config.models[target]
            if not canonical.enabled or not canonical.advertise:
                diagnostics.append({"type": "alias_targets_unlisted_model", "alias": alias, "model": target})
        configured_elsewhere: dict[str, str] = {}
        for model in self.config.models.values():
            configured_elsewhere.setdefault(model.backend_model, model.adapter)
        for item in results:
            for candidate in item.get("new_candidates", []):
                other = configured_elsewhere.get(candidate["id"])
                if other is not None and other != item["adapter"]:
                    diagnostics.append({"type": "candidate_shadows_other_adapter", "adapter": item["adapter"], "backend_model": candidate["id"], "configured_adapter": other})
        return diagnostics

    @property
    def active_requests(self) -> int:
        return len(self._requests) + self._legacy_active_requests

    @active_requests.setter
    def active_requests(self, value: int):
        self._legacy_active_requests = max(0, int(value))

    @property
    def current_model(self) -> str | None:
        ready = [name for name, runtime in self._models.items() if runtime.state == "ready"]
        if len(ready) == 1:
            return ready[0]
        if self._legacy_current_model in ready:
            return self._legacy_current_model
        return None

    @current_model.setter
    def current_model(self, value: str | None):
        self._legacy_current_model = value

    @property
    def state(self) -> str:
        states = [runtime.state for runtime in self._models.values()]
        for state in ("unloading", "loading", "failed", "unknown"):
            if state in states:
                return state
        return "ready" if "ready" in states else "unloaded"

    @state.setter
    def state(self, value: str):
        if self._legacy_current_model in self._models:
            self._models[self._legacy_current_model].state = value

    @property
    def backend(self):
        model_name = self._legacy_current_model or self.current_model
        if model_name in self.config.models:
            adapter = self.config.models[model_name].adapter
            return self._backends.get(adapter, self._legacy_backend)
        if len(self._backends) == 1:
            return next(iter(self._backends.values()))
        return self._legacy_backend

    @backend.setter
    def backend(self, value):
        self._legacy_backend = value
        if self._legacy_current_model in self.config.models:
            adapter = self.config.models[self._legacy_current_model].adapter
            if value is None:
                self._backends.pop(adapter, None)
            else:
                self._backends[adapter] = value

    def _request_count(self, model_name: str | None = None) -> int:
        return sum(
            1
            for meta in self._requests.values()
            if model_name is None or meta.get("model") == model_name
        ) + (self._legacy_active_requests if model_name is None else 0)

    def _busy(self) -> bool:
        return any(runtime.state in {"loading", "unloading"} for runtime in self._models.values())

    def _service_state(self, adapter: AdapterConfig) -> str:
        states = [
            runtime.state
            for model in self.config.models.values()
            if model.adapter == adapter.name
            for runtime in [self._models[model.name]]
        ]
        for state in ("unloading", "loading", "failed", "unknown", "ready"):
            if state in states:
                return state
        if adapter.type in {"external", "observe", "http-managed"}:
            return "unknown"
        backend = self._backends.get(adapter.name)
        process = getattr(backend, "process", None)
        if process is not None and getattr(process, "poll", lambda: None)() is None:
            return "ready"
        return "stopped"

    def _unload_service(self, adapter: AdapterConfig, keep_service_running: bool = False, excluding: str | None = None):
        if keep_service_running or adapter.type in {"external", "observe", "http-managed"}:
            return
        if any(
            model.adapter == adapter.name and model.name != excluding and self._models[model.name].state in {"loading", "ready", "unloading"}
            for model in self.config.models.values()
        ):
            return
        backend = self._backends.get(adapter.name)
        if backend is None and self._legacy_backend is not None:
            legacy_adapter = getattr(self._legacy_backend, "adapter", adapter)
            if getattr(legacy_adapter, "name", adapter.name) == adapter.name:
                backend = self._legacy_backend
        if backend:
            backend.stop()

    def _backend_for(self, model: ModelConfig):
        adapter = self.config.adapters[model.adapter]
        backend = self._backends.get(adapter.name)
        if backend is None and self._legacy_backend is not None:
            legacy_adapter = getattr(self._legacy_backend, "adapter", adapter)
            if getattr(legacy_adapter, "name", adapter.name) == adapter.name:
                return self._legacy_backend
        if backend is None:
            backend = ProcessBackend(adapter, self.root)
            self._backends[adapter.name] = backend
            self._legacy_backend = backend
        return backend

    @staticmethod
    def _exclusive_groups(model: ModelConfig) -> set[str]:
        return set(model.exclusive_groups)

    def _native_route_switch_safe(self, current: ModelConfig, target: ModelConfig) -> bool:
        """Same-adapter native model switch: the shared service stays up and the
        target activates through the service's own route, so the current model
        is not a capacity blocker. Managed process-as-model runtimes embed the
        model in the process, and a current model without a deactivation route
        cannot be unloaded, so both still count as blockers."""
        return bool(target.activate_path and current.deactivate_path)

    def _external_activity(self, model: ModelConfig) -> bool:
        """Probe an external adapter's own activity endpoint. A configured probe
        that fails counts as busy: we never evict a model on an unknown state."""
        adapter = self.config.adapters[model.adapter]
        backend = self._backends.get(adapter.name)
        unowned_managed = bool(
            adapter.type == "managed"
            and backend is not None
            and getattr(backend, "process", None) is None
            and hasattr(backend, "healthy")
            and backend.healthy()
        )
        # A managed process owned by this dispatcher is normally safe to stop
        # without a probe. If an activity probe is explicitly configured,
        # direct backend requests can still be in flight and must block a
        # switch/unload just like external traffic.
        if adapter.type == "managed" and not adapter.active_requests_path and not unowned_managed:
            return False
        if adapter.type not in {"external", "http-managed", "managed"} and not unowned_managed:
            return False
        if not adapter.active_requests_path:
            with self.condition:
                self.last_error = f"adapter {adapter.name!r} has no activity probe; assuming busy"
            return True
        status, payload = http_json(endpoint_for(adapter) + adapter.active_requests_path, timeout=self.config.connect_timeout_seconds, headers=backend_headers(adapter))
        if status != 200 or not isinstance(payload, dict):
            with self.condition:
                self.last_error = f"activity probe for adapter {adapter.name!r} failed (HTTP {status}); assuming busy"
            return True
        value = payload.get("active_requests")
        gauges = payload.get("gauges")
        if value is None and isinstance(gauges, dict):
            running = gauges.get("requests_running")
            waiting = gauges.get("requests_waiting", 0)
            if all(not isinstance(item, bool) and isinstance(item, (int, float)) and item >= 0 for item in (running, waiting)):
                value = running + waiting
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            with self.condition:
                self.last_error = f"activity probe for adapter {adapter.name!r} returned an invalid active_requests; assuming busy"
            return True
        return value > 0

    def _reported_model_loaded(self, adapter: AdapterConfig, model: ModelConfig) -> bool | None:
        if not adapter.model_state_path:
            return None
        status, payload = http_json(endpoint_for(adapter) + adapter.model_state_path, timeout=self.config.connect_timeout_seconds, headers=backend_headers(adapter))
        if status != 200 or not isinstance(payload, dict):
            raise DispatchError(f"state of {model.name!r} could not be verified (HTTP {status})", 502, "model_state_unverified")
        candidates = []
        for key in ("loaded_models", "models", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                for item in value:
                    if not isinstance(item, dict):
                        candidates.append(item)
                        continue
                    if item.get("id") != model.backend_model:
                        continue
                    if isinstance(item.get("loaded"), bool):
                        return item["loaded"]
                    state = item.get("state")
                    if isinstance(state, str):
                        return state.lower() in {"ready", "loaded", "loading", "generating", "busy"}
                    candidates.append(item.get("id"))
                break
        else:
            loaded = payload.get("loaded")
            if isinstance(loaded, bool):
                return loaded
            raise DispatchError(f"state of {model.name!r} could not be verified: no model list in probe", 502, "model_state_unverified")
        return model.backend_model in candidates

    def _ensure_activated(self, adapter: AdapterConfig, model: ModelConfig):
        if self._reported_model_loaded(adapter, model) is False:
            raise DispatchError(f"activation of {model.name!r} could not be verified", 502, "model_activation_unverified")

    def _ensure_deactivated(self, adapter: AdapterConfig, model: ModelConfig):
        if adapter.type != "observe" and self._reported_model_loaded(adapter, model) is True:
            raise DispatchError(f"deactivation of {model.name!r} could not be verified: still listed by the backend", 502, "model_deactivation_unverified")

    def _keep_resident(self, model: ModelConfig) -> bool:
        mp = (self.settings.get("model_policies") or {}).get(model.name, {})
        ap = (self.settings.get("adapter_policies") or {}).get(model.adapter, {})
        return mp.get("keep_resident", ap.get("keep_resident", model.keep_resident))

    def _effective_groups(self, model: ModelConfig) -> set[str]:
        mp = (self.settings.get("model_policies") or {}).get(model.name, {})
        ap = (self.settings.get("adapter_policies") or {}).get(model.adapter, {})
        return set(mp.get("exclusive_groups", ap.get("exclusive_groups", model.exclusive_groups)))

    def _effective_estimate(self, model: ModelConfig) -> float | None:
        return (self.settings.get("model_policies") or {}).get(model.name, {}).get("estimated_memory_gb", model.estimated_memory_gb)

    def _record_observed_memory(self, model: ModelConfig):
        """Capture one post-load listener RSS sample for audit only.

        A missing or platform-unavailable sample stays unknown.  This evidence
        is never copied into the configured estimate automatically.
        """
        adapter = self.config.adapters[model.adapter]
        try:
            observed = listener_rss_gb(endpoint_port(adapter))
        except (ConfigError, OSError, ValueError):
            observed = None
        if observed is None or observed <= 0:
            return
        with self.condition:
            runtime = self._models[model.name]
            runtime.observed_memory_gb = round(float(observed), 3)
            runtime.observed_memory_source = "listener_rss_post_load"
            runtime.observed_memory_at = time.time()
            self.condition.notify_all()

    def _external_memory_gb(self, exclude_adapters: set[str] | None = None) -> float:
        excluded = exclude_adapters or set()
        total = 0.0
        for adapter in self.config.adapters.values():
            if adapter.type not in {"external", "observe", "http-managed"} or adapter.name in excluded:
                continue
            if not ProcessBackend(adapter, self.root).healthy():
                continue
            adapter_models = [model for model in self.config.models.values() if model.adapter == adapter.name]
            estimates = [
                estimate
                for model in adapter_models
                for estimate in [self._effective_estimate(model)]
                if estimate is not None
            ]
            observed = listener_rss_gb(endpoint_port(adapter))
            if observed is not None:
                total += max([observed, *estimates])
                continue
            if not adapter_models:
                raise DispatchError(
                    f"external adapter {adapter.name!r} is healthy but its memory use is unobservable",
                    409,
                    "memory_status_unknown",
                )
            if not estimates:
                raise DispatchError(
                    f"external adapter {adapter.name!r} has no estimated_memory_gb; refusing memory-limited admission",
                    409,
                    "memory_estimate_required",
                )
            total += max(estimates)
        return total

    def _check_memory(self, model: ModelConfig, exclude_models: set[str] | None = None):
        excluded = exclude_models or set()
        runtime = self._models[model.name]
        if runtime.state == "ready":
            return
        if self.config.adapters[model.adapter].type != "fake":
            pressure = memory_pressure_level()
            if pressure is not None and pressure < 50:
                raise DispatchError(f"system memory pressure level {pressure} is below 50; refusing a new model load", 409, "memory_pressure")
            if sys.platform == "darwin" and pressure is None:
                raise DispatchError("system memory pressure is unavailable; refusing a new model load", 409, "memory_status_unknown")
        limit = self.settings.get("memory_limit_gb")
        if limit is None:
            return
        estimate = self._effective_estimate(model)
        if estimate is None:
            raise DispatchError(f"model {model.name!r} has no estimated_memory_gb; refusing memory-limited admission", 409, "memory_estimate_required")
        loaded = 0.0
        for name, item in self._models.items():
            if name == model.name or name in excluded or item.state != "ready":
                continue
            if self.config.adapters[self.config.models[name].adapter].type in {"external", "observe", "http-managed"}:
                continue
            value = self._effective_estimate(self.config.models[name])
            if value is None:
                raise DispatchError(f"model {name!r} has no estimated_memory_gb; refusing memory-limited admission", 409, "memory_estimate_required")
            loaded += value
        reserved = sum(
            item.loading_reservation_gb or 0.0
            for name, item in self._models.items()
            if name != model.name and name not in excluded and item.state == "loading"
            and self.config.adapters[self.config.models[name].adapter].type not in {"external", "observe", "http-managed"}
        )
        excluded_adapters = {model.adapter}
        excluded_adapters.update(self.config.models[name].adapter for name in excluded)
        external = self._external_memory_gb(exclude_adapters=excluded_adapters)
        total = loaded + reserved + external + estimate
        if total > limit:
            raise DispatchError(
                f"model {model.name!r} requires {estimate:g}GB; admitted {loaded:g}GB, loading {reserved:g}GB, external {external:g}GB exceed memory limit {limit:g}GB",
                409,
                "memory_limit_exceeded",
            )

    def _capacity_blockers(self, model: ModelConfig) -> list[ModelConfig]:
        blockers: dict[str, ModelConfig] = {}
        for other in self.config.models.values():
            if other.name == model.name:
                continue
            if self._models[other.name].state not in {"loading", "ready", "unknown"}:
                continue
            if other.adapter == model.adapter and not self._native_route_switch_safe(other, model):
                blockers[other.name] = other
        for group_name in self._effective_groups(model):
            group = self.config.resource_groups.get(group_name)
            capacity = group.capacity if group is not None else 1
            occupants = [
                other
                for other in self.config.models.values()
                if other.name != model.name
                and self._models[other.name].state in {"loading", "ready"}
                and group_name in self._effective_groups(other)
            ]
            required = len(occupants) - capacity + 1
            if required > 0:
                occupants.sort(key=lambda other: (self._request_count(other.name) > 0, self._keep_resident(other), other.name))
                blockers.update((other.name, other) for other in occupants[:required])
        return list(blockers.values())

    def _deactivate_blocker(self, blocker: ModelConfig, target: ModelConfig):
        runtime = self._models[blocker.name]
        adapter = self.config.adapters[blocker.adapter]
        target_adapter = self.config.adapters[target.adapter]
        backend = self._backends.get(adapter.name)
        unowned_managed = bool(
            adapter.type == "managed"
            and backend is not None
            and getattr(backend, "process", None) is None
            and hasattr(backend, "healthy")
            and backend.healthy()
        )
        if unowned_managed and not blocker.deactivate_path:
            raise DispatchError(
                f"cannot switch away from unowned managed backend {adapter.name!r}",
                409,
                "external_backend_conflict",
            )
        if adapter.name != target_adapter.name and adapter.type in {"external", "observe", "http-managed"} and (adapter.type == "observe" or not blocker.deactivate_path):
            raise DispatchError(
                f"cannot switch away from external backend {adapter.name!r}; it is not owned by model-dispatch",
                409,
                "external_backend_conflict",
            )
        if adapter.name == target_adapter.name and target_adapter.type in {"external", "observe", "http-managed"} and not target.activate_path:
            raise DispatchError(
                f"external adapter {target_adapter.name!r} has no activation route for model {target.name!r}",
                409,
                "external_model_route_unavailable",
            )
        with self.condition:
            runtime.state = "unloading"
            self.condition.notify_all()
        try:
            deactivate_model_if_needed(adapter, blocker, self.config.load_timeout_seconds)
            self._ensure_deactivated(adapter, blocker)
        except Exception:
            with self.condition:
                runtime.state = "ready"
                self.condition.notify_all()
            raise
        keep_service = self._keep_resident(blocker) or (
            adapter.name == target_adapter.name and self._native_route_switch_safe(blocker, target)
        )
        self._unload_service(adapter, keep_service_running=keep_service, excluding=blocker.name)
        with self.condition:
            runtime.state = "unloaded"
            runtime.loading_reservation_gb = None
            runtime.observed_state = "unloaded"
            runtime.observed_at = time.time()
            runtime.last_confirmed_state = "unloaded"
            runtime.last_confirmed_at = runtime.observed_at
            self.condition.notify_all()

    def activate(self, model_name: str):
        model_name = self.resolve_model_name(model_name)
        model = self.config.models.get(model_name)
        if model is None:
            raise DispatchError(f"unknown model: {model_name}", 404, "unknown_model")
        if not model.enabled:
            raise DispatchError(f"model {model_name} is disabled", 409, "model_disabled")
        self._activate_model(model, explicit=True)

    def _can_recover_metal_timeout(self, model: ModelConfig) -> bool:
        adapter = self.config.adapters[model.adapter]
        backend = self._backends.get(adapter.name)
        return bool(
            adapter.type == "managed"
            and (adapter.plugin_id == "mlx-serve" or adapter.name == "mlx-serve")
            and getattr(backend, "process", None) is not None
            and self._request_count(model.name) == 1
        )

    def _recover_metal_timeout(self, model: ModelConfig):
        adapter = self.config.adapters[model.adapter]
        backend = self._backends[adapter.name]
        # Restart the owned server only.  The caller's activation loop performs
        # the single retried /load-model action so recovery never loads twice.
        backend.stop()
        backend.ensure_started(self.config.load_timeout_seconds)

    def _activate_model(
        self,
        model: ModelConfig,
        cancel_event: threading.Event | None = None,
        explicit: bool = False,
        recovery_state: dict[str, Any] | None = None,
    ):
        runtime = self._models[model.name]
        self._ensure_catalog_available(model, runtime)
        wait_started = time.monotonic()
        deadline = wait_started + self.config.load_timeout_seconds
        self._refresh_observed_states()
        with self.condition:
            if runtime.state == "ready":
                return (time.monotonic() - wait_started) * 1000, None
        transition_locks = self._acquire_transitions([model], cancel_event, deadline)
        try:
            while True:
                with self.condition:
                    if cancel_event is not None and cancel_event.is_set():
                        raise DispatchError("request cancelled while waiting for the model", 409, "request_cancelled")
                    if runtime.state == "ready":
                        return (time.monotonic() - wait_started) * 1000, None
                    if runtime.state in {"loading", "unloading"}:
                        self.condition.wait(timeout=self.config.request_poll_seconds)
                        continue
                    blockers = self._capacity_blockers(model)
                    if blockers and not explicit and not self.settings.get("smart_scheduling", False):
                        raise DispatchError(
                            "smart scheduling is disabled; use /v1/switch for an explicit model switch",
                            409,
                            "smart_scheduling_disabled",
                        )
                    uncertain = [item.name for item in blockers if self._models[item.name].state == "unknown"]
                    if uncertain:
                        raise DispatchError(
                            f"model {model.name!r} cannot replace model(s) with unverified state: {', '.join(sorted(uncertain))}",
                            409,
                            "model_state_unverified",
                        )
                    busy = [item.name for item in blockers if self._request_count(item.name) > 0 or self._legacy_active_requests > 0]
                    loading = [item.name for item in blockers if self._models[item.name].state == "loading"]
                    probe_candidates = [
                        item for item in blockers
                        if item.name not in busy and item.name not in loading and item.deactivate_path
                    ]
                # Probe outside the condition lock: the transition locks we hold
                # already freeze blocker state, and HTTP must not block waiters.
                for item in probe_candidates:
                    if self._external_activity(item):
                        busy.append(item.name)
                if busy or loading:
                    raise DispatchError(
                        f"model {model.name!r} cannot replace busy model(s): {', '.join(sorted(busy + loading))}",
                        409,
                        "model_busy",
                    )
                with self._memory_lock:
                    self._check_memory(model, exclude_models={item.name for item in blockers})
                for blocker in blockers:
                    self._deactivate_blocker(blocker, model)
                if blockers:
                    continue
                break
            wait_ms = (time.monotonic() - wait_started) * 1000
            load_started = time.monotonic()
            with self._memory_lock:
                self._check_memory(model)
                estimate = self._effective_estimate(model) if self.settings.get("memory_limit_gb") is not None else None
                with self.condition:
                    runtime.loading_reservation_gb = estimate
                    runtime.state = "loading"
                    self.last_error = None
                    self.condition.notify_all()
            try:
                backend = self._backend_for(model)
                # A reusable server may outlive a previous core process. Adopt it
                # only when the model has native lifecycle routes; process-as-model
                # runtimes still require ownership before we may replace them.
                reusable_service = bool(model.activate_path and model.deactivate_path and backend.healthy())
                if not reusable_service:
                    backend.ensure_started(self.config.load_timeout_seconds)
                # Model load/unload endpoints may block while large weights move into memory.
                adapter = self.config.adapters[model.adapter]
                while True:
                    try:
                        activate_model_if_needed(adapter, model, self.config.load_timeout_seconds)
                        self._ensure_activated(adapter, model)
                    except DispatchError as exc:
                        if (
                            exc.error_type in {"metal_gpu_timeout", "mlx_activation_retryable"}
                            and recovery_state is not None
                            and not recovery_state.get("used")
                            and self._can_recover_metal_timeout(model)
                        ):
                            recovery_state["used"] = True
                            metrics = recovery_state.get("metrics")
                            if isinstance(metrics, dict):
                                metrics["recovery_used"] = True
                                metrics["recovery_attempted"] = "mlx-serve-metal-timeout"
                            self._recover_metal_timeout(model)
                            if isinstance(metrics, dict):
                                metrics["recovery_succeeded"] = True
                            continue
                        raise
                    break
            except Exception as exc:
                with self.condition:
                    runtime.state = "failed"
                    runtime.loading_reservation_gb = None
                    self.last_error = str(exc)
                    self.condition.notify_all()
                # A failed activation after earlier blockers were deactivated must
                # not restore them optimistically: their unloads already happened.
                for blocker in blockers:
                    blocker_runtime = self._models[blocker.name]
                    if blocker_runtime.state == "unloaded":
                        self._legacy_current_model = None
                    elif blocker_runtime.state == "ready":
                        self._legacy_current_model = blocker.name
                if isinstance(exc, DispatchError):
                    raise
                raise DispatchError(str(exc), 500) from exc
            with self.condition:
                runtime.state = "ready"
                # The idle clock starts when the model becomes resident, not when
                # the runtime object was created: an explicit switch hours later
                # would otherwise unload the model the moment it finished loading.
                runtime.last_request_finished = time.monotonic()
                runtime.loading_reservation_gb = None
                runtime.observed_state = "ready"
                runtime.observed_at = time.time()
                runtime.last_confirmed_state = "ready"
                runtime.last_confirmed_at = runtime.observed_at
                self._legacy_current_model = model.name
                self.condition.notify_all()
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                else:
                    cancelled = False
                cleanup = False
                if cancelled and self._request_count(model.name) <= 1 and not self._legacy_active_requests:
                    # The only waiter cancelled: do not leave a large model loaded
                    # for nobody. Block new arrivals, then roll back the load.
                    runtime.state = "unloading"
                    cleanup = True
            if cleanup:
                adapter = self.config.adapters[model.adapter]
                try:
                    deactivate_model_if_needed(adapter, model, self.config.load_timeout_seconds)
                    self._unload_service(adapter, keep_service_running=self._keep_resident(model), excluding=model.name)
                    cleanup_state = "unloaded"
                except Exception as exc:
                    cleanup_state = "ready"
                    with self.condition:
                        self.last_error = str(exc)
                with self.condition:
                    runtime.state = cleanup_state
                    runtime.loading_reservation_gb = None
                    self.condition.notify_all()
            if cancelled:
                raise DispatchError("request cancelled while loading the model", 409, "request_cancelled")
            # Record evidence only after the load remains confirmed ready.  A
            # cancelled waiter may briefly reach ``ready`` before rollback;
            # sampling that transient state would publish a false success.
            self._record_observed_memory(model)
            return wait_ms, (time.monotonic() - load_started) * 1000
        finally:
            self._release_transitions(transition_locks)

    def unload(self, model_name: str | None = None):
        if model_name is not None:
            model_name = self.resolve_model_name(model_name)
        lock_models = list(self.config.models.values()) if model_name is None else [self.config.models.get(model_name)]
        if any(model is None for model in lock_models):
            raise DispatchError(f"unknown model: {model_name}", 404, "unknown_model")
        transition_locks = self._acquire_transitions(lock_models)
        try:
            with self.condition:
                targets = [
                    self.config.models[name]
                    for name, runtime in self._models.items()
                    if runtime.state in {"ready", "unknown"} and (model_name is None or name == model_name)
                ]
                busy = [model.name for model in targets if self._request_count(model.name)]
                if busy:
                    raise DispatchError(f"cannot unload busy model(s): {', '.join(sorted(busy))}", 409, "model_busy")
                for current in targets:
                    adapter = self.config.adapters[current.adapter]
                    if self._models[current.name].state == "unknown":
                        raise DispatchError(
                            f"cannot unload model {current.name!r}; backend state is unverified",
                            409,
                            "model_state_unverified",
                        )
                    if adapter.type == "observe" or (adapter.type in {"external", "http-managed"} and not current.deactivate_path):
                        raise DispatchError(
                            f"cannot unload external backend {adapter.name!r}; it is not owned by model-dispatch",
                            409,
                            "external_backend_not_owned",
                        )
            externally_busy = [current.name for current in targets if self._external_activity(current)]
            if externally_busy:
                raise DispatchError(
                    f"cannot unload model(s) with active or unknown external requests: {', '.join(sorted(externally_busy))}",
                    409,
                    "model_busy",
                )
            for current in targets:
                adapter = self.config.adapters[current.adapter]
                runtime = self._models[current.name]
                with self.condition:
                    runtime.state = "unloading"
                    self.last_error = None
                    self.condition.notify_all()
                try:
                    deactivate_model_if_needed(adapter, current, self.config.load_timeout_seconds)
                    self._ensure_deactivated(adapter, current)
                except Exception:
                    with self.condition:
                        runtime.state = "ready"
                        self.condition.notify_all()
                    raise
                self._unload_service(adapter, keep_service_running=self._keep_resident(current), excluding=current.name)
                with self.condition:
                    runtime.state = "unloaded"
                    runtime.loading_reservation_gb = None
                    runtime.observed_state = "unloaded"
                    runtime.observed_at = time.time()
                    runtime.last_confirmed_state = "unloaded"
                    runtime.last_confirmed_at = runtime.observed_at
            with self.condition:
                self.last_error = None
                self.condition.notify_all()
        finally:
            self._release_transitions(transition_locks)

    def begin_request(self, model_name: str, streaming: bool, meta: dict[str, Any] | None = None):
        model_name = self.resolve_model_name(model_name)
        model = self.config.models.get(model_name)
        if model is None:
            raise DispatchError(f"unknown model: {model_name}", 404, "unknown_model")
        if not model.enabled:
            raise DispatchError(f"model {model_name} is disabled", 409, "model_disabled")
        if streaming and not model.capabilities.get("stream", False):
            raise DispatchError(f"model {model_name} does not support streaming", 400, "unsupported_capability")
        with self.condition:
            if meta is not None and meta["request_id"] in self._requests:
                raise DispatchError("request id is already active", 409, "duplicate_request_id")
            if meta is not None:
                meta["model"] = model_name
                meta["started"] = time.monotonic()
                meta["started_wall"] = time.time()
                meta["upstream"] = None
                self._requests[meta["request_id"]] = meta
        try:
            wait_ms, load_ms = self._activate_model(
                model,
                meta["cancel_event"] if meta else None,
                recovery_state=meta.get("recovery_state") if meta else None,
            )
        except Exception:
            if meta is not None:
                with self.condition:
                    self._requests.pop(meta["request_id"], None)
                    self.condition.notify_all()
            raise
        with self.condition:
            if self._models[model_name].state != "ready":
                raise DispatchError(f"model {model_name} is not ready", 503, "model_not_ready")
            if meta is None:
                self._legacy_active_requests += 1
            return wait_ms, load_ms

    def set_upstream(self, request_id: str, upstream: Any):
        with self.condition:
            current = self._requests.get(request_id)
            if current is not None:
                current["upstream"] = upstream

    def clear_upstream(self, request_id: str):
        with self.condition:
            current = self._requests.get(request_id)
            if current is not None:
                current["upstream"] = None

    def cancel(self, request_id: str | None = None) -> int:
        with self.condition:
            targets = [m for m in self._requests.values() if request_id is None or m["request_id"] == request_id]
            if not targets:
                return 0
            for meta in targets:
                meta["cancel_event"].set()
            upstreams = [m.get("upstream") for m in targets]
        for upstream in upstreams:
            if upstream is None:
                continue
            try:
                upstream.close()
            except OSError:
                pass
        return len(targets)

    def end_request(self, request_id: str | None = None):
        with self.condition:
            if request_id is not None:
                meta = self._requests.pop(request_id, None)
                if meta and meta.get("model") in self._models:
                    self._models[meta["model"]].last_request_finished = time.monotonic()
            else:
                self._legacy_active_requests = max(0, self._legacy_active_requests - 1)
            if self.active_requests == 0:
                self.last_request_finished = time.monotonic()
            self.condition.notify_all()

    def shutdown(self):
        self._idle_stop.set()
        with self.condition:
            for meta in self._requests.values():
                meta["cancel_event"].set()
            self.condition.notify_all()
            stopped = set()
            for backend in self._backends.values():
                stopped.add(id(backend))
                backend.stop()
            if self._legacy_backend is not None and id(self._legacy_backend) not in stopped:
                self._legacy_backend.stop()


class DispatchHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, handler_class, dispatcher: ModelDispatcher):
        super().__init__(server_address, handler_class)
        self.dispatcher = dispatcher
        self.run_id = f"{time.time_ns()}-{os.getpid()}"


class RequestRecorder:
    # Only explicit terminal success states contribute to persistent rate
    # summaries. Failed, incomplete, cancelled and disconnected requests stay
    # visible in the recent request log but cannot skew benchmarks.
    SUCCESS_FINISH_REASONS = frozenset({"stop", "tool_calls", "length", "eos", "complete", "completed"})

    def __init__(self, path: Path, memory_limit: int = 100):
        self.path = path
        self.aggregate_path = path.with_suffix(".summary.json")
        self.max_bytes = 10 * 1024 * 1024
        self.memory_limit = memory_limit
        self.lock = threading.Lock()
        self.recent: list[dict[str, Any]] = []

    def record(self, entry: dict[str, Any]):
        sensitive_fields = {"prompt", "messages", "input", "image", "images", "tools", "body", "request", "api_key", "authorization"}
        entry = {
            key: value for key, value in entry.items()
            if value is not None and not key.startswith("_") and key.lower() not in sensitive_fields
        }
        with self.lock:
            self.recent.append(entry)
            self.recent = self.recent[-self.memory_limit:]
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                    self.path.replace(self.path.with_suffix(".jsonl.1"))
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, sort_keys=True) + "\n")
                self._update_aggregate(entry)
            except OSError:
                pass

    def _update_aggregate(self, entry: dict[str, Any]):
        """Persist small numeric summaries without retaining request content."""
        if entry.get("finish_reason") not in self.SUCCESS_FINISH_REASONS:
            return
        try:
            aggregate = json.loads(self.aggregate_path.read_text(encoding="utf-8")) if self.aggregate_path.exists() else {"version": 1, "models": {}}
            models = aggregate.setdefault("models", {})
            model = str(entry.get("model", "unknown"))
            bucket = "cold_start" if float(entry.get("cold_load_ms") or 0) > 0 else "warm"
            cached = entry.get("cached_tokens")
            if isinstance(cached, (int, float)) and not isinstance(cached, bool):
                cache_bucket = "cache_hit" if cached > 0 else "cache_miss"
            else:
                cache_bucket = "cache_unknown"
            model_data = models.setdefault(model, {})
            for name in (bucket, cache_bucket):
                bucket_data = model_data.setdefault(name, {"count": 0, "metrics": {}, "sources": {}})
                bucket_data["count"] += 1
                source = str(entry.get("metrics_source", "dispatcher"))
                bucket_data["sources"][source] = bucket_data["sources"].get(source, 0) + 1
                for metric in ("tokens_per_second", "prefill_tokens_per_second", "ttft_ms", "cold_load_ms", "cached_tokens"):
                    value = entry.get(metric)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        stats = bucket_data["metrics"].setdefault(metric, {"count": 0, "min": value, "max": value, "sum": 0.0, "values": []})
                        # Keep bounded numeric samples for an exact median; no request content is stored.
                        if isinstance(stats, list):
                            stats = {"count": 0, "min": value, "max": value, "sum": 0.0, "values": []}
                            bucket_data["metrics"][metric] = stats
                        stats["count"] += 1
                        stats["min"] = min(stats["min"], value)
                        stats["max"] = max(stats["max"], value)
                        stats["sum"] += float(value)
                        stats["values"].append(float(value))
                        del stats["values"][:-1000]
            temp = self.aggregate_path.with_name(self.aggregate_path.name + ".tmp")
            temp.write_text(json.dumps(aggregate, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(temp, self.aggregate_path)
        except (OSError, ValueError, TypeError):
            return

    def latest(self) -> dict[str, Any] | None:
        with self.lock:
            return self.recent[-1] if self.recent else None

    @staticmethod
    def _series(values: list[float]) -> dict[str, Any] | None:
        if not values:
            return None
        return {
            "count": len(values),
            "min": round(min(values), 2),
            "max": round(max(values), 2),
            "mean": round(statistics.fmean(values), 2),
            "median": round(statistics.median(values), 2),
        }

    def snapshot(self, limit: int = 100) -> dict[str, Any]:
        # ponytail: aggregates the in-memory tail only; older history lives in the
        # jsonl file. If the menu ever needs full history, page the file instead.
        with self.lock:
            entries = list(self.recent[-limit:])
        requests = list(reversed(entries))
        by_model: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            by_model.setdefault(str(entry.get("model", "unknown")), []).append(entry)
        summaries = []
        for name, items in sorted(by_model.items()):
            successful = [
                entry for entry in items
                if entry.get("finish_reason") in self.SUCCESS_FINISH_REASONS
            ]

            def values(key: str) -> list[float]:
                return [
                    float(entry[key]) for entry in successful
                    if isinstance(entry.get(key), (int, float)) and float(entry[key]) > 0
                ]

            summaries.append({
                "model": name,
                "window_start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(items[0].get("ts", 0))),
                "window_end": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(items[-1].get("ts", 0))),
                "total_requests": len(items),
                "successful_requests": len(successful),
                "excluded_requests": len(items) - len(successful),
                "decode": self._series(values("tokens_per_second")),
                "prefill": self._series(values("prefill_tokens_per_second")),
                "ttft_ms": self._series(values("ttft_ms")),
                "cache": self._series(values("cached_tokens")),
            })
        persistent = None
        try:
            persistent = json.loads(self.aggregate_path.read_text(encoding="utf-8")) if self.aggregate_path.exists() else None
        except (OSError, ValueError):
            persistent = None
        if isinstance(persistent, dict):
            for model_data in (persistent.get("models", {}) or {}).values():
                for bucket_data in model_data.values():
                    metrics = bucket_data.get("metrics", {}) if isinstance(bucket_data, dict) else {}
                    for stats in metrics.values():
                        if not isinstance(stats, dict) or "values" not in stats:
                            continue
                        values = stats.pop("values")
                        stats["mean"] = round(stats["sum"] / stats["count"], 2) if stats["count"] else None
                        stats["median"] = round(statistics.median(values), 2) if values else None
                        stats.pop("sum", None)
        return {"requests": requests, "summaries": summaries, "persistent_summaries": persistent}


class DispatchHandler(BaseHTTPRequestHandler):
    server: DispatchHTTPServer

    @staticmethod
    def _is_metal_gpu_timeout(value: Any) -> bool:
        text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
        lowered = text.lower()
        return "metal" in lowered and ("gpu" in lowered or "command buffer" in lowered) and ("timeout" in lowered or "timed out" in lowered)

    def _can_recover_metal_timeout(self, model: ModelConfig, request_id: str) -> bool:
        adapter = self.server.dispatcher.config.adapters[model.adapter]
        backend = self.server.dispatcher._backends.get(adapter.name)
        process = getattr(backend, "process", None)
        # ProcessBackend.process is populated only for a process spawned by
        # this dispatcher; it may already be exited after a graceful timeout.
        return bool(
            adapter.type == "managed"
            and (adapter.plugin_id == "mlx-serve" or adapter.name == "mlx-serve")
            and process is not None
            and self.server.dispatcher._request_count(model.name) == 1
            and request_id in self.server.dispatcher._requests
        )

    def _recover_metal_backend(self, model: ModelConfig):
        adapter = self.server.dispatcher.config.adapters[model.adapter]
        backend = self.server.dispatcher._backends[adapter.name]
        runtime = self.server.dispatcher._models[model.name]
        with self.server.dispatcher.condition:
            runtime.state = "loading"
            self.server.dispatcher.condition.notify_all()
        try:
            backend.stop()
            backend.ensure_started(self.server.dispatcher.config.load_timeout_seconds)
            activate_model_if_needed(adapter, model, self.server.dispatcher.config.load_timeout_seconds)
            self.server.dispatcher._ensure_activated(adapter, model)
        except Exception:
            with self.server.dispatcher.condition:
                runtime.state = "failed"
                self.server.dispatcher.condition.notify_all()
            raise
        with self.server.dispatcher.condition:
            runtime.state = "ready"
            runtime.observed_state = "ready"
            runtime.observed_at = time.time()
            runtime.last_confirmed_state = "ready"
            runtime.last_confirmed_at = runtime.observed_at
            self.server.dispatcher.condition.notify_all()

    def log_message(self, fmt, *args):
        return

    def _reject_cross_site(self) -> bool:
        """Loopback-only API: reject browser cross-site calls and DNS rebinding."""
        host = self.headers.get("Host")
        if host:
            if host.startswith("["):
                hostname = host[1:].split("]", 1)[0]
            elif host.count(":") == 1:
                hostname = host.split(":", 1)[0]
            else:
                hostname = host
            if hostname not in {"127.0.0.1", "localhost", "::1"}:
                self._json(403, {"error": {"message": "Host must be a loopback address", "type": "forbidden"}})
                return True
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or (parsed.hostname or "") not in {"127.0.0.1", "localhost", "::1"}:
                self._json(403, {"error": {"message": "cross-site requests are not allowed", "type": "forbidden"}})
                return True
        return False

    def _json(self, status: int, payload: dict[str, Any]):
        payload = dict(payload)
        payload["dispatcher_run"] = self.server.run_id
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, error: DispatchError):
        self._json(error.status, {"error": {"message": error.message, "type": error.error_type}})

    def _content_length(self, require_body: bool = False) -> int:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError as exc:
            raise DispatchError("invalid Content-Length", 400, "invalid_request_error") from exc
        if length < 0 or length > self.server.dispatcher.config.max_body_bytes or (require_body and length == 0):
            raise DispatchError("invalid or oversized request body", 400, "invalid_request_error")
        return length

    def _body(self) -> dict[str, Any]:
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise DispatchError("Content-Type must be application/json", 415, "invalid_request_error")
        length = self._content_length(require_body=True)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise DispatchError(f"invalid JSON body: {exc}", 400, "invalid_request_error") from exc
        if not isinstance(payload, dict):
            raise DispatchError("JSON body must be an object", 400, "invalid_request_error")
        return payload

    def do_GET(self):
        if self._reject_cross_site():
            return
        if self.path == "/health":
            self._json(200, {"status": "ok", "service": "model-dispatch"})
        elif self.path == "/v1/settings":
            self._json(200, self.server.dispatcher.settings)
        elif self.path == "/v1/metrics" or self.path.startswith("/v1/metrics?"):
            try:
                limit = int(dict(parse_qsl(urlsplit(self.path).query)).get("limit", "100"))
            except ValueError:
                limit = 100
            limit = max(1, min(limit, 500))
            self._json(200, self.server.dispatcher.recorder.snapshot(limit))
        elif self.path == "/v1/models":
            self._json(200, {"object": "list", "data": self.server.dispatcher.model_entries()})
        elif self.path == "/v1/config":
            self._json(200, self.server.dispatcher.config_report())
        elif self.path == "/v1/status":
            self._json(200, self.server.dispatcher.status())
        else:
            self._json(404, {"error": {"message": f"unknown path {self.path}", "type": "not_found"}})

    def do_POST(self):
        if self._reject_cross_site():
            return
        if self.path == "/v1/settings":
            try:
                settings = self.server.dispatcher.update_settings(self._body())
            except (ConfigError, OSError) as exc:
                self._error(DispatchError(str(exc), 400 if isinstance(exc, ConfigError) else 500, "invalid_settings" if isinstance(exc, ConfigError) else "settings_persist_failed"))
            else:
                self._json(200, settings)
            return
        if self.path == "/v1/cancel":
            try:
                request_id = None
                if self._content_length() > 0:
                    request_id = self._body().get("request_id")
                    if request_id is not None and (not isinstance(request_id, str) or not request_id):
                        raise DispatchError("request_id must be a non-empty string", 400, "invalid_request_error")
            except DispatchError as exc:
                self._error(exc)
                return
            cancelled_count = self.server.dispatcher.cancel(request_id)
            self._json(200, {"cancelled": cancelled_count > 0, "cancelled_count": cancelled_count, "model_kept_loaded": True, "message": "Cancellation requested; model remains loaded." if cancelled_count else "No active request."})
            return

        if self.path == "/v1/unload":
            try:
                model_name = None
                if self._content_length() > 0:
                    model_name = self._body().get("model")
                    if model_name is not None and (not isinstance(model_name, str) or not model_name):
                        raise DispatchError("model must be a non-empty string", 400, "invalid_request_error")
                self.server.dispatcher.unload(model_name)
            except DispatchError as exc:
                self._error(exc)
            else:
                self._json(200, {"status": "unloaded"})
            return

        if self.path == "/v1/reconcile":
            try:
                result = self.server.dispatcher.reconcile_models()
            except DispatchError as exc:
                self._error(exc)
            else:
                self._json(200, result)
            return

        if self.path == "/v1/reload":
            try:
                result = self.server.dispatcher.reload_config()
            except DispatchError as exc:
                self._error(exc)
            else:
                self._json(200, result)
            return

        if self.path == "/v1/switch":
            try:
                payload = self._body()
                model_name = payload.get("model")
                if not isinstance(model_name, str) or not model_name:
                    raise DispatchError("missing or invalid 'model' key", 400, "invalid_request_error")
                model_name = self.server.dispatcher.resolve_model_name(model_name)
                self.server.dispatcher.activate(model_name)
            except DispatchError as exc:
                self._error(exc)
            else:
                self._json(200, {"status": "ready", "active_model": model_name})
            return

        if self.path not in {"/v1/chat/completions", "/v1/responses"}:
            self._json(404, {"error": {"message": f"unknown path {self.path}", "type": "not_found"}})
            return

        request_started = False
        request_id = self.headers.get("X-InferenceDock-Request-ID") or uuid.uuid4().hex
        if len(request_id) > 128 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for ch in request_id):
            self._error(DispatchError("invalid X-InferenceDock-Request-ID", 400, "invalid_request_error"))
            return
        started_at = time.monotonic()
        upstream_path = self.path
        metrics: dict[str, Any] = {"stream": False, "protocol": "responses" if upstream_path == "/v1/responses" else "chat_completions", "_started_at": started_at}
        request_meta = {"request_id": request_id, "cancel_event": threading.Event(), "metrics": metrics}
        try:
            payload = self._body()
            model_name = payload.get("model")
            if not isinstance(model_name, str) or not model_name:
                raise DispatchError("missing or invalid 'model' key", 400, "invalid_request_error")
            model_name = self.server.dispatcher.resolve_model_name(model_name)
            model = self.server.dispatcher.config.models.get(model_name)
            if model is None:
                raise DispatchError(f"unknown model: {model_name}", 404, "unknown_model")
            stream_value = payload.get("stream", False)
            if not isinstance(stream_value, bool):
                raise DispatchError("'stream' must be a boolean", 400, "invalid_request_error")
            streaming = stream_value
            if upstream_path == "/v1/responses" and payload.get("background") is True:
                raise DispatchError(
                    "background Responses are not supported; use a foreground request",
                    400,
                    "unsupported_request",
                )
            metrics["stream"] = streaming
            payload["model"] = model.backend_model
            body = json.dumps(payload).encode("utf-8")
            request_meta["recovery_state"] = {"used": False, "metrics": metrics}
            queue_ms, cold_load_ms = self.server.dispatcher.begin_request(model_name, streaming, request_meta)
            metrics["queue_ms"] = round(queue_ms, 1)
            metrics["cold_load_ms"] = round(cold_load_ms, 1) if cold_load_ms is not None else None
            request_started = True
            upstream = self._open_upstream(model, body, upstream_path)
            self.server.dispatcher.set_upstream(request_id, upstream)
            try:
                response_bytes = 0
                metrics["response_bytes"] = 0
                response_sample = bytearray()
                cancelled = False
                if not streaming:
                    retried_metal_timeout = False
                    while True:
                        try:
                            response_body = upstream.read()
                        except TimeoutError as exc:
                            raise DispatchError("upstream response timed out", 504, "upstream_timeout") from exc
                        except (OSError, ValueError) as exc:
                            if request_meta["cancel_event"].is_set():
                                raise DispatchError("request cancelled", 409, "request_cancelled") from exc
                            raise DispatchError(f"upstream response failed: {exc}", 502, "upstream_unavailable") from exc
                        if (
                            not retried_metal_timeout
                            and not metrics.get("recovery_used")
                            and upstream.status >= 500
                            and self._is_metal_gpu_timeout(response_body)
                            and self._can_recover_metal_timeout(model, request_id)
                        ):
                            metrics["recovery_attempted"] = "mlx-serve-metal-timeout"
                            metrics["recovery_used"] = True
                            upstream.close()
                            self.server.dispatcher.clear_upstream(request_id)
                            try:
                                self._recover_metal_backend(model)
                            except Exception as exc:
                                metrics["recovery_error"] = type(exc).__name__
                            else:
                                retried_metal_timeout = True
                                metrics["recovery_succeeded"] = True
                                upstream = self._open_upstream(model, body, upstream_path)
                                self.server.dispatcher.set_upstream(request_id, upstream)
                                continue
                        break
                    if request_meta["cancel_event"].is_set():
                        raise DispatchError("request cancelled", 409, "request_cancelled")
                    self.send_response(upstream.status)
                    self.send_header("X-InferenceDock-Request-ID", request_id)
                    self.send_header("Content-Type", upstream.headers.get("Content-Type", "application/json"))
                    self.send_header("Content-Length", str(len(response_body)))
                    self.end_headers()
                    self.wfile.write(response_body)
                    self.wfile.flush()
                    metrics["response_bytes"] = len(response_body)
                    self._consume_json_metrics(response_body[:1048576], metrics)
                    return
                self.send_response(upstream.status)
                self.send_header("X-InferenceDock-Request-ID", request_id)
                self.send_header("Content-Type", upstream.headers.get("Content-Type", "application/json"))
                if streaming and "text/event-stream" in upstream.headers.get("Content-Type", ""):
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                self.end_headers()
                try:
                    while True:
                        if request_meta["cancel_event"].is_set():
                            cancelled = True
                            metrics["finish_reason"] = "request_cancelled"
                            break
                        chunk = upstream.read1(8192)
                        if not chunk:
                            break
                        response_bytes += len(chunk)
                        if streaming:
                            self._consume_sse_bytes(chunk, metrics)
                        elif len(response_sample) < 1048576:
                            response_sample.extend(chunk[: 1048576 - len(response_sample)])
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        metrics["response_bytes"] = response_bytes
                        if streaming and metrics.get("_done"):
                            break
                except TimeoutError:
                    metrics["finish_reason"] = "upstream_timeout"
                    self.close_connection = True
                    if streaming:
                        error = json.dumps({"error": {"message": "upstream response timed out", "type": "upstream_timeout"}}).encode()
                        try:
                            self.wfile.write(b"data: " + error + b"\n\ndata: [DONE]\n\n")
                            self.wfile.flush()
                        except OSError:
                            pass
                except OSError:
                    if request_meta["cancel_event"].is_set():
                        cancelled = True
                        metrics["finish_reason"] = "request_cancelled"
                    else:
                        raise
                if streaming:
                    self._flush_sse_buffer(metrics)
                    self.close_connection = True
                    if cancelled:
                        metrics["finish_reason"] = "request_cancelled"
                else:
                    self._consume_json_metrics(bytes(response_sample), metrics)
                metrics["response_bytes"] = response_bytes
            finally:
                upstream.close()
                self.server.dispatcher.clear_upstream(request_id)
        except (BrokenPipeError, ConnectionResetError):
            # The client went away; close the upstream and record cancellation without fabricating a backend finish.
            metrics.setdefault("finish_reason", "client_disconnect")
        except DispatchError as exc:
            self._error(exc)
        finally:
            if request_started:
                try:
                    duration_ms = round((time.monotonic() - started_at) * 1000, 1)
                    self.server.dispatcher.recorder.record(
                        {
                            "ts": time.time(),
                            "model": model_name,
                            "adapter": model.adapter,
                            "protocol": metrics.get("protocol"),
                            "stream": metrics.get("stream", False),
                            "duration_ms": duration_ms,
                            "queue_ms": metrics.get("queue_ms"),
                            "cold_load_ms": metrics.get("cold_load_ms"),
                            "ttft_ms": metrics.get("ttft_ms"),
                            "usage": metrics.get("usage"),
                            "response_status": metrics.get("response_status"),
                            "cached_tokens": metrics.get("cached_tokens"),
                            "response_bytes": metrics.get("response_bytes"),
                            "sse_chunks": metrics.get("sse_chunks"),
                            "finish_reason": metrics.get("finish_reason"),
                            "tokens_per_second": metrics.get("tokens_per_second"),
                            "prefill_tokens_per_second": metrics.get("prefill_tokens_per_second"),
                            "metrics_source": "upstream" if any(
                                metrics.get(key) is not None
                                for key in ("usage", "cached_tokens", "tokens_per_second", "prefill_tokens_per_second")
                            ) else "dispatcher",
                            "recovery_attempted": metrics.get("recovery_attempted"),
                            "recovery_succeeded": metrics.get("recovery_succeeded"),
                            "recovery_error": metrics.get("recovery_error"),
                        }
                    )
                except Exception:
                    pass
                self.server.dispatcher.end_request(request_id)

    def _consume_sse_bytes(self, chunk: bytes, metrics: dict[str, Any]):
        buffer = metrics.setdefault("_sse_buffer", b"")
        buffer += chunk
        if len(buffer) > 65536:
            buffer = buffer[-8192:]
        lines = buffer.split(b"\n")
        buffer = lines.pop()
        for line in lines:
            self._consume_sse_line(line, metrics)
        metrics["_sse_buffer"] = bytes(buffer)

    def _consume_sse_line(self, line: bytes, metrics: dict[str, Any]):
        line = line.strip()
        if not line.startswith(b"data:"):
            return
        data = line[5:].strip()
        if data == b"[DONE]":
            metrics["_done"] = True
            return
        try:
            event = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        # SSE data is not required to be an object (some backends emit [] or
        # null keep-alives). Ignore such frames for metrics without affecting
        # the byte-for-byte response forwarding path.
        if not isinstance(event, dict):
            return
        metrics["sse_chunks"] = metrics.get("sse_chunks", 0) + 1
        event_type = event.get("type")
        if event_type == "response.output_text.delta" and isinstance(event.get("delta"), str) and event["delta"]:
            metrics.setdefault("ttft_ms", round((time.monotonic() - metrics["_started_at"]) * 1000, 1))
        if event_type == "response.completed" and isinstance(event.get("response"), dict):
            response = event["response"]
            response_status = response.get("status")
            if isinstance(response_status, str):
                metrics["response_status"] = response_status
            # Preserve the existing normalized stop reason for completed
            # streams while retaining the Responses-specific status above.
            metrics["finish_reason"] = "stop" if response_status == "completed" else response_status
            usage = response.get("usage")
            if isinstance(usage, dict):
                metrics["usage"] = usage
                details = usage.get("input_tokens_details")
                if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
                    metrics["cached_tokens"] = details["cached_tokens"]
            timings = response.get("timings")
            if isinstance(timings, dict):
                if isinstance(timings.get("predicted_per_second"), (int, float)):
                    metrics["tokens_per_second"] = timings["predicted_per_second"]
                if isinstance(timings.get("prompt_per_second"), (int, float)):
                    metrics["prefill_tokens_per_second"] = timings["prompt_per_second"]
        usage = event.get("usage")
        if isinstance(usage, dict):
            metrics["usage"] = usage
            details = usage.get("prompt_tokens_details", usage.get("input_tokens_details"))
            if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
                metrics["cached_tokens"] = details["cached_tokens"]
        finish = event.get("finish_reason")
        if isinstance(finish, str):
            metrics["finish_reason"] = finish
        choices = event.get("choices")
        if isinstance(choices, list) and choices:
            if "ttft_ms" not in metrics and any(self._has_sse_content(choice) for choice in choices):
                metrics["ttft_ms"] = round((time.monotonic() - metrics["_started_at"]) * 1000, 1)
            choice_finish = choices[0].get("finish_reason") if isinstance(choices[0], dict) else None
            if isinstance(choice_finish, str):
                metrics["finish_reason"] = choice_finish
        timings = event.get("timings")
        if isinstance(timings, dict) and isinstance(timings.get("predicted_per_second"), (int, float)):
            metrics["tokens_per_second"] = timings["predicted_per_second"]

    @staticmethod
    def _has_sse_content(choice: Any) -> bool:
        if not isinstance(choice, dict):
            return False
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            return False
        return any(
            (isinstance(delta.get(key), str) and bool(delta[key]))
            or (isinstance(delta.get(key), list) and bool(delta[key]))
            for key in ("content", "reasoning_content", "tool_calls")
        )

    def _flush_sse_buffer(self, metrics: dict[str, Any]):
        buffer = metrics.get("_sse_buffer", b"")
        if buffer:
            self._consume_sse_line(buffer, metrics)
            metrics["_sse_buffer"] = b""

    def _consume_json_metrics(self, body: bytes, metrics: dict[str, Any]):
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        response = payload.get("response") if isinstance(payload.get("response"), dict) else payload
        if metrics.get("protocol") == "responses":
            status = response.get("status")
            if isinstance(status, str) and status in {"completed", "failed", "incomplete"}:
                metrics["response_status"] = status
                metrics["finish_reason"] = "stop" if status == "completed" else status
        usage = response.get("usage")
        if isinstance(usage, dict):
            metrics["usage"] = usage
            details = usage.get("prompt_tokens_details", usage.get("input_tokens_details"))
            if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
                metrics["cached_tokens"] = details["cached_tokens"]
        timings = response.get("timings")
        if isinstance(timings, dict):
            if isinstance(timings.get("predicted_per_second"), (int, float)):
                metrics["tokens_per_second"] = timings["predicted_per_second"]
            if isinstance(timings.get("prompt_per_second"), (int, float)):
                metrics["prefill_tokens_per_second"] = timings["prompt_per_second"]
        choices = response.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            finish_reason = choices[0].get("finish_reason")
            if isinstance(finish_reason, str) and finish_reason:
                metrics["finish_reason"] = finish_reason

    def _open_upstream(self, model: ModelConfig, body: bytes, path: str = "/v1/chat/completions"):
        adapter = self.server.dispatcher.config.adapters[model.adapter]
        request = urllib.request.Request(endpoint_for(adapter) + path, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        request.headers.update(backend_headers(adapter))
        if self.headers.get("Accept"):
            request.add_header("Accept", self.headers["Accept"])
        try:
            return urllib.request.urlopen(request, timeout=self.server.dispatcher.config.request_timeout_seconds)
        except urllib.error.HTTPError as exc:
            return exc
        except TimeoutError as exc:
            raise DispatchError(f"upstream request timed out: {exc}", 504, "upstream_timeout") from exc
        except OSError as exc:
            raise DispatchError(f"upstream request failed: {exc}", 502, "upstream_unavailable") from exc


def serve(config_path: Path):
    config = load_config(config_path)
    dispatcher = ModelDispatcher(config, ROOT)
    server = DispatchHTTPServer((config.listen_host, config.listen_port), DispatchHandler, dispatcher)

    def stop(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        dispatcher.shutdown()
        server.server_close()


def check_config(config_path: Path, probe: bool = False):
    config = load_config(config_path)
    print(f"config ok: {config.path}")
    print(f"listen: {config.listen_host}:{config.listen_port}")
    print("models: " + ", ".join(sorted(config.models)))
    if probe:
        for adapter in config.adapters.values():
            if adapter.type not in {"external", "observe", "http-managed", "managed"}:
                continue
            try:
                status, _ = http_json(endpoint_for(adapter) + adapter.health_path, timeout=config.connect_timeout_seconds, headers=backend_headers(adapter))
            except DispatchError as exc:
                print(f"probe {adapter.name}: {exc.error_type}")
                continue
            state = "healthy" if status == 200 else f"unhealthy:{status}"
            print(f"probe {adapter.name}: {state}")


def resolve_config_path(path: Path) -> Path:
    """Prefer the user's engines.yaml over a config bundled inside the app.

    App updates replace the bundle, never Application Support. An explicit
    non-bundle --config is always honored, and an invalid user config falls
    back to the bundled one with a warning instead of failing to start.
    """
    if ".app/Contents/Resources" not in str(path):
        return path
    settings_path = Path(os.environ.get("INFERENCEDOCK_SETTINGS_PATH", str(SETTINGS_PATH))).expanduser()
    user_config = settings_path.parent / "engines.yaml"
    if not user_config.is_file():
        return path
    try:
        load_config(user_config)
    except ConfigError as exc:
        print(f"warning: ignoring invalid user config {user_config}: {exc}", file=sys.stderr)
        return path
    return user_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--check-config", action="store_true", help="validate config and exit")
    parser.add_argument("--probe", action="store_true", help="with --check-config, perform read-only health probes")
    args = parser.parse_args()
    try:
        config_path = resolve_config_path(args.config.resolve())
        if args.check_config:
            check_config(config_path, probe=args.probe)
        else:
            if args.probe:
                raise ConfigError("--probe requires --check-config")
            serve(config_path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
