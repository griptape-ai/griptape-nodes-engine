"""Unit tests for SubflowNodeGroup."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import create_autospec

from griptape_nodes.exe_types.node_groups.subflow_node_group import SubflowNodeGroup
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    import pytest


class TestSubflowNodeGroupCreateSubflow:
    """_create_subflow must persist the deduplicated flow name it actually created."""

    def test_records_deduplicated_flow_name_on_collision(
        self,
        griptape_nodes: GriptapeNodes,  # noqa: ARG002 - initialises the engine singleton for construction
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The group records the flow name it got back, not the (colliding) name it requested."""
        group = _MiniSubflowGroup(name="G")

        # Simulate the engine deduplicating the requested "G_subflow" (already taken) to "G_subflow_1".
        deduped_result = CreateFlowResultSuccess(flow_name="G_subflow_1", result_details="created")
        mock_handle = create_autospec(GriptapeNodes.handle_request, return_value=deduped_result)
        monkeypatch.setattr(GriptapeNodes, "handle_request", mock_handle)

        # _create_subflow reads the current flow only to parent the request; keep it off engine state.
        context_manager = GriptapeNodes.ContextManager()
        monkeypatch.setattr(
            context_manager,
            "get_current_flow",
            create_autospec(context_manager.get_current_flow, return_value=None),
        )

        group._create_subflow()

        # The request is derived from the group's own name...
        mock_handle.assert_called_once_with(
            CreateFlowRequest(
                flow_name="G_subflow",
                parent_flow_name=None,
                set_as_new_context=False,
                metadata={"flow_type": "NodeGroupFlow"},
            )
        )
        # ...but the group must record the flow it ACTUALLY got back, not the requested name.
        assert group.metadata["subflow_name"] == "G_subflow_1"


class _MiniSubflowGroup(SubflowNodeGroup):
    """Minimal concrete SubflowNodeGroup exercising only _create_subflow."""

    async def aprocess(self) -> None:  # pragma: no cover - execution not exercised here
        await self.execute_subflow()

    def process(self) -> Any:  # pragma: no cover - execution not exercised here
        return None
