"""Deprecated-node problems name the node's replacement when the manifest gives one."""

from __future__ import annotations

from griptape_nodes.node_library.library_registry import NodeDeprecationReplacement
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import DeprecatedNodeWarningProblem
from griptape_nodes.retained_mode.managers.fitness_problems.workflows import DeprecatedNodeInWorkflowProblem

_REPLACEMENT = NodeDeprecationReplacement(library="Media Library", node_type="LoadImage")


def _library_problem(
    display_name: str, *, replacement: NodeDeprecationReplacement | None = None
) -> DeprecatedNodeWarningProblem:
    return DeprecatedNodeWarningProblem(
        display_name=display_name,
        class_name=display_name.replace(" ", ""),
        removal_version="2.0.0",
        deprecation_message=None,
        replacement=replacement,
    )


def _workflow_problem(
    display_name: str,
    *,
    replacement: NodeDeprecationReplacement | None = None,
    deprecation_message: str | None = None,
) -> DeprecatedNodeInWorkflowProblem:
    return DeprecatedNodeInWorkflowProblem(
        node_display_name=display_name,
        node_type=display_name.replace(" ", ""),
        library_name="Old Library",
        current_library_version="1.0.0",
        workflow_library_version="1.0.0",
        removal_version="2.0.0",
        deprecation_message=deprecation_message,
        replacement=replacement,
    )


class TestDeprecatedNodeWarningProblem:
    def test_single_problem_names_the_replacement(self) -> None:
        message = DeprecatedNodeWarningProblem.collate_problems_for_display(
            [_library_problem("Old Loader", replacement=_REPLACEMENT)]
        )

        assert message.endswith("Use 'LoadImage' from library 'Media Library' instead.")

    def test_grouped_problems_name_each_replacement(self) -> None:
        message = DeprecatedNodeWarningProblem.collate_problems_for_display(
            [_library_problem("Old Loader", replacement=_REPLACEMENT), _library_problem("Old Saver")]
        )

        assert "    - Old Loader (use 'LoadImage' from library 'Media Library')" in message.splitlines()
        assert "    - Old Saver" in message.splitlines()


class TestDeprecatedNodeInWorkflowProblem:
    def test_replacement_counts_as_remediation(self) -> None:
        message = DeprecatedNodeInWorkflowProblem.collate_problems_for_display(
            [_workflow_problem("Old Loader", replacement=_REPLACEMENT)]
        )

        assert "Use 'LoadImage' from library 'Media Library' instead." in message
        assert "No remediation" not in message

    def test_message_and_replacement_read_as_separate_sentences(self) -> None:
        message = DeprecatedNodeInWorkflowProblem.collate_problems_for_display(
            [_workflow_problem("Old Loader", replacement=_REPLACEMENT, deprecation_message="Superseded.")]
        )

        assert message.endswith(
            "(removed in v2.0.0). Superseded. Use 'LoadImage' from library 'Media Library' instead."
        )

    def test_without_message_or_replacement_says_there_is_no_remediation(self) -> None:
        message = DeprecatedNodeInWorkflowProblem.collate_problems_for_display([_workflow_problem("Old Loader")])

        assert message.endswith("No remediation steps provided by library author.")

    def test_grouped_problems_name_each_replacement(self) -> None:
        message = DeprecatedNodeInWorkflowProblem.collate_problems_for_display(
            [_workflow_problem("Old Loader", replacement=_REPLACEMENT), _workflow_problem("Old Saver")]
        )

        lines = message.splitlines()
        assert "    - Old Loader (use 'LoadImage' from library 'Media Library')" in lines
        assert "    - Old Saver (no remediation steps provided by library author)" in lines
