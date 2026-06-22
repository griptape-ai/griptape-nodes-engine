from __future__ import annotations

import logging
from dataclasses import dataclass

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem

logger = logging.getLogger(__name__)


@dataclass
class RequestHandlersWorkerIncompatibleProblem(LibraryProblem):
    """Problem raised when a worker-mode library declares request/response handlers.

    Handlers registered via get_request_handlers() are registered in whichever
    process loads the library. For worker-mode libraries this is the worker
    process; the orchestrator's event manager never sees them, so orchestrator
    routing to those request types will fail at runtime.

    Tracked in: https://github.com/griptape-ai/griptape-nodes-engine/issues/4748
    """

    library_name: str
    handler_count: int

    @classmethod
    def collate_problems_for_display(cls, instances: list[RequestHandlersWorkerIncompatibleProblem]) -> str:
        if len(instances) > 1:
            logger.error(
                "RequestHandlersWorkerIncompatibleProblem: Expected 1 instance but got %s.",
                len(instances),
            )
        p = instances[0]
        return (
            f"Library '{p.library_name}' declares {p.handler_count} request/response handler(s) "
            f"via get_request_handlers() but is configured to run in worker mode. "
            f"Handlers registered in the worker process are not reachable from the orchestrator. "
            f"Cross-worker handler forwarding is tracked in "
            f"https://github.com/griptape-ai/griptape-nodes-engine/issues/4748"
        )
