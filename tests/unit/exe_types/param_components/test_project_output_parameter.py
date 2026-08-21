"""Unit tests for ProjectOutputParameter._get_upstream_destination."""

from unittest import mock

import pytest

from griptape_nodes.exe_types import core_types
from griptape_nodes.exe_types.param_components import project_output_parameter
from griptape_nodes.retained_mode.events import connection_events

HANDLE_REQUEST_PATH = "griptape_nodes.retained_mode.griptape_nodes.GriptapeNodes.handle_request"
OBJECT_MANAGER_PATH = "griptape_nodes.retained_mode.griptape_nodes.GriptapeNodes.ObjectManager"


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


def _make_param(param_name: str = "output") -> _ConcreteParam:
    mock_node = mock.MagicMock()
    mock_node.name = "MyNode"
    return _ConcreteParam(mock_node, param_name, default_value="default.txt", situation="save_node_output")


def _make_connections_result(
    *connections: connection_events.IncomingConnection,
) -> connection_events.ListConnectionsForNodeResultSuccess:
    return connection_events.ListConnectionsForNodeResultSuccess(
        result_details="ok",
        incoming_connections=list(connections),
        outgoing_connections=[],
    )


def _make_connection(
    target_param: str,
    source_node: str = "UpstreamNode",
    source_param: str = "test_destination",
) -> connection_events.IncomingConnection:
    return connection_events.IncomingConnection(
        source_node_name=source_node,
        source_parameter_name=source_param,
        target_parameter_name=target_param,
    )


class TestGetUpstreamDestination:
    """Tests for _get_upstream_destination, which finds an upstream provider via hasattr."""

    def test_returns_none_when_list_connections_fails(self) -> None:
        param = _make_param()
        failure = connection_events.ListConnectionsForNodeResultFailure(result_details="error")

        with mock.patch(HANDLE_REQUEST_PATH, return_value=failure):
            result = param._get_upstream_destination("test_destination", "TestDestination")

        assert result is None

    def test_returns_none_when_no_incoming_connections(self) -> None:
        param = _make_param()
        success = _make_connections_result()

        with mock.patch(HANDLE_REQUEST_PATH, return_value=success):
            result = param._get_upstream_destination("test_destination", "TestDestination")

        assert result is None

    def test_returns_none_when_connection_targets_different_parameter(self) -> None:
        param = _make_param("output")
        conn = _make_connection(target_param="other_param")
        success = _make_connections_result(conn)
        mock_source = mock.MagicMock(spec=[])  # no attributes

        with (
            mock.patch(HANDLE_REQUEST_PATH, return_value=success),
            mock.patch(OBJECT_MANAGER_PATH) as mock_om,
        ):
            mock_om.return_value.attempt_get_object_by_name.return_value = mock_source
            result = param._get_upstream_destination("test_destination", "TestDestination")

        assert result is None

    def test_returns_none_when_source_node_not_found(self) -> None:
        param = _make_param()
        conn = _make_connection(target_param="output")
        success = _make_connections_result(conn)

        with (
            mock.patch(HANDLE_REQUEST_PATH, return_value=success),
            mock.patch(OBJECT_MANAGER_PATH) as mock_om,
        ):
            mock_om.return_value.attempt_get_object_by_name.return_value = None
            result = param._get_upstream_destination("test_destination", "TestDestination")

        assert result is None

    def test_returns_none_when_source_node_lacks_attribute(self) -> None:
        param = _make_param()
        conn = _make_connection(target_param="output")
        success = _make_connections_result(conn)
        mock_source = mock.MagicMock(spec=[])  # no attributes at all

        with (
            mock.patch(HANDLE_REQUEST_PATH, return_value=success),
            mock.patch(OBJECT_MANAGER_PATH) as mock_om,
        ):
            mock_om.return_value.attempt_get_object_by_name.return_value = mock_source
            result = param._get_upstream_destination("test_destination", "TestDestination")

        assert result is None

    def test_returns_destination_when_source_has_attribute(self) -> None:
        param = _make_param()
        conn = _make_connection(target_param="output")
        success = _make_connections_result(conn)
        expected_dest = mock.MagicMock()
        mock_source = mock.MagicMock(spec=["test_destination"])
        mock_source.test_destination = expected_dest

        with (
            mock.patch(HANDLE_REQUEST_PATH, return_value=success),
            mock.patch(OBJECT_MANAGER_PATH) as mock_om,
        ):
            mock_om.return_value.attempt_get_object_by_name.return_value = mock_source
            result = param._get_upstream_destination("test_destination", "TestDestination")

        assert result is expected_dest

    def test_raises_when_provider_attribute_returns_none(self) -> None:
        param = _make_param()
        conn = _make_connection(target_param="output")
        success = _make_connections_result(conn)
        mock_source = mock.MagicMock(spec=["test_destination"])
        mock_source.test_destination = None

        with mock.patch(HANDLE_REQUEST_PATH, return_value=success), mock.patch(OBJECT_MANAGER_PATH) as mock_om:
            mock_om.return_value.attempt_get_object_by_name.return_value = mock_source
            with pytest.raises(ValueError, match="UpstreamNode"):
                param._get_upstream_destination("test_destination", "TestDestination")

    def test_skips_non_provider_and_returns_provider_destination(self) -> None:
        """A non-provider connection followed by a provider: skips first, returns second."""
        param = _make_param()
        conn_non_provider = _make_connection(target_param="output", source_node="PlainNode")
        conn_provider = _make_connection(target_param="output", source_node="ProviderNode")
        success = _make_connections_result(conn_non_provider, conn_provider)

        expected_dest = mock.MagicMock()
        plain_source = mock.MagicMock(spec=[])  # no test_destination
        provider_source = mock.MagicMock(spec=["test_destination"])
        provider_source.test_destination = expected_dest

        def get_node(name: str) -> mock.MagicMock:
            return plain_source if name == "PlainNode" else provider_source

        with (
            mock.patch(HANDLE_REQUEST_PATH, return_value=success),
            mock.patch(OBJECT_MANAGER_PATH) as mock_om,
        ):
            mock_om.return_value.attempt_get_object_by_name.side_effect = get_node
            result = param._get_upstream_destination("test_destination", "TestDestination")

        assert result is expected_dest

    def test_allowed_modes_default(self) -> None:
        param = _make_param()
        assert param._allowed_modes == {core_types.ParameterMode.INPUT, core_types.ParameterMode.PROPERTY}

    def test_custom_allowed_modes(self) -> None:
        mock_node = mock.MagicMock()
        mock_node.name = "N"
        param = _ConcreteParam(
            mock_node, "out", default_value="x", situation="s", allowed_modes={core_types.ParameterMode.OUTPUT}
        )
        assert param._allowed_modes == {core_types.ParameterMode.OUTPUT}
