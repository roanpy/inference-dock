#!/usr/bin/env python3
"""Isolated update monitor tests using throwaway git repositories."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import update_monitor


def git(*args, cwd=None):
    env = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(cwd), "GIT_TERMINAL_PROMPT": "0"}
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True)


def make_repo(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir()
    git("init", "-b", "main", cwd=repo)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-m", "one", cwd=repo)
    return repo


def test_local_state_and_remote_compare():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        origin = root / "origin.git"
        origin.mkdir()
        git("init", "--bare", "-b", "main", cwd=origin)
        repo = make_repo(root, "work")
        git("remote", "add", "origin", str(origin), cwd=repo)
        git("push", "-u", "origin", "main", cwd=repo)

        state = update_monitor.local_repo_state(repo)
        assert state["branch"] == "main"
        assert state["dirty"] is False
        heads = update_monitor.remote_heads(str(origin))
        assert heads == {"main": state["head"]}
        assert update_monitor.compare_relation(repo, state["head"], heads["main"]) == "current"

        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-m", "two", cwd=repo)
        ahead = update_monitor.local_repo_state(repo)
        assert ahead["head"] != heads["main"]
        assert update_monitor.compare_relation(repo, ahead["head"], heads["main"]) == "ahead"

        (repo / "dirty.txt").write_text("x")
        assert update_monitor.local_repo_state(repo)["dirty"] is True


def test_upstream_is_preferred_over_origin():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        origin = root / "origin.git"
        upstream = root / "upstream.git"
        origin.mkdir()
        upstream.mkdir()
        git("init", "--bare", "-b", "main", cwd=origin)
        git("init", "--bare", "-b", "main", cwd=upstream)
        repo = make_repo(root, "work")
        git("remote", "add", "origin", str(origin), cwd=repo)
        git("remote", "add", "upstream", str(upstream), cwd=repo)
        state = update_monitor.local_repo_state(repo)
        assert state["remote_name"] == "upstream"
        assert state["remote"] == str(upstream)


def test_missing_repo_isolated_error():
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "nope"
        state = update_monitor.local_repo_state(missing)
        assert "error" in state


def test_remote_heads_unreachable():
    assert update_monitor.remote_heads("/definitely/not/a/repo.git") is None


TESTS = [
    test_local_state_and_remote_compare,
    test_upstream_is_preferred_over_origin,
    test_missing_repo_isolated_error,
    test_remote_heads_unreachable,
]


def main():
    for test in TESTS:
        print(f"run {test.__name__}", flush=True)
        test()
        print(f"ok {test.__name__}", flush=True)


if __name__ == "__main__":
    main()
