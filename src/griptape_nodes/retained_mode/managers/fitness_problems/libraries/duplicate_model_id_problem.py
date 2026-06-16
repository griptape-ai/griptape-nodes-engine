from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem

logger = logging.getLogger(__name__)


@dataclass
class DuplicateModelIdProblem(LibraryProblem):
    """The same model id appears under two different providers within a library's catalog.

    Pydantic enforces sibling-uniqueness within a single provider's ``models``
    dict, so this only fires when the *same* id is used under different
    providers (e.g. once under ``anthropic.models`` and again under
    ``kling.models``). Model ids must be unique across the whole library so a
    node's ``model_usage`` can reference one by key alone; a cross-provider
    collision makes that reference ambiguous.

    Stackable.
    """

    library_name: str
    model_id: str
    # Provider ids where this model id appeared (e.g. "anthropic", "kling").
    provider_ids: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def collate_problems_for_display(cls, instances: list[DuplicateModelIdProblem]) -> str:
        if len(instances) == 1:
            problem = instances[0]
            providers = ", ".join(f"'{p}'" for p in problem.provider_ids)
            return (
                f"Library '{problem.library_name}' declares model id "
                f"'{problem.model_id}' under multiple providers: {providers}. "
                f"Model ids must be unique across the library."
            )

        by_library: dict[str, list[DuplicateModelIdProblem]] = defaultdict(list)
        for problem in instances:
            by_library[problem.library_name].append(problem)

        output_lines = [f"Encountered {len(instances)} duplicate model ids:"]
        for library_name in sorted(by_library.keys()):
            output_lines.append(f"  Library '{library_name}':")
            for problem in sorted(by_library[library_name], key=lambda p: p.model_id):
                providers = ", ".join(f"'{p}'" for p in problem.provider_ids)
                output_lines.append(f"    - id '{problem.model_id}' appears under: {providers}")
        return "\n".join(output_lines)
