from __future__ import annotations

import logging
from dataclasses import dataclass

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem

logger = logging.getLogger(__name__)


@dataclass
class NodeModuleNamespaceCollisionProblem(LibraryProblem):
    """Problem indicating a node file maps to a module namespace another file already uses.

    This is stackable - multiple nodes can collide.
    """

    class_name: str
    file_path: str
    conflicting_file_path: str
    stable_namespace: str

    @classmethod
    def collate_problems_for_display(cls, instances: list[NodeModuleNamespaceCollisionProblem]) -> str:
        """Display node module namespace collision problems.

        Can handle multiple collisions - they will be listed out sorted by class_name.
        """
        if len(instances) == 1:
            problem = instances[0]
            return (
                f"Attempted to load node '{problem.class_name}' from '{problem.file_path}'. Failed because "
                f"'{problem.conflicting_file_path}' already uses the same module name. Rename one of the two "
                "files so their names differ."
            )

        # Multiple namespace collisions - list them sorted by class_name
        sorted_instances = sorted(instances, key=lambda p: p.class_name)
        error_lines = []
        for i, problem in enumerate(sorted_instances, 1):
            error_lines.append(
                f"  {i}. Node '{problem.class_name}' from '{problem.file_path}' conflicts with "
                f"'{problem.conflicting_file_path}' under module name '{problem.stable_namespace}'"
            )

        header = f"Encountered {len(instances)} node files with conflicting module names:"
        return header + "\n" + "\n".join(error_lines)
