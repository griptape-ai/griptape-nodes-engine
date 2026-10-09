from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.node_library.library_registry import (
        LibrarySchema,
    )
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
    from griptape_nodes.retained_mode.managers.fitness_problems.libraries import (
        LibraryProblem,
    )


LIBRARY_CONFIG_FILENAME = "griptape_nodes_library.json"
LIBRARY_CONFIG_GLOB_PATTERN = "griptape[_-]nodes[_-]library.json"


class LibraryLifecycleState(StrEnum):
    """Lifecycle states for library loading."""

    FAILURE = "failure"
    DISCOVERED = "discovered"
    METADATA_LOADED = "metadata_loaded"
    EVALUATED = "evaluated"
    DEPENDENCIES_INSTALLED = "dependencies_installed"
    LOADED = "loaded"
    DISABLED = "disabled"


class LibraryFitness(StrEnum):
    """Fitness of the library that was attempted to be loaded."""

    GOOD = "GOOD"  # No errors detected during loading. Registered.
    FLAWED = "FLAWED"  # Some errors detected, but recoverable. Registered.
    UNUSABLE = "UNUSABLE"  # Errors detected and not recoverable. Not registered.
    MISSING = "MISSING"  # File not found. Not registered.
    NOT_EVALUATED = "NOT_EVALUATED"  # Library has not been evaluated yet.


@dataclass(frozen=True)
class LibraryLoadProgress:
    """Where a library sits in the load that is registering it, for its progress events.

    Attributes:
        current: The library's 1-based position in the load
        total: How many libraries the load registers
    """

    current: int
    total: int


@dataclass
class RegisteredEventHandler[TRegisteredEventData]:
    """Information regarding an event handler from a registered library.

    The generic type parameter TRegisteredEventData allows each event type
    to specify its own structured additional data.
    """

    handler: Callable[[RequestPayload], ResultPayload]
    library_data: LibrarySchema
    event_data: TRegisteredEventData | None = None


@dataclass
class LibraryInfo:
    """Information about a library that was attempted to be loaded.

    Tracks the lifecycle state (where we are in the loading process) and fitness (health/quality).
    Includes the file path and any problems encountered during loading.

    Attributes:
        lifecycle_state: Current phase of the library loading lifecycle (DISCOVERED → METADATA_LOADED →
                       EVALUATED → DEPENDENCIES_INSTALLED → LOADED, or FAILURE at any phase)
        fitness: Health/quality assessment of the library (GOOD, FLAWED, UNUSABLE, NOT_EVALUATED)
        library_path: Absolute path to the library JSON file or sandbox directory
        is_sandbox: True if this is a sandbox library (user-created nodes in workspace), False for regular libraries
        library_name: Name of the library from metadata (None until METADATA_LOADED phase)
        library_version: Schema version from metadata (None until METADATA_LOADED phase)
        problems: List of issues encountered during any phase (version incompatibilities, node load failures, etc.)
                 Problems accumulate across lifecycle phases and determine final fitness level.
    """

    lifecycle_state: LibraryLifecycleState
    fitness: LibraryFitness
    # Resolved absolute path to the library JSON file. Used as the dict key in
    # `_library_file_path_to_info` and for filesystem operations.
    library_path: str
    is_sandbox: bool
    library_name: str | None = None
    library_version: str | None = None
    # Why this library cannot execute right now; None when nothing is known to be wrong. A
    # library can be perfectly loaded and editable while execution is unavailable. Set when its
    # worker is evicted, and deliberately NOT when execution dependencies fail to install: that
    # happens in the worker's own process, so the orchestrator never learns it.
    execution_unavailable_reason: str | None = None
    # The path string the user wrote in `libraries_to_register` before workspace
    # resolution / `~`-expansion / symlink-following. Surfaced to the GUI so the
    # settings panel can match library metadata back to its config row using the
    # exact key the user sees in their config. None for sandbox libraries
    # (registered through workspace discovery, not via `libraries_to_register`).
    registered_path: str | None = None
    problems: list[LibraryProblem] = field(default_factory=list)
    # Mirrors LibraryRegistration.enabled from the user's libraries_to_register config.
    # True for sandbox, ad-hoc, and bare-string entries; False only when the user
    # explicitly set enabled=false on the config object form.
    enabled: bool = True
    # True when this library's nodes EXECUTE in a dedicated worker process, because it
    # declares execution dependencies (pip_dependencies_exec). The library loads REAL
    # nodes on the orchestrator and only its process() runs in the worker, where
    # .venv-exec is on sys.path. Consumed by execution routing; never by load-time skips.
    executes_in_worker: bool = False

    # Why the last `.venv-exec` build failed; None when it succeeded. Deliberately separate
    # from execution_unavailable_reason, which _start_workers clears before every spawn attempt
    # -- the spawn refusal reads THIS field, so a failure recorded at registration must survive
    # that clearing.
    execution_env_failure: str | None = None
