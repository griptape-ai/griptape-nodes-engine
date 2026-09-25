from __future__ import annotations

from dataclasses import dataclass

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem


@dataclass
class RezEnvironmentProblem(LibraryProblem):
    """Problem indicating a library's rez environment does not resolve on this workstation."""

    error_message: str

    @classmethod
    def collate_problems_for_display(cls, instances: list[RezEnvironmentProblem]) -> str:
        if len(instances) == 1:
            return f"The library's rez environment is not available on this workstation: {instances[0].error_message}"
        lines = ["The library's rez environment is not available on this workstation:"]
        lines.extend(f"  {i.error_message}" for i in instances)
        return "\n".join(lines)
