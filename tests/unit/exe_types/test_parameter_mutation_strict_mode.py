"""Tests for the parameter-mutation-during-aprocess strict-mode detector.

The detector lives in ``BaseNode.add_parameter`` and
``BaseNode.remove_parameter_element``. It fires when a node mutates its
own parameter list while ``aprocess_scope()`` is active (set only around
``await node.aprocess()``) and the call is not wrapped by the handler-side
``sanctioned_parameter_mutation()`` context.
"""

from __future__ import annotations

import pytest

from griptape_nodes.common.strict_mode import STRICT_MODE, StrictModeScopeKind
from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.exe_types.node_types import aprocess_scope, sanctioned_parameter_mutation
from griptape_nodes.node_library.library_registry import LibraryRegistry, _constructing_node

from .mocks import MockNode


@pytest.fixture
def mock_node() -> MockNode:
    """Return a fresh MockNode for each test."""
    return MockNode(name="detector_test_node")


class TestParameterMutationDetector:
    def test_no_violation_when_no_scope_active(self, mock_node: MockNode) -> None:
        param = Parameter(name="p1", type="str")
        mock_node.add_parameter(param)
        # Outside a strict-mode scope nothing is tracked.
        # The add_parameter call should succeed without raising.

    def test_violation_reported_when_adding_parameter_inside_aprocess_scope(self, mock_node: MockNode) -> None:
        param = Parameter(name="p_added", type="str")
        with (
            STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.RUNTIME_EXECUTE,
                subject=mock_node.name,
                library_name=None,
                is_worker=False,
            ) as scope,
            aprocess_scope(),
        ):
            mock_node.add_parameter(param)
        assert len(scope.violations) == 1
        violation = scope.violations[0]
        assert violation.rule_id == "parameter-mutation-during-aprocess"
        assert "p_added" in violation.message
        assert "add_parameter" in violation.message
        # The message names the actual offending node and class so a reader
        # can tell when the mutation came from a node other than the one the
        # active strict-mode scope is attributed to (e.g. a helper node
        # constructed during aprocess of a different node).
        assert mock_node.name in violation.message
        assert type(mock_node).__name__ in violation.message

    def test_violation_reported_when_removing_parameter_inside_aprocess_scope(self, mock_node: MockNode) -> None:
        param = Parameter(name="p_to_remove", type="str")
        mock_node.add_parameter(param)
        with (
            STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.RUNTIME_EXECUTE,
                subject=mock_node.name,
                library_name=None,
                is_worker=False,
            ) as scope,
            aprocess_scope(),
        ):
            mock_node.remove_parameter_element(param)
        assert len(scope.violations) == 1
        violation = scope.violations[0]
        assert violation.rule_id == "parameter-mutation-during-aprocess"
        assert "p_to_remove" in violation.message
        assert "remove_parameter_element" in violation.message
        assert mock_node.name in violation.message
        assert type(mock_node).__name__ in violation.message

    def test_violation_message_names_offender_distinct_from_scope_subject(self) -> None:
        """When a node mutates parameters inside a *different* node's scope.

        The strict-mode scope subject names the executing node, but the
        violation message must name the actual offender so a reader can
        tell the two apart -- this is the failure mode that motivated
        adding ``node_name`` / ``node_class`` to the remediation
        template.
        """
        outer = MockNode(name="outer_executing_node")
        offender = MockNode(name="inner_offender")
        param = Parameter(name="leaked", type="str")
        with (
            STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.RUNTIME_EXECUTE,
                subject=outer.name,
                library_name=None,
                is_worker=False,
            ) as scope,
            aprocess_scope(),
        ):
            offender.add_parameter(param)
        assert len(scope.violations) == 1
        violation = scope.violations[0]
        # Scope subject still names the active executing node.
        assert violation.subject == outer.name
        # But the message body names the actual offender, not the scope.
        assert offender.name in violation.message
        assert type(offender).__name__ in violation.message
        assert outer.name not in violation.message

    def test_no_violation_when_handler_sanctions_add(self, mock_node: MockNode) -> None:
        param = Parameter(name="sanctioned_add", type="str")
        with (
            STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.RUNTIME_EXECUTE,
                subject=mock_node.name,
                library_name=None,
                is_worker=False,
            ) as scope,
            aprocess_scope(),
            sanctioned_parameter_mutation(),
        ):
            mock_node.add_parameter(param)
        assert scope.violations == []

    def test_no_violation_when_handler_sanctions_remove(self, mock_node: MockNode) -> None:
        param = Parameter(name="sanctioned_remove", type="str")
        mock_node.add_parameter(param)
        with (
            STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.RUNTIME_EXECUTE,
                subject=mock_node.name,
                library_name=None,
                is_worker=False,
            ) as scope,
            aprocess_scope(),
            sanctioned_parameter_mutation(),
        ):
            mock_node.remove_parameter_element(param)
        assert scope.violations == []

    def test_worker_scope_escalates_to_error_severity(self, mock_node: MockNode) -> None:
        param = Parameter(name="worker_add", type="str")
        with (
            STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.RUNTIME_EXECUTE,
                subject=mock_node.name,
                library_name=None,
                is_worker=True,
            ) as scope,
            aprocess_scope(),
        ):
            mock_node.add_parameter(param)
        assert len(scope.violations) == 1
        violation = scope.violations[0]
        assert violation.severity.value == "error"

    def test_no_violation_during_hydration_under_runtime_execute(self, mock_node: MockNode) -> None:
        """Hydration runs add_parameter under RUNTIME_EXECUTE without aprocess.

        ``_hydrate_and_run_node_inner`` calls ``set_parameter_value``
        (firing ``before_value_set`` / ``after_value_set``) inside the
        ``RUNTIME_EXECUTE`` scope but before ``await node.aprocess()``.
        Real nodes (e.g. dynamic-pipeline diffuser nodes) call
        ``add_parameter`` from those hooks. The detector must stay silent
        until the framework enters ``aprocess_scope()``.
        """
        param = Parameter(name="hydration_added", type="str")
        with STRICT_MODE.open_scope(
            kind=StrictModeScopeKind.RUNTIME_EXECUTE,
            subject=mock_node.name,
            library_name=None,
            is_worker=True,
        ) as scope:
            mock_node.add_parameter(param)
        assert scope.violations == []

    def test_no_violation_when_add_parameter_called_from_init_under_load_probe(self) -> None:
        """__init__ calls to add_parameter during a LOAD_PROBE scope are not violations.

        LibraryManager._serialize_library_node_schemas instantiates every node
        class inside a LOAD_PROBE scope. Nodes legitimately declare their
        parameters by calling self.add_parameter(...) from __init__, so those
        calls must not report parameter-mutation-during-aprocess. The
        constructor flag suppresses the rule in this case; the
        ``_in_aprocess`` flag is also unset because no aprocess is running.
        """

        class NodeAddsParameterInInit(MockNode):
            def __init__(self, name: str = "probe_node") -> None:
                super().__init__(name=name)
                self.add_parameter(Parameter(name="declared_in_init", type="str"))

        token = _constructing_node.set(True)
        try:
            with STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.LOAD_PROBE,
                subject="NodeAddsParameterInInit",
                library_name="test_library",
                is_worker=True,
            ) as scope:
                NodeAddsParameterInInit(name="__schema_probe__")
        finally:
            _constructing_node.reset(token)
        assert scope.violations == []

    def test_no_violation_for_nested_node_construction_inside_aprocess(self, mock_node: MockNode) -> None:
        """A node constructed inside aprocess can declare params in __init__.

        ``LibraryRegistry.create_node`` sets the is-constructing flag for the
        duration of the inner node's ``__init__``. The detector's constructor
        short-circuit must hold even when ``aprocess_scope()`` is active for
        the outer node, otherwise creating helper nodes from aprocess would
        spuriously trip the rule.
        """

        class InnerNodeWithParam(MockNode):
            def __init__(self, name: str = "inner") -> None:
                super().__init__(name=name)
                self.add_parameter(Parameter(name="inner_p", type="str"))

        with (
            STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.RUNTIME_EXECUTE,
                subject=mock_node.name,
                library_name=None,
                is_worker=False,
            ) as scope,
            aprocess_scope(),
        ):
            token = _constructing_node.set(True)
            try:
                InnerNodeWithParam(name="constructed_during_aprocess")
            finally:
                _constructing_node.reset(token)
        assert scope.violations == []

    def test_no_violation_for_direct_construction_wrapped_in_constructing_node(self, mock_node: MockNode) -> None:
        """Direct construction wrapped in ``LibraryRegistry.constructing_node()`` is silent.

        Production has two known sites that build an ephemeral node by
        calling ``type(node)(name=...)`` / ``node_class(name=...)`` directly
        rather than going through ``LibraryRegistry.create_node`` -- node
        serialization's REFERENCE NODE comparison and the
        ``DescribeNodeTypeRequest`` probe. Without the wrapper the
        detector mistakes every ``add_parameter`` call in the helper's
        ``__init__`` for an aprocess-time mutation, since the active
        scope belongs to the outer node that is genuinely running.
        Wrapping the helper construction with the public
        ``constructing_node()`` context manager must reproduce the
        same suppression that ``create_node`` provides.
        """

        class HelperNodeWithParams(MockNode):
            def __init__(self, name: str = "helper") -> None:
                super().__init__(name=name)
                self.add_parameter(Parameter(name="helper_a", type="str"))
                self.add_parameter(Parameter(name="helper_b", type="str"))

        with (
            STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.RUNTIME_EXECUTE,
                subject=mock_node.name,
                library_name=None,
                is_worker=True,
            ) as scope,
            aprocess_scope(),
            LibraryRegistry.constructing_node(),
        ):
            HelperNodeWithParams(name="REFERENCE NODE")
        assert scope.violations == []

    def test_violation_when_direct_construction_is_not_wrapped(self, mock_node: MockNode) -> None:
        """The wrapper is load-bearing: removing it surfaces the false positive.

        This is the production bug the wrapper fixes. Without
        ``LibraryRegistry.constructing_node()`` the detector reports
        every parameter declared in the helper's ``__init__`` against
        the *outer* node's active scope -- producing exactly the noisy
        log a workflow with several library nodes used to emit during
        autosave / serialization.
        """

        class HelperNodeWithParams(MockNode):
            def __init__(self, name: str = "helper") -> None:
                super().__init__(name=name)
                self.add_parameter(Parameter(name="helper_a", type="str"))
                self.add_parameter(Parameter(name="helper_b", type="str"))

        with (
            STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.RUNTIME_EXECUTE,
                subject=mock_node.name,
                library_name=None,
                is_worker=False,
            ) as scope,
            aprocess_scope(),
        ):
            HelperNodeWithParams(name="REFERENCE NODE")

        # Two parameter declarations in __init__ trip the rule once each.
        expected_violations = 2
        assert len(scope.violations) == expected_violations
        # Each violation names the helper, not the outer scope's node, so
        # the operator can spot that the mutations originated outside the
        # node the scope is attributed to. (Same condition tested in
        # ``test_violation_message_names_offender_distinct_from_scope_subject``.)
        for violation in scope.violations:
            assert "REFERENCE NODE" in violation.message
            assert "HelperNodeWithParams" in violation.message
            assert violation.subject == mock_node.name


class TestConstructingNodeContextManager:
    """``LibraryRegistry.constructing_node()`` toggles the task-local flag."""

    def test_flag_set_inside_block_and_reset_after(self) -> None:
        assert LibraryRegistry.is_constructing_node() is False
        with LibraryRegistry.constructing_node():
            assert LibraryRegistry.is_constructing_node() is True
        assert LibraryRegistry.is_constructing_node() is False

    def test_nesting_restores_outer_state_on_exit(self) -> None:
        """Outer ``constructing_node`` flag survives nested enter/exit.

        Tokens stack: an outer block stays True across an inner block's
        enter/exit, and the outer block's exit restores the original
        False state.
        """
        assert LibraryRegistry.is_constructing_node() is False
        with LibraryRegistry.constructing_node():
            assert LibraryRegistry.is_constructing_node() is True
            with LibraryRegistry.constructing_node():
                assert LibraryRegistry.is_constructing_node() is True
            # Inner reset must not clear the outer flag.
            assert LibraryRegistry.is_constructing_node() is True
        assert LibraryRegistry.is_constructing_node() is False

    def test_flag_reset_on_exception(self) -> None:
        """Exiting via exception still resets the flag."""

        def _trigger() -> None:
            with LibraryRegistry.constructing_node():
                assert LibraryRegistry.is_constructing_node() is True
                msg = "boom"
                raise RuntimeError(msg)

        assert LibraryRegistry.is_constructing_node() is False
        with pytest.raises(RuntimeError, match="boom"):
            _trigger()
        assert LibraryRegistry.is_constructing_node() is False
