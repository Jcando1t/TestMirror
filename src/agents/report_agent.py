"""Report Agent: aggregate every agent's output into a final evaluation report."""

from __future__ import annotations

from typing import Any, Dict, List

from src.models.state import WorkflowState
from src.utils.helpers import utc_now
from src.utils.logging_config import get_logger
from src.utils.test_results import pass_rate_from_counts

logger = get_logger(__name__)


def _percentage(value: Any) -> float | None:
    try:
        percent = float(value)
    except (TypeError, ValueError):
        return None
    return percent if 0 <= percent <= 100 else None


def compute_metrics(state: Dict[str, Any]) -> Dict[str, Any]:
    """Derive objective metrics from the shared workflow state."""
    commands: List[Dict[str, Any]] = state.get("command_results", [])
    executed = len(commands)
    failed = sum(
        1 for c in commands
        if c.get("exit_code", 0) != 0 and not c.get("optional")
        and c.get("phase", "test") == "test"
    )
    coverage_data = state.get("coverage_data") or {}
    coverage = _percentage(coverage_data.get("percent_covered"))
    errors = len(state.get("errors", []))
    exit_code = int(state.get("exit_code", -1))
    counts = state.get("test_counts")
    pass_rate = pass_rate_from_counts(counts)
    required = [cmd for cmd in commands if not cmd.get("optional") and cmd.get("phase", "test") == "test"]
    if (state.get("status") == "error"
            or any(cmd.get("failure_kind") == "execution_failed" for cmd in required)
            or pass_rate is None
            or (exit_code != 0 and not counts["failed"])):
        outcome = "execution_failed"
    elif failed or exit_code or (counts and counts["failed"]):
        outcome = "test_failed"
    else:
        outcome = "passed"
    min_coverage = state.get("min_coverage")
    gate_reason = outcome
    if outcome == "passed" and min_coverage is not None:
        if coverage is None:
            gate_reason = "coverage_unavailable"
        elif coverage < min_coverage:
            gate_reason = "coverage_below_minimum"

    metrics = {
        "success": outcome == "passed",
        "outcome": outcome,
        "exit_code": exit_code,
        "commands_executed": executed,
        "commands_failed": failed,
        "test_counts": counts,
        "pass_rate": pass_rate if outcome != "execution_failed" else None,
        "execution_time": round(float(state.get("execution_time") or 0.0), 4),
        "coverage_percent": coverage,
        "coverage_source": state.get("coverage_data", {}).get("source"),
        "error_count": errors,
        "retry_count": int(state.get("retry_count") or 0),
        "project_type": state.get("project_type"),
        "framework": state.get("framework"),
        "test_tool": state.get("test_tool"),
        "language": state.get("language"),
        "classification_confidence": state.get("classification_confidence"),
        "quality_gate": {
            "policy_version": 1,
            "passed": gate_reason == "passed",
            "reason": gate_reason,
            "min_coverage": min_coverage,
        },
    }
    return metrics


def compute_score(metrics: Dict[str, Any]) -> float | None:
    """Compute a reference score when pass rate and coverage are both known."""
    pass_rate = metrics["pass_rate"]
    coverage = metrics["coverage_percent"]
    if pass_rate is None or coverage is None:
        return None
    score = 60.0 * pass_rate + 30.0 * coverage / 100.0
    score += 10.0 if metrics["error_count"] == 0 else max(0.0, 10.0 - 5 * metrics["error_count"])
    return round(min(100.0, max(0.0, score)), 2)


def build_recommendations(state: Dict[str, Any], metrics: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Produce actionable, prioritized recommendations."""
    recs: List[Dict[str, Any]] = []

    if metrics["outcome"] == "execution_failed":
        recs.append({
            "category": "execution",
            "priority": "high",
            "title": "Check the test runner and environment",
            "message": "Inspect failed command output and workflow errors before retrying.",
        })
    elif metrics["exit_code"] != 0:
        recs.append({
            "category": "tests",
            "priority": "high",
            "title": "Fix failing test commands",
            "message": (
                f"Primary test execution exited with code {metrics['exit_code']}. "
                "Inspect the execution stderr/stdout in the detailed report."
            ),
        })

    for cmd in state.get("command_results", []):
        if cmd.get("exit_code", 0) != 0 and not cmd.get("optional"):
            snippet = (cmd.get("stderr") or cmd.get("stdout") or "").strip()[-500:]
            recs.append({
                "category": "execution" if cmd.get("failure_kind") == "execution_failed" else "tests",
                "priority": "high",
                "title": f"Command failed: {' '.join(cmd.get('command', []))[:120]}",
                "message": snippet or "No diagnostic output was captured.",
            })

    coverage = metrics.get("coverage_percent")
    if coverage is None:
        recs.append({
            "category": "coverage",
            "priority": "medium",
            "title": "Enable coverage reporting",
            "message": (
                "No coverage report was found. Configure pytest-cov, istanbul/nyc, "
                "go coverprofile or equivalent so coverage can be tracked."
            ),
        })
    elif coverage < 60:
        recs.append({
            "category": "coverage",
            "priority": "medium",
            "title": "Increase test coverage",
            "message": f"Current coverage is {coverage}%. Consider a target of at least 60-80%.",
        })

    if metrics["error_count"] > 0:
        recs.append({
            "category": "reliability",
            "priority": "high",
            "title": "Resolve workflow errors",
            "message": f"{metrics['error_count']} agent error(s) were recorded; see the errors table.",
        })

    if metrics["commands_executed"] == 0:
        recs.append({
            "category": "tests",
            "priority": "high",
            "title": "No automated tests detected",
            "message": "Add a test suite and configure a recognized test runner.",
        })

    if not recs:
        recs.append({
            "category": "maintenance",
            "priority": "low",
            "title": "Pipeline healthy",
            "message": "All checks passed. Keep the suite green and review coverage trends.",
        })
    return recs


def build_report(state: Dict[str, Any]) -> Dict[str, Any]:
    """Build the complete Report Agent output contract."""
    analysis = state.get("analysis") or {}
    metrics = compute_metrics(state)
    score = compute_score(metrics)
    recommendations = analysis.get("recommendations") or build_recommendations(state, metrics)
    timestamp = utc_now()

    if metrics["outcome"] == "execution_failed":
        summary = f"Test execution could not complete (exit code {metrics['exit_code']})."
        if state.get("status") == "error" and state.get("errors"):
            summary += f" {state['errors'][-1]['message']}"
        elif clues := analysis.get("test_evidence", analysis.get("issues", [])):
            detail = clues[0].get("detail") or clues[0].get("message")
            if detail:
                summary += f" {detail}"
    elif metrics["success"]:
        summary = (
            "Tests passed. "
            f"{metrics['commands_executed']} command(s) executed in "
            f"{metrics['execution_time']}s; "
            f"coverage {metrics['coverage_percent'] if metrics['coverage_percent'] is not None else 'n/a'}%."
        )
    else:
        summary = (
            "Tests failed "
            f"(exit code {metrics['exit_code']}, "
            f"{metrics['commands_failed']}/{metrics['commands_executed']} commands failed). "
            f"{len(recommendations)} recommendation(s) generated."
        )

    detailed = {
        "evaluation_id": state.get("evaluation_id"),
        "project": {
            "path": state.get("project_path"),
            "name": state.get("project_name"),
            "type": state.get("project_type"),
            "framework": state.get("framework"),
            "language": state.get("language"),
            "test_tool": state.get("test_tool"),
            "classification_confidence": state.get("classification_confidence"),
            "classification_evidence": state.get("classification_evidence", []),
        },
        "plan": {
            "strategy": state.get("test_strategy"),
            "exclude_dirs": state.get("exclude_dirs", []),
            "commands": state.get("commands", []),
            "environment_vars": state.get("environment_vars", {}),
            "expected_output": state.get("expected_output", {}),
            "timeout_seconds": state.get("timeout_seconds"),
        },
        "execution": {
            "exit_code": state.get("exit_code"),
            "provenance": state.get("execution_provenance"),
            "execution_time": state.get("execution_time"),
            "coverage": state.get("coverage_data", {}),
            "test_counts": state.get("test_counts"),
            "test_case_results": state.get("test_case_results"),
            "commands": state.get("command_results", []),
            "stdout": state.get("stdout", ""),
            "stderr": state.get("stderr", ""),
        },
        "analysis": analysis,
        "llm_usage": state.get("llm_usage", {}),
        "errors": state.get("errors", []),
        "workflow_logs": state.get("logs", []),
    }

    return {
        "summary": summary,
        "detailed_report": detailed,
        "metrics": metrics,
        "recommendations": recommendations,
        "timestamp": timestamp,
        "overall_score": score,
        "evaluation_status": "passed" if metrics["success"] else "failed",
        "status": "running",
    }


def report_node(state: WorkflowState) -> Dict[str, Any]:
    """LangGraph node: aggregate results, even when earlier agents failed."""
    logger.info("Generating report for %s", state.get("evaluation_id"))
    try:
        report = build_report(state)
        logger.info("Report ready: score=%s, %d recommendation(s)",
                    report["overall_score"], len(report["recommendations"]))
        return {
            **report,
            "current_agent": "reporter",
            "logs": [f"[{utc_now()}] Generated report with score {report['overall_score']}"],
        }
    except Exception:  # noqa: BLE001
        logger.exception("Report generation failed")
        raise
