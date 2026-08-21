"""Tests for inline workflow variable substitution in get_parameter_value()."""

from contextlib import AbstractContextManager
from typing import Any
from unittest.mock import MagicMock, patch

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import TrackedParameterOutputValues, aprocess_scope
from griptape_nodes.exe_types.variable_resolver import _aprocess_variable_cache
from griptape_nodes.retained_mode.events.base_events import ProgressEvent
from griptape_nodes.retained_mode.events.execution_events import ParameterValueUpdateEvent
from griptape_nodes.retained_mode.events.parameter_events import AlterElementEvent
from griptape_nodes.retained_mode.events.variable_events import (
    ListVariablesRequest,
    ListVariablesResultSuccess,
)
from griptape_nodes.retained_mode.variable_types import FlowVariable, VariableLayerKind

from .mocks import MockNode

# GriptapeNodes is lazy-imported inside _param_has_incoming_connection and
# _resolve_variables_in_string to break the exe_types <-> retained_mode cycle.
# Patch it at the source module so the lazy `from ... import GriptapeNodes`
# picks up the mock at call time.
_GN_PATCH = "griptape_nodes.retained_mode.griptape_nodes.GriptapeNodes"


def _make_str_param(name: str, default: str = "", modes: set | None = None) -> Parameter:
    if modes is None:
        modes = {ParameterMode.INPUT, ParameterMode.PROPERTY}
    return Parameter(
        name=name,
        default_value=default,
        input_types=["str"],
        output_type="str",
        type="str",
        allowed_modes=modes,
        tooltip="test",
    )


def _make_property_output_param(name: str, default: Any) -> Parameter:
    return Parameter(
        name=name,
        default_value=default,
        input_types=["str"],
        output_type="str",
        type="str",
        allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
        tooltip="test",
    )


def _mock_gn(
    variables: dict,
    *,
    connected_params: set[str] | None = None,
    substitution_enabled: bool = True,
) -> AbstractContextManager:
    """Patch GriptapeNodes managers for substitution tests.

    connected_params: parameter names on "mock_node" that have incoming connections.
    substitution_enabled: value returned by is_variable_substitution_enabled().
    """
    if connected_params is None:
        connected_params = set()

    mock_gn = MagicMock()
    mock_gn.NodeManager.return_value.get_node_parent_flow_by_name.return_value = "test_flow"
    mock_gn.handle_request.side_effect = lambda req: (
        _list_variables_result(variables) if isinstance(req, ListVariablesRequest) else MagicMock()
    )

    incoming_index = {"mock_node": dict.fromkeys(connected_params, True)} if connected_params else {}
    mock_connections = MagicMock()
    mock_connections.incoming_index = incoming_index
    mock_gn.FlowManager.return_value.get_connections.return_value = mock_connections
    mock_gn.WorkflowManager.return_value.is_variable_substitution_enabled.return_value = substitution_enabled

    return patch(_GN_PATCH, mock_gn)


def _list_variables_result(variables: dict) -> ListVariablesResultSuccess:
    """Build the ListVariablesResultSuccess a real hierarchical walk would return for `variables`."""
    flow_vars = [
        FlowVariable(name=name, owning_flow_name="test_flow", type="str", value=value)
        for name, value in variables.items()
    ]
    return ListVariablesResultSuccess(
        variables=flow_vars,
        layers=[VariableLayerKind.FLOW] * len(flow_vars),
        result_details="ok",
    )


def _display_value_from_event(captured: list) -> object:
    """Extract the display value from the first captured put_event call."""
    assert len(captured) == 1
    return captured[0].wrapped_event.payload.element_details["value"]


def _payloads_of_type(captured: list, payload_type: type) -> list:
    """All captured payloads of a given event type, in emission order.

    Handles both shapes put on the event manager: payloads wrapped in
    ExecutionGriptapeNodeEvent (AlterElementEvent, ParameterValueUpdateEvent) and
    payloads emitted bare (ProgressEvent).
    """
    payloads = [e.wrapped_event.payload if hasattr(e, "wrapped_event") else e for e in captured]
    return [p for p in payloads if isinstance(p, payload_type)]


def _capturing_gn_mock(captured: list, variables: dict, *, connected_params: set[str] | None = None) -> Any:
    """A GN patch that captures events and makes every suppression gate explicit.

    Every gate in ``_variable_template_to_preserve`` is configured here rather than
    left to MagicMock's defaults. Leaving them implicit is how these tests used to
    pass: ``_param_has_incoming_connection`` did ``param_name in <MagicMock>``, which
    is False only because ``MagicMock.__contains__`` defaults to False, so any
    refactor of that lookup would flip every assertion for the wrong reason.
    """
    mock_gn = MagicMock()
    mock_gn.NodeManager.return_value.get_node_parent_flow_by_name.return_value = "test_flow"
    mock_gn.handle_request.side_effect = lambda req: (
        _list_variables_result(variables) if isinstance(req, ListVariablesRequest) else MagicMock()
    )
    incoming_index = {"mock_node": dict.fromkeys(connected_params, True)} if connected_params else {}
    mock_gn.FlowManager.return_value.get_connections.return_value = MagicMock(incoming_index=incoming_index)
    mock_gn.WorkflowManager.return_value.is_variable_substitution_enabled.return_value = True
    mock_gn.EventManager.return_value.put_event.side_effect = captured.append
    return patch(_GN_PATCH, mock_gn)


def _run_tracked_set(
    node: MockNode,
    param_name: str,
    value: object,
    *,
    in_aprocess: bool,
    variables: dict | None = None,
) -> tuple[list, TrackedParameterOutputValues]:
    """Set a value on TrackedParameterOutputValues and return (events, tracker).

    `variables` defaults to empty, which leaves substitution enabled but with nothing
    to substitute -- what display-suppression-only tests want, since the values they
    set contain no resolvable {VAR} references.
    """
    tracked = TrackedParameterOutputValues(node)
    captured: list = []
    ctx = _capturing_gn_mock(captured, variables if variables is not None else {})

    if in_aprocess:
        with ctx, aprocess_scope():
            tracked[param_name] = value
    else:
        with ctx:
            tracked[param_name] = value

    return captured, tracked


def _run_publish_update(
    node: MockNode,
    param_name: str,
    value: object,
    *,
    in_aprocess: bool,
    variables: dict | None = None,
) -> list:
    """Call publish_update_to_parameter and return the captured put_event calls."""
    captured: list = []
    ctx = _capturing_gn_mock(captured, variables if variables is not None else {})

    if in_aprocess:
        with ctx, aprocess_scope():
            node.publish_update_to_parameter(param_name, value)
    else:
        with ctx:
            node.publish_update_to_parameter(param_name, value)

    return captured


class TestVariableSubstitutionDuringExecution:
    """Variable substitution only fires inside aprocess_scope."""

    def test_substitutes_known_variable_during_aprocess(self) -> None:
        node = MockNode(name="mock_node")
        node.add_parameter(_make_str_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"

        with _mock_gn({"SHOT": "sc001"}), aprocess_scope():
            value = node.get_parameter_value("text")

        assert value == "sc001"

    def test_no_substitution_outside_aprocess(self) -> None:
        node = MockNode(name="mock_node")
        node.add_parameter(_make_str_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"

        with _mock_gn({"SHOT": "sc001"}):
            value = node.get_parameter_value("text")

        assert value == "{SHOT}"

    def test_substitutes_multiple_variables(self) -> None:
        node = MockNode(name="mock_node")
        node.add_parameter(_make_str_param("text", "{SHOW}_{SHOT}"))
        node.parameter_values["text"] = "{SHOW}_{SHOT}"

        with _mock_gn({"SHOW": "myshow", "SHOT": "sc001"}), aprocess_scope():
            value = node.get_parameter_value("text")

        assert value == "myshow_sc001"

    def test_partial_substitution_leaves_unknown_variable(self) -> None:
        node = MockNode(name="mock_node")
        node.add_parameter(_make_str_param("text", "{KNOWN}_{UNKNOWN}"))
        node.parameter_values["text"] = "{KNOWN}_{UNKNOWN}"

        with _mock_gn({"KNOWN": "hello"}), aprocess_scope():
            value = node.get_parameter_value("text")

        assert value == "hello_{UNKNOWN}"

    def test_no_substitution_when_no_variables_defined(self) -> None:
        node = MockNode(name="mock_node")
        node.add_parameter(_make_str_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"

        with _mock_gn({}), aprocess_scope():
            value = node.get_parameter_value("text")

        assert value == "{SHOT}"

    def test_plain_string_untouched(self) -> None:
        node = MockNode(name="mock_node")
        node.add_parameter(_make_str_param("text", "hello world"))
        node.parameter_values["text"] = "hello world"

        with _mock_gn({"SHOT": "sc001"}), aprocess_scope():
            value = node.get_parameter_value("text")

        assert value == "hello world"

    def test_format_spec_applied(self) -> None:
        node = MockNode(name="mock_node")
        node.add_parameter(_make_str_param("text", "{SHOT:upper}"))
        node.parameter_values["text"] = "{SHOT:upper}"

        with _mock_gn({"SHOT": "sc001"}), aprocess_scope():
            value = node.get_parameter_value("text")

        assert value == "SC001"

    def test_invalid_syntax_passes_through(self) -> None:
        node = MockNode(name="mock_node")
        node.add_parameter(_make_str_param("text", "hello } world"))
        node.parameter_values["text"] = "hello } world"

        with _mock_gn({"SHOT": "sc001"}), aprocess_scope():
            value = node.get_parameter_value("text")

        assert value == "hello } world"

    def test_variable_inside_json_string_is_substituted(self) -> None:
        """Variables embedded inside JSON values should be resolved.

        Previously the outer JSON braces caused a MacroSyntaxError that silently
        swallowed the substitution.
        """
        node = MockNode(name="mock_node")
        node.add_parameter(_make_str_param("text", '{"status": "{STATUS}"}'))
        node.parameter_values["text"] = '{"status": "{STATUS}"}'

        with _mock_gn({"STATUS": "active"}), aprocess_scope():
            value = node.get_parameter_value("text")

        assert value == '{"status": "active"}'

    def test_plain_json_without_variables_is_not_mangled(self) -> None:
        """A JSON string with no variable references must pass through unchanged."""
        node = MockNode(name="mock_node")
        raw = '{"key": "value"}'
        node.add_parameter(_make_str_param("text", raw))
        node.parameter_values["text"] = raw

        with _mock_gn({"STATUS": "active"}), aprocess_scope():
            value = node.get_parameter_value("text")

        assert value == raw

    def test_dict_value_not_substituted_at_get_level(self) -> None:
        """Dict parameters are NOT substituted at get_parameter_value time.

        Substitution for dicts happens in TrackedParameterOutputValues so that the
        node's internal view of its own property is the raw template (unchanged),
        while downstream nodes receive the resolved value.
        """
        node = MockNode(name="mock_node")
        node.add_parameter(_make_str_param("data", ""))
        node.parameter_values["data"] = {"char": "{CHAR}", "count": 1}

        with _mock_gn({"CHAR": "carl"}), aprocess_scope():
            value = node.get_parameter_value("data")

        assert value == {"char": "{CHAR}", "count": 1}

    def test_uses_default_value_when_no_parameter_value_set(self) -> None:
        node = MockNode(name="mock_node")
        node.add_parameter(_make_str_param("text", "{SHOT}"))
        # parameter_values not set — falls back to default_value

        with _mock_gn({"SHOT": "sc001"}), aprocess_scope():
            value = node.get_parameter_value("text")

        assert value == "sc001"


class TestVariableSubstitutionConnectionGating:
    """Substitution must not run on parameters that receive values from upstream nodes."""

    def test_no_substitution_when_parameter_has_incoming_connection(self) -> None:
        node = MockNode(name="mock_node")
        node.add_parameter(_make_str_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"

        with _mock_gn({"SHOT": "sc001"}, connected_params={"text"}), aprocess_scope():
            value = node.get_parameter_value("text")

        assert value == "{SHOT}"

    def test_substitution_when_different_param_has_connection(self) -> None:
        node = MockNode(name="mock_node")
        node.add_parameter(_make_str_param("text", "{SHOT}"))
        node.add_parameter(_make_str_param("other", "untouched"))
        node.parameter_values["text"] = "{SHOT}"

        with _mock_gn({"SHOT": "sc001"}, connected_params={"other"}), aprocess_scope():
            value = node.get_parameter_value("text")

        assert value == "sc001"


class TestVariableSubstitutionFallbacks:
    """Substitution degrades gracefully when managers are unavailable."""

    def test_node_not_in_flow_returns_raw(self) -> None:
        node = MockNode(name="mock_node")
        node.add_parameter(_make_str_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"

        mock_gn = MagicMock()
        mock_gn.NodeManager.return_value.get_node_parent_flow_by_name.side_effect = KeyError("mock_node")
        mock_connections = MagicMock()
        mock_connections.incoming_index = {}
        mock_gn.FlowManager.return_value.get_connections.return_value = mock_connections

        with patch(_GN_PATCH, mock_gn), aprocess_scope():
            value = node.get_parameter_value("text")

        assert value == "{SHOT}"


class TestTrackedOutputValuesDisplayDuringSubstitution:
    """TrackedParameterOutputValues must not overwrite the template in the UI.

    The display suppression logic lives inside _emit_parameter_change_event, so
    these tests let that method run its real logic and instead mock only the
    final put_event call. The display value is read back from the captured event's
    element_details dict.
    """

    def test_ui_shows_template_not_substituted_value(self) -> None:
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"

        captured, _ = _run_tracked_set(node, "text", "sc001", in_aprocess=True)

        assert _display_value_from_event(captured) == "{SHOT}"

    def test_ui_shows_computed_value_when_no_template(self) -> None:
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "hello"))
        node.parameter_values["text"] = "hello"

        captured, _ = _run_tracked_set(node, "text", "hello", in_aprocess=True)

        assert _display_value_from_event(captured) == "hello"

    def test_loop_counter_shows_computed_value(self) -> None:
        """PROPERTY|OUTPUT integer parameters (e.g. index_count) must not be suppressed."""
        expected_count = 3
        node = MockNode(name="mock_node")
        node.add_parameter(
            Parameter(
                name="index_count",
                default_value=0,
                input_types=["int"],
                output_type="int",
                type="int",
                allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
                tooltip="test",
            )
        )
        node.parameter_values["index_count"] = 0

        captured, _ = _run_tracked_set(node, "index_count", expected_count, in_aprocess=True)

        assert _display_value_from_event(captured) == expected_count

    def test_ui_shows_template_outside_aprocess(self) -> None:
        """Suppression must not depend on aprocess_scope.

        The orchestrator copies worker/group outputs back into
        parameter_output_values *after* aprocess_scope has exited. Gating
        suppression on _in_aprocess made that copy-back emit the resolved
        value, so a node inside a ForEach/ForLoop group showed the last
        iteration's substituted text instead of the template.
        """
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"

        captured, _ = _run_tracked_set(node, "text", "sc001", in_aprocess=False)

        assert _display_value_from_event(captured) == "{SHOT}"

    def test_stored_output_keeps_resolved_value_outside_aprocess(self) -> None:
        """Suppression is display-only: the copied-back output value is untouched."""
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"

        _, tracked = _run_tracked_set(node, "text", "sc001", in_aprocess=False)

        assert tracked["text"] == "sc001"

    def test_ui_shows_computed_when_raw_matches_output(self) -> None:
        """If the output value equals the raw template, no suppression — show normally."""
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"

        captured, _ = _run_tracked_set(node, "text", "{SHOT}", in_aprocess=True)

        assert _display_value_from_event(captured) == "{SHOT}"

    def test_ui_shows_template_dict_not_substituted_dict(self) -> None:
        """When a PROPERTY|OUTPUT dict parameter contains variable macros, the UI must show the raw template.

        Previously the display suppression only applied to str parameters; dicts were
        always shown with their substituted output value.
        """
        node = MockNode(name="mock_node")
        raw = {"char": "{CHAR}"}
        node.add_parameter(_make_property_output_param("data", raw))
        node.parameter_values["data"] = raw

        captured, _ = _run_tracked_set(node, "data", {"char": "carl"}, in_aprocess=True)

        assert _display_value_from_event(captured) == raw

    def test_ui_shows_computed_value_for_plain_json_output(self) -> None:
        """A JSON string with no variable macros must not trigger display suppression.

        The old ``'{' in raw_value`` heuristic would incorrectly suppress the
        display for any string containing a ``{``, including plain JSON.  The
        new ``_HAS_VARIABLE_MACRO.search`` check only suppresses when the raw
        value contains ``{Letter`` (a potential variable reference).
        """
        node = MockNode(name="mock_node")
        raw = '{"key": "value"}'
        node.add_parameter(_make_property_output_param("text", raw))
        node.parameter_values["text"] = raw

        # Pretend the node computed something different (e.g. a transformed value)
        captured, _ = _run_tracked_set(node, "text", '{"key": "transformed"}', in_aprocess=True)

        assert _display_value_from_event(captured) == '{"key": "transformed"}'

    def test_dict_output_is_substituted_for_downstream(self) -> None:
        """Dict output goes through substitution so downstream nodes receive resolved values.

        JSON Input stores its template as a dict; the node reads the raw template
        but TrackedParameterOutputValues substitutes variables before propagation.
        """
        node = MockNode(name="mock_node")
        raw = {"char": "{CHAR}", "count": 1}
        node.add_parameter(_make_property_output_param("data", raw))
        node.parameter_values["data"] = raw

        _, tracked = _run_tracked_set(node, "data", raw, in_aprocess=True, variables={"CHAR": "carl"})

        assert tracked["data"] == {"char": "carl", "count": 1}

    def test_nested_dict_output_is_substituted(self) -> None:
        """Substitution recurses into nested dict output values."""
        node = MockNode(name="mock_node")
        raw = {"outer": {"inner": "{CHAR}"}}
        node.add_parameter(_make_property_output_param("data", raw))
        node.parameter_values["data"] = raw

        _, tracked = _run_tracked_set(node, "data", raw, in_aprocess=True, variables={"CHAR": "carl"})

        assert tracked["data"] == {"outer": {"inner": "carl"}}

    def test_list_output_is_substituted(self) -> None:
        """List output values have their string items substituted."""
        node = MockNode(name="mock_node")
        raw = ["{CHAR}", "literal", 42]
        node.add_parameter(_make_property_output_param("data", raw))
        node.parameter_values["data"] = raw

        _, tracked = _run_tracked_set(node, "data", raw, in_aprocess=True, variables={"CHAR": "carl"})

        assert tracked["data"] == ["carl", "literal", 42]

    def test_dict_output_ui_shows_template_not_substituted(self) -> None:
        """When dict output is substituted, the UI event still shows the raw template."""
        node = MockNode(name="mock_node")
        raw = {"char": "{CHAR}"}
        node.add_parameter(_make_property_output_param("data", raw))
        node.parameter_values["data"] = raw

        captured, _ = _run_tracked_set(node, "data", raw, in_aprocess=True, variables={"CHAR": "carl"})

        assert _display_value_from_event(captured) == raw

    def test_no_substitution_outside_aprocess_for_dict(self) -> None:
        """Dict output values are NOT substituted outside aprocess_scope."""
        node = MockNode(name="mock_node")
        raw = {"char": "{CHAR}"}
        node.add_parameter(_make_property_output_param("data", raw))
        node.parameter_values["data"] = raw

        _, tracked = _run_tracked_set(node, "data", raw, in_aprocess=False, variables={"CHAR": "carl"})

        assert tracked["data"] == {"char": "{CHAR}"}


class TestGetDisplayValueForOutput:
    """get_display_value_for_output returns the UI display value WITHOUT modifying stored output values.

    Setup uses dict.__setitem__ directly to bypass TrackedParameterOutputValues event
    emission (which requires GriptapeNodes). We're testing the read-only display logic,
    not the event path.
    """

    def _seed_output(self, node: MockNode, name: str, value: Any) -> None:
        """Write directly into the underlying dict to avoid GriptapeNodes event machinery."""
        dict.__setitem__(node.parameter_output_values, name, value)

    def test_returns_template_for_property_param_with_macro(self) -> None:
        """Display value is the template; the stored output is the substituted value."""
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"
        self._seed_output(node, "text", "25")

        display = node.get_display_value_for_output("text", "25")

        assert display == "{SHOT}"
        # Stored output value must NOT be overwritten — downstream nodes still read "25".
        assert node.parameter_output_values["text"] == "25"

    def test_stored_output_unchanged_after_display_suppression(self) -> None:
        """Calling get_display_value_for_output is read-only: parameter_output_values is preserved."""
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"
        self._seed_output(node, "text", "sc001")

        node.get_display_value_for_output("text", "sc001")

        assert node.parameter_output_values["text"] == "sc001"

    def test_returns_output_when_no_macro_in_template(self) -> None:
        """No suppression when the raw parameter value contains no variable macro."""
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "hello"))
        node.parameter_values["text"] = "hello"

        display = node.get_display_value_for_output("text", "computed")

        assert display == "computed"

    def test_returns_output_for_input_only_param(self) -> None:
        """Non-PROPERTY parameters are never suppressed even if template has a macro."""
        node = MockNode(name="mock_node")
        param = Parameter(
            name="text",
            default_value="{SHOT}",
            input_types=["str"],
            output_type="str",
            type="str",
            allowed_modes={ParameterMode.INPUT},
            tooltip="test",
        )
        node.add_parameter(param)
        node.parameter_values["text"] = "{SHOT}"

        display = node.get_display_value_for_output("text", "25")

        assert display == "25"

    def test_returns_output_when_template_matches_output(self) -> None:
        """If the output already equals the template, no suppression is needed."""
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"

        display = node.get_display_value_for_output("text", "{SHOT}")

        assert display == "{SHOT}"

    def test_returns_template_for_dict_property_param_with_macro(self) -> None:
        """Dict PROPERTY params with macros also return the raw template for display."""
        node = MockNode(name="mock_node")
        raw = {"char": "{CHAR}"}
        node.add_parameter(_make_property_output_param("data", raw))
        node.parameter_values["data"] = raw
        substituted = {"char": "carl"}
        self._seed_output(node, "data", substituted)

        display = node.get_display_value_for_output("data", substituted)

        assert display == raw
        assert node.parameter_output_values["data"] == substituted


class TestPublishUpdateToParameterDisplay:
    """publish_update_to_parameter must not leak the resolved value to the UI.

    It emits two events for one call: writing parameter_output_values fires the
    guarded AlterElementEvent, then ParameterValueUpdateEvent follows. If the
    second one carries the raw value it overwrites the template the first one
    just set, so both have to agree.
    """

    def test_parameter_value_update_event_shows_template(self) -> None:
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"

        captured = _run_publish_update(node, "text", "sc001", in_aprocess=True)

        updates = _payloads_of_type(captured, ParameterValueUpdateEvent)
        assert [u.value for u in updates] == ["{SHOT}"]

    def test_all_emitted_events_agree_on_the_template(self) -> None:
        """No event in the batch may carry the resolved value, whatever the order."""
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"

        captured = _run_publish_update(node, "text", "sc001", in_aprocess=True)

        alters = _payloads_of_type(captured, AlterElementEvent)
        updates = _payloads_of_type(captured, ParameterValueUpdateEvent)
        assert [a.element_details["value"] for a in alters] == ["{SHOT}"]
        assert [u.value for u in updates] == ["{SHOT}"]

    def test_stored_output_keeps_resolved_value(self) -> None:
        """Suppression is display-only: downstream nodes still get the resolved value."""
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"

        _run_publish_update(node, "text", "sc001", in_aprocess=True)

        assert node.parameter_output_values["text"] == "sc001"

    def test_shows_template_outside_aprocess(self) -> None:
        """Group/worker execution publishes after aprocess_scope has exited."""
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"

        captured = _run_publish_update(node, "text", "sc001", in_aprocess=False)

        updates = _payloads_of_type(captured, ParameterValueUpdateEvent)
        assert [u.value for u in updates] == ["{SHOT}"]

    def test_computed_value_published_when_no_template(self) -> None:
        """Parameters without a macro publish their real value as before."""
        expected = 3
        node = MockNode(name="mock_node")
        node.add_parameter(
            Parameter(
                name="index_count",
                default_value=0,
                input_types=["int"],
                output_type="int",
                type="int",
                allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
                tooltip="test",
            )
        )
        node.parameter_values["index_count"] = 0

        captured = _run_publish_update(node, "index_count", expected, in_aprocess=True)

        updates = _payloads_of_type(captured, ParameterValueUpdateEvent)
        assert [u.value for u in updates] == [expected]


class TestTemplatePreservationGates:
    """A template is only preserved where substitution would actually have replaced it.

    `should_preserve_stored_template` gates a *write* to parameter_values, so a
    condition missing here does not just misdraw a field -- it silently drops a
    legitimate stored-value update on the group copy-back path.
    """

    def test_no_preservation_when_substitution_not_allowed(self) -> None:
        """allow_variable_substitution=False means the macro is never resolved."""
        node = MockNode(name="mock_node")
        node.add_parameter(
            Parameter(
                name="text",
                default_value="{SHOT}",
                input_types=["str"],
                output_type="str",
                type="str",
                allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
                allow_variable_substitution=False,
                tooltip="test",
            )
        )
        node.parameter_values["text"] = "{SHOT}"

        with _mock_gn({"SHOT": "sc001"}):
            assert node.get_display_value_for_output("text", "SOMETHING ELSE") == "SOMETHING ELSE"
            assert node.should_preserve_stored_template("text", "SOMETHING ELSE") is False

    def test_no_preservation_when_param_has_incoming_connection(self) -> None:
        """A connected parameter is fed by upstream, so its stored text is stale, not a template."""
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"

        with _mock_gn({"SHOT": "sc001"}, connected_params={"text"}):
            assert node.get_display_value_for_output("text", "from_upstream") == "from_upstream"
            assert node.should_preserve_stored_template("text", "from_upstream") is False

    def test_no_preservation_when_substitution_disabled(self) -> None:
        """The per-workflow toggle governs the write guard, not just get_parameter_value."""
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"

        with _mock_gn({"SHOT": "sc001"}, substitution_enabled=False):
            assert node.get_display_value_for_output("text", "sc001") == "sc001"
            assert node.should_preserve_stored_template("text", "sc001") is False

    def test_preservation_for_plain_template_param(self) -> None:
        """The positive case: an unconnected, substitution-enabled PROPERTY template."""
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"

        with _mock_gn({"SHOT": "sc001"}):
            assert node.should_preserve_stored_template("text", "sc001") is True

    def test_write_guard_declines_text_that_only_looks_templated(self) -> None:
        r"""`{color: red}` matches the `\{[A-Za-z_]` heuristic but names no variable.

        Display may still suppress on the heuristic -- a misdrawn field is cosmetic
        -- but declining the stored-state write would freeze the value for good, so
        the write guard has to be exact.
        """
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "body {color: red}"))
        node.parameter_values["text"] = "body {color: red}"

        with _mock_gn({"SHOT": "sc001"}):
            assert node.should_preserve_stored_template("text", "computed") is False

    def test_write_guard_preserves_template_naming_an_undefined_variable(self) -> None:
        """A template naming a variable the user has not created yet is still theirs.

        `_substitution_would_rewrite` is checked against the live variable set, so
        this only holds because SHOT is defined -- see the sibling test for the
        genuinely-unknown name.
        """
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "{SHOT} and {NOT_YET}"))
        node.parameter_values["text"] = "{SHOT} and {NOT_YET}"

        with _mock_gn({"SHOT": "sc001"}):
            assert node.should_preserve_stored_template("text", "sc001 and {NOT_YET}") is True

    def test_write_guard_declines_template_whose_only_variable_is_unknown(self) -> None:
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "{NOT_A_VARIABLE}"))
        node.parameter_values["text"] = "{NOT_A_VARIABLE}"

        with _mock_gn({"SHOT": "sc001"}):
            # Display still suppresses on the heuristic; only the write guard is exact.
            assert node.get_display_value_for_output("text", "computed") == "{NOT_A_VARIABLE}"
            assert node.should_preserve_stored_template("text", "computed") is False

    def test_write_guard_preserves_optional_template_for_undefined_variable(self) -> None:
        """`{SHOT?}` substitutes to "" whether or not SHOT exists, so it is a real rewrite.

        Requiring the name to be *defined* would decline the write guard here while
        display still suppressed, and the copy-back would then store the empty
        resolved text over the user's template -- unrecoverable.
        """
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "a {SHOT?} b"))
        node.parameter_values["text"] = "a {SHOT?} b"

        with _mock_gn({"OTHER": "x"}):
            # resolve_macro_token drops a missing optional token, so the output differs.
            assert node.get_display_value_for_output("text", "a  b") == "a {SHOT?} b"
            assert node.should_preserve_stored_template("text", "a  b") is True

    def test_write_guard_preserves_optional_template_for_defined_variable(self) -> None:
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "{SHOT?}"))
        node.parameter_values["text"] = "{SHOT?}"

        with _mock_gn({"SHOT": "sc001"}):
            assert node.should_preserve_stored_template("text", "sc001") is True

    def test_write_guard_leaves_no_variable_cache_behind(self) -> None:
        """The guard runs outside aprocess_scope, so its lookup must not memoise.

        ``get_variables_if_enabled`` caches into a ContextVar with no reset token.
        Calling it from here would pin that dict onto the ambient context, and every
        later read on the same task -- the next node's copy-back, or the next test in
        this worker -- would silently reuse it.
        """
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"

        assert _aprocess_variable_cache.get() is None
        with _mock_gn({"SHOT": "sc001"}):
            assert node.should_preserve_stored_template("text", "sc001") is True
        assert _aprocess_variable_cache.get() is None

    def test_incomparable_output_does_not_raise(self) -> None:
        """`!=` returning a non-bool must not propagate out of the guard.

        A node is free to emit a value whose __ne__ is elementwise (numpy array,
        DataFrame). This comparison runs inside parameter_output_values.__setitem__,
        so raising here would fail the node's execution.
        """

        class _Elementwise:
            def __ne__(self, other: object) -> Any:
                msg = "truth value of an array with more than one element is ambiguous"
                raise ValueError(msg)

            def __hash__(self) -> int:
                return 0

        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"
        output = _Elementwise()

        with _mock_gn({"SHOT": "sc001"}):
            assert node.get_display_value_for_output("text", output) is output
            assert node.should_preserve_stored_template("text", output) is False


class TestAppendValueToParameterDisplay:
    """Streamed deltas must not rebuild the resolved text over a preserved template."""

    def test_no_progress_event_when_template_preserved(self) -> None:
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"

        captured: list = []
        with _capturing_gn_mock(captured, {"SHOT": "sc001"}):
            node.append_value_to_parameter("text", "sc0")
            node.append_value_to_parameter("text", "01")

        assert _payloads_of_type(captured, ProgressEvent) == []
        # The accumulated output value is still correct for downstream nodes.
        assert node.parameter_output_values["text"] == "sc001"

    def test_progress_event_still_emitted_without_template(self) -> None:
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "plain"))
        node.parameter_values["text"] = "plain"

        captured: list = []
        with _capturing_gn_mock(captured, {"SHOT": "sc001"}):
            node.append_value_to_parameter("text", "chunk")

        assert [p.value for p in _payloads_of_type(captured, ProgressEvent)] == ["chunk"]

    def test_no_progress_event_when_only_display_suppresses(self) -> None:
        """Streaming must follow the *display* predicate, not the narrower write guard.

        `{NOT_A_VARIABLE}` suppresses the AlterElementEvent (display is deliberately
        loose) but does not pass the write guard. Asking the write guard here would
        let the deltas through and rebuild the resolved text over a field the UI was
        just told to keep -- the original leak, one chunk at a time.
        """
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "{NOT_A_VARIABLE}"))
        node.parameter_values["text"] = "{NOT_A_VARIABLE}"

        captured: list = []
        with _capturing_gn_mock(captured, {"SHOT": "sc001"}):
            node.append_value_to_parameter("text", "chu")
            node.append_value_to_parameter("text", "nk")
            # The two predicates genuinely disagree here; streaming follows display.
            assert node.should_preserve_stored_template("text", "chunk") is False

        assert _payloads_of_type(captured, ProgressEvent) == []

    def test_no_progress_event_when_template_preserved_inside_aprocess(self) -> None:
        """The shape that actually ships: streaming only ever happens inside aprocess.

        The sibling tests run outside aprocess_scope, which real streaming never
        does. Inside the scope __setitem__ also resolves the accumulated value, so
        this additionally pins that the suppression survives that path.
        """
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"

        captured: list = []
        with _capturing_gn_mock(captured, {"SHOT": "sc001"}), aprocess_scope({"SHOT": "sc001"}):
            node.append_value_to_parameter("text", "sc0")
            node.append_value_to_parameter("text", "01")

        assert _payloads_of_type(captured, ProgressEvent) == []
        assert node.parameter_output_values["text"] == "sc001"


class TestVariableSubstitutionDisableToggle:
    """When variable_substitution_enabled is False on the workflow, substitution is skipped."""

    def test_no_substitution_when_disabled(self) -> None:
        node = MockNode(name="mock_node")
        node.add_parameter(_make_str_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"

        with _mock_gn({"SHOT": "sc001"}, substitution_enabled=False), aprocess_scope():
            value = node.get_parameter_value("text")

        assert value == "{SHOT}"

    def test_substitution_still_works_when_enabled(self) -> None:
        """Sanity-check: the same setup with enabled=True substitutes normally."""
        node = MockNode(name="mock_node")
        node.add_parameter(_make_str_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"

        with _mock_gn({"SHOT": "sc001"}), aprocess_scope():
            value = node.get_parameter_value("text")

        assert value == "sc001"


class TestOptionalVariableSubstitution:
    """{VAR?} tokens are omitted (empty string) when the variable is absent."""

    def test_optional_var_omitted_when_missing(self) -> None:
        node = MockNode(name="mock_node")
        node.add_parameter(_make_str_param("text", "Hello {title?} {name}"))
        node.parameter_values["text"] = "Hello {title?} {name}"

        with _mock_gn({"name": "Jason"}), aprocess_scope():
            value = node.get_parameter_value("text")

        assert value == "Hello  Jason"

    def test_optional_var_substituted_when_present(self) -> None:
        node = MockNode(name="mock_node")
        node.add_parameter(_make_str_param("text", "Hello {title?} {name}"))
        node.parameter_values["text"] = "Hello {title?} {name}"

        with _mock_gn({"title": "Dr.", "name": "Jason"}), aprocess_scope():
            value = node.get_parameter_value("text")

        assert value == "Hello Dr. Jason"

    def test_required_var_leaves_token_when_missing(self) -> None:
        """Required {VAR} tokens stay as-is when the variable is absent."""
        node = MockNode(name="mock_node")
        node.add_parameter(_make_str_param("text", "Hello {name}"))
        node.parameter_values["text"] = "Hello {name}"

        with _mock_gn({}), aprocess_scope():
            value = node.get_parameter_value("text")

        assert value == "Hello {name}"
