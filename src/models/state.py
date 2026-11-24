"""Shared state definitions for the TestMirror multi-agent workflow.

All agents communicate exclusively through :class:`WorkflowState`.  The
TypedDict mirrors the state schema consumed by LangGraph's ``StateGraph``.
"""

from __future__ import annotations

from typing import Annotated, Any, Dict, List, Optional, TypedDict
from src.utils.test_results import PytestCaseResults, TestCounts


class ClassificationResult(TypedDict, total=False):
    """Output contract of the classification agent."""

    project_type: str
    framework: str
    test_tool: str
    language: str
    confidence_score: float
    evidence: List[str]


class ModelDiagnosis(TypedDict):
    root_cause: str
    issues: List[Dict[str, str]]


class AnalysisResult(TypedDict, total=False):
    """Output contract of the analysis agent.

    ``issues`` and ``recommendations`` are lists of small dictionaries so they
    can be persisted directly (see :meth:`Database.add_recommendation`).
    """

    issues: List[Dict[str, str]]
    test_evidence: List[Dict[str, str]]
    model_diagnosis: ModelDiagnosis
    severity: str
    root_cause: str
    recommendations: List[Dict[str, str]]
    summary: str
    pass_rate: float | None
    metrics: Dict[str, Any]


class TestCommand(TypedDict, total=False):
    """A single executable command in a test plan."""

    command: List[str]
    description: str
    phase: str
    optional: bool  # optional commands never fail the whole execution


class TestPlan(TypedDict, total=False):
    """Output contract of the planning agent."""

    test_strategy: str
    commands: List[TestCommand]
    environment_vars: Dict[str, str]
    expected_output: Dict[str, Any]
    timeout_seconds: int


class ExecutionResult(TypedDict, total=False):
    """Output contract of the execution agent."""

    exit_code: int
    stdout: str
    stderr: str
    execution_time: float
    coverage_data: Dict[str, Any]
    command_results: List[Dict[str, Any]]
    test_counts: TestCounts | None
    test_case_results: PytestCaseResults | None


class ReportOutput(TypedDict, total=False):
    """Output contract of the report agent."""

    summary: str
    detailed_report: Dict[str, Any]
    metrics: Dict[str, Any]
    recommendations: List[Dict[str, Any]]
    timestamp: str
    overall_score: float | None


class AgentError(TypedDict, total=False):
    """Structured error record appended to the workflow state."""

    agent: str
    error_type: str
    message: str
    traceback: str
    retry_count: int
    timestamp: str


def append_list(left: List[Any], right: List[Any]) -> List[Any]:
    """LangGraph reducer: concatenate list values instead of replacing them."""
    return (left or []) + (right or [])


class WorkflowState(TypedDict, total=False):
    """The single source of truth flowing through every workflow node."""

    # ---- run identity -------------------------------------------------
    evaluation_id: str
    project_path: str
    project_name: str
    exclude_dirs: List[str]
    min_coverage: float | None

    # ---- classification output ---------------------------------------
    project_type: str
    framework: str
    test_tool: str
    language: str
    classification_confidence: float
    classification_evidence: Annotated[List[str], append_list]

    # ---- planning output ----------------------------------------------
    test_strategy: str
    commands: List[TestCommand]
    environment_vars: Dict[str, str]
    expected_output: Dict[str, Any]
    timeout_seconds: int

    # ---- execution output ---------------------------------------------
    exit_code: int
    stdout: str
    stderr: str
    execution_time: float
    coverage_data: Dict[str, Any]
    command_results: List[Dict[str, Any]]
    test_counts: TestCounts | None
    execution_provenance: Dict[str, Any]
    test_case_results: PytestCaseResults | None

    # ---- analysis output ----------------------------------------------
    analysis: AnalysisResult
    llm_usage: Dict[str, Any]

    # ---- report output ------------------------------------------------
    summary: str
    detailed_report: Dict[str, Any]
    metrics: Dict[str, Any]
    recommendations: Annotated[List[Dict[str, Any]], append_list]
    timestamp: str
    overall_score: float | None

    # ---- persistence output -------------------------------------------
    persisted: bool
    database_path: str

    # ---- workflow control ---------------------------------------------
    current_agent: str
    status: str  # running | success | error  (node-level control)
    evaluation_status: str  # passed | failed  (evaluation result)
    errors: Annotated[List[AgentError], append_list]
    logs: Annotated[List[str], append_list]
    retry_count: int
    max_retries: int
    started_at: str
    completed_at: str
    metadata: Dict[str, Any]
    messages: Annotated[List[Dict[str, Any]], append_list]


def initial_state(
    project_path: str,
    evaluation_id: str,
    *,
    max_retries: int = 2,
    metadata: Optional[Dict[str, Any]] = None,
) -> WorkflowState:
    """Build a clean initial state for a workflow run."""
    from src.utils.helpers import utc_now

    now = utc_now()
    return WorkflowState(
        evaluation_id=evaluation_id,
        project_path=project_path,
        project_name=project_path.rstrip("/\\").split("/")[-1].split("\\")[-1],
        classification_evidence=[],
        recommendations=[],
        errors=[],
        messages=[],
        logs=[f"[{now}] Workflow initialized for {project_path}"],
        retry_count=0,
        max_retries=max_retries,
        status="running",
        current_agent="classifier",
        started_at=now,
        metadata=metadata or {},
    )
