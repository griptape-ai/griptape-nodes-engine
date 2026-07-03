from __future__ import annotations

import logging
from dataclasses import dataclass

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem

logger = logging.getLogger(__name__)


@dataclass
class RequestHandlerRegistrationProblem(LibraryProblem):
    """Problem indicating a failure registering request/response handlers for a library."""

    error_message: str

    @classmethod
    def collate_problems_for_display(cls, instances: list[RequestHandlerRegistrationProblem]) -> str:
        if len(instances) > 1:
            logger.error(
                "RequestHandlerRegistrationProblem: Expected 1 instance but got %s.",
                len(instances),
            )
        return f"Error registering request handlers: {instances[0].error_message}"
