from __future__ import annotations

import logging
from dataclasses import dataclass

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem

logger = logging.getLogger(__name__)


@dataclass
class MCPToolRegistrationProblem(LibraryProblem):
    """Problem indicating a failure registering MCP tools for a library."""

    error_message: str

    @classmethod
    def collate_problems_for_display(cls, instances: list[MCPToolRegistrationProblem]) -> str:
        if len(instances) > 1:
            logger.error(
                "MCPToolRegistrationProblem: Expected 1 instance but got %s.",
                len(instances),
            )
        return f"Error registering MCP tools: {instances[0].error_message}"
