"""A deprecated node in a workflow names its replacements when the manifest gives them."""

from __future__ import annotations

from griptape_nodes.retained_mode.managers.fitness_problems.workflows import DeprecatedNodeInWorkflowProblem

_REPLACEMENT = "'Load Image' from library 'Media Library'"


def _workflow_problem(
    display_name: str,
    *,
    replacement_names: list[str] | None = None,
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
        replacement_names=replacement_names or [],
    )


class TestDeprecatedNodeInWorkflowProblem:
    def test_replacement_counts_as_remediation(self) -> None:
        message = DeprecatedNodeInWorkflowProblem.collate_problems_for_display(
            [_workflow_problem("Old Loader", replacement_names=[_REPLACEMENT])]
        )

        assert "Use 'Load Image' from library 'Media Library' instead." in message
        assert "No remediation" not in message

    def test_message_and_replacement_read_as_separate_sentences(self) -> None:
        message = DeprecatedNodeInWorkflowProblem.collate_problems_for_display(
            [_workflow_problem("Old Loader", replacement_names=[_REPLACEMENT], deprecation_message="Superseded.")]
        )

        assert message.endswith(
            "(removed in v2.0.0). Superseded. Use 'Load Image' from library 'Media Library' instead."
        )

    def test_deprecation_message_keeps_its_own_punctuation(self) -> None:
        message = DeprecatedNodeInWorkflowProblem.collate_problems_for_display(
            [_workflow_problem("Old Loader", deprecation_message="Need help? Ask in Discord!")]
        )

        assert message.endswith("(removed in v2.0.0). Need help? Ask in Discord!")

    def test_several_replacements_are_offered_as_alternatives(self) -> None:
        message = DeprecatedNodeInWorkflowProblem.collate_problems_for_display(
            [_workflow_problem("Old Loader", replacement_names=["'Loader 2.0'", "'Loader 2.5'"])]
        )

        assert message.endswith("Use 'Loader 2.0' or 'Loader 2.5' instead.")

    def test_without_message_or_replacement_says_there_is_no_remediation(self) -> None:
        message = DeprecatedNodeInWorkflowProblem.collate_problems_for_display([_workflow_problem("Old Loader")])

        assert message.endswith("No remediation steps provided by library author.")

    def test_grouped_problems_name_each_replacement(self) -> None:
        message = DeprecatedNodeInWorkflowProblem.collate_problems_for_display(
            [_workflow_problem("Old Loader", replacement_names=[_REPLACEMENT]), _workflow_problem("Old Saver")]
        )

        lines = message.splitlines()
        assert "    - Old Loader (use 'Load Image' from library 'Media Library')" in lines
        assert "    - Old Saver (no remediation steps provided by library author)" in lines
