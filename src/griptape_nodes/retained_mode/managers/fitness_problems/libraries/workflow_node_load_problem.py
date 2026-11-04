from __future__ import annotations

import logging
from dataclasses import dataclass

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem

logger = logging.getLogger(__name__)


@dataclass
class WorkflowNodeLoadProblem(LibraryProblem):
    """Problem indicating a node declared in `workflow_nodes` could not be generated.

    This is stackable - a library can declare many workflow-backed nodes.
    """

    node_type: str
    workflow_path: str
    error_message: str

    @classmethod
    def collate_problems_for_display(cls, instances: list[WorkflowNodeLoadProblem]) -> str:
        """List each workflow-backed node that failed to load, with the reason."""
        if len(instances) == 1:
            problem = instances[0]
            return f"Failed to build node '{problem.node_type}' from workflow '{problem.workflow_path}': {problem.error_message}"

        sorted_instances = sorted(instances, key=lambda problem: problem.node_type)
        output_lines = [f"Encountered {len(instances)} workflow-backed node failures:"]
        output_lines.extend(
            f"  - {problem.node_type} (from '{problem.workflow_path}'): {problem.error_message}"
            for problem in sorted_instances
        )
        return "\n".join(output_lines)
