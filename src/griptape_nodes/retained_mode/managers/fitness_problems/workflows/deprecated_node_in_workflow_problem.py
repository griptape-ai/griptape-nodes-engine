from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from griptape_nodes.retained_mode.managers.fitness_problems.workflows.workflow_problem import WorkflowProblem


@dataclass
class DeprecatedNodeInWorkflowProblem(WorkflowProblem):
    """Problem indicating a workflow uses a deprecated node.

    This is stackable - workflows can use multiple deprecated nodes.
    """

    node_display_name: str
    node_type: str
    library_name: str
    current_library_version: str
    workflow_library_version: str | None
    removal_version: str | None
    deprecation_message: str | None
    # How a user would name each replacement, e.g. "'Load Image'", or "'Load Image' from library 'Media'"
    # when it lives in another library.
    replacement_names: list[str] = field(default_factory=list)

    @classmethod
    def collate_problems_for_display(cls, instances: list[DeprecatedNodeInWorkflowProblem]) -> str:
        """Display deprecated node in workflow problems.

        Groups by library, then by deprecation message within each library.
        """
        if len(instances) == 1:
            return cls._single_problem_message(instances[0])

        # Group by library
        by_library = defaultdict(list)
        for problem in instances:
            by_library[problem.library_name].append(problem)

        # Sort libraries alphabetically
        sorted_libraries = sorted(by_library.keys())

        output_lines = []
        output_lines.append(f"Uses {len(instances)} deprecated nodes:")

        for library_name in sorted_libraries:
            nodes = by_library[library_name]
            output_lines.append(f"  From '{library_name}':")

            # Group nodes within this library by deprecation_message
            by_message = defaultdict(list)
            for node in nodes:
                message_key = node.deprecation_message or ""
                by_message[message_key].append(node)

            # Sort messages alphabetically (empty string first)
            sorted_messages = sorted(by_message.keys(), key=lambda m: (m != "", m))

            for message in sorted_messages:
                message_nodes = by_message[message]
                # Sort nodes by display_name within each message group
                message_nodes.sort(key=lambda p: p.node_display_name)

                if message:
                    output_lines.append(f"    {message}:")
                    output_lines.extend(f"      - {cls._node_line(node)}" for node in message_nodes)
                else:
                    output_lines.extend(f"    - {cls._node_line(node)}" for node in message_nodes)

        return "\n".join(output_lines)

    @staticmethod
    def _single_problem_message(problem: DeprecatedNodeInWorkflowProblem) -> str:
        removal_info = (
            f"removed in v{problem.removal_version}" if problem.removal_version else "may be removed in future"
        )
        message = f"Uses deprecated node '{problem.node_display_name}' from '{problem.library_name}' ({removal_info})"
        sentences = [f"{message}."]
        if problem.deprecation_message:
            deprecation_message = problem.deprecation_message.strip()
            if not deprecation_message.endswith((".", "!", "?")):
                deprecation_message += "."
            sentences.append(deprecation_message)
        if problem.replacement_names:
            sentences.append(f"Use {' or '.join(problem.replacement_names)} instead.")
        if not problem.deprecation_message and not problem.replacement_names:
            sentences.append("No remediation steps provided by library author.")
        return " ".join(sentences)

    @staticmethod
    def _node_line(problem: DeprecatedNodeInWorkflowProblem) -> str:
        if problem.replacement_names:
            return f"{problem.node_display_name} (use {' or '.join(problem.replacement_names)})"
        if problem.deprecation_message:
            return problem.node_display_name
        return f"{problem.node_display_name} (no remediation steps provided by library author)"
