"""Tests for the recursion-depth refusal of referenced workflow imports."""

from unittest.mock import Mock

import pytest

from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.exe_types.node_types import NODE_GROUP_FLOW
from griptape_nodes.exe_types.workflow_node_errors import WorkflowRecursionLimitError
from griptape_nodes.node_library.workflow_registry import Workflow, _WorkflowRegistry
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.workflow_events import ImportWorkflowAsReferencedSubFlowResultFailure
from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.managers.flow_manager import FlowManager
from griptape_nodes.retained_mode.managers.workflow.referenced_import import ReferencedWorkflowImport
from griptape_nodes.retained_mode.runtime.objects import WorkflowObjects
from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

TWO_IMPORTS = 2


@pytest.fixture
def imported() -> Mock:
    """A saved workflow, as the registry resolves a request's address."""
    imported = Mock(spec=Workflow)
    imported.name = "wf"
    imported.workflow_id = "wf-id"
    return imported


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

    def test_refusal_carries_the_recursion_limit_error(
        self, importer: ReferencedWorkflowImport, imported: Mock
    ) -> None:
        importer._count_enclosing_imports_of.return_value = 3  # type: ignore[attr-defined]

        result = importer._validate_recursion_depth(Mock(spec=WorkflowRuntime), "flow", imported)

        assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultFailure)
        assert isinstance(result.exception, WorkflowRecursionLimitError)
        assert str(result.exception) == str(result.result_details)
        assert "'wf' contains itself more than 2 levels deep" in str(result.result_details)

    def test_refusal_uses_the_singular_when_the_limit_is_one_level(
        self, importer: ReferencedWorkflowImport, imported: Mock
    ) -> None:
        importer.engine.config_manager.max_workflow_recursion_depth = 1  # type: ignore[attr-defined]
        importer._count_enclosing_imports_of.return_value = 2  # type: ignore[attr-defined]

        result = importer._validate_recursion_depth(Mock(spec=WorkflowRuntime), "flow", imported)

        assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultFailure)
        assert "more than 1 level deep" in str(result.result_details)
        assert "levels" not in str(result.result_details)

    def test_within_the_limit_is_not_refused(self, importer: ReferencedWorkflowImport, imported: Mock) -> None:
        importer._count_enclosing_imports_of.return_value = 2  # type: ignore[attr-defined]

        assert importer._validate_recursion_depth(Mock(spec=WorkflowRuntime), "flow", imported) is None


class TestCountEnclosingImportsOf:
    """`_count_enclosing_imports_of` counts the imported copies of a workflow that a flow sits inside."""

    @pytest.fixture
    def importer(self) -> ReferencedWorkflowImport:
        engine = Mock(spec=Engine)
        engine.flow_manager = Mock(spec=FlowManager)
        return ReferencedWorkflowImport(Mock(spec=EventManager), engine=engine)

    @pytest.fixture(autouse=True)
    def not_the_opened_workflow(self, monkeypatch: pytest.MonkeyPatch, importer: ReferencedWorkflowImport) -> None:
        monkeypatch.setattr(
            importer, "_is_opened_workflow", Mock(spec=importer._is_opened_workflow, return_value=False)
        )

    @pytest.fixture
    def workflow(self) -> Mock:
        workflow = Mock(spec=WorkflowRuntime)
        workflow.objects = Mock(spec=WorkflowObjects)
        return workflow

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
        workflow: Mock,
        flows: dict[str, Mock],
        enclosing: dict[str, str | None],
        referenced: dict[str, str | None],
    ) -> None:
        engine = importer.engine
        workflow.objects.get_as_type.side_effect = lambda name, _type: flows.get(name)
        engine.flow_manager.get_enclosing_flow.side_effect = lambda _workflow, name: enclosing[name]  # type: ignore[attr-defined]
        engine.flow_manager.get_referenced_workflow_name.side_effect = lambda _workflow, flow: referenced[flow.name]  # type: ignore[attr-defined]

    def test_walks_up_through_the_owner_link_and_counts_each_import(
        self, importer: ReferencedWorkflowImport, workflow: Mock, imported: Mock
    ) -> None:
        flows = {name: self._flow(name) for name in ("inner", "middle", "outer")}
        # `inner` is an iteration flow whose enclosing flow is the loop's flow, `middle`.
        self._arrange(
            importer,
            workflow,
            flows,
            enclosing={"inner": "middle", "middle": "outer", "outer": None},
            referenced={"inner": "wf", "middle": "wf", "outer": "other"},
        )

        assert importer._count_enclosing_imports_of(workflow, "inner", imported) == TWO_IMPORTS

    def test_skips_group_subflows(self, importer: ReferencedWorkflowImport, workflow: Mock, imported: Mock) -> None:
        flows = {
            "import": self._flow("import"),
            "group": self._flow("group", flow_type=NODE_GROUP_FLOW),
        }
        self._arrange(
            importer,
            workflow,
            flows,
            enclosing={"group": "import", "import": None},
            referenced={"group": "wf", "import": "wf"},
        )

        assert importer._count_enclosing_imports_of(workflow, "group", imported) == 1

    def test_stops_when_the_walk_revisits_a_flow(
        self, importer: ReferencedWorkflowImport, workflow: Mock, imported: Mock
    ) -> None:
        flows = {name: self._flow(name) for name in ("a", "b")}
        self._arrange(
            importer,
            workflow,
            flows,
            enclosing={"a": "b", "b": "a"},
            referenced={"a": "wf", "b": "wf"},
        )

        assert importer._count_enclosing_imports_of(workflow, "a", imported) == TWO_IMPORTS

    def test_counts_the_opened_workflow_as_one_level(
        self, importer: ReferencedWorkflowImport, workflow: Mock, imported: Mock
    ) -> None:
        importer._is_opened_workflow.return_value = True  # type: ignore[attr-defined]
        flows = {"import": self._flow("import")}
        self._arrange(importer, workflow, flows, enclosing={"import": None}, referenced={"import": "wf"})

        assert importer._count_enclosing_imports_of(workflow, "import", imported) == TWO_IMPORTS


class TestIsOpenedWorkflow:
    """`_is_opened_workflow` compares the imported entry with the open workflow by registry id."""

    @pytest.fixture
    def registry(self) -> Mock:
        return Mock(spec=_WorkflowRegistry)

    @pytest.fixture
    def importer(self, registry: Mock) -> ReferencedWorkflowImport:
        engine = Mock(spec=Engine)
        engine.workflow_registry = registry
        return ReferencedWorkflowImport(Mock(spec=EventManager), engine=engine)

    @pytest.fixture
    def workflow(self) -> Mock:
        workflow = Mock(spec=WorkflowRuntime)
        workflow.workflow_id = "open-id"
        return workflow

    def test_the_open_workflow_itself(self, importer: ReferencedWorkflowImport, imported: Mock, workflow: Mock) -> None:
        workflow.workflow_id = imported.workflow_id

        assert importer._is_opened_workflow(workflow, imported) is True

    def test_the_open_workflow_is_an_unsaved_copy_of_it(
        self, importer: ReferencedWorkflowImport, registry: Mock, imported: Mock, workflow: Mock
    ) -> None:
        copy = Mock(spec=Workflow)
        registry.get.return_value = copy
        registry.seed_source.return_value = imported

        assert importer._is_opened_workflow(workflow, imported) is True

    def test_the_open_workflow_is_an_unsaved_copy_of_a_different_workflow(
        self, importer: ReferencedWorkflowImport, registry: Mock, imported: Mock, workflow: Mock
    ) -> None:
        copy = Mock(spec=Workflow)
        other = Mock(spec=Workflow)
        other.workflow_id = "other-id"
        registry.get.return_value = copy
        registry.seed_source.return_value = other

        assert importer._is_opened_workflow(workflow, imported) is False

    def test_another_workflow(
        self, importer: ReferencedWorkflowImport, registry: Mock, imported: Mock, workflow: Mock
    ) -> None:
        registry.get.return_value = Mock(spec=Workflow)
        registry.seed_source.return_value = None

        assert importer._is_opened_workflow(workflow, imported) is False
