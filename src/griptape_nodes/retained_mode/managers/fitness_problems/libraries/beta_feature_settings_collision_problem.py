from __future__ import annotations

from dataclasses import dataclass

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem


@dataclass
class BetaFeatureSettingsCollisionProblem(LibraryProblem):
    """Problem indicating two libraries would store their beta feature choices in the same place.

    Library names are turned into config keys by lowercasing and replacing punctuation, so two
    different names can end up with the same key.
    """

    other_library_name: str
    config_key: str

    @classmethod
    def collate_problems_for_display(cls, instances: list[BetaFeatureSettingsCollisionProblem]) -> str:
        """Display the collisions, one line per other library."""
        lines = [
            f"Beta features share their settings with library '{problem.other_library_name}', because both "
            f"libraries store them under '{problem.config_key}'. A feature id declared by both libraries uses one "
            "toggle for both. Rename one of the libraries to keep them separate."
            for problem in instances
        ]
        return "\n".join(lines)
