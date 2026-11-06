import pytest

from griptape_nodes.retained_mode.retained_mode import node_param_split


class TestNodeParamSplit:
    def test_simple_node_and_param(self) -> None:
        node, param = node_param_split("my_node.my_param")
        assert node == "my_node"
        assert param == "my_param"

    def test_node_name_contains_dot(self) -> None:
        # Regression: https://github.com/griptape-ai/griptape-nodes-engine/issues/4545
        # Node names like "FLUX.2 Image Generation" must split on the final '.' only.
        node, param = node_param_split("FLUX.2 Image Generation.file_destination")
        assert node == "FLUX.2 Image Generation"
        assert param == "file_destination"

    def test_node_name_with_multiple_dots(self) -> None:
        node, param = node_param_split("a.b.c.d.param")
        assert node == "a.b.c.d"
        assert param == "param"

    def test_missing_dot_raises(self) -> None:
        with pytest.raises(ValueError, match=r"Expected format 'node\.param'"):
            node_param_split("no_dot_here")
