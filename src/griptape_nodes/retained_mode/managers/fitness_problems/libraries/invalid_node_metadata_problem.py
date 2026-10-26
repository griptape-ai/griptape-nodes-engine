from __future__ import annotations

from dataclasses import dataclass

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem


@dataclass
class InvalidNodeMetadataProblem(LibraryProblem):
    """Problem indicating a value in a node's metadata isn't one the engine recognizes.

    This is stackable - several nodes can have problems. The bad value is dropped and the node
    still registers.
    """

    node_type: str
    reason: str

    @classmethod
    def collate_problems_for_display(cls, instances: list[InvalidNodeMetadataProblem]) -> str:
        """Display node metadata problems, one line per problem."""
        if len(instances) == 1:
            problem = instances[0]
            return f"Node '{problem.node_type}' metadata: {problem.reason}."

        output_lines = [f"Ignored {len(instances)} node metadata values:"]
        output_lines.extend(
            f"  - '{problem.node_type}': {problem.reason}."
            for problem in sorted(instances, key=lambda p: (p.node_type, p.reason))
        )
        return "\n".join(output_lines)
