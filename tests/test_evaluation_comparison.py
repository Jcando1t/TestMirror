"""Regression comparison of saved reports, including incomplete pytest runs."""

from fastapi.testclient import TestClient

from config.settings import Settings
from src.api.fastapi_server import create_app
from src.models.database import Database


def test_comparison_finds_regression_even_when_total_pass_rate_is_unchanged(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (project / "test_example.py").write_text(
        """from example import add_label

def test_independent_calls():
    assert add_label("alpha") == ["alpha"]
    assert add_label("beta") == ["beta"]

def test_shared_list_when_supplied():
    labels = ["first"]
    assert add_label("second", labels) == ["first", "second"]
""", encoding="utf-8",
    )
    implementation = project / "example.py"
    settings = Settings(_env_file=None, database_path=str(tmp_path / "history.db"), use_deepseek=False)
    with TestClient(create_app(settings)) as client:
        for name, code, threshold in (
            ("original", "def add_label(label, labels=[]):\n    labels.append(label)\n    return labels\n", 80),
            ("wrong-fix", "def add_label(label, labels=None):\n    return [label]\n", 80),
            ("correct-fix", "def add_label(label, labels=None):\n    if labels is None:\n"
             "        labels = []\n    labels.append(label)\n    return labels\n", 85),
        ):
            implementation.write_text(code, encoding="utf-8")
            response = client.post("/evaluations", json={
                "project_path": str(project), "evaluation_id": name, "min_coverage": threshold,
            })
            assert response.status_code == 200
            assert response.json()["persisted"]

        response = client.get("/evaluation-comparisons", params={
            "baseline_id": "original", "current_id": "wrong-fix",
        })
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "complete"
        assert result["baseline"]["pass_rate"] == result["current"]["pass_rate"] == 0.5
        assert result["baseline"]["snapshot"]["sha256"] != result["current"]["snapshot"]["sha256"]
        assert result["baseline"]["runtime"] == result["current"]["runtime"]
        assert result["warnings"] == []
        assert result["matched_count"] == 2
        assert result["counts"] == {"new_failures": 1, "fixed": 1, "added": 0, "removed": 0, "skip_changes": 0}
        assert result["changes"]["new_failures"] == [{
            "node_id": "test_example.py::test_shared_list_when_supplied", "before": "passed", "after": "failed",
        }]
        assert result["changes"]["fixed"] == [{
            "node_id": "test_example.py::test_independent_calls", "before": "failed", "after": "passed",
        }]

        fixed = client.get("/evaluation-comparisons", params={
            "baseline_id": "wrong-fix", "current_id": "correct-fix",
        }).json()
        assert fixed["current"]["test_counts"] == {"passed": 2, "failed": 0, "skipped": 0}
        assert fixed["counts"] == {"new_failures": 0, "fixed": 1, "added": 0, "removed": 0, "skip_changes": 0}
        assert fixed["warnings"] == ["quality_gate_policy_changed"]
        assert fixed["current"]["quality_gate"]["min_coverage"] == 85


def test_comparison_limits_differences_to_available_outcomes(tmp_path):
    db_path = tmp_path / "history.db"
    baseline_cases = [
        {"node_id": "test_demo.py::test_skip[x]", "outcome": "skipped"},
        {"node_id": "test_demo.py::test_removed", "outcome": "passed"},
    ]
    current_cases = [
        {"node_id": "test_demo.py::test_skip[x]", "outcome": "failed"},
        {"node_id": "test_demo.py::test_added", "outcome": "skipped"},
    ]
    with Database(db_path) as db:
        for name, status, rows, reason in (
            ("baseline", "complete", baseline_cases, None),
            ("current", "complete", current_cases, None),
            ("partial", "incomplete", current_cases, "incomplete_execution"),
            ("collection", "unavailable", [], "collection_failed"),
            ("invalid", "complete", [*current_cases, current_cases[0]], None),
        ):
            db.save_workflow_state({
                "evaluation_id": name, "test_tool": "pytest",
                "execution_provenance": {"runtime": {"os": "Linux" if name == "current" else "Windows"}},
                "detailed_report": {"plan": {"exclude_dirs": ["docs"] if name == "current" else []}},
                "test_case_results": {
                    "format": "pytest-nodeid-v1", "status": status,
                    "reason": reason, "selected_count": len(rows), "cases": rows,
                },
            })
        db.save_workflow_state({"evaluation_id": "old", "test_tool": "pytest"})
    settings = Settings(_env_file=None, database_path=str(db_path))
    with TestClient(create_app(settings)) as client:
        params = {"baseline_id": "baseline", "current_id": "current"}
        full = client.get("/evaluation-comparisons", params=params).json()
        assert full["status"] == "complete"
        assert full["warnings"] == ["runtime_changed", "exclude_dirs_changed"]
        assert full["changes"]["added"] == [
            {"node_id": "test_demo.py::test_added", "before": None, "after": "skipped"},
        ]
        assert full["changes"]["removed"] == [
            {"node_id": "test_demo.py::test_removed", "before": "passed", "after": None},
        ]
        assert full["changes"]["skip_changes"] == full["changes"]["new_failures"] == [
            {"node_id": "test_demo.py::test_skip[x]", "before": "skipped", "after": "failed"},
        ]
        reverse = client.get("/evaluation-comparisons", params={
            "baseline_id": "current", "current_id": "baseline",
        }).json()
        assert reverse["changes"]["fixed"] == []
        assert reverse["changes"]["skip_changes"] == [
            {"node_id": "test_demo.py::test_skip[x]", "before": "failed", "after": "skipped"},
        ]

        def compare_with(current_id):
            response = client.get("/evaluation-comparisons", params={**params, "current_id": current_id})
            assert response.status_code == 200
            return response.json()

        limited = compare_with("partial")
        assert limited["status"] == "partial"
        assert limited["matched_count"] == 1
        assert limited["current"]["case_reason"] == "incomplete_execution"
        assert limited["changes"]["new_failures"] == full["changes"]["new_failures"]
        assert limited["changes"]["added"] == limited["changes"]["removed"] == []
        for name, reason in (
            ("old", "case_data_missing"), ("collection", "collection_failed"), ("invalid", "case_data_invalid"),
        ):
            result = compare_with(name)
            assert result["status"] == "unavailable"
            assert result["current"]["case_reason"] == reason
            assert not any(result["counts"].values())
        assert client.get("/evaluations/old").status_code == 200
        assert client.get("/evaluation-comparisons", params={**params, "current_id": "missing"}).status_code == 404
        assert client.get("/evaluation-comparisons", params={**params, "baseline_id": "missing"}).status_code == 404
        assert client.get("/evaluation-comparisons", params={"baseline_id": "baseline"}).status_code == 422
