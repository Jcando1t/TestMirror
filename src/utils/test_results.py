"""Count tests from runner output without treating tooling commands as tests."""

import json
import re
from pathlib import Path
from typing import Literal, TypedDict


class TestCounts(TypedDict):
    passed: int
    failed: int
    skipped: int


class TestCaseResult(TypedDict):
    node_id: str
    outcome: Literal["passed", "failed", "skipped"]


class PytestCaseResults(TypedDict):
    format: Literal["pytest-nodeid-v1"]
    status: Literal["complete", "incomplete", "unavailable"]
    reason: str | None
    selected_count: int | None
    cases: list[TestCaseResult]


def validate_pytest_cases(data: object) -> PytestCaseResults | None:
    if (not isinstance(data, dict) or data.get("format") != "pytest-nodeid-v1"
            or not {"status", "reason", "selected_count", "cases"} <= data.keys()):
        return None
    cases = data.get("cases")
    count = data.get("selected_count")
    status = data.get("status")
    reason = data.get("reason")
    if count is not None and (type(count) is not int or count < 0):
        return None
    if not isinstance(cases, list) or not all(
        isinstance(case, dict)
        and isinstance(case.get("node_id"), str) and case["node_id"]
        and case.get("outcome") in ("passed", "failed", "skipped") for case in cases
    ):
        return None
    if len({case["node_id"] for case in cases}) != len(cases):
        return None
    if status == "complete":
        if cases and count == len(cases) and reason is None:
            return data
    elif isinstance(reason, str) and reason:
        if (status == "incomplete" and cases) or (status == "unavailable" and not cases):
            return data
    return None


def read_pytest_cases(path: Path, exit_code: int) -> PytestCaseResults:
    if exit_code in (124, 126, 127):
        reason = "timeout" if exit_code == 124 else "runner_unavailable"
    else:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            reason = "case_report_missing_or_invalid"
        else:
            report = validate_pytest_cases(data)
            if report is not None and (report["status"] != "complete" or exit_code in (0, 1)):
                return report
            reason = "case_report_missing_or_invalid"
    return {
        "format": "pytest-nodeid-v1", "status": "unavailable", "reason": reason,
        "selected_count": None, "cases": [],
    }


def parse_test_counts(stdout: str, stderr: str = "", tool: str | None = None) -> TestCounts | None:
    text = stdout + "\n" + stderr
    if tool == "jest" or (tool is None and '"numPassedTests"' in stdout):
        for line in reversed(stdout.splitlines()):
            try:
                result = json.loads(line)
            except ValueError:
                continue
            keys = ("numPassedTests", "numFailedTests", "numPendingTests", "numTodoTests")
            if (isinstance(result, dict) and all(k in result for k in keys[:2])
                    and all(type(result.get(k, 0)) is int and result.get(k, 0) >= 0 for k in keys)):
                return TestCounts(
                    passed=result["numPassedTests"], failed=result["numFailedTests"],
                    skipped=result.get("numPendingTests", 0) + result.get("numTodoTests", 0),
                )
    if tool == "go-test" or (tool is None and '"Action"' in stdout and '"Package"' in stdout):
        counts = TestCounts(passed=0, failed=0, skipped=0)
        events_seen = False
        actions = {"pass": "passed", "fail": "failed", "skip": "skipped"}
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("Action") not in actions:
                continue
            events_seen = True
            test = event.get("Test")
            if isinstance(test, str) and test and "/" not in test:  # Do not double-count subtests.
                counts[actions[event["Action"]]] += 1
        if events_seen:
            return counts
    if tool in (None, "pytest", "jest"):
        # Warnings can appear in the summary but do not count as test cases.
        summaries = re.findall(
            r"^\s*(?:=+\s*)?(?:Tests:\s*)?((?:\d+ (?:subtests? (?:passed|failed|skipped)|passed|failed|skipped|pending|todo|errors?|xfailed|xpassed|deselected|warnings?)(?:,\s*|\b))+)"
            r"(?=\s*(?:in\b|=|\d+ total|$))",
            text, re.MULTILINE | re.IGNORECASE,
        )
        if summaries:
            terms = {kind.lower(): int(n) for n, kind in re.findall(r"(\d+) (\w+)", summaries[-1])}
            return TestCounts(
                passed=terms.get("passed", 0) + terms.get("xpassed", 0),
                failed=terms.get("failed", 0) + terms.get("error", 0) + terms.get("errors", 0),
                skipped=sum(terms.get(kind, 0) for kind in ("skipped", "pending", "todo", "xfailed")),
            )
    if tool in (None, "unittest"):
        ran = re.search(r"^Ran (\d+) tests?\b", text, re.MULTILINE)
        if ran:
            summary = re.search(r"^(?:OK|FAILED)\s*\(([^\n]*)\)", text, re.MULTILINE)
            terms = {kind: int(n) for kind, n in re.findall(r"([\w ]+)=(\d+)", summary[1] if summary else "")}
            terms = {key.strip(): value for key, value in terms.items()}
            failed = sum(terms.get(kind, 0) for kind in ("failures", "errors", "unexpected successes"))
            skipped = terms.get("skipped", 0) + terms.get("expected failures", 0)
            return TestCounts(passed=max(0, int(ran[1]) - failed - skipped), failed=failed, skipped=skipped)
    return None


def pass_rate_from_counts(counts: TestCounts | None) -> float | None:
    if not counts or not (total := counts["passed"] + counts["failed"]):
        return None
    return round(counts["passed"] / total, 4)
