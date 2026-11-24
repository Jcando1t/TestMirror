"""Small shared helpers (timestamps, JSON-safe conversion)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp with a ``Z`` suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def json_dumps(value: Any) -> str:
    """JSON-serialize arbitrary values without failing on non-serializable objects."""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(value), ensure_ascii=False)


def json_loads(value: str | None, default: Any = None) -> Any:
    """Safely deserialize a JSON string."""
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def truncate(text: str | None, limit: int = 20_000) -> str:
    """Bound captured output so database rows stay manageable."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"
