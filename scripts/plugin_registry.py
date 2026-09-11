#!/usr/bin/env python3
"""Safe, declarative runtime discovery for InferenceDock plugins."""

from __future__ import annotations

import argparse
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
    unknown_service = sorted(set(service) - {"mode", "service_key", "endpoint", "health_path", "models_path", "protocol", "activation", "deactivation", "start_argv", "stop_strategy"})
    if unknown_service:
        raise ManifestError(f"{path}: unknown service field(s): {', '.join(unknown_service)}")
    mode = service.get("mode", "observe")
    if mode not in {"managed", "external", "observe"}:
        raise ManifestError(f"{path}: service.mode must be managed, external or observe")
    endpoint = service.get("endpoint")
    if endpoint is not None:
        try:
            parsed = urlsplit(endpoint)
            if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.username or parsed.password:
                raise ValueError
            parsed.port
        except (ValueError, TypeError):
            raise ManifestError(f"{path}: service.endpoint must be loopback http URL")
    if not executables and endpoint is None:
        raise ManifestError(f"{path}: declare discover.executables or service.endpoint")
    models_path = service.get("models_path")
    if models_path is not None and (not isinstance(models_path, str) or not models_path.startswith("/") or ".." in Path(models_path).parts):
        raise ManifestError(f"{path}: service.models_path must be a safe relative URL path")
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


def _endpoint_running(service: dict[str, Any]) -> bool | None:
    endpoint = service.get("endpoint")
    if not endpoint:
        return None
    url = endpoint.rstrip("/") + str(service.get("health_path", "/health"))
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, new):
            return None
    try:
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(url, timeout=0.5) as response:
            return 200 <= response.status < 500
    except urllib.error.HTTPError:
        # Any HTTP response, even an error status, proves the service is alive.
        return True
    except (OSError, urllib.error.URLError):
        return False


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
        running = _endpoint_running(manifest.service)
        if running:
            status = "running"
        elif executable:
            status = "installed"
        elif manifest.service.get("endpoint"):
            status = "not_running"
        else:
            status = "not_found"
        item = {"id": manifest.id, "display_name": manifest.display_name, "status": status, "service_mode": manifest.service.get("mode", "observe"), "path": str(executable) if executable else None, "version": _probe_version(manifest, executable) if probe_version and executable else None, "endpoint_running": running, "observed_at": observed_at, "import_preview": import_preview(manifest, roots)}
        output.append(item)
    return output


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
    for name in ("check", "scan", "preview"):
        p = sub.add_parser(name)
        p.add_argument("--plugins-dir", type=Path, default=BUILTIN_DIR)
        p.add_argument("--root", action="append", type=Path, default=[])
        p.add_argument("--path", action="append", default=[], metavar="ID=EXECUTABLE")
        p.add_argument("--probe-version", action="store_true", help="run the manifest's argv version check for built-ins")
        p.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    manifests, errors = load_manifests(args.plugins_dir)
    if args.command == "check":
        result = check(manifests, errors)
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
