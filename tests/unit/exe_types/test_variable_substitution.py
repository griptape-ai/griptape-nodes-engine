"""Tests for inline workflow variable substitution in get_parameter_value()."""

from contextlib import AbstractContextManager
from unittest.mock import MagicMock, patch

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import TrackedParameterOutputValues, aprocess_scope

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


def _make_property_output_param(name: str, default: str) -> Parameter:
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
    mock_gn.VariablesManager.return_value.get_variables_for_macro_resolution.return_value = variables
    mock_gn.SecretsManager.return_value = MagicMock()

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


def _run_tracked_set(node: MockNode, param_name: str, value: object, *, in_aprocess: bool) -> list:
    """Set a value on TrackedParameterOutputValues and return captured events."""
    tracked = TrackedParameterOutputValues(node)
    captured: list = []
    mock_gn = MagicMock()
    mock_gn.EventManager.return_value.put_event.side_effect = captured.append

    if in_aprocess:
        with patch(_GN_PATCH, mock_gn), aprocess_scope():
            tracked[param_name] = value
    else:
        with patch(_GN_PATCH, mock_gn):
            tracked[param_name] = value

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

        captured = _run_tracked_set(node, "text", "sc001", in_aprocess=True)

        assert _display_value_from_event(captured) == "{SHOT}"

    def test_ui_shows_computed_value_when_no_template(self) -> None:
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "hello"))
        node.parameter_values["text"] = "hello"

        captured = _run_tracked_set(node, "text", "hello", in_aprocess=True)

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

        captured = _run_tracked_set(node, "index_count", expected_count, in_aprocess=True)

        assert _display_value_from_event(captured) == expected_count

    def test_ui_suppression_only_active_during_aprocess(self) -> None:
        """Outside aprocess, the computed value is always emitted as-is."""
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"

        captured = _run_tracked_set(node, "text", "sc001", in_aprocess=False)

        assert _display_value_from_event(captured) == "sc001"

    def test_ui_shows_computed_when_raw_matches_output(self) -> None:
        """If the output value equals the raw template, no suppression — show normally."""
        node = MockNode(name="mock_node")
        node.add_parameter(_make_property_output_param("text", "{SHOT}"))
        node.parameter_values["text"] = "{SHOT}"

        captured = _run_tracked_set(node, "text", "{SHOT}", in_aprocess=True)

        assert _display_value_from_event(captured) == "{SHOT}"


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
