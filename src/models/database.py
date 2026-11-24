"""SQLite persistence layer for TestMirror evaluation history."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from src.utils.helpers import json_dumps, json_loads, truncate, utc_now
from src.utils.logging_config import get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS evaluations (
    id                  TEXT PRIMARY KEY,
    project_path        TEXT NOT NULL,
    project_name        TEXT,
    project_type        TEXT,
    framework           TEXT,
    test_tool           TEXT,
    language            TEXT,
    status              TEXT NOT NULL DEFAULT 'running',
    strategy            TEXT,
    started_at          TEXT NOT NULL,
    completed_at        TEXT,
    total_execution_time REAL DEFAULT 0.0,
    overall_score       REAL,
    schema_version      INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id TEXT NOT NULL REFERENCES evaluations(id) ON DELETE CASCADE,
    stage         TEXT NOT NULL,
    agent_name    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'success',
    exit_code     INTEGER,
    stdout        TEXT,
    stderr        TEXT,
    execution_time REAL DEFAULT 0.0,
    coverage_data TEXT,
    output_data   TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS errors (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id TEXT NOT NULL REFERENCES evaluations(id) ON DELETE CASCADE,
    stage        TEXT,
    agent_name   TEXT,
    error_type   TEXT,
    error_message TEXT,
    traceback    TEXT,
    retry_count  INTEGER DEFAULT 0,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recommendations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id TEXT NOT NULL REFERENCES evaluations(id) ON DELETE CASCADE,
    category      TEXT,
    priority      TEXT,
    title         TEXT,
    message       TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_results_evaluation ON results(evaluation_id);
CREATE INDEX IF NOT EXISTS idx_errors_evaluation  ON errors(evaluation_id);
CREATE INDEX IF NOT EXISTS idx_recs_evaluation    ON recommendations(evaluation_id);
CREATE INDEX IF NOT EXISTS idx_evaluations_time   ON evaluations(started_at);
"""


class Database:
    """Thread-safe SQLite store with a small, explicit CRUD API."""

    def __init__(self, db_path: str | Path = "data/testmirror.db"):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self.initialize()

    # ---- connection management ----------------------------------------
    def initialize(self) -> None:
        with self._lock:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()
            logger.debug("SQLite schema initialized at %s", self.db_path)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._conn
            assert conn is not None
            if not conn.in_transaction:
                with conn:
                    conn.execute("BEGIN")
                    yield conn
                return

            # CRUD helpers can run inside a report transaction without committing it.
            conn.execute("SAVEPOINT testmirror_nested")
            try:
                yield conn
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK TO SAVEPOINT testmirror_nested")
                    conn.execute("RELEASE SAVEPOINT testmirror_nested")
                raise
            else:
                conn.execute("RELEASE SAVEPOINT testmirror_nested")

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ---- writes ---------------------------------------------------------
    def create_evaluation(self, state: Dict[str, Any]) -> None:
        """Insert (or replace) the evaluation root row from a workflow state."""
        with self.transaction() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO evaluations
                   (id, project_path, project_name, project_type, framework,
                    test_tool, language, status, strategy, started_at,
                    completed_at, total_execution_time, overall_score, schema_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    state.get("evaluation_id"),
                    state.get("project_path", ""),
                    state.get("project_name"),
                    state.get("project_type"),
                    state.get("framework"),
                    state.get("test_tool"),
                    state.get("language"),
                    state.get("evaluation_status")
                    or ("error" if state.get("errors") else "success"),
                    state.get("test_strategy"),
                    state.get("started_at") or utc_now(),
                    state.get("completed_at"),
                    float(state.get("execution_time") or 0.0),
                    state.get("overall_score"),
                    SCHEMA_VERSION,
                ),
            )

    def add_result(self, evaluation_id: str, stage: str, agent_name: str,
                   output: Dict[str, Any], status: str = "success") -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO results
                   (evaluation_id, stage, agent_name, status, exit_code, stdout,
                    stderr, execution_time, coverage_data, output_data, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    evaluation_id, stage, agent_name, status,
                    output.get("exit_code"),
                    truncate(output.get("stdout")),
                    truncate(output.get("stderr")),
                    float(output.get("execution_time") or 0.0),
                    json_dumps(output.get("coverage_data")) if output.get("coverage_data") is not None else None,
                    json_dumps(output),
                    utc_now(),
                ),
            )
            return int(cur.lastrowid)

    def add_error(self, evaluation_id: str, stage: str, agent_name: str,
                  error_type: str, message: str, traceback_text: str = "",
                  retry_count: int = 0) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO errors
                   (evaluation_id, stage, agent_name, error_type, error_message,
                    traceback, retry_count, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    evaluation_id, stage, agent_name, error_type,
                    truncate(message, 4000), truncate(traceback_text, 8000),
                    retry_count, utc_now(),
                ),
            )
            return int(cur.lastrowid)

    def add_recommendation(self, evaluation_id: str, rec: Dict[str, Any]) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO recommendations
                   (evaluation_id, category, priority, title, message, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    evaluation_id,
                    rec.get("category", "general"),
                    rec.get("priority", "medium"),
                    rec.get("title", ""),
                    rec.get("message", ""),
                    utc_now(),
                ),
            )
            return int(cur.lastrowid)

    def save_workflow_state(self, state: Dict[str, Any]) -> str:
        """Persist a complete workflow run (evaluation + results + errors + recs)."""
        evaluation_id = state["evaluation_id"]

        stage_map = [
            ("classifier", "classification", {
                "project_type": state.get("project_type"),
                "framework": state.get("framework"),
                "test_tool": state.get("test_tool"),
                "language": state.get("language"),
                "confidence": state.get("classification_confidence"),
                "evidence": state.get("classification_evidence", []),
            }),
            ("planner", "planning", {
                "test_strategy": state.get("test_strategy"),
                "commands": state.get("commands", []),
                "environment_vars": state.get("environment_vars", {}),
                "expected_output": state.get("expected_output", {}),
            }),
            ("executor", "execution", {
                "exit_code": state.get("exit_code"),
                "provenance": state.get("execution_provenance"),
                "stdout": state.get("stdout"),
                "stderr": state.get("stderr"),
                "execution_time": state.get("execution_time"),
                "coverage_data": state.get("coverage_data"),
                "test_counts": state.get("test_counts"),
                "test_case_results": state.get("test_case_results"),
                "command_results": state.get("command_results", []),
            }),
            ("analyst", "analysis", state.get("analysis", {})),
            ("reporter", "reporting", {
                "summary": state.get("summary"),
                "detailed_report": state.get("detailed_report"),
                "metrics": state.get("metrics"),
                "overall_score": state.get("overall_score"),
                "recommendations": state.get("recommendations"),
            }),
        ]
        with self.transaction() as conn:
            conn.execute("DELETE FROM evaluations WHERE id = ?", (evaluation_id,))
            self.create_evaluation(state)
            for agent, stage, output in stage_map:
                self.add_result(evaluation_id, stage, agent, output)

            for err in state.get("errors", []):
                self.add_error(
                    evaluation_id,
                    err.get("agent", "workflow"),
                    err.get("agent", "unknown"),
                    err.get("error_type", "Error"),
                    err.get("message", ""),
                    err.get("traceback", ""),
                    int(err.get("retry_count") or 0),
                )

            for rec in state.get("recommendations", []):
                self.add_recommendation(evaluation_id, rec)

        logger.info("Persisted evaluation %s to %s", evaluation_id, self.db_path)
        return evaluation_id

    # ---- reads ----------------------------------------------------------
    def _rows_to_dicts(self, rows: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
        return [dict(row) for row in rows]

    def get_evaluation(self, evaluation_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM evaluations WHERE id = ?", (evaluation_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_evaluations(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT e.id AS evaluation_id, e.project_name, e.started_at,
                          COALESCE(json_extract(r.output_data, '$.metrics.outcome'),
                                   CASE e.status WHEN 'passed' THEN 'passed'
                                                 WHEN 'error' THEN 'execution_failed'
                                                 ELSE 'unknown' END) AS status,
                          CASE WHEN json_type(r.output_data, '$.metrics.test_counts') = 'object'
                               THEN json_extract(r.output_data, '$.metrics.pass_rate') END AS pass_rate,
                          json_extract(r.output_data, '$.metrics.quality_gate.passed') AS gate_passed
                   FROM evaluations e
                   LEFT JOIN results r ON r.id = (
                       SELECT id FROM results
                       WHERE evaluation_id = e.id AND stage = 'reporting'
                       ORDER BY id DESC LIMIT 1
                   )
                   ORDER BY e.started_at DESC, e.rowid DESC LIMIT ?""", (limit,),
            ).fetchall()
            return self._rows_to_dicts(rows)

    def get_results(self, evaluation_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM results WHERE evaluation_id = ? ORDER BY id", (evaluation_id,)
            ).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                item["coverage_data"] = json_loads(item.get("coverage_data"))
                item["output_data"] = json_loads(item.get("output_data"))
                results.append(item)
            return results

    def get_errors(self, evaluation_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM errors WHERE evaluation_id = ? ORDER BY id", (evaluation_id,)
            ).fetchall()
            return self._rows_to_dicts(rows)

    def get_recommendations(self, evaluation_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM recommendations WHERE evaluation_id = ? ORDER BY id",
                (evaluation_id,),
            ).fetchall()
            return self._rows_to_dicts(rows)

    def get_full_report(self, evaluation_id: str) -> Optional[Dict[str, Any]]:
        with self.transaction():
            evaluation = self.get_evaluation(evaluation_id)
            if evaluation is None:
                return None
            return {
                "evaluation": evaluation,
                "results": self.get_results(evaluation_id),
                "errors": self.get_errors(evaluation_id),
                "recommendations": self.get_recommendations(evaluation_id),
            }

    def count(self, table: str) -> int:
        if table not in {"evaluations", "results", "errors", "recommendations"}:
            raise ValueError(f"Unknown table: {table}")
        with self._lock:
            return int(self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
