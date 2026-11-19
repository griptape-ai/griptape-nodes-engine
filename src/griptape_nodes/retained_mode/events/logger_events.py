from dataclasses import dataclass

from griptape_nodes.retained_mode.events.base_events import (
    AppPayload,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry


@dataclass
@PayloadRegistry.register
class LogHandlerEvent(AppPayload):
    """Log message event from the logging system.

    Use when: Capturing log messages for UI display, implementing log viewers,
    monitoring application behavior, debugging system issues.

    Args:
        message: The log message content
        levelname: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        created: Timestamp when the log entry was created
        node_name: The node that logged it while running, or None for the engine's own logs. A node's
            line names its workflow on the `AppEvent` that carries it.
    """

    message: str
    levelname: str
    created: float
    node_name: str | None = None
