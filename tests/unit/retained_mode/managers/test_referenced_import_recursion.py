"""Tests for the recursion-depth refusal of referenced workflow imports."""

from unittest.mock import Mock

import pytest

from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.exe_types.node_types import NODE_GROUP_FLOW
from griptape_nodes.exe_types.workflow_node_errors import WorkflowRecursionLimitError
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.workflow_events import ImportWorkflowAsReferencedSubFlowResultFailure
from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
from griptape_nodes.retained_mode.managers.context_manager import ContextManager
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.managers.flow_manager import FlowManager
from griptape_nodes.retained_mode.managers.object_manager import ObjectManager
from griptape_nodes.retained_mode.managers.workflow.referenced_import import ReferencedWorkflowImport

TWO_IMPORTS = 2


class TestValidateRecursionDepth:
    """`_validate_recursion_depth` refuses a workflow nested deeper than the limit."""

    @pytest.fixture
    def importer(self, monkeypatch: pytest.MonkeyPatch) -> ReferencedWorkflowImport:
        config_manager = Mock(spec=ConfigManager)
        config_manager.max_workflow_recursion_depth = 2
        engine = Mock(spec=Engine)
        engine.config_manager = config_manager
        importer = ReferencedWorkflowImport(Mock(spec=EventManager), engine=engine)
        monkeypatch.setattr(importer, "_count_enclosing_imports_of", Mock(spec=importer._count_enclosing_imports_of))
        return importer

    def test_refusal_carries_the_recursion_limit_error(self, importer: ReferencedWorkflowImport) -> None:
        importer._count_enclosing_imports_of.return_value = 3  # type: ignore[attr-defined]

        result = importer._validate_recursion_depth("flow", "wf")

        assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultFailure)
        assert isinstance(result.exception, WorkflowRecursionLimitError)
        assert str(result.exception) == str(result.result_details)
        assert "'wf' contains itself more than 2 levels deep" in str(result.result_details)

    def test_within_the_limit_is_not_refused(self, importer: ReferencedWorkflowImport) -> None:
        importer._count_enclosing_imports_of.return_value = 2  # type: ignore[attr-defined]

        assert importer._validate_recursion_depth("flow", "wf") is None


class TestCountEnclosingImportsOf:
    """`_count_enclosing_imports_of` counts the imported copies of a workflow that a flow sits inside."""

    @pytest.fixture
    def importer(self) -> ReferencedWorkflowImport:
        engine = Mock(spec=Engine)
        engine.flow_manager = Mock(spec=FlowManager)
        engine.object_manager = Mock(spec=ObjectManager)
        engine.context_manager = Mock(spec=ContextManager)
        engine.context_manager.has_current_workflow.return_value = False
        return ReferencedWorkflowImport(Mock(spec=EventManager), engine=engine)

    @staticmethod
    def _flow(name: str, *, flow_type: str | None = None) -> Mock:
        flow = Mock(spec=ControlFlow)
        flow.name = name
        flow.metadata = {}
        if flow_type is not None:
            flow.metadata["flow_type"] = flow_type
        return flow

    def _arrange(
        self,
        importer: ReferencedWorkflowImport,
        flows: dict[str, Mock],
        enclosing: dict[str, str | None],
        referenced: dict[str, str | None],
    ) -> None:
        engine = importer.engine
        engine.object_manager.attempt_get_object_by_name_as_type.side_effect = lambda name, _type: flows.get(name)  # type: ignore[attr-defined]
        engine.flow_manager.get_enclosing_flow.side_effect = lambda name: enclosing[name]  # type: ignore[attr-defined]
        engine.flow_manager.get_referenced_workflow_name.side_effect = lambda flow: referenced[flow.name]  # type: ignore[attr-defined]

    def test_walks_up_through_the_owner_link_and_counts_each_import(self, importer: ReferencedWorkflowImport) -> None:
        flows = {name: self._flow(name) for name in ("inner", "middle", "outer")}
        # `inner` is an iteration flow whose enclosing flow is the loop's flow, `middle`.
        self._arrange(
            importer,
            flows,
            enclosing={"inner": "middle", "middle": "outer", "outer": None},
            referenced={"inner": "wf", "middle": "wf", "outer": "other"},
        )

        assert importer._count_enclosing_imports_of("inner", "wf") == TWO_IMPORTS

    def test_skips_group_subflows(self, importer: ReferencedWorkflowImport) -> None:
        flows = {
            "import": self._flow("import"),
            "group": self._flow("group", flow_type=NODE_GROUP_FLOW),
        }
        self._arrange(
            importer,
            flows,
            enclosing={"group": "import", "import": None},
            referenced={"group": "wf", "import": "wf"},
        )

        assert importer._count_enclosing_imports_of("group", "wf") == 1

    def test_stops_when_the_walk_revisits_a_flow(self, importer: ReferencedWorkflowImport) -> None:
        flows = {name: self._flow(name) for name in ("a", "b")}
        self._arrange(
            importer,
            flows,
            enclosing={"a": "b", "b": "a"},
            referenced={"a": "wf", "b": "wf"},
        )

        assert importer._count_enclosing_imports_of("a", "wf") == TWO_IMPORTS
