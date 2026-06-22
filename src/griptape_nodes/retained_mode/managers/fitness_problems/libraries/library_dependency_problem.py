from __future__ import annotations

from dataclasses import dataclass

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem


@dataclass
class LibraryDependencyProblem(LibraryProblem):
    """Problem indicating a required library dependency failed to load."""

    dependency_name: str
    error_message: str

    @classmethod
    def collate_problems_for_display(cls, instances: list[LibraryDependencyProblem]) -> str:
        if len(instances) == 1:
            return f"Required library dependency '{instances[0].dependency_name}' failed to load: {instances[0].error_message}"
        lines = ["Required library dependencies failed to load:"]
        lines.extend(f"  '{i.dependency_name}': {i.error_message}" for i in instances)
        return "\n".join(lines)
