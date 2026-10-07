from __future__ import annotations

import logging
from dataclasses import dataclass

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem
from griptape_nodes.utils.engine_dirs import engine_data_dir

logger = logging.getLogger(__name__)


@dataclass
class OldXdgLocationWarningProblem(LibraryProblem):
    """Problem warning that a library is located in the old XDG data directory.

    This is a warning-level problem (FLAWED status) - the library will still
    load and function normally, but users should migrate to the new location.
    """

    old_path: str

    @classmethod
    def collate_problems_for_display(cls, instances: list[OldXdgLocationWarningProblem]) -> str:
        """Display old XDG location warning.

        There should only be one instance per library since each LibraryInfo
        is already associated with a specific library path.
        """
        if len(instances) > 1:
            logger.error(
                "OldXdgLocationWarningProblem: Expected 1 instance but got %s. Each LibraryInfo should only have one OldXdgLocationWarningProblem.",
                len(instances),
            )

        old_libraries_path = engine_data_dir() / "libraries"
        return (
            f"WARNING: Starting with version 0.65.0, libraries are now managed in your workspace directory. "
            f"This library is located in {old_libraries_path} and will not receive updates because it is not tracked "
            f"by the library manager. "
            f"To migrate: run 'gtn init'. "
            f"The library will continue to function normally until migrated."
        )
