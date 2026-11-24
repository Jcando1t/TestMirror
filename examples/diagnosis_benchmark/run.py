"""Compare offline and live diagnosis on bounded, synthetic pytest failures."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent

from examples.interview_demo.run import PYTEST_ARGS, run_pytest
from src.agents.analysis_agent import AnalysisAgent, _SYSTEM_PROMPT, deterministic_analysis_node
from src.core.deepseek_client import DEFAULT_BASE_URL, DeepSeekClient
from src.utils.test_results import parse_test_counts


@dataclass(frozen=True)
class Case:
    name: str
    broken: str
    repaired: str
    test: str
    verified_change: str


CASES = (
    Case(
        "inclusive_boundary",
        "def fee(cents):\n    return 0 if cents > 5000 else 500\n",
        "def fee(cents):\n    return 0 if cents >= 5000 else 500\n",
        dedent("""\
            import pytest
            from example import fee

            @pytest.mark.parametrize("cents, expected", [(4999, 500), (5000, 0), (5001, 0)])
            def test_free_shipping(cents, expected):
                assert fee(cents) == expected, "Free shipping starts at 5000 cents inclusive"
            """),
        "Use >= 5000 in fee; the equality boundary must return zero fees.",
    ),
    Case(
        "state_leak",
        "def add_label(label, labels=[]):\n    labels.append(label)\n    return labels\n",
        "def add_label(label, labels=None):\n    if labels is None:\n        labels = []\n    labels.append(label)\n    return labels\n",
        dedent("""\
            from example import add_label

            def test_independent_calls():
                assert add_label("alpha") == ["alpha"]
                assert add_label("beta") == ["beta"]

            def test_shared_list_when_supplied():
                labels = ["first"]
                assert add_label("second", labels) == ["first", "second"]
            """),
        "Use None as the default and allocate per call; preserve a caller-provided list.",
    ),
    Case(
        "decimal_rounding",
        "def display_price(amount):\n    return f'{round(float(amount), 2):.2f}'\n",
        "from decimal import Decimal, ROUND_HALF_UP\n\ndef display_price(amount):\n"
        "    return str(Decimal(amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))\n",
        dedent("""\
            import pytest
            from example import display_price

            @pytest.mark.parametrize("amount, expected", [("2.675", "2.68"), ("12.10", "12.10")])
            def test_price_uses_decimal_half_up(amount, expected):
                assert display_price(amount) == expected
            """),
        "Use Decimal on the original string and quantize to cents with ROUND_HALF_UP.",
    ),
    Case(
        "config_lookup",
        "def read_limit(settings=None):\n    return (settings or {}).get('max-items', 25)\n",
        "def read_limit(settings=None):\n    return (settings or {}).get('max_items', 25)\n",
        dedent("""\
            from example import read_limit

            def test_custom_maximum():
                assert read_limit({"max_items": 50}) == 50

            def test_default_maximum():
                assert read_limit() == 25
            """),
        "Read the max_items key rather than max-items while preserving the default of 25.",
    ),
)


def check_case(case: Case, workspace: Path, output: Path, client: DeepSeekClient | None) -> dict:
    project = workspace / case.name
    project.mkdir()
    implementation = project / "example.py"
    implementation.write_text(case.broken, encoding="utf-8")
    (project / "test_example.py").write_text(case.test, encoding="utf-8")
    failing = run_pytest(project)
    counts = parse_test_counts(failing.stdout, tool="pytest")
    assert failing.returncode == 1 and not failing.stderr and counts and counts["failed"] == 1, failing
    assert str(project) not in failing.stdout and project.as_posix() not in failing.stdout
    (output / f"{case.name}-pytest.txt").write_text(failing.stdout, encoding="utf-8", newline="\n")
    execution = {
        "stdout": failing.stdout, "stderr": "", "exit_code": failing.returncode, "test_counts": counts,
        "command_results": [{
            "command": ["python", *PYTEST_ARGS], "phase": "test",
            "exit_code": failing.returncode, "stdout": failing.stdout, "stderr": "",
        }],
    }
    offline = deterministic_analysis_node(execution)["analysis"]
    assert offline["test_evidence"] and offline["metrics"]["outcome"] == "test_failed"
    result = {
        "name": case.name,
        "pytest_sha256": hashlib.sha256(failing.stdout.encode("utf-8")).hexdigest(),
        "test_counts": counts,
        "offline": offline,
    }

    if client is not None:
        client.reset_usage()
        start = time.perf_counter()
        analysis = AnalysisAgent(client=client).analyze_state(execution)
        elapsed = round(time.perf_counter() - start, 3)
        assert analysis["test_evidence"] == offline["test_evidence"]
        assert analysis["metrics"] == offline["metrics"]
        usage = client.usage_report()
        result["deepseek"] = {
            "prompt_sha256": hashlib.sha256(_SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
            "analysis": analysis,
            "usage": usage,
            "elapsed_seconds": elapsed,
            "diagnosis_received": usage["calls"] > 0 and "model_diagnosis" in analysis,
        }

    implementation.write_text(case.repaired, encoding="utf-8")
    repaired = run_pytest(project)
    repaired_counts = parse_test_counts(repaired.stdout, tool="pytest")
    assert repaired.returncode == 0 and not repaired.stderr, repaired
    assert repaired_counts == {"passed": counts["passed"] + 1, "failed": 0, "skipped": 0}
    result["verified_change"] = case.verified_change
    result["repaired_counts"] = repaired_counts
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="New directory for test evidence and analysis")
    parser.add_argument("--live", action="store_true", help="Send synthetic failure logs to DeepSeek")
    parser.add_argument("--model", help="DeepSeek model name")
    args = parser.parse_args()
    for variable in ("LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING", "LANGCHAIN_TRACING_V2"):
        os.environ[variable] = "false"
    output = args.out.resolve()
    output.mkdir(parents=True)
    client = DeepSeekClient(base_url=DEFAULT_BASE_URL, model=args.model, timeout=30, max_retries=1) if args.live else None
    with TemporaryDirectory(dir=output) as workspace:
        results = [check_case(case, Path(workspace), output, client) for case in CASES]
    (output / "comparison.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    for result in results:
        print(f"{result['name']}: {result['test_counts']} → {result['repaired_counts']}")
    print(f"Evidence: {output}")
    return int(args.live and any(not result["deepseek"]["diagnosis_received"] for result in results))


if __name__ == "__main__":
    sys.exit(main())
