"""Opening a saved workflow whose library will not register (issue #5505).

A library that cannot be registered -- uninstalled, or present on disk but disabled in
``libraries_to_register`` -- must cost execution, never editing. The workflow still opens:
nodes from the missing library come back as ``ErrorProxyNode`` placeholders carrying the
reason, while every node, value, and connection from the libraries that did load is intact.

What that covers is the missing library's NODE TYPES. A parameter value whose class the
library declares is a separate matter: codegen emits it as a hard import inside
``build_workflow()``, which raises before any node exists. These tests deliberately hold
plain values, so none of them stray into that case.

Each test drives the real path an artist takes. A graph is built against two real fixture
libraries and saved through the real codegen, then the engine is rebuilt with only one of
those libraries installed and the saved file is replayed through
``WorkflowManager.run_workflow`` -- the same call ``RunWorkflowFromRegistryRequest`` makes.
Nothing about the missing library is mocked: it is genuinely absent from the second engine's
``LibraryRegistry``, so ``CreateNodeRequest`` fails the way it does in production.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from griptape_nodes.exe_types.node_types import ErrorProxyNode
from griptape_nodes.files.path_utils import derive_registry_key
from griptape_nodes.files.project_file import ProjectFileDestination
from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.node_library.workflow_registry import WorkflowRegistry, read_workflow_metadata
from griptape_nodes.retained_mode.engine import current_engine, reset_root_engine
from griptape_nodes.retained_mode.events.base_events import ResultDetails
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
    ListConnectionsForNodeRequest,
    ListConnectionsForNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultFailure,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.parameter_events import (
    GetParameterValueRequest,
    GetParameterValueResultSuccess,
    SetParameterValueRequest,
    SetParameterValueResultFailure,
    SetParameterValueResultSuccess,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    ImportWorkflowAsReferencedSubFlowRequest,
    ImportWorkflowAsReferencedSubFlowResultSuccess,
    RegisterWorkflowRequest,
    RegisterWorkflowResultSuccess,
    RunWorkflowFromRegistryRequest,
    RunWorkflowFromRegistryResultSuccess,
    RunWorkflowFromScratchRequest,
    RunWorkflowFromScratchResultSuccess,
    SaveWorkflowFileFromSerializedFlowResultSuccess,
    WorkflowStatus,
)
from griptape_nodes.retained_mode.managers.fitness_problems.workflows import LibraryNotRegisteredProblem
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.utils.version_utils import engine_version

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine

_FLOW_NAME = "ControlFlow_1"
_AVAILABLE_LIBRARY = "Missing Library Fixture (Available)"
_UNAVAILABLE_LIBRARY = "Missing Library Fixture (Unavailable)"

# One module body serves both fixture libraries; each library's manifest points at its own
# copy under its own directory, so the two class names never collide in LibraryRegistry.
_FIXTURE_NODE_MODULE = '''
"""Fixture nodes for the missing-library load suite. Not part of any shipped library."""

from __future__ import annotations

from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode


class {class_name}(DataNode):
    """A data node with two independently connectable any-typed parameters."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        for param_name in ("value", "value2"):
            self.add_parameter(
                Parameter(
                    name=param_name,
                    type="any",
                    default_value=None,
                    tooltip="",
                    allowed_modes={{ParameterMode.INPUT, ParameterMode.PROPERTY, ParameterMode.OUTPUT}},
                )
            )

    def process(self) -> None:
        pass
'''


def _library_schema(library_name: str, class_name: str, module_file: str) -> dict[str, Any]:
    return {
        "name": library_name,
        "library_schema_version": "0.7.0",
        "metadata": {
            "author": "Test Fixture",
            "description": "Minimal library used by the missing-library load tests",
            "library_version": "0.1.0",
            "engine_version": engine_version,
            "tags": ["test"],
            "dependencies": {"pip_dependencies": []},
        },
        "categories": [
            {
                "test": {
                    "title": "test",
                    "description": "Test nodes",
                    "color": "border-gray-500",
                    "icon": "Folder",
                }
            }
        ],
        "nodes": [
            {
                "class_name": class_name,
                "file_path": module_file,
                "metadata": {"category": "test", "description": "Two any-typed parameters", "display_name": class_name},
            }
        ],
    }


@pytest.fixture(autouse=True)
def _clear_process_global_registries() -> Generator[None, None, None]:
    """Clear the process-global library and workflow registries around every test in this file.

    Both keep their state in ``ClassVar`` dicts the engine does not own, so the fixture
    libraries and saved workflows registered here would otherwise leak into whichever test runs
    next in the same xdist worker. Sibling files clear ``LibraryRegistry`` the same way.
    """
    LibraryRegistry._clear()
    with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
        yield
    LibraryRegistry._clear()


def _write_library(root: Path, library_name: str, class_name: str) -> Path:
    """Write one fixture library (manifest + node module) under its own directory."""
    directory = root / class_name
    directory.mkdir(parents=True, exist_ok=True)
    module_file = f"{class_name.lower()}_nodes.py"
    (directory / module_file).write_text(_FIXTURE_NODE_MODULE.format(class_name=class_name))
    library_json = directory / "griptape_nodes_library.json"
    library_json.write_text(json.dumps(_library_schema(library_name, class_name, module_file), indent=2))
    return library_json


def _register(engine: Engine, library_json: Path) -> str:
    result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(result, RegisterLibraryFromFileResultSuccess), result
    return result.library_name


def _create_node(engine: Engine, node_type: str, library_name: str, node_name: str, flow_name: str) -> str:
    result = engine.handle_request(
        CreateNodeRequest(
            node_type=node_type,
            specific_library_name=library_name,
            node_name=node_name,
            override_parent_flow_name=flow_name,
        )
    )
    assert isinstance(result, CreateNodeResultSuccess), result
    return result.node_name


def _set_value(engine: Engine, node_name: str, parameter_name: str, value: Any) -> None:
    result = engine.handle_request(
        SetParameterValueRequest(node_name=node_name, parameter_name=parameter_name, value=value)
    )
    assert isinstance(result, SetParameterValueResultSuccess), result


def _get_value(engine: Engine, node_name: str, parameter_name: str) -> Any:
    result = engine.handle_request(GetParameterValueRequest(node_name=node_name, parameter_name=parameter_name))
    assert isinstance(result, GetParameterValueResultSuccess), result
    return result.value


def _save_two_library_workflow(tmp_path: Path, file_stem: str = "two_library_workflow") -> str:
    """Build and save a graph spanning both fixture libraries; return the relative file name.

    The graph is deliberately mixed: an available-library node feeding an
    unavailable-library node, values on both sides, so a load can be checked for what it
    preserved as well as what it replaced.
    """
    engine = current_engine()
    engine.config_manager.workspace_path = tmp_path
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

    available_json = _write_library(tmp_path / "libraries", _AVAILABLE_LIBRARY, "AvailableNode")
    unavailable_json = _write_library(tmp_path / "libraries", _UNAVAILABLE_LIBRARY, "UnavailableNode")
    _register(engine, available_json)
    _register(engine, unavailable_json)

    engine.context_manager.push_workflow(workflow_name=file_stem)
    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name=_FLOW_NAME, set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result

    _create_node(engine, "AvailableNode", _AVAILABLE_LIBRARY, "Kept", _FLOW_NAME)
    _create_node(engine, "UnavailableNode", _UNAVAILABLE_LIBRARY, "Vanishing", _FLOW_NAME)
    _set_value(engine, "Kept", "value", "kept value")
    _set_value(engine, "Vanishing", "value2", "vanishing value")

    connection_result = engine.handle_request(
        CreateConnectionRequest(
            source_node_name="Kept",
            source_parameter_name="value",
            target_node_name="Vanishing",
            target_parameter_name="value",
        )
    )
    assert isinstance(connection_result, CreateConnectionResultSuccess), connection_result

    serialize_result = engine.handle_request(SerializeFlowToCommandsRequest(flow_name=_FLOW_NAME))
    assert isinstance(serialize_result, SerializeFlowToCommandsResultSuccess), serialize_result

    save_result = engine.workflow_manager._save_workflow_file_inline(
        destination=ProjectFileDestination(str(tmp_path / f"{file_stem}.py")),
        serialized_flow_commands=serialize_result.serialized_flow_commands,
        file_name=file_stem,
        creation_date=None,
        display_name=None,
        image_path=None,
        description=None,
        is_template=None,
        branched_from=None,
        workflow_shape=None,
        pickle_control_flow_result=False,
    )
    assert isinstance(save_result, SaveWorkflowFileFromSerializedFlowResultSuccess), save_result
    return f"{file_stem}.py"


def _restart_engine(tmp_path: Path) -> Engine:
    """Hand back an engine in the state a just-restarted editor is in: no libraries at all.

    Rebuilding is what makes the second half of each test honest. Dropping only the registry
    entry would leave the library manager still believing the missing library is LOADED, and
    dropping the registry without ``sys.modules`` would leave a library-owned class still
    importable -- so a test would pass on an import a restarted editor could not resolve.
    Registering a library imports its node module under a generated name and aliases it into
    the ``griptape_nodes.node_libraries`` namespace; both live in ``sys.modules``, which is
    process state no engine reset touches.

    Every rebuild in this file goes through here, so no case can accidentally keep the
    modules of a library it means to be missing.
    """
    reset_root_engine()
    LibraryRegistry._clear()
    stale = [
        name for name in sys.modules if name.startswith(("gtn_dynamic_module_", LibraryManager.STABLE_NAMESPACE_PREFIX))
    ]
    for name in stale:
        del sys.modules[name]
    engine = current_engine()
    engine.config_manager.workspace_path = tmp_path
    return engine


def _rebuild_engine_without_library(tmp_path: Path, *, disabled: bool = False) -> Engine:
    """Restart the engine with the available library registered and the other one absent.

    ``disabled=True`` reproduces the reported case: the library is still on disk and still
    named in the user's config, just toggled off, so it is discovered and then refused.
    """
    engine = _restart_engine(tmp_path)
    _register(engine, tmp_path / "libraries" / "AvailableNode" / "griptape_nodes_library.json")
    if disabled:
        engine.library_manager._create_library_info_entry(
            str(tmp_path / "libraries" / "UnavailableNode" / "griptape_nodes_library.json"),
            is_sandbox=False,
            enabled=False,
        )
    return engine


def _run(engine: Engine, relative_file_path: str) -> Any:
    return asyncio.run(engine.workflow_manager.run_workflow(relative_file_path=relative_file_path))


def _register_workflow(engine: Engine, tmp_path: Path, relative_file_path: str) -> str:
    """Put the saved file in the workflow registry so it can be opened by name."""
    metadata = read_workflow_metadata(tmp_path / relative_file_path)
    result = engine.handle_request(RegisterWorkflowRequest(metadata=metadata, file_name=relative_file_path))
    assert isinstance(result, RegisterWorkflowResultSuccess), result
    return derive_registry_key(relative_file_path)


def _node(engine: Engine, node_name: str) -> Any:
    return engine.object_manager.get_object_by_name(node_name)


class TestWorkflowOpensWithoutItsLibrary:
    """The load succeeds and only the missing library's nodes are degraded."""

    @pytest.mark.parametrize("disabled", [False, True], ids=["library_uninstalled", "library_disabled_in_config"])
    def test_workflow_opens(self, engine: Engine, tmp_path: Path, disabled: bool) -> None:  # noqa: FBT001
        del engine  # The fixture builds the first engine; this test replaces it mid-way.
        relative_path = _save_two_library_workflow(tmp_path)

        reopened = _rebuild_engine_without_library(tmp_path, disabled=disabled)
        result = _run(reopened, relative_path)

        assert result.execution_successful, result.execution_details
        # Both nodes exist: the graph is whole, not truncated at the first unusable node.
        assert not isinstance(_node(reopened, "Kept"), ErrorProxyNode)
        assert isinstance(_node(reopened, "Vanishing"), ErrorProxyNode)

    def test_unresolved_library_is_reported_once_with_its_reason(self, engine: Engine, tmp_path: Path) -> None:
        del engine
        relative_path = _save_two_library_workflow(tmp_path)

        reopened = _rebuild_engine_without_library(tmp_path, disabled=True)
        result = _run(reopened, relative_path)

        assert [type(problem) for problem in result.problems] == [LibraryNotRegisteredProblem]
        problem = result.problems[0]
        assert problem.library_name == _UNAVAILABLE_LIBRARY
        # The reason travels with it, so "it's disabled" is not left for the reader to guess --
        # the library is on disk and named in the user's config, just switched off.
        assert problem.reason is not None
        assert "disabled in libraries_to_register" in problem.reason

    def test_a_load_with_placeholders_is_flawed_not_good(self, engine: Engine, tmp_path: Path) -> None:
        """The status is what a caller gates on without parsing prose."""
        del engine
        relative_path = _save_two_library_workflow(tmp_path)

        reopened = _rebuild_engine_without_library(tmp_path, disabled=True)
        result = _run(reopened, relative_path)

        assert result.execution_successful
        assert result.status is WorkflowStatus.FLAWED

    def test_placeholder_names_the_library_and_node_type_it_stands_in_for(self, engine: Engine, tmp_path: Path) -> None:
        del engine
        relative_path = _save_two_library_workflow(tmp_path)

        reopened = _rebuild_engine_without_library(tmp_path)
        _run(reopened, relative_path)

        placeholder = _node(reopened, "Vanishing")
        assert placeholder.original_node_type == "UnavailableNode"
        assert placeholder.original_library_name == _UNAVAILABLE_LIBRARY
        # The proxy is a load failure, not a permission gate.
        assert placeholder.denied_by_policy is False

    def test_values_and_connections_survive_on_both_sides(self, engine: Engine, tmp_path: Path) -> None:
        del engine
        relative_path = _save_two_library_workflow(tmp_path)

        reopened = _rebuild_engine_without_library(tmp_path)
        _run(reopened, relative_path)

        # The intact node keeps its own value...
        assert _get_value(reopened, "Kept", "value") == "kept value"
        # ...and the placeholder keeps the value it was saved with, on a parameter it had to
        # invent, because the class that declared it is gone.
        assert _get_value(reopened, "Vanishing", "value2") == "vanishing value"

        connections = reopened.handle_request(ListConnectionsForNodeRequest(node_name="Kept"))
        assert isinstance(connections, ListConnectionsForNodeResultSuccess), connections
        assert [
            (outgoing.source_parameter_name, outgoing.target_node_name, outgoing.target_parameter_name)
            for outgoing in connections.outgoing_connections
        ] == [("value", "Vanishing", "value")]

    def test_placeholder_refuses_to_run_but_the_graph_stays_editable(self, engine: Engine, tmp_path: Path) -> None:
        del engine
        relative_path = _save_two_library_workflow(tmp_path)

        reopened = _rebuild_engine_without_library(tmp_path)
        _run(reopened, relative_path)

        # Execution is what the missing library costs.
        exceptions = _node(reopened, "Vanishing").validate_before_node_run()
        assert exceptions is not None
        assert _UNAVAILABLE_LIBRARY in str(exceptions[0])

        # Editing the healthy side of the graph is not.
        _set_value(reopened, "Kept", "value2", "edited after reopen")
        assert _get_value(reopened, "Kept", "value2") == "edited after reopen"

        # A placeholder's parameters stay frozen: they belong to a class that is not loaded,
        # so accepting edits would invent state the restored node may not accept.
        rejected = reopened.handle_request(
            SetParameterValueRequest(node_name="Vanishing", parameter_name="value2", value="nope")
        )
        assert isinstance(rejected, SetParameterValueResultFailure)


class TestEveryLibraryUnavailable:
    """A graph made entirely of unavailable nodes still opens; there is nothing left to salvage but the shape."""

    def test_all_nodes_become_placeholders_and_the_flow_survives(self, engine: Engine, tmp_path: Path) -> None:
        del engine
        relative_path = _save_two_library_workflow(tmp_path)

        reopened = _restart_engine(tmp_path)

        result = _run(reopened, relative_path)

        assert result.execution_successful, result.execution_details
        # One problem per library, not one per node...
        assert {problem.library_name for problem in result.problems} == {_AVAILABLE_LIBRARY, _UNAVAILABLE_LIBRARY}
        # ...and both collate into a single warning, because they are the same problem type.
        assert len(reopened.workflow_manager.collate_problems_for_display(result.problems)) == 1
        assert isinstance(_node(reopened, "Kept"), ErrorProxyNode)
        assert isinstance(_node(reopened, "Vanishing"), ErrorProxyNode)
        # The flow itself is engine-owned, so it is there to hold them.
        assert reopened.flow_manager.get_flow_by_name(_FLOW_NAME) is not None


class TestPlaceholdersRoundTripBackToRealNodes:
    """Saving with placeholders and reopening once the library returns restores the real nodes."""

    def test_reopening_with_the_library_available_restores_the_original_node(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        del engine
        relative_path = _save_two_library_workflow(tmp_path)

        # Open without the library, save the graph as it now stands (placeholder and all)...
        degraded = _rebuild_engine_without_library(tmp_path)
        assert _run(degraded, relative_path).execution_successful
        resaved_path = _resave_current_flow(degraded, tmp_path, "resaved_workflow")

        # ...then reopen that file on an engine where the library is back.
        restored = _restart_engine(tmp_path)
        _register(restored, tmp_path / "libraries" / "AvailableNode" / "griptape_nodes_library.json")
        _register(restored, tmp_path / "libraries" / "UnavailableNode" / "griptape_nodes_library.json")

        result = _run(restored, resaved_path)

        assert result.execution_successful, result.execution_details
        assert result.status is WorkflowStatus.GOOD
        assert result.problems == ()
        # The placeholder was a stand-in, not a replacement: the real node comes back.
        assert not isinstance(_node(restored, "Vanishing"), ErrorProxyNode)
        assert _get_value(restored, "Vanishing", "value2") == "vanishing value"


def _resave_current_flow(engine: Engine, tmp_path: Path, file_stem: str) -> str:
    serialize_result = engine.handle_request(SerializeFlowToCommandsRequest(flow_name=_FLOW_NAME))
    assert isinstance(serialize_result, SerializeFlowToCommandsResultSuccess), serialize_result
    save_result = engine.workflow_manager._save_workflow_file_inline(
        destination=ProjectFileDestination(str(tmp_path / f"{file_stem}.py")),
        serialized_flow_commands=serialize_result.serialized_flow_commands,
        file_name=file_stem,
        creation_date=None,
        display_name=None,
        image_path=None,
        description=None,
        is_template=None,
        branched_from=None,
        workflow_shape=None,
        pickle_control_flow_result=False,
    )
    assert isinstance(save_result, SaveWorkflowFileFromSerializedFlowResultSuccess), save_result
    return f"{file_stem}.py"


def _visible_register_failures(engine: Engine, relative_file_path: str) -> tuple[Any, list[str]]:
    """Run the workflow, returning the register-library failures a user would actually see.

    Two things have to be true at once for a failure to reach the editor as a toast: it has to
    be broadcast at all, and it has to carry a detail above DEBUG. The engine's own pre-exec
    registration attempt is broadcast but pinned to DEBUG (``failure_log_level``), so filtering
    on level is what separates "reported quietly, once" from "toasted twice".
    """
    queued: list[Any] = []

    with pytest.MonkeyPatch.context() as monkeypatch:
        original = engine.event_manager.aput_event

        async def record(event: Any) -> None:
            queued.append(event)
            await original(event)

        monkeypatch.setattr(engine.event_manager, "aput_event", record)
        result = _run(engine, relative_file_path)

    visible = []
    for event in queued:
        payload = getattr(getattr(event, "wrapped_event", None), "result", None)
        if not isinstance(payload, RegisterLibraryFromFileResultFailure):
            continue
        visible.extend(message for level, message in _details(payload) if level > logging.DEBUG)
    return result, visible


def _details(payload: Any) -> list[tuple[int, str]]:
    """The (level, message) pairs a result payload carries."""
    assert isinstance(payload.result_details, ResultDetails), payload.result_details
    return [(detail.level, detail.message) for detail in payload.result_details.result_details]


def _strip_metadata_header(workflow_file: Path) -> None:
    """Remove the `# /// script` metadata block, leaving the rest of the file intact."""
    lines = workflow_file.read_text().splitlines()
    closing_index = next(index for index, line in enumerate(lines) if index > 0 and line.strip() == "# ///")
    workflow_file.write_text("\n".join(lines[closing_index + 1 :]))


class TestDuplicateLibraryFailureIsNotBroadcast:
    """The saved file re-registers its own libraries; that second failure must not reach the GUI.

    Paired with the headerless case below: suppression is conditional on the engine having
    already reported the library, so the same in-file failure is silenced in one case and
    broadcast in the other. Suppressing unconditionally would make an unreadable header --
    where the in-file calls are the only record of what the workflow needs -- load silently.
    """

    def test_in_file_registration_failure_is_suppressed(self, engine: Engine, tmp_path: Path) -> None:
        del engine
        relative_path = _save_two_library_workflow(tmp_path)

        reopened = _rebuild_engine_without_library(tmp_path)
        result, visible_failures = _visible_register_failures(reopened, relative_path)

        assert result.execution_successful
        assert result.problems != ()
        assert visible_failures == [], f"unexpected RegisterLibraryFromFile failure on the wire: {visible_failures}"

    def test_failure_is_broadcast_when_the_header_cannot_be_read(self, engine: Engine, tmp_path: Path) -> None:
        del engine
        relative_path = _save_two_library_workflow(tmp_path)
        _strip_metadata_header(tmp_path / relative_path)

        reopened = _rebuild_engine_without_library(tmp_path)
        result, visible_failures = _visible_register_failures(reopened, relative_path)

        # Nothing could be pre-registered, so the engine has nothing of its own to report...
        assert result.problems == ()
        # ...and the file's own registration failure stays visible as the sole signal.
        assert len(visible_failures) == 1
        assert _UNAVAILABLE_LIBRARY in visible_failures[0]


class TestImportAsReferencedSubFlow:
    """Importing a workflow as a subflow reports its unresolved libraries too.

    This is the only result that can: a referenced subflow's libraries are not in the importing
    workflow's own metadata header, so the outer load has nothing to report about them.
    """

    def test_import_succeeds_and_names_the_library(self, engine: Engine, tmp_path: Path) -> None:
        del engine
        relative_path = _save_two_library_workflow(tmp_path, "importable_workflow")
        reopened = _rebuild_engine_without_library(tmp_path, disabled=True)
        workflow_name = _register_workflow(reopened, tmp_path, relative_path)

        # Somewhere to import into.
        reopened.context_manager.push_workflow(workflow_name="host_workflow")
        host_flow = reopened.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="HostFlow", set_as_new_context=False)
        )
        assert isinstance(host_flow, CreateFlowResultSuccess), host_flow

        result = reopened.handle_request(
            ImportWorkflowAsReferencedSubFlowRequest(workflow_name=workflow_name, flow_name=host_flow.flow_name)
        )

        assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultSuccess), result
        warnings = [
            message
            for level, message in _details(result)
            if level == logging.WARNING and _UNAVAILABLE_LIBRARY in message
        ]
        assert len(warnings) == 1, result.result_details


class TestOpenWorkflowRequest:
    """The reported symptom, at the seam the editor's Open Workflow actually calls."""

    def test_run_from_registry_succeeds_and_keeps_the_graph(self, engine: Engine, tmp_path: Path) -> None:
        """`RunWorkflowFromRegistryRequest` must not fail, because failing there wipes the canvas.

        Its failure branch clears all object state, which is why the reported bug left the
        editor empty rather than showing a partly-broken graph.
        """
        del engine
        relative_path = _save_two_library_workflow(tmp_path)
        reopened = _rebuild_engine_without_library(tmp_path, disabled=True)
        workflow_name = _register_workflow(reopened, tmp_path, relative_path)

        result = reopened.handle_request(RunWorkflowFromRegistryRequest(workflow_name=workflow_name))

        assert isinstance(result, RunWorkflowFromRegistryResultSuccess), result
        assert not isinstance(_node(reopened, "Kept"), ErrorProxyNode)
        assert isinstance(_node(reopened, "Vanishing"), ErrorProxyNode)

    def test_the_library_is_named_in_the_result_at_warning_level(self, engine: Engine, tmp_path: Path) -> None:
        del engine
        relative_path = _save_two_library_workflow(tmp_path)
        reopened = _rebuild_engine_without_library(tmp_path, disabled=True)
        workflow_name = _register_workflow(reopened, tmp_path, relative_path)

        result = reopened.handle_request(RunWorkflowFromRegistryRequest(workflow_name=workflow_name))

        warnings = [
            message
            for level, message in _details(result)
            if level == logging.WARNING and _UNAVAILABLE_LIBRARY in message
        ]
        assert len(warnings) == 1, result.result_details


class TestHeadlessLoadRefusesPlaceholders:
    """The tolerance is the editor's trade, not the executor's.

    An artist can see placeholders on a canvas and decide what to do. A headless run has
    nobody to see them, so proceeding would produce output from a graph that is missing nodes.
    `LocalWorkflowExecutor` is the only sender of `RunWorkflowFromScratchRequest`.
    """

    def test_a_flawed_load_is_reported_on_the_result_payload(self, engine: Engine, tmp_path: Path) -> None:
        """The status rides on the payload, so a caller gates without parsing prose."""
        del engine
        relative_path = _save_two_library_workflow(tmp_path)
        reopened = _rebuild_engine_without_library(tmp_path, disabled=True)

        result = asyncio.run(reopened.ahandle_request(RunWorkflowFromScratchRequest(file_path=relative_path)))

        assert isinstance(result, RunWorkflowFromScratchResultSuccess), result
        assert result.status is WorkflowStatus.FLAWED

    def test_a_clean_load_reports_good(self, engine: Engine, tmp_path: Path) -> None:
        """The gate has to distinguish, so a healthy load must not be refused."""
        del engine
        relative_path = _save_two_library_workflow(tmp_path)
        restored = _restart_engine(tmp_path)
        _register(restored, tmp_path / "libraries" / "AvailableNode" / "griptape_nodes_library.json")
        _register(restored, tmp_path / "libraries" / "UnavailableNode" / "griptape_nodes_library.json")

        result = asyncio.run(restored.ahandle_request(RunWorkflowFromScratchRequest(file_path=relative_path)))

        assert isinstance(result, RunWorkflowFromScratchResultSuccess), result
        assert result.status is WorkflowStatus.GOOD

    def test_the_executor_refuses_a_flawed_load(self, engine: Engine, tmp_path: Path) -> None:
        """The graph loaded, but the executor must not run it."""
        del engine
        from griptape_nodes.bootstrap.workflow_executors.local_workflow_executor import (
            LocalExecutorError,
            LocalWorkflowExecutor,
        )

        relative_path = _save_two_library_workflow(tmp_path)
        reopened = _rebuild_engine_without_library(tmp_path, disabled=True)
        del reopened

        with pytest.raises(LocalExecutorError, match="FLAWED"):
            asyncio.run(LocalWorkflowExecutor()._load_workflow_from_path(relative_path))

    def test_the_executor_accepts_a_clean_load(self, engine: Engine, tmp_path: Path) -> None:
        """The refusal must key on the status, not on merely having gone through this path."""
        del engine
        from griptape_nodes.bootstrap.workflow_executors.local_workflow_executor import LocalWorkflowExecutor

        relative_path = _save_two_library_workflow(tmp_path)
        restored = _restart_engine(tmp_path)
        _register(restored, tmp_path / "libraries" / "AvailableNode" / "griptape_nodes_library.json")
        _register(restored, tmp_path / "libraries" / "UnavailableNode" / "griptape_nodes_library.json")

        asyncio.run(LocalWorkflowExecutor()._load_workflow_from_path(relative_path))

        assert not isinstance(_node(restored, "Vanishing"), ErrorProxyNode)
