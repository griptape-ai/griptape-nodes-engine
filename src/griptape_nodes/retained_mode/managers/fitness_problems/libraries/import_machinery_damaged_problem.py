from __future__ import annotations

import logging
from dataclasses import dataclass

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem

logger = logging.getLogger(__name__)


@dataclass
class ImportMachineryDamagedProblem(LibraryProblem):
    """Problem indicating a library's load hook damaged Python's import machinery.

    The engine puts the affected modules back, so this records damage that has already been
    repaired. It exists to attribute the damage to the library that caused it: without it the
    only visible symptom is import failures in unrelated libraries loaded afterwards.
    """

    hook_name: str
    module_names: list[str]

    @classmethod
    def collate_problems_for_display(cls, instances: list[ImportMachineryDamagedProblem]) -> str:
        """Display import machinery damage caused by this library's hooks.

        There is at most one instance per hook, so a library can report this twice (once for
        `before_library_nodes_loaded` and once for `after_library_nodes_loaded`).
        """
        descriptions = []
        for instance in instances:
            modules = ", ".join(instance.module_names)
            descriptions.append(f"{instance.hook_name} replaced Python's import machinery ({modules})")

        joined = "; ".join(descriptions)
        return (
            f"This library damaged Python's import machinery: {joined}. The engine restored it, but "
            f"other libraries loaded in between may have failed to load. Update this library, or "
            f"disable it and restart the engine."
        )
