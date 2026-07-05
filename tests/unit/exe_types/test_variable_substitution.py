"""Tests for inline workflow variable substitution in get_parameter_value()."""

from contextlib import AbstractContextManager
from typing import Any
from unittest.mock import MagicMock, patch

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import TrackedParameterOutputValues, aprocess_scope
from griptape_nodes.retained_mode.events.variable_events import (
    ResolveSubstitutionRequest,
    ResolveSubstitutionResultSuccess,
)

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
        ResolveSubstitutionResultSuccess(variables=variables, result_details="ok")
        if isinstance(req, ResolveSubstitutionRequest)
        else MagicMock()
    )

    incoming_index = {"mock_node": dict.fromkeys(connected_params, True)} if connected_params else {}
    mock_connections = MagicMock()
    mock_connections.incoming_index = incoming_index
    mock_gn.FlowManager.return_value.get_connections.return_value = mock_connections
    mock_gn.WorkflowManager.return_value.is_variable_substitution_enabled.return_value = substitution_enabled

    return patch(_GN_PATCH, mock_gn)


def _display_value_from_event(captured: list) -> object:
    """Extract the display value from the first captured put_event call."""
    assert len(captured) == 1
    return captured[0].wrapped_event.payload.element_details["value"]


def _run_tracked_set(
    node: MockNode,
    param_name: str,
    value: object,
    *,
    in_aprocess: bool,
    variables: dict | None = None,
) -> tuple[list, TrackedParameterOutputValues]:
    """Set a value on TrackedParameterOutputValues and return (events, tracker).

    When `variables` is provided, a full GN mock (substitution + event capture) is
    used. Otherwise only EventManager is mocked (for display-suppression-only tests
    where the values set contain no {Letter} patterns and thus bypass substitution).
    """
    tracked = TrackedParameterOutputValues(node)
    captured: list = []

    if variables is not None:
        # Build a unified mock that handles both substitution and event capture.
        mock_gn = MagicMock()
        mock_gn.NodeManager.return_value.get_node_parent_flow_by_name.return_value = "test_flow"
        mock_gn.handle_request.side_effect = lambda req: (
            ResolveSubstitutionResultSuccess(variables=variables, result_details="ok")
            if isinstance(req, ResolveSubstitutionRequest)
            else MagicMock()
        )
        mock_gn.FlowManager.return_value.get_connections.return_value = MagicMock(incoming_index={})
        mock_gn.WorkflowManager.return_value.is_variable_substitution_enabled.return_value = True
        mock_gn.EventManager.return_value.put_event.side_effect = captured.append
        ctx: Any = patch(_GN_PATCH, mock_gn)
    else:
        minimal_mock = MagicMock()
        minimal_mock.EventManager.return_value.put_event.side_effect = captured.append
        ctx = patch(_GN_PATCH, minimal_mock)

    if in_aprocess:
        with ctx, aprocess_scope():
            tracked[param_name] = value
    else:
        with ctx:
            tracked[param_name] = value

    return captured, tracked


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

    def test_ui_suppression_only_active_during_aprocess(self) -> None:
        """Outside aprocess, the computed value is always emitted as-is."""
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"

        captured, _ = _run_tracked_set(node, "text", "sc001", in_aprocess=False)

        assert _display_value_from_event(captured) == "sc001"

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
