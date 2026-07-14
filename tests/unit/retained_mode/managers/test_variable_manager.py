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
    CreateVariableResultFailure,
    CreateVariableResultSuccess,
    DeleteVariableRequest,
    DeleteVariableResultFailure,
    DeleteVariableResultSuccess,
    GetVariableRequest,
    GetVariableResultFailure,
    GetVariableResultSuccess,
    GetVariablesRequest,
    GetVariablesResultFailure,
    GetVariablesResultSuccess,
    GetVariableValueRequest,
    GetVariableValueResultFailure,
    GetVariableValueResultSuccess,
    HasVariableRequest,
    HasVariableResultSuccess,
    ListSubstitutablesRequest,
    ListSubstitutablesResultFailure,
    ListSubstitutablesResultSuccess,
    RenameVariableRequest,
    RenameVariableResultFailure,
    RenameVariableResultSuccess,
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
    FlowVariable,
    VariablePermission,
    VariableScope,
)

_GET_PROJECT_VAR_PATCH = "griptape_nodes.retained_mode.managers.variable_manager.VariablesManager._get_project_variable"
_LIST_PROJECT_VAR_NAMES_PATCH = (
    "griptape_nodes.retained_mode.managers.variable_manager.VariablesManager._list_project_variable_names"
)


def _macro(name: str, value: Any) -> FlowVariable:
    """Build a stand-in for a resolved project variable (builtin or directory).

    Returns a plain, READ_ONLY FlowVariable — the shape the GetProjectVariableRequest
    handler produces after resolving a value. Tests patch `_get_project_variable` to
    return these.
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

    def list_names(_self: object, *, project_id: str | None = None) -> list[str]:  # noqa: ARG001
        return sorted(macros.keys())

    def get_var(_self: object, name: str, *, project_id: str | None = None) -> FlowVariable | None:  # noqa: ARG001
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
        """Precedence FLOW > PROJECT: a flow-scoped user var shadows a project macro of the same name.

        Uses a non-reserved project name (a builtin/directory name can't be taken by a
        flow var — see TestReservedNames), so the shadow is one the API actually permits.
        """
        _add_variable(griptape_nodes, "custom_var", "/my/override")
        with project_macros({"custom_var": "/project"}):
            result = griptape_nodes.handle_request(ListSubstitutablesRequest(starting_flow=flow_name))
        assert isinstance(result, ListSubstitutablesResultSuccess)
        entries = [s for s in result.substitutables if s.name == "custom_var"]
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

    def test_global_colliding_with_project_name_is_still_returned(
        self, griptape_nodes: GriptapeNodes, flow_name: str
    ) -> None:
        """A user global sharing a name with a project builtin must not be shadowed out.

        The project layer is excluded from this view entirely, so the user's own global
        value is returned rather than being masked by (and filtered away with) the
        same-named project builtin.
        """
        griptape_nodes.handle_request(
            CreateVariableRequest(name="workspace_dir", type="str", value="/user/global", is_global=True)
        )
        with project_macros({"workspace_dir": "/project/builtin"}):
            bulk = griptape_nodes.handle_request(GetVariablesRequest(starting_flow=flow_name))
            named = griptape_nodes.handle_request(GetVariablesRequest(starting_flow=flow_name, names=["workspace_dir"]))
        assert isinstance(bulk, GetVariablesResultSuccess)
        assert bulk.variables.get("workspace_dir") == "/user/global"
        assert isinstance(named, GetVariablesResultSuccess)
        assert named.variables == {"workspace_dir": "/user/global"}

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
        """A flow-scoped user var shadows the project macro (new precedence: FLOW > PROJECT).

        Uses a non-reserved project name — a builtin/directory name can't be taken by a
        flow var (see TestReservedNames).
        """
        _add_variable(griptape_nodes, "custom_var", "/my/override")
        with project_macros({"custom_var": "/project"}):
            result = griptape_nodes.handle_request(ResolveSubstitutionRequest(starting_flow=flow_name))
        assert isinstance(result, ResolveSubstitutionResultSuccess)
        assert result.variables["custom_var"] == "/my/override"

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
        # Non-reserved name so the flow var can be created; PROJECT_ONLY still ignores it.
        _add_variable(griptape_nodes, "custom_var", "/from_flow")
        with project_macros({"custom_var": "/from_project"}):
            result = griptape_nodes.handle_request(
                GetVariableRequest(name="custom_var", starting_flow=flow_name, lookup_scope=VariableScope.PROJECT_ONLY)
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
        # Non-reserved name so the flow var can be created; the FROM_PROJECT walk skips it.
        _add_variable(griptape_nodes, "custom_var", "/from_flow")
        with project_macros({"custom_var": "/from_project"}):
            result = griptape_nodes.handle_request(
                GetVariableRequest(
                    name="custom_var",
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
        # The message must name the layer the variable was actually resolved from —
        # 'project', recorded at discovery, not inferred from the search scope.
        assert "project layer" in str(result.result_details)

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

    def test_rename_flow_var_to_reserved_name_fails(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        """Renaming a flow var to a reserved project name (workspace_dir) is refused.

        Matches create's rule — a flow var may not take a reserved project builtin/directory
        name — so the two agree. workspace_dir is reserved by the loaded system-defaults project.
        """
        _add_variable(griptape_nodes, "temp_name", "sc001")
        result = griptape_nodes.handle_request(
            RenameVariableRequest(name="temp_name", new_name="workspace_dir", starting_flow=flow_name)
        )
        assert isinstance(result, RenameVariableResultFailure)
        assert "reserved" in str(result.result_details)

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


class TestReservedNames:
    """A flow variable may not be created or renamed to a reserved project name.

    workspace_dir is a project builtin reserved by the loaded system-defaults project,
    so these exercise the real ListProjectVariables(reserved=True) path — no patching.
    """

    def test_create_flow_var_with_reserved_name_fails(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        result = griptape_nodes.handle_request(
            CreateVariableRequest(name="workspace_dir", type="str", value="/hijack", owning_flow=flow_name)
        )
        assert isinstance(result, CreateVariableResultFailure)
        assert "reserved" in str(result.result_details)

    def test_create_flow_var_with_unreserved_name_succeeds(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        result = griptape_nodes.handle_request(
            CreateVariableRequest(name="not_a_builtin", type="str", value="ok", owning_flow=flow_name)
        )
        assert isinstance(result, CreateVariableResultSuccess)

    def test_create_with_blank_name_fails(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        for blank in ("", "   "):
            result = griptape_nodes.handle_request(
                CreateVariableRequest(name=blank, type="str", value="x", owning_flow=flow_name)
            )
            assert isinstance(result, CreateVariableResultFailure)
            assert "empty name" in str(result.result_details)

    @pytest.mark.usefixtures("flow_name")
    def test_create_global_with_reserved_name_is_allowed(self, griptape_nodes: GriptapeNodes) -> None:
        """Global is not gated on reserved names (only FLOW create/rename is).

        Depends on the flow_name fixture for clean engine state + a current project, but
        doesn't need the flow value itself (globals aren't flow-scoped).
        """
        result = griptape_nodes.handle_request(
            CreateVariableRequest(name="workspace_dir", type="str", value="/global", is_global=True)
        )
        assert isinstance(result, CreateVariableResultSuccess)

    def test_create_flow_var_shadowing_a_global_is_allowed(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        """A flow var may share a name with a global (only reserved names are refused)."""
        griptape_nodes.handle_request(
            CreateVariableRequest(name="shared", type="str", value="from_global", is_global=True)
        )
        result = griptape_nodes.handle_request(
            CreateVariableRequest(name="shared", type="str", value="from_flow", owning_flow=flow_name)
        )
        assert isinstance(result, CreateVariableResultSuccess)

    def test_rename_to_same_name_is_noop_success(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        """Renaming a variable to its current name is an idempotent success, not a self-collision crash."""
        _add_variable(griptape_nodes, "keeper", "v1")
        result = griptape_nodes.handle_request(
            RenameVariableRequest(name="keeper", new_name="keeper", starting_flow=flow_name)
        )
        assert isinstance(result, RenameVariableResultSuccess)
        # Value preserved.
        after = griptape_nodes.handle_request(GetVariablesRequest(starting_flow=flow_name, names=["keeper"]))
        assert isinstance(after, GetVariablesResultSuccess)
        assert after.variables == {"keeper": "v1"}

    def test_rename_to_blank_name_fails(self, griptape_nodes: GriptapeNodes, flow_name: str) -> None:
        _add_variable(griptape_nodes, "keeper", "v1")
        for blank in ("", "   "):
            result = griptape_nodes.handle_request(
                RenameVariableRequest(name="keeper", new_name=blank, starting_flow=flow_name)
            )
            assert isinstance(result, RenameVariableResultFailure)
            assert "empty name" in str(result.result_details)

    @pytest.mark.usefixtures("flow_name")
    def test_rename_reserved_named_global_to_itself_is_noop_success(self, griptape_nodes: GriptapeNodes) -> None:
        """A global named like a reserved builtin (globals aren't reserved-gated) can be renamed to itself.

        Exercises the idempotent short-circuit that runs BEFORE the reserved-name gate — without it,
        a no-op rename of a reserved-named variable would surface a spurious 'that name is reserved' failure.
        """
        griptape_nodes.handle_request(
            CreateVariableRequest(name="workspace_dir", type="str", value="/g", is_global=True)
        )
        result = griptape_nodes.handle_request(
            RenameVariableRequest(
                name="workspace_dir", new_name="workspace_dir", lookup_scope=VariableScope.GLOBAL_ONLY
            )
        )
        assert isinstance(result, RenameVariableResultSuccess)

    @pytest.mark.usefixtures("flow_name")
    def test_delete_global_variable_routes_to_global_layer(self, griptape_nodes: GriptapeNodes) -> None:
        """Delete routes by layer provenance — a global (owning_flow_name=None) leaves the global layer."""
        griptape_nodes.handle_request(CreateVariableRequest(name="g_del", type="str", value="v", is_global=True))
        result = griptape_nodes.handle_request(
            DeleteVariableRequest(name="g_del", lookup_scope=VariableScope.GLOBAL_ONLY)
        )
        assert isinstance(result, DeleteVariableResultSuccess)
        after = griptape_nodes.handle_request(
            GetVariableValueRequest(name="g_del", lookup_scope=VariableScope.GLOBAL_ONLY)
        )
        assert isinstance(after, GetVariableValueResultFailure)

    @pytest.mark.usefixtures("flow_name")
    def test_rename_global_variable_routes_to_global_layer(self, griptape_nodes: GriptapeNodes) -> None:
        """Rename routes by layer provenance — a global renames within the global layer."""
        griptape_nodes.handle_request(CreateVariableRequest(name="g_old", type="str", value="v", is_global=True))
        result = griptape_nodes.handle_request(
            RenameVariableRequest(name="g_old", new_name="g_new", lookup_scope=VariableScope.GLOBAL_ONLY)
        )
        assert isinstance(result, RenameVariableResultSuccess)
        new_val = griptape_nodes.handle_request(
            GetVariableValueRequest(name="g_new", lookup_scope=VariableScope.GLOBAL_ONLY)
        )
        assert isinstance(new_val, GetVariableValueResultSuccess)
        assert new_val.value == "v"
        old_val = griptape_nodes.handle_request(
            GetVariableValueRequest(name="g_old", lookup_scope=VariableScope.GLOBAL_ONLY)
        )
        assert isinstance(old_val, GetVariableValueResultFailure)


class TestProjectVariableSerialization:
    """Regression: project-layer variables must cross the request boundary as plain, serializable FlowVariables."""

    def test_get_variable_from_project_returns_plain_flow_variable(
        self, griptape_nodes: GriptapeNodes, flow_name: str
    ) -> None:
        """A HIERARCHICAL Get that resolves to the project layer returns a plain, serializable FlowVariable."""
        with project_macros({"workspace_dir": "/proj"}):
            result = griptape_nodes.handle_request(GetVariableRequest(name="workspace_dir", starting_flow=flow_name))
        assert isinstance(result, GetVariableResultSuccess)
        # Must be a plain FlowVariable carrying a stored value — no live resolver.
        assert type(result.variable) is FlowVariable
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
