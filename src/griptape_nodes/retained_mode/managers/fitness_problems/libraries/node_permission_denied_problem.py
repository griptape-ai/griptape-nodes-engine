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
        """List every denied node type with its reasons as nested bullets, de-duplicated in order."""
        entries: list[str] = []
        for problem in instances:
            reasons: list[str] = []
            for message in problem.messages:
                if message not in reasons:
                    reasons.append(message)
            if not reasons:
                reasons = ["Denied by the license policy."]
            nested = "\n".join(f"  - {reason}" for reason in reasons)
            entry = f"- {problem.node_type}:\n{nested}"
            if entry not in entries:
                entries.append(entry)
        if not entries:
            return "Some nodes are not permitted by your license."
        body = "\n".join(entries)
        return f"Some nodes are not permitted by your license:\n{body}"
