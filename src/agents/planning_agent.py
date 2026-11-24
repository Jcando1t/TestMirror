"""Planning Agent: turn a classification result into an executable test plan.

Contract
--------
Input  (from Classification Agent): {project_type, framework, test_tool, language}
Output (to Execution Agent):        {test_strategy, commands, environment_vars,
                                     expected_output}

The generated plan is data-only: commands are argv lists (never shell strings)
so the Execution Agent can validate and sandbox them safely.
"""

from __future__ import annotations

from typing import Any, Dict, List

from src.models.state import TestCommand, WorkflowState
from src.utils.helpers import utc_now
from src.utils.logging_config import get_logger
from src.utils.security import validate_command, validate_environment

logger = get_logger(__name__)


def _cmd(argv: List[str], description: str, phase: str = "test",
         optional: bool = False) -> TestCommand:
    return TestCommand(
        command=validate_command(argv),
        description=description,
        phase=phase,
        optional=optional,
    )


def _pytest_plan() -> Dict[str, Any]:
    return {
        "test_strategy": (
            "Run the project's pytest suite in a sandboxed working copy with "
            "deterministic hashes and CI flags; collect JUnit XML and optional "
            "coverage in the same test run."
        ),
        "commands": [
            _cmd(
                ["python", "-m", "pytest", "-q", "--disable-warnings",
                 "--junitxml=test-results.xml"],
                "Execute pytest once and collect JUnit XML and optional coverage",
            ),
        ],
        "environment_vars": {
            "PYTEST_ADDOPTS": "--color=no",
            "PYTHONHASHSEED": "0",
        },
        "expected_output": {
            "exit_code": 0,
            "indicators": ["passed", "test-results.xml"],
            "coverage_file": "coverage.json",
        },
        "timeout_seconds": 180,
    }


def _unittest_plan() -> Dict[str, Any]:
    return {
        "test_strategy": "Discover and run Python unittest suites with verbose output.",
        "commands": [
            _cmd(
                ["python", "-m", "unittest", "discover", "-s", ".", "-v"],
                "Discover and run unittest cases",
            ),
        ],
        "environment_vars": {"PYTHONHASHSEED": "0"},
        "expected_output": {"exit_code": 0, "indicators": ["Ran ", "OK"]},
        "timeout_seconds": 180,
    }


def _npm_plan(test_tool: str) -> Dict[str, Any]:
    if test_tool == "vitest":
        argv = ["npm", "exec", "--", "vitest", "run", "--reporter=json"]
        summary = "Run Vitest in CI mode with a machine-readable JSON reporter."
    elif test_tool == "mocha":
        argv = ["npm", "exec", "--", "mocha", "--reporter", "json"]
        summary = "Run Mocha with the JSON reporter for structured results."
    elif test_tool == "jest":
        argv = ["npm", "test", "--", "--ci", "--runInBand", "--json",
                "--coverage", "--coverageReporters=lcov"]
        summary = "Run Jest in CI mode and collect JSON results and LCOV coverage."
    else:
        argv = ["npm", "test", "--", "--ci", "--reporter=json"]
        summary = "Run the project's npm test script in CI mode."
    return {
        "test_strategy": summary,
        "commands": [_cmd(argv, f"Execute {test_tool} test suite")],
        "environment_vars": {"CI": "true", "NODE_ENV": "test"},
        "expected_output": {"exit_code": 0, "indicators": ["pass", "numPassedTests"]},
        "timeout_seconds": 240,
    }


def _go_plan() -> Dict[str, Any]:
    return {
        "test_strategy": "Run all Go packages with coverage profiling enabled.",
        "commands": [
            _cmd(["go", "test", "-json", "./...", "-coverprofile=coverage.out"],
                 "Run Go tests with coverage"),
        ],
        "environment_vars": {"GO111MODULE": "on"},
        "expected_output": {"exit_code": 0, "indicators": ["ok", "PASS"]},
        "timeout_seconds": 240,
    }


def _java_plan(tool: str) -> Dict[str, Any]:
    if tool == "gradle-test":
        argv = ["gradle", "test", "--console=plain"]
    else:
        argv = ["mvn", "-B", "test"]
    return {
        "test_strategy": f"Run the {tool} test phase in batch/CI mode.",
        "commands": [_cmd(argv, "Execute Java test phase")],
        "environment_vars": {"CI": "true"},
        "expected_output": {"exit_code": 0, "indicators": ["BUILD SUCCESS", "Tests run"]},
        "timeout_seconds": 300,
    }


def _rust_plan() -> Dict[str, Any]:
    return {
        "test_strategy": "Run the full Cargo test suite.",
        "commands": [_cmd(["cargo", "test", "--all-targets"], "Run cargo tests")],
        "environment_vars": {"CARGO_TERM_COLOR": "never"},
        "expected_output": {"exit_code": 0, "indicators": ["test result: ok"]},
        "timeout_seconds": 300,
    }


def _generic_plan(language: str) -> Dict[str, Any]:
    if language == "python":
        return _unittest_plan()
    if language in {"javascript", "typescript"}:
        return _npm_plan("npm-test")
    return {
        "test_strategy": (
            "No specialized runner detected; perform a safe environment "
            "sanity check and record that no automated suite was found."
        ),
        "commands": [
            _cmd(["python", "-m", "unittest", "discover", "-v"],
                 "Attempt generic unittest discovery", optional=True),
        ],
        "environment_vars": {},
        "expected_output": {"exit_code": 0, "indicators": []},
        "timeout_seconds": 120,
    }


PLAN_FACTORIES = {
    "pytest": lambda _: _pytest_plan(),
    "unittest": lambda _: _unittest_plan(),
    "nose": lambda _: _unittest_plan(),
    "jest": lambda t: _npm_plan(t),
    "vitest": lambda t: _npm_plan(t),
    "mocha": lambda t: _npm_plan(t),
    "npm-test": lambda t: _npm_plan(t),
    "jasmine": lambda t: _npm_plan(t),
    "go-test": lambda _: _go_plan(),
    "maven-surefire": lambda _: _java_plan("maven-surefire"),
    "junit": lambda _: _java_plan("maven-surefire"),
    "gradle-test": lambda _: _java_plan("gradle-test"),
    "cargo-test": lambda _: _rust_plan(),
}


def build_plan(classification: Dict[str, Any]) -> Dict[str, Any]:
    """Pure planning function; returns the exact output contract."""
    tool = str(classification.get("test_tool") or "generic").lower()
    language = str(classification.get("language") or "unknown").lower()
    factory = PLAN_FACTORIES.get(tool)
    plan = factory(tool) if factory else _generic_plan(language)

    # Enforce the contract and re-validate every generated command.
    plan["commands"] = [
        _cmd(
            item["command"],
            item.get("description", "test command"),
            item.get("phase", "test"),
            bool(item.get("optional", False)),
        )
        for item in plan["commands"]
    ]
    plan["environment_vars"] = validate_environment(plan.get("environment_vars", {}))
    plan["test_strategy"] = str(plan.get("test_strategy", ""))
    plan["expected_output"] = dict(plan.get("expected_output", {}))
    plan["timeout_seconds"] = int(plan.get("timeout_seconds", 180))
    return plan


def planning_node(state: WorkflowState) -> Dict[str, Any]:
    """LangGraph node: generate a test plan from the classification state."""
    logger.info(
        "Planning tests: project_type=%s framework=%s tool=%s language=%s",
        state.get("project_type"), state.get("framework"),
        state.get("test_tool"), state.get("language"),
    )
    try:
        plan = build_plan({
            "project_type": state.get("project_type"),
            "framework": state.get("framework"),
            "test_tool": state.get("test_tool"),
            "language": state.get("language"),
        })
        logger.info("Plan ready: %d command(s), strategy=%s",
                    len(plan["commands"]), plan["test_strategy"][:60])
        return {
            **plan,
            "current_agent": "planner",
            "logs": [f"[{utc_now()}] Generated test plan with {len(plan['commands'])} command(s)"],
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("Planning failed")
        raise
