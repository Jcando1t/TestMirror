"""Execution Agent: run generated test commands inside an isolated sandbox.

Safety measures
---------------
* commands are validated argv vectors (no shell interpretation),
* tests run against a bounded temporary copy of the project (not the source),
* a sanitized environment and per-command timeouts are enforced,
* captured output is truncated before it enters the shared state.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from src.models.state import WorkflowState
from src.utils.helpers import truncate, utc_now
from src.utils.logging_config import get_logger
from src.utils.provenance import describe_execution
from src.utils.security import (
    CommandValidationError,
    COPY_EXCLUDES,
    Sandbox,
    minimal_env,
    validate_command,
)
from src.utils.test_results import PytestCaseResults, TestCounts, parse_test_counts, read_pytest_cases

logger = get_logger(__name__)


def parse_coverage(workspace: Path, *, pytest_report: Path | None = None) -> Dict[str, Any]:
    """Extract coverage information from common report formats."""
    coverage: Dict[str, Any] = {"source": None}

    json_file = pytest_report if pytest_report is not None else workspace / "coverage.json"
    if json_file.is_file():
        try:
            data = json.loads(json_file.read_text(encoding="utf-8", errors="replace"))
            totals = data.get("totals", {}) if isinstance(data, dict) else {}
            if totals:
                coverage.update({
                    "source": "coverage.json",
                    "percent_covered": round(float(totals.get("percent_covered", 0.0)), 2),
                    "lines_covered": totals.get("covered_lines"),
                    "lines_total": totals.get("num_statements"),
                    "branches_covered": totals.get("covered_branches"),
                    "branches_total": totals.get("num_branches"),
                    "files": len(data.get("files", {})),
                })
                return coverage
        except (AttributeError, ValueError, TypeError, OSError) as exc:
            logger.warning("Failed to parse coverage.json: %s", exc)

    if pytest_report is not None:
        return coverage

    for lcov in (workspace / "lcov.info", workspace / "coverage" / "lcov.info"):
        if not lcov.is_file():
            continue
        try:
            lines_found = lines_hit = 0
            for line in lcov.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("LF:"):
                    lines_found += int(line[3:])
                elif line.startswith("LH:"):
                    lines_hit += int(line[3:])
            if lines_found:
                coverage.update({
                    "source": lcov.relative_to(workspace).as_posix(),
                    "lines_covered": lines_hit,
                    "lines_total": lines_found,
                    "percent_covered": round(100.0 * lines_hit / lines_found, 2),
                })
                return coverage
        except (ValueError, OSError) as exc:
            logger.warning("Failed to parse lcov.info: %s", exc)

    go_profile = workspace / "coverage.out"
    if go_profile.is_file():
        try:
            lines = go_profile.read_text(encoding="utf-8").splitlines()
            if lines and lines[0].startswith("mode: "):
                blocks: dict[str, tuple[int, bool]] = {}
                for line in lines[1:]:
                    location, statements, hits = line.rsplit(maxsplit=2)
                    blocks[location] = (int(statements), int(hits) > 0 or blocks.get(location, (0, False))[1])
                total = sum(size for size, _ in blocks.values())
                if total:
                    covered = sum(size for size, hit in blocks.values() if hit)
                    return {"source": "coverage.out", "percent_covered": round(100 * covered / total, 2),
                            "statements_covered": covered, "statements_total": total}
        except (ValueError, IndexError, OSError) as exc:
            logger.warning("Failed to parse coverage.out: %s", exc)

    return coverage


def _runner_argv(argv: list[str]) -> list[str]:
    if argv[0] in {"python", "python3", "py"}:
        return [sys.executable, *argv[1:]]
    if os.name == "nt" and argv[0] == "npm":
        # Standard Node.js on Windows ships npm as a batch shim to this JS entry point.
        npm = shutil.which("npm.cmd")
        node = shutil.which("node.exe")
        cli = Path(npm).parent / "node_modules" / "npm" / "bin" / "npm-cli.js" if npm else None
        if not node or cli is None or not cli.is_file():
            raise FileNotFoundError("Install Node.js with its bundled npm CLI and add it to PATH")
        return [node, str(cli), *argv[1:]]
    return argv


def execute_plan(plan: Dict[str, Any], project_path: str, *, test_tool: str | None = None,
                 exclude_dirs: list[str] | None = None) -> Dict[str, Any]:
    """Pure-ish execution function; returns the Execution Agent output contract."""
    commands: List[Dict[str, Any]] = plan.get("commands") or []
    if not commands:
        raise ValueError("Test plan contains no commands")

    env = minimal_env(plan.get("environment_vars", {}))
    default_timeout = int(plan.get("timeout_seconds") or 180)
    command_results: List[Dict[str, Any]] = []
    overall_exit = 0
    stdout_parts: List[str] = []
    stderr_parts: List[str] = []
    coverage_data: Dict[str, Any] = {"source": None}
    test_counts: TestCounts | None = None
    test_case_results: PytestCaseResults | None = None
    required_test_commands = sum(
        not item.get("optional") and item.get("phase", "test") == "test" for item in commands
    )
    started = time.perf_counter()

    uses_npm = any(item["command"] and item["command"][0] == "npm" for item in commands)
    excludes = COPY_EXCLUDES - {"node_modules"} if uses_npm else COPY_EXCLUDES
    with Sandbox(project_path, excludes=excludes, exclude_dirs=exclude_dirs or ()) as workspace:
        logger.info("Sandbox ready at %s", workspace)
        provenance = describe_execution(
            Path(project_path), workspace,
            uses_python=any(item["command"][0] in {"python", "python3", "py"} for item in commands),
        )
        pytest_report = None
        for index, item in enumerate(commands, start=1):
            argv = validate_command(item["command"])
            python_runner = argv[0] in {"python", "python3", "py"}
            pytest_runner = python_runner and argv[1:3] == ["-m", "pytest"]
            optional = bool(item.get("optional", False))
            timeout = int(item.get("timeout_seconds") or default_timeout)
            logger.info("[%d/%d] %s", index, len(commands), " ".join(argv))
            cmd_started = time.perf_counter()
            try:
                argv = _runner_argv(argv)
                if pytest_runner:
                    # Keep the report outside the copy so stale project artifacts are ignored.
                    pytest_report = workspace.parent / "coverage.json"
                    runner = Path(__file__).with_name("pytest_runner.py").resolve()
                    case_report = workspace.parent / f"pytest-cases-{index}.json"
                    argv = [argv[0], str(runner), str(pytest_report), str(case_report), *argv[3:]]
                proc = subprocess.run(
                    argv,
                    cwd=str(workspace),
                    env={**env, "PYTHONIOENCODING": "utf-8"} if python_runner else env,
                    shell=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                )
                exit_code = int(proc.returncode)
                out, err = proc.stdout or "", proc.stderr or ""
            except subprocess.TimeoutExpired as exc:
                exit_code = 124
                out = exc.stdout or ""
                if isinstance(out, bytes):
                    out = out.decode("utf-8", "replace")
                err = f"Command timed out after {timeout}s: {' '.join(argv)}"
            except FileNotFoundError as exc:
                exit_code = 127
                out, err = "", f"Executable not found: {exc}"
            except OSError as exc:
                exit_code = 126
                out, err = "", f"OS error while executing command: {exc}"

            duration = round(time.perf_counter() - cmd_started, 4)
            failure_kind = None
            if item.get("phase", "test") == "test" and not optional:
                if pytest_runner:
                    if required_test_commands == 1:
                        test_case_results = read_pytest_cases(case_report, exit_code)
                    else:
                        test_case_results = {
                            "format": "pytest-nodeid-v1", "status": "unavailable",
                            "reason": "multiple_test_commands", "selected_count": None, "cases": [],
                        }
                # Pytest 2–5 indicate interruption/collection, internal/usage errors, or no tests.
                collection_failed = pytest_runner and bool(
                    "ERROR collecting " in out
                    or "during collection" in (out + err).lower()
                )
                counts = (parse_test_counts(out, err, test_tool)
                          if not collection_failed and not (pytest_runner and exit_code not in (0, 1))
                          and exit_code not in (124, 126, 127) else None)
                if exit_code:
                    failure_kind = (
                        "test_failed" if counts and counts["failed"]
                        and not collection_failed else "execution_failed"
                    )
                elif counts is not None and not any(counts.values()):
                    failure_kind = "execution_failed"
                if counts is not None:
                    if test_counts is None:
                        test_counts = TestCounts(passed=0, failed=0, skipped=0)
                    for key in ("passed", "failed", "skipped"):
                        test_counts[key] += counts[key]
            stdout_parts.append(f"--- [{index}] {' '.join(argv)} ---\n{truncate(out)}")
            stderr_parts.append(f"--- [{index}] {' '.join(argv)} ---\n{truncate(err)}")
            command_results.append({
                "index": index,
                "command": argv,
                "description": item.get("description", ""),
                "phase": item.get("phase", "test"),
                "optional": optional,
                "exit_code": exit_code,
                "stdout": truncate(out),
                "stderr": truncate(err),
                "execution_time": duration,
                "timeout_seconds": timeout,
                "timed_out": exit_code == 124,
                "failure_kind": failure_kind,
            })
            if exit_code != 0 and not optional:
                if overall_exit == 0:
                    overall_exit = exit_code
                logger.warning("Command %d failed (exit=%s): %s", index, exit_code, argv)
            elif exit_code != 0:
                logger.info("Optional command %d failed (exit=%s), continuing", index, exit_code)

        parsed = parse_coverage(workspace, pytest_report=pytest_report)
        if parsed.get("source"):
            coverage_data = parsed

    total_time = round(time.perf_counter() - started, 4)
    return {
        "exit_code": overall_exit,
        "stdout": "\n".join(stdout_parts),
        "stderr": "\n".join(stderr_parts),
        "execution_time": total_time,
        "coverage_data": coverage_data,
        "test_counts": test_counts,
        "test_case_results": test_case_results,
        "command_results": command_results,
        "execution_provenance": provenance,
    }


def execution_node(state: WorkflowState) -> Dict[str, Any]:
    """LangGraph node: execute the plan and publish results to the shared state."""
    logger.info("Executing test plan for %s", state.get("evaluation_id"))
    try:
        result = execute_plan(
            {
                "commands": state.get("commands", []),
                "environment_vars": state.get("environment_vars", {}),
                "timeout_seconds": state.get("timeout_seconds", 180),
            },
            state["project_path"],
            test_tool=state.get("test_tool"),
            exclude_dirs=state.get("exclude_dirs"),
        )
        logger.info(
            "Execution finished: exit=%s time=%.3fs coverage=%s",
            result["exit_code"], result["execution_time"],
            result["coverage_data"].get("percent_covered"),
        )
        return {
            **result,
            "current_agent": "executor",
            "logs": [f"[{utc_now()}] Executed {len(result['command_results'])} command(s), exit={result['exit_code']}"],
        }
    except (CommandValidationError, ValueError, FileNotFoundError):
        logger.exception("Execution rejected")
        raise
    except Exception:  # noqa: BLE001
        logger.exception("Execution failed")
        raise
