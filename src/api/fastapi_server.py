"""Local API: inspect the graph, run evaluations and retrieve reports."""

from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config.settings import Settings
from src.core.deepseek_client import DeepSeekClient
from src.core.evaluation_comparison import compare_evaluations
from src.core.langgraph_orchestrator import WorkflowEngine
from src.core.state_manager import WorkflowRequest
from src.models.database import Database


class EvaluationSummary(BaseModel):
    evaluation_id: str
    project_name: str | None
    started_at: str
    status: Literal["passed", "test_failed", "execution_failed", "unknown"]
    pass_rate: float | None
    gate_passed: bool | None = None


STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="TestMirror", version="0.3.0")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        """Zero-build local page; talks to the endpoints below only."""
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/workflow")
    def workflow() -> dict[str, str]:
        with WorkflowEngine(":memory:", use_checkpointer=False) as engine:
            return {"mermaid": engine.mermaid()}

    @app.post("/evaluations")
    def evaluate(request: WorkflowRequest) -> dict:
        with WorkflowEngine(
            settings.database_path, max_retries=settings.max_retries,
            deepseek_client=DeepSeekClient() if settings.use_deepseek else None,
        ) as engine:
            return engine.run(
                str(request.project_path), evaluation_id=request.evaluation_id,
                max_retries=(
                    request.max_retries if "max_retries" in request.model_fields_set
                    else settings.max_retries
                ),
                metadata=request.metadata,
                exclude_dirs=request.exclude_dirs,
                min_coverage=request.min_coverage,
            )

    @app.get("/evaluations", response_model=list[EvaluationSummary])
    def recent_evaluations(limit: int = Query(default=20, ge=1, le=100)) -> list[dict]:
        with Database(settings.database_path) as db:
            return db.list_evaluations(limit)

    @app.get("/evaluations/{evaluation_id}")
    def get_report(evaluation_id: str) -> dict:
        with Database(settings.database_path) as db:
            report = db.get_full_report(evaluation_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Evaluation not found")
        return report

    @app.get("/evaluation-comparisons")
    def compare_reports(
        baseline_id: str = Query(min_length=1),
        current_id: str = Query(min_length=1),
    ) -> dict:
        """Compare saved pytest results without running tests again."""
        with Database(settings.database_path) as db, db.transaction():
            baseline = db.get_full_report(baseline_id)
            if baseline is None:
                raise HTTPException(status_code=404, detail="Baseline evaluation not found")
            current = db.get_full_report(current_id)
            if current is None:
                raise HTTPException(status_code=404, detail="Current evaluation not found")
        return compare_evaluations(baseline, current)

    return app


app = create_app()
