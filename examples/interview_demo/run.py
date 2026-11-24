"""Compare analysis modes with one real pytest result replayed through LangGraph."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.core import langgraph_orchestrator as workflow
from src.core.deepseek_client import DEFAULT_BASE_URL, DeepSeekClient
from src.utils.test_results import parse_test_counts


PROJECT = Path(__file__).with_name("project")
PYTEST_ARGS = ["-m", "pytest", "-q", "--color=no", "-p", "no:cacheprovider"]
FAILURE_COUNTS = {"passed": 2, "failed": 1, "skipped": 0}
CASE_ID = "test_shipping.py::test_free_shipping_threshold[5000-0]"
LOCATION = "test_shipping.py:11"


def run_pytest(project):
    env = {
        **os.environ, "PYTEST_ADDOPTS": "", "PYTEST_PLUGINS": "",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONUTF8": "1",
    }
    return subprocess.run(
        [sys.executable, *PYTEST_ARGS], cwd=project, env=env, timeout=20,
        capture_output=True, encoding="utf-8", check=False,
    )


def compare_run(database, project, name, execution, client=None):
    # Keep classification deterministic so only the analyst varies.
    with (
        patch.object(workflow, "execution_node", lambda state: deepcopy(execution)),
        patch.object(workflow, "deepseek_classification_node",
                     lambda state, client: workflow.classification_node(state)),
        workflow.WorkflowEngine(str(database), deepseek_client=client) as engine,
    ):
        final = engine.run(str(project), evaluation_id=name)
        report = engine.get_report(name)

    assert final["status"] == "completed" and final["persisted"]
    assert report["evaluation"]["status"] == "failed"
    stages = {row["stage"]: row["output_data"] for row in report["results"]}
    assert stages["execution"]["stdout"] == execution["stdout"]
    assert stages["execution"]["command_results"] == execution["command_results"]
    assert stages["analysis"] == final["analysis"]
    assert stages["reporting"]["metrics"]["outcome"] == "test_failed"
    node_order = [message["sender"] for message in final["messages"]]
    assert node_order == list(workflow.NODE_NAMES)
    return {
        "evaluation_id": name,
        "pytest_sha256": hashlib.sha256(execution["stdout"].encode()).hexdigest(),
        "node_order": node_order,
        "metrics": final["metrics"],
        "analysis": final["analysis"],
        "analysis_usage": final.get("llm_usage", {}).get("analysis"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Send synthetic pytest output to DeepSeek.")
    parser.add_argument("--model", help="DeepSeek model name, defaults to client configuration.")
    parser.add_argument("--out", type=Path, required=True, help="New directory for evaluation evidence.")
    args = parser.parse_args()
    for name in ("LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"):
        os.environ[name] = "false"
    out = args.out.resolve()
    out.mkdir(parents=True)
    database = out / "interview.db"

    with TemporaryDirectory(dir=out) as tmp:
        project = Path(tmp) / "shipping_demo"
        shutil.copytree(PROJECT, project, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        failed = run_pytest(project)
        assert failed.returncode == 1 and not failed.stderr, (failed.stdout, failed.stderr)
        failure_counts = parse_test_counts(failed.stdout, tool="pytest")
        assert failure_counts == FAILURE_COUNTS
        assert CASE_ID in failed.stdout and LOCATION in failed.stdout
        assert str(project) not in failed.stdout
        execution = {
            "stdout": failed.stdout, "stderr": "", "exit_code": failed.returncode,
            "test_counts": failure_counts,
            "command_results": [
                {"command": ["python", *PYTEST_ARGS], "phase": "test",
                 "exit_code": failed.returncode, "stdout": failed.stdout, "stderr": ""},
            ],
        }
        (out / "pytest-failed.txt").write_text(failed.stdout, encoding="utf-8", newline="\n")
        offline = compare_run(database, project, "offline", execution)
        evidence = offline["analysis"]["test_evidence"]
        assert any(issue["location"] == LOCATION for issue in evidence)

        timeout_client = DeepSeekClient(api_key="demo-timeout", max_retries=2, backoff_base=0)
        with patch.object(timeout_client, "_open", side_effect=TimeoutError("demo transport timeout")) as transport:
            timeout = compare_run(database, project, "timeout", execution, timeout_client)
        assert transport.call_count == 2
        assert timeout["metrics"] == offline["metrics"]
        for field in ("test_evidence", "root_cause", "severity", "summary", "pass_rate"):
            assert timeout["analysis"][field] == offline["analysis"][field]
        assert "model_diagnosis" not in timeout["analysis"]
        timeout["simulated_transport_attempts"] = transport.call_count
        runs = [offline, timeout]

        if args.live:
            client = DeepSeekClient(base_url=DEFAULT_BASE_URL, model=args.model, timeout=30, max_retries=1)
            live = compare_run(database, project, "deepseek", execution, client)
            assert live["metrics"] == offline["metrics"]
            assert live["analysis"]["test_evidence"] == evidence
            live["model_output_applied"] = (
                live["analysis_usage"]["calls"] > 0
                and "model_diagnosis" in live["analysis"]
            )
            runs.append(live)

        source = project / "shipping.py"
        original = source.read_text(encoding="utf-8")
        fixed = original.replace("subtotal_cents > 5_000", "subtotal_cents >= 5_000")
        assert fixed != original
        source.write_text(fixed, encoding="utf-8")
        repaired = run_pytest(project)
        assert repaired.returncode == 0 and not repaired.stderr, (repaired.stdout, repaired.stderr)
        repaired_counts = parse_test_counts(repaired.stdout, tool="pytest")
        assert repaired_counts == {"passed": 3, "failed": 0, "skipped": 0}
        (out / "pytest-fixed.txt").write_text(repaired.stdout, encoding="utf-8")

    evidence = {"runs": runs, "fix": "shipping.py:3, > to >=", "fixed_counts": repaired_counts}
    (out / "comparison.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    for run in runs:
        print(f"{run['evaluation_id']}: {run['analysis']['root_cause']}")
        print(f"  pytest SHA-256: {run['pytest_sha256']}")
    print(f"Fixed: {repaired_counts}. Evidence: {out}")
    if args.live and not live["model_output_applied"]:
        print("DeepSeek analysis was unavailable or used the deterministic fallback.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
