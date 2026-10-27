"""Unit tests for ProjectFileParameter.build_file."""

from unittest import mock

from griptape_nodes.exe_types.param_components import project_file_parameter


def _built_filename(value: object) -> str:
    """Run build_file with the given parameter value and return the filename it saves under."""
    mock_node = mock.MagicMock()
    mock_node.name = "MyNode"
    mock_node.get_parameter_value.return_value = value
    param = project_file_parameter.ProjectFileParameter(mock_node, "output_file", default_filename="image.png")
    with (
        mock.patch.object(param, "_get_upstream_destination", return_value=None),
        mock.patch.object(project_file_parameter.ProjectFileDestination, "from_situation") as from_situation,
    ):
        param.build_file()
    return from_situation.call_args.args[0]


class TestBuildFile:
    def test_adds_default_extension_when_value_has_none(self) -> None:
        assert _built_filename("bedroom_flipped_grid_bw") == "bedroom_flipped_grid_bw.png"

    def test_keeps_value_extension(self) -> None:
        assert _built_filename("shot.jpg") == "shot.jpg"

    def test_uses_default_filename_when_value_empty(self) -> None:
        assert _built_filename("") == "image.png"
