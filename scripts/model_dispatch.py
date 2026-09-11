#!/usr/bin/env python3
"""Minimal loopback model dispatcher core."""

from __future__ import annotations

import argparse
import json
import math
import os
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
    env: dict[str, str] = field(default_factory=dict)
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
    metrics_path = Path(_string(root.get("metrics_path", str(ROOT / "logs" / "request-metrics.jsonl")), "metrics_path")).expanduser()
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
        adapters[name] = AdapterConfig(
            name=name,
            type=adapter_type,
            command=command,
            env=dict(env),
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


def http_json(url: str, timeout: float = 0.5) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
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
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status >= 400:
                raise DispatchError(f"model {action} failed with HTTP {response.status}", 502, f"model_{action}_failed")
    except urllib.error.HTTPError as exc:
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
        self.log_path = Path(adapter.log_path or root / "logs" / f"{adapter.name}.log").expanduser()
        if not self.log_path.is_absolute():
            self.log_path = root / self.log_path
        self.run_id = f"{time.time_ns()}-{os.getpid()}"
        self.events_path = Path(adapter.events_path) if adapter.events_path else root / "logs" / f"fake-{adapter.port}-{self.run_id}.events.jsonl"
        self.stop_file = root / ".tmp" / f"fake-{adapter.port}-{self.run_id}.stop"

    def healthy(self) -> bool:
        status, _ = http_json(self.endpoint + self.adapter.health_path, timeout=0.4)
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
        self.root.joinpath(".tmp").mkdir(parents=True, exist_ok=True)
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
        group_names = set(config.resource_groups)
        group_names.update(group for adapter in config.adapters.values() for group in adapter.exclusive_groups)
        group_names.update(group for model in config.models.values() for group in model.exclusive_groups)
        self._transition_locks = {
            **{f"adapter:{name}": threading.Lock() for name in config.adapters},
            **{
                f"group:{name}": threading.BoundedSemaphore(
                    config.resource_groups[name].capacity if name in config.resource_groups else 1
                )
                for name in group_names
            },
        }
        self._memory_lock = threading.Lock()
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
        self.last_request_finished = time.monotonic()
        self._idle_stop = threading.Event()
        self._idle_thread = None
        if self.settings.get("idle_unload_seconds") is not None:
            self._idle_thread = threading.Thread(target=self._idle_loop, name="model-dispatch-idle", daemon=True)
            self._idle_thread.start()

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

    def _transition_keys(self, model: ModelConfig) -> set[str]:
        keys = {f"adapter:{model.adapter}"}
        keys.update(f"group:{name}" for name in self._effective_groups(model))
        return keys

    def _acquire_transitions(self, models: list[ModelConfig], cancel_event=None, deadline=None):
        locks = [self._transition_locks[key] for key in sorted(set().union(*(self._transition_keys(model) for model in models)))]
        acquired = []
        try:
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
            raise

    def model_entries(self, include_hidden: bool = False) -> list[dict[str, Any]]:
        with self.condition:
            states = {name: runtime.state for name, runtime in self._models.items()}
            request_counts = {name: self._request_count(name) for name in self.config.models}
            server_states = {name: self._service_state(adapter) for name, adapter in self.config.adapters.items()}
        return [
            {
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
                "capabilities": model.capabilities,
                "display_name": model.display_name or model.name,
                "context_window": model.context_window,
                "max_output_tokens": model.max_output_tokens,
                "reasoning_levels": list(model.reasoning_levels),
                "runtime_summary": list(model.runtime_summary),
                "state": states[model.name],
                "advertise": model.advertise,
                "enabled": model.enabled,
                "canonical": model.canonical,
                "aliases": list(model.aliases),
                "loaded": states[model.name] == "ready",
                "active_requests": request_counts[model.name],
                "server_status": server_states[model.adapter],
            }
            for model in self.config.models.values()
            if include_hidden or (model.advertise and model.enabled)
        ]

    def status(self) -> dict[str, Any]:
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
                "models": self.model_entries(include_hidden=True),
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
        results = []
        for adapter in self.config.adapters.values():
            if not adapter.models_path:
                continue
            configured = sorted({m.backend_model for m in self.config.models.values() if m.adapter == adapter.name})
            backend = ProcessBackend(adapter, self.root)
            try:
                if not backend.healthy():
                    results.append({"adapter": adapter.name, "status": "unreachable", "configured": configured})
                    continue
                status, payload = http_json(endpoint_for(adapter) + adapter.models_path, timeout=self.config.connect_timeout_seconds)
                if status != 200 or not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    results.append({"adapter": adapter.name, "status": "invalid_response", "configured": configured})
                    continue
                discovered = sorted({item.get("id") for item in payload["data"] if isinstance(item, dict) and isinstance(item.get("id"), str)})
                results.append({
                    "adapter": adapter.name,
                    "status": "ok",
                    "configured": configured,
                    "discovered": discovered,
                    "new_candidates": sorted(set(discovered) - set(configured)),
                    "missing_candidates": sorted(set(configured) - set(discovered)),
                })
            except DispatchError as exc:
                results.append({"adapter": adapter.name, "status": "error", "error": exc.message, "configured": configured})
        return {"results": results, "mutated": False}

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
        for state in ("unloading", "loading", "failed"):
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
        for state in ("unloading", "loading", "failed", "ready"):
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

    def _external_memory_gb(self, exclude_adapter: str | None = None) -> float:
        total = 0.0
        for adapter in self.config.adapters.values():
            if adapter.type not in {"external", "observe"} or adapter.name == exclude_adapter:
                continue
            if not ProcessBackend(adapter, self.root).healthy():
                continue
            observed = listener_rss_gb(endpoint_port(adapter))
            if observed is not None:
                total += observed
                continue
            adapter_models = [model for model in self.config.models.values() if model.adapter == adapter.name]
            if not adapter_models:
                raise DispatchError(
                    f"external adapter {adapter.name!r} is healthy but its memory use is unobservable",
                    409,
                    "memory_status_unknown",
                )
            estimates = [
                estimate
                for model in adapter_models
                for estimate in [self._effective_estimate(model)]
                if estimate is not None
            ]
            if not estimates:
                raise DispatchError(
                    f"external adapter {adapter.name!r} has no estimated_memory_gb; refusing memory-limited admission",
                    409,
                    "memory_estimate_required",
                )
            total += max(estimates)
        return total

    def _check_memory(self, model: ModelConfig):
        runtime = self._models[model.name]
        if runtime.state == "ready":
            return
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
            if name == model.name or item.state != "ready":
                continue
            if self.config.adapters[self.config.models[name].adapter].type in {"external", "observe"}:
                continue
            value = self._effective_estimate(self.config.models[name])
            if value is None:
                raise DispatchError(f"model {name!r} has no estimated_memory_gb; refusing memory-limited admission", 409, "memory_estimate_required")
            loaded += value
        reserved = sum(
            item.loading_reservation_gb or 0.0
            for name, item in self._models.items()
            if name != model.name and item.state == "loading"
            and self.config.adapters[self.config.models[name].adapter].type not in {"external", "observe"}
        )
        external = self._external_memory_gb(exclude_adapter=model.adapter)
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
            if self._models[other.name].state not in {"loading", "ready"}:
                continue
            if other.adapter == model.adapter:
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
        except Exception:
            with self.condition:
                runtime.state = "ready"
                self.condition.notify_all()
            raise
        self._unload_service(adapter, keep_service_running=bool(blocker.deactivate_path), excluding=blocker.name)
        with self.condition:
            runtime.state = "unloaded"
            runtime.loading_reservation_gb = None
            self.condition.notify_all()

    def activate(self, model_name: str):
        model_name = self.resolve_model_name(model_name)
        model = self.config.models.get(model_name)
        if model is None:
            raise DispatchError(f"unknown model: {model_name}", 404, "unknown_model")
        if not model.enabled:
            raise DispatchError(f"model {model_name} is disabled", 409, "model_disabled")
        self._activate_model(model, explicit=True)

    def _activate_model(self, model: ModelConfig, cancel_event: threading.Event | None = None, explicit: bool = False):
        runtime = self._models[model.name]
        wait_started = time.monotonic()
        deadline = wait_started + self.config.load_timeout_seconds
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
                    busy = [item.name for item in blockers if self._request_count(item.name) > 0 or self._legacy_active_requests > 0]
                    loading = [item.name for item in blockers if self._models[item.name].state == "loading"]
                    if busy or loading:
                        if time.monotonic() >= deadline:
                            raise DispatchError(
                                f"model {model.name!r} is waiting for busy model(s): {', '.join(sorted(busy + loading))}",
                                409,
                                "model_busy",
                            )
                        self.condition.wait(timeout=self.config.request_poll_seconds)
                        continue
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
                activate_model_if_needed(self.config.adapters[model.adapter], model, self.config.load_timeout_seconds)
            except Exception as exc:
                with self.condition:
                    runtime.state = "failed"
                    runtime.loading_reservation_gb = None
                    self.last_error = str(exc)
                    self.condition.notify_all()
                if isinstance(exc, DispatchError):
                    raise
                raise DispatchError(str(exc), 500) from exc
            with self.condition:
                runtime.state = "ready"
                runtime.loading_reservation_gb = None
                self._legacy_current_model = model.name
                self.condition.notify_all()
                if cancel_event is not None and cancel_event.is_set():
                    raise DispatchError("request cancelled while loading the model", 409, "request_cancelled")
            return wait_ms, (time.monotonic() - load_started) * 1000
        finally:
            for lock in reversed(transition_locks):
                lock.release()

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
                    if runtime.state == "ready" and (model_name is None or name == model_name)
                ]
                busy = [model.name for model in targets if self._request_count(model.name)]
                if busy:
                    raise DispatchError(f"cannot unload busy model(s): {', '.join(sorted(busy))}", 409, "model_busy")
                for current in targets:
                    adapter = self.config.adapters[current.adapter]
                    if adapter.type == "observe" or (adapter.type in {"external", "http-managed"} and not current.deactivate_path):
                        raise DispatchError(
                            f"cannot unload external backend {adapter.name!r}; it is not owned by model-dispatch",
                            409,
                            "external_backend_not_owned",
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
                except Exception:
                    with self.condition:
                        runtime.state = "ready"
                        self.condition.notify_all()
                    raise
                self._unload_service(adapter, keep_service_running=bool(current.deactivate_path), excluding=current.name)
                with self.condition:
                    runtime.state = "unloaded"
                    runtime.loading_reservation_gb = None
            with self.condition:
                self.last_error = None
                self.condition.notify_all()
        finally:
            for lock in reversed(transition_locks):
                lock.release()

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
            wait_ms, load_ms = self._activate_model(model, meta["cancel_event"] if meta else None)
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
    def __init__(self, path: Path, memory_limit: int = 100):
        self.path = path
        self.max_bytes = 10 * 1024 * 1024
        self.memory_limit = memory_limit
        self.lock = threading.Lock()
        self.recent: list[dict[str, Any]] = []

    def record(self, entry: dict[str, Any]):
        entry = {key: value for key, value in entry.items() if value is not None and not key.startswith("_")}
        with self.lock:
            self.recent.append(entry)
            self.recent = self.recent[-self.memory_limit:]
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                    self.path.replace(self.path.with_suffix(".jsonl.1"))
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, sort_keys=True) + "\n")
            except OSError:
                pass

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
                if entry.get("finish_reason") in {"stop", "tool_calls", "length", "eos", "complete"}
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
        return {"requests": requests, "summaries": summaries}


class DispatchHandler(BaseHTTPRequestHandler):
    server: DispatchHTTPServer

    def log_message(self, fmt, *args):
        return

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

    def _body(self) -> dict[str, Any]:
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise DispatchError("Content-Type must be application/json", 415, "invalid_request_error")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise DispatchError("invalid Content-Length", 400, "invalid_request_error") from exc
        if length <= 0 or length > self.server.dispatcher.config.max_body_bytes:
            raise DispatchError("invalid or oversized request body", 400, "invalid_request_error")
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise DispatchError(f"invalid JSON body: {exc}", 400, "invalid_request_error") from exc
        if not isinstance(payload, dict):
            raise DispatchError("JSON body must be an object", 400, "invalid_request_error")
        return payload

    def do_GET(self):
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
        elif self.path == "/v1/status":
            self._json(200, self.server.dispatcher.status())
        else:
            self._json(404, {"error": {"message": f"unknown path {self.path}", "type": "not_found"}})

    def do_POST(self):
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
                if int(self.headers.get("Content-Length", "0") or "0") > 0:
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
                if int(self.headers.get("Content-Length", "0") or "0") > 0:
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

        if self.path != "/v1/chat/completions":
            self._json(404, {"error": {"message": f"unknown path {self.path}", "type": "not_found"}})
            return

        request_started = False
        request_id = self.headers.get("X-InferenceDock-Request-ID") or uuid.uuid4().hex
        if len(request_id) > 128 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for ch in request_id):
            self._error(DispatchError("invalid X-InferenceDock-Request-ID", 400, "invalid_request_error"))
            return
        started_at = time.monotonic()
        metrics: dict[str, Any] = {"stream": False, "_started_at": started_at}
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
            metrics["stream"] = streaming
            payload["model"] = model.backend_model
            body = json.dumps(payload).encode("utf-8")
            queue_ms, cold_load_ms = self.server.dispatcher.begin_request(model_name, streaming, request_meta)
            metrics["queue_ms"] = round(queue_ms, 1)
            metrics["cold_load_ms"] = round(cold_load_ms, 1) if cold_load_ms is not None else None
            request_started = True
            upstream = self._open_upstream(model, body)
            self.server.dispatcher.set_upstream(request_id, upstream)
            try:
                response_bytes = 0
                metrics["response_bytes"] = 0
                response_sample = bytearray()
                cancelled = False
                if not streaming:
                    try:
                        response_body = upstream.read()
                    except TimeoutError as exc:
                        raise DispatchError("upstream response timed out", 504, "upstream_timeout") from exc
                    except (OSError, ValueError) as exc:
                        if request_meta["cancel_event"].is_set():
                            raise DispatchError("request cancelled", 409, "request_cancelled") from exc
                        raise DispatchError(f"upstream response failed: {exc}", 502, "upstream_unavailable") from exc
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
                            "stream": metrics.get("stream", False),
                            "duration_ms": duration_ms,
                            "queue_ms": metrics.get("queue_ms"),
                            "cold_load_ms": metrics.get("cold_load_ms"),
                            "ttft_ms": metrics.get("ttft_ms"),
                            "usage": metrics.get("usage"),
                            "cached_tokens": metrics.get("cached_tokens"),
                            "response_bytes": metrics.get("response_bytes"),
                            "sse_chunks": metrics.get("sse_chunks"),
                            "finish_reason": metrics.get("finish_reason"),
                            "tokens_per_second": metrics.get("tokens_per_second"),
                            "prefill_tokens_per_second": metrics.get("prefill_tokens_per_second"),
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
        metrics["sse_chunks"] = metrics.get("sse_chunks", 0) + 1
        usage = event.get("usage")
        if isinstance(usage, dict):
            metrics["usage"] = usage
            details = usage.get("prompt_tokens_details")
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
        usage = payload.get("usage")
        if isinstance(usage, dict):
            metrics["usage"] = usage
            details = usage.get("prompt_tokens_details")
            if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
                metrics["cached_tokens"] = details["cached_tokens"]
        timings = payload.get("timings")
        if isinstance(timings, dict):
            if isinstance(timings.get("predicted_per_second"), (int, float)):
                metrics["tokens_per_second"] = timings["predicted_per_second"]
            if isinstance(timings.get("prompt_per_second"), (int, float)):
                metrics["prefill_tokens_per_second"] = timings["prompt_per_second"]
        choices = payload.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            finish_reason = choices[0].get("finish_reason")
            if isinstance(finish_reason, str) and finish_reason:
                metrics["finish_reason"] = finish_reason

    def _open_upstream(self, model: ModelConfig, body: bytes):
        adapter = self.server.dispatcher.config.adapters[model.adapter]
        request = urllib.request.Request(endpoint_for(adapter) + "/v1/chat/completions", data=body, method="POST")
        request.add_header("Content-Type", "application/json")
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
            status, _ = http_json(endpoint_for(adapter) + adapter.health_path, timeout=config.connect_timeout_seconds)
            state = "healthy" if status == 200 else f"unhealthy:{status}"
            print(f"probe {adapter.name}: {state}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--check-config", action="store_true", help="validate config and exit")
    parser.add_argument("--probe", action="store_true", help="with --check-config, perform read-only health probes")
    args = parser.parse_args()
    try:
        config_path = args.config.resolve()
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
