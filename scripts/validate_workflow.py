#!/usr/bin/env python3
"""End-to-end workflow validation script.

Usage:
    python scripts/validate_workflow.py [--project PATH] [--db PATH]

Creates a temporary demo project when no --project is given, runs the complete
five-agent LangGraph workflow, verifies the database records, and exits non-zero
if any invariant is violated.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.langgraph_orchestrator import WorkflowEngine  # noqa: E402
from src.models.database import Database  # noqa: E402


def create_demo_project(root: Path) -> Path:
    (root / "pyproject.toml").write_text(
        '[project]\nname = "testmirror-demo"\nversion = "1.0.0"\n\n'
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        encoding="utf-8",
    )
    tests = root / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "test_demo.py").write_text(
        "def test_always_passes():\n    assert True\n\n"
        "def test_math():\n    assert 2 * 3 == 6\n",
        encoding="utf-8",
    )
    return root


def check(condition: bool, label: str, failures: list[str]) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    if not condition:
        failures.append(label)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the TestMirror workflow")
    parser.add_argument("--project", help="Project directory to evaluate")
    parser.add_argument("--db", default=str(ROOT / "data" / "validation.db"),
                        help="SQLite path for evaluation history")
    args = parser.parse_args()

    print("=" * 68)
    print("TestMirror workflow validation")
    print("=" * 68)

    with tempfile.TemporaryDirectory(prefix="testmirror_demo_") as temp:
        project = Path(args.project).resolve() if args.project else create_demo_project(Path(temp))
        if not project.is_dir():
            print(f"ERROR: project directory not found: {project}")
            return 2
        print(f"Project : {project}")
        print(f"Database: {args.db}\n")

        failures: list[str] = []
        try:
            with WorkflowEngine(args.db, max_retries=1, use_checkpointer=False) as engine:
                final = engine.run(str(project), evaluation_id="validation-run")
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: workflow execution raised {type(exc).__name__}: {exc}")
            return 3

        print("\nWorkflow results:")
        check(final.get("project_type") is not None, "classification completed", failures)
        check(bool(final.get("test_strategy")), "test plan generated", failures)
        check(bool(final.get("command_results")), "commands executed in sandbox", failures)
        check(bool(final.get("analysis")), "analysis completed", failures)
        check(final.get("exit_code") == 0, "test commands passed", failures)
        check(bool(final.get("summary")), "report generated", failures)
        score = final.get("overall_score")
        coverage = final.get("metrics", {}).get("coverage_percent")
        score_valid = score is None if coverage is None else isinstance(score, (int, float)) and 0 <= score <= 100
        check(score_valid, "score matches available coverage", failures)
        check(bool(final.get("recommendations")), "recommendations present", failures)
        check(final.get("persisted") is True, "results persisted", failures)

        print("\nDatabase verification:")
        with Database(args.db) as db:
            stored = db.get_full_report("validation-run")
            check(stored is not None, "evaluation row exists", failures)
            if stored:
                check(len(stored["results"]) == 5, "five agent results stored", failures)
                check(stored["evaluation"]["status"] == "passed", "status recorded as passed", failures)
            for table in ("evaluations", "results", "errors", "recommendations"):
                check(db.count(table) >= (1 if table in {"evaluations", "results", "recommendations"} else 0),
                      f"{table} table queryable", failures)

        print("\nSummary:")
        summary = {
            "evaluation_id": final.get("evaluation_id"),
            "status": final.get("status"),
            "score": final.get("overall_score"),
            "summary": final.get("summary"),
            "classification": {
                "project_type": final.get("project_type"),
                "framework": final.get("framework"),
                "test_tool": final.get("test_tool"),
                "language": final.get("language"),
            },
            "execution_time": final.get("execution_time"),
            "coverage": final.get("coverage_data"),
            "recommendations": final.get("recommendations"),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))

        print("\n" + "=" * 68)
        if failures:
            print(f"VALIDATION FAILED ({len(failures)} check(s)):")
            for item in failures:
                print(f"  - {item}")
            return 1
        print("VALIDATION PASSED - all workflow invariants satisfied")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
