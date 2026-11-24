"""Analysis Agent (LLM-backed): diagnose test execution results.

This agent takes a raw test execution result (stdout, stderr, exit code and
coverage data) and produces a structured diagnosis:

    {issues, severity, root_cause, recommendations}

The workflow is two-stage:

1. A deterministic parser extracts objective signals -- pass/fail counts, error
   lines, tracebacks and coverage -- from the captured output.
2. Those signals (plus the truncated raw output) are sent to the DeepSeek API
   for root-cause analysis and actionable fix recommendations.

When the LLM is unavailable or returns an unparseable payload, the agent
degrades gracefully to the deterministic diagnosis so callers always receive a
usable :class:`AnalysisResult`.

Integration contract (see :mod:`src.core.langgraph_orchestrator`)::

    analysis_node(state) -> {"analysis": AnalysisResult, "current_agent": "analyst", ...}
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from src.core.deepseek_client import DeepSeekClient, DeepSeekError, extract_json
from src.models.state import AnalysisResult, ExecutionResult, WorkflowState
from src.utils.helpers import truncate, utc_now
from src.utils.logging_config import get_logger
from src.utils.test_results import parse_test_counts, pass_rate_from_counts

logger = get_logger(__name__)

_SEVERITIES = {"critical", "high", "medium", "low", "pass"}
_SEVERITY_PRIORITY = {
    "critical": "high",
    "high": "high",
    "medium": "medium",
    "low": "low",
    "pass": "low",
}

# ---- local output parsing patterns --------------------------------------- #
_ERROR_LINE = re.compile(
    r"^\s*(?:E\s+|FAILED|ERROR:|Error:|AssertionError|[A-Za-z]+(?:Error|Exception):|Traceback|panic:|---\s+FAIL:|\u2715|\u2717|\u25CF)\s*(?P<text>.*)$",
    re.MULTILINE,
)
_LOCATION = re.compile(r"(?P<loc>[^\s\"']+\.(?:py|js|ts|go):\d+(?::\d+)?)")
_PYTEST_LOCATION = re.compile(r"^(?P<loc>.+\.py:\d+): [\w.]+(?::.*)?$")


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_failures(text: str, limit: int = 12) -> List[Dict[str, str]]:
    """Pull representative failure lines and their source locations."""
    issues: List[Dict[str, str]] = []
    pytest_error: Dict[str, str] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if pytest_error is not None:
            if location_match := _PYTEST_LOCATION.fullmatch(stripped):
                pytest_error["location"] = location_match.group("loc")
            if not stripped.startswith("E "):
                pytest_error = None
        if len(issues) >= limit:
            if pytest_error is None:
                break
            continue
        match = _ERROR_LINE.match(stripped)
        if not match:
            continue
        message = match.group("text").strip()
        detail = message[:300]
        location = ""
        loc = _LOCATION.search(stripped)
        if loc:
            location = loc.group("loc")
        if detail or location:
            title = detail[:80] or "Test failure"
            if stripped.startswith("FAILED ") and "::" in message:
                title = message
            issue = {
                "title": title,
                "detail": detail,
                "location": location,
            }
            issues.append(issue)
            if pytest_error is None and stripped.startswith("E "):
                pytest_error = issue
    return issues


def _parse_test_output(result: Dict[str, Any]) -> AnalysisResult:
    """Deterministic diagnosis used both as LLM context and as the fallback."""
    exit_code = int(result.get("exit_code") or 0)
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    primary = [cmd for cmd in result.get("command_results", [])
               if not cmd.get("optional") and cmd.get("phase", "test") == "test"]
    combined = ("\n".join((cmd.get("stdout") or "") + "\n" + (cmd.get("stderr") or "")
                          for cmd in primary) if primary else f"{stdout}\n{stderr}")
    coverage = result.get("coverage_data") or {}
    percent = _to_float(coverage.get("percent_covered")) if isinstance(coverage, dict) else None

    counts = (
        None if result.get("failure_kind") == "execution_failed"
        else result["test_counts"] if "test_counts" in result else parse_test_counts(stdout, stderr)
    )
    failed = counts["failed"] if counts is not None else 0

    issues = _extract_failures(combined)
    pass_rate = pass_rate_from_counts(counts)

    # ---- severity -------------------------------------------------------- #
    if result.get("failure_kind") == "execution_failed" or (exit_code != 0 and not issues and failed == 0):
        severity = "critical"  # infrastructure failure, no tests recognized
    elif exit_code != 0 or failed:
        severity = "high"
    elif percent is not None and percent < 60:
        severity = "medium"
    elif exit_code == 0:
        severity = "pass" if percent is None or percent >= 80 else "low"
    else:
        severity = "low"

    # ---- root cause ------------------------------------------------------- #
    if exit_code == 0 and failed == 0:
        root_cause = "The test suite completed successfully."
    elif issues or failed:
        first = f" First failure: {issues[0]['title']}." if issues else ""
        root_cause = (
            f"{failed} failing test(s) detected." + first
            if failed else f"Failure output: {issues[0]['title']}."
        )
    else:
        root_cause = (
            f"Test execution exited with code {exit_code} but no individual test "
            "failure could be identified; this usually indicates an environment, "
            "build or runner configuration problem."
        )

    # ---- recommendations -------------------------------------------------- #
    recommendations: List[Dict[str, str]] = []
    if exit_code != 0 or failed:
        recommendations.append({
            "title": "Fix the failing tests",
            "message": root_cause,
            "category": "tests",
            "priority": _SEVERITY_PRIORITY.get(severity, "high"),
        })
        if stderr.strip():
            recommendations.append({
                "title": "Inspect runner stderr",
                "message": stderr.strip()[-500:],
                "category": "diagnostics",
                "priority": "high",
            })
    if percent is None:
        recommendations.append({
            "title": "Enable coverage reporting",
            "message": "No coverage data was captured; configure coverage output for this test runner.",
            "category": "coverage",
            "priority": "medium",
        })
    elif percent < 60:
        recommendations.append({
            "title": "Increase test coverage",
            "message": f"Current coverage is {percent}%; aim for at least 60-80%.",
            "category": "coverage",
            "priority": "medium",
        })
    if not recommendations:
        recommendations.append({
            "title": "Keep the suite green",
            "message": "All checks passed; monitor coverage trends and flaky tests.",
            "category": "maintenance",
            "priority": "low",
        })

    return AnalysisResult(
        issues=list(issues),
        test_evidence=issues,
        severity=severity,
        root_cause=root_cause,
        recommendations=recommendations,
        summary=(
            f"{counts['passed']} passed, {failed} failed, {counts['skipped']} skipped" if counts is not None
            else f"exit code {exit_code}, no test counts detected"
        ),
        pass_rate=pass_rate,
    )


_SYSTEM_PROMPT = (
    "You are TestMirror's test-failure analysis expert. Diagnose the supplied "
    "test execution output. Respond with ONLY a JSON object (no markdown) with "
    "exactly these keys:\n"
    '{\n'
    '  "issues": [{"title": "...", "detail": "...", "location": "..."}],\n'
    '  "severity": "<critical|high|medium|low|pass>",\n'
    '  "root_cause": "<one-paragraph root-cause analysis>",\n'
    '  "recommendations": [{"title": "...", "message": "..."}]\n'
    "}\n"
    "Ground each issue in the supplied output. Use only locations explicitly shown "
    "there; leave location empty when no location is available. Distinguish observed "
    "failures from hypotheses about implementation details that are not visible. "
    "Recommend a minimal change conditional on confirming the cause. With only "
    "test output, describe the specific edit and preserve existing parameters, "
    "caller-provided data, and unaffected branches. Avoid replacing the whole "
    "function or introducing an unconditional return from partial evidence. "
    "Rerun the existing suite to check the failed case and preserve passing behavior. "
    "Avoid suggesting tests already shown in the output or changes to unrelated behavior. "
    "Provide at most 8 issues and 5 recommendations."
)


class AnalysisAgent:
    """Analyze a test execution result and produce a structured diagnosis."""

    def __init__(
        self,
        client: Optional[DeepSeekClient] = None,
        model: Optional[str] = None,
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> None:
        self.client = client or DeepSeekClient(model=model)
        self.temperature = temperature
        self.max_tokens = max_tokens

    # ---- public API ------------------------------------------------------ #
    def analyze(self, execution_result: ExecutionResult) -> AnalysisResult:
        """Return the diagnosis for ``execution_result`` (never raises on LLM issues)."""
        data = dict(execution_result)
        fallback = _parse_test_output(data)
        try:
            text = self._generate(data, fallback)
            result = self._parse(text, fallback)
            logger.info(
                "LLM analysis: severity=%s issues=%d recommendations=%d",
                result["severity"], len(result["issues"]), len(result["recommendations"]),
            )
            return result
        except (DeepSeekError, ValueError) as exc:
            logger.warning(
                "LLM analysis failed (%s); returning deterministic diagnosis",
                type(exc).__name__,
            )
            return fallback

    def analyze_state(self, state: WorkflowState) -> AnalysisResult:
        """Analyze a full workflow state.

        Combines two layers of diagnosis:

        1. *Workflow health* -- agent errors are aggregated
           into ``issues`` entries carrying an ``agent``/``message`` shape (the
           contract the report and persistence layers rely on).
        2. *Test output* -- the raw stdout/stderr/coverage is passed to the LLM
           (with a deterministic fallback) to extract test-failure issues and
           actionable recommendations.
        """
        from src.agents.report_agent import build_recommendations, compute_metrics

        metrics = compute_metrics(state)

        workflow_issues: List[Dict[str, str]] = [
            {"agent": error.get("agent", "unknown"), "message": str(error.get("message", ""))}
            for error in state.get("errors", [])
        ]
        execution_result: ExecutionResult = {
            "stdout": state.get("stdout", ""),
            "stderr": state.get("stderr", ""),
            "exit_code": state.get("exit_code", -1),
            "coverage_data": state.get("coverage_data", {}),
            "test_counts": state.get("test_counts"),
            "command_results": state.get("command_results", []),
            "execution_time": state.get("execution_time"),
        }
        llm = self.analyze(execution_result)

        issues = workflow_issues + [dict(i) for i in llm.get("issues", [])]

        recommendations: List[Dict[str, str]] = list(build_recommendations(state, metrics))
        seen = {(rec.get("source"), rec.get("title")) for rec in recommendations}
        for rec in llm.get("recommendations", []):
            key = (rec.get("source"), rec.get("title"))
            if key not in seen:
                recommendations.append(dict(rec))
                seen.add(key)

        if workflow_issues:
            severity = "high"
            root_cause = workflow_issues[0]["message"]
            summary = f"{len(workflow_issues)} workflow issue(s)"
        else:
            severity = llm.get("severity", "info")
            root_cause = llm.get("root_cause", "")
            summary = llm.get("summary", "")

        result = AnalysisResult(
            issues=issues,
            test_evidence=llm["test_evidence"],
            severity=severity,
            root_cause=root_cause,
            recommendations=recommendations,
            summary=summary or f"{len(issues)} issue(s)",
            pass_rate=metrics["pass_rate"],
            metrics=metrics,
        )
        if "model_diagnosis" in llm:
            result["model_diagnosis"] = llm["model_diagnosis"]
        return result

    # ---- internals ------------------------------------------------------- #
    def _generate(self, result: Dict[str, Any], fallback: AnalysisResult) -> str:
        user = json.dumps({
            "exit_code": result.get("exit_code", 0),
            "deterministic_summary": {
                "severity": fallback.get("severity"),
                "pass_rate": fallback.get("pass_rate"),
                "issues": fallback.get("issues", [])[:10],
            },
            "coverage": result.get("coverage_data") or {},
            "stdout": truncate(str(result.get("stdout") or ""), 8_000),
            "stderr": truncate(str(result.get("stderr") or ""), 8_000),
        }, ensure_ascii=False, default=str)

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]
        kwargs: Dict[str, Any] = {
            "json_mode": True,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        stream_method = getattr(self.client, "stream", None)
        if callable(stream_method):
            stream = stream_method(messages, **kwargs)
            chunks: List[str] = []
            for chunk in stream:
                if isinstance(chunk, str):
                    chunks.append(chunk)
            accumulated = getattr(stream, "text", None)
            return accumulated if accumulated is not None else "".join(chunks)

        response = self.client.chat(messages, **kwargs)
        content = getattr(response, "content", None)
        if content is not None:
            return str(content)
        return str(response)

    def _parse(self, text: str, fallback: AnalysisResult) -> AnalysisResult:
        data = extract_json(text)
        if not isinstance(data, dict):
            raise ValueError("Analysis response is not a JSON object")

        issues = self._coerce_issues(data.get("issues"))
        recommendations = self._coerce_recommendations(data.get("recommendations"))
        for recommendation in recommendations:
            recommendation["source"] = "deepseek"
        severity = str(data.get("severity") or "").strip().lower()
        if severity not in _SEVERITIES:
            severity = fallback.get("severity", "low")
        root_cause = str(data.get("root_cause") or "").strip()

        result = AnalysisResult(
            issues=issues or fallback.get("issues", []),
            test_evidence=fallback["test_evidence"],
            severity=severity,
            root_cause=root_cause or fallback.get("root_cause", ""),
            recommendations=recommendations or fallback.get("recommendations", []),
            summary=fallback.get("summary", ""),
            pass_rate=fallback.get("pass_rate"),
        )
        if issues or root_cause:
            result["model_diagnosis"] = {"issues": issues, "root_cause": root_cause}
        return result

    @staticmethod
    def _coerce_issues(value: Any) -> List[Dict[str, str]]:
        if not isinstance(value, list):
            return []
        issues: List[Dict[str, str]] = []
        for item in value:
            if isinstance(item, str):
                issues.append({"title": item, "detail": item, "location": ""})
            elif isinstance(item, dict):
                title = str(item.get("title") or item.get("message") or "Test issue")
                issues.append({
                    "title": title,
                    "detail": str(item.get("detail") or title),
                    "location": str(item.get("location") or ""),
                })
        return issues

    @staticmethod
    def _coerce_recommendations(value: Any) -> List[Dict[str, str]]:
        if not isinstance(value, list):
            return []
        recs: List[Dict[str, str]] = []
        for item in value:
            if isinstance(item, str):
                recs.append({"title": item, "message": item, "category": "general", "priority": "medium"})
            elif isinstance(item, dict):
                title = str(item.get("title") or item.get("message") or "Recommendation")
                recs.append({
                    "title": title,
                    "message": str(item.get("message") or title),
                    "category": str(item.get("category") or "general"),
                    "priority": str(item.get("priority") or "medium"),
                })
        return recs


def deterministic_analysis_node(state: WorkflowState) -> Dict[str, Any]:
    """Offline workflow default; preserve test findings and workflow failures."""
    from src.agents.report_agent import build_recommendations, compute_metrics

    result = _parse_test_output({**state, "exit_code": state.get("exit_code", -1)})
    metrics = compute_metrics(state)
    result["metrics"] = metrics
    if metrics["outcome"] == "execution_failed":
        errors = state.get("errors", [])
        message = errors[-1].get("message") if errors else None
        if not message:
            message = next(
                (cmd.get("stderr") or cmd.get("stdout") or "The test runner could not complete."
                 for cmd in state.get("command_results", []) if cmd.get("failure_kind") == "execution_failed"),
                "No completed test cases could be verified.",
            )
        result["issues"] = [{
            "agent": "executor", "title": "Test execution could not complete.",
            "detail": message, "location": "",
        }]
        result["root_cause"] = message
        result["severity"] = "critical"
        result["recommendations"] = build_recommendations(state, metrics)
    else:
        for error in state.get("errors", []):
            result["issues"].append({
                "agent": error.get("agent", "unknown"),
                "title": error.get("error_type", "Workflow error"),
                "detail": error.get("message", ""),
                "location": "",
            })
    if state.get("errors"):
        result["severity"] = "high"
        result["recommendations"] = build_recommendations(state, metrics)
    return {
        "analysis": dict(result),
        "current_agent": "analyst",
        "logs": [f"[{utc_now()}] Analyzed execution results without external API calls"],
    }


def analyze_results(state: WorkflowState) -> AnalysisResult:
    """Convenience wrapper: analyze a workflow state with a default client.

    Kept for backward compatibility with callers that used the original
    rule-based ``analyze_results`` entry point.
    """
    return AnalysisAgent().analyze_state(state)


def analysis_node(state: WorkflowState, client: Optional[DeepSeekClient] = None) -> Dict[str, Any]:
    """LangGraph node: analyze captured execution results.

    Returned contract matches the orchestrator / persistence layer:
    ``analysis`` holds the :class:`AnalysisResult`, and ``current_agent`` is
    ``"analyst"``.
    """
    logger.info("Analyzing execution results for %s", state.get("evaluation_id"))
    result = AnalysisAgent(client=client).analyze_state(state)
    return {
        "analysis": dict(result),
        "current_agent": "analyst",
        "logs": [
            f"[{utc_now()}] Analysis: severity={result['severity']}, "
            f"{len(result['issues'])} issue(s)"
        ],
    }
