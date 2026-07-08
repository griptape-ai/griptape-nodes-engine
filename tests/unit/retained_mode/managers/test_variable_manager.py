"""Tests for VariablesManager request handlers."""

from unittest.mock import patch

import pytest

from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.variable_events import (
    CreateVariableRequest,
    CreateVariableResultSuccess,
    GetVariablesRequest,
    GetVariablesResultFailure,
    GetVariablesResultSuccess,
    ListSubstitutablesRequest,
    ListSubstitutablesResultFailure,
    ListSubstitutablesResultSuccess,
    ResolveSubstitutionRequest,
    ResolveSubstitutionResultFailure,
    ResolveSubstitutionResultSuccess,
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


class TestGetVariablesRequest:
    """GetVariablesRequest returns user-defined variables only — no project macros."""

    def test_returns_user_vars(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        _add_variable(griptape_nodes, "SHOT", "sc001")
        result = griptape_nodes.handle_request(GetVariablesRequest(starting_flow=flow_name))
        assert isinstance(result, GetVariablesResultSuccess)
        assert result.variables == {"SHOT": "sc001"}

    def test_does_not_include_project_macros(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        with patch(_MACRO_PATCH, return_value={"workspace_dir": "/workspace"}):
            result = griptape_nodes.handle_request(GetVariablesRequest(starting_flow=flow_name))
        assert isinstance(result, GetVariablesResultSuccess)
        assert "workspace_dir" not in result.variables

    def test_named_lookup_returns_requested_vars(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        _add_variable(griptape_nodes, "SHOT", "sc001")
        _add_variable(griptape_nodes, "SHOW", "myshow")
        result = griptape_nodes.handle_request(GetVariablesRequest(starting_flow=flow_name, names=["SHOT"]))
        assert isinstance(result, GetVariablesResultSuccess)
        assert result.variables == {"SHOT": "sc001"}

    def test_named_lookup_fails_if_any_name_missing(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        _add_variable(griptape_nodes, "SHOT", "sc001")
        result = griptape_nodes.handle_request(GetVariablesRequest(starting_flow=flow_name, names=["SHOT", "MISSING"]))
        assert isinstance(result, GetVariablesResultFailure)

    def test_returns_empty_when_no_vars(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        result = griptape_nodes.handle_request(GetVariablesRequest(starting_flow=flow_name))
        assert isinstance(result, GetVariablesResultSuccess)
        assert result.variables == {}

    def test_returns_failure_for_unknown_flow(self, griptape_nodes: GriptapeNodes) -> None:
        result = griptape_nodes.handle_request(GetVariablesRequest(starting_flow="does_not_exist"))
        assert isinstance(result, GetVariablesResultFailure)


class TestResolveSubstitutionRequest:
    """ResolveSubstitutionRequest merges project macros with user vars; user vars win on collision."""

    def test_returns_user_vars_and_macros(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        _add_variable(griptape_nodes, "SHOT", "sc001")
        with patch(_MACRO_PATCH, return_value={"workspace_dir": "/workspace"}):
            result = griptape_nodes.handle_request(ResolveSubstitutionRequest(starting_flow=flow_name))
        assert isinstance(result, ResolveSubstitutionResultSuccess)
        assert result.variables["SHOT"] == "sc001"
        assert result.variables["workspace_dir"] == "/workspace"

    def test_user_var_wins_on_name_collision(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        _add_variable(griptape_nodes, "workspace_dir", "/my/override")
        with patch(_MACRO_PATCH, return_value={"workspace_dir": "/workspace"}):
            result = griptape_nodes.handle_request(ResolveSubstitutionRequest(starting_flow=flow_name))
        assert isinstance(result, ResolveSubstitutionResultSuccess)
        assert result.variables["workspace_dir"] == "/my/override"

    def test_named_lookup_finds_macro(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        with patch(_MACRO_PATCH, return_value={"workspace_dir": "/workspace"}):
            result = griptape_nodes.handle_request(
                ResolveSubstitutionRequest(starting_flow=flow_name, names=["workspace_dir"])
            )
        assert isinstance(result, ResolveSubstitutionResultSuccess)
        assert result.variables == {"workspace_dir": "/workspace"}

    def test_named_lookup_fails_if_not_in_vars_or_macros(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        _add_variable(griptape_nodes, "SHOT", "sc001")
        with patch(_MACRO_PATCH, return_value={"workspace_dir": "/workspace"}):
            result = griptape_nodes.handle_request(
                ResolveSubstitutionRequest(starting_flow=flow_name, names=["SHOT", "workspace_dir", "MISSING"])
            )
        assert isinstance(result, ResolveSubstitutionResultFailure)
        assert result.unresolved == ["MISSING"]
        assert result.resolved == {"SHOT": "sc001", "workspace_dir": "/workspace"}

    def test_returns_failure_for_unknown_flow(self, griptape_nodes: GriptapeNodes) -> None:
        with patch(_MACRO_PATCH, return_value={}):
            result = griptape_nodes.handle_request(ResolveSubstitutionRequest(starting_flow="does_not_exist"))
        assert isinstance(result, ResolveSubstitutionResultFailure)
