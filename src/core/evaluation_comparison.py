"""Compare two saved evaluations using their pytest case results."""

from src.utils.test_results import validate_pytest_cases


def _comparison_input(report: dict) -> tuple[dict, dict[str, str]]:
    evaluation = report["evaluation"]
    stages = {row["stage"]: row["output_data"] or {} for row in report["results"]}
    execution = stages.get("execution", {})
    reporting = stages.get("reporting", {})
    detailed = reporting.get("detailed_report") or {}
    metrics = reporting.get("metrics") or {}
    raw_cases = execution.get("test_case_results")
    cases = validate_pytest_cases(raw_cases)
    provenance = execution.get("provenance") or {}
    metadata = {
        "evaluation_id": evaluation["id"],
        "project_name": evaluation.get("project_name"),
        "project_path": evaluation.get("project_path"),
        "started_at": evaluation.get("started_at"),
        "test_tool": evaluation.get("test_tool"),
        "outcome": metrics.get("outcome"),
        "pass_rate": metrics.get("pass_rate"),
        "test_counts": metrics.get("test_counts"),
        "coverage_percent": metrics.get("coverage_percent"),
        "snapshot": provenance.get("snapshot"),
        "git": provenance.get("git"),
        "runtime": provenance.get("runtime"),
        "quality_gate": metrics.get("quality_gate"),
        "exclude_dirs": (detailed.get("plan") or {}).get("exclude_dirs"),
        "case_status": "unavailable",
        "case_reason": "case_data_missing" if raw_cases is None else "case_data_invalid",
        "selected_count": None,
        "recorded_count": 0,
    }
    if cases is None:
        return metadata, {}
    items = {case["node_id"]: case["outcome"] for case in cases["cases"]}
    metadata.update(
        case_status=cases["status"], case_reason=cases["reason"],
        selected_count=cases["selected_count"], recorded_count=len(items),
    )
    return metadata, items


def _context_warnings(baseline: dict, current: dict) -> list[str]:
    warnings = []
    for field in ("project_path", "runtime"):
        if baseline[field] is not None and current[field] is not None and baseline[field] != current[field]:
            warnings.append(f"{field}_changed")
    before, after = baseline["exclude_dirs"], current["exclude_dirs"]
    if isinstance(before, list) and isinstance(after, list) and sorted(before) != sorted(after):
        warnings.append("exclude_dirs_changed")
    gates = baseline["quality_gate"], current["quality_gate"]
    if all(gate is not None for gate in gates):
        policies = [(gate.get("policy_version"), gate.get("min_coverage")) for gate in gates]
        if policies[0] != policies[1]:
            warnings.append("quality_gate_policy_changed")
    return warnings


def compare_evaluations(baseline_report: dict, current_report: dict) -> dict:
    baseline, before = _comparison_input(baseline_report)
    current, after = _comparison_input(current_report)
    full = baseline["case_status"] == current["case_status"] == "complete"
    status = "complete" if full else "partial" if before and after else "unavailable"
    changes: dict[str, list[dict]] = {
        "new_failures": [], "fixed": [], "added": [], "removed": [], "skip_changes": [],
    }
    common = before.keys() & after.keys()
    for node_id in sorted(common):
        previous, outcome = before[node_id], after[node_id]
        row = {"node_id": node_id, "before": previous, "after": outcome}
        if outcome == "failed" and previous != "failed":
            changes["new_failures"].append(row)
        if previous == "failed" and outcome == "passed":
            changes["fixed"].append(row)
        if previous != outcome and "skipped" in (previous, outcome):
            changes["skip_changes"].append(row)
    if full:
        changes["added"] = [
            {"node_id": node_id, "before": None, "after": after[node_id]}
            for node_id in sorted(after.keys() - before.keys())
        ]
        changes["removed"] = [
            {"node_id": node_id, "before": before[node_id], "after": None}
            for node_id in sorted(before.keys() - after.keys())
        ]
    return {
        "status": status,
        "baseline": baseline,
        "current": current,
        "warnings": _context_warnings(baseline, current),
        "matched_count": len(common),
        "counts": {name: len(rows) for name, rows in changes.items()},
        "changes": changes,
    }
