from __future__ import annotations

import logging
from dataclasses import dataclass

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem

logger = logging.getLogger(__name__)


@dataclass
class ModifiedParametersSetRemovedProblem(LibraryProblem):
    """Problem indicating a library is incompatible due to modified_parameters_set removal.

    This is stackable - multiple libraries can have this issue.
    This severity is UNUSABLE - the library cannot be loaded.
    """

    library_engine_version: str

    @classmethod
    def collate_problems_for_display(cls, instances: list[ModifiedParametersSetRemovedProblem]) -> str:
        """Display modified_parameters_set removal problems.

        Can handle multiple instances - they will be listed out sorted by library_engine_version.
        """
        if len(instances) == 1:
            version = instances[0].library_engine_version
            return (
                f"This library was not loaded because its griptape-nodes-library.json declares engine_version {version}, "
                "and Griptape Nodes 0.39.0 removed the 'modified_parameters_set' parameter from these BaseNode methods: "
                "'after_incoming_connection', 'after_outgoing_connection', 'after_incoming_connection_removed', "
                "'after_outgoing_connection_removed', 'before_value_set', and 'after_value_set'. "
                "To load it: if any of its nodes override one of those methods, remove the 'modified_parameters_set' "
                "parameter from that method; then set 'engine_version' under 'metadata' in griptape-nodes-library.json "
                "to the version of Griptape Nodes you are running. "
                "If you did not write this library, ask its author for a version built for 0.39.0 or later."
            )

        # Multiple libraries with this issue - list them sorted by version
        sorted_instances = sorted(instances, key=lambda p: p.library_engine_version)
        error_lines = []
        for i, problem in enumerate(sorted_instances, 1):
            error_lines.append(f"  {i}. Library declaring engine_version {problem.library_engine_version}")

        header = (
            f"{len(instances)} libraries were not loaded because they declare an engine_version older than 0.39.0, "
            "which removed the 'modified_parameters_set' parameter from the BaseNode connection and value-set methods:"
        )
        footer = (
            "For each one: remove the 'modified_parameters_set' parameter from any node method that still takes it, "
            "then set 'engine_version' under 'metadata' in that library's griptape-nodes-library.json to the version "
            "of Griptape Nodes you are running."
        )
        return header + "\n" + "\n".join(error_lines) + "\n" + footer
