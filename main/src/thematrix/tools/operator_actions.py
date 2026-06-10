from __future__ import annotations

from pydantic import BaseModel


class OperatorToolResult(BaseModel):
    """Outcome of an agent-requested Operator action (notify or schedule)."""

    operation: str
    target: str = ""
    purpose: str = ""
    decision: str = "blocked"
    executed: bool = False
    reason: str = ""
    goal_id: str | None = None
