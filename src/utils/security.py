"""Security helpers for command validation and sandbox preparation."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

# Test runners we are willing to execute.  Anything else is rejected.
ALLOWED_COMMANDS = {
    "python",
    "python3",
    "py",
    "pytest",
    "python -m pytest",
    "node",
    "npm",
    "npx",
    "yarn",
    "pnpm",
    "jest",
    "vitest",
    "mocha",
    "go",
    "mvn",
    "gradle",
    "gradlew",
    "cargo",
    "dotnet",
    "ruby",
    "bundle",
    "rspec",
    "php",
}

# Tokens that must never be executed, even if embedded in a script name.
DENIED_TOKENS = {
    "rm", "del", "format", "mkfs", "sudo", "su", "chmod", "chown",
    "curl", "wget", "ssh", "scp", "powershell", "pwsh", "cmd", "eval",
    "shutdown", "reboot", "taskkill", "killall",
}

SHELL_METACHARACTERS = (";", "|", "&", ">", "<", "`", "$(", "${", "\n", "\r")

# Directories excluded from the sandbox copy (large / irrelevant / VCS).
COPY_EXCLUDES = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", "dist", "build", "target", "coverage", "htmlcov",
    ".idea", ".vscode", ".next", ".nuxt", "vendor",
}

MAX_COPY_BYTES = 64 * 1024 * 1024  # 64 MB safety ceiling


class CommandValidationError(ValueError):
    """Raised when a planned command fails the security policy."""


def validate_command(command: Sequence[str]) -> List[str]:
    """Validate a command vector and return a normalized copy.

    Rules:
      * must be a non-empty argv list (never a shell string),
      * executable must be on the allow-list,
      * no shell metacharacters or dangerous tokens,
      * no path traversal via ``..``.
    """
    if not command or not isinstance(command, (list, tuple)):
        raise CommandValidationError("Command must be a non-empty argv list")

    argv = [str(part) for part in command]
    executable = Path(argv[0]).name.lower()

    # Special case: `python -m pytest` / `python -m unittest` are allow-listed
    # through the interpreter so make sure the module arg is safe too.
    if len(argv) >= 3 and argv[1] == "-m":
        module = argv[2].lower()
        if module not in {"pytest", "unittest", "coverage", "nose"}:
            raise CommandValidationError(f"Python module not allowed: {module}")
    elif executable not in ALLOWED_COMMANDS:
        raise CommandValidationError(f"Command not allowed: {executable}")

    for token in argv:
        if any(meta in token for meta in SHELL_METACHARACTERS):
            raise CommandValidationError(f"Shell metacharacter in token: {token!r}")
        if token.lower() in DENIED_TOKENS:
            raise CommandValidationError(f"Denied token in command: {token!r}")
        if token == ".." or token.startswith("../") or "..\\" in token:
            raise CommandValidationError(f"Path traversal in token: {token!r}")
    return argv


def validate_environment(env: Dict[str, str]) -> Dict[str, str]:
    """Validate plan-provided environment variables before injecting them."""
    clean: Dict[str, str] = {}
    for key, value in (env or {}).items():
        if not isinstance(key, str) or not key.replace("_", "").isalnum():
            raise CommandValidationError(f"Invalid env var name: {key!r}")
        text = str(value)
        if "\n" in text or "\r" in text or "\x00" in text:
            raise CommandValidationError(f"Invalid characters in env var {key}")
        clean[key] = text
    return clean


def minimal_env(overrides: Dict[str, str] | None = None) -> Dict[str, str]:
    """Build a sanitized environment: inherited PATH plus test-only variables."""
    passthrough = [
        "PATH", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP", "HOME",
        "USERPROFILE", "LANG", "LC_ALL", "PYTHONPATH", "NODE_PATH",
        "USERNAME", "LOCALAPPDATA", "GOCACHE", "GOPATH", "GOROOT",
    ]
    env = {k: os.environ[k] for k in passthrough if k in os.environ}
    env.update({
        "CI": "true",
        "NODE_ENV": "test",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    env.update(validate_environment(overrides or {}))
    return env


class Sandbox:
    """A bounded, temporary working copy of a project for test execution."""

    def __init__(self, source: str | Path, excludes: Iterable[str] = COPY_EXCLUDES,
                 *, exclude_dirs: Iterable[str] = ()):
        self.source = Path(source).resolve()
        self.excludes = set(excludes)
        self.exclude_dirs = {Path(path) for path in exclude_dirs}
        self.path: Path | None = None
        self._tempdir: tempfile.TemporaryDirectory[str] | None = None
        self.total_bytes = 0
        self.skipped: List[str] = []

    def __enter__(self) -> Path:
        if not self.source.is_dir():
            raise FileNotFoundError(f"Project directory not found: {self.source}")
        self._tempdir = tempfile.TemporaryDirectory(prefix="testmirror_")
        self.path = Path(self._tempdir.name) / "workspace"
        try:
            self.path.mkdir(parents=True)
            self._copy_tree(self.source, self.path)
        except BaseException:
            self._tempdir.cleanup()
            raise
        return self.path

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._tempdir is not None:
            self._tempdir.cleanup()
            self._tempdir = None
            self.path = None

    def _copy_tree(self, src: Path, dst: Path, *, dependency_tree: bool = False) -> None:
        for item in sorted(src.iterdir()):
            name = item.name.lower()
            if (name == ".env" or name.startswith(".env.") or name.endswith(".env")
                    or (not dependency_tree and item.name in self.excludes)):
                self.skipped.append(item.name)
                continue
            target = dst / item.name
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                self.skipped.append(item.name)
            elif stat.S_ISDIR(info.st_mode):
                if item.relative_to(self.source) in self.exclude_dirs:
                    self.skipped.append(item.relative_to(self.source).as_posix())
                    continue
                target.mkdir()
                self._copy_tree(item, target, dependency_tree=dependency_tree or item.name == "node_modules")
            elif stat.S_ISREG(info.st_mode):
                self.total_bytes += info.st_size
                if self.total_bytes > MAX_COPY_BYTES:
                    raise ValueError(f"Project copy exceeds {MAX_COPY_BYTES // (1024 * 1024)} MiB")
                shutil.copy2(item, target)


def find_files(root: str | Path, pattern: str, max_depth: int = 3) -> List[Path]:
    """Shallow, bounded glob for project classification."""
    root_path = Path(root)
    if not root_path.is_dir():
        return []
    results: List[Path] = []
    base_depth = len(root_path.parts)
    for path in root_path.rglob(pattern):
        if any(part in COPY_EXCLUDES for part in path.parts):
            continue
        if len(path.parts) - base_depth > max_depth:
            continue
        if path.is_file():
            results.append(path)
        if len(results) >= 50:
            break
    return results
