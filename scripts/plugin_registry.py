#!/usr/bin/env python3
"""Safe, declarative runtime discovery for InferenceDock plugins."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
BUILTIN_DIR = ROOT / "plugins"
SAFE_FIELDS = {
    "model", "models", "model_id", "model_path", "endpoint", "host", "port", "aliases",
    "context_window", "max_tokens", "max_output_tokens", "vision", "concurrency",
    "max_active_requests", "idle_timeout_seconds", "cache_path", "cache_dir",
    "context", "output", "label", "path", "executable", "ssd_streaming", "symlink", "plist",
    "vision_model", "auxiliary_model", "batched_sessions", "backend_start_timeout_seconds", "dspark", "mtp_draft", "dspark_confidence", "dspark_strict", "environment",
    "advertise",
}
SECRET_KEY = re.compile(r"^(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|authorization|credential)$", re.I)
SECRET_VALUE = re.compile(r"(?i)(bearer\s+|https?://[^/\s:]+:)[^\s]+|([A-Za-z0-9_-]*(?:token|secret|password|key)[A-Za-z0-9_-]*\s*[=:]\s*)[^\s,]+")

# Candidate ladder from the execution plan. scan()/import_instance() may only
# emit the first three levels; "configured" requires a validated config preview,
# and the tested levels are recorded by the test phases, never by discovery.
CANDIDATE_LEVELS = ("untested", "registered", "discovered", "configured", "contract_tested", "real_tested")
STATUS_LABELS_ZH = {
    "not_found": "未安装或未登记",
    "registered": "已登记",
    "installed": "已登记（可执行文件已确认）",
    "not_running": "已登记，服务未运行",
    "running": "已发现（服务在线）",
    "reachable_unverified": "端口可达，服务身份未验证",
}
_STATUS_CANDIDATE_LEVEL = {
    "not_found": "untested",
    "registered": "registered",
    "installed": "registered",
    "not_running": "registered",
    "running": "discovered",
}


def _runtime_metadata(manifest: "Manifest", *, status: str, path: str | None, endpoint: str | None, version: str | None = None, source: str | None = None) -> dict[str, Any]:
    """Return stable, non-secret metadata for UI/API consumers.

    The manifest is the platform contract; each scan/import result is one
    installation and one runtime instance.  This is descriptive only and
    never implies that an untested action is available.
    """
    source = source or ("explicit" if path else ("manifest" if endpoint else "unresolved"))
    # Keep local filesystem details out of public metadata while preserving a
    # stable identity for executable-only installations.
    install_key = hashlib.sha256(path.encode("utf-8")).hexdigest()[:12] if path else "manifest"
    instance_key = endpoint or f"installation:{install_key}"
    return {
        "platform": {"id": manifest.id, "display_name": manifest.display_name},
        "installation": {"id": f"{manifest.id}:{install_key}", "version": version, "source": source},
        "instance": {
            "id": f"{manifest.id}:{instance_key}",
            "endpoint": endpoint,
            "mode": manifest.ownership.get("mode", manifest.service.get("mode", "observe")),
            "status": status,
        },
    }


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class Manifest:
    id: str
    display_name: str
    path: Path
    discover: dict[str, Any]
    service: dict[str, Any]
    models_source: str
    ownership: dict[str, Any]
    capabilities: dict[str, Any]
    metrics: dict[str, Any]
    exclusive_group: str | None
    keep_resident: bool
    repositories: list[dict[str, str]]
    trusted: bool = False


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{name} must be an object")
    return value


def _strings(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ManifestError(f"{name} must be an array of strings")
    return [v.strip() for v in value]


def _loopback_endpoint(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{name} must be a loopback http URL")
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.username or parsed.password:
            raise ValueError
        parsed.port
    except (ValueError, TypeError):
        raise ManifestError(f"{name} must be a loopback http URL")
    return value.rstrip("/")


def load_manifest(path: Path, *, trusted: bool = False) -> Manifest:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ManifestError(f"{path}: {exc}") from exc
    data = _mapping(raw, str(path))
    allowed_top = {"schema_version", "id", "display_name", "discover", "service", "ownership", "models_source", "capabilities", "metrics", "exclusive_group", "keep_resident", "repositories"}
    unknown_top = sorted(set(data) - allowed_top)
    if unknown_top:
        raise ManifestError(f"{path}: unknown field(s): {', '.join(unknown_top)}")
    if data.get("schema_version") != 1:
        raise ManifestError(f"{path}: schema_version must be 1")
    plugin_id = data.get("id")
    if not isinstance(plugin_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", plugin_id):
        raise ManifestError(f"{path}: id must be lowercase kebab-case")
    display_name = data.get("display_name", plugin_id)
    if not isinstance(display_name, str) or not display_name.strip():
        raise ManifestError(f"{path}: display_name must be non-empty")
    discover = _mapping(data.get("discover", {}), f"{path}: discover")
    unknown_discover = sorted(set(discover) - {"executables", "version_args", "config_files", "config_reader", "repo_paths"})
    if unknown_discover:
        raise ManifestError(f"{path}: unknown discover field(s): {', '.join(unknown_discover)}")
    executables = _strings(discover.get("executables", []), f"{path}: discover.executables")
    version_args = _strings(discover.get("version_args", []), f"{path}: discover.version_args")
    if any(arg.startswith(";") or arg.startswith("|") for arg in version_args):
        raise ManifestError(f"{path}: version_args contains shell syntax")
    config_files = _strings(discover.get("config_files", []), f"{path}: discover.config_files")
    service = _mapping(data.get("service", {}), f"{path}: service")
    unknown_service = sorted(set(service) - {"mode", "service_key", "endpoint", "health_path", "models_path", "catalog_args", "active_requests_path", "model_state_path", "protocol", "activation", "deactivation", "start_argv", "stop_strategy"})
    if unknown_service:
        raise ManifestError(f"{path}: unknown service field(s): {', '.join(unknown_service)}")
    mode = service.get("mode", "observe")
    if mode not in {"managed", "external", "observe"}:
        raise ManifestError(f"{path}: service.mode must be managed, external or observe")
    endpoint = service.get("endpoint")
    if endpoint is not None:
        _loopback_endpoint(endpoint, f"{path}: service.endpoint")
    if not executables and endpoint is None:
        raise ManifestError(f"{path}: declare discover.executables or service.endpoint")
    for path_field in ("models_path", "active_requests_path", "model_state_path"):
        route = service.get(path_field)
        if route is not None and (not isinstance(route, str) or not route.startswith("/") or ".." in Path(route).parts):
            raise ManifestError(f"{path}: service.{path_field} must be a safe relative URL path")
    catalog_args = service.get("catalog_args")
    if catalog_args is not None:
        catalog_args = _strings(catalog_args, f"{path}: service.catalog_args")
        if any(re.search(r"(?:;|\||&&|\||`|\$\()", arg) for arg in catalog_args):
            raise ManifestError(f"{path}: catalog_args contains shell syntax")
        service = {**service, "catalog_args": catalog_args}
    ownership = _mapping(data.get("ownership", {}), f"{path}: ownership")
    if ownership:
        unknown_ownership = sorted(set(ownership) - {"mode", "start_argv", "stop_strategy"})
        if unknown_ownership:
            raise ManifestError(f"{path}: unknown ownership field(s): {', '.join(unknown_ownership)}")
        mode = ownership.get("mode", mode)
        if mode not in {"managed", "external", "observe"}:
            raise ManifestError(f"{path}: ownership.mode is invalid")
    start_argv = ownership.get("start_argv", service.get("start_argv"))
    if start_argv is not None:
        start_argv = _strings(start_argv, f"{path}: start_argv")
        if any(re.search(r"(?:;|\||&&|\|\||`|\$\()", arg) for arg in start_argv):
            raise ManifestError(f"{path}: start_argv contains shell syntax")
        ownership = {**ownership, "start_argv": start_argv}
    capabilities = _mapping(data.get("capabilities", {}), f"{path}: capabilities")
    metrics = _mapping(data.get("metrics", {}), f"{path}: metrics")
    for action_name in ("activation", "deactivation"):
        action = service.get(action_name, {})
        if action is not None and not isinstance(action, dict):
            raise ManifestError(f"{path}: service.{action_name} must be an object")
        if isinstance(action, dict):
            unknown_action = sorted(set(action) - {"type", "path", "method", "payload"})
            if unknown_action:
                raise ManifestError(f"{path}: unknown {action_name} field(s): {', '.join(unknown_action)}")
            if action.get("type") not in {None, "http", "argv"}:
                raise ManifestError(f"{path}: {action_name}.type must be http or argv")
            if action.get("type") == "http":
                path_value = action.get("path")
                if not isinstance(path_value, str) or not path_value.startswith("/") or ".." in Path(path_value).parts:
                    raise ManifestError(f"{path}: {action_name}.path must be a safe relative URL path")
            if action.get("method") is not None and action["method"] not in {"GET", "POST"}:
                raise ManifestError(f"{path}: {action_name}.method must be GET or POST")
    repositories_raw = data.get("repositories", [])
    if not isinstance(repositories_raw, list):
        raise ManifestError(f"{path}: repositories must be an array")
    repositories: list[dict[str, str]] = []
    for index, item in enumerate(repositories_raw):
        if isinstance(item, str):
            item = {"remote": item}
        if not isinstance(item, dict):
            raise ManifestError(f"{path}: repositories[{index}] must be a URL or object")
        unknown_repo = sorted(set(item) - {"path", "remote", "branch"})
        if unknown_repo:
            raise ManifestError(f"{path}: unknown repository field(s): {', '.join(unknown_repo)}")
        remote = item.get("remote")
        if remote is not None and (not isinstance(remote, str) or not remote.startswith(("https://", "git@")) or re.search(r"[\s;|&`$]", remote)):
            raise ManifestError(f"{path}: repository remote must be https or ssh URL")
        repo_path = item.get("path")
        branch = item.get("branch", "HEAD")
        if repo_path is not None and (not isinstance(repo_path, str) or not repo_path.strip() or re.search(r"[\x00\n\r]", repo_path)):
            raise ManifestError(f"{path}: repository path is invalid")
        if not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch):
            raise ManifestError(f"{path}: repository branch is invalid")
        repositories.append({k: str(v) for k, v in item.items() if v is not None})
    discover = {**discover, "executables": executables, "version_args": version_args, "config_files": config_files, "repo_paths": _strings(discover.get("repo_paths", []), f"{path}: discover.repo_paths")}
    exclusive_group = data.get("exclusive_group")
    if exclusive_group is not None and (not isinstance(exclusive_group, str) or not exclusive_group.strip()):
        raise ManifestError(f"{path}: exclusive_group must be a non-empty string")
    keep_resident = data.get("keep_resident", False)
    if not isinstance(keep_resident, bool):
        raise ManifestError(f"{path}: keep_resident must be boolean")
    return Manifest(plugin_id, display_name.strip(), path, {**discover, "executables": executables, "version_args": version_args, "config_files": config_files}, service, str(data.get("models_source", "none")), ownership, capabilities, metrics, exclusive_group, keep_resident, repositories, trusted)


def load_manifests(directory: Path = BUILTIN_DIR, *, include_external: bool = True) -> tuple[list[Manifest], list[dict[str, str]]]:
    manifests: list[Manifest] = []
    errors: list[dict[str, str]] = []
    for path in sorted(directory.glob("*/manifest.yaml")):
        try:
            manifests.append(load_manifest(path, trusted=directory.resolve() == BUILTIN_DIR.resolve()))
        except ManifestError as exc:
            errors.append({"path": str(path), "error": str(exc)})
    if include_external:
        external = Path.home() / "Library/Application Support/InferenceDock/plugins"
        if external != directory and external.is_dir():
            for path in sorted(external.glob("*/manifest.yaml")):
                try:
                    manifests.append(load_manifest(path, trusted=False))
                except ManifestError as exc:
                    errors.append({"path": str(path), "error": str(exc)})
    return manifests, errors


def _find_executable(token: str, roots: list[Path], explicit: dict[str, Path]) -> Path | None:
    expanded = Path(token).expanduser()
    if token.startswith("/") or "/" in token or token.startswith("~"):
        if expanded.is_file() and os.access(expanded, os.X_OK):
            return expanded.resolve()
        for root in roots:
            candidate = (root / token.lstrip("/")).expanduser()
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate.resolve()
        return None
    for root in roots:
        direct = root / token
        if direct.is_file() and os.access(direct, os.X_OK):
            return direct.resolve()
        for base, dirs, files in os.walk(root):
            depth = len(Path(base).relative_to(root).parts)
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            if depth >= 3:
                dirs[:] = []
            if token in files:
                candidate = Path(base) / token
                if os.access(candidate, os.X_OK):
                    return candidate.resolve()
    found = shutil.which(token)
    return Path(found).resolve() if found else None


def _probe_version(manifest: Manifest, executable: Path) -> str | None:
    if not manifest.trusted or not manifest.discover.get("version_args"):
        return None
    try:
        proc = subprocess.run([str(executable), *manifest.discover["version_args"]], capture_output=True, text=True, timeout=3, check=False, shell=False)
    except (OSError, subprocess.SubprocessError):
        return None
    output = (proc.stdout or proc.stderr).strip().splitlines()
    if not output:
        return None
    return _redact(output[0][:200])


def _redact(value: str) -> str:
    return SECRET_VALUE.sub(r"\1[redacted]", value)


def _endpoint_probe(service: dict[str, Any]) -> dict[str, Any]:
    """Probe reachability separately from service identity.

    A 404/500 proves only that something answered on the port. Identity is
    verified only from a declared health/model response shape.
    """
    endpoint = service.get("endpoint")
    if not endpoint:
        return {"reachable": None, "identity_verified": False, "status_code": None}
    url = endpoint.rstrip("/") + str(service.get("health_path", "/health"))
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, new):
            return None
    try:
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(url, timeout=0.5) as response:
            body = response.read(65536)
            return {
                "reachable": True,
                "identity_verified": _identity_from_health(service, response.status, body),
                "status_code": response.status,
            }
    except urllib.error.HTTPError as exc:
        # Any HTTP response, even an error status, proves the port is reachable
        # but never verifies that the expected server owns it.
        return {"reachable": True, "identity_verified": False, "status_code": exc.code}
    except (OSError, urllib.error.URLError):
        return {"reachable": False, "identity_verified": False, "status_code": None}


def _identity_from_health(service: dict[str, Any], status: int, body: bytes) -> bool:
    if status != 200:
        return False
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    # Health routes identify themselves through a conventional status value;
    # model catalog routes identify the expected API shape.
    state = payload.get("status")
    if isinstance(state, str) and state.lower() in {"ok", "healthy", "ready"}:
        return True
    data = payload.get("data")
    if isinstance(data, list) and all(isinstance(item, dict) and isinstance(item.get("id"), str) for item in data):
        return True
    models = payload.get("models")
    return isinstance(models, list) and all(isinstance(item, dict) and isinstance(item.get("name", item.get("id")), str) for item in models)


def _endpoint_running(service: dict[str, Any]) -> bool | None:
    """Backward-compatible alias for reachability, not identity."""
    return _endpoint_probe(service)["reachable"]


def _safe_config(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            return None
        if path.suffix.lower() in {".yaml", ".yml"}:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
        elif path.suffix.lower() == ".json":
            value = json.loads(path.read_text(encoding="utf-8"))
        elif path.suffix.lower() == ".toml":
            value = tomllib.loads(path.read_text(encoding="utf-8"))
        elif path.suffix.lower() == ".plist":
            value = plistlib.loads(path.read_bytes())
        else:
            return None
    except (OSError, ValueError, TypeError, yaml.YAMLError, plistlib.InvalidFileException):
        return None
    return value if isinstance(value, dict) else None


def _filter_fields(value: Any, *, named_entries: bool = False) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if SECRET_KEY.search(str(key)):
                continue
            if named_entries or key in SAFE_FIELDS:
                out[key] = _filter_fields(item, named_entries=(key in {"models", "aliases"}))
            elif isinstance(item, dict):
                # Keep unknown containers only when something inside survives the allowlist.
                filtered = _filter_fields(item)
                if filtered:
                    out[key] = filtered
        return out
    if isinstance(value, list):
        return [item for item in (_filter_fields(entry) for entry in value[:100]) if item is not None]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return _redact(str(value)) if isinstance(value, str) else value
    return None


def import_preview(manifest: Manifest, roots: list[Path]) -> list[dict[str, Any]]:
    result = []
    for token in manifest.discover.get("config_files", []):
        path = Path(token).expanduser()
        if not path.is_file() and not path.is_absolute():
            path = next((root / token for root in roots if (root / token).is_file()), path)
        if not path.is_file():
            continue
        data = _safe_config(path)
        if data is None:
            continue
        fields = _filter_fields(data)
        result.append({"path": str(path.resolve()), "fields": fields})
    return result


def scan(manifests: list[Manifest], *, roots: list[Path] | None = None, explicit: dict[str, Path] | None = None, probe_version: bool = False) -> list[dict[str, Any]]:
    roots = [r.expanduser().resolve() for r in (roots or []) if r.expanduser().is_dir()]
    explicit = explicit or {}
    observed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    output = []
    for manifest in manifests:
        executable = explicit.get(manifest.id)
        if executable is not None:
            candidate = executable.expanduser().resolve()
            if candidate.is_file() and os.access(candidate, os.X_OK):
                executable = candidate
            else:
                executable = None
        if executable is None:
            for token in manifest.discover["executables"]:
                executable = _find_executable(token, roots, explicit)
                if executable:
                    break
        endpoint_probe = _endpoint_probe(manifest.service)
        running = endpoint_probe["reachable"]
        identity_verified = endpoint_probe["identity_verified"]
        if identity_verified:
            status = "running"
        elif running:
            status = "reachable_unverified"
        elif executable:
            status = "installed"
        elif manifest.service.get("endpoint"):
            status = "not_running"
        else:
            status = "not_found"
        executable_path = str(executable) if executable else None
        version = _probe_version(manifest, executable) if probe_version and executable else None
        item = {"id": manifest.id, "display_name": manifest.display_name, "status": status, "status_label": STATUS_LABELS_ZH[status], "candidate_level": _STATUS_CANDIDATE_LEVEL.get(status, "registered"), "service_mode": manifest.service.get("mode", "observe"), "path": executable_path, "version": version, "endpoint_running": running, "endpoint_reachable": running, "identity_verified": identity_verified, "endpoint_status_code": endpoint_probe.get("status_code"), "observed_at": observed_at, "import_preview": import_preview(manifest, roots)}
        source = "explicit" if manifest.id in explicit else ("discovered" if executable_path else None)
        item.update(_runtime_metadata(manifest, status=status, path=executable_path, endpoint=manifest.service.get("endpoint"), version=version, source=source))
        output.append(item)
    return output


def import_instance(manifest: Manifest, *, executable: Path | None = None, endpoint: str | None = None, config_path: Path | None = None, source_dir: Path | None = None) -> dict[str, Any]:
    """Register an explicitly user-supplied install as a reviewable candidate.

    Only the exact paths given here are checked: no PATH lookup, no directory
    walk, no service start. The result is a candidate, not an activation.
    """
    notes: list[str] = []
    resolved_exe: Path | None = None
    resolved_source: Path | None = None
    if executable is not None:
        candidate = Path(executable).expanduser()
        if not candidate.is_absolute():
            raise ManifestError("import executable path must be absolute")
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ManifestError(f"import path is not an executable file: {candidate}")
        resolved_exe = candidate.resolve()
    if source_dir is not None:
        candidate_source = Path(source_dir).expanduser()
        if not candidate_source.is_absolute():
            raise ManifestError("import source directory must be absolute")
        if not candidate_source.is_dir():
            raise ManifestError(f"import source directory not found: {candidate_source}")
        resolved_source = candidate_source.resolve()
        notes.append("源码目录仅作为候选路径记录；不会递归扫描、执行或修改其中内容")
    if executable is None and source_dir is None and manifest.discover.get("executables"):
        notes.append("未提供可执行文件路径；本次只登记服务信息")
    effective_endpoint = manifest.service.get("endpoint")
    if endpoint is not None:
        effective_endpoint = _loopback_endpoint(endpoint, "import endpoint")
        if manifest.service.get("endpoint") and effective_endpoint != str(manifest.service["endpoint"]).rstrip("/"):
            notes.append("导入端点与插件默认端点不同；请确认这是同一个服务的实际监听地址")
    config_preview = None
    if config_path is not None:
        candidate_config = Path(config_path).expanduser()
        if not candidate_config.is_file():
            raise ManifestError(f"import config file not found: {candidate_config}")
        data = _safe_config(candidate_config)
        if data is None:
            raise ManifestError(f"import config is not a supported mapping file: {candidate_config}")
        config_preview = {"path": str(candidate_config.resolve()), "fields": _filter_fields(data)}
    endpoint_probe = _endpoint_probe({**manifest.service, "endpoint": effective_endpoint}) if effective_endpoint else {"reachable": None, "identity_verified": False, "status_code": None}
    running = endpoint_probe["reachable"]
    identity_verified = endpoint_probe["identity_verified"]
    if executable is None and endpoint is None and config_path is None and source_dir is None:
        raise ManifestError("import needs at least one explicit executable, source directory, endpoint or config")
    if identity_verified:
        status = "running"
    elif running:
        status = "reachable_unverified"
    elif resolved_exe is not None or resolved_source is not None:
        status = "installed"
    elif effective_endpoint is not None:
        status = "not_running"
    else:
        status = "registered"
    result = {
        "id": manifest.id,
        "display_name": manifest.display_name,
        "status": status,
        "status_label": STATUS_LABELS_ZH[status],
        "candidate_level": _STATUS_CANDIDATE_LEVEL[status],
        "service_mode": manifest.service.get("mode", "observe"),
        "path": str(resolved_exe) if resolved_exe else None,
        "source_dir": str(resolved_source) if resolved_source else None,
        "endpoint": effective_endpoint,
        "endpoint_running": running,
        "endpoint_reachable": running,
        "identity_verified": identity_verified,
        "endpoint_status_code": endpoint_probe.get("status_code"),
        "config_preview": config_preview,
        "notes_zh": notes,
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    metadata_path = str(resolved_exe or resolved_source) if (resolved_exe or resolved_source) else None
    result.update(_runtime_metadata(manifest, status=status, path=metadata_path, endpoint=effective_endpoint))
    return result


def check(manifests: list[Manifest], errors: list[dict[str, str]]) -> dict[str, Any]:
    ids = [m.id for m in manifests]
    if len(ids) != len(set(ids)):
        errors = [*errors, {"path": "", "error": "duplicate manifest id"}]
    return {"ok": not errors, "manifests": [{"id": m.id, "path": str(m.path)} for m in manifests], "errors": errors}


def _parse_paths(values: list[str]) -> dict[str, Path]:
    result = {}
    for value in values:
        if "=" not in value:
            raise ValueError("--path expects id=executable")
        key, path = value.split("=", 1)
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", key) or not path:
            raise ValueError("--path expects id=executable")
        result[key] = Path(path)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "scan", "preview", "import-instance"):
        p = sub.add_parser(name)
        p.add_argument("--plugins-dir", type=Path, default=BUILTIN_DIR)
        p.add_argument("--root", action="append", type=Path, default=[])
        p.add_argument("--path", action="append", default=[], metavar="ID=EXECUTABLE")
        p.add_argument("--probe-version", action="store_true", help="run the manifest's argv version check for built-ins")
        p.add_argument("--pretty", action="store_true")
        p.add_argument("--id", help="plugin id for import-instance")
        p.add_argument("--executable", type=Path, help="explicit executable path for import-instance")
        p.add_argument("--endpoint", help="explicit loopback endpoint for import-instance")
        p.add_argument("--config", type=Path, help="explicit config file for import-instance")
        p.add_argument("--source-dir", type=Path, help="explicit source/build directory for import-instance")
    args = parser.parse_args(argv)
    manifests, errors = load_manifests(args.plugins_dir)
    if args.command == "check":
        result = check(manifests, errors)
    elif args.command == "import-instance":
        if not args.id:
            parser.error("import-instance requires --id")
        manifest = next((m for m in manifests if m.id == args.id), None)
        if manifest is None:
            parser.error(f"unknown plugin id: {args.id}")
        try:
            result = {"instance": import_instance(manifest, executable=args.executable, endpoint=args.endpoint, config_path=args.config, source_dir=args.source_dir), "errors": errors}
        except ManifestError as exc:
            print(json.dumps({"instance": None, "errors": [*errors, {"path": "", "error": str(exc)}]}, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
            return 1
    else:
        try:
            explicit = _parse_paths(args.path)
        except ValueError as exc:
            parser.error(str(exc))
        result = {"results": scan(manifests, roots=args.root, explicit=explicit, probe_version=args.probe_version), "errors": errors}
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return 0 if (result.get("ok", not errors)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
