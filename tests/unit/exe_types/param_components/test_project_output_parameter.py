"""Unit tests for ProjectOutputParameter._get_upstream_destination."""

from typing import Protocol, runtime_checkable
from unittest import mock

import pytest

from griptape_nodes.exe_types import core_types
from griptape_nodes.exe_types.param_components import project_output_parameter
from griptape_nodes.retained_mode.events import connection_events


@runtime_checkable
class _TestProvider(Protocol):
    @property
    def test_destination(self) -> object | None:
        """Stand-in destination the lookup under test reads from a provider node."""


class _ProviderNode:
    def __init__(self, destination: object | None) -> None:
        self._destination = destination

    @property
    def test_destination(self) -> object | None:
        return self._destination


class _PlainNode:
    pass


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


def _make_param(
    connections_result: connection_events.ListConnectionsForNodeResultSuccess
    | connection_events.ListConnectionsForNodeResultFailure,
    nodes_by_name: dict[str, object] | None = None,
    param_name: str = "output",
) -> _ConcreteParam:
    mock_node = mock.MagicMock()
    mock_node.name = "MyNode"
    mock_node.engine.handle_request.return_value = connections_result
    lookup = nodes_by_name or {}
    mock_node.engine.object_manager.attempt_get_object_by_name.side_effect = lookup.get
    return _ConcreteParam(mock_node, param_name, default_value="default.txt", situation="save_node_output")


def _get(param: _ConcreteParam) -> object | None:
    return param._get_upstream_destination(_TestProvider, lambda p: p.test_destination, "TestDestination")


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
    """Tests for _get_upstream_destination, which finds an upstream node implementing a provider protocol."""

    def test_returns_none_when_list_connections_fails(self) -> None:
        param = _make_param(connection_events.ListConnectionsForNodeResultFailure(result_details="error"))
        assert _get(param) is None

    def test_returns_none_when_no_incoming_connections(self) -> None:
        param = _make_param(_make_connections_result())
        assert _get(param) is None

    def test_returns_none_when_connection_targets_different_parameter(self) -> None:
        param = _make_param(
            _make_connections_result(_make_connection(target_param="other_param")),
            {"UpstreamNode": _ProviderNode(object())},
        )
        assert _get(param) is None

    def test_returns_none_when_source_node_not_found(self) -> None:
        param = _make_param(_make_connections_result(_make_connection(target_param="output")))
        assert _get(param) is None

    def test_returns_none_when_source_node_is_not_a_provider(self) -> None:
        param = _make_param(
            _make_connections_result(_make_connection(target_param="output")),
            {"UpstreamNode": _PlainNode()},
        )
        assert _get(param) is None

    def test_returns_destination_from_provider(self) -> None:
        expected_dest = object()
        param = _make_param(
            _make_connections_result(_make_connection(target_param="output")),
            {"UpstreamNode": _ProviderNode(expected_dest)},
        )
        assert _get(param) is expected_dest

    def test_raises_when_provider_returns_none(self) -> None:
        param = _make_param(
            _make_connections_result(_make_connection(target_param="output")),
            {"UpstreamNode": _ProviderNode(None)},
        )
        with pytest.raises(ValueError, match="UpstreamNode"):
            _get(param)

    def test_skips_non_provider_and_returns_provider_destination(self) -> None:
        """A non-provider connection followed by a provider: skips first, returns second."""
        expected_dest = object()
        param = _make_param(
            _make_connections_result(
                _make_connection(target_param="output", source_node="PlainNode"),
                _make_connection(target_param="output", source_node="ProviderNode"),
            ),
            {"PlainNode": _PlainNode(), "ProviderNode": _ProviderNode(expected_dest)},
        )
        assert _get(param) is expected_dest

    def test_allowed_modes_default(self) -> None:
        param = _make_param(_make_connections_result())
        assert param._allowed_modes == {core_types.ParameterMode.INPUT, core_types.ParameterMode.PROPERTY}

    def test_custom_allowed_modes(self) -> None:
        mock_node = mock.MagicMock()
        mock_node.name = "N"
        param = _ConcreteParam(
            mock_node, "out", default_value="x", situation="s", allowed_modes={core_types.ParameterMode.OUTPUT}
        )
        assert param._allowed_modes == {core_types.ParameterMode.OUTPUT}
