from __future__ import annotations

from dataclasses import dataclass

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem


@dataclass
class InvalidPresetProblem(LibraryProblem):
    """Problem indicating a preset declared in the library JSON can't be used as written.

    This is stackable - several presets can have problems. The preset is dropped and the rest of
    the library still loads.
    """

    preset_name: str
    reason: str

    @classmethod
    def collate_problems_for_display(cls, instances: list[InvalidPresetProblem]) -> str:
        """Display preset problems, one line per preset."""
        if len(instances) == 1:
            problem = instances[0]
            return f"Preset '{problem.preset_name}' was skipped because {problem.reason}."

        output_lines = [f"Skipped {len(instances)} presets:"]
        output_lines.extend(
            f"  - '{problem.preset_name}': {problem.reason}."
            for problem in sorted(instances, key=lambda p: p.preset_name)
        )
        return "\n".join(output_lines)
