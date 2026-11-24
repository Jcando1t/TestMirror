"""Persistence Agent: final workflow node that stores the evaluation in SQLite."""

from __future__ import annotations

from typing import Any, Callable, Dict

from src.agents.report_agent import compute_metrics
from src.models.database import Database
from src.models.state import WorkflowState
from src.utils.helpers import utc_now
from src.utils.logging_config import get_logger

logger = get_logger(__name__)

PersistenceNode = Callable[[WorkflowState], Dict[str, Any]]


def create_persistence_node(db: Database) -> PersistenceNode:
    """Build a LangGraph node bound to a specific :class:`Database` instance.

    A factory is used (instead of a module-level global) so tests and the
    workflow engine can each supply their own database, including ``:memory:``.
    """

    def persistence_node(state: WorkflowState) -> Dict[str, Any]:
        logger.info("Persisting evaluation %s", state.get("evaluation_id"))
        try:
            completed = dict(state)
            completed["completed_at"] = utc_now()
            if "metrics" not in completed:
                completed["metrics"] = compute_metrics(state)
                completed["evaluation_status"] = "failed"
            db.save_workflow_state(completed)
            logger.info("Evaluation %s persisted successfully", state.get("evaluation_id"))
            return {
                "persisted": True,
                "database_path": db.db_path,
                "completed_at": completed["completed_at"],
                "metrics": completed["metrics"],
                "evaluation_status": completed["evaluation_status"],
                "current_agent": "persistence",
                "logs": [f"[{utc_now()}] Evaluation saved to {db.db_path}"],
            }
        except Exception as exc:  # noqa: BLE001 - reported to the workflow router
            logger.exception("Persistence failed for %s", state.get("evaluation_id"))
            raise

    return persistence_node
