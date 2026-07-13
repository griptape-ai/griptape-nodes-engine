"""Tests for VariablesManager request handlers."""

import contextlib
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.variable_events import (
    CreateVariableRequest,
    CreateVariableResultSuccess,
    DeleteVariableRequest,
    DeleteVariableResultFailure,
    GetVariableRequest,
    GetVariableResultFailure,
    GetVariableResultSuccess,
    GetVariablesRequest,
    GetVariablesResultFailure,
    GetVariablesResultSuccess,
    HasVariableRequest,
    HasVariableResultSuccess,
    ListSubstitutablesRequest,
    ListSubstitutablesResultFailure,
    ListSubstitutablesResultSuccess,
    RenameVariableRequest,
    RenameVariableResultFailure,
    ResolveSubstitutionRequest,
    ResolveSubstitutionResultFailure,
    ResolveSubstitutionResultSuccess,
    SetVariablesRequest,
    SetVariablesResultFailure,
    SetVariableValueRequest,
    SetVariableValueResultFailure,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.variable_types import (
    ComputedFlowVariable,
    FlowVariable,
    VariablePermission,
    VariableScope,
)

_GET_PROJECT_VAR_PATCH = "griptape_nodes.retained_mode.managers.variable_manager.VariablesManager._get_project_variable"
_LIST_PROJECT_VAR_NAMES_PATCH = (
    "griptape_nodes.retained_mode.managers.variable_manager.VariablesManager._list_project_variable_names"
)


def _macro(name: str, value: Any) -> FlowVariable:
    """Build a stand-in for a project macro that resolves to `value`.

    Returns a plain FlowVariable (not ComputedFlowVariable) to match the invariant that
    the GetProjectVariableRequest handler unwraps before returning — ComputedFlowVariable
    never crosses the request boundary. Tests that patch `_get_project_variable` are
    exercising post-unwrap state.
    """
    return FlowVariable(
        name=name,
        owning_flow_name=None,
        type="str",
        value=value,
        permission=VariablePermission.READ_ONLY,
    )


@contextlib.contextmanager
def project_macros(macros: dict[str, Any]) -> Iterator[None]:
    """Patch VariablesManager's project-layer seams to return the given macros.

    Usage: `with project_macros({"workspace_dir": "/x"}): ...`.
    """

    def list_names(_self: object, _project_id: str | None = None) -> list[str]:
        return sorted(macros.keys())

    def get_var(_self: object, name: str, _project_id: str | None = None) -> FlowVariable | None:
        if name not in macros:
            return None
        return _macro(name, macros[name])

    with (
        patch(_LIST_PROJECT_VAR_NAMES_PATCH, autospec=True, side_effect=list_names),
        patch(_GET_PROJECT_VAR_PATCH, autospec=True, side_effect=get_var),
    ):
        yield


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
        with project_macros({"workspace_dir": "/workspace"}):
            result = griptape_nodes.handle_request(ListSubstitutablesRequest(starting_flow=flow_name))
        assert isinstance(result, ListSubstitutablesResultSuccess)
        assert {s.name for s in result.substitutables} == {"SHOT", "workspace_dir"}

    def test_macro_has_correct_metadata(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        with project_macros({"workspace_dir": "/workspace"}):
            result = griptape_nodes.handle_request(ListSubstitutablesRequest(starting_flow=flow_name))
        assert isinstance(result, ListSubstitutablesResultSuccess)
        macro = next(s for s in result.substitutables if s.name == "workspace_dir")
        assert macro.source == "macro"
        assert macro.read_only is True
        assert macro.value == "/workspace"

    def test_user_var_has_correct_metadata(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        _add_variable(griptape_nodes, "SHOT", "sc001")
        with project_macros({}):
            result = griptape_nodes.handle_request(ListSubstitutablesRequest(starting_flow=flow_name))
        assert isinstance(result, ListSubstitutablesResultSuccess)
        var = next(s for s in result.substitutables if s.name == "SHOT")
        assert var.source == "variable"
        assert var.read_only is False
        assert var.value == "sc001"

    def test_flow_var_shadows_macro_on_name_collision(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        """Precedence FLOW > PROJECT: a flow-scoped user var shadows a project macro of the same name."""
        _add_variable(griptape_nodes, "workspace_dir", "/my/override")
        with project_macros({"workspace_dir": "/workspace"}):
            result = griptape_nodes.handle_request(ListSubstitutablesRequest(starting_flow=flow_name))
        assert isinstance(result, ListSubstitutablesResultSuccess)
        entries = [s for s in result.substitutables if s.name == "workspace_dir"]
        assert len(entries) == 1
        assert entries[0].source == "variable"
        assert entries[0].read_only is False
        assert entries[0].value == "/my/override"

    def test_macro_shadows_global_on_name_collision(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        """Precedence PROJECT > GLOBAL: a project macro shadows a global user var of the same name."""
        griptape_nodes.handle_request(
            CreateVariableRequest(name="workspace_dir", type="str", value="/my/override", is_global=True)
        )
        with project_macros({"workspace_dir": "/workspace"}):
            result = griptape_nodes.handle_request(ListSubstitutablesRequest(starting_flow=flow_name))
        assert isinstance(result, ListSubstitutablesResultSuccess)
        entries = [s for s in result.substitutables if s.name == "workspace_dir"]
        assert len(entries) == 1
        assert entries[0].source == "macro"
        assert entries[0].read_only is True
        assert entries[0].value == "/workspace"

    def test_filters_out_non_substitutable_user_vars(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        """User vars with non-str/int values are excluded, matching ResolveSubstitutionRequest behavior."""
        _add_variable(griptape_nodes, "SHOT", "sc001")
        _add_variable(griptape_nodes, "META", None)
        _add_variable(griptape_nodes, "TAGS", ["a", "b"])
        with project_macros({}):
            result = griptape_nodes.handle_request(ListSubstitutablesRequest(starting_flow=flow_name))
        assert isinstance(result, ListSubstitutablesResultSuccess)
        names = {s.name for s in result.substitutables}
        assert "SHOT" in names
        assert "META" not in names
        assert "TAGS" not in names

    def test_returns_empty_when_no_vars_and_no_macros(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        with project_macros({}):
            result = griptape_nodes.handle_request(ListSubstitutablesRequest(starting_flow=flow_name))
        assert isinstance(result, ListSubstitutablesResultSuccess)
        assert result.substitutables == []

    def test_returns_failure_for_unknown_flow(self, griptape_nodes: GriptapeNodes) -> None:
        with project_macros({}):
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
        with project_macros({"workspace_dir": "/workspace"}):
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
    """ResolveSubstitutionRequest layers project vars with user vars; precedence FLOW > PROJECT > GLOBAL."""

    def test_returns_user_vars_and_macros(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        _add_variable(griptape_nodes, "SHOT", "sc001")
        with project_macros({"workspace_dir": "/workspace"}):
            result = griptape_nodes.handle_request(ResolveSubstitutionRequest(starting_flow=flow_name))
        assert isinstance(result, ResolveSubstitutionResultSuccess)
        assert result.variables["SHOT"] == "sc001"
        assert result.variables["workspace_dir"] == "/workspace"

    def test_flow_var_wins_over_macro(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        """A flow-scoped user var shadows the project macro (new precedence: FLOW > PROJECT)."""
        _add_variable(griptape_nodes, "workspace_dir", "/my/override")
        with project_macros({"workspace_dir": "/workspace"}):
            result = griptape_nodes.handle_request(ResolveSubstitutionRequest(starting_flow=flow_name))
        assert isinstance(result, ResolveSubstitutionResultSuccess)
        assert result.variables["workspace_dir"] == "/my/override"

    def test_macro_wins_over_global(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        """A project macro shadows a global user var (new precedence: PROJECT > GLOBAL)."""
        griptape_nodes.handle_request(
            CreateVariableRequest(name="workspace_dir", type="str", value="/my/override", is_global=True)
        )
        with project_macros({"workspace_dir": "/workspace"}):
            result = griptape_nodes.handle_request(ResolveSubstitutionRequest(starting_flow=flow_name))
        assert isinstance(result, ResolveSubstitutionResultSuccess)
        assert result.variables["workspace_dir"] == "/workspace"

    def test_named_lookup_finds_macro(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        with project_macros({"workspace_dir": "/workspace"}):
            result = griptape_nodes.handle_request(
                ResolveSubstitutionRequest(starting_flow=flow_name, names=["workspace_dir"])
            )
        assert isinstance(result, ResolveSubstitutionResultSuccess)
        assert result.variables == {"workspace_dir": "/workspace"}

    def test_named_lookup_fails_if_not_in_vars_or_macros(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        _add_variable(griptape_nodes, "SHOT", "sc001")
        with project_macros({"workspace_dir": "/workspace"}):
            result = griptape_nodes.handle_request(
                ResolveSubstitutionRequest(starting_flow=flow_name, names=["SHOT", "workspace_dir", "MISSING"])
            )
        assert isinstance(result, ResolveSubstitutionResultFailure)
        assert result.unresolved == ["MISSING"]
        assert result.resolved == {"SHOT": "sc001", "workspace_dir": "/workspace"}

    def test_returns_failure_for_unknown_flow(self, griptape_nodes: GriptapeNodes) -> None:
        with project_macros({}):
            result = griptape_nodes.handle_request(ResolveSubstitutionRequest(starting_flow="does_not_exist"))
        assert isinstance(result, ResolveSubstitutionResultFailure)


class TestProjectLayerScopes:
    """PROJECT_ONLY and HIERARCHICAL_FROM_PROJECT — new opt-in scopes."""

    def test_project_only_returns_project_entry(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        with project_macros({"workspace_dir": "/proj"}):
            result = griptape_nodes.handle_request(
                GetVariableRequest(
                    name="workspace_dir", starting_flow=flow_name, lookup_scope=VariableScope.PROJECT_ONLY
                )
            )
        assert isinstance(result, GetVariableResultSuccess)
        assert result.variable.value == "/proj"

    def test_project_only_ignores_flow_var(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        _add_variable(griptape_nodes, "workspace_dir", "/from_flow")
        with project_macros({"workspace_dir": "/from_project"}):
            result = griptape_nodes.handle_request(
                GetVariableRequest(
                    name="workspace_dir", starting_flow=flow_name, lookup_scope=VariableScope.PROJECT_ONLY
                )
            )
        assert isinstance(result, GetVariableResultSuccess)
        assert result.variable.value == "/from_project"

    def test_project_only_missing_returns_failure(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        with project_macros({}):
            result = griptape_nodes.handle_request(
                GetVariableRequest(
                    name="workspace_dir", starting_flow=flow_name, lookup_scope=VariableScope.PROJECT_ONLY
                )
            )
        assert isinstance(result, GetVariableResultFailure)

    def test_hierarchical_from_project_finds_project(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        _add_variable(griptape_nodes, "workspace_dir", "/from_flow")
        with project_macros({"workspace_dir": "/from_project"}):
            result = griptape_nodes.handle_request(
                GetVariableRequest(
                    name="workspace_dir",
                    starting_flow=flow_name,
                    lookup_scope=VariableScope.HIERARCHICAL_FROM_PROJECT,
                )
            )
        assert isinstance(result, GetVariableResultSuccess)
        # Flow var must not shadow — the walk skips flows entirely.
        assert result.variable.value == "/from_project"

    def test_hierarchical_from_project_falls_through_to_global(
        self, griptape_nodes: GriptapeNodes, flow_name: str
    ) -> None:
        griptape_nodes.handle_request(
            CreateVariableRequest(name="only_global", type="str", value="from_global", is_global=True)
        )
        with project_macros({}):
            result = griptape_nodes.handle_request(
                GetVariableRequest(
                    name="only_global",
                    starting_flow=flow_name,
                    lookup_scope=VariableScope.HIERARCHICAL_FROM_PROJECT,
                )
            )
        assert isinstance(result, GetVariableResultSuccess)
        assert result.variable.value == "from_global"

    def test_hierarchical_from_project_ignores_flow_vars(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        _add_variable(griptape_nodes, "only_flow", "from_flow")
        with project_macros({}):
            result = griptape_nodes.handle_request(
                GetVariableRequest(
                    name="only_flow",
                    starting_flow=flow_name,
                    lookup_scope=VariableScope.HIERARCHICAL_FROM_PROJECT,
                )
            )
        assert isinstance(result, GetVariableResultFailure)

    def test_hierarchical_walks_flow_project_global(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        """HIERARCHICAL: flow first, then project, then global."""
        # Only in project layer:
        with project_macros({"workspace_dir": "/from_project"}):
            result = griptape_nodes.handle_request(GetVariableRequest(name="workspace_dir", starting_flow=flow_name))
        assert isinstance(result, GetVariableResultSuccess)
        assert result.variable.value == "/from_project"

    def test_has_variable_reports_found_scope_project(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        with project_macros({"workspace_dir": "/from_project"}):
            result = griptape_nodes.handle_request(HasVariableRequest(name="workspace_dir", starting_flow=flow_name))
        assert isinstance(result, HasVariableResultSuccess)
        assert result.exists is True
        assert result.found_scope is VariableScope.PROJECT_ONLY


class TestVariablePermission:
    """READ_ONLY variables refuse writes uniformly."""

    def test_set_value_on_project_variable_fails(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        with project_macros({"workspace_dir": "/proj"}):
            result = griptape_nodes.handle_request(
                SetVariableValueRequest(name="workspace_dir", value="/new", starting_flow=flow_name)
            )
        assert isinstance(result, SetVariableValueResultFailure)

    def test_delete_project_variable_fails(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        with project_macros({"workspace_dir": "/proj"}):
            result = griptape_nodes.handle_request(DeleteVariableRequest(name="workspace_dir", starting_flow=flow_name))
        assert isinstance(result, DeleteVariableResultFailure)

    def test_rename_project_variable_fails(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        with project_macros({"workspace_dir": "/proj"}):
            result = griptape_nodes.handle_request(
                RenameVariableRequest(name="workspace_dir", new_name="new", starting_flow=flow_name)
            )
        assert isinstance(result, RenameVariableResultFailure)

    def test_set_variables_batch_with_project_hit_fails(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        _add_variable(griptape_nodes, "SHOT", "sc001")
        with project_macros({"workspace_dir": "/proj"}):
            result = griptape_nodes.handle_request(
                SetVariablesRequest(
                    starting_flow=flow_name,
                    variables={"SHOT": "sc002", "workspace_dir": "/hacked"},
                )
            )
        assert isinstance(result, SetVariablesResultFailure)
        # SHOT must NOT have been written (all-or-nothing).
        after = griptape_nodes.handle_request(GetVariablesRequest(starting_flow=flow_name, names=["SHOT"]))
        assert isinstance(after, GetVariablesResultSuccess)
        assert after.variables == {"SHOT": "sc001"}


class TestComputedFlowVariable:
    """ComputedFlowVariable invokes its resolver on every read; writing raises."""

    def test_value_invokes_resolver_each_call(self) -> None:
        calls = {"n": 0}

        def resolver() -> str:
            calls["n"] += 1
            return f"call-{calls['n']}"

        var = ComputedFlowVariable(name="foo", type="str", resolver=resolver)
        expected_calls = 2
        assert var.value == "call-1"
        assert var.value == "call-2"
        assert calls["n"] == expected_calls

    def test_value_setter_raises(self) -> None:
        var = ComputedFlowVariable(name="foo", type="str", resolver=lambda: "x")
        with pytest.raises(ValueError, match="READ_ONLY"):
            var.value = "y"

    def test_permission_is_read_only(self) -> None:
        var = ComputedFlowVariable(name="foo", type="str", resolver=lambda: "x")
        assert var.permission is VariablePermission.READ_ONLY


class TestProjectVariableSerialization:
    """Regression: project-layer variables must not leak ComputedFlowVariable across the request boundary."""

    def test_get_variable_from_project_returns_plain_flow_variable(
        self, griptape_nodes: GriptapeNodes, flow_name: str
    ) -> None:
        """A HIERARCHICAL Get that resolves to the project layer returns a plain FlowVariable, not a ComputedFlowVariable."""
        with project_macros({"workspace_dir": "/proj"}):
            result = griptape_nodes.handle_request(GetVariableRequest(name="workspace_dir", starting_flow=flow_name))
        assert isinstance(result, GetVariableResultSuccess)
        # Must be a *plain* FlowVariable — no live resolver attached.
        assert type(result.variable) is FlowVariable
        assert not isinstance(result.variable, ComputedFlowVariable)
        assert result.variable.value == "/proj"

    def test_get_variable_from_project_serializes(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        """The Success payload must survive cattrs unstructure (the broadcast path)."""
        from griptape_nodes.retained_mode.events.event_converter import safe_unstructure

        with project_macros({"workspace_dir": "/proj"}):
            result = griptape_nodes.handle_request(GetVariableRequest(name="workspace_dir", starting_flow=flow_name))
        assert isinstance(result, GetVariableResultSuccess)
        serialized = safe_unstructure(result)
        assert serialized["variable"]["name"] == "workspace_dir"
        assert serialized["variable"]["value"] == "/proj"
