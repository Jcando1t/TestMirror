"""Describe the copied project and the environment used for an evaluation."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any


def describe_execution(source: Path, workspace: Path, *, uses_python: bool) -> dict[str, Any]:
    return {
        "snapshot": _snapshot(workspace),
        "git": _git_checkout(source.resolve()),
        "runtime": _runtime(uses_python),
    }


def _snapshot(workspace: Path) -> dict[str, Any]:
    digest = hashlib.sha256(b"testmirror-tree-v1\n")
    file_count = 0
    byte_count = 0
    for relative in sorted(path.relative_to(workspace).as_posix() for path in workspace.rglob("*")):
        path = workspace / relative
        info = path.stat()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISDIR(info.st_mode):
            entry = ["dir", relative, mode]
        else:
            with path.open("rb") as stream:
                content_hash = hashlib.file_digest(stream, "sha256").hexdigest()
            entry = ["file", relative, mode, content_hash]
            file_count += 1
            byte_count += info.st_size
        digest.update(json.dumps(entry, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n")
    return {
        "format": "testmirror-tree-v1",
        "sha256": digest.hexdigest(),
        "files": file_count,
        "bytes": byte_count,
    }


def _git_checkout(source: Path) -> dict[str, Any] | None:
    git = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(source)]
    env = dict(os.environ)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        env.pop(key, None)

    def read_git(*args: str) -> str:
        return subprocess.run(
            [*git, *args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=5, check=True, env=env,
        ).stdout.rstrip("\r\n")

    try:
        if Path(read_git("rev-parse", "--show-toplevel")).resolve() != source:
            return None
        commit = read_git("rev-parse", "--verify", "HEAD")
    except (OSError, subprocess.SubprocessError):
        return None

    try:
        dirty = bool(read_git("status", "--porcelain=v1", "--untracked-files=normal"))
    except (OSError, subprocess.SubprocessError):
        dirty = None
    return {"commit": commit, "dirty": dirty}


def _runtime(uses_python: bool) -> dict[str, Any]:
    runtime: dict[str, Any] = {
        "os": platform.system(),
        "os_release": platform.release(),
        "architecture": platform.machine(),
    }
    if uses_python:
        packages = {}
        for name in ("pytest", "pytest-cov", "coverage"):
            try:
                packages[name] = version(name)
            except PackageNotFoundError:
                continue
        runtime["python"] = {
            "executable": sys.executable,
            "version": platform.python_version(),
            "installed_packages": packages,
        }
    return runtime
