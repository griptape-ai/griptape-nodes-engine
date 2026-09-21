from __future__ import annotations

import logging
from dataclasses import dataclass

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem

logger = logging.getLogger(__name__)


@dataclass
class PostDispatchHooksWorkerIncompatibleProblem(LibraryProblem):
    """Problem raised when a worker-mode library declares post-dispatch hooks.

    Hooks registered via get_post_dispatch_hooks() are registered on the event
    manager of whichever process loads the library. For worker-mode libraries
    that is the worker process, which only ever dispatches the requests forwarded
    to it, so hooks on requests handled by the orchestrator never fire.

    Tracked in: https://github.com/griptape-ai/griptape-nodes-engine/issues/4748
    """

    library_name: str
    hook_count: int

    @classmethod
    def collate_problems_for_display(cls, instances: list[PostDispatchHooksWorkerIncompatibleProblem]) -> str:
        if len(instances) > 1:
            logger.error(
                "PostDispatchHooksWorkerIncompatibleProblem: Expected 1 instance but got %s.",
                len(instances),
            )
        p = instances[0]
        return (
            f"Library '{p.library_name}' declares {p.hook_count} post-dispatch hook(s) "
            f"via get_post_dispatch_hooks() but is configured to run in worker mode. "
            f"Hooks registered in the worker process do not observe requests handled by the orchestrator. "
            f"Cross-worker hook support is tracked in "
            f"https://github.com/griptape-ai/griptape-nodes-engine/issues/4748"
        )
