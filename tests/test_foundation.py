"""Foundation contracts: messages, state, callbacks, failure paths and API."""

import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.callbacks import BaseCallbackHandler
from pydantic import ValidationError

from config.settings import Settings
from src.api.fastapi_server import create_app
from src.core import langgraph_orchestrator as orchestrator
from src.core.langgraph_orchestrator import AGENT_NAMES, WorkflowEngine
from src.core.message_schema import AgentMessage
from src.core.state_manager import StateManager


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8",
    )
    (root / "test_demo.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    return root


@pytest.mark.parametrize(
    ("coverage", "counts", "threshold", "reason"),
    [
        (None, {"passed": 1, "failed": 0, "skipped": 0}, 80, "coverage_unavailable"),
        (79.5, {"passed": 1, "failed": 0, "skipped": 0}, 80, "coverage_below_minimum"),
        (80, {"passed": 1, "failed": 0, "skipped": 0}, 80, "passed"),
        (100, None, None, "execution_failed"),
    ],
)
def test_quality_gate_is_returned_and_saved(monkeypatch, tmp_path, project, coverage, counts, threshold, reason):
    monkeypatch.setattr(orchestrator, "execution_node", lambda state: {
        "stdout": "1 passed" if counts else "Unrecognized runner output",
        "exit_code": 0,
        "test_counts": counts,
        "coverage_data": {"percent_covered": coverage},
        "command_results": [{"phase": "test", "exit_code": 0}],
    })
    settings = Settings(_env_file=None, database_path=str(tmp_path / "gate.db"), use_deepseek=False)
    with TestClient(create_app(settings)) as client:
        response = client.post("/evaluations", json={
            "project_path": str(project),
            "min_coverage": threshold,
        })
        assert response.status_code == 200
        result = response.json()
        assert result["persisted"] is True
        assert result["errors"] == []
        assert result["metrics"]["outcome"] == ("execution_failed" if counts is None else "passed")
        gate = result["metrics"]["quality_gate"]
        assert gate == {
            "policy_version": 1, "passed": reason == "passed", "reason": reason, "min_coverage": threshold,
        }
        assert client.get("/evaluations").json()[0]["gate_passed"] is gate["passed"]
        saved = client.get(f"/evaluations/{result['evaluation_id']}").json()
        reporting = next(row["output_data"] for row in saved["results"] if row["stage"] == "reporting")
        assert reporting["metrics"]["quality_gate"] == gate


class Events(BaseCallbackHandler):
    def __init__(self):
        self.events = []

    def on_custom_event(self, name, data, **kwargs):
        self.events.append((name, data["agent"]))


def test_message_validation_and_json_round_trip():
    fields = dict(
        evaluation_id="eval-contract", sender="classifier", receiver="planner",
        kind="result", payload={"project_type": "python"},
    )
    message = AgentMessage(**fields)
    assert AgentMessage.model_validate_json(message.model_dump_json()) == message
    for invalid in (
        {"sender": "unknown"}, {"kind": "invalid"}, {"unexpected": True},
        {"payload": {"not_json": object()}}, {"evaluation_id": ""},
        {"timestamp": "2026-01-01T00:00:00"},
    ):
        with pytest.raises(ValidationError):
            AgentMessage(**{**fields, **invalid})


def test_state_validation_and_isolation(project):
    first = StateManager.create(str(project))
    second = StateManager.create(str(project))
    assert first["evaluation_id"] != second["evaluation_id"]
    first["errors"].append({"message": "first only"})
    assert second["errors"] == []
    with pytest.raises(ValidationError):
        StateManager.create(str(project / "missing"))
    for retries in (-1, 21):
        with pytest.raises(ValidationError):
            StateManager.create(str(project), max_retries=retries)
        with pytest.raises(ValidationError):
            WorkflowEngine(":memory:", max_retries=retries)


def test_graph_order_messages_and_callbacks(project):
    events = Events()
    with WorkflowEngine(":memory:") as engine:
        graph = engine.graph.get_graph()
        assert set(AGENT_NAMES) <= set(graph.nodes)
        updates = list(engine.stream(str(project), config={"callbacks": [events]}))
        assert [next(iter(update)) for update in updates] == [*AGENT_NAMES, "persistence"]
        messages = [message for update in updates for value in update.values() for message in value["messages"]]
        assert [AgentMessage.model_validate(m).sender for m in messages] == [*AGENT_NAMES, "persistence"]
        assert ("agent_start", "analyst") in events.events
        assert ("agent_end", "reporter") in events.events
        json.dumps(messages)


def test_retry_emits_error_callback_and_preserves_analysis(monkeypatch, project):
    real_node = orchestrator.planning_node
    attempts = []

    def flaky(state):
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("transient planning failure")
        return real_node(state)

    monkeypatch.setattr(orchestrator, "planning_node", flaky)
    events = Events()
    with WorkflowEngine(":memory:", max_retries=1) as engine:
        final = engine.run(str(project), config={"callbacks": [events]})
        stored = engine.get_report(final["evaluation_id"])
    assert final["retry_count"] == 1
    assert final["evaluation_status"] == "passed"
    assert final["analysis"]["issues"][0]["agent"] == "planner"
    assert ("agent_error", "planner") in events.events
    assert [m["kind"] for m in final["messages"]].count("error") == 1
    assert next(r for r in stored["results"] if r["agent_name"] == "analyst")["output_data"] == final["analysis"]


@pytest.mark.parametrize("node", ["analysis_node", "report_node", "create_persistence_node"])
def test_terminal_failures_exit_with_bounded_retries(monkeypatch, project, node):
    def broken(state):
        raise RuntimeError("persistent failure")

    replacement = (lambda db: broken) if node == "create_persistence_node" else broken
    monkeypatch.setattr(orchestrator, node, replacement)
    with WorkflowEngine(":memory:", max_retries=1) as engine:
        final = engine.run(str(project))
    assert final["retry_count"] == 1
    assert len(final["errors"]) == 2
    if node == "create_persistence_node":
        assert final["status"] == "error"
    else:
        assert final["persisted"] is True


def test_reused_evaluation_id_does_not_reuse_checkpoint_state(monkeypatch, project):
    # Keep this isolation check focused on state rather than subprocess execution.
    monkeypatch.setattr(orchestrator, "execution_node", lambda state: {
        "exit_code": 0, "command_results": [{"exit_code": 0}],
    })
    with WorkflowEngine(":memory:") as engine:
        first = engine.run(str(project), evaluation_id="same-id")
        second = engine.run(str(project), evaluation_id="same-id")
        assert len(first["messages"]) == len(second["messages"]) == 6
        assert len(engine.get_report("same-id")["results"]) == 5


def test_api_evaluation_and_report(project, tmp_path):
    settings = Settings(_env_file=None, database_path=str(tmp_path / "api.db"), max_retries=0)
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert "analyst" in client.get("/workflow").json()["mermaid"]
        assert client.get("/evaluations/missing").status_code == 404
        assert client.post("/evaluations", json={"project_path": str(project / "missing")}).status_code == 422
        assert client.post("/evaluations", json={"project_path": str(project), "max_retries": -1}).status_code == 422
        response = client.post("/evaluations", json={"project_path": str(project)})
        assert response.status_code == 200
        final = response.json()
        assert final["max_retries"] == 0
        assert final["evaluation_status"] == "passed"
        assert final["persisted"] is True
        assert final["detailed_report"]["analysis"] == final["analysis"]
        report = client.get(f"/evaluations/{final['evaluation_id']}").json()
        assert len(report["results"]) == 5


def test_pytest_node_ids_and_phase_outcomes_are_saved(project, tmp_path):
    (project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (project / "test_demo.py").write_text(
        """
import pytest

@pytest.mark.parametrize("number", [1, 2, 3], ids=["pass", "fail::detail", "skip"])
def test_case(number):
    if number == 3:
        pytest.skip("example")
    assert number == 1

@pytest.fixture
def broken_setup():
    raise RuntimeError("setup failed")

def test_setup(broken_setup):
    pass

@pytest.fixture
def broken_teardown():
    yield
    raise RuntimeError("teardown failed")

def test_teardown(broken_teardown):
    pass

@pytest.mark.xfail
def test_expected_failure():
    assert False
""",
        encoding="utf-8",
    )
    settings = Settings(_env_file=None, database_path=str(tmp_path / "cases.db"), use_deepseek=False)
    with TestClient(create_app(settings)) as client:
        response = client.post("/evaluations", json={"project_path": str(project)})
        assert response.status_code == 200
        result = response.json()
        assert result["persisted"]
        case_results = result["test_case_results"]
        assert case_results == {
            "format": "pytest-nodeid-v1", "status": "complete", "reason": None, "selected_count": 6,
            "cases": [
                {"node_id": "test_demo.py::test_case[fail::detail]", "outcome": "failed"},
                {"node_id": "test_demo.py::test_case[pass]", "outcome": "passed"},
                {"node_id": "test_demo.py::test_case[skip]", "outcome": "skipped"},
                {"node_id": "test_demo.py::test_expected_failure", "outcome": "skipped"},
                {"node_id": "test_demo.py::test_setup", "outcome": "failed"},
                {"node_id": "test_demo.py::test_teardown", "outcome": "failed"},
            ],
        }
        saved = client.get(f"/evaluations/{result['evaluation_id']}")
        assert saved.status_code == 200
        stages = {row["stage"]: row["output_data"] for row in saved.json()["results"]}
        assert stages["execution"]["test_case_results"] == case_results
        assert stages["reporting"]["detailed_report"]["execution"]["test_case_results"] == case_results


def test_saved_execution_provenance_tracks_only_copied_inputs(project, tmp_path):
    excluded = project / "external_data"
    excluded.mkdir()
    excluded_input = excluded / "data.txt"
    excluded_input.write_text("first", encoding="utf-8")
    settings = Settings(_env_file=None, database_path=str(tmp_path / "trace.db"), use_deepseek=False)
    payload = {"project_path": str(project), "exclude_dirs": ["external_data"], "min_coverage": 80}
    with TestClient(create_app(settings)) as client:
        first = client.post("/evaluations", json=payload)
        assert first.status_code == 200
        first = first.json()
        excluded_input.write_text("changed outside the copy", encoding="utf-8")
        same_inputs = client.post("/evaluations", json=payload)
        assert same_inputs.status_code == 200
        (project / "test_demo.py").write_text("def test_ok():\n    assert 1 + 1 == 3\n", encoding="utf-8")
        changed_inputs = client.post("/evaluations", json=payload)
        assert changed_inputs.status_code == 200
        changed = changed_inputs.json()

        original = first["execution_provenance"]
        assert original["git"] is None
        assert original["snapshot"]["files"] == 2
        assert len(original["snapshot"]["sha256"]) == 64
        assert original["runtime"]["python"]["installed_packages"]["pytest"]
        assert same_inputs.json()["execution_provenance"]["snapshot"] == original["snapshot"]
        assert changed["execution_provenance"]["snapshot"]["sha256"] != original["snapshot"]["sha256"]
        assert first["metrics"]["outcome"] == "passed"
        assert changed["metrics"]["outcome"] == "test_failed"

        for current in (first, changed):
            historical = client.get(f"/evaluations/{current['evaluation_id']}")
            assert historical.status_code == 200
            stages = {row["stage"]: row["output_data"] for row in historical.json()["results"]}
            execution = stages["reporting"]["detailed_report"]["execution"]
            assert execution["provenance"] == stages["execution"]["provenance"] == current["execution_provenance"]
            assert execution["commands"][0]["command"] == current["command_results"][0]["command"]
            assert execution["commands"][0]["timeout_seconds"] == 180
            assert stages["reporting"]["metrics"]["quality_gate"] == current["metrics"]["quality_gate"]


def test_recent_evaluations_are_ordered_and_open_matching_reports(project, tmp_path):
    settings = Settings(_env_file=None, database_path=str(tmp_path / "recent.db"), max_retries=0)
    with TestClient(create_app(settings)) as client:
        assert client.get("/evaluations?limit=0").status_code == 422
        for evaluation_id in ("older", "newer"):
            response = client.post("/evaluations", json={
                "project_path": str(project), "evaluation_id": evaluation_id,
                "exclude_dirs": ["data/raw", ".workbuddy"],
            })
            assert response.status_code == 200
        rows = client.get("/evaluations?limit=2").json()
        assert [row["evaluation_id"] for row in rows] == ["newer", "older"]
        assert rows[0]["project_name"] == project.name
        assert rows[0]["status"] == "passed"
        assert rows[0]["pass_rate"] is not None
        for row in rows:
            report = client.get(f"/evaluations/{row['evaluation_id']}").json()
            assert report["evaluation"]["id"] == row["evaluation_id"]
        assert client.get("/evaluations/missing").status_code == 404


def test_project_runner_failures_are_not_reported_as_failing_tests(monkeypatch, project):
    monkeypatch.setattr(orchestrator, "execution_node", lambda state: {
        "exit_code": 2, "stdout": "ERROR: file or directory not found: tests",
        "stderr": "", "test_counts": None,
        "command_results": [{
            "phase": "test", "optional": False, "exit_code": 2,
            "failure_kind": "execution_failed", "stdout": "ERROR: file or directory not found: tests",
        }],
    })
    with WorkflowEngine(":memory:", max_retries=0) as engine:
        execution_failure = engine.run(
            str(project), evaluation_id="execution-failure",
        )
    assert execution_failure["status"] == "completed"
    assert execution_failure["metrics"]["outcome"] == "execution_failed"
    assert execution_failure["metrics"]["pass_rate"] is None
    assert "could not complete" in execution_failure["summary"].lower()
    assert not any(issue["title"] == "AssertionError" for issue in execution_failure["analysis"]["issues"])


def test_failed_optional_coverage_commands_do_not_count_as_failed_tests():
    from src.agents.report_agent import compute_metrics

    metrics = compute_metrics({
        "exit_code": 0,
        "command_results": [
            {"phase": "test", "exit_code": 0},
            {"phase": "coverage", "optional": True, "exit_code": 1},
        ],
        "test_counts": {"passed": 4, "failed": 0, "skipped": 0},
    })
    assert metrics["outcome"] == "passed"
    assert metrics["commands_failed"] == 0
    assert metrics["pass_rate"] == 1.0


def test_settings_read_environment(monkeypatch):
    monkeypatch.setenv("TESTMIRROR_MAX_RETRIES", "3")
    assert Settings(_env_file=None).max_retries == 3
    monkeypatch.setenv("TESTMIRROR_MAX_RETRIES", "-1")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
