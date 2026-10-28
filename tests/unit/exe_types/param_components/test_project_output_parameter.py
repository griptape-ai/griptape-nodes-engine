"""Unit tests for ProjectOutputParameter._get_upstream_destination."""

from typing import cast
from unittest import mock

import pytest

from griptape_nodes.exe_types import core_types
from griptape_nodes.exe_types.param_components import project_output_parameter
from griptape_nodes.retained_mode.events import destination_events
from griptape_nodes.retained_mode.events.os_events import ExistingFilePolicy

_SPEC = destination_events.DirectoryDestinationSpec(
    dir_path="/renders/out",
    existing_dir_policy=ExistingFilePolicy.FAIL,
    create_parents=False,
)


class _ConcreteParam(project_output_parameter.ProjectOutputParameter):
    """Minimal concrete subclass for testing the base class."""

    @property
    def _settings_node_type(self) -> str:
        return "TestSettings"

    @property
    def _settings_value_param_name(self) -> str:
        return "value"

    @property
    def _settings_source_param_name(self) -> str:
        return "test_destination"

    @property
    def _parameter_output_type(self) -> str:
        return "str"


def _make_param(result: object, param_name: str = "output") -> _ConcreteParam:
    mock_node = mock.MagicMock()
    mock_node.name = "MyNode"
    mock_node.engine.handle_request.return_value = result
    return _ConcreteParam(mock_node, param_name, default_value="default.txt", situation="save_node_output")


def _mock_node(param: _ConcreteParam) -> mock.MagicMock:
    return cast("mock.MagicMock", param._node)


def _request(param: _ConcreteParam) -> destination_events.GetConnectedDirectoryDestinationRequest:
    return destination_events.GetConnectedDirectoryDestinationRequest(
        node_name=param._node.name, parameter_name=param._name
    )


def _get(param: _ConcreteParam) -> object | None:
    return param._get_upstream_destination(
        _request(param),
        destination_events.GetConnectedDirectoryDestinationResultSuccess,
        lambda spec: ("rebuilt", spec),
    )


class TestGetUpstreamDestination:
    """Tests for _get_upstream_destination, which asks for the destination a connected settings node supplies."""

    def test_sends_the_request_through_the_nodes_engine(self) -> None:
        param = _make_param(
            destination_events.GetConnectedDirectoryDestinationResultSuccess(destination=None, result_details="ok")
        )

        _get(param)

        _mock_node(param).engine.handle_request.assert_called_once_with(_request(param))

    def test_returns_none_when_no_settings_node_is_connected(self) -> None:
        param = _make_param(
            destination_events.GetConnectedDirectoryDestinationResultSuccess(destination=None, result_details="ok")
        )

        assert _get(param) is None

    def test_rebuilds_the_destination_from_its_spec(self) -> None:
        param = _make_param(
            destination_events.GetConnectedDirectoryDestinationResultSuccess(destination=_SPEC, result_details="ok")
        )

        assert _get(param) == ("rebuilt", _SPEC)

    def test_raises_with_the_failure_details(self) -> None:
        param = _make_param(
            destination_events.GetConnectedDirectoryDestinationResultFailure(
                result_details="'UpstreamNode' has no destination set"
            )
        )

        with pytest.raises(ValueError, match="UpstreamNode"):
            _get(param)

    def test_never_reads_the_local_object_store(self) -> None:
        param = _make_param(
            destination_events.GetConnectedDirectoryDestinationResultSuccess(destination=_SPEC, result_details="ok")
        )

        _get(param)

        _mock_node(param).engine.object_manager.attempt_get_object_by_name.assert_not_called()

    def test_allowed_modes_default(self) -> None:
        param = _make_param(None)
        assert param._allowed_modes == {core_types.ParameterMode.INPUT, core_types.ParameterMode.PROPERTY}

    def test_custom_allowed_modes(self) -> None:
        mock_node = mock.MagicMock()
        mock_node.name = "N"
        param = _ConcreteParam(
            mock_node, "out", default_value="x", situation="s", allowed_modes={core_types.ParameterMode.OUTPUT}
        )
        assert param._allowed_modes == {core_types.ParameterMode.OUTPUT}
