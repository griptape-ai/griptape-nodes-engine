from griptape_nodes.retained_mode.managers.fitness_problems.libraries import (
    NodePermissionDeniedProblem,
    PermissionDeniedProblem,
)


class TestNodePermissionDeniedProblemDisplay:
    """Each denied node type lists its reasons as nested bullets, never joined into one run-on line."""

    def test_reasons_are_nested_bullets_under_the_node_type(self) -> None:
        problem = NodePermissionDeniedProblem(
            node_type="InvertImage",
            messages=["Inverting images is not allowed.", "Labs nodes are disabled."],
        )

        text = NodePermissionDeniedProblem.collate_problems_for_display([problem])

        assert text == (
            "Some nodes are not permitted by your license:\n"
            "- InvertImage:\n"
            "  - Inverting images is not allowed.\n"
            "  - Labs nodes are disabled."
        )
        assert ".;" not in text

    def test_repeated_reasons_and_repeated_entries_collapse(self) -> None:
        first = NodePermissionDeniedProblem(node_type="A", messages=["Denied.", "Denied."])
        duplicate = NodePermissionDeniedProblem(node_type="A", messages=["Denied."])
        other = NodePermissionDeniedProblem(node_type="B", messages=["Denied."])

        text = NodePermissionDeniedProblem.collate_problems_for_display([first, duplicate, other])

        assert text == ("Some nodes are not permitted by your license:\n- A:\n  - Denied.\n- B:\n  - Denied.")

    def test_a_node_without_messages_gets_the_default_reason(self) -> None:
        problem = NodePermissionDeniedProblem(node_type="A")

        assert "  - Denied by the license policy." in NodePermissionDeniedProblem.collate_problems_for_display(
            [problem]
        )

    def test_no_instances_gives_the_bare_headline(self) -> None:
        assert (
            NodePermissionDeniedProblem.collate_problems_for_display([])
            == "Some nodes are not permitted by your license."
        )


class TestPermissionDeniedProblemDisplay:
    def test_each_distinct_message_is_one_bullet(self) -> None:
        first = PermissionDeniedProblem(library_name="lib", messages=["One.", "Two."])
        second = PermissionDeniedProblem(library_name="lib", messages=["Two.", "Three."])

        text = PermissionDeniedProblem.collate_problems_for_display([first, second])

        assert text == "This library is not permitted by your license:\n- One.\n- Two.\n- Three."
