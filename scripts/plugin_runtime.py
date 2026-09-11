#!/usr/bin/env python3
"""Compile discovered plugin manifests into a read-only runtime config preview."""

from __future__ import annotations

import argparse
import json
import plistlib
import re
import time
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_dispatch
import plugin_registry


LIFECYCLE_ACTIONS = ("discover", "health", "list_models", "active_requests", "model_state", "start", "load", "unload", "cancel", "stop")


def _action(available: bool, kind: str | None, detail: Any, note_zh: str) -> dict[str, Any]:
    return {"available": available, "kind": kind, "detail": detail, "note_zh": note_zh}


def lifecycle_template(manifest: plugin_registry.Manifest) -> dict[str, dict[str, Any]]:
    """Normalize a manifest into a per-action lifecycle contract preview.

    Every action states whether the plugin can perform it and why not when it
    cannot. Unavailable actions are honest gaps, never guessed routes.
    """
    service = manifest.service
    ownership = manifest.ownership or {}
    mode = ownership.get("mode", service.get("mode", "observe"))
    start_argv = ownership.get("start_argv") or service.get("start_argv")
    stop_strategy = ownership.get("stop_strategy") or service.get("stop_strategy")
    activation = service.get("activation") if isinstance(service.get("activation"), dict) and service.get("activation") else None
    deactivation = service.get("deactivation") if isinstance(service.get("deactivation"), dict) and service.get("deactivation") else None

    actions: dict[str, dict[str, Any]] = {}
    actions["discover"] = _action(True, "registry", None, "仅检查显式路径、已登记根目录与已声明配置，不扫描全盘")
    if service.get("health_path"):
        actions["health"] = _action(True, "http", {"method": "GET", "path": service["health_path"]}, "健康检查")
    else:
        actions["health"] = _action(False, None, None, "未声明健康检查路径")
    if service.get("models_path"):
        actions["list_models"] = _action(True, "http", {"method": "GET", "path": service["models_path"]}, "模型目录列表；不等于驻留状态")
    elif service.get("catalog_args"):
        actions["list_models"] = _action(True, "argv", list(service["catalog_args"]), "通过已登记程序只读检查本地模型目录")
    else:
        actions["list_models"] = _action(False, None, None, "未声明模型列表接口")
    if service.get("active_requests_path"):
        actions["active_requests"] = _action(True, "http", {"method": "GET", "path": service["active_requests_path"]}, "在途请求探针")
    else:
        actions["active_requests"] = _action(False, None, None, "缺少在途请求探针；切换前无法确认空闲时按忙碌处理")
    if service.get("model_state_path"):
        actions["model_state"] = _action(True, "http", {"method": "GET", "path": service["model_state_path"]}, "驻留状态探针")
    else:
        actions["model_state"] = _action(False, None, None, "缺少驻留状态探针；卸载结果不可验证时按失败处理")
    if mode == "managed":
        if start_argv:
            actions["start"] = _action(True, "argv", list(start_argv), "按登记的 argv 启动服务")
        else:
            actions["start"] = _action(False, None, None, "缺少已验证的启动命令，保持草稿状态")
    else:
        actions["start"] = _action(False, None, None, "外部/常驻服务不由 InferenceDock 启动")
    if activation and mode != "observe":
        actions["load"] = _action(True, activation.get("type"), activation, "通过插件声明的原生路由加载模型")
    else:
        actions["load"] = _action(False, None, None, "未声明原生加载路由，不做能力假设")
    if deactivation and mode != "observe":
        actions["unload"] = _action(True, deactivation.get("type"), deactivation, "通过插件声明的原生路由卸载模型")
    else:
        actions["unload"] = _action(False, None, None, "未声明原生卸载路由，不做能力假设")
    actions["cancel"] = _action(False, None, None, "取消通过关闭请求连接实现；插件未声明原生取消接口")
    if mode == "managed" and stop_strategy:
        actions["stop"] = _action(True, "strategy", stop_strategy, "按登记策略停止本程序启动的服务")
    else:
        actions["stop"] = _action(False, None, None, "不停止外部/常驻服务")
    return actions


def compile_runtime(manifests: list[plugin_registry.Manifest], results: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {manifest.id: manifest for manifest in manifests}
    adapters: dict[str, Any] = {}
    models: dict[str, Any] = {}
    warnings: list[str] = []
    seen_models: dict[tuple[str, str, str], str] = {}
    for item in results:
        manifest = by_id[item["id"]]
        mode = manifest.ownership.get("mode", manifest.service.get("mode", "observe"))
        entry: dict[str, Any] = {"type": mode}
        if manifest.service.get("endpoint"):
            entry["endpoint"] = manifest.service["endpoint"]
        if manifest.service.get("health_path"):
            entry["health_path"] = manifest.service["health_path"]
        if manifest.service.get("models_path"):
            entry["models_path"] = manifest.service["models_path"]
        if manifest.service.get("catalog_args"):
            entry["catalog_args"] = list(manifest.service["catalog_args"])
        for path_field in ("active_requests_path", "model_state_path"):
            if manifest.service.get(path_field):
                entry[path_field] = manifest.service[path_field]
        if mode == "managed":
            start_argv = manifest.ownership.get("start_argv") or manifest.service.get("start_argv")
            if start_argv:
                entry["command"] = list(start_argv)
            else:
                # A managed adapter without a verified command stays a draft.
                entry["unresolved"] = ["start_argv"]
        if manifest.exclusive_group:
            entry["exclusive_group"] = manifest.exclusive_group
        if manifest.keep_resident:
            entry["keep_resident"] = True
        instance = item.get("instance") if isinstance(item.get("instance"), dict) else {}
        installation = item.get("installation") if isinstance(item.get("installation"), dict) else {}
        platform = item.get("platform") if isinstance(item.get("platform"), dict) else {}
        entry["platform"] = platform or {"id": manifest.id, "display_name": manifest.display_name}
        entry["installation"] = installation
        entry["instance"] = instance or {"id": manifest.id, "endpoint": entry.get("endpoint"), "mode": mode}
        adapters[manifest.id] = entry
        reader = manifest.discover.get("config_reader")
        if reader == "ds4_runtime":
            for preview in item.get("import_preview", []):
                generated = compile_ds4_models(preview["fields"])
                instance_id = str((instance or {}).get("id", manifest.id))
                for model_id, model in generated.items():
                    backend_id = str(model.get("backend_model", model_id))
                    key = (manifest.id, instance_id, backend_id)
                    previous = seen_models.get(key)
                    if previous is not None:
                        models[previous].setdefault("aliases", []).append(model_id)
                        continue
                    model = dict(model)
                    model.setdefault("backend_model", backend_id)
                    model.setdefault("instance_id", instance_id)
                    seen_models[key] = model_id
                    models[model_id] = model
        elif reader:
            warnings.append(f"{manifest.id}: unknown config_reader {reader}")
    for name, adapter in adapters.items():
        for missing in adapter.get("unresolved", []):
            warnings.append(f"{name}: missing {missing} (draft)")
    groups = {
        name: {"capacity": 1}
        for name in {a.get("exclusive_group") for a in adapters.values()} | {m.get("resource_group") for m in models.values()}
        if name
    }
    return {"adapters": adapters, "models": models, "resource_groups": groups, "warnings": warnings}


def _ds4_model(variant: dict[str, Any], service: dict[str, Any]) -> dict[str, Any]:
    runtime = variant.get("runtime") if isinstance(variant.get("runtime"), dict) else {}
    entry: dict[str, Any] = {
        "adapter": "ds4",
        "lifecycle_owner": "ds4-router",
        "activate_path": "/_router/switch",
        "resource_group": "large-local",
        "capabilities": {"chat": True, "stream": True, "vision": bool(runtime.get("vision_model"))},
    }
    backend_model = service.get("model") or variant.get("model")
    if isinstance(backend_model, str) and backend_model.strip():
        entry["backend_model"] = backend_model.strip()
    if variant.get("advertise") is False:
        entry["advertise"] = False
    context = variant.get("context") or service.get("context")
    output = variant.get("output") or service.get("output")
    if isinstance(context, int) and context > 0:
        entry["context_window"] = context
    if isinstance(output, int) and output > 0:
        entry["max_output_tokens"] = output
    return entry


def compile_ds4_models(fields: dict[str, Any]) -> dict[str, Any]:
    models: dict[str, Any] = {}
    variants = fields.get("variants") if isinstance(fields.get("variants"), dict) else {}
    services = fields.get("services") if isinstance(fields.get("services"), dict) else {}
    aliases = fields.get("aliases") if isinstance(fields.get("aliases"), dict) else {}
    for alias, target in aliases.items():
        service_name = target[0] if isinstance(target, list) and target else None
        service = services.get(service_name, {}) if isinstance(service_name, str) else {}
        variant = variants.get(service.get("model"), {}) if isinstance(service.get("model"), str) else {}
        models[alias] = _ds4_model(variant, service)
    for variant_id, variant in variants.items():
        if not isinstance(variant, dict) or variant.get("advertise") is False or variant_id in models:
            continue
        models[variant_id] = _ds4_model(variant, {})
    return models


DS4_RUNTIME_FLAGS = (
    ("ssd_streaming", "--ssd-streaming", 0),
    ("ssd_streaming_cache_experts", "--ssd-streaming-cache-experts", 1),
    ("auxiliary_model", "--mtp-model", 1),
    ("dspark", "--dspark", 0),
    ("mtp_draft", "--mtp-draft", 1),
    ("dspark_confidence", "--dspark-confidence", 1),
    ("dspark_strict", "--dspark-strict", 0),
    ("vision_model", "--vision", 1),
)


def ds4_variant_argv(variant: dict[str, Any]) -> list[str]:
    runtime = variant.get("runtime") if isinstance(variant.get("runtime"), dict) else {}
    argv: list[str] = []
    for key, flag, takes_value in DS4_RUNTIME_FLAGS:
        value = runtime.get(key)
        if value is None or value is False:
            continue
        argv.append(flag)
        if takes_value:
            argv.append(str(value))
    return argv


def _validate_runtime_options(runtime: dict[str, Any], name: str) -> list[str]:
    known = {key for key, _, _ in DS4_RUNTIME_FLAGS} | {"ssd_streaming_cache_experts", "environment"}
    unknown = sorted(set(runtime) - known)
    if unknown:
        raise ValueError(f"{name}.runtime has unknown field(s): {', '.join(unknown)}")
    for key in ("ssd_streaming", "dspark", "dspark_strict"):
        if key in runtime and not isinstance(runtime[key], bool):
            raise ValueError(f"{name}.runtime.{key} must be a boolean")
    cache_experts = runtime.get("ssd_streaming_cache_experts")
    if cache_experts is not None and not (isinstance(cache_experts, str) and cache_experts.strip()):
        raise ValueError(f"{name}.runtime.ssd_streaming_cache_experts must be a non-empty string")
    for key in ("vision_model", "auxiliary_model"):
        if runtime.get(key) is not None and not (isinstance(runtime[key], str) and runtime[key].strip()):
            raise ValueError(f"{name}.runtime.{key} must be a non-empty path string")
    confidence = runtime.get("dspark_confidence")
    if confidence is not None and (
        isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1
    ):
        # ds4-server parse_float_arg bounds --dspark-confidence to [0, 1].
        raise ValueError(f"{name}.runtime.dspark_confidence must be a number between 0 and 1")
    draft = runtime.get("mtp_draft")
    if draft is not None and (isinstance(draft, bool) or not isinstance(draft, int) or draft < 1):
        raise ValueError(f"{name}.runtime.mtp_draft must be a positive integer")
    # ds4 aborts with "--dspark requires --mtp-model FILE"; --dspark-strict/--dspark-confidence imply --dspark.
    if (runtime.get("dspark") or runtime.get("dspark_strict") or confidence is not None) and not runtime.get("auxiliary_model"):
        raise ValueError(f"{name}.runtime: dspark options require auxiliary_model (ds4 --mtp-model)")
    environment = runtime.get("environment", {})
    if environment is not None:
        if not isinstance(environment, dict):
            raise ValueError(f"{name}.runtime.environment must be an object")
        for key, value in environment.items():
            if not isinstance(key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", key) or plugin_registry.SECRET_KEY.search(key):
                raise ValueError(f"{name}.runtime.environment contains unsafe key")
            if not isinstance(value, (str, int, float, bool)):
                raise ValueError(f"{name}.runtime.environment values must be scalar")
    return unknown


def ds4_plist_preview(service: dict[str, Any], variant_id: str, variant: dict[str, Any]) -> dict[str, Any]:
    arguments = [
        "ds4-server",
        "--model", str(service.get("symlink", "ds4flash.gguf")),
        "--port", str(service.get("port", "unknown")),
        "--ctx", str(variant.get("context", "unknown")),
        "--tokens", str(variant.get("output", "unknown")),
        *ds4_variant_argv(variant),
    ]
    plist: dict[str, Any] = {
        "Label": service.get("label", f"inferencedock.ds4.{variant_id}"),
        "ProgramArguments": arguments,
        "RunAtLoad": False,
        "KeepAlive": False,
    }
    runtime = variant.get("runtime") if isinstance(variant.get("runtime"), dict) else {}
    environment = runtime.get("environment")
    if isinstance(environment, dict) and environment:
        plist["EnvironmentVariables"] = {str(k): str(v) for k, v in environment.items()}
    return plist


def plan_actions(manifest: plugin_registry.Manifest, item: dict[str, Any]) -> dict[str, Any]:
    plan: dict[str, Any] = {"id": manifest.id, "warnings": []}
    plan["lifecycle"] = lifecycle_template(manifest)
    activation = manifest.service.get("activation")
    if isinstance(activation, dict) and activation:
        plan["activation_template"] = activation
    deactivation = manifest.service.get("deactivation")
    if isinstance(deactivation, dict) and deactivation:
        plan["deactivation_template"] = deactivation
    mode = manifest.ownership.get("mode", manifest.service.get("mode", "observe"))
    if mode == "managed":
        start_argv = manifest.ownership.get("start_argv") or manifest.service.get("start_argv")
        if start_argv:
            plan["start"] = {"type": "argv", "argv": list(start_argv)}
        if manifest.ownership.get("stop_strategy") or manifest.service.get("stop_strategy"):
            plan["stop"] = {"type": "strategy", "strategy": manifest.ownership.get("stop_strategy", manifest.service.get("stop_strategy"))}
    if manifest.discover.get("config_reader") != "ds4_runtime":
        return plan
    previews = item.get("import_preview", [])
    if not previews:
        plan["warnings"].append("no DS4 runtime config preview available")
        return plan
    fields = previews[0]["fields"]
    router = fields.get("router", {})
    plan["router"] = {
        key: router[key]
        for key in ("host", "port", "idle_timeout_seconds", "backend_start_timeout_seconds")
        if key in router
    }
    models = compile_ds4_models(fields)
    plan["activation"] = [
        {"model": name, "http": {"method": "POST", "path": "/_router/switch", "payload": {"model": name}}}
        for name in sorted(models)
    ]
    variants = fields.get("variants", {})
    services = fields.get("services", {})
    service = services.get("ds4", {}) if isinstance(services.get("ds4"), dict) else {}
    variant_plans = {}
    for variant_id, variant in sorted(variants.items()):
        if not isinstance(variant, dict):
            continue
        variant_plans[variant_id] = {
            "context": variant.get("context"),
            "output": variant.get("output"),
            "advertise": variant.get("advertise", True),
            "argv_fragment": ds4_variant_argv(variant),
            "plist_target": Path(str(service.get("plist", f"ds4-{variant_id}.plist"))).name,
        }
    plan["variants"] = variant_plans
    plan["rollback"] = {
        "capture": ["symlink_target", "plist_context", "plist_runtime_options"],
        "restore_order": ["stop_service", "restore_symlink", "restore_plist", "start_service"],
    }
    return plan


def write_plan(plan: dict[str, Any], out_dir: Path, fields: dict[str, Any] | None) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    if any(out_dir.iterdir()):
        raise ValueError(f"plan output directory must be empty: {out_dir}")
    written = []
    if fields and plan.get("variants"):
        services = fields.get("services", {})
        service = services.get("ds4", {}) if isinstance(services.get("ds4"), dict) else {}
        paths = fields.get("paths", {})
        if isinstance(paths, dict) and paths.get("symlink"):
            service = {**service, "symlink": paths["symlink"]}
        variants = fields.get("variants", {})
        for variant_id in plan["variants"]:
            variant = variants.get(variant_id)
            if not isinstance(variant, dict):
                continue
            plist_path = out_dir / plan["variants"][variant_id]["plist_target"].replace(".plist", f".{variant_id}.plist")
            plist_path.write_bytes(plistlib.dumps(ds4_plist_preview(service, variant_id, variant), sort_keys=False))
            written.append(str(plist_path))
    plan_path = out_dir / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    written.append(str(plan_path))
    return written


def _replace_arg(argv: list[str], flag: str, value: str) -> None:
    if flag in argv:
        index = argv.index(flag)
        if index + 1 >= len(argv):
            raise ValueError(f"DS4 plist flag {flag} has no value")
        argv[index + 1] = value
    else:
        argv.extend([flag, value])


def _remove_flag(argv: list[str], flag: str, count: int = 0) -> None:
    while flag in argv:
        index = argv.index(flag)
        del argv[index:index + 1 + count]


def _safe_ds4_path(value: Any, base: Path, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty path")
    raw = Path(value).expanduser()
    resolved = raw.resolve() if raw.is_absolute() else (base / raw).resolve()
    if not raw.is_absolute() and base not in resolved.parents and resolved != base:
        raise ValueError(f"{name} escapes config root")
    return str(resolved)


def _plist_command(path: Path) -> tuple[list[str], dict[str, str], str | None]:
    try:
        data = plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc:
        raise ValueError(f"invalid DS4 plist {path}: {exc}") from exc
    argv = data.get("ProgramArguments")
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
        raise ValueError(f"{path}: ProgramArguments must be a non-empty string array")
    if any(re.search(r"(?:;|\||&&|\|\||`|\$\()", x) for x in argv):
        raise ValueError(f"{path}: ProgramArguments contains shell syntax")
    executable = Path(argv[0]).expanduser()
    if not executable.is_absolute():
        raise ValueError(f"{path}: executable must be absolute")
    env = data.get("EnvironmentVariables", {})
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise ValueError(f"{path}: EnvironmentVariables must be a string map")
    return list(argv), dict(env), data.get("WorkingDirectory")


def compile_ds4_takeover(config_path: Path, *, plist_root: Path | None = None) -> dict[str, Any]:
    """Compile DS4 services/variants into independent managed adapters, without applying them."""
    config_path = config_path.expanduser().resolve()
    def _no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key in {config_path}: {key}")
            result[key] = value
        return result
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicates)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read DS4 config: {config_path}: {exc}") from exc
    if data is None:
        raise ValueError(f"unable to read DS4 config: {config_path}")
    base = config_path.parent.parent
    router = data.get("router", {}) if isinstance(data.get("router"), dict) else {}
    host = router.get("host", "127.0.0.1")
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("DS4 router host must be loopback")
    variants = data.get("variants", {})
    services = data.get("services", {})
    aliases = data.get("aliases", {})
    if not isinstance(variants, dict) or not isinstance(services, dict) or not isinstance(aliases, dict):
        raise ValueError("DS4 variants/services/aliases must be objects")
    normalized: dict[str, dict[str, Any]] = {}
    for name, raw in variants.items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            raise ValueError("DS4 variant entries must be objects")
        if name in normalized:
            raise ValueError(f"duplicate DS4 variant: {name}")
        item = dict(raw)
        item["path"] = _safe_ds4_path(item.get("path"), base, f"variants.{name}.path")
        context = item.get("context")
        output = item.get("output")
        if isinstance(context, bool) or not isinstance(context, int) or context < 1:
            raise ValueError(f"variants.{name}.context must be a positive integer")
        if isinstance(output, bool) or not isinstance(output, int) or output < 1:
            raise ValueError(f"variants.{name}.output must be a positive integer")
        if output > context:
            raise ValueError(f"variants.{name}.output exceeds context")
        runtime = item.get("runtime", {})
        if not isinstance(runtime, dict):
            raise ValueError(f"variants.{name}.runtime must be an object")
        runtime = dict(runtime)
        _validate_runtime_options(runtime, f"variants.{name}")
        for key in ("vision_model", "auxiliary_model"):
            if runtime.get(key):
                runtime[key] = _safe_ds4_path(runtime[key], base, f"variants.{name}.runtime.{key}")
        item["runtime"] = runtime
        normalized[name] = item
    adapters: dict[str, Any] = {}
    models: dict[str, Any] = {}
    warnings: list[str] = []
    group = "large-local"
    plist_root = plist_root.expanduser().resolve() if plist_root else Path.home()

    def service_plist(service_name: str, service_raw: dict[str, Any]) -> Path:
        plist_value = service_raw.get("plist")
        plist_path = Path(str(plist_value)).expanduser() if plist_value else plist_root / "Library/LaunchAgents" / f"{service_name}.plist"
        if not plist_path.is_absolute():
            plist_path = (base / plist_path).resolve()
        if not plist_path.is_file():
            raise ValueError(f"DS4 service plist not found: {plist_path}")
        return plist_path

    def managed_adapter(adapter_id: str, argv: list[str], env: dict[str, str], cwd: str | None, port: Any, plist_path: Path) -> None:
        adapters[adapter_id] = {
            "type": "managed", "command": argv, "endpoint": f"http://{host}:{port}", "health_path": "/v1/models",
            "working_directory": cwd, "env": env, "exclusive_group": group, "stop_strategy": "process-group", "keep_resident": False,
            "plugin_id": "ds4", "service_key": service_name, "source_plist": str(plist_path),
        }

    def require_assets(adapter_id: str, paths: list[str | None]) -> None:
        for path in paths:
            if path and not Path(path).is_file():
                raise ValueError(f"{adapter_id}: model asset missing: {path}")

    for service_name, service_raw in services.items():
        if not isinstance(service_name, str) or not isinstance(service_raw, dict):
            raise ValueError("DS4 service entries must be objects")
        plist_path = service_plist(service_name, service_raw)
        argv, env, cwd = _plist_command(plist_path)
        port = service_raw.get("port", 18888)
        if service_name != "ds4":
            # Independent service pinned to its own model: keep its argv verbatim.
            managed_adapter(f"ds4-{service_name}", argv, env, cwd, port, plist_path)
            model_path = None
            for flag in ("--model", "-m"):
                if flag in argv and argv.index(flag) + 1 < len(argv):
                    model_path = argv[argv.index(flag) + 1]
            require_assets(f"ds4-{service_name}", [model_path])
            continue
        for variant_name, variant in sorted(normalized.items()):
            variant_argv = list(argv)
            _replace_arg(variant_argv, "--model", variant["path"])
            _replace_arg(variant_argv, "--ctx", str(variant.get("context")))
            _replace_arg(variant_argv, "--tokens", str(variant.get("output")))
            # --mtp is a boolean flag in ds4-server; stripping a value here would eat the next argument.
            for flag, count in (("--ssd-streaming", 0), ("--ssd-streaming-cache-experts", 1), ("--mtp-model", 1), ("--mtp", 0), ("--mtp-draft", 1), ("--dspark", 0), ("--dspark-confidence", 1), ("--dspark-strict", 0), ("--vision", 1)):
                _remove_flag(variant_argv, flag, count)
            variant_argv.extend(ds4_variant_argv(variant))
            runtime = variant.get("runtime", {})
            environment = runtime.get("environment", {}) if isinstance(runtime, dict) else {}
            variant_env = {**env, **({str(k): str(v) for k, v in environment.items()} if environment else {})}
            runtime_assets = variant.get("runtime", {})
            assets = [variant.get("path"), runtime_assets.get("vision_model"), runtime_assets.get("auxiliary_model")]
            missing = [path for path in assets if path and not Path(path).is_file()]
            if missing:
                # A hidden variant whose weights were deleted must not block the whole
                # takeover; advertised variants with missing assets are a hard error.
                if variant.get("advertise") is False:
                    warnings.append(f"variant {variant_name} is hidden and its assets are missing; skipped: {missing[0]}")
                    continue
                raise ValueError(f"ds4-{service_name}-{variant_name}: model asset missing: {missing[0]}")
            # advertise=false means hidden from listings, not disabled: the
            # adapter must still exist so authorized aliases keep working.
            if variant.get("advertise") is False:
                warnings.append(f"variant {variant_name} is hidden (advertise=false); compiled but not listed")
            managed_adapter(f"ds4-{service_name}-{variant_name}", variant_argv, variant_env, cwd, port, plist_path)

    default_aliases = {
        "ds4new": ["ds4", "ds4new"],
        "ds4f/ds4new": ["ds4", "ds4new"],
        "ds4mxfp4": ["ds4", "ds4mxfp4"],
        "ds4f/ds4mxfp4": ["ds4", "ds4mxfp4"],
        "m5max_ds4f/ds4mxfp4": ["ds4", "ds4mxfp4"],
        "ds4high": ["ds4", "ds4high"],
        "ds4f/ds4high": ["ds4", "ds4high"],
        "deepseek-v4-flash": ["ds4", "ds4high"],
        "ds4f/deepseek-v4-flash": ["ds4", "ds4high"],
        "m5max_ds4f/deepseek-v4-flash": ["ds4", "ds4high"],
    }
    merged_aliases = {**default_aliases, **{str(k): v for k, v in aliases.items()}}
    for alias, target in merged_aliases.items():
        from_config = alias in aliases
        if not isinstance(target, list) or len(target) != 2:
            raise ValueError(f"alias {alias} must be [service, variant]")
        service_name, variant_name = target
        if service_name not in services:
            raise ValueError(f"alias {alias} references unknown service {service_name}")
        if variant_name is not None and variant_name not in normalized:
            if from_config:
                raise ValueError(f"alias {alias} references unknown variant {variant_name}")
            continue
        service_raw = services[service_name]
        if service_name == "ds4":
            if variant_name is None:
                warnings.append(f"alias {alias} has no variant; skipped")
                continue
            adapter_id = f"ds4-{service_name}-{variant_name}"
            if adapter_id not in adapters:
                warnings.append(f"alias {alias} references skipped variant {variant_name}; skipped")
                continue
            variant = normalized[variant_name]
            vision = bool(variant.get("runtime", {}).get("vision_model"))
            context, output = variant.get("context"), variant.get("output")
            advertised = bool(variant.get("advertise", True))
        else:
            adapter_id = f"ds4-{service_name}"
            vision = False
            context, output = service_raw.get("context"), service_raw.get("output")
            advertised = True
        adapter = adapters[adapter_id]
        # Per-variant ds4 processes serve whatever the alias resolves to; other
        # services pin their own backend model id from the config.
        backend_model = str(service_raw.get("model", alias)) if service_name != "ds4" else str(alias)
        model_entry = {
            "adapter": adapter_id, "backend_model": backend_model, "resource_group": group,
            "context_window": context, "max_output_tokens": output,
            "capabilities": {"chat": True, "stream": True, "vision": vision or "--vision" in adapter.get("command", [])},
            "lifecycle_owner": "model-dispatch",
            "advertise": advertised,
        }
        if service_name == "ds4" and isinstance(variant_name, str):
            model_entry["service_variant"] = variant_name
        levels = normalized.get(variant_name, {}).get("reasoning_levels") if service_name == "ds4" and isinstance(variant_name, str) else service_raw.get("reasoning_levels")
        if isinstance(levels, list) and all(isinstance(level, str) and level for level in levels):
            model_entry["reasoning_levels"] = levels
        else:
            model_entry["reasoning_levels"] = ["off", "low", "medium", "xhigh"] if service_name == "qwen38" else ["off", "high", "max"]
        command = adapter.get("command", [])
        asset_paths = []
        for flag in ("--model", "-m", "--vision", "--mtp-model"):
            if flag in command and command.index(flag) + 1 < len(command):
                asset_paths.append(command[command.index(flag) + 1])
        if asset_paths:
            model_entry["asset_paths"] = list(dict.fromkeys(asset_paths))
        summary = []
        if "--ssd-streaming" in command:
            summary.append("SSD streaming")
        if "--ssd-streaming-cache-experts" in command:
            index = command.index("--ssd-streaming-cache-experts")
            summary.append(f"Expert cache {command[index + 1]}")
        if "--dspark" in command:
            summary.append("DSpark")
        elif "--mtp" in command or "--mtp-model" in command:
            summary.append("MTP")
        if "--kv-disk-space-mb" in command:
            index = command.index("--kv-disk-space-mb")
            summary.append(f"KV disk {int(command[index + 1]) // 1024}GB")
        model_entry["runtime_summary"] = summary
        models[str(alias)] = model_entry
    audit = {"config_path": str(config_path), "compiled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "adapters": {}, "models": {}}
    for adapter_id, adapter in adapters.items():
        plist_path = Path(adapter["source_plist"])
        audit["adapters"][adapter_id] = {
            "source_plist": adapter["source_plist"],
            "source_plist_bytes": plist_path.stat().st_size if plist_path.is_file() else None,
        }
    for alias, target in merged_aliases.items():
        if str(alias) not in models:
            continue
        service_name, variant_name = target
        if service_name == "ds4" and isinstance(variant_name, str) and variant_name in normalized:
            model_path = normalized[variant_name].get("path")
            audit["models"][str(alias)] = {
                "model_path": model_path,
                "model_file_bytes": Path(model_path).stat().st_size if isinstance(model_path, str) and Path(model_path).is_file() else None,
            }
    return {"adapters": adapters, "models": models, "resource_groups": {group: {"capacity": 1}}, "warnings": warnings, "audit": audit, "rollback": {"capture": ["plist", "symlink_target"], "restore_order": ["stop_service", "restore_plist", "restore_symlink", "start_service"]}}


def validate_runtime(compiled: dict[str, Any]) -> list[str]:
    config = {
        "listen_host": "127.0.0.1",
        "listen_port": 18800,
        "adapters": {
            name: {key: value for key, value in adapter.items() if key != "unresolved"}
            for name, adapter in compiled["adapters"].items()
            if not adapter.get("unresolved")
        },
        "resource_groups": compiled.get("resource_groups", {}),
        "models": compiled.get("models", {}),
    }
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        yaml.safe_dump(config, handle)
        temp_path = Path(handle.name)
    try:
        model_dispatch.load_config(temp_path)
    except model_dispatch.ConfigError as exc:
        return [str(exc)]
    finally:
        temp_path.unlink(missing_ok=True)
    return []


def merge_base_config(compiled: dict[str, Any], base_path: Path, drop_adapters: set[str]) -> dict[str, Any]:
    try:
        base = yaml.safe_load(base_path.expanduser().read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"unable to read base config {base_path}: {exc}") from exc
    if not isinstance(base, dict):
        raise ValueError(f"base config must be an object: {base_path}")
    merged_adapters = dict(compiled["adapters"])
    for name, adapter in (base.get("adapters") or {}).items():
        if name in drop_adapters:
            continue
        if name in compiled["adapters"]:
            compiled["warnings"].append(f"base adapter {name} overridden by takeover")
            continue
        merged_adapters[name] = adapter
    merged_models = dict(compiled["models"])
    for name, model in (base.get("models") or {}).items():
        if isinstance(model, dict) and model.get("adapter") in drop_adapters:
            continue
        if name in merged_models:
            compiled["warnings"].append(f"base model {name} overridden by takeover")
            continue
        merged_models[name] = model
    merged_groups = {**(base.get("resource_groups") or {}), **compiled["resource_groups"]}
    compiled = {**compiled, "adapters": merged_adapters, "models": merged_models, "resource_groups": merged_groups}
    compiled["_base_listen"] = {key: base[key] for key in ("listen_host", "listen_port", "load_timeout_seconds", "connect_timeout_seconds", "request_timeout_seconds", "max_body_bytes", "idle_unload_seconds", "metrics_path") if key in base}
    return compiled


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["preview", "check", "plan", "takeover"])
    parser.add_argument("--plugins-dir", type=Path, default=plugin_registry.BUILTIN_DIR)
    parser.add_argument("--root", action="append", type=Path, default=[])
    parser.add_argument("--path", action="append", default=[], metavar="ID=EXECUTABLE")
    parser.add_argument("--id")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--listen-port", type=int)
    parser.add_argument("--ds4-config", type=Path, default=Path("~/Developer/ds4/config/local-runtime.json"))
    parser.add_argument("--base-config", type=Path)
    parser.add_argument("--drop-adapter", action="append", default=[], metavar="NAME")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "takeover":
        try:
            compiled = compile_ds4_takeover(args.ds4_config)
        except ValueError as exc:
            print(f"takeover compile failed: {exc}", file=sys.stderr)
            return 1
        base_listen: dict[str, Any] = {}
        if args.base_config:
            try:
                compiled = merge_base_config(compiled, args.base_config, set(args.drop_adapter))
            except ValueError as exc:
                print(f"base config merge failed: {exc}", file=sys.stderr)
                return 1
            base_listen = compiled.pop("_base_listen")
        validation = validate_runtime(compiled)
        written: list[str] = []
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            if any(args.out.iterdir()):
                print(f"takeover output directory must be empty: {args.out}", file=sys.stderr)
                return 1
            audit_keys = {"source_plist", "stop_strategy"}
            clean_adapters = {
                name: {key: value for key, value in adapter.items() if key not in audit_keys and not (key == "env" and not value)}
                for name, adapter in compiled["adapters"].items()
            }
            full_config = {
                "listen_host": base_listen.get("listen_host", "127.0.0.1"),
                "listen_port": args.listen_port if args.listen_port is not None else base_listen.get("listen_port", 18800),
                "load_timeout_seconds": base_listen.get("load_timeout_seconds", 900),
                "connect_timeout_seconds": base_listen.get("connect_timeout_seconds", 3),
                "request_timeout_seconds": base_listen.get("request_timeout_seconds", 300),
                "max_body_bytes": base_listen.get("max_body_bytes", 67108864),
                "adapters": clean_adapters,
                "models": compiled["models"],
                "resource_groups": compiled["resource_groups"],
            }
            for key in ("idle_unload_seconds", "metrics_path"):
                if key in base_listen:
                    full_config[key] = base_listen[key]
            config_path = args.out / "ds4-takeover.yaml"
            config_path.write_text(yaml.safe_dump(full_config, allow_unicode=True, sort_keys=True), encoding="utf-8")
            audit_path = args.out / "takeover-audit.json"
            audit_path.write_text(json.dumps({"audit": compiled["audit"], "rollback": compiled["rollback"], "warnings": compiled["warnings"]}, ensure_ascii=False, indent=2), encoding="utf-8")
            written = [str(config_path), str(audit_path)]
        print(yaml.safe_dump({"compiled": compiled, "validation_errors": validation, "written": written}, allow_unicode=True, sort_keys=True))
        return 0 if not validation else 1

    manifests, errors = plugin_registry.load_manifests(args.plugins_dir)
    explicit = plugin_registry._parse_paths(args.path)
    results = plugin_registry.scan(manifests, roots=args.root, explicit=explicit)
    if args.command == "plan":
        by_id = {item["id"]: item for item in results}
        plans = []
        written: list[str] = []
        for manifest in manifests:
            if args.id and manifest.id != args.id:
                continue
            item = by_id[manifest.id]
            plan = plan_actions(manifest, item)
            plans.append(plan)
            if args.out and manifest.discover.get("config_reader") and item.get("import_preview"):
                written.extend(write_plan(plan, args.out, item["import_preview"][0]["fields"]))
        print(yaml.safe_dump({"plans": plans, "written": written, "manifest_errors": errors}, allow_unicode=True, sort_keys=True))
        return 0 if not errors else 1
    compiled = compile_runtime(manifests, results)
    validation = validate_runtime(compiled)
    failed = bool(errors or validation)
    payload = {"compiled": compiled, "manifest_errors": errors, "validation_errors": validation}
    if args.command == "preview":
        print(yaml.safe_dump(payload, allow_unicode=True, sort_keys=True))
    else:
        print(json.dumps({"ok": not failed, **payload}, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
