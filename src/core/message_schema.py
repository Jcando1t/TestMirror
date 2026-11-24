"""Versioned, JSON-safe envelopes exchanged through workflow state."""

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

AgentName = Literal[
    "classifier", "planner", "executor", "analyst", "reporter",
    "persistence", "orchestrator",
]


class AgentMessage(BaseModel):
    """A node result or error, correlated with one workflow evaluation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    message_id: UUID = Field(default_factory=uuid4)
    evaluation_id: str = Field(min_length=1)
    sender: AgentName
    receiver: AgentName
    kind: Literal["result", "error"]
    payload: dict[str, JsonValue]
    timestamp: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))
