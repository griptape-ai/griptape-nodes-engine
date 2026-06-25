from __future__ import annotations

from dataclasses import dataclass, field

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem


@dataclass
class PermissionDeniedProblem(LibraryProblem):
    """Problem indicating the license policy does not permit loading this library.

    `messages` is one human-readable sentence per missing permission, so the
    failure icon can list every reason the library is blocked rather than the
    first.
    """

    library_name: str
    messages: list[str] = field(default_factory=list)

    @classmethod
    def collate_problems_for_display(cls, instances: list[PermissionDeniedProblem]) -> str:
        """List every missing permission across all instances, de-duplicated in order."""
        lines: list[str] = []
        for problem in instances:
            for message in problem.messages:
                if message not in lines:
                    lines.append(message)
        if not lines:
            return "This library is not permitted by your license."
        bullets = "\n".join(f"- {line}" for line in lines)
        return f"This library is not permitted by your license:\n{bullets}"
