"""Unit tests for the LLM-backed agents and the DeepSeek client.

All tests are hermetic: network access is replaced with fake transports, so
no API key or live endpoint is required.
"""

from __future__ import annotations

import json
import time
import urllib.error
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from config.settings import Settings
from src.agents.analysis_agent import AnalysisAgent, _extract_failures, _parse_test_output
from src.agents.classification_agent import ClassificationAgent
from src.api.fastapi_server import create_app
from src.core import langgraph_orchestrator as orchestrator
from src.core.deepseek_client import (
    DeepSeekClient,
    DeepSeekClientError,
    DeepSeekConfigError,
    DeepSeekError,
    DeepSeekRateLimitError,
    DeepSeekServerError,
    extract_json,
)
from src.utils.test_results import parse_test_counts


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
def _chat_payload(
    content: str,
    usage: dict | None = None,
    model: str = "deepseek-chat",
    finish_reason: str = "stop",
) -> dict:
    return {
        "id": "cmpl-test",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


class FakeHTTPResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body


class FakeStreamResponse:
    def __init__(self, lines: list[str]):
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)


@pytest.mark.parametrize("mode", ["offline", "normal", "wrong_location", "timeout", "unavailable"])
def test_api_deepseek_is_opt_in_and_preserves_offline_fallback(
    monkeypatch, tmp_path, mode,
):
    enabled = mode != "offline"
    available = mode in {"normal", "wrong_location"}
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    counts = {"passed": 2, "failed": 1, "skipped": 0}
    case_id = "test_shipping.py::test_free_shipping_threshold[5000-0]"
    stdout = (
        "E       AssertionError: Free shipping starts at 5000 cents\n"
        "E       assert 500 == 0\n"
        "\ntest_shipping.py:11: AssertionError\n"
        f"FAILED {case_id}\n"
        "1 failed, 2 passed in 0.10s"
    )
    monkeypatch.setattr(orchestrator, "execution_node", lambda state: {
        "stdout": stdout, "exit_code": 1, "test_counts": counts,
    })
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEP_SEEK_API_KEY", "fixture-key")
    model_location = "unrelated.py:999" if mode == "wrong_location" else "test_shipping.py:11"
    responses = [
        {"project_type": "python", "framework": "generic", "test_tool": "pytest",
         "language": "python", "confidence_score": 0.73},
        {"severity": "pass", "root_cause": "Model fixture root cause",
         "issues": [{"title": "Model fixture issue", "location": model_location}],
         "recommendations": [{"title": "Model fixture recommendation", "source": "pytest"}]},
    ]
    requests = []

    def open_fixture(self, request):
        body = json.loads(request.data)
        requests.append(body)
        assert body["stream"] is True
        if mode == "timeout" and len(requests) > 1:
            raise TimeoutError("Fixture analyst timeout")
        if mode == "unavailable":
            raise DeepSeekClientError("Fixture provider unavailable")
        data = {"choices": [{"delta": {"content": json.dumps(responses[len(requests) - 1])}}]}
        usage = {"usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}
        return FakeStreamResponse([f"data: {json.dumps(data)}\n", f"data: {json.dumps(usage)}\n", "data: [DONE]\n"])

    monkeypatch.setattr(DeepSeekClient, "_open", open_fixture)
    monkeypatch.setattr(DeepSeekClient, "_backoff", lambda *args: None)
    settings = Settings(_env_file=None, database_path=str(tmp_path / "api.db"), use_deepseek=enabled)
    with TestClient(create_app(settings)) as client:
        response = client.post("/evaluations", json={"project_path": str(project)})
        assert response.status_code == 200
        final = response.json()
    with TestClient(create_app(settings)) as client:
        history = client.get("/evaluations").json()
        reopened = client.get(f"/evaluations/{final['evaluation_id']}")
        assert reopened.status_code == 200
        report = reopened.json()

    assert final["test_tool"] == "pytest"
    assert final["evaluation_status"] == "failed"
    assert final["metrics"]["outcome"] == history[0]["status"] == "test_failed"
    assert final["metrics"]["test_counts"] == counts
    assert final["metrics"]["pass_rate"] == 0.6667
    stages = {row["stage"]: row["output_data"] for row in report["results"]}
    reporting = stages["reporting"]
    assert reporting["metrics"] == final["metrics"]
    assert reporting["recommendations"] == final["recommendations"]
    assert stages["analysis"] == reporting["detailed_report"]["analysis"] == final["analysis"]
    assert reporting["detailed_report"]["execution"]["stdout"] == stdout
    analysis = final["analysis"]
    assert [(issue["title"], issue["location"]) for issue in analysis["test_evidence"]] == [
        ("AssertionError: Free shipping starts at 5000 cents", "test_shipping.py:11"),
        ("assert 500 == 0", ""),
        (case_id, ""),
    ]
    assert len(requests) == (0 if not enabled else 4 if mode == "timeout" else 2)
    if enabled:
        usage = final["llm_usage"]
        assert usage["classification"]["total_tokens"] == (0 if mode == "unavailable" else 7)
        assert usage["analysis"]["total_tokens"] == (7 if available else 0)
        assert reporting["detailed_report"]["llm_usage"] == usage
    else:
        assert "llm_usage" not in final
    if enabled and available:
        assert final["classification_confidence"] == 0.73
        assert analysis["model_diagnosis"]["root_cause"] == "Model fixture root cause"
        assert analysis["model_diagnosis"]["issues"][0]["location"] == model_location
        assert [rec["title"] for rec in reporting["recommendations"] if rec.get("source") == "deepseek"] == [
            "Model fixture recommendation",
        ]
    else:
        assert "model_diagnosis" not in analysis
        assert all(rec.get("source") != "deepseek" for rec in reporting["recommendations"])


@pytest.mark.parametrize(("output", "tool", "expected"), [
    ("2 failed, 1 passed, 3 skipped in 0.20s", "pytest", (1, 2, 3)),
    ("Tests: 2 failed, 1 passed, 3 total", "jest", (1, 2, 0)),
    ('{"numPassedTests":1,"numFailedTests":2,"numPendingTests":1}', "jest", (1, 2, 1)),
    ("Ran 5 tests in 0.10s\nFAILED (failures=2, errors=1, skipped=1)", "unittest", (1, 3, 1)),
    ("runner unavailable", "pytest", None),
    ("1 passed, 1 warning in 0.10s", "pytest", (1, 0, 0)),
    ("1 failed, 1 passed, 2 warnings in 0.10s", "pytest", (1, 1, 0)),
])
def test_parse_runner_counts(output, tool, expected):
    counts = parse_test_counts(output, tool=tool)
    assert counts == (dict(zip(("passed", "failed", "skipped"), expected)) if expected is not None else None)


def test_go_counts_top_level_tests_only():
    events = [("pass", "TestPassing"), ("fail", "TestParent/child"), ("fail", "TestParent"), ("fail", None)]
    output = "\n".join(json.dumps({"Action": action, "Package": "example",
                                   **({"Test": name} if name else {})}) for action, name in events)
    assert parse_test_counts(output, tool="go-test") == {"passed": 1, "failed": 1, "skipped": 0}


def test_pytest_collection_errors_are_execution_failures_not_failed_cases():
    from src.agents.analysis_agent import _parse_test_output

    output = "ERROR collecting test_example.py\nSyntaxError: invalid syntax\n"
    result = _parse_test_output({
        "stdout": output, "stderr": "", "exit_code": 2, "test_counts": None,
        "failure_kind": "execution_failed",
    })
    assert result["severity"] == "critical"
    assert result["pass_rate"] is None
    assert result["summary"] == "exit code 2, no test counts detected"
    assert "invalid syntax" in result["issues"][0]["detail"]


class FakeChatClient:
    """Duck-typed client exposing only ``chat`` (no streaming)."""

    def __init__(self, content: str | None = None, exc: Exception | None = None):
        self.content = content
        self.exc = exc
        self.calls: list = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(content=self.content, usage=None)


class _FakeStream:
    def __init__(self, chunks: list[str]):
        self._chunks = list(chunks)
        self.text = ""

    def __iter__(self):
        return self

    def __next__(self):
        if not self._chunks:
            raise StopIteration
        chunk = self._chunks.pop(0)
        self.text += chunk
        return chunk


class FakeStreamClient:
    """Duck-typed client exposing only ``stream``."""

    def __init__(self, chunks: list[str]):
        self._chunks = chunks

    def stream(self, messages, **kwargs):
        return _FakeStream(self._chunks)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def node_project(tmp_path):
    proj = tmp_path / "node_proj"
    proj.mkdir()
    (proj / "package.json").write_text(json.dumps({
        "scripts": {"test": "jest"},
        "devDependencies": {"jest": "^29.0.0"},
    }))
    return proj


@pytest.fixture
def python_project(tmp_path):
    proj = tmp_path / "py_proj"
    proj.mkdir()
    (proj / "requirements.txt").write_text("pytest==7.4.0\n")
    return proj


# --------------------------------------------------------------------------- #
# deepseek_client: extract_json
# --------------------------------------------------------------------------- #
def test_extract_json_plain_object():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_fenced():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_with_surrounding_prose():
    assert extract_json('here is the answer: {"a": [1, 2]} thanks') == {"a": [1, 2]}


def test_extract_json_raises_on_garbage():
    with pytest.raises(ValueError):
        extract_json("no json here at all")


# --------------------------------------------------------------------------- #
# deepseek_client: chat + usage
# --------------------------------------------------------------------------- #
def test_chat_parses_response_and_tracks_usage(monkeypatch):
    client = DeepSeekClient(api_key="test-key")
    body = json.dumps(_chat_payload("hello")).encode("utf-8")
    monkeypatch.setattr(client, "_open", lambda request: FakeHTTPResponse(body))

    resp = client.chat([{"role": "user", "content": "hi"}])

    assert resp.content == "hello"
    assert resp.model == "deepseek-chat"
    assert resp.finish_reason == "stop"
    assert resp.usage.total_tokens == 30
    report = client.usage_report()
    assert report["calls"] == 1
    assert report["prompt_tokens"] == 10
    assert report["completion_tokens"] == 20
    assert report["total_tokens"] == 30


def test_chat_requires_api_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEP_SEEK_API_KEY", raising=False)
    client = DeepSeekClient(api_key=None)
    with pytest.raises(DeepSeekConfigError):
        client.chat([{"role": "user", "content": "hi"}])


# --------------------------------------------------------------------------- #
# deepseek_client: retry behaviour
# --------------------------------------------------------------------------- #
def test_chat_retries_transient_error_then_succeeds(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    client = DeepSeekClient(api_key="k", max_retries=3)
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("connection refused")
        return FakeHTTPResponse(json.dumps(_chat_payload("ok")).encode("utf-8"))

    monkeypatch.setattr(client, "_open", flaky)
    resp = client.chat([{"role": "user", "content": "hi"}])

    assert resp.content == "ok"
    assert calls["n"] == 3
    assert client._calls == 1


def test_chat_retries_exhausted_raises_server_error(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    client = DeepSeekClient(api_key="k", max_retries=3)
    calls = {"n": 0}

    def always_fail(request):
        calls["n"] += 1
        raise urllib.error.HTTPError("http://x", 500, "boom", None, None)

    monkeypatch.setattr(client, "_open", always_fail)
    with pytest.raises(DeepSeekServerError):
        client.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 3


def test_chat_does_not_retry_client_errors(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    client = DeepSeekClient(api_key="k", max_retries=3)
    calls = {"n": 0}

    def auth_fail(request):
        calls["n"] += 1
        raise urllib.error.HTTPError("http://x", 401, "unauthorized", None, None)

    monkeypatch.setattr(client, "_open", auth_fail)
    with pytest.raises(DeepSeekClientError):
        client.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 1


def test_rate_limit_maps_to_rate_limit_error(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    client = DeepSeekClient(api_key="k", max_retries=2)

    def rate_limited(request):
        raise urllib.error.HTTPError("http://x", 429, "slow down", None, None)

    monkeypatch.setattr(client, "_open", rate_limited)
    with pytest.raises(DeepSeekRateLimitError):
        client.chat([{"role": "user", "content": "hi"}])


# --------------------------------------------------------------------------- #
# deepseek_client: streaming
# --------------------------------------------------------------------------- #
def test_stream_accumulates_deltas_and_usage(monkeypatch):
    client = DeepSeekClient(api_key="k")
    lines = [
        'data: {"choices":[{"delta":{"content":"Hello "}}]}\n',
        'data: {"choices":[{"delta":{"content":"world"}}]}\n',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n',
        "data: [DONE]\n",
    ]
    monkeypatch.setattr(client, "_open", lambda request: FakeStreamResponse(lines))

    stream = client.stream([{"role": "user", "content": "hi"}])
    assert "".join(stream) == "Hello world"
    assert stream.text == "Hello world"
    assert stream.finish_reason == "stop"
    assert stream.usage.total_tokens == 7
    assert stream.usage.estimated is False
    assert client._calls == 1


def test_stream_estimates_usage_when_not_reported(monkeypatch):
    client = DeepSeekClient(api_key="k")
    lines = [
        'data: {"choices":[{"delta":{"content":"short"}}]}\n',
        "data: [DONE]\n",
    ]
    monkeypatch.setattr(client, "_open", lambda request: FakeStreamResponse(lines))

    stream = client.stream([{"role": "user", "content": "hello"}])
    "".join(stream)
    assert stream.usage.estimated is True
    assert stream.usage.completion_tokens > 0


# --------------------------------------------------------------------------- #
# ClassificationAgent
# --------------------------------------------------------------------------- #
def test_classify_uses_llm_result(node_project):
    content = json.dumps({
        "project_type": "node",
        "framework": "express",
        "test_tool": "jest",
        "language": "typescript",
        "confidence_score": 0.91,
        "evidence": ["package.json devDependencies include jest"],
    })
    agent = ClassificationAgent(client=FakeChatClient(content=content))
    result = agent.classify(str(node_project))

    assert result["project_type"] == "node"
    assert result["framework"] == "express"
    assert result["test_tool"] == "jest"
    assert result["language"] == "typescript"
    assert result["confidence_score"] == 0.91


def test_classify_falls_back_to_rules_on_llm_error(node_project):
    agent = ClassificationAgent(client=FakeChatClient(exc=DeepSeekError("boom")))
    result = agent.classify(str(node_project))

    assert result["project_type"] == "node"
    assert result["test_tool"] == "jest"
    assert result["language"] == "javascript"
    assert 0.0 <= result["confidence_score"] <= 1.0


def test_classify_falls_back_on_invalid_json(node_project):
    agent = ClassificationAgent(client=FakeChatClient(content="not valid json"))
    result = agent.classify(str(node_project))
    assert result["test_tool"] == "jest"


def test_classify_merges_vague_llm_with_rules(node_project):
    content = json.dumps({
        "project_type": "generic",
        "framework": "generic",
        "test_tool": "generic",
        "language": "unknown",
        "confidence_score": 0.4,
        "evidence": [],
    })
    agent = ClassificationAgent(client=FakeChatClient(content=content))
    result = agent.classify(str(node_project))

    assert result["project_type"] == "node"
    assert result["test_tool"] == "jest"
    assert result["language"] == "javascript"


def test_classify_clamps_confidence(node_project):
    content = json.dumps({
        "project_type": "python",
        "framework": "generic",
        "test_tool": "pytest",
        "language": "python",
        "confidence_score": 12.0,
        "evidence": [],
    })
    agent = ClassificationAgent(client=FakeChatClient(content=content))
    result = agent.classify(str(node_project))
    assert result["confidence_score"] == 1.0


def test_classify_supports_streaming_client(python_project):
    chunks = [
        '{"project_type": "python", "framework": "flask", "test_tool": "pytest", ',
        '"language": "python", "confidence_score": 0.8, "evidence": ["requirements.txt"]}',
    ]
    agent = ClassificationAgent(client=FakeStreamClient(chunks))
    result = agent.classify(str(python_project))

    assert result["project_type"] == "python"
    assert result["test_tool"] == "pytest"
    assert result["framework"] == "flask"
    assert result["confidence_score"] == 0.8


# --------------------------------------------------------------------------- #
# AnalysisAgent
# --------------------------------------------------------------------------- #
def _pytest_failure_stdout() -> str:
    return (
        "test_x.py::test_ok PASSED\n"
        "test_x.py::test_bad FAILED\n"
        "E   assert 1 == 2\n"
        "test_x.py:12: AssertionError\n"
        "3 passed, 1 failed in 0.10s\n"
    )


def test_parse_test_output_deterministic():
    result = _parse_test_output({
        "stdout": _pytest_failure_stdout(),
        "stderr": "",
        "exit_code": 1,
        "coverage_data": {"percent_covered": 45.0},
    })

    assert result["severity"] == "high"
    assert result["pass_rate"] == 0.75
    assert len(result["issues"]) >= 1
    assert result["root_cause"]
    assert any("coverage" in r["title"].lower() for r in result["recommendations"])


def test_pytest_failure_keeps_source_line_assertion_diff_and_full_node_id():
    node_id = (
        "test_datamind_config.py::DatasetConfigTests::"
        "test_company_config_sets_currency_status_and_isolated_databases"
    )
    stdout = (
        "E       AssertionError: 'CNY' != 'USD'\n"
        "E       - CNY\nE       + USD\n\n"
        "test_datamind_config.py:26: AssertionError\n"
        "______________________________ test_other ______________________________\n"
        "E       ValueError: invalid input\n\n"
        "folder with spaces/test_other.py:8: ValueError\n"
        "=========================== short test summary info ===========================\n"
        f"FAILED {node_id}\n"
    )
    issues = _extract_failures(stdout)
    assert [(issue["title"], issue["location"]) for issue in issues] == [
        ("AssertionError: 'CNY' != 'USD'", "test_datamind_config.py:26"),
        ("- CNY", ""),
        ("+ USD", ""),
        ("ValueError: invalid input", "folder with spaces/test_other.py:8"),
        (node_id, ""),
    ]
    assert _extract_failures(stdout, limit=1) == issues[:1]


def test_parse_test_output_passing_with_coverage():
    result = _parse_test_output({
        "stdout": "2 passed in 0.05s\n",
        "stderr": "",
        "exit_code": 0,
        "coverage_data": {"percent_covered": 85.0},
    })
    assert result["severity"] == "pass"
    assert result["pass_rate"] == 1.0


def test_analyze_uses_llm_result():
    content = json.dumps({
        "issues": [{"title": "Assertion failed", "detail": "1 != 2", "location": "test_x.py:12"}],
        "severity": "high",
        "root_cause": "A logic bug in the comparison.",
        "recommendations": [{"title": "Fix the assertion", "message": "Check the expected value."}],
    })
    agent = AnalysisAgent(client=FakeChatClient(content=content))
    result = agent.analyze({
        "stdout": _pytest_failure_stdout(),
        "stderr": "",
        "exit_code": 1,
        "coverage_data": {},
    })

    assert result["severity"] == "high"
    assert result["root_cause"] == "A logic bug in the comparison."
    assert result["issues"][0]["title"] == "Assertion failed"
    assert result["recommendations"][0]["title"] == "Fix the assertion"


def test_analyze_falls_back_on_llm_error():
    agent = AnalysisAgent(client=FakeChatClient(exc=DeepSeekError("boom")))
    result = agent.analyze({
        "stdout": _pytest_failure_stdout(),
        "stderr": "",
        "exit_code": 1,
        "coverage_data": {},
    })

    assert result["severity"] == "high"
    assert len(result["issues"]) >= 1
    assert result["recommendations"]


def test_analyze_coerces_string_lists():
    content = json.dumps({
        "issues": ["some issue"],
        "severity": "medium",
        "root_cause": "the cause",
        "recommendations": ["do a thing"],
    })
    agent = AnalysisAgent(client=FakeChatClient(content=content))
    result = agent.analyze({
        "stdout": "1 passed\n",
        "stderr": "",
        "exit_code": 0,
        "coverage_data": {"percent_covered": 50.0},
    })

    assert result["issues"][0]["title"] == "some issue"
    assert result["recommendations"][0]["title"] == "do a thing"
