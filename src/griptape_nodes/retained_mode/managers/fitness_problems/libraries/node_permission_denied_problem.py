from __future__ import annotations

from dataclasses import dataclass, field

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem


@dataclass
class NodePermissionDeniedProblem(LibraryProblem):
    """Problem indicating the license policy does not permit a node type in this library.

    Surfaced while a library loads: the library itself still loads (its permitted
    nodes work), but each denied node type is listed so the GUI failure icon
    explains why it is blocked and what to ask an admin for. Instantiating a denied
    node later substitutes an Error Proxy carrying the same reasons. Stackable: one
    instance per denied node type.
    """

    node_type: str
    messages: list[str] = field(default_factory=list)

    @classmethod
    def collate_problems_for_display(cls, instances: list[NodePermissionDeniedProblem]) -> str:
        """List every denied node type with the reasons it is blocked, de-duplicated in order."""
        lines: list[str] = []
        for problem in instances:
            reasons = "; ".join(problem.messages) or "Denied by the license policy."
            entry = f"- {problem.node_type}: {reasons}"
            if entry not in lines:
                lines.append(entry)
        if not lines:
            return "Some nodes are not permitted by your license."
        body = "\n".join(lines)
        return f"Some nodes are not permitted by your license:\n{body}"
