from __future__ import annotations

from dataclasses import dataclass

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem


@dataclass
class InvalidBetaFeatureProblem(LibraryProblem):
    """Problem indicating a beta feature declared in the library JSON can't be used as written.

    This is stackable - several features can have problems. The rest of the library still loads.
    """

    feature_id: str
    reason: str

    @classmethod
    def collate_problems_for_display(cls, instances: list[InvalidBetaFeatureProblem]) -> str:
        """Display beta feature problems, one line per feature."""
        if len(instances) == 1:
            problem = instances[0]
            return f"Beta feature '{problem.feature_id}' {problem.reason}."

        output_lines = [f"Encountered {len(instances)} problems with beta features:"]
        output_lines.extend(
            f"  - '{problem.feature_id}' {problem.reason}." for problem in sorted(instances, key=lambda p: p.feature_id)
        )
        return "\n".join(output_lines)
