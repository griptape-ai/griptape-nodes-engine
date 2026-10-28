"""Who opens the workflow a saved workflow file builds into.

`build_workflow()` opens nothing: it builds into the workflow its caller opened or bound. The
engine's load paths decide through `WorkflowRunner.run_workflow(opens_workflow=...)`, and a file
run on its own opens itself in `aexecute_workflow()`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.node_types import NodeDependencies
from griptape_nodes.node_library.workflow_registry import WorkflowMetadata, WorkflowShape
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, SerializedFlowCommands
from griptape_nodes.retained_mode.runtime.open_workflow_set import OpenWorkflowSet

if TYPE_CHECKING:
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine

# The guard files saved before carry in build_workflow().
_SAVED_BEFORE_GUARD = """    context_manager = GriptapeNodes.ContextManager()
    if not context_manager.has_current_workflow():
        context_manager.push_workflow(file_path=__file__)
"""


def _generate(engine: Engine, *, workflow_shape: WorkflowShape | None = None) -> str:
    serialized_flow_commands = SerializedFlowCommands(
        flow_initialization_command=CreateFlowRequest(flow_name="canvas", parent_flow_name=None),
        serialized_node_commands=[],
        serialized_connections=[],
        unique_parameter_uuid_to_values={},
        set_parameter_value_commands={},
        set_lock_commands_per_node={},
        sub_flows_commands=[],
        node_dependencies=NodeDependencies(),
        node_types_used=set(),
        flow_name="canvas",
    )
    metadata = WorkflowMetadata(
        name="saved",
        schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
        engine_version_created_with="1.0.0",
        node_libraries_referenced=[],
        workflow_shape=workflow_shape,
    )
    return engine.workflow_manager.codegen.generate_workflow_file_content(
        serialized_flow_commands=serialized_flow_commands, workflow_metadata=metadata
    )


def _run(engine: Engine, path: Path, *, opens_workflow: bool) -> None:
    result = asyncio.run(engine.workflow_manager.runner.run_workflow(str(path), opens_workflow=opens_workflow))
    assert result.execution_successful, result.execution_details


class TestSavedWorkflowOpen:
    def test_build_workflow_opens_nothing_and_a_standalone_run_opens_the_file(self, engine: Engine) -> None:
        generated = _generate(engine, workflow_shape=WorkflowShape(inputs={}, outputs={}))
        build_workflow, _, standalone = generated.partition("async def aexecute_workflow(")

        assert "push_workflow" not in build_workflow
        assert "has_current_workflow" not in generated
        open_call = "GriptapeNodes.ContextManager().push_workflow(file_path=__file__)"
        assert open_call in standalone
        assert standalone.index(open_call) < standalone.index("await build_workflow()")

    def test_the_runner_opens_the_file_and_builds_into_it(self, engine: Engine, tmp_path: Path) -> None:
        path = tmp_path / "saved.py"
        path.write_text(_generate(engine), encoding="utf-8")

        _run(engine, path, opens_workflow=True)

        [opened] = engine.open_workflows.all_open()
        assert engine.workflow_registry.get(opened.workflow_id).absolute_path == path.resolve()
        assert opened.objects.has("canvas")

    def test_the_runner_builds_into_the_bound_workflow_however_many_are_open(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        path = tmp_path / "saved.py"
        path.write_text(_generate(engine), encoding="utf-8")
        engine._open_workflows = OpenWorkflowSet(allow_several_open=True)
        engine.context_manager.push_workflow(workflow_name="other")
        loading = engine.open_workflows.open(
            engine.workflow_registry.register_unsaved("loading", listed=False).workflow_id
        )

        with engine.bind_workflow(loading):
            _run(engine, path, opens_workflow=False)

        assert len(engine.open_workflows.all_open()) == 2  # noqa: PLR2004
        assert loading.objects.has("canvas")

    def test_a_file_saved_before_builds_into_the_workflow_the_runner_opened(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        path = tmp_path / "saved_before.py"
        generated = _generate(engine)
        signature = "async def build_workflow() -> None:\n"
        path.write_text(generated.replace(signature, signature + _SAVED_BEFORE_GUARD), encoding="utf-8")

        _run(engine, path, opens_workflow=True)

        [opened] = engine.open_workflows.all_open()
        assert opened.objects.has("canvas")


class TestWorkflowsDoNotNest:
    def test_unbound_work_may_push_while_several_are_open(self, engine: Engine) -> None:
        engine._open_workflows = OpenWorkflowSet(allow_several_open=True)
        engine.context_manager.push_workflow(workflow_name="first")
        engine.context_manager.push_workflow(workflow_name="second")

        engine.context_manager.push_workflow(workflow_name="third")

        assert len(engine.open_workflows.all_open()) == 3  # noqa: PLR2004

    def test_a_push_while_bound_to_a_live_workflow_is_refused_however_many_may_be_open(self, engine: Engine) -> None:
        engine._open_workflows = OpenWorkflowSet(allow_several_open=True)
        engine.context_manager.push_workflow(workflow_name="bound")
        bound = engine.current_workflow()

        with (
            engine.bind_workflow(bound),
            pytest.raises(engine.context_manager.WorkflowAlreadyOpenError, match="is already open"),
        ):
            engine.context_manager.push_workflow(workflow_name="nested")

        assert engine.open_workflows.all_open() == [bound]
        assert engine.workflow_registry.find_by_name("nested") is None
