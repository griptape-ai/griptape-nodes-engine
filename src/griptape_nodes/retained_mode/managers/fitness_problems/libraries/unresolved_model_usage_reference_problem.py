from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem

logger = logging.getLogger(__name__)


@dataclass
class UnresolvedModelUsageReferenceProblem(LibraryProblem):
    """A node's `ModelUsageNodeProperty.model_ids` entry doesn't resolve to any catalog model.

    Either the catalog doesn't declare the id, or the node was authored against
    an older version of the catalog. Either way the engine can't tell what the
    node intended.

    Stackable.
    """

    library_name: str
    class_name: str
    model_id: str

    @classmethod
    def collate_problems_for_display(cls, instances: list[UnresolvedModelUsageReferenceProblem]) -> str:
        if len(instances) == 1:
            problem = instances[0]
            return (
                f"Node '{problem.class_name}' in library '{problem.library_name}' "
                f"references model id '{problem.model_id}', which is not "
                f"declared in the library's ModelCatalogLibraryProperty."
            )

        by_library: dict[str, list[UnresolvedModelUsageReferenceProblem]] = defaultdict(list)
        for problem in instances:
            by_library[problem.library_name].append(problem)

        output_lines = [f"Encountered {len(instances)} unresolved model references:"]
        for library_name in sorted(by_library.keys()):
            output_lines.append(f"  Library '{library_name}':")
            for problem in sorted(by_library[library_name], key=lambda p: (p.class_name, p.model_id)):
                output_lines.append(  # noqa: PERF401  -- explicit loop is clearer than list.extend
                    f"    - node '{problem.class_name}' references missing model '{problem.model_id}'"
                )
        return "\n".join(output_lines)
