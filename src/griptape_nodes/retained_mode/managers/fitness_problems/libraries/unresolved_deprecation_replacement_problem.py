from __future__ import annotations

from dataclasses import dataclass

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem


@dataclass
class UnresolvedDeprecationReplacementProblem(LibraryProblem):
    """A deprecated node names a replacement that no loaded library provides.

    Either the replacement's library isn't loaded, or it is loaded and has no node type by that name.
    The deprecated node still loads; only the pointer to its replacement is wrong.

    Stackable.
    """

    display_name: str
    replacement_library: str
    replacement_node_type: str
    replacement_library_loaded: bool

    @classmethod
    def collate_problems_for_display(cls, instances: list[UnresolvedDeprecationReplacementProblem]) -> str:
        if len(instances) == 1:
            return f"Deprecated node {cls._describe(instances[0])}."

        output_lines = [f"Encountered {len(instances)} deprecated nodes whose replacement can't be found:"]
        output_lines.extend(
            f"  - {cls._describe(problem)}" for problem in sorted(instances, key=lambda p: p.display_name)
        )
        return "\n".join(output_lines)

    @staticmethod
    def _describe(problem: UnresolvedDeprecationReplacementProblem) -> str:
        target = f"'{problem.replacement_node_type}' from library '{problem.replacement_library}'"
        if problem.replacement_library_loaded:
            reason = f"library '{problem.replacement_library}' has no node type '{problem.replacement_node_type}'"
        else:
            reason = f"library '{problem.replacement_library}' is not loaded"
        return f"'{problem.display_name}' names {target} as its replacement, but {reason}"
