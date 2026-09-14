"""MCP (Model Context Protocol) server management.

Handles MCP server configurations, enabling/disabling servers, and provides
event-based interface for frontend and backend interactions.
"""

import logging
from typing import Any

from griptape_nodes.retained_mode.events.mcp_events import (
    CreateMCPServerRequest,
    CreateMCPServerResultFailure,
    CreateMCPServerResultSuccess,
    DeleteMCPServerRequest,
    DeleteMCPServerResultFailure,
    DeleteMCPServerResultSuccess,
    DisableMCPServerRequest,
    DisableMCPServerResultFailure,
    DisableMCPServerResultSuccess,
    EnableMCPServerRequest,
    EnableMCPServerResultFailure,
    EnableMCPServerResultSuccess,
    GetEnabledMCPServersRequest,
    GetEnabledMCPServersResultFailure,
    GetEnabledMCPServersResultSuccess,
    GetMCPServerRequest,
    GetMCPServerResultFailure,
    GetMCPServerResultSuccess,
    ListMCPServersRequest,
    ListMCPServersResultFailure,
    ListMCPServersResultSuccess,
    UpdateMCPServerRequest,
    UpdateMCPServerResultFailure,
    UpdateMCPServerResultSuccess,
)
from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.managers.settings import MCPServerConfig

logger = logging.getLogger("griptape_nodes")


class MCPManager:
    """Manager for MCP server configurations and operations."""

    def __init__(self, event_manager: EventManager | None = None, config_manager: ConfigManager | None = None) -> None:
        """Initialize the MCPManager.

        Args:
            event_manager: The EventManager instance to use for event handling.
            config_manager: The ConfigManager instance to use for configuration management.
        """
        self.config_manager = config_manager
        if event_manager is not None:
            # Register event handlers
            event_manager.assign_manager_to_request_type(ListMCPServersRequest, self.on_list_mcp_servers_request)
            event_manager.assign_manager_to_request_type(GetMCPServerRequest, self.on_get_mcp_server_request)
            event_manager.assign_manager_to_request_type(CreateMCPServerRequest, self.on_create_mcp_server_request)
            event_manager.assign_manager_to_request_type(UpdateMCPServerRequest, self.on_update_mcp_server_request)
            event_manager.assign_manager_to_request_type(DeleteMCPServerRequest, self.on_delete_mcp_server_request)
            event_manager.assign_manager_to_request_type(EnableMCPServerRequest, self.on_enable_mcp_server_request)
            event_manager.assign_manager_to_request_type(DisableMCPServerRequest, self.on_disable_mcp_server_request)
            event_manager.assign_manager_to_request_type(
                GetEnabledMCPServersRequest, self.on_get_enabled_mcp_servers_request
            )

    def _get_mcp_servers(self, filter_by: dict[str, Any] | None = None) -> list[MCPServerConfig]:
        """Get the current MCP servers configuration, treating an unreadable config as empty.

        Args:
            filter_by: Optional dict of field=value pairs to filter by.
                      Keys should match server config field names, values are the expected values.
        """
        try:
            return self._read_mcp_servers(filter_by=filter_by)
        except Exception as e:
            logger.error("Failed to parse MCP servers configuration: %s", e)
            return []

    def _read_mcp_servers(self, filter_by: dict[str, Any] | None = None) -> list[MCPServerConfig]:
        """Read and validate the MCP servers configuration, raising if it cannot be parsed.

        Prefer this over :meth:`_get_mcp_servers` wherever an empty list would be
        acted on as "the user has no servers". A caller that cannot tell those
        apart will happily shut running servers down because a single malformed
        entry made the whole list unreadable.

        Args:
            filter_by: Optional dict of field=value pairs to filter by.
                      Keys should match server config field names, values are the expected values.
        """
        if self.config_manager is None:
            return []

        mcp_config_data = self.config_manager.get_config_value("mcp_servers", default=[])
        if not mcp_config_data:
            return []

        servers = [MCPServerConfig.model_validate(server) for server in mcp_config_data]
        if not filter_by:
            return servers

        filtered_servers = []
        for server in servers:
            match = True
            for field, value in filter_by.items():
                if getattr(server, field, None) != value:
                    match = False
                    break
            if match:
                filtered_servers.append(server)
        return filtered_servers

    def _save_mcp_servers(self, servers: list[MCPServerConfig]) -> None:
        """Save the MCP servers configuration to the config manager."""
        if self.config_manager is None:
            logger.warning("No config manager available, cannot save MCP configuration")
            return

        try:
            self.config_manager.set_config_value("mcp_servers", [server.model_dump() for server in servers])
        except Exception as e:
            logger.error("Failed to save MCP servers configuration: %s", e)

    def _update_server_fields(self, server_config: MCPServerConfig, request: UpdateMCPServerRequest) -> None:
        """Update server configuration fields from request."""
        # Map request fields to server config attributes
        field_mapping = {
            "new_name": "name",
            "transport": "transport",
            "enabled": "enabled",
            "command": "command",
            "args": "args",
            "env": "env",
            "cwd": "cwd",
            "encoding": "encoding",
            "encoding_error_handler": "encoding_error_handler",
            "url": "url",
            "headers": "headers",
            "timeout": "timeout",
            "sse_read_timeout": "sse_read_timeout",
            "terminate_on_close": "terminate_on_close",
            "description": "description",
            "capabilities": "capabilities",
            "rules": "rules",
        }

        # Update fields that are not None
        for request_field, config_field in field_mapping.items():
            value = getattr(request, request_field, None)
            if value is not None:
                setattr(server_config, config_field, value)

    def on_list_mcp_servers_request(
        self, request: ListMCPServersRequest
    ) -> ListMCPServersResultSuccess | ListMCPServersResultFailure:
        """Handle list MCP servers request."""
        try:
            servers = self._get_mcp_servers()
        except Exception as e:
            logger.error("Failed to list MCP servers: %s", e)
            return ListMCPServersResultFailure(result_details=f"Failed to list MCP servers: {e}")

        if request.include_disabled:
            servers_dict = {server.name: server.model_dump() for server in servers}
        else:
            enabled_servers = [server for server in servers if server.enabled]
            servers_dict = {server.name: server.model_dump() for server in enabled_servers}

        # Success path after exception handling
        return ListMCPServersResultSuccess(
            # Pydantic model.model_dump() returns dict[str, Any] which matches MCPServerConfig TypedDict structure
            servers=servers_dict,  # type: ignore[arg-type]
            result_details=f"Successfully listed {len(servers_dict)} MCP servers",
        )

    def on_get_mcp_server_request(
        self, request: GetMCPServerRequest
    ) -> GetMCPServerResultSuccess | GetMCPServerResultFailure:
        """Handle get MCP server request."""
        servers = self._get_mcp_servers(filter_by={"name": request.name})
        server_config = servers[0] if servers else None

        if server_config is None:
            return GetMCPServerResultFailure(result_details=f"Failed to get MCP server '{request.name}' - not found")

        # Success path after exception handling
        return GetMCPServerResultSuccess(
            # Pydantic model.model_dump() returns dict[str, Any] which matches MCPServerConfig TypedDict structure
            server_config=server_config.model_dump(),  # type: ignore[arg-type]
            result_details=f"Successfully retrieved MCP server '{request.name}'",
        )

    def on_create_mcp_server_request(
        self, request: CreateMCPServerRequest
    ) -> CreateMCPServerResultSuccess | CreateMCPServerResultFailure:
        """Handle create MCP server request."""
        try:
            servers = self._get_mcp_servers()
        except Exception as e:
            logger.error("Failed to create MCP server '%s': %s", request.name, e)
            return CreateMCPServerResultFailure(result_details=f"Failed to create MCP server '{request.name}': {e}")

        # Check if server already exists
        for server in servers:
            if server.name == request.name:
                return CreateMCPServerResultFailure(
                    result_details=f"Failed to create MCP server '{request.name}' - already exists"
                )

        # Create new server configuration
        server_config = MCPServerConfig(
            name=request.name,
            enabled=request.enabled,
            transport=request.transport,
            # StdioConnection fields
            command=request.command,
            args=request.args or [],
            env=request.env or {},
            cwd=request.cwd,
            encoding=request.encoding,
            encoding_error_handler=request.encoding_error_handler,
            # HTTP-based connection fields
            url=request.url,
            headers=request.headers,
            timeout=request.timeout,
            sse_read_timeout=request.sse_read_timeout,
            terminate_on_close=request.terminate_on_close,
            # Common fields
            description=request.description,
            capabilities=request.capabilities or [],
            rules=request.rules,
        )

        servers.append(server_config)

        try:
            self._save_mcp_servers(servers)
        except Exception as e:
            logger.error("Failed to save MCP server '%s': %s", request.name, e)
            return CreateMCPServerResultFailure(result_details=f"Failed to save MCP server '{request.name}': {e}")

        # Success path after exception handling
        return CreateMCPServerResultSuccess(
            name=request.name, result_details=f"Successfully created MCP server '{request.name}'"
        )

    def on_update_mcp_server_request(
        self, request: UpdateMCPServerRequest
    ) -> UpdateMCPServerResultSuccess | UpdateMCPServerResultFailure:
        """Handle update MCP server request."""
        servers = self._get_mcp_servers()

        # Find the server to update
        server_index = next((i for i, server in enumerate(servers) if server.name == request.name), None)

        if server_index is None:
            return UpdateMCPServerResultFailure(
                result_details=f"Failed to update MCP server '{request.name}' - not found"
            )

        # Create a backup of the original server and update a copy
        original_server = servers[server_index]
        updated_server = original_server.model_copy()
        self._update_server_fields(updated_server, request)

        # Create a copy of the servers list with the updated server
        updated_servers = servers.copy()
        updated_servers[server_index] = updated_server

        try:
            self._save_mcp_servers(updated_servers)
        except Exception as e:
            logger.error("Failed to save MCP server '%s': %s", request.name, e)
            return UpdateMCPServerResultFailure(result_details=f"Failed to save MCP server '{request.name}': {e}")

        # Success path after exception handling
        return UpdateMCPServerResultSuccess(
            name=request.name, result_details=f"Successfully updated MCP server '{request.name}'"
        )

    def on_delete_mcp_server_request(
        self, request: DeleteMCPServerRequest
    ) -> DeleteMCPServerResultSuccess | DeleteMCPServerResultFailure:
        """Handle delete MCP server request."""
        try:
            servers = self._get_mcp_servers()

            # Find and remove server by server_id
            server_found = False
            for i, server in enumerate(servers):
                if server.name == request.name:
                    servers.pop(i)
                    server_found = True
                    break

            self._save_mcp_servers(servers)

        except Exception as e:
            logger.error("Failed to delete MCP server '%s': %s", request.name, e)
            return DeleteMCPServerResultFailure(result_details=f"Failed to delete MCP server '{request.name}': {e}")

        if not server_found:
            return DeleteMCPServerResultFailure(
                result_details=f"Failed to delete MCP server '{request.name}' - not found"
            )

        # Success path after exception handling
        return DeleteMCPServerResultSuccess(
            name=request.name, result_details=f"Successfully deleted MCP server '{request.name}'"
        )

    def on_enable_mcp_server_request(
        self, request: EnableMCPServerRequest
    ) -> EnableMCPServerResultSuccess | EnableMCPServerResultFailure:
        """Handle enable MCP server request."""
        servers = self._get_mcp_servers()
        server_config = None
        for server in servers:
            if server.name == request.name:
                server_config = server
                break

        if server_config is None:
            return EnableMCPServerResultFailure(
                result_details=f"Failed to enable MCP server '{request.name}' - not found"
            )

        server_config.enabled = True

        try:
            self._save_mcp_servers(servers)
        except Exception as e:
            logger.error("Failed to save MCP server '%s': %s", request.name, e)
            return EnableMCPServerResultFailure(result_details=f"Failed to save MCP server '{request.name}': {e}")

        # Success path after exception handling
        return EnableMCPServerResultSuccess(
            name=request.name, result_details=f"Successfully enabled MCP server '{request.name}'"
        )

    def on_disable_mcp_server_request(
        self, request: DisableMCPServerRequest
    ) -> DisableMCPServerResultSuccess | DisableMCPServerResultFailure:
        """Handle disable MCP server request."""
        servers = self._get_mcp_servers()
        server_config = None
        for server in servers:
            if server.name == request.name:
                server_config = server
                break

        if server_config is None:
            return DisableMCPServerResultFailure(
                result_details=f"Failed to disable MCP server '{request.name}' - not found"
            )

        server_config.enabled = False

        try:
            self._save_mcp_servers(servers)
        except Exception as e:
            logger.error("Failed to save MCP server '%s': %s", request.name, e)
            return DisableMCPServerResultFailure(result_details=f"Failed to save MCP server '{request.name}': {e}")

        # Success path after exception handling
        return DisableMCPServerResultSuccess(
            name=request.name, result_details=f"Successfully disabled MCP server '{request.name}'"
        )

    def on_get_enabled_mcp_servers_request(
        self,
        request: GetEnabledMCPServersRequest,  # noqa: ARG002
    ) -> GetEnabledMCPServersResultSuccess | GetEnabledMCPServersResultFailure:
        """Handle get enabled MCP servers request.

        Reads through `_read_mcp_servers` rather than `_get_mcp_servers` so an
        unreadable config fails the request instead of answering "no servers are
        enabled". Callers use this to decide which servers to attach *and* which
        to shut down, so the difference is the difference between a warning and
        tearing down every running server over one bad config entry.
        """
        try:
            enabled_servers = self._read_mcp_servers(filter_by={"enabled": True})
            servers_dict = {server.name: server.model_dump() for server in enabled_servers}

        except Exception as e:
            logger.error("Failed to get enabled MCP servers: %s", e)
            return GetEnabledMCPServersResultFailure(result_details=f"Failed to get enabled MCP servers: {e}")

        # Success path after exception handling
        return GetEnabledMCPServersResultSuccess(
            # Pydantic model.model_dump() returns dict[str, Any] which matches MCPServerConfig TypedDict structure
            servers=servers_dict,  # type: ignore[arg-type]
            result_details=f"Successfully retrieved {len(servers_dict)} enabled MCP servers",
        )
