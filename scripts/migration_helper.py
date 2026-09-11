#!/usr/bin/env python3
"""Plan or apply file-level migration to the InferenceDock dispatcher."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_dispatch


MODEL_ENV_KEYS = {
    "ANTHROPIC_MODEL",
    "DEFAULT_MODEL",
    "INFERENCE_MODEL",
    "MODEL",
    "OPENAI_DEFAULT_MODEL",
    "OPENAI_MODEL",
}
LAUNCHER_TOKENS = ("ds4", "mlx-serve", "mlxserve", "mtplx")
MAX_FILE_BYTES = 1024 * 1024
URL_PATTERN = re.compile(r"https?://(?:127\.0\.0\.1|localhost):(\d+)(?=$|[/?#])")
ENV_PATTERN = re.compile(r"^(?P<prefix>\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")


class MigrationError(ValueError):
    pass


def _strip_quotes(value: str) -> tuple[str, str, str]:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[0], text[1:-1], text[0]
    return "", text, ""


def _target(config_path: Path) -> tuple[model_dispatch.DispatchConfig, str, set[int], dict[str, str]]:
    try:
        config = model_dispatch.load_config(config_path)
    except model_dispatch.ConfigError as exc:
        raise MigrationError(str(exc)) from exc
    dispatcher = f"http://{config.listen_host}:{config.listen_port}"
    ports = set()
    for adapter in config.adapters.values():
        if adapter.endpoint:
            match = URL_PATTERN.search(adapter.endpoint)
            if match:
                ports.add(int(match.group(1)))
        if adapter.port is not None:
            ports.add(adapter.port)
    ports.discard(config.listen_port)
    aliases: dict[str, str] = {}
    ambiguous = set()
    for model in config.models.values():
        for alias in {model.backend_model, model.display_name, model.name} - {None}:
            if alias in aliases and aliases[alias] != model.name:
                ambiguous.add(alias)
                continue
            aliases[alias] = model.name
    for alias in ambiguous:
        aliases.pop(alias, None)
    return config, dispatcher, ports, aliases


def _default_files() -> list[Path]:
    candidates = [Path.home() / name for name in (".zshrc", ".zshenv", ".profile")]
    return [path for path in candidates if path.is_file()]


def _read_text(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
        if b"\0" in data:
            return None
        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _rewrite_line(
    line: str,
    *,
    dispatcher: str,
    legacy_ports: set[int],
    model_aliases: dict[str, str],
    actions: list[dict[str, Any]],
    line_number: int,
) -> str:
    rewritten = line

    def replace_url(match: re.Match[str]) -> str:
        port = int(match.group(1))
        if port not in legacy_ports:
            return match.group(0)
        actions.append({
            "type": "rewrite_endpoint",
            "line": line_number,
            "from_port": port,
            "to": dispatcher,
        })
        return dispatcher

    rewritten = URL_PATTERN.sub(replace_url, rewritten)

    env = ENV_PATTERN.match(rewritten)
    if env and env.group("key") in MODEL_ENV_KEYS:
        open_quote, value, close_quote = _strip_quotes(env.group("value"))
        if value in model_aliases and model_aliases[value] != value:
            replacement = f"{open_quote}{model_aliases[value]}{close_quote}"
            rewritten = f"{env.group('prefix')}{env.group('key')}={replacement}"
            actions.append({
                "type": "rewrite_model",
                "line": line_number,
                "key": env.group("key"),
                "from": value,
                "to": model_aliases[value],
            })
        elif value not in model_aliases:
            actions.append({
                "type": "manual_review",
                "line": line_number,
                "reason": f"{env.group('key')} is not in the target model map",
            })

    lowered = rewritten.lower()
    if not env and any(token in lowered for token in LAUNCHER_TOKENS):
        actions.append({
            "type": "manual_review",
            "line": line_number,
            "reason": "legacy launcher reference; verify ownership before replacing it",
        })
    return rewritten


def plan_file(path: Path, *, dispatcher: str, legacy_ports: set[int], model_aliases: dict[str, str]) -> dict[str, Any]:
    original = _read_text(path)
    result: dict[str, Any] = {"path": str(path), "actions": [], "status": "unchanged"}
    if original is None:
        result["status"] = "skipped"
        result["reason"] = "missing, symlink, binary, unreadable, or larger than 1 MiB"
        return result

    changed_lines = []
    for number, line in enumerate(original.splitlines(keepends=True), start=1):
        body = line[:-1] if line.endswith("\n") else line
        newline = "\n" if line.endswith("\n") else ""
        before = len(result["actions"])
        rewritten = _rewrite_line(
            body,
            dispatcher=dispatcher,
            legacy_ports=legacy_ports,
            model_aliases=model_aliases,
            actions=result["actions"],
            line_number=number,
        )
        changed_lines.append(rewritten + newline)
        if rewritten != body:
            result["status"] = "changed"
        elif len(result["actions"]) > before and result["status"] == "unchanged":
            result["status"] = "manual_review"
    result["content"] = "".join(changed_lines)
    result["source_sha256"] = hashlib.sha256(original.encode("utf-8")).hexdigest()
    if result["status"] == "unchanged" and result["actions"]:
        result["status"] = "manual_review"
    return result


def _backup_path(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.bak-{stamp}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{counter}")
        counter += 1
    return candidate


def apply_file(path: Path, planned: dict[str, Any]) -> None:
    if planned["status"] != "changed":
        return
    if path.is_symlink() or not path.is_file():
        raise MigrationError(f"refusing to replace non-regular file: {path}")
    current_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    if current_hash != planned["source_sha256"]:
        raise MigrationError(f"file changed after migration plan: {path}")
    backup = _backup_path(path)
    shutil.copy2(path, backup)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(planned["content"])
        shutil.copystat(path, temp)
        os.replace(temp, path)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    planned["backup"] = str(backup)


def build_plan(files: list[Path], config_path: Path, legacy_ports_override: set[int] | None = None) -> dict[str, Any]:
    config, dispatcher, legacy_ports, model_aliases = _target(config_path)
    if legacy_ports_override is not None:
        legacy_ports = legacy_ports_override
    planned = [
        plan_file(path.expanduser(), dispatcher=dispatcher, legacy_ports=legacy_ports, model_aliases=model_aliases)
        for path in files
    ]
    changed = sum(1 for item in planned if item["status"] == "changed")
    manual = sum(1 for item in planned if item["status"] == "manual_review")
    skipped = sum(1 for item in planned if item["status"] == "skipped")
    return {
        "ok": True,
        "mode": "plan",
        "target_config": str(config.path),
        "dispatcher": dispatcher,
        "legacy_ports": sorted(legacy_ports),
        "files": planned,
        "summary": {"files": len(planned), "changed": changed, "manual_review": manual, "skipped": skipped},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", type=Path, help="files to inspect; plan mode defaults to shell startup files")
    parser.add_argument("--config", type=Path, required=True, help="validated target dispatcher configuration")
    parser.add_argument("--apply", action="store_true", help="replace explicitly listed files after creating backups")
    parser.add_argument("--legacy-port", action="append", type=int, help="rewrite only this legacy loopback port (repeatable)")
    parser.add_argument("--pretty", action="store_true", help="pretty-print JSON output")
    args = parser.parse_args(argv)

    if args.apply and not args.files:
        parser.error("--apply requires at least one explicit file")
    files = args.files if args.files else _default_files()
    try:
        plan = build_plan(files, args.config.expanduser(), set(args.legacy_port) if args.legacy_port else None)
        if args.apply:
            plan["mode"] = "apply"
            for item in plan["files"]:
                apply_file(Path(item["path"]), item)
                item.pop("content", None)
        else:
            for item in plan["files"]:
                item.pop("content", None)
    except (MigrationError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    print(json.dumps(plan, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
