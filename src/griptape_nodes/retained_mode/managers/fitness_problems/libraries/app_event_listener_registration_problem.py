from __future__ import annotations

import logging
from dataclasses import dataclass

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem

logger = logging.getLogger(__name__)


@dataclass
class AppEventListenerRegistrationProblem(LibraryProblem):
    """Problem indicating a failure registering app event listeners for a library."""

    error_message: str

    @classmethod
    def collate_problems_for_display(cls, instances: list[AppEventListenerRegistrationProblem]) -> str:
        if len(instances) > 1:
            logger.error(
                "AppEventListenerRegistrationProblem: Expected 1 instance but got %s.",
                len(instances),
            )
        return f"Error registering app event listeners: {instances[0].error_message}"
