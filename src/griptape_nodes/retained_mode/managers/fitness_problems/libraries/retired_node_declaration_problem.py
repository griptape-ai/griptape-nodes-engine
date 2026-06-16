from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem

logger = logging.getLogger(__name__)


@dataclass
class RetiredNodeDeclarationProblem(LibraryProblem):
    """A node declares a `type` that was valid in an older schema but has since been removed.

    The discriminated-union validator would otherwise reject these with an
    opaque "Input tag ... does not match any of the expected tags" message.
    This problem replaces that with targeted migration guidance so authors of
    libraries written against an older schema know what to change.

    Stackable.
    """

    library_name: str
    class_name: str
    declaration_type: str
    guidance: str

    @classmethod
    def collate_problems_for_display(cls, instances: list[RetiredNodeDeclarationProblem]) -> str:
        if len(instances) == 1:
            problem = instances[0]
            return (
                f"Node '{problem.class_name}' in library '{problem.library_name}' uses retired node "
                f"declaration type '{problem.declaration_type}'. {problem.guidance}"
            )

        by_library: dict[str, list[RetiredNodeDeclarationProblem]] = defaultdict(list)
        for problem in instances:
            by_library[problem.library_name].append(problem)

        output_lines = [f"Encountered {len(instances)} retired node declarations:"]
        for library_name in sorted(by_library.keys()):
            output_lines.append(f"  Library '{library_name}':")
            for problem in sorted(by_library[library_name], key=lambda p: (p.class_name, p.declaration_type)):
                output_lines.append(  # noqa: PERF401  -- explicit loop is clearer than list.extend
                    f"    - node '{problem.class_name}' uses retired type "
                    f"'{problem.declaration_type}'. {problem.guidance}"
                )
        return "\n".join(output_lines)
