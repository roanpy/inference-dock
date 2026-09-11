#!/usr/bin/env python3
"""Read-only update monitor: compare registered plugin repos with their remotes."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plugin_registry

GIT_TIMEOUT = 10
GIT_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": os.environ.get("HOME", ""),
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oStrictHostKeyChecking=accept-new",
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=GIT_TIMEOUT, env=GIT_ENV, check=False,
    )


def local_repo_state(repo: Path) -> dict[str, Any]:
    if not (repo / ".git").exists():
        return {"path": str(repo), "error": "not a git repository"}
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    head = _git(repo, "rev-parse", "HEAD")
    dirty = _git(repo, "status", "--porcelain")
    remotes = _git(repo, "remote", "-v")
    fetch_remotes = {}
    for line in remotes.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[-1] == "(fetch)":
            fetch_remotes[parts[0]] = parts[1]
    remote_name = "upstream" if "upstream" in fetch_remotes else "origin"
    remote_url = fetch_remotes.get(remote_name)
    if branch.returncode or head.returncode:
        return {"path": str(repo), "error": "unable to read git HEAD"}
    return {
        "path": str(repo),
        "branch": branch.stdout.strip(),
        "head": head.stdout.strip(),
        "dirty": bool(dirty.stdout.strip()) if dirty.returncode == 0 else None,
        "remote_name": remote_name if remote_url else None,
        "remote": remote_url,
    }


def remote_heads(remote_url: str) -> dict[str, str] | None:
    try:
        proc = subprocess.run(
            ["git", "ls-remote", "--heads", remote_url],
            capture_output=True, text=True, timeout=GIT_TIMEOUT, env=GIT_ENV, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    heads = {}
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            heads[parts[1][len("refs/heads/"):]] = parts[0]
    return heads


def compare_relation(repo: Path, local_head: str, remote_head: str) -> str:
    if local_head == remote_head:
        return "current"
    if _git(repo, "cat-file", "-e", f"{remote_head}^{{commit}}").returncode:
        return "unknown"
    if _git(repo, "merge-base", "--is-ancestor", local_head, remote_head).returncode == 0:
        return "behind"
    if _git(repo, "merge-base", "--is-ancestor", remote_head, local_head).returncode == 0:
        return "ahead"
    return "diverged"


def check_plugins(manifests: list[plugin_registry.Manifest], *, offline: bool = False) -> list[dict[str, Any]]:
    observed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    results = []
    for manifest in manifests:
        entry: dict[str, Any] = {"id": manifest.id, "observed_at": observed_at, "repos": []}
        repo_specs = list(manifest.repositories)
        repo_specs.extend({"path": token} for token in manifest.discover.get("repo_paths", []))
        for spec in repo_specs:
            repo_value = spec.get("path")
            repo = Path(repo_value).expanduser() if repo_value else None
            state = local_repo_state(repo) if repo and repo.is_dir() else ({"path": str(repo), "error": "path not found"} if repo else {"error": "repository path not configured"})
            remote = spec.get("remote") or state.get("remote")
            branch = spec.get("branch") or state.get("branch")
            if remote and branch and not offline:
                heads = remote_heads(remote)
                state["remote"] = remote
                state["branch"] = branch
                state["remote_head"] = heads.get(branch) if heads else None
                state["status"] = "ok" if heads is not None else "unreachable"
                if state.get("head") and state.get("remote_head"):
                    state["matches_remote"] = state["head"] == state["remote_head"]
                    state["relation"] = compare_relation(repo, state["head"], state["remote_head"])
            entry["repos"].append(state)
        results.append(entry)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugins-dir", type=Path, default=plugin_registry.BUILTIN_DIR)
    parser.add_argument("--id")
    parser.add_argument("--offline", action="store_true", help="skip remote ls-remote probes")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    manifests, errors = plugin_registry.load_manifests(args.plugins_dir)
    if args.id:
        manifests = [m for m in manifests if m.id == args.id]
        if not manifests:
            print(f"unknown plugin id: {args.id}", file=sys.stderr)
            return 1
    result = {"results": check_plugins(manifests, offline=args.offline), "manifest_errors": errors}
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
