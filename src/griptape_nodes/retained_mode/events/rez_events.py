"""Rez integration request/response events."""

from dataclasses import dataclass, field

from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayloadSuccess,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry


@dataclass
class RezHealthStatus:
    """Structured health check result with timing information."""

    healthy: bool = False
    check_duration_ms: float = 0.0
    package_count: int = 0
    timestamp: str = ""


@dataclass
class RezLibraryStatus:
    """Rez package status for a single registered library."""

    library_name: str = ""
    library_path: str = ""
    has_rez_package: bool = False
    rez_family: str | None = None
    rez_version: str | None = None


@dataclass
@PayloadRegistry.register
class GetRezStatusRequest(RequestPayload):
    """Request current rez integration status."""


@dataclass
@PayloadRegistry.register
class GetRezStatusResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Current rez integration status."""

    enabled: bool = False
    context_string: str = ""
    studio_root: str = ""
    path_map: dict[str, str] = field(default_factory=dict)
    health: RezHealthStatus = field(default_factory=RezHealthStatus)
    library_statuses: list[RezLibraryStatus] = field(default_factory=list)
    health_green_threshold_ms: float = 500.0
    health_red_threshold_ms: float = 2000.0


@dataclass
@PayloadRegistry.register
class CheckRezHealthRequest(RequestPayload):
    """Request a rez health re-check with timing data."""


@dataclass
@PayloadRegistry.register
class CheckRezHealthResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Rez health check result with timing."""

    healthy: bool = False
    check_duration_ms: float = 0.0
    package_count: int = 0
    timestamp: str = ""
