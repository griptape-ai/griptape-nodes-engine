"""Save-to-disk and load-back fidelity for the workflow codegen pipeline.

Drives a real graph through ``SerializeFlowToCommandsRequest``, writes the generated Python
source to a real file the same way ``SaveWorkflowRequest`` does, then executes the file's
``build_workflow()`` against a cleared engine and inspects the rebuilt state. This is the
outermost seam of the serialization pipeline: it exercises value pooling, codegen, and
deserialization together, the way an artist reopening a saved workflow would exercise them.

A tiny fixture library is written to a temp directory and registered per test, because no
node type is registered by default in a unit test process and the round trip has to go
through real ``CreateNodeRequest`` -> ``LibraryRegistry.create_node()`` resolution on both the
save side and the generated file's own reload side.

Save-path naming, versioning, display-name resolution, and overwrite protection are covered
in ``test_workflow_manager.py`` and are deliberately bypassed here via a plain, unresolved
``ProjectFileDestination`` pointing at a temp workspace.
"""

from __future__ import annotations

import ast
import asyncio
import datetime as datetime_module
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.files.project_file import ProjectFileDestination
from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    AddNodesToNodeGroupRequest,
    AddNodesToNodeGroupResultSuccess,
    CreateNodeRequest,
    CreateNodeResultSuccess,
    GetNodeMetadataRequest,
    GetNodeMetadataResultSuccess,
    SetLockNodeStateRequest,
    SetLockNodeStateResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.parameter_events import (
    GetParameterValueRequest,
    GetParameterValueResultSuccess,
    SetParameterValueRequest,
    SetParameterValueResultSuccess,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    SaveWorkflowFileFromSerializedFlowResultSuccess,
)
from griptape_nodes.utils.version_utils import engine_version

if TYPE_CHECKING:
    from collections.abc import Generator

    from griptape_nodes.retained_mode.engine import Engine

_FLOW_NAME = "ControlFlow_1"

_FIXTURE_NODE_MODULE = '''
"""Minimal fixture nodes for the save/load round-trip suite. Not part of any shipped library."""

from __future__ import annotations

from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_groups.base_node_group import BaseNodeGroup
from griptape_nodes.exe_types.node_types import DataNode


class RoundTripNode(DataNode):
    """A plain data node with two independently connectable any-typed parameters."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        for param_name in ("value", "value2"):
            self.add_parameter(
                Parameter(
                    name=param_name,
                    type="any",
                    default_value=None,
                    tooltip="",
                    allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY, ParameterMode.OUTPUT},
                )
            )

    def process(self) -> None:
        pass


class RoundTripGroupNode(BaseNodeGroup):
    """A plain node group with no data parameters of its own."""

    def process(self) -> Any:
        pass
'''

_FIXTURE_LIBRARY_SCHEMA: dict[str, Any] = {
    "name": "Round Trip Fixture Library",
    "library_schema_version": "0.7.0",
    "metadata": {
        "author": "Test Fixture",
        "description": "Minimal library used by the save/load round-trip unit tests",
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
            "class_name": "RoundTripNode",
            "file_path": "round_trip_fixture_nodes.py",
            "metadata": {"category": "test", "description": "Two any-typed parameters", "display_name": "RoundTrip"},
        },
        {
            "class_name": "RoundTripGroupNode",
            "file_path": "round_trip_fixture_nodes.py",
            "metadata": {"category": "test", "description": "Plain node group", "display_name": "RoundTrip Group"},
        },
    ],
}


class _FrozenDateTime(datetime_module.datetime):
    """A ``datetime`` subclass whose ``now()`` always answers the same instant.

    ``_generate_workflow_metadata_from_commands`` stamps ``last_modified_date`` with
    ``datetime.now(tz=UTC)`` internally, with no caller-supplied override. Two saves of an
    otherwise-identical graph would then differ by that timestamp alone, which would make a
    byte-identical-output assertion fail for a reason that has nothing to do with the
    serializer. Freezing the clock isolates the actual contract under test.
    """

    _FIXED = datetime_module.datetime(2024, 1, 1, tzinfo=datetime_module.UTC)

    @classmethod
    def now(cls, tz: datetime_module.tzinfo | None = None) -> datetime_module.datetime:  # noqa: ARG003
        return cls._FIXED


@pytest.fixture(autouse=True)
def _clear_library_registry_state() -> Generator[None, None, None]:
    """Clear the process-global LibraryRegistry before and after every test in this file.

    ``LibraryRegistry`` keeps its state in ``ClassVar`` dicts the engine does not own (see
    ``node_library/library_registry.py``), so the "Round Trip Fixture Library" this file
    registers per test (``_register_fixture_library``) would otherwise outlive the test and leak
    into whichever test runs next in the same xdist worker. Sibling files
    (``test_node_serialization_commands.py``, ``test_selected_nodes_serialization.py``) clear it
    the same way around their own fixture libraries.
    """
    LibraryRegistry._clear()
    yield
    LibraryRegistry._clear()


def _write_fixture_library(target_dir: Path) -> Path:
    """Write the fixture library's JSON manifest and node module into ``target_dir``."""
    target_dir.mkdir(parents=True, exist_ok=True)
    library_json_path = target_dir / "griptape_nodes_library.json"
    library_json_path.write_text(json.dumps(_FIXTURE_LIBRARY_SCHEMA, indent=2))
    (target_dir / "round_trip_fixture_nodes.py").write_text(_FIXTURE_NODE_MODULE)
    return library_json_path


def _register_fixture_library(engine: Engine, tmp_path: Path) -> str:
    """Register the fixture library into a clean engine and return its library name."""
    library_json = _write_fixture_library(tmp_path / "fixture_library")
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result
    return register_result.library_name


def _fresh_flow(engine: Engine, workflow_name: str, tmp_path: Path) -> tuple[str, str]:
    """Clear all engine state, register the fixture library, and create one empty flow.

    Returns the flow name and the fixture library name.
    """
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    library_name = _register_fixture_library(engine, tmp_path)
    engine.context_manager.push_workflow(workflow_name=workflow_name)
    result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name=_FLOW_NAME, set_as_new_context=False)
    )
    assert isinstance(result, CreateFlowResultSuccess), result
    return result.flow_name, library_name


def _create_round_trip_node(
    engine: Engine, node_name: str, flow_name: str, library_name: str, metadata: dict | None = None
) -> str:
    result = engine.handle_request(
        CreateNodeRequest(
            node_type="RoundTripNode",
            specific_library_name=library_name,
            node_name=node_name,
            override_parent_flow_name=flow_name,
            metadata=metadata,
        )
    )
    assert isinstance(result, CreateNodeResultSuccess), result
    return result.node_name


def _set_value(engine: Engine, node_name: str, parameter_name: str, value: Any) -> None:
    set_result = engine.handle_request(
        SetParameterValueRequest(node_name=node_name, parameter_name=parameter_name, value=value)
    )
    assert isinstance(set_result, SetParameterValueResultSuccess), set_result


def _get_value(engine: Engine, node_name: str, parameter_name: str) -> Any:
    result = engine.handle_request(GetParameterValueRequest(node_name=node_name, parameter_name=parameter_name))
    assert isinstance(result, GetParameterValueResultSuccess), result
    return result.value


def _save_flow_to_disk(engine: Engine, flow_name: str, tmp_path: Path, file_stem: str) -> str:
    """Serialize ``flow_name`` and write it to ``tmp_path`` via the same low-level writer ``SaveWorkflowRequest`` uses."""
    engine.config_manager.workspace_path = tmp_path

    serialize_result = engine.handle_request(SerializeFlowToCommandsRequest(flow_name=flow_name))
    assert isinstance(serialize_result, SerializeFlowToCommandsResultSuccess), serialize_result

    destination = ProjectFileDestination(str(tmp_path / f"{file_stem}.py"))
    save_result = engine.workflow_manager._save_workflow_file_inline(
        destination=destination,
        serialized_flow_commands=serialize_result.serialized_flow_commands,
        file_name=file_stem,
        creation_date=_FrozenDateTime.now(),
        display_name=None,
        image_path=None,
        description=None,
        is_template=None,
        branched_from=None,
        workflow_shape=None,
        pickle_control_flow_result=False,
    )
    assert isinstance(save_result, SaveWorkflowFileFromSerializedFlowResultSuccess), save_result
    return save_result.file_path


def _read_saved_source(file_path: str) -> str:
    source = Path(file_path).read_text()
    ast.parse(source)  # the written file must always be syntactically valid Python
    return source


def _reload_from_disk(engine: Engine, file_path: str) -> None:
    """Tear down the live graph, then exec the saved file's ``build_workflow()`` against the engine."""
    engine.clear_current_workflow_data()

    source = _read_saved_source(file_path)
    exec_globals: dict[str, object] = {"__file__": file_path}
    exec(compile(source, file_path, "exec"), exec_globals)  # noqa: S102
    build_workflow = exec_globals["build_workflow"]
    asyncio.run(build_workflow())  # type: ignore[operator]


def _round_trip_single_value(engine: Engine, tmp_path: Path, file_stem: str, value: Any) -> Any:
    """Build one node with one parameter value, save it, reload it, and return the reloaded value."""
    flow_name, library_name = _fresh_flow(engine, f"{file_stem}_workflow", tmp_path)
    node_name = _create_round_trip_node(engine, "Holder", flow_name, library_name)
    _set_value(engine, node_name, "value", value)

    file_path = _save_flow_to_disk(engine, flow_name, tmp_path, file_stem)
    with pytest.MonkeyPatch.context() as monkeypatch:
        import griptape_nodes.retained_mode.managers.workflow_manager as workflow_manager_module

        monkeypatch.setattr(workflow_manager_module, "datetime", _FrozenDateTime)
        _reload_from_disk(engine, file_path)

    return _get_value(engine, node_name, "value")


class TestScalarValueRoundTrip:
    """A scalar parameter value must come back with the same type and content it was saved with."""

    @pytest.mark.parametrize(
        ("case_name", "value"),
        [
            ("plain_string", "hello"),
            ("empty_string", ""),
            ("positive_int", 7),
            ("zero_int", 0),
            ("negative_int", -42),
            ("large_int", 2**62),
            ("float_value", 3.14),
            ("negative_float", -0.5),
            ("bool_true", True),
            ("bool_false", False),
            ("none_value", None),
            ("unicode_string", "café 寿し 😀"),
            ("quotes_and_newlines", "line one\nline \"two\" and 'three'\ttabbed"),
            ("backslashes", "C:\\path\\to\\file\\n_not_a_newline"),
        ],
    )
    def test_scalar_value_survives_save_and_reload(
        self, engine: Engine, tmp_path: Path, case_name: str, value: Any
    ) -> None:
        reloaded = _round_trip_single_value(engine, tmp_path, case_name, value)

        assert reloaded == value
        assert type(reloaded) is type(value)


class TestContainerValueRoundTrip:
    """Container parameter values must preserve their shape, key types, and nesting."""

    @pytest.mark.parametrize(
        ("case_name", "value"),
        [
            ("empty_dict", {}),
            ("empty_list", []),
            ("nested_dict", {"a": {"b": {"c": [1, 2, 3]}}, "d": None}),
            ("list_of_dicts", [{"x": 1}, {"y": 2}, {"z": [1, 2]}]),
            ("dict_with_non_identifier_keys", {"has space": 1, "has-dash": 2, "123numeric": 3}),
        ],
    )
    def test_container_value_survives_save_and_reload(
        self, engine: Engine, tmp_path: Path, case_name: str, value: Any
    ) -> None:
        reloaded = _round_trip_single_value(engine, tmp_path, case_name, value)

        assert reloaded == value


class TestSharedValueDeduplication:
    """A value shared by two parameters is pooled once but both readers still see it."""

    def test_shared_value_used_by_two_nodes_is_stored_once_and_both_read_back_equal(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        flow_name, library_name = _fresh_flow(engine, "dedup_workflow", tmp_path)
        shared_value = {"shared": ["value", "payload"]}

        node_a = _create_round_trip_node(engine, "NodeA", flow_name, library_name)
        node_b = _create_round_trip_node(engine, "NodeB", flow_name, library_name)
        _set_value(engine, node_a, "value", shared_value)
        _set_value(engine, node_b, "value", shared_value)

        file_path = _save_flow_to_disk(engine, flow_name, tmp_path, "dedup")
        source = _read_saved_source(file_path)

        # The unique-values pool is keyed by the pickled value; the same object must land in
        # exactly one pool entry rather than being duplicated once per referencing parameter.
        assert source.count("pickle.loads(") == 1

        _reload_from_disk(engine, file_path)

        assert _get_value(engine, node_a, "value") == shared_value
        assert _get_value(engine, node_b, "value") == shared_value


class TestNodeMetadataRoundTrip:
    """Node metadata (position, custom keys) survives the save/reload round trip."""

    def test_position_and_custom_metadata_key_round_trip(self, engine: Engine, tmp_path: Path) -> None:
        flow_name, library_name = _fresh_flow(engine, "metadata_workflow", tmp_path)
        node_name = _create_round_trip_node(
            engine,
            "Positioned",
            flow_name,
            library_name,
            metadata={"position": {"x": 123, "y": 456}, "my_custom_key": "my_custom_value"},
        )

        file_path = _save_flow_to_disk(engine, flow_name, tmp_path, "metadata")
        _reload_from_disk(engine, file_path)

        result = engine.handle_request(GetNodeMetadataRequest(node_name=node_name))
        assert isinstance(result, GetNodeMetadataResultSuccess), result
        assert result.metadata["position"] == {"x": 123, "y": 456}
        assert result.metadata["my_custom_key"] == "my_custom_value"


class TestLockStateRoundTrip:
    """A node's lock state is part of what gets saved and must come back the same way."""

    def test_locked_node_reloads_locked(self, engine: Engine, tmp_path: Path) -> None:
        flow_name, library_name = _fresh_flow(engine, "lock_workflow", tmp_path)
        node_name = _create_round_trip_node(engine, "Locked", flow_name, library_name)
        lock_result = engine.handle_request(SetLockNodeStateRequest(node_name=node_name, lock=True))
        assert isinstance(lock_result, SetLockNodeStateResultSuccess), lock_result

        file_path = _save_flow_to_disk(engine, flow_name, tmp_path, "locked")
        _reload_from_disk(engine, file_path)

        reloaded_node = engine.node_manager.get_node_by_name(node_name)
        assert reloaded_node.lock is True

    def test_unlocked_node_reloads_unlocked(self, engine: Engine, tmp_path: Path) -> None:
        flow_name, library_name = _fresh_flow(engine, "unlock_workflow", tmp_path)
        node_name = _create_round_trip_node(engine, "Unlocked", flow_name, library_name)

        file_path = _save_flow_to_disk(engine, flow_name, tmp_path, "unlocked")
        _reload_from_disk(engine, file_path)

        reloaded_node = engine.node_manager.get_node_by_name(node_name)
        assert reloaded_node.lock is False


class TestConnectionsRoundTrip:
    """Data connections, and a node fed by several incoming edges, survive save and reload."""

    def test_data_connection_direction_and_endpoints_survive(self, engine: Engine, tmp_path: Path) -> None:
        flow_name, library_name = _fresh_flow(engine, "connection_workflow", tmp_path)
        source = _create_round_trip_node(engine, "Source", flow_name, library_name)
        target = _create_round_trip_node(engine, "Target", flow_name, library_name)
        _set_value(engine, source, "value", "from source")

        connect_result = engine.handle_request(
            CreateConnectionRequest(
                source_node_name=source,
                source_parameter_name="value",
                target_node_name=target,
                target_parameter_name="value",
            )
        )
        assert isinstance(connect_result, CreateConnectionResultSuccess), connect_result

        file_path = _save_flow_to_disk(engine, flow_name, tmp_path, "connection")
        _reload_from_disk(engine, file_path)

        connections = engine.flow_manager.get_connections()
        reloaded_source = engine.node_manager.get_node_by_name(source)
        reloaded_target = engine.node_manager.get_node_by_name(target)
        outgoing = connections.get_all_outgoing_connections(reloaded_source)
        edge_labels = {
            f"{edge.source_parameter.name}->{edge.target_node.name}.{edge.target_parameter.name}" for edge in outgoing
        }
        assert edge_labels == {f"value->{reloaded_target.name}.value"}

    def test_node_with_several_incoming_edges_keeps_them_all(self, engine: Engine, tmp_path: Path) -> None:
        flow_name, library_name = _fresh_flow(engine, "fan_in_workflow", tmp_path)
        source_a = _create_round_trip_node(engine, "SourceA", flow_name, library_name)
        source_b = _create_round_trip_node(engine, "SourceB", flow_name, library_name)
        target = _create_round_trip_node(engine, "Target", flow_name, library_name)
        _set_value(engine, source_a, "value", "from A")
        _set_value(engine, source_b, "value", "from B")

        for source, target_param in ((source_a, "value"), (source_b, "value2")):
            result = engine.handle_request(
                CreateConnectionRequest(
                    source_node_name=source,
                    source_parameter_name="value",
                    target_node_name=target,
                    target_parameter_name=target_param,
                )
            )
            assert isinstance(result, CreateConnectionResultSuccess), result

        file_path = _save_flow_to_disk(engine, flow_name, tmp_path, "fan_in")
        _reload_from_disk(engine, file_path)

        connections = engine.flow_manager.get_connections()
        reloaded_target = engine.node_manager.get_node_by_name(target)
        incoming = connections.get_all_incoming_connections(reloaded_target)
        edge_labels = {
            f"{edge.source_node.name}.{edge.source_parameter.name}->{edge.target_parameter.name}" for edge in incoming
        }
        assert edge_labels == {"SourceA.value->value", "SourceB.value->value2"}


class TestUnserializableParameterRoundTrip:
    """A parameter opted out of serialization must not sink the rest of the graph on reload."""

    def test_serializable_false_parameter_reloads_unresolved_rest_of_graph_intact(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        from griptape_nodes.exe_types.node_types import NodeResolutionState

        flow_name, library_name = _fresh_flow(engine, "unserializable_workflow", tmp_path)
        quiet_node = _create_round_trip_node(engine, "Quiet", flow_name, library_name)
        loud_node = _create_round_trip_node(engine, "Loud", flow_name, library_name)

        _set_value(engine, quiet_node, "value", object())
        parameter = engine.node_manager.get_node_by_name(quiet_node).get_parameter_by_name("value")
        assert parameter is not None
        parameter.serializable = False

        _set_value(engine, loud_node, "value", "still here")

        file_path = _save_flow_to_disk(engine, flow_name, tmp_path, "unserializable")
        _reload_from_disk(engine, file_path)

        reloaded_quiet = engine.node_manager.get_node_by_name(quiet_node)
        assert reloaded_quiet.state == NodeResolutionState.UNRESOLVED
        assert _get_value(engine, loud_node, "value") == "still here"


class TestNodeGroupRoundTrip:
    """A node group's membership survives being saved to disk and reloaded."""

    def test_group_children_are_members_after_reload(self, engine: Engine, tmp_path: Path) -> None:
        from griptape_nodes.exe_types.node_groups.base_node_group import BaseNodeGroup

        flow_name, library_name = _fresh_flow(engine, "group_workflow", tmp_path)
        group_result = engine.handle_request(
            CreateNodeRequest(
                node_type="RoundTripGroupNode",
                specific_library_name=library_name,
                node_name="MyGroup",
                override_parent_flow_name=flow_name,
            )
        )
        assert isinstance(group_result, CreateNodeResultSuccess), group_result
        group_name = group_result.node_name

        child_a = _create_round_trip_node(engine, "ChildA", flow_name, library_name)
        child_b = _create_round_trip_node(engine, "ChildB", flow_name, library_name)
        add_result = engine.handle_request(
            AddNodesToNodeGroupRequest(node_names=[child_a, child_b], node_group_name=group_name, flow_name=flow_name)
        )
        assert isinstance(add_result, AddNodesToNodeGroupResultSuccess), add_result

        file_path = _save_flow_to_disk(engine, flow_name, tmp_path, "group")
        _reload_from_disk(engine, file_path)

        reloaded_group = engine.node_manager.get_node_by_name(group_name)
        assert isinstance(reloaded_group, BaseNodeGroup)
        assert child_a in reloaded_group.nodes
        assert child_b in reloaded_group.nodes


class TestSubFlowRoundTrip:
    """A nested child Flow, its node membership, and its connections survive save and reload.

    ``_save_flow_to_disk`` serializes only the top-level flow, but ``on_serialize_flow_to_commands``
    recurses into every nested child Flow (``sub_flows_commands``) and the codegen
    (``_generate_flow_code``) emits nested flow-creation code for each one. This is the first test
    in the suite that actually executes that nested codegen against a real engine, rather than
    only ``ast.parse``-ing it (see ``test_workflow_codegen_serialization.py``) or inspecting the
    command objects without saving to disk (see ``test_flow_serialization_commands.py``).
    """

    def test_child_flow_nodes_and_connections_survive_save_and_reload(self, engine: Engine, tmp_path: Path) -> None:
        flow_name, library_name = _fresh_flow(engine, "subflow_workflow", tmp_path)

        child_flow_result = engine.handle_request(
            CreateFlowRequest(parent_flow_name=flow_name, flow_name="ChildFlow", set_as_new_context=False)
        )
        assert isinstance(child_flow_result, CreateFlowResultSuccess), child_flow_result
        child_flow_name = child_flow_result.flow_name

        parent_node = _create_round_trip_node(engine, "ParentNode", flow_name, library_name)
        child_a = _create_round_trip_node(engine, "ChildA", child_flow_name, library_name)
        child_b = _create_round_trip_node(engine, "ChildB", child_flow_name, library_name)
        _set_value(engine, parent_node, "value", "from parent")
        _set_value(engine, child_a, "value", "from child a")

        # A connection entirely inside the child Flow.
        internal_connection = engine.handle_request(
            CreateConnectionRequest(
                source_node_name=child_a,
                source_parameter_name="value",
                target_node_name=child_b,
                target_parameter_name="value",
            )
        )
        assert isinstance(internal_connection, CreateConnectionResultSuccess), internal_connection

        # A connection crossing the parent/child boundary.
        boundary_connection = engine.handle_request(
            CreateConnectionRequest(
                source_node_name=parent_node,
                source_parameter_name="value",
                target_node_name=child_b,
                target_parameter_name="value2",
            )
        )
        assert isinstance(boundary_connection, CreateConnectionResultSuccess), boundary_connection

        file_path = _save_flow_to_disk(engine, flow_name, tmp_path, "subflow")
        _reload_from_disk(engine, file_path)

        # The child Flow exists after reload, and its nodes are parented to it.
        reloaded_child_flow = engine.flow_manager.get_flow_by_name(child_flow_name)
        assert reloaded_child_flow.name == child_flow_name
        assert engine.node_manager.get_node_parent_flow_by_name(child_a) == child_flow_name
        assert engine.node_manager.get_node_parent_flow_by_name(child_b) == child_flow_name

        connections = engine.flow_manager.get_connections()
        reloaded_child_a = engine.node_manager.get_node_by_name(child_a)
        reloaded_parent_node = engine.node_manager.get_node_by_name(parent_node)

        internal_outgoing = connections.get_all_outgoing_connections(reloaded_child_a)
        internal_labels = {
            f"{edge.source_parameter.name}->{edge.target_node.name}.{edge.target_parameter.name}"
            for edge in internal_outgoing
        }
        assert internal_labels == {f"value->{child_b}.value"}

        boundary_outgoing = connections.get_all_outgoing_connections(reloaded_parent_node)
        boundary_labels = {
            f"{edge.source_parameter.name}->{edge.target_node.name}.{edge.target_parameter.name}"
            for edge in boundary_outgoing
        }
        assert boundary_labels == {f"value->{child_b}.value2"}

        assert _get_value(engine, child_a, "value") == "from child a"
        assert _get_value(engine, parent_node, "value") == "from parent"


class TestDeterministicSaveOutput:
    """Whether saving the same live graph twice at the same instant must not churn the diff.

    Pending the ruling in #5441 on whether saved workflow files must be diff-stable. The
    mechanism below (a fresh UUID pool key minted on every serialization) is real and
    deterministic; whether it should be fixed depends on that decision.
    """

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "DECISION: pending #5441 (must saved workflow files be diff-stable?). The disk-save "
            "path (SaveWorkflowRequest -> SerializeFlowToCommandsRequest -> "
            "on_serialize_node_to_commands -> handle_parameter_value_saving -> "
            "_handle_value_hashing, all in node_manager.py) mints a fresh str(uuid4()) pool key "
            "every time a value is serialized, so two saves of the identical, unedited graph land "
            "the same pickled bytes under two different unique_values_dict keys. If #5441 rules "
            "that saves must be diff-stable: saving an unedited graph twice in a row must not move "
            "the diff, since nothing about the graph changed, and this test should be promoted. If "
            "not: delete this test, since it asserts a contract nobody has ratified. - see #5441"
        ),
    )
    def test_two_saves_of_the_same_graph_are_byte_identical(self, engine: Engine, tmp_path: Path) -> None:
        flow_name, library_name = _fresh_flow(engine, "determinism_workflow", tmp_path)
        node_name = _create_round_trip_node(engine, "Holder", flow_name, library_name)
        _set_value(engine, node_name, "value", {"a": 1, "b": [1, 2, 3]})

        with pytest.MonkeyPatch.context() as monkeypatch:
            import griptape_nodes.retained_mode.managers.workflow_manager as workflow_manager_module

            monkeypatch.setattr(workflow_manager_module, "datetime", _FrozenDateTime)

            first_path = _save_flow_to_disk(engine, flow_name, tmp_path, "first_save")
            second_path = _save_flow_to_disk(engine, flow_name, tmp_path, "second_save")

        first_source = _read_saved_source(first_path)
        second_source = _read_saved_source(second_path)

        # Only the embedded workflow-name metadata line legitimately differs (the two files were
        # saved under different names). Everything else, including the node and flow creation
        # lines that assign to `<thing>N_name`, must match.
        first_lines = [line for line in first_source.splitlines() if not line.strip().startswith("# name = ")]
        second_lines = [line for line in second_source.splitlines() if not line.strip().startswith("# name = ")]
        assert first_lines == second_lines


class TestIdempotentSaveLoadSave:
    """Whether save, reload, and save again should reproduce the first file exactly.

    Pending the ruling in #5441 on whether saved workflow files must be diff-stable. If it
    rules yes, this is the strongest single guarantee in the pipeline: reopening and resaving
    a workflow with no edits should not move anything, and any drift would mean some piece of
    state is not round-tripping losslessly through the generated code.
    """

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "DECISION: pending #5441 (must saved workflow files be diff-stable?). The "
            "unique-values pool on the disk-save path is keyed by a fresh str(uuid4()) on every "
            "serialization (node_manager.py handle_parameter_value_saving -> "
            "_handle_value_hashing, reached via SerializeFlowToCommandsRequest -> "
            "on_serialize_node_to_commands), so reloading a saved workflow and resaving it "
            "without any edits mints new pool keys for the same values and the resave diffs "
            "against the original. If #5441 rules that saves must be diff-stable: a "
            "reopen-and-resave with no edits must reproduce the original file exactly, and this "
            "test should be promoted. If not: delete this test, since it asserts a contract nobody "
            "has ratified. - see #5441"
        ),
    )
    def test_resaving_a_freshly_reloaded_workflow_reproduces_the_first_save(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        flow_name, library_name = _fresh_flow(engine, "idempotent_workflow", tmp_path)
        node_name = _create_round_trip_node(engine, "Holder", flow_name, library_name)
        _set_value(engine, node_name, "value", {"a": 1, "b": [1, 2, 3]})
        _set_value(engine, node_name, "value2", "some text")

        with pytest.MonkeyPatch.context() as monkeypatch:
            import griptape_nodes.retained_mode.managers.workflow_manager as workflow_manager_module

            monkeypatch.setattr(workflow_manager_module, "datetime", _FrozenDateTime)

            first_path = _save_flow_to_disk(engine, flow_name, tmp_path, "roundtrip")
            first_source = _read_saved_source(first_path)

            _reload_from_disk(engine, first_path)

            second_path = _save_flow_to_disk(engine, flow_name, tmp_path, "roundtrip_resaved")
            second_source = _read_saved_source(second_path)

        # Only the embedded workflow-name metadata line legitimately differs (the resave was
        # written under a different name). Everything else, including the node and flow creation
        # lines that assign to `<thing>N_name`, must match.
        first_lines = [line for line in first_source.splitlines() if not line.strip().startswith("# name = ")]
        second_lines = [line for line in second_source.splitlines() if not line.strip().startswith("# name = ")]
        assert first_lines == second_lines
