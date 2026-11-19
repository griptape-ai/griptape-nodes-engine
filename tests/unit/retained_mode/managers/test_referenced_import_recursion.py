"""Tests for the recursion-depth refusal of referenced workflow imports."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.exe_types.node_types import NODE_GROUP_FLOW
from griptape_nodes.node_library.workflow_registry import Workflow, WorkflowMetadata, _WorkflowRegistry
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.failure_tree import WorkflowRecursionLimitError
from griptape_nodes.retained_mode.events.workflow_events import ImportWorkflowAsReferencedSubFlowResultFailure
from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.managers.flow_manager import FlowManager
from griptape_nodes.retained_mode.managers.workflow.referenced_import import ReferencedWorkflowImport
from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

TWO_IMPORTS = 2
THREE_IMPORTS = 3


class RealWorkflowTree:
    """A real registry, flow manager and workflow runtime for the importer to walk."""

    @pytest.fixture
    def registry(self, tmp_path: Path) -> _WorkflowRegistry:
        config_manager = Mock(spec=ConfigManager)
        config_manager.workspace_path = tmp_path
        return _WorkflowRegistry(config_manager, is_workflow_open=lambda _workflow_id: False)

    @pytest.fixture
    def saved_workflow(self, registry: _WorkflowRegistry, tmp_path: Path) -> Workflow:
        """The saved workflow named 'wf.py', which is the one being imported."""
        path = tmp_path / "wf.py"
        path.write_text("# stub")
        metadata = WorkflowMetadata(
            name="wf",
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with="test",
            node_libraries_referenced=[],
        )
        return registry.register_file(path, metadata)

    @pytest.fixture
    def engine(self, registry: _WorkflowRegistry) -> Mock:
        engine = Mock(spec=Engine)
        engine.config_manager = Mock(spec=ConfigManager)
        engine.config_manager.max_workflow_recursion_depth = 2
        engine.workflow_registry = registry
        engine.flow_manager = FlowManager(Mock(spec=EventManager), engine=engine)
        return engine

    @pytest.fixture
    def importer(self, engine: Mock) -> ReferencedWorkflowImport:
        return ReferencedWorkflowImport(Mock(spec=EventManager), engine=engine)

    @pytest.fixture
    def workflow(self, engine: Mock, registry: _WorkflowRegistry) -> WorkflowRuntime:
        """The open workflow: an unsaved one that is not a copy of anything."""
        return WorkflowRuntime(registry.register_unsaved("opened").workflow_id, engine)

    @staticmethod
    def _add_flow(
        workflow: WorkflowRuntime,
        name: str,
        *,
        parent: str | None,
        imports: str | None = None,
        flow_type: str | None = None,
    ) -> ControlFlow:
        """Add a flow under `parent`, tagged as an import of the workflow named `imports`."""
        metadata = {}
        if flow_type is not None:
            metadata["flow_type"] = flow_type
        flow = ControlFlow(name, workflow.engine, metadata)
        workflow.objects.add(name, flow)
        workflow.flows.name_to_parent_name[name] = parent
        if imports is not None:
            workflow.flows.flow_to_referenced_workflow_name[flow] = imports
        return flow

    def _add_nested_imports(self, workflow: WorkflowRuntime, depth: int) -> str:
        """Add `depth` imports of 'wf' inside one another, and return the name of the innermost."""
        parent = None
        for level in range(depth):
            name = f"import_{level}"
            self._add_flow(workflow, name, parent=parent, imports="wf")
            parent = name
        assert parent is not None
        return parent


class TestValidateRecursionDepth(RealWorkflowTree):
    """`_validate_recursion_depth` refuses a workflow nested deeper than the limit."""

    def test_depth_equal_to_the_limit_is_not_refused(
        self, importer: ReferencedWorkflowImport, workflow: WorkflowRuntime, saved_workflow: Workflow
    ) -> None:
        innermost = self._add_nested_imports(workflow, 2)

        assert importer._validate_recursion_depth(workflow, innermost, saved_workflow) is None

    def test_one_level_past_the_limit_is_refused(
        self, importer: ReferencedWorkflowImport, workflow: WorkflowRuntime, saved_workflow: Workflow
    ) -> None:
        innermost = self._add_nested_imports(workflow, 3)

        result = importer._validate_recursion_depth(workflow, innermost, saved_workflow)

        assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultFailure)

    def test_refusal_carries_the_recursion_limit_error(
        self, importer: ReferencedWorkflowImport, workflow: WorkflowRuntime, saved_workflow: Workflow
    ) -> None:
        innermost = self._add_nested_imports(workflow, 3)

        result = importer._validate_recursion_depth(workflow, innermost, saved_workflow)

        assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultFailure)
        assert isinstance(result.exception, WorkflowRecursionLimitError)
        assert str(result.exception) == str(result.result_details)
        assert "'wf' contains itself more than 2 levels deep" in str(result.result_details)
        assert "into flow 'import_2'" in str(result.result_details)

    def test_refusal_uses_the_singular_when_the_limit_is_one_level(
        self,
        importer: ReferencedWorkflowImport,
        engine: Mock,
        workflow: WorkflowRuntime,
        saved_workflow: Workflow,
    ) -> None:
        engine.config_manager.max_workflow_recursion_depth = 1
        innermost = self._add_nested_imports(workflow, 2)

        result = importer._validate_recursion_depth(workflow, innermost, saved_workflow)

        assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultFailure)
        assert "more than 1 level deep" in str(result.result_details)
        assert "levels" not in str(result.result_details)

    def test_the_opened_workflow_counts_toward_the_limit(
        self, importer: ReferencedWorkflowImport, engine: Mock, saved_workflow: Workflow
    ) -> None:
        workflow = WorkflowRuntime(saved_workflow.workflow_id, engine)
        within = self._add_nested_imports(workflow, 1)

        assert importer._validate_recursion_depth(workflow, within, saved_workflow) is None

        self._add_flow(workflow, "import_1", parent="import_0", imports="wf")
        result = importer._validate_recursion_depth(workflow, "import_1", saved_workflow)

        assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultFailure)


class TestCountEnclosingImportsOf(RealWorkflowTree):
    """`_count_enclosing_imports_of` counts the imported copies of a workflow that a flow sits inside."""

    def test_counts_each_enclosing_import(
        self, importer: ReferencedWorkflowImport, workflow: WorkflowRuntime, saved_workflow: Workflow
    ) -> None:
        innermost = self._add_nested_imports(workflow, 3)

        assert importer._count_enclosing_imports_of(workflow, innermost, saved_workflow) == THREE_IMPORTS

    def test_flows_that_import_another_workflow_or_nothing_are_not_counted(
        self, importer: ReferencedWorkflowImport, workflow: WorkflowRuntime, saved_workflow: Workflow
    ) -> None:
        self._add_flow(workflow, "plain", parent=None)
        self._add_flow(workflow, "other", parent="plain", imports="other_workflow")
        self._add_flow(workflow, "inner", parent="other")

        assert importer._count_enclosing_imports_of(workflow, "inner", saved_workflow) == 0

    def test_walks_up_through_the_owner_link_of_an_iteration_flow(
        self, importer: ReferencedWorkflowImport, engine: Mock, workflow: WorkflowRuntime, saved_workflow: Workflow
    ) -> None:
        self._add_flow(workflow, "outer", parent=None, imports="other_workflow")
        self._add_flow(workflow, "middle", parent="outer", imports="wf")
        # The iteration flow is parented to a flow that says nothing about the loop that runs it.
        self._add_flow(workflow, "unrelated", parent=None)
        self._add_flow(workflow, "inner", parent="unrelated", imports="wf")
        engine.flow_manager.record_iteration_flow_owner(workflow, "inner", "middle")

        assert importer._count_enclosing_imports_of(workflow, "inner", saved_workflow) == TWO_IMPORTS

    def test_skips_group_subflows(
        self, importer: ReferencedWorkflowImport, workflow: WorkflowRuntime, saved_workflow: Workflow
    ) -> None:
        self._add_flow(workflow, "import", parent=None, imports="wf")
        self._add_flow(workflow, "group", parent="import", imports="wf", flow_type=NODE_GROUP_FLOW)

        assert importer._count_enclosing_imports_of(workflow, "group", saved_workflow) == 1

    def test_stops_when_the_walk_revisits_a_flow(
        self, importer: ReferencedWorkflowImport, workflow: WorkflowRuntime, saved_workflow: Workflow
    ) -> None:
        self._add_flow(workflow, "a", parent="b", imports="wf")
        self._add_flow(workflow, "b", parent="a", imports="wf")

        assert importer._count_enclosing_imports_of(workflow, "a", saved_workflow) == TWO_IMPORTS

    def test_counts_the_opened_workflow_as_one_more_level(
        self, importer: ReferencedWorkflowImport, engine: Mock, saved_workflow: Workflow
    ) -> None:
        workflow = WorkflowRuntime(saved_workflow.workflow_id, engine)
        self._add_flow(workflow, "import", parent=None, imports="wf")

        assert importer._count_enclosing_imports_of(workflow, "import", saved_workflow) == TWO_IMPORTS

    def test_counts_an_unsaved_copy_of_the_workflow_as_the_opened_workflow(
        self, importer: ReferencedWorkflowImport, engine: Mock, registry: _WorkflowRegistry, saved_workflow: Workflow
    ) -> None:
        copy = registry.register_unsaved_copy(saved_workflow)
        workflow = WorkflowRuntime(copy.workflow_id, engine)
        self._add_flow(workflow, "import", parent=None, imports="wf")

        assert importer._count_enclosing_imports_of(workflow, "import", saved_workflow) == TWO_IMPORTS

    def test_does_not_count_an_opened_workflow_that_is_something_else(
        self, importer: ReferencedWorkflowImport, workflow: WorkflowRuntime, saved_workflow: Workflow
    ) -> None:
        self._add_flow(workflow, "import", parent=None, imports="wf")

        assert importer._count_enclosing_imports_of(workflow, "import", saved_workflow) == 1


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
    def imported(self) -> Mock:
        """A saved workflow, as the registry resolves a request's address."""
        imported = Mock(spec=Workflow)
        imported.name = "wf"
        imported.workflow_id = "wf-id"
        return imported

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
