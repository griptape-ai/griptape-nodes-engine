"""Unit tests for ParameterTransitionComponent.

Two test groups:

- ``compute_plan`` purity tests: build a MockNode populated with Parameter
  objects directly (no event dispatch), instantiate the component, and assert
  on the four TransitionPlan buckets.

- ``transition_to`` dispatch-sequence tests: patch
  ``GriptapeNodes.handle_request`` and assert on the exact request sequence
  the component dispatches. This is the closest unit-level proxy for end-to-end
  behaviour without standing up a real flow/workflow context.
"""

from typing import Any, cast
from unittest.mock import Mock, patch

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.param_components.parameter_transition_component import (
    ParameterTransitionComponent,
    TransitionParameter,
    TransitionPlan,
)
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
    IncomingConnection,
    OutgoingConnection,
)
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterToNodeRequest,
    AddParameterToNodeResultFailure,
    AddParameterToNodeResultSuccess,
    GetConnectionsForParameterRequest,
    GetConnectionsForParameterResultFailure,
    GetConnectionsForParameterResultSuccess,
    RemoveParameterFromNodeRequest,
    RemoveParameterFromNodeResultFailure,
    RemoveParameterFromNodeResultSuccess,
)
from tests.unit.exe_types.mocks import MockNode

# Patch target — GriptapeNodes is imported INTO the component module, so the
# binding to patch is the module-local reference, not the global singleton.
_HANDLE_REQUEST_TARGET = (
    "griptape_nodes.exe_types.param_components.parameter_transition_component.GriptapeNodes.handle_request"
)


# ---------------------------------------------------------------------------
# Helpers


def _factory_for(name: str) -> Mock:
    """Return a Mock add-request factory.

    Returned as a Mock so call-site assertions (``assert_called_once`` /
    ``assert_not_called``) type-check against the value the test sees. The
    component receives it as ``Callable[[], AddParameterToNodeRequest]`` —
    Mock satisfies that shape at runtime.
    """
    factory = Mock(spec=lambda: None)
    factory.return_value = AddParameterToNodeRequest(parameter_name=name)
    return factory


def _transition_param(
    name: str,
    *,
    allowed_modes: frozenset[ParameterMode] = frozenset({ParameterMode.INPUT, ParameterMode.PROPERTY}),
    input_types: frozenset[str] = frozenset({"str"}),
    output_type: str = "str",
) -> TransitionParameter:
    """Build a TransitionParameter with sensible defaults for the test cases."""
    return TransitionParameter(
        name=name,
        allowed_modes=allowed_modes,
        input_types=input_types,
        output_type=output_type,
        add_request_factory=_factory_for(name),
    )


def _add_param_to_node(
    node: MockNode,
    name: str,
    *,
    allowed_modes: set[ParameterMode] | None = None,
    input_types: list[str] | None = None,
    output_type: str | None = None,
) -> Parameter:
    """Construct a Parameter and attach it to a MockNode via add_parameter.

    Defaults mirror the input-only / property-bearing parameters EngineNode produces:
    ``input_types=["str"]``, ``allowed_modes={INPUT, PROPERTY}``. ``output_type`` is
    omitted by default so the property's first-input-type fallback applies.
    """
    if allowed_modes is None:
        allowed_modes = {ParameterMode.INPUT, ParameterMode.PROPERTY}
    if input_types is None:
        input_types = ["str"]
    parameter = Parameter(
        name=name,
        tooltip=f"Test parameter {name}",
        input_types=input_types,
        output_type=output_type,
        allowed_modes=allowed_modes,
    )
    node.add_parameter(parameter)
    return parameter


@pytest.fixture
def host_node() -> MockNode:
    """Fresh MockNode for each test."""
    return MockNode(name="host")


@pytest.fixture
def manage_dyn_prefix_component(host_node: MockNode) -> ParameterTransitionComponent:
    """Component scoped to parameters whose names start with 'dyn_'."""
    return ParameterTransitionComponent(
        host_node,
        manages_parameter=lambda p: p.name.startswith("dyn_"),
    )


# ---------------------------------------------------------------------------
# Empty / asymmetric cases


class TestComputePlanEmptyCases:
    def test_empty_current_empty_desired_yields_empty_plan(
        self, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        plan = manage_dyn_prefix_component.compute_plan([])
        assert plan == TransitionPlan(
            to_preserve=frozenset(),
            to_replace=frozenset(),
            to_remove=frozenset(),
            to_add=frozenset(),
        )

    def test_empty_current_non_empty_desired_routes_all_to_add(
        self, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        desired = [_transition_param("dyn_a"), _transition_param("dyn_b")]

        plan = manage_dyn_prefix_component.compute_plan(desired)

        assert plan.to_add == frozenset({"dyn_a", "dyn_b"})
        assert plan.to_preserve == frozenset()
        assert plan.to_replace == frozenset()
        assert plan.to_remove == frozenset()

    def test_non_empty_current_empty_desired_routes_all_to_remove(
        self, host_node: MockNode, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        _add_param_to_node(host_node, "dyn_a")
        _add_param_to_node(host_node, "dyn_b")

        plan = manage_dyn_prefix_component.compute_plan([])

        assert plan.to_remove == frozenset({"dyn_a", "dyn_b"})
        assert plan.to_preserve == frozenset()
        assert plan.to_replace == frozenset()
        assert plan.to_add == frozenset()


# ---------------------------------------------------------------------------
# Identity matching


class TestComputePlanIdenticalSignatures:
    def test_identical_signatures_route_to_preserve(
        self, host_node: MockNode, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        _add_param_to_node(
            host_node, "dyn_a", input_types=["str"], allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY}
        )

        desired = [
            _transition_param(
                "dyn_a",
                allowed_modes=frozenset({ParameterMode.INPUT, ParameterMode.PROPERTY}),
                input_types=frozenset({"str"}),
                output_type="str",  # property fallback for an input-only param: first input type
            )
        ]

        plan = manage_dyn_prefix_component.compute_plan(desired)

        assert plan.to_preserve == frozenset({"dyn_a"})
        assert plan.to_replace == frozenset()
        assert plan.to_remove == frozenset()
        assert plan.to_add == frozenset()


# ---------------------------------------------------------------------------
# Replace cases — type changes


class TestComputePlanTypeReplace:
    def test_input_types_narrowed_routes_to_replace(
        self, host_node: MockNode, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        _add_param_to_node(host_node, "dyn_a", input_types=["int", "str"])

        desired = [
            _transition_param(
                "dyn_a",
                input_types=frozenset({"int"}),
                output_type="int",
            )
        ]

        plan = manage_dyn_prefix_component.compute_plan(desired)

        assert plan.to_replace == frozenset({"dyn_a"})
        assert plan.to_preserve == frozenset()

    def test_input_types_widened_routes_to_replace(
        self, host_node: MockNode, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        _add_param_to_node(host_node, "dyn_a", input_types=["int"])

        desired = [
            _transition_param(
                "dyn_a",
                input_types=frozenset({"int", "str", "float"}),
                output_type="int",
            )
        ]

        plan = manage_dyn_prefix_component.compute_plan(desired)

        assert plan.to_replace == frozenset({"dyn_a"})

    def test_output_type_changed_routes_to_replace(
        self, host_node: MockNode, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        _add_param_to_node(
            host_node,
            "dyn_a",
            input_types=None,
            output_type="str",
            allowed_modes={ParameterMode.OUTPUT},
        )

        desired = [
            _transition_param(
                "dyn_a",
                allowed_modes=frozenset({ParameterMode.OUTPUT}),
                input_types=frozenset({"int"}),
                output_type="int",
            )
        ]

        plan = manage_dyn_prefix_component.compute_plan(desired)

        assert plan.to_replace == frozenset({"dyn_a"})


# ---------------------------------------------------------------------------
# Replace cases — mode changes


class TestComputePlanModeReplace:
    def test_gain_output_mode_routes_to_replace(
        self, host_node: MockNode, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        _add_param_to_node(
            host_node,
            "dyn_a",
            input_types=["str"],
            allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
        )

        desired = [
            _transition_param(
                "dyn_a",
                allowed_modes=frozenset({ParameterMode.INPUT, ParameterMode.PROPERTY, ParameterMode.OUTPUT}),
                input_types=frozenset({"str"}),
                output_type="str",
            )
        ]

        plan = manage_dyn_prefix_component.compute_plan(desired)

        assert plan.to_replace == frozenset({"dyn_a"})

    def test_lose_output_mode_routes_to_replace(
        self, host_node: MockNode, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        _add_param_to_node(
            host_node,
            "dyn_a",
            input_types=["str"],
            allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY, ParameterMode.OUTPUT},
        )

        desired = [
            _transition_param(
                "dyn_a",
                allowed_modes=frozenset({ParameterMode.INPUT, ParameterMode.PROPERTY}),
                input_types=frozenset({"str"}),
                output_type="str",
            )
        ]

        plan = manage_dyn_prefix_component.compute_plan(desired)

        assert plan.to_replace == frozenset({"dyn_a"})

    def test_full_mode_swap_input_to_output_routes_to_replace(
        self, host_node: MockNode, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        _add_param_to_node(
            host_node,
            "dyn_a",
            input_types=["str"],
            allowed_modes={ParameterMode.INPUT},
        )

        desired = [
            _transition_param(
                "dyn_a",
                allowed_modes=frozenset({ParameterMode.OUTPUT}),
                input_types=frozenset({"str"}),
                output_type="str",
            )
        ]

        plan = manage_dyn_prefix_component.compute_plan(desired)

        assert plan.to_replace == frozenset({"dyn_a"})


# ---------------------------------------------------------------------------
# Rename / asymmetric / mixed


class TestComputePlanRenameAndMixed:
    def test_rename_is_remove_plus_add_not_replace(
        self, host_node: MockNode, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        _add_param_to_node(host_node, "dyn_old")

        desired = [_transition_param("dyn_new")]

        plan = manage_dyn_prefix_component.compute_plan(desired)

        assert plan.to_remove == frozenset({"dyn_old"})
        assert plan.to_add == frozenset({"dyn_new"})
        assert plan.to_replace == frozenset()
        assert plan.to_preserve == frozenset()

    def test_mixed_scenario_routes_each_disposition_correctly(
        self, host_node: MockNode, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        # preserved: identical signature on both sides
        _add_param_to_node(host_node, "dyn_keep", input_types=["str"])
        # replaced: present on both, signature differs
        _add_param_to_node(host_node, "dyn_change", input_types=["str"])
        # removed: present in current only
        _add_param_to_node(host_node, "dyn_drop", input_types=["str"])

        desired = [
            _transition_param("dyn_keep", input_types=frozenset({"str"}), output_type="str"),
            _transition_param("dyn_change", input_types=frozenset({"int"}), output_type="int"),
            # added: present in desired only
            _transition_param("dyn_new", input_types=frozenset({"float"}), output_type="float"),
        ]

        plan = manage_dyn_prefix_component.compute_plan(desired)

        assert plan.to_preserve == frozenset({"dyn_keep"})
        assert plan.to_replace == frozenset({"dyn_change"})
        assert plan.to_remove == frozenset({"dyn_drop"})
        assert plan.to_add == frozenset({"dyn_new"})


# ---------------------------------------------------------------------------
# Validation / predicate behaviour


class TestComputePlanValidation:
    def test_duplicate_name_in_desired_raises_value_error(
        self, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        desired = [_transition_param("dyn_a"), _transition_param("dyn_a")]

        with pytest.raises(ValueError, match="appears twice in the desired list"):
            manage_dyn_prefix_component.compute_plan(desired)

    def test_predicate_excludes_unmanaged_parameters_from_current_set(
        self, host_node: MockNode, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        # 'static_a' is not managed by the dyn_-prefixed component; it should be
        # invisible to compute_plan even when desired contains a parameter that
        # collides on name only with the unmanaged set.
        _add_param_to_node(host_node, "static_a", input_types=["str"])

        plan = manage_dyn_prefix_component.compute_plan([_transition_param("dyn_a")])

        assert plan.to_add == frozenset({"dyn_a"})
        assert plan.to_remove == frozenset()
        # static_a remains untouched on the node — verified implicitly by it not
        # appearing in any bucket of the plan.

    def test_always_false_predicate_yields_empty_current_set(self, host_node: MockNode) -> None:
        _add_param_to_node(host_node, "dyn_a")
        _add_param_to_node(host_node, "dyn_b")

        component = ParameterTransitionComponent(host_node, manages_parameter=lambda _p: False)

        plan = component.compute_plan([_transition_param("dyn_a")])

        # Current set is empty under the predicate, so the existing dyn_a on the
        # node is invisible and the desired dyn_a lands in to_add.
        assert plan.to_add == frozenset({"dyn_a"})
        assert plan.to_preserve == frozenset()
        assert plan.to_replace == frozenset()
        assert plan.to_remove == frozenset()


# ---------------------------------------------------------------------------
# Purity / idempotence


class TestComputePlanPurity:
    def test_compute_plan_is_idempotent(
        self, host_node: MockNode, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        _add_param_to_node(host_node, "dyn_a", input_types=["str"])
        desired = [
            _transition_param("dyn_a", input_types=frozenset({"str"}), output_type="str"),
            _transition_param("dyn_b", input_types=frozenset({"int"}), output_type="int"),
        ]

        first = manage_dyn_prefix_component.compute_plan(desired)
        second = manage_dyn_prefix_component.compute_plan(desired)

        assert first == second

    def test_compute_plan_does_not_mutate_node_state(
        self, host_node: MockNode, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        _add_param_to_node(host_node, "dyn_a", input_types=["str"])
        desired = [
            _transition_param("dyn_a", input_types=frozenset({"int"}), output_type="int"),
            _transition_param("dyn_new", input_types=frozenset({"str"}), output_type="str"),
        ]

        names_before = sorted(p.name for p in host_node.parameters)
        manage_dyn_prefix_component.compute_plan(desired)
        names_after = sorted(p.name for p in host_node.parameters)

        assert names_before == names_after
        # Existing parameter's signature is also untouched.
        existing = next(p for p in host_node.parameters if p.name == "dyn_a")
        assert frozenset(existing.input_types) == frozenset({"str"})


# ---------------------------------------------------------------------------
# transition_to dispatch-sequence tests
#
# These patch GriptapeNodes.handle_request inside the component module so we
# can observe the exact request sequence transition_to dispatches without
# standing up a real flow/workflow context. They exercise:
#
#   - dispatch ordering across the four buckets (replace / remove / add)
#   - per-replace ordering: capture connections -> remove -> add -> recreate
#   - failure handling: a failed remove or add is logged and does not abort
#   - idempotence: a repeat call with the same desired set dispatches nothing


def _success_get_connections(
    *,
    parameter_name: str,
    node_name: str,
    incoming: list[IncomingConnection] | None = None,
    outgoing: list[OutgoingConnection] | None = None,
) -> GetConnectionsForParameterResultSuccess:
    return GetConnectionsForParameterResultSuccess(
        parameter_name=parameter_name,
        node_name=node_name,
        incoming_connections=incoming or [],
        outgoing_connections=outgoing or [],
        result_details="ok",
    )


class _DispatchRecorder:
    """Records every handle_request call and returns a caller-supplied response per type.

    A response can either be a result-payload instance (returned as-is) or a
    callable taking the request and returning a result payload (lets a single
    test vary its response across multiple same-typed requests).
    """

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self._responses: dict[type, Any] = {}

    def map_type(self, request_type: type, response: Any) -> None:
        self._responses[request_type] = response

    def __call__(self, request: Any) -> Any:
        self.calls.append(request)
        if type(request) not in self._responses:
            msg = f"DispatchRecorder has no response configured for {type(request).__name__}"
            raise AssertionError(msg)
        response = self._responses[type(request)]
        if callable(response):
            return response(request)
        return response


class TestTransitionToDispatchSequence:
    """Mock-based coverage of transition_to's request dispatch."""

    def test_pure_add_dispatches_only_add_requests(
        self, host_node: MockNode, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        desired = [_transition_param("dyn_a"), _transition_param("dyn_b")]
        recorder = _DispatchRecorder()
        recorder.map_type(
            AddParameterToNodeRequest,
            AddParameterToNodeResultSuccess(
                parameter_name="ignored",
                type="str",
                node_name=host_node.name,
                result_details="ok",
            ),
        )

        with patch(_HANDLE_REQUEST_TARGET, side_effect=recorder):
            manage_dyn_prefix_component.transition_to(desired)

        assert all(isinstance(call, AddParameterToNodeRequest) for call in recorder.calls)
        # Both factories were invoked exactly once each (the component is the
        # only thing that calls them).
        for desired_param in desired:
            cast("Mock", desired_param.add_request_factory).assert_called_once()

    def test_pure_remove_dispatches_only_remove_requests(
        self, host_node: MockNode, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        _add_param_to_node(host_node, "dyn_a")
        _add_param_to_node(host_node, "dyn_b")
        recorder = _DispatchRecorder()
        recorder.map_type(
            RemoveParameterFromNodeRequest,
            RemoveParameterFromNodeResultSuccess(result_details="ok"),
        )

        with patch(_HANDLE_REQUEST_TARGET, side_effect=recorder):
            manage_dyn_prefix_component.transition_to([])

        removed_names = {call.parameter_name for call in recorder.calls}
        assert removed_names == {"dyn_a", "dyn_b"}
        assert all(isinstance(call, RemoveParameterFromNodeRequest) for call in recorder.calls)

    def test_preserve_only_dispatches_no_requests(
        self, host_node: MockNode, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        _add_param_to_node(host_node, "dyn_a", input_types=["str"])
        desired = [_transition_param("dyn_a", input_types=frozenset({"str"}), output_type="str")]
        recorder = _DispatchRecorder()

        with patch(_HANDLE_REQUEST_TARGET, side_effect=recorder):
            manage_dyn_prefix_component.transition_to(desired)

        assert recorder.calls == []
        # The factory must not be invoked when nothing is added.
        cast("Mock", desired[0].add_request_factory).assert_not_called()

    def test_replace_orders_capture_remove_add_recreate(
        self, host_node: MockNode, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        _add_param_to_node(host_node, "dyn_a", input_types=["str"])

        incoming = IncomingConnection(
            source_node_name="upstream", source_parameter_name="out", target_parameter_name="dyn_a"
        )
        outgoing = OutgoingConnection(
            source_parameter_name="dyn_a", target_node_name="downstream", target_parameter_name="in"
        )

        recorder = _DispatchRecorder()
        recorder.map_type(
            GetConnectionsForParameterRequest,
            _success_get_connections(
                parameter_name="dyn_a",
                node_name=host_node.name,
                incoming=[incoming],
                outgoing=[outgoing],
            ),
        )
        recorder.map_type(
            RemoveParameterFromNodeRequest,
            RemoveParameterFromNodeResultSuccess(result_details="ok"),
        )
        recorder.map_type(
            AddParameterToNodeRequest,
            AddParameterToNodeResultSuccess(
                parameter_name="dyn_a",
                type="int",
                node_name=host_node.name,
                result_details="ok",
            ),
        )
        # Two CreateConnectionRequest dispatches expected (one per captured edge).
        recorder.map_type(
            CreateConnectionRequest,
            CreateConnectionResultSuccess(result_details="ok"),
        )

        desired = [
            _transition_param("dyn_a", input_types=frozenset({"int"}), output_type="int"),
        ]

        with patch(_HANDLE_REQUEST_TARGET, side_effect=recorder):
            manage_dyn_prefix_component.transition_to(desired)

        request_types = [type(call) for call in recorder.calls]
        # Per-replace order: Get -> Remove -> Add -> CreateConnection x2.
        assert request_types == [
            GetConnectionsForParameterRequest,
            RemoveParameterFromNodeRequest,
            AddParameterToNodeRequest,
            CreateConnectionRequest,
            CreateConnectionRequest,
        ]

        # Verify the recreated connection requests carry the captured edge identities.
        create_calls = [call for call in recorder.calls if isinstance(call, CreateConnectionRequest)]
        incoming_recreated = next(call for call in create_calls if call.target_node_name == host_node.name)
        outgoing_recreated = next(call for call in create_calls if call.source_node_name == host_node.name)
        assert incoming_recreated.source_node_name == "upstream"
        assert incoming_recreated.source_parameter_name == "out"
        assert incoming_recreated.target_parameter_name == "dyn_a"
        assert outgoing_recreated.source_parameter_name == "dyn_a"
        assert outgoing_recreated.target_node_name == "downstream"
        assert outgoing_recreated.target_parameter_name == "in"

    def test_replace_with_no_connections_skips_recreate_step(
        self, host_node: MockNode, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        _add_param_to_node(host_node, "dyn_a", input_types=["str"])

        recorder = _DispatchRecorder()
        recorder.map_type(
            GetConnectionsForParameterRequest,
            _success_get_connections(parameter_name="dyn_a", node_name=host_node.name),
        )
        recorder.map_type(
            RemoveParameterFromNodeRequest,
            RemoveParameterFromNodeResultSuccess(result_details="ok"),
        )
        recorder.map_type(
            AddParameterToNodeRequest,
            AddParameterToNodeResultSuccess(
                parameter_name="dyn_a", type="int", node_name=host_node.name, result_details="ok"
            ),
        )

        desired = [_transition_param("dyn_a", input_types=frozenset({"int"}), output_type="int")]

        with patch(_HANDLE_REQUEST_TARGET, side_effect=recorder):
            manage_dyn_prefix_component.transition_to(desired)

        request_types = [type(call) for call in recorder.calls]
        assert request_types == [
            GetConnectionsForParameterRequest,
            RemoveParameterFromNodeRequest,
            AddParameterToNodeRequest,
        ]

    def test_replace_aborts_when_remove_fails(
        self, host_node: MockNode, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        """If RemoveParameter fails during a replace, the add and recreate steps must NOT run."""
        _add_param_to_node(host_node, "dyn_a", input_types=["str"])

        recorder = _DispatchRecorder()
        recorder.map_type(
            GetConnectionsForParameterRequest,
            _success_get_connections(parameter_name="dyn_a", node_name=host_node.name),
        )
        recorder.map_type(
            RemoveParameterFromNodeRequest,
            RemoveParameterFromNodeResultFailure(result_details="boom"),
        )

        desired = [_transition_param("dyn_a", input_types=frozenset({"int"}), output_type="int")]

        with patch(_HANDLE_REQUEST_TARGET, side_effect=recorder):
            manage_dyn_prefix_component.transition_to(desired)

        request_types = [type(call) for call in recorder.calls]
        assert request_types == [GetConnectionsForParameterRequest, RemoveParameterFromNodeRequest]
        # Factory must not be invoked when the remove blocks the replace.
        cast("Mock", desired[0].add_request_factory).assert_not_called()

    def test_replace_aborts_when_add_fails(
        self, host_node: MockNode, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        """If AddParameter fails during a replace, recreate-connections must NOT run."""
        _add_param_to_node(host_node, "dyn_a", input_types=["str"])

        recorder = _DispatchRecorder()
        recorder.map_type(
            GetConnectionsForParameterRequest,
            _success_get_connections(
                parameter_name="dyn_a",
                node_name=host_node.name,
                incoming=[
                    IncomingConnection(
                        source_node_name="upstream",
                        source_parameter_name="out",
                        target_parameter_name="dyn_a",
                    )
                ],
            ),
        )
        recorder.map_type(
            RemoveParameterFromNodeRequest,
            RemoveParameterFromNodeResultSuccess(result_details="ok"),
        )
        recorder.map_type(
            AddParameterToNodeRequest,
            AddParameterToNodeResultFailure(result_details="boom"),
        )

        desired = [_transition_param("dyn_a", input_types=frozenset({"int"}), output_type="int")]

        with patch(_HANDLE_REQUEST_TARGET, side_effect=recorder):
            manage_dyn_prefix_component.transition_to(desired)

        request_types = [type(call) for call in recorder.calls]
        assert request_types == [
            GetConnectionsForParameterRequest,
            RemoveParameterFromNodeRequest,
            AddParameterToNodeRequest,
        ]
        # No CreateConnectionRequest dispatched after the add failed.
        assert not any(isinstance(call, CreateConnectionRequest) for call in recorder.calls)

    def test_failed_capture_skips_recreate_but_continues_replace(
        self, host_node: MockNode, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        """If GetConnections fails, the replace still removes + adds; just no recreates."""
        _add_param_to_node(host_node, "dyn_a", input_types=["str"])

        recorder = _DispatchRecorder()
        recorder.map_type(
            GetConnectionsForParameterRequest,
            GetConnectionsForParameterResultFailure(result_details="lookup failed"),
        )
        recorder.map_type(
            RemoveParameterFromNodeRequest,
            RemoveParameterFromNodeResultSuccess(result_details="ok"),
        )
        recorder.map_type(
            AddParameterToNodeRequest,
            AddParameterToNodeResultSuccess(
                parameter_name="dyn_a", type="int", node_name=host_node.name, result_details="ok"
            ),
        )

        desired = [_transition_param("dyn_a", input_types=frozenset({"int"}), output_type="int")]

        with patch(_HANDLE_REQUEST_TARGET, side_effect=recorder):
            manage_dyn_prefix_component.transition_to(desired)

        request_types = [type(call) for call in recorder.calls]
        assert request_types == [
            GetConnectionsForParameterRequest,
            RemoveParameterFromNodeRequest,
            AddParameterToNodeRequest,
        ]
        assert not any(isinstance(call, CreateConnectionRequest) for call in recorder.calls)

    def test_remove_failure_does_not_block_other_removes(
        self, host_node: MockNode, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        """A pure-remove failure on one parameter still lets the others be removed."""
        _add_param_to_node(host_node, "dyn_a")
        _add_param_to_node(host_node, "dyn_b")

        def remove_response(request: RemoveParameterFromNodeRequest) -> Any:
            if request.parameter_name == "dyn_a":
                return RemoveParameterFromNodeResultFailure(result_details="boom")
            return RemoveParameterFromNodeResultSuccess(result_details="ok")

        recorder = _DispatchRecorder()
        recorder.map_type(RemoveParameterFromNodeRequest, remove_response)

        with patch(_HANDLE_REQUEST_TARGET, side_effect=recorder):
            manage_dyn_prefix_component.transition_to([])

        removed_names = sorted(
            call.parameter_name for call in recorder.calls if isinstance(call, RemoveParameterFromNodeRequest)
        )
        # Both removes were attempted regardless of the first one failing.
        assert removed_names == ["dyn_a", "dyn_b"]

    def test_add_failure_does_not_block_other_adds(
        self, host_node: MockNode, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        """A pure-add failure on one parameter still lets the others be added."""

        def add_response(request: AddParameterToNodeRequest) -> Any:
            if request.parameter_name == "dyn_a":
                return AddParameterToNodeResultFailure(result_details="boom")
            return AddParameterToNodeResultSuccess(
                parameter_name=request.parameter_name or "",
                type="str",
                node_name=host_node.name,
                result_details="ok",
            )

        recorder = _DispatchRecorder()
        recorder.map_type(AddParameterToNodeRequest, add_response)

        desired = [_transition_param("dyn_a"), _transition_param("dyn_b")]

        with patch(_HANDLE_REQUEST_TARGET, side_effect=recorder):
            manage_dyn_prefix_component.transition_to(desired)

        added_names = sorted(
            call.parameter_name
            for call in recorder.calls
            if isinstance(call, AddParameterToNodeRequest) and call.parameter_name is not None
        )
        assert added_names == ["dyn_a", "dyn_b"]

    def test_idempotent_call_dispatches_nothing_on_second_pass(
        self, host_node: MockNode, manage_dyn_prefix_component: ParameterTransitionComponent
    ) -> None:
        """A repeat transition_to call with the same desired set computes all-preserve and dispatches nothing."""
        _add_param_to_node(host_node, "dyn_a", input_types=["str"])
        desired = [_transition_param("dyn_a", input_types=frozenset({"str"}), output_type="str")]

        recorder = _DispatchRecorder()

        with patch(_HANDLE_REQUEST_TARGET, side_effect=recorder):
            first_plan = manage_dyn_prefix_component.transition_to(desired)
            second_plan = manage_dyn_prefix_component.transition_to(desired)

        # First call computed all-preserve (signatures matched), so zero dispatches.
        assert first_plan.to_preserve == frozenset({"dyn_a"})
        assert first_plan.to_replace == frozenset()
        # Second call must also be all-preserve and dispatch nothing.
        assert second_plan == first_plan
        assert recorder.calls == []
