"""Tests for VariablesManager.on_list_substitutables_request."""

from unittest.mock import patch

import pytest

from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.variable_events import (
    CreateVariableRequest,
    CreateVariableResultSuccess,
    ListSubstitutablesRequest,
    ListSubstitutablesResultFailure,
    ListSubstitutablesResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

_MACRO_PATCH = "griptape_nodes.retained_mode.managers.variable_manager.VariablesManager._get_project_macro_variables"


@pytest.fixture
def flow_name(griptape_nodes: GriptapeNodes) -> str:
    """Bootstrap a clean workflow + flow and return the flow name."""
    griptape_nodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    griptape_nodes.ContextManager().push_workflow("test_wf")
    result = griptape_nodes.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="test_flow", set_as_new_context=True)
    )
    assert isinstance(result, CreateFlowResultSuccess)
    return "test_flow"


def _add_variable(griptape_nodes: GriptapeNodes, name: str, value: object, type_: str = "str") -> None:
    result = griptape_nodes.handle_request(CreateVariableRequest(name=name, type=type_, value=value))
    assert isinstance(result, CreateVariableResultSuccess)


class TestListSubstitutablesRequest:
    def test_returns_user_vars_and_macros(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        _add_variable(griptape_nodes, "SHOT", "sc001")
        with patch(_MACRO_PATCH, return_value={"workspace_dir": "/workspace"}):
            result = griptape_nodes.handle_request(ListSubstitutablesRequest(starting_flow=flow_name))
        assert isinstance(result, ListSubstitutablesResultSuccess)
        assert {s.name for s in result.substitutables} == {"SHOT", "workspace_dir"}

    def test_macro_has_correct_metadata(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        with patch(_MACRO_PATCH, return_value={"workspace_dir": "/workspace"}):
            result = griptape_nodes.handle_request(ListSubstitutablesRequest(starting_flow=flow_name))
        assert isinstance(result, ListSubstitutablesResultSuccess)
        macro = next(s for s in result.substitutables if s.name == "workspace_dir")
        assert macro.source == "macro"
        assert macro.read_only is True
        assert macro.value == "/workspace"

    def test_user_var_has_correct_metadata(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        _add_variable(griptape_nodes, "SHOT", "sc001")
        with patch(_MACRO_PATCH, return_value={}):
            result = griptape_nodes.handle_request(ListSubstitutablesRequest(starting_flow=flow_name))
        assert isinstance(result, ListSubstitutablesResultSuccess)
        var = next(s for s in result.substitutables if s.name == "SHOT")
        assert var.source == "variable"
        assert var.read_only is False
        assert var.value == "sc001"

    def test_macro_wins_on_name_collision(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        """When a user var and a macro share a name, only the macro appears (one entry, macro value)."""
        _add_variable(griptape_nodes, "workspace_dir", "/my/override")
        with patch(_MACRO_PATCH, return_value={"workspace_dir": "/workspace"}):
            result = griptape_nodes.handle_request(ListSubstitutablesRequest(starting_flow=flow_name))
        assert isinstance(result, ListSubstitutablesResultSuccess)
        entries = [s for s in result.substitutables if s.name == "workspace_dir"]
        assert len(entries) == 1
        assert entries[0].source == "macro"
        assert entries[0].value == "/workspace"

    def test_filters_out_non_substitutable_user_vars(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        """User vars with non-str/int values are excluded, matching ResolveSubstitutionRequest behavior."""
        _add_variable(griptape_nodes, "SHOT", "sc001")
        _add_variable(griptape_nodes, "META", None)
        _add_variable(griptape_nodes, "TAGS", ["a", "b"])
        with patch(_MACRO_PATCH, return_value={}):
            result = griptape_nodes.handle_request(ListSubstitutablesRequest(starting_flow=flow_name))
        assert isinstance(result, ListSubstitutablesResultSuccess)
        names = {s.name for s in result.substitutables}
        assert "SHOT" in names
        assert "META" not in names
        assert "TAGS" not in names

    def test_returns_empty_when_no_vars_and_no_macros(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        with patch(_MACRO_PATCH, return_value={}):
            result = griptape_nodes.handle_request(ListSubstitutablesRequest(starting_flow=flow_name))
        assert isinstance(result, ListSubstitutablesResultSuccess)
        assert result.substitutables == []

    def test_returns_failure_for_unknown_flow(self, griptape_nodes: GriptapeNodes) -> None:
        with patch(_MACRO_PATCH, return_value={}):
            result = griptape_nodes.handle_request(ListSubstitutablesRequest(starting_flow="does_not_exist"))
        assert isinstance(result, ListSubstitutablesResultFailure)
