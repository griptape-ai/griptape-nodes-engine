"""Tests for how `MCPManager` reports an MCP server config it cannot read.

The distinction under test is narrow but destructive to get wrong: callers use
the enabled-servers list to decide which servers to attach *and* which running
ones to shut down, so "I could not read the config" must not arrive looking like
"the user has no enabled servers".
"""

from __future__ import annotations

from typing import Any

import pytest

from griptape_nodes.retained_mode.events.mcp_events import (
    GetEnabledMCPServersRequest,
    GetEnabledMCPServersResultFailure,
    GetEnabledMCPServersResultSuccess,
    ListMCPServersRequest,
    ListMCPServersResultSuccess,
)
from griptape_nodes.retained_mode.managers.mcp_manager import MCPManager


class _StubConfigManager:
    """Returns a canned `mcp_servers` value, without touching config files."""

    def __init__(self, mcp_servers: Any) -> None:
        self._mcp_servers = mcp_servers

    def get_config_value(self, key: str, default: Any = None) -> Any:
        if key == "mcp_servers":
            return self._mcp_servers
        return default


def _manager(mcp_servers: Any) -> MCPManager:
    return MCPManager(config_manager=_StubConfigManager(mcp_servers))  # type: ignore[arg-type]


# A server entry that cannot be validated: `transport` is required and absent,
# so parsing the list raises rather than yielding a partial result.
_UNREADABLE = [{"nonsense": True}]


class TestGetEnabledMCPServers:
    def test_a_valid_config_lists_only_enabled_servers(self) -> None:
        manager = _manager(
            [
                {"name": "on", "transport": "stdio", "command": "run", "enabled": True},
                {"name": "off", "transport": "stdio", "command": "run", "enabled": False},
            ]
        )

        result = manager.on_get_enabled_mcp_servers_request(GetEnabledMCPServersRequest())

        assert isinstance(result, GetEnabledMCPServersResultSuccess)
        assert list(result.servers) == ["on"]

    def test_no_configured_servers_succeeds_with_an_empty_list(self) -> None:
        result = _manager([]).on_get_enabled_mcp_servers_request(GetEnabledMCPServersRequest())

        assert isinstance(result, GetEnabledMCPServersResultSuccess)
        assert result.servers == {}

    def test_an_unreadable_config_fails_rather_than_reporting_none_enabled(self) -> None:
        """The important case: a Failure, not a Success carrying `{}`.

        A Success with no servers is acted on as "shut everything down", which
        would kill every running server over one malformed entry.
        """
        result = _manager(_UNREADABLE).on_get_enabled_mcp_servers_request(GetEnabledMCPServersRequest())

        assert isinstance(result, GetEnabledMCPServersResultFailure)


class TestListMCPServers:
    def test_an_unreadable_config_still_lists_empty(self) -> None:
        """Listing keeps its old forgiving behaviour: an empty settings panel beats an error.

        Nothing is torn down on the strength of this answer, so degrading to
        "nothing to show" is safe here in a way it is not for the enabled list.
        """
        result = _manager(_UNREADABLE).on_list_mcp_servers_request(ListMCPServersRequest(include_disabled=True))

        assert isinstance(result, ListMCPServersResultSuccess)
        assert result.servers == {}


@pytest.mark.parametrize("servers", [None, []])
def test_a_missing_config_is_not_an_error(servers: Any) -> None:
    """An absent or empty `mcp_servers` setting is a fresh install, not a failure."""
    result = _manager(servers).on_get_enabled_mcp_servers_request(GetEnabledMCPServersRequest())

    assert isinstance(result, GetEnabledMCPServersResultSuccess)
    assert result.servers == {}
