"""Create validated, independent state for each workflow run."""

from pathlib import PureWindowsPath
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, DirectoryPath, Field, field_validator

from src.models.state import WorkflowState, initial_state


class WorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_path: DirectoryPath
    evaluation_id: str | None = Field(default=None, min_length=1)
    max_retries: int = Field(default=2, ge=0, le=20)
    min_coverage: float | None = Field(default=None, ge=0, le=100)
    metadata: dict[str, Any] = Field(default_factory=dict)
    exclude_dirs: list[str] = Field(default_factory=list)

    @field_validator("exclude_dirs")
    @classmethod
    def relative_directories(cls, values: list[str]) -> list[str]:
        paths = [PureWindowsPath(value) for value in values]
        if any(path.anchor or not path.parts or ".." in path.parts or "\x00" in str(path) for path in paths):
            raise ValueError("exclude_dirs entries must be relative subdirectories")
        return [path.as_posix() for path in paths]


class StateManager:
    """Initial state factory; LangGraph's checkpointer owns runtime snapshots."""

    @staticmethod
    def create(project_path: str, **kwargs: Any) -> WorkflowState:
        request = WorkflowRequest(project_path=project_path, **kwargs)
        state = initial_state(
            str(request.project_path.resolve()),
            request.evaluation_id or f"eval-{uuid4()}",
            max_retries=request.max_retries,
            metadata=request.metadata,
        )
        state["exclude_dirs"] = request.exclude_dirs
        state["min_coverage"] = request.min_coverage
        return state
