"""Payloads carried by element message callbacks."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel


class NodeMessagePayload(BaseModel):
    """Structured payload for node messages.

    This replaces the use of Any in message payloads, providing
    better type safety and validation for node message handling.
    """

    data: Any = None


class NodeMessageResult(BaseModel):
    """Result from a node message callback.

    Attributes:
        success: True if the message was handled successfully, False otherwise
        details: Human-readable description of what happened
        response: Optional response data to return to the sender
        altered_workflow_state: True if the message handling altered workflow state.
            Clients can use this to determine if the workflow needs to be re-saved.
    """

    success: bool
    details: str
    response: NodeMessagePayload | None = None
    altered_workflow_state: bool = True


# Type alias for element message callback functions
type ElementMessageCallback = Callable[[str, "NodeMessagePayload | None"], "NodeMessageResult"]
