"""Unit tests for ProjectFileParameter.build_file reading a connected settings node."""

from typing import cast
from unittest import mock

import pytest

from griptape_nodes.common.macro_parser import ParsedMacro
from griptape_nodes.exe_types.param_components.project_file_parameter import ProjectFileParameter
from griptape_nodes.files.project_file import ProjectFileDestination
from griptape_nodes.retained_mode.events import destination_events
from griptape_nodes.retained_mode.events.os_events import ExistingFilePolicy
from griptape_nodes.retained_mode.events.project_events import MacroPath

_SPEC = destination_events.FileDestinationSpec(
    file_path=MacroPath(ParsedMacro("{outputs}/{name}.png"), {"name": "render"}),
    existing_file_policy=ExistingFilePolicy.FAIL,
    append=False,
    create_parents=False,
    coerce_extension_to_match_bytes=False,
    file_metadata=None,
    map_to_project=True,
)


def _make_param(result: object) -> ProjectFileParameter:
    node = mock.MagicMock()
    node.name = "Saver"
    node.engine.handle_request.return_value = result
    return ProjectFileParameter(node, "output_file", default_filename="default.png")


class TestBuildFileFromConnectedSettings:
    def test_asks_for_the_destination_connected_to_this_parameter(self) -> None:
        param = _make_param(
            destination_events.GetConnectedFileDestinationResultSuccess(destination=_SPEC, result_details="ok")
        )

        param.build_file()

        cast("mock.MagicMock", param._node).engine.handle_request.assert_called_once_with(
            destination_events.GetConnectedFileDestinationRequest(node_name="Saver", parameter_name="output_file")
        )

    def test_returns_the_settings_nodes_destination(self) -> None:
        param = _make_param(
            destination_events.GetConnectedFileDestinationResultSuccess(destination=_SPEC, result_details="ok")
        )

        destination = param.build_file()

        assert isinstance(destination, ProjectFileDestination)
        assert destination.to_spec() == _SPEC

    def test_a_failed_read_raises(self) -> None:
        param = _make_param(
            destination_events.GetConnectedFileDestinationResultFailure(
                result_details="'Settings' has no destination set"
            )
        )

        with pytest.raises(ValueError, match="has no destination set"):
            param.build_file()
