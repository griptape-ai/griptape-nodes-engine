from __future__ import annotations

import logging
from dataclasses import dataclass

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem

logger = logging.getLogger(__name__)


@dataclass
class PreDispatchHookRegistrationProblem(LibraryProblem):
    """Problem indicating a failure registering pre-dispatch hooks for a library."""

    error_message: str

    @classmethod
    def collate_problems_for_display(cls, instances: list[PreDispatchHookRegistrationProblem]) -> str:
        if len(instances) > 1:
            logger.error(
                "PreDispatchHookRegistrationProblem: Expected 1 instance but got %s.",
                len(instances),
            )
        messages = "; ".join(p.error_message for p in instances)
        return f"Error registering pre-dispatch hooks: {messages}"
