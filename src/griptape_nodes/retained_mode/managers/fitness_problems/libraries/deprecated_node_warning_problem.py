from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem

logger = logging.getLogger(__name__)


@dataclass
class DeprecatedNodeWarningProblem(LibraryProblem):
    """Problem indicating a library contains deprecated nodes.

    This is stackable - multiple nodes can be deprecated.
    """

    display_name: str
    class_name: str
    removal_version: str | None
    deprecation_message: str | None

    @classmethod
    def collate_problems_for_display(cls, instances: list[DeprecatedNodeWarningProblem]) -> str:
        """Display deprecated node warnings.

        Groups by removal version, then lists nodes within each group.
        """
        if len(instances) == 1:
            problem = instances[0]
            removal_info = (
                f"will be removed in version {problem.removal_version}"
                if problem.removal_version
                else "may be removed in future versions"
            )
            message = f"Node '{problem.display_name}' is deprecated and {removal_info}."
            if problem.deprecation_message:
                message += f" {problem.deprecation_message}"
            return message

        # Group by removal version
        by_version = defaultdict(list)
        for problem in instances:
            by_version[problem.removal_version].append(problem)

        # Sort versions (None comes last)
        sorted_versions = sorted(by_version.keys(), key=lambda v: (v is None, v or ""))

        output_lines = []
        output_lines.append(f"Encountered {len(instances)} deprecated nodes:")

        for version in sorted_versions:
            nodes = by_version[version]

            if version is None:
                output_lines.append("  Nodes that may be removed in future versions:")
            else:
                output_lines.append(f"  Nodes to be removed in version {version}:")

            # Group nodes within this version by deprecation_message
            by_message = defaultdict(list)
            for node in nodes:
                # Use empty string as key if no message
                message_key = node.deprecation_message or ""
                by_message[message_key].append(node)

            # Sort messages alphabetically (empty string first)
            sorted_messages = sorted(by_message.keys(), key=lambda m: (m != "", m))

            for message in sorted_messages:
                message_nodes = by_message[message]
                # Sort nodes by display_name within each message group
                message_nodes.sort(key=lambda p: p.display_name)

                if message:
                    output_lines.append(f"    {message}:")
                    output_lines.extend(f"      - {node.display_name}" for node in message_nodes)
                else:
                    # No deprecation message provided
                    output_lines.extend(f"    - {node.display_name}" for node in message_nodes)

        return "\n".join(output_lines)
