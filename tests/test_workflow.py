"""Integration tests for the complete TestMirror multi-agent workflow.

Run with:  python -m pytest tests/test_workflow.py -v
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agents.classifier_agent import classify_project  # noqa: E402
from src.agents.execution_agent import execute_plan, parse_coverage  # noqa: E402
from src.agents.planning_agent import build_plan  # noqa: E402
from src.agents.report_agent import build_report  # noqa: E402
from src.core import langgraph_orchestrator as orchestrator  # noqa: E402
from src.core.langgraph_orchestrator import WorkflowEngine, run_workflow  # noqa: E402
from src.models.database import Database  # noqa: E402
from src.models.state import initial_state  # noqa: E402
from src.utils.security import (  # noqa: E402
    CommandValidationError,
    Sandbox,
    validate_command,
)


@pytest.fixture()
def sample_project(tmp_path: Path) -> Path:
    """A minimal but realistic Python/pytest project."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n\n'
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        encoding="utf-8",
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_example.py").write_text(
        "def test_addition():\n    assert 1 + 1 == 2\n\n"
        "def test_string():\n    assert 'ok'.upper() == 'OK'\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture()
def failing_project(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "broken"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_broken.py").write_text(
        "def test_pass():\n    assert True\n\n"
        "def test_failure():\n    assert False, 'intentional failure'\n\n"
        "def test_second_failure():\n    assert False, 'another intentional failure'\n",
        encoding="utf-8",
    )
    return tmp_path


# ---- individual agent contracts ----------------------------------------

def test_classifier_contract(sample_project: Path):
    result = classify_project(str(sample_project))
    for key in ("project_type", "framework", "test_tool", "language"):
        assert key in result, f"missing classification key: {key}"
    assert result["project_type"] == "python"
    assert result["test_tool"] == "pytest"
    assert result["language"] == "python"
    assert 0 < result["confidence"] <= 1
    assert result["evidence"]


def test_planner_contract(sample_project: Path):
    classification = classify_project(str(sample_project))
    plan = build_plan(classification)
    for key in ("test_strategy", "commands", "environment_vars", "expected_output"):
        assert key in plan, f"missing plan key: {key}"
    assert isinstance(plan["test_strategy"], str) and plan["test_strategy"]
    assert len(plan["commands"]) >= 1
    for cmd in plan["commands"]:
        assert validate_command(cmd["command"]) == cmd["command"]
    assert plan["timeout_seconds"] > 0
    assert plan["expected_output"]["exit_code"] == 0


def test_execution_contract_and_isolation(sample_project: Path):
    plan = build_plan(classify_project(str(sample_project)))
    result = execute_plan(plan, str(sample_project))
    for key in ("exit_code", "stdout", "stderr", "execution_time", "coverage_data"):
        assert key in result, f"missing execution key: {key}"
    assert result["exit_code"] == 0, result["stderr"]
    assert result["execution_time"] > 0
    assert len(result["command_results"]) == len(plan["commands"])
    # This fixture has only one measured source file, even in an editable install.
    assert result["coverage_data"]["files"] == 1
    assert result["coverage_data"]["percent_covered"] == 100.0
    # Sandbox isolation: no artifacts leaked into the source project.
    assert not (sample_project / "coverage.json").exists()
    assert not (sample_project / "test-results.xml").exists()
    assert "passed" in result["stdout"] or "test" in result["stdout"].lower()


@pytest.mark.parametrize(
    ("source", "without_coverage", "outcome", "exit_code", "counts"),
    [
        ("import warnings\ndef test_case():\n    warnings.warn('example')\n", False, "passed", 0, (1, 0, 0)),
        ("import warnings\ndef test_case():\n    warnings.warn('example')\n    assert False\n",
         False, "test_failed", 1, (0, 1, 0)),
        ("def test_case(\n", False, "execution_failed", 2, None),
        ("def test_case():\n    assert True\n", True, "passed", 0, (1, 0, 0)),
        ("import pytest\ndef test_case():\n    pytest.skip('example')\n",
         False, "execution_failed", 0, (0, 0, 1)),
    ],
    ids=["passed", "assertion-failure", "collection-error", "coverage-missing", "all-skipped"],
)
def test_pytest_starts_one_session(tmp_path, monkeypatch, source, without_coverage, outcome, exit_code, counts):
    project = tmp_path / "project"
    project.mkdir()
    (project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (project / "test_case.py").write_text(source, encoding="utf-8")
    sessions = tmp_path / "sessions.txt"
    (project / "conftest.py").write_text(
        "from pathlib import Path\n"
        "def pytest_sessionstart(session):\n"
        f"    with Path({str(sessions)!r}).open('a', encoding='utf-8') as stream:\n"
        "        stream.write('started\\n')\n",
        encoding="utf-8",
    )

    if without_coverage:
        (project / "pytest.ini").write_text(
            "[pytest]\naddopts = --cov test_case --cov-fail-under 99 --cov-report=json\n",
            encoding="utf-8",
        )
        (project / "coverage.py").write_text("raise ModuleNotFoundError('coverage is unavailable')\n", encoding="utf-8")
        # A copied report must not supply stale coverage when the tool is unavailable.
        (project / "coverage.json").write_text('{"totals": {"percent_covered": 99.99}}', encoding="utf-8")
        original_planner = orchestrator.planning_node

        def plan_without_plugins(state):
            plan = original_planner(state)
            plan["environment_vars"]["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
            return plan

        monkeypatch.setattr(orchestrator, "planning_node", plan_without_plugins)

    result = run_workflow(str(project), db_path=":memory:")
    assert sessions.read_text(encoding="utf-8").splitlines() == ["started"]
    assert len(result["command_results"]) == 1
    assert result["metrics"]["outcome"] == outcome
    assert result["metrics"]["quality_gate"]["passed"] == (outcome == "passed")
    assert bool(result["metrics"]["pass_rate"] is None) == (outcome == "execution_failed")
    assert result["exit_code"] == exit_code
    assert result["test_counts"] == (None if counts is None else dict(zip(("passed", "failed", "skipped"), counts)))
    cases = result["test_case_results"]
    if counts is None:
        assert cases["status"] == "unavailable"
        assert cases["reason"] == "collection_failed"
        assert cases["cases"] == []
    else:
        assert cases["status"] == "complete"
        assert cases["selected_count"] == 1
        expected = next(label for label, count in zip(("passed", "failed", "skipped"), counts) if count)
        assert cases["cases"] == [{"node_id": "test_case.py::test_case", "outcome": expected}]
    if without_coverage:
        assert result["coverage_data"] == {"source": None}
        assert result["metrics"]["coverage_percent"] is None
        assert result["overall_score"] is None
        assert "pytest-cov is not loaded" in result["stderr"]
    elif outcome != "execution_failed":
        assert result["coverage_data"]["source"] == "coverage.json"
        assert result["metrics"]["coverage_percent"] > 0


def test_pytest_early_stop_keeps_only_finished_cases(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "pytest.ini").write_text("[pytest]\naddopts = -x\n", encoding="utf-8")
    (project / "test_sample.py").write_text(
        "def test_first():\n    assert False\n\ndef test_second():\n    pass\n",
        encoding="utf-8",
    )
    result = run_workflow(str(project), db_path=":memory:")
    assert result["test_case_results"] == {
        "format": "pytest-nodeid-v1", "status": "incomplete", "reason": "incomplete_execution",
        "selected_count": 2, "cases": [{"node_id": "test_sample.py::test_first", "outcome": "failed"}],
    }


def test_pytest_case_ids_stay_stable_with_an_ancestor_root(tmp_path, monkeypatch):
    sandbox_parent = tmp_path / "sandbox"
    sandbox_parent.mkdir()
    config = sandbox_parent / "pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    monkeypatch.setattr(tempfile, "tempdir", str(sandbox_parent))
    project = tmp_path / "project"
    project.mkdir()
    (project / "test_sample.py").write_text(
        """import unittest

class Sample(unittest.TestCase):
    def test_subtests(self):
        for n in (1, 2):
            with self.subTest(n=n):
                self.assertEqual(n, 1)

    def test_other(self):
        pass
""",
        encoding="utf-8",
    )
    plan = {"commands": [{"command": ["python", "-m", "pytest", "-q"]}]}
    full = execute_plan(plan, str(project), test_tool="pytest")["test_case_results"]
    assert full["status"] == "complete"
    assert full["cases"] == [
        {"node_id": "test_sample.py::Sample::test_other", "outcome": "passed"},
        {"node_id": "test_sample.py::Sample::test_subtests", "outcome": "failed"},
    ]
    config.write_text("[pytest]\naddopts = -k other\n", encoding="utf-8")
    filtered = execute_plan(plan, str(project), test_tool="pytest")["test_case_results"]
    assert filtered["status"] == "incomplete"
    assert filtered["reason"] == "tests_deselected"
    assert filtered["cases"] == full["cases"][:1]


def test_report_contract(sample_project: Path):
    classification = classify_project(str(sample_project))
    plan = build_plan(classification)
    execution = execute_plan(plan, str(sample_project))
    state = initial_state(str(sample_project), "eval-test-report")
    state.update({**classification, **plan, **execution})
    report = build_report(state)
    for key in ("summary", "detailed_report", "metrics", "recommendations", "timestamp"):
        assert key in report, f"missing report key: {key}"
    assert isinstance(report["summary"], str) and report["summary"]
    assert report["metrics"]["success"] is True
    assert 0 <= report["overall_score"] <= 100
    assert report["recommendations"]
    assert report["detailed_report"]["execution"]["exit_code"] == 0


@pytest.mark.parametrize(("config_name", "threshold", "exit_code"), [
    ("pytest.ini", 60, 0),
    ("pyproject.toml", 90, 1),
])
def test_pytest_cov_uses_current_scoped_coverage(tmp_path, config_name, threshold, exit_code):
    from coverage import Coverage

    project = tmp_path / "project"
    project.mkdir()
    # The old run took the other branch as well. --cov-append must not import it.
    source = project / "scoped.py"
    source.write_text("def choose(value):\n    if value:\n        return 1\n    return 0\n", encoding="utf-8")
    tracer = Coverage(data_file=str(project / ".coverage"), branch=True, config_file=False)
    with tracer.collect():
        from runpy import run_path

        choose = run_path(str(source))["choose"]
        choose(True)
        choose(False)
    tracer.save()
    # Even a preexisting JSON export must not be used as the current report.
    (project / "coverage.json").write_text('{"totals":{"percent_covered":99.99}}', encoding="utf-8")
    (project / "unscoped.py").write_text("value = 42\n", encoding="utf-8")
    (project / "test_case.py").write_text(
        "from scoped import choose\nfrom unscoped import value\n"
        "def test_case():\n    assert choose(True) == 1 and value == 42\n",
        encoding="utf-8",
    )
    sessions = tmp_path / "sessions.txt"
    (project / "conftest.py").write_text(
        "from pathlib import Path\n"
        "def pytest_sessionstart(session):\n"
        f"    with Path({str(sessions)!r}).open('a', encoding='utf-8') as stream:\n"
        "        stream.write('started\\n')\n",
        encoding="utf-8",
    )

    options = f"--cov scoped --cov-branch --cov-append --cov-report= --cov-fail-under {threshold}"
    config = "[pytest]\naddopts = " + options if config_name == "pytest.ini" else (
        f'[tool.pytest.ini_options]\naddopts = "{options}"'
    )
    (project / config_name).write_text(config + "\n", encoding="utf-8")

    with WorkflowEngine(":memory:") as engine:
        result = engine.run(str(project))
        report = engine.get_report(result["evaluation_id"])
    assert sessions.read_text(encoding="utf-8").splitlines() == ["started"]
    assert result["exit_code"] == exit_code
    assert result["test_counts"] == {"passed": 1, "failed": 0, "skipped": 0}
    assert ("Coverage failure:" in result["stdout"]) == bool(exit_code)
    coverage = result["coverage_data"]
    assert coverage["source"] == "coverage.json"
    assert coverage["files"] == 1
    assert coverage["branches_covered"] == 1
    assert coverage["branches_total"] == 2
    assert coverage["percent_covered"] == 66.67
    reporting = next(row["output_data"] for row in report["results"] if row["stage"] == "reporting")
    assert reporting["metrics"]["coverage_percent"] == 66.67


def test_failing_tests_are_reported(failing_project: Path):
    with WorkflowEngine(":memory:") as engine:
        final = engine.run(str(failing_project))
        report = engine.get_report(final["evaluation_id"])
    assert final["evaluation_status"] == "failed"
    assert final["test_counts"] == {"passed": 1, "failed": 2, "skipped": 0}
    assert final["metrics"]["pass_rate"] == final["analysis"]["pass_rate"] == 0.3333
    reporting = next(row["output_data"] for row in report["results"] if row["stage"] == "reporting")
    assert reporting["metrics"]["pass_rate"] == 0.3333
    assert any(r["priority"] == "high" for r in final["recommendations"])


def test_go_coverage_weights_statements(tmp_path: Path):
    (tmp_path / "coverage.out").write_text(
        "mode: set\nexample/math.go:3.20,5.2 1 1\nexample/math.go:6.20,8.2 3 0\n"
        "example/math.go:3.20,5.2 1 0\n",
        encoding="utf-8",
    )
    assert parse_coverage(tmp_path) == {
        "source": "coverage.out", "percent_covered": 25.0,
        "statements_covered": 1, "statements_total": 4,
    }


# ---- security ------------------------------------------------------------

def test_command_validation_blocks_dangerous_commands():
    with pytest.raises(CommandValidationError):
        validate_command(["rm", "-rf", "/"])
    with pytest.raises(CommandValidationError):
        validate_command(["python", "-c", "print('x; rm -rf /')"])
    with pytest.raises(CommandValidationError):
        validate_command(["python", "-m", "evil"])
    with pytest.raises(CommandValidationError):
        validate_command("python -m pytest")  # type: ignore[arg-type]
    assert validate_command(["python", "-m", "pytest", "-q"])[0] == "python"


def test_sandbox_excludes_unwanted_directories(tmp_path: Path):
    (tmp_path / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "huge.js").write_text("x" * 100_000, encoding="utf-8")
    with Sandbox(str(tmp_path)) as workspace:
        assert (workspace / "app.py").exists()
        assert not (workspace / "node_modules").exists()
    assert (tmp_path / "app.py").exists()  # source untouched


# ---- database ------------------------------------------------------------

def test_database_schema_and_crud(sample_project: Path):
    with Database(":memory:") as db:
        state = initial_state(str(sample_project), "eval-db-test")
        state.update({
            **classify_project(str(sample_project)),
            "test_strategy": "strategy",
            "exit_code": 0,
            "execution_time": 1.5,
            "overall_score": 88.5,
            "evaluation_status": "passed",
            "summary": "all good",
            "recommendations": [{
                "category": "maintenance", "priority": "low",
                "title": "Healthy", "message": "Keep it up",
            }],
            "errors": [],
        })
        db.save_workflow_state(state)
        assert db.count("evaluations") == 1
        assert db.count("results") >= 4
        assert db.count("recommendations") == 1
        report = db.get_full_report("eval-db-test")
        assert report["evaluation"]["status"] == "passed"
        assert report["results"][0]["agent_name"] == "classifier"
        assert report["recommendations"][0]["title"] == "Healthy"


@pytest.mark.parametrize("has_previous", [False, True])
def test_report_write_failure_leaves_no_partial_history(tmp_path, has_previous):
    db_path = tmp_path / "history.db"
    evaluation_id = "eval-atomic"
    with Database(db_path) as writer, Database(db_path) as reader:
        if has_previous:
            writer.save_workflow_state({
                "evaluation_id": evaluation_id,
                "evaluation_status": "passed",
                "recommendations": [{"title": "Old recommendation"}],
            })
        previous = reader.get_full_report(evaluation_id)
        with writer.transaction() as conn:
            conn.execute("""
                CREATE TEMP TRIGGER fail_planning_write BEFORE INSERT ON results
                WHEN NEW.stage = 'planning'
                BEGIN
                    SELECT RAISE(ABORT, 'planning write failed');
                END
            """)
        updated = {
            "evaluation_id": evaluation_id,
            "evaluation_status": "failed",
            "recommendations": [{"title": "New recommendation"}],
            "errors": [{"agent": "executor", "message": "New error"}],
        }
        with pytest.raises(sqlite3.IntegrityError, match="planning write failed"):
            writer.save_workflow_state(updated)
        assert reader.get_full_report(evaluation_id) == previous

        with writer.transaction() as conn:
            conn.execute("DROP TRIGGER fail_planning_write")
        writer.save_workflow_state(updated)
        replaced = reader.get_full_report(evaluation_id)
        assert replaced["evaluation"]["status"] == "failed"
        assert [row["stage"] for row in replaced["results"]] == [
            "classification", "planning", "execution", "analysis", "reporting",
        ]
        assert [row["error_message"] for row in replaced["errors"]] == ["New error"]
        assert [row["title"] for row in replaced["recommendations"]] == ["New recommendation"]


def test_history_read_uses_one_snapshot_during_replacement(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    evaluation_id = "eval-snapshot"
    with sqlite3.connect(db_path) as setup:
        assert setup.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    setup.close()
    with Database(db_path) as writer, Database(db_path) as reader:
        writer.save_workflow_state({
            "evaluation_id": evaluation_id,
            "evaluation_status": "passed",
            "summary": "Old summary",
        })
        previous = reader.get_full_report(evaluation_id)
        get_evaluation = reader.get_evaluation

        def replace_after_read(run_id):
            evaluation = get_evaluation(run_id)
            writer.save_workflow_state({
                "evaluation_id": run_id,
                "evaluation_status": "failed",
                "summary": "New summary",
            })
            return evaluation

        with monkeypatch.context() as context:
            context.setattr(reader, "get_evaluation", replace_after_read)
            assert reader.get_full_report(evaluation_id) == previous
        replaced = reader.get_full_report(evaluation_id)
        assert replaced["evaluation"]["status"] == "failed"
        assert replaced["results"][-1]["output_data"]["summary"] == "New summary"


# ---- full workflow -------------------------------------------------------

def test_full_workflow_end_to_end(sample_project: Path, tmp_path: Path):
    db_path = tmp_path / "history.db"
    with WorkflowEngine(str(db_path), max_retries=1, use_checkpointer=False) as engine:
        final = engine.run(str(sample_project), evaluation_id="eval-e2e")

    assert final["evaluation_id"] == "eval-e2e"
    assert final["persisted"] is True
    assert final["status"] in {"success", "completed"}
    assert final["evaluation_status"] == "passed"
    assert final["exit_code"] == 0
    assert final["overall_score"] > 0
    assert final["summary"]
    assert len(final["recommendations"]) >= 1
    assert final["errors"] == []

    with Database(str(db_path)) as db:
        stored = db.get_full_report("eval-e2e")
        assert stored is not None
        assert stored["evaluation"]["project_type"] == "python"
        assert len(stored["results"]) == 5


def test_workflow_retry_then_success(monkeypatch, sample_project: Path, tmp_path: Path):
    """A transient agent failure is retried and the run still completes."""
    real_planning = orchestrator.planning_node
    calls = {"count": 0}

    def flaky_planning(state):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("transient planner failure")
        return real_planning(state)

    monkeypatch.setattr(orchestrator, "planning_node", flaky_planning)
    with WorkflowEngine(str(tmp_path / "retry.db"), max_retries=2,
                        use_checkpointer=False) as engine:
        final = engine.run(str(sample_project), evaluation_id="eval-retry")

    assert calls["count"] == 2
    assert final["retry_count"] == 1
    assert final["persisted"] is True
    assert final["evaluation_status"] == "passed"
    assert any(e["agent"] == "planner" for e in final["errors"])


def test_workflow_error_boundary_still_produces_report(monkeypatch, sample_project: Path,
                                                       tmp_path: Path):
    """When an agent keeps failing, the report + persistence nodes still run."""
    def broken_execution(state):  # noqa: ARG001
        raise RuntimeError("sandbox unavailable")

    monkeypatch.setattr(orchestrator, "execution_node", broken_execution)
    with WorkflowEngine(str(tmp_path / "error.db"), max_retries=0,
                        use_checkpointer=False) as engine:
        final = engine.run(str(sample_project), evaluation_id="eval-error")

    assert final["retry_count"] == 0
    assert final["evaluation_status"] == "failed"
    assert final["summary"]
    assert any(e["agent"] == "executor" for e in final["errors"])
    assert final["persisted"] is True

    with Database(str(tmp_path / "error.db")) as db:
        assert db.get_full_report("eval-error") is not None
        assert len(db.get_errors("eval-error")) >= 1


def test_convenience_api_and_stream(sample_project: Path, tmp_path: Path):
    final = run_workflow(
        str(sample_project),
        db_path=str(tmp_path / "conv.db"),
        evaluation_id="eval-convenience",
    )
    assert final["persisted"] is True

    engine = WorkflowEngine(str(tmp_path / "stream.db"), use_checkpointer=False)
    try:
        snapshots = list(engine.stream(str(sample_project), evaluation_id="eval-stream"))
        assert len(snapshots) >= 5
    finally:
        engine.close()
