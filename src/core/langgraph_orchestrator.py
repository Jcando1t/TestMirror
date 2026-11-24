"""LangGraph orchestration engine for the TestMirror multi-agent workflow.

Graph topology (5 agents + internal persistence and retry nodes)::

    START -> classifier -> planner -> executor -> analyst -> reporter -> persistence -> END

Every agent node is wrapped by an error boundary.  Failures are recorded in
``state["errors"]`` and routed through a conditional edge to a bounded retry
node; when retries are exhausted the graph degrades gracefully to the Report
Agent so that a partial evaluation is still produced and persisted.
"""

from __future__ import annotations

import traceback
from functools import partial
from typing import Annotated, Any, Callable, Dict, Iterable, Optional
from uuid import uuid4

from langchain_core.callbacks.manager import dispatch_custom_event
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from pydantic import Field, TypeAdapter

from src.agents.analysis_agent import deterministic_analysis_node as analysis_node
from src.agents.analysis_agent import analysis_node as deepseek_analysis_node
from src.agents.classification_agent import classification_node as deepseek_classification_node
from src.agents.classifier_agent import classification_node
from src.agents.execution_agent import execution_node
from src.agents.persistence_agent import create_persistence_node
from src.agents.planning_agent import planning_node
from src.agents.report_agent import compute_metrics, report_node
from src.core.deepseek_client import DeepSeekClient
from src.core.message_schema import AgentMessage, AgentName
from src.core.state_manager import StateManager
from src.models.database import Database
from src.models.state import WorkflowState
from src.utils.helpers import utc_now
from src.utils.logging_config import get_logger

logger = get_logger(__name__)

State = WorkflowState
AGENT_NAMES = ("classifier", "planner", "executor", "analyst", "reporter")
NODE_NAMES = (*AGENT_NAMES, "persistence")
NEXT_AGENT = dict(zip(NODE_NAMES, (*NODE_NAMES[1:], "orchestrator")))
RetryBudget = Annotated[int, Field(ge=0, le=20, strict=True)]


def _make_router(abort_node: str = "reporter") -> Callable[[WorkflowState], str]:
    """Create a conditional-edge function: continue / retry / abort."""

    def router(state: WorkflowState) -> str:
        if state.get("status") == "error":
            if int(state.get("retry_count", 0)) < int(state.get("max_retries", 2)):
                return "retry"
            logger.error(
                "Retries exhausted at agent '%s'; routing to %s",
                state.get("current_agent"), abort_node,
            )
            return "abort"
        return "continue"

    return router


class WorkflowEngine:
    """Compile and run the TestMirror workflow."""

    def __init__(
        self,
        db_path: str = "data/testmirror.db",
        *,
        max_retries: int = 2,
        use_checkpointer: bool = True,
        deepseek_client: DeepSeekClient | None = None,
    ):
        self.max_retries = TypeAdapter(RetryBudget).validate_python(max_retries)
        self.db = Database(db_path)
        self.checkpointer = MemorySaver() if use_checkpointer else None
        self.deepseek_client = deepseek_client
        self.graph = self._build_graph()

    # ---- graph construction -------------------------------------------
    @staticmethod
    def _guarded(node: Callable[[WorkflowState], Dict[str, Any]], name: AgentName):
        """Convert a raising node into a state-error record (no graph crash)."""

        def wrapped(state: WorkflowState, config: RunnableConfig) -> Dict[str, Any]:
            event = {"evaluation_id": state["evaluation_id"], "agent": name}
            dispatch_custom_event("agent_start", event, config=config)
            try:
                output = node(state)
                if not isinstance(output, dict):
                    raise TypeError(f"Node {name} must return a dict, got {type(output)}")
                message = AgentMessage(
                    evaluation_id=state["evaluation_id"], sender=name,
                    receiver=NEXT_AGENT[name], kind="result",
                    payload={key: value for key, value in output.items() if key != "messages"},
                ).model_dump(mode="json")
            except Exception as exc:  # noqa: BLE001 - this is the error boundary
                tb = traceback.format_exc()
                logger.error("Agent '%s' failed: %s", name, exc)
                error = {
                    "agent": name,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": tb,
                    "retry_count": int(state.get("retry_count", 0)),
                    "timestamp": utc_now(),
                }
                message = AgentMessage(
                    evaluation_id=state["evaluation_id"], sender=name,
                    receiver="orchestrator", kind="error", payload=error,
                ).model_dump(mode="json")
                dispatch_custom_event("agent_error", {**event, "error": error}, config=config)
                return {
                    "status": "error",
                    "current_agent": name,
                    "errors": [error],
                    "messages": [message],
                    "logs": [f"[{utc_now()}] Agent {name} failed: {type(exc).__name__}: {exc}"],
                }
            dispatch_custom_event("agent_end", event, config=config)
            return {**output, "status": "running", "current_agent": name, "messages": [message]}

        wrapped.__name__ = f"guarded_{name}"
        return wrapped

    @staticmethod
    def _retry_node(state: WorkflowState) -> Dict[str, Any]:
        """Increment the shared retry budget and clear the node failure flag."""
        count = int(state.get("retry_count", 0)) + 1
        target = state.get("current_agent", "classifier")
        logger.warning("Retry #%d for agent '%s'", count, target)
        return {
            "retry_count": count,
            "status": "running",
            "logs": [f"[{utc_now()}] Retry #{count} for {target}"],
        }

    @staticmethod
    def _retry_target(state: WorkflowState) -> str:
        target = state.get("current_agent", "classifier")
        return target if target in NODE_NAMES else "classifier"

    def _build_graph(self):
        builder = StateGraph(WorkflowState)
        classify, analyze = classification_node, analysis_node
        if self.deepseek_client is not None:
            classify = self._with_usage(deepseek_classification_node, "classification")
            analyze = self._with_usage(deepseek_analysis_node, "analysis")

        # Five connected agent nodes, followed by internal persistence.
        builder.add_node("classifier", self._guarded(classify, "classifier"))
        builder.add_node("planner", self._guarded(planning_node, "planner"))
        builder.add_node("executor", self._guarded(execution_node, "executor"))
        builder.add_node("analyst", self._guarded(analyze, "analyst"))
        builder.add_node("reporter", self._guarded(report_node, "reporter"))
        builder.add_node("persistence", self._guarded(create_persistence_node(self.db), "persistence"))

        # Internal retry control node.
        builder.add_node("retry", self._retry_node)

        builder.add_edge(START, "classifier")

        for node, next_node, abort in (
            ("classifier", "planner", "analyst"),
            ("planner", "executor", "analyst"),
            ("executor", "analyst", "analyst"),
            ("analyst", "reporter", "reporter"),
            ("reporter", "persistence", "persistence"),
            ("persistence", END, END),
        ):
            builder.add_conditional_edges(
                node,
                _make_router(abort),
                {"retry": "retry", "continue": next_node, "abort": abort},
            )

        builder.add_conditional_edges(
            "retry",
            self._retry_target,
            {name: name for name in NODE_NAMES},
        )

        return builder.compile(checkpointer=self.checkpointer)

    def _with_usage(self, node, stage: str):
        client = self.deepseek_client
        assert client is not None
        run = partial(node, client=client)

        def track(state: WorkflowState) -> Dict[str, Any]:
            before = client.usage_report()
            output = run(state)
            after = client.usage_report()
            usage = {"model": after["model"], **{
                key: after[key] - before[key]
                for key in ("calls", "prompt_tokens", "completion_tokens", "total_tokens", "estimated_calls")
            }}
            return {**output, "llm_usage": {**state.get("llm_usage", {}), stage: usage}}

        return track

    # ---- execution -----------------------------------------------------
    def run(
        self,
        project_path: str,
        *,
        evaluation_id: Optional[str] = None,
        max_retries: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
        exclude_dirs: list[str] | None = None,
        min_coverage: float | None = None,
        config: Optional[RunnableConfig] = None,
    ) -> Dict[str, Any]:
        """Execute the full workflow and return the final state as a plain dict."""
        state, run_config = self._prepare_run(
            project_path, evaluation_id=evaluation_id, max_retries=max_retries,
            metadata=metadata, config=config,
            exclude_dirs=exclude_dirs, min_coverage=min_coverage,
        )
        eval_id = state["evaluation_id"]
        logger.info("Starting workflow %s for %s", eval_id, project_path)
        final = self.graph.invoke(state, config=run_config)
        final = dict(final)
        if not final.get("persisted"):
            final["metrics"] = compute_metrics(final)

        # Node-level status vs. evaluation outcome.
        evaluation_status = final.get("evaluation_status")
        if evaluation_status is None:
            evaluation_status = "failed" if final.get("errors") or final.get("exit_code", 0) != 0 else "passed"
        final["evaluation_status"] = evaluation_status
        final["status"] = (
            "success" if final.get("errors") == [] and evaluation_status == "passed"
            else "completed" if final.get("persisted")
            else "error"
        )
        final["completed_at"] = final.get("completed_at") or utc_now()

        logger.info(
            "Workflow %s finished: status=%s score=%s",
            eval_id, final["status"], final.get("overall_score"),
        )
        return final

    def _prepare_run(
        self, project_path: str, *, evaluation_id: Optional[str] = None,
        max_retries: Optional[int] = None, metadata: Optional[Dict[str, Any]] = None,
        exclude_dirs: list[str] | None = None,
        min_coverage: float | None = None,
        config: Optional[RunnableConfig] = None,
    ) -> tuple[WorkflowState, RunnableConfig]:
        state = StateManager.create(
            project_path, evaluation_id=evaluation_id,
            max_retries=self.max_retries if max_retries is None else max_retries,
            metadata=metadata or {},
            exclude_dirs=exclude_dirs or [], min_coverage=min_coverage,
        )
        run_config: RunnableConfig = dict(config or {})
        run_config.setdefault("recursion_limit", 2 * state["max_retries"] + len(NODE_NAMES) + 5)
        run_config["configurable"] = {
            **run_config.get("configurable", {}),
            # Each invocation is a new run, even if the evaluation id is reused.
            "thread_id": str(uuid4()),
        }
        return state, run_config

    def stream(self, project_path: str, **kwargs) -> Iterable[Dict[str, Any]]:
        """Yield per-node updates; accepts the same options/callbacks as run()."""
        state, config = self._prepare_run(project_path, **kwargs)
        yield from self.graph.stream(state, config=config, stream_mode="updates")

    def get_report(self, evaluation_id: str) -> Optional[Dict[str, Any]]:
        """Read a persisted report from the database."""
        return self.db.get_full_report(evaluation_id)

    def mermaid(self) -> str:
        """Return a Mermaid representation of the graph (used by validation)."""
        return self.graph.get_graph().draw_mermaid()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "WorkflowEngine":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def run_workflow(project_path: str, db_path: str = "data/testmirror.db", **kwargs) -> Dict[str, Any]:
    """Convenience one-shot API used by scripts and integration tests."""
    with WorkflowEngine(
        db_path, max_retries=kwargs.pop("max_retries", 2),
        deepseek_client=kwargs.pop("deepseek_client", None),
    ) as engine:
        return engine.run(project_path, **kwargs)


def summarize(state: Dict[str, Any]) -> Dict[str, Any]:
    """Compact, JSON-safe summary for CLI output."""
    return {
        "evaluation_id": state.get("evaluation_id"),
        "status": state.get("status"),
        "evaluation_status": state.get("evaluation_status"),
        "score": state.get("overall_score"),
        "summary": state.get("summary"),
        "classification": {
            "project_type": state.get("project_type"),
            "framework": state.get("framework"),
            "test_tool": state.get("test_tool"),
            "language": state.get("language"),
        },
        "exit_code": state.get("exit_code"),
        "execution_time": state.get("execution_time"),
        "execution_provenance": state.get("execution_provenance"),
        "coverage": state.get("coverage_data"),
        "metrics": state.get("metrics"),
        "recommendations": len(state.get("recommendations", [])),
        "errors": len(state.get("errors", [])),
        "retry_count": state.get("retry_count"),
        "persisted": state.get("persisted"),
    }
