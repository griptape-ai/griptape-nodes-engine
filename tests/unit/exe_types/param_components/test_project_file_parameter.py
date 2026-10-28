"""Unit tests for ProjectFileParameter.build_file."""

import re
from pathlib import Path
from unittest import mock

import pytest

from griptape_nodes.exe_types.param_components import project_file_parameter


def _param(value: object) -> project_file_parameter.ProjectFileParameter:
    mock_node = mock.MagicMock()
    mock_node.name = "MyNode"
    mock_node.get_parameter_value.return_value = value
    return project_file_parameter.ProjectFileParameter(mock_node, "output_file", default_filename="image.png")


def _built_filename(value: object) -> str:
    """Run build_file with the given parameter value and return the filename it saves under."""
    param = _param(value)
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

    def test_keeps_absolute_path_as_typed(self, tmp_path: Path) -> None:
        """An absolute path is an exact location, so it is saved under the name typed."""
        typed = str(tmp_path / "shot")
        assert _built_filename(typed) == typed

    def test_keeps_file_uri_as_typed(self) -> None:
        assert _built_filename("file:///renders/shot") == "file:///renders/shot"

    def test_refused_url_is_quoted_as_entered(self) -> None:
        param = _param("https://example.com/shot")
        with (
            mock.patch.object(param, "_get_upstream_destination", return_value=None),
            pytest.raises(ValueError, match=re.escape("'https://example.com/shot'")),
        ):
            param.build_file()

    def test_refuses_folder_name(self) -> None:
        with pytest.raises(ValueError, match="names a folder"):
            _built_filename("renders/")
