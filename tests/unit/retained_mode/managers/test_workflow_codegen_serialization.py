"""Tests for the AST-based workflow file codegen: SerializedFlowCommands -> saved `.py` source.

These tests build `SerializedFlowCommands`/`SerializedNodeCommands` by hand rather than driving them
through the engine, so each codegen shape (nested flows, shared values, missing endpoints, dynamic
module pickling) can be constructed directly without needing a real connectable node graph. Library
and node type names below are fabricated on purpose: codegen only writes out whatever the serialized
commands say, it never needs the library to actually be registered.
"""

from __future__ import annotations

import ast
import logging
import sys
import types
from typing import TYPE_CHECKING, Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from griptape_nodes.exe_types.node_types import NodeDependencies
from griptape_nodes.node_library.workflow_registry import WorkflowMetadata
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, SerializedFlowCommands
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    SerializedNodeCommands,
    SetLockNodeStateRequest,
)
from griptape_nodes.retained_mode.events.parameter_events import AddParameterToNodeRequest, SetParameterValueRequest
from griptape_nodes.retained_mode.events.variable_events import CreateVariableRequest
from griptape_nodes.retained_mode.events.workflow_events import ImportWorkflowAsReferencedSubFlowRequest
from griptape_nodes.retained_mode.managers.workflow_manager import ImportRecorder, WorkflowCodegenState, WorkflowManager
from griptape_nodes.utils.ast_utils import rewrite_string_comments

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine

# --- Shared fixture builders (local to this lane; not shared with other test files) ---


def _node_dependencies() -> NodeDependencies:
    return NodeDependencies()


def _empty_flow_commands(**overrides: Any) -> SerializedFlowCommands:
    """Build a shape-free SerializedFlowCommands, overridable field by field."""
    defaults: dict[str, Any] = {
        "flow_initialization_command": None,
        "serialized_node_commands": [],
        "serialized_connections": [],
        "unique_parameter_uuid_to_values": {},
        "set_parameter_value_commands": {},
        "set_lock_commands_per_node": {},
        "sub_flows_commands": [],
        "node_dependencies": _node_dependencies(),
        "node_types_used": set(),
    }
    defaults.update(overrides)
    return SerializedFlowCommands(**defaults)


def _node_command(  # noqa: PLR0913
    node_name: str,
    *,
    node_type: str = "FakeNodeType",
    library: str | None = "Fake Library",
    node_uuid: str | None = None,
    element_modification_commands: list | None = None,
    metadata: dict | None = None,
) -> SerializedNodeCommands:
    """Build a SerializedNodeCommands for a fabricated node type; codegen never touches the library."""
    kwargs: dict[str, Any] = {
        "create_node_command": CreateNodeRequest(
            node_type=node_type,
            specific_library_name=library,
            node_name=node_name,
            metadata=metadata,
            initial_setup=True,
        ),
        "element_modification_commands": element_modification_commands or [],
        "node_dependencies": _node_dependencies(),
    }
    if node_uuid is not None:
        kwargs["node_uuid"] = SerializedNodeCommands.NodeUUID(node_uuid)
    return SerializedNodeCommands(**kwargs)


def _minimal_metadata(name: str = "codegen_test") -> WorkflowMetadata:
    return WorkflowMetadata(
        name=name,
        schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
        engine_version_created_with="0.0.0",
        node_libraries_referenced=[],
    )


def _calls_named(tree: ast.AST, name: str) -> list[ast.Call]:
    """Every `name(...)` call anywhere in an AST subtree, matched by bare function name."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    ]


def _kwargs_of(call: ast.Call) -> dict[str, ast.expr]:
    return {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}


def _constant_value(expr: ast.expr) -> Any:
    """Narrow an ast.expr believed to be an ast.Constant and return its value."""
    assert isinstance(expr, ast.Constant)
    return expr.value


class _DynamicLibraryClass:
    """Stand-in for a class whose real __module__ is a dynamically loaded library module.

    Declared at module scope (not nested in a class) so its true __module__ is this test file's
    own importable module -- the tests below temporarily repoint it to fabricate a "dynamic module"
    before-state, then repoint it to this true value to stand in for a "stable namespace".
    """

    def __init__(self) -> None:
        self.value = 1


class TestFlowHasContentToGenerate:
    """`_flow_has_content_to_generate` decides whether a Flow is worth a context block at all."""

    def test_fully_empty_flow_has_no_content(self) -> None:
        assert WorkflowManager._flow_has_content_to_generate(_empty_flow_commands()) is False

    def test_node_commands_count_as_content(self) -> None:
        flow = _empty_flow_commands(serialized_node_commands=[_node_command("node_a")])
        assert WorkflowManager._flow_has_content_to_generate(flow) is True

    def test_connections_count_as_content(self) -> None:
        connection = SerializedFlowCommands.IndirectConnectionSerialization(
            source_node_uuid=SerializedNodeCommands.NodeUUID("a"),
            source_parameter_name="out",
            target_node_uuid=SerializedNodeCommands.NodeUUID("b"),
            target_parameter_name="in",
        )
        flow = _empty_flow_commands(serialized_connections=[connection])
        assert WorkflowManager._flow_has_content_to_generate(flow) is True

    def test_set_parameter_value_commands_count_as_content(self) -> None:
        flow = _empty_flow_commands(set_parameter_value_commands={SerializedNodeCommands.NodeUUID("a"): []})
        assert WorkflowManager._flow_has_content_to_generate(flow) is True

    def test_sub_flows_count_as_content(self) -> None:
        flow = _empty_flow_commands(sub_flows_commands=[_empty_flow_commands()])
        assert WorkflowManager._flow_has_content_to_generate(flow) is True

    def test_lock_commands_count_as_content(self) -> None:
        flow = _empty_flow_commands(
            set_lock_commands_per_node={
                SerializedNodeCommands.NodeUUID("a"): SetLockNodeStateRequest(node_name=None, lock=True)
            }
        )
        assert WorkflowManager._flow_has_content_to_generate(flow) is True

    def test_variable_commands_count_as_content(self) -> None:
        variable_command = SerializedFlowCommands.SerializedVariableCommand(
            create_variable_command=CreateVariableRequest(
                name="v", type="str", is_global=False, value=None, owning_flow="flow"
            ),
            unique_value_uuid=SerializedNodeCommands.UniqueParameterValueUUID(str(uuid4())),
        )
        flow = _empty_flow_commands(serialized_variable_commands=[variable_command])
        assert WorkflowManager._flow_has_content_to_generate(flow) is True


class TestGenerateFlowInitializationCode:
    """The statements that bring one Flow into existence, dispatched by initialization command type."""

    def test_none_command_emits_nothing(self, engine: Engine) -> None:
        statements = engine.workflow_manager._generate_flow_initialization_code(
            flow_initialization_command=None,
            import_recorder=ImportRecorder(),
            codegen_state=WorkflowCodegenState(),
            flow_creation_index=0,
            parent_flow_creation_index=None,
        )
        assert statements == []

    def test_create_flow_command_registers_subflow_name_variable(self, engine: Engine) -> None:
        codegen_state = WorkflowCodegenState()
        command = CreateFlowRequest(parent_flow_name=None, flow_name="named_flow")
        statements = engine.workflow_manager._generate_flow_initialization_code(
            flow_initialization_command=command,
            import_recorder=ImportRecorder(),
            codegen_state=codegen_state,
            flow_creation_index=5,
            parent_flow_creation_index=None,
        )
        assert len(statements) > 0
        assert codegen_state.subflow_name_to_variable_name["named_flow"] == "flow5_name"

    def test_import_workflow_command_emits_statements(self, engine: Engine) -> None:
        command = ImportWorkflowAsReferencedSubFlowRequest(workflow_name="referenced_workflow")
        statements = engine.workflow_manager._generate_flow_initialization_code(
            flow_initialization_command=command,
            import_recorder=ImportRecorder(),
            codegen_state=WorkflowCodegenState(),
            flow_creation_index=0,
            parent_flow_creation_index=None,
        )
        assert len(statements) > 0

    def test_unrecognized_command_type_raises_type_error(self, engine: Engine) -> None:
        """A flow-creation command this codegen has never learned to write must fail the save loudly.

        Silently emitting nothing would write a file whose Flow is never created, so every node
        inside it lands wherever the script happens to be pointing -- a wrong graph that loads
        without complaint. That is worse than a save the artist knows did not happen.
        """
        with pytest.raises(TypeError):
            engine.workflow_manager._generate_flow_initialization_code(
                flow_initialization_command=object(),  # type: ignore[arg-type]
                import_recorder=ImportRecorder(),
                codegen_state=WorkflowCodegenState(),
                flow_creation_index=0,
                parent_flow_creation_index=None,
            )


class TestGenerateAssignFlowContext:
    """The `with` block that scopes graph-building calls into the right Flow."""

    def test_none_command_looks_up_the_current_flow_by_name(self, engine: Engine) -> None:
        with_stmt = engine.workflow_manager._generate_assign_flow_context(
            flow_initialization_command=None, flow_creation_index=0
        )
        source = ast.unparse(with_stmt)
        assert "GriptapeNodes.ContextManager().get_current_flow().flow_name" in source

    def test_create_flow_command_references_its_own_flow_variable(self, engine: Engine) -> None:
        command = CreateFlowRequest(parent_flow_name=None, flow_name="my_flow")
        with_stmt = engine.workflow_manager._generate_assign_flow_context(
            flow_initialization_command=command, flow_creation_index=3
        )
        source = ast.unparse(with_stmt)
        assert "GriptapeNodes.ContextManager().flow(flow3_name)" in source


class TestGenerateCreateFlow:
    """The statement that issues CreateFlowRequest and captures the resulting flow name."""

    def test_emits_awaited_call_assigned_to_flow_name_variable(self, engine: Engine) -> None:
        command = CreateFlowRequest(parent_flow_name=None, flow_name="root_flow", set_as_new_context=False)
        import_recorder = ImportRecorder()
        module = engine.workflow_manager._generate_create_flow(command, import_recorder, flow_creation_index=0)
        source = ast.unparse(module)
        assert "flow0_name = (await GriptapeNodes.ahandle_request(CreateFlowRequest(" in source
        assert ").flow_name" in source
        assert "CreateFlowRequest" in import_recorder.from_imports.get(
            "griptape_nodes.retained_mode.events.flow_events", set()
        )

    def test_parent_flow_name_becomes_a_variable_reference_not_a_string(self, engine: Engine) -> None:
        command = CreateFlowRequest(parent_flow_name="parent_flow", flow_name="child_flow")
        module = engine.workflow_manager._generate_create_flow(
            command, ImportRecorder(), flow_creation_index=1, parent_flow_creation_index=0
        )
        call = _calls_named(module, "CreateFlowRequest")[0]
        kwargs = _kwargs_of(call)
        assert isinstance(kwargs["parent_flow_name"], ast.Name)
        assert kwargs["parent_flow_name"].id == "flow0_name"

    def test_omits_optional_fields_left_at_their_default(self, engine: Engine) -> None:
        command = CreateFlowRequest(parent_flow_name=None, flow_name="root_flow")
        module = engine.workflow_manager._generate_create_flow(command, ImportRecorder(), flow_creation_index=0)
        call = _calls_named(module, "CreateFlowRequest")[0]
        kwargs = _kwargs_of(call)
        assert "set_as_new_context" not in kwargs
        assert "metadata" not in kwargs
        assert _constant_value(kwargs["flow_name"]) == "root_flow"
        # parent_flow_name has no dataclass default (it's required), so it is always written,
        # even when the value itself is None.
        assert _constant_value(kwargs["parent_flow_name"]) is None


class TestGenerateImportWorkflow:
    """The statement that issues ImportWorkflowAsReferencedSubFlowRequest for a referenced workflow."""

    def test_emits_awaited_call_using_created_flow_name_attribute(self, engine: Engine) -> None:
        command = ImportWorkflowAsReferencedSubFlowRequest(workflow_name="referenced_workflow")
        import_recorder = ImportRecorder()
        module = engine.workflow_manager._generate_import_workflow(command, import_recorder, flow_creation_index=2)
        source = ast.unparse(module)
        assert "flow2_name = (await GriptapeNodes.ahandle_request(ImportWorkflowAsReferencedSubFlowRequest(" in source
        assert ").created_flow_name" in source
        assert "ImportWorkflowAsReferencedSubFlowRequest" in import_recorder.from_imports.get(
            "griptape_nodes.retained_mode.events.workflow_events", set()
        )


class TestGenerateNodeCreationCode:
    """The statement (plus optional element-modification block) that recreates one node."""

    def test_names_node_type_library_and_reapplies_metadata(self, engine: Engine) -> None:
        node_command = _node_command(
            "node_a", node_type="FakeType", library="Fake Library", metadata={"position": {"x": 10, "y": 20}}
        )
        statements = engine.workflow_manager._generate_node_creation_code(
            node_command,
            node_index=0,
            import_recorder=ImportRecorder(),
            node_uuid_to_node_variable_name={},
            subflow_name_to_variable_name={},
        )
        module = ast.Module(body=statements, type_ignores=[])
        call = _calls_named(module, "CreateNodeRequest")[0]
        kwargs = _kwargs_of(call)
        assert _constant_value(kwargs["node_type"]) == "FakeType"
        assert _constant_value(kwargs["specific_library_name"]) == "Fake Library"
        assert ast.literal_eval(kwargs["metadata"]) == {"position": {"x": 10, "y": 20}}

    def test_registers_node_variable_name_for_downstream_references(self, engine: Engine) -> None:
        node_command = _node_command("node_a", node_uuid="uuid-a")
        node_uuid_to_node_variable_name: dict = {}
        engine.workflow_manager._generate_node_creation_code(
            node_command,
            node_index=7,
            import_recorder=ImportRecorder(),
            node_uuid_to_node_variable_name=node_uuid_to_node_variable_name,
            subflow_name_to_variable_name={},
        )
        assert node_uuid_to_node_variable_name[SerializedNodeCommands.NodeUUID("uuid-a")] == "node7_name"

    def test_no_element_modification_commands_omits_with_block(self, engine: Engine) -> None:
        node_command = _node_command("node_a")
        statements = engine.workflow_manager._generate_node_creation_code(
            node_command,
            node_index=0,
            import_recorder=ImportRecorder(),
            node_uuid_to_node_variable_name={},
            subflow_name_to_variable_name={},
        )
        assert not any(isinstance(stmt, ast.With) for stmt in statements)

    def test_element_modification_commands_run_inside_node_context_and_skip_defaults(self, engine: Engine) -> None:
        add_parameter_command = AddParameterToNodeRequest(parameter_name="extra", default_value="hi", type="str")
        node_command = _node_command("node_a", element_modification_commands=[add_parameter_command])
        statements = engine.workflow_manager._generate_node_creation_code(
            node_command,
            node_index=0,
            import_recorder=ImportRecorder(),
            node_uuid_to_node_variable_name={},
            subflow_name_to_variable_name={},
        )
        with_stmts: list[ast.stmt] = [stmt for stmt in statements if isinstance(stmt, ast.With)]
        assert len(with_stmts) == 1
        module = ast.Module(body=with_stmts, type_ignores=[])
        call = _calls_named(module, "AddParameterToNodeRequest")[0]
        kwargs = _kwargs_of(call)
        assert _constant_value(kwargs["parameter_name"]) == "extra"
        assert _constant_value(kwargs["default_value"]) == "hi"
        # node_name stays at its default (None): the call runs inside the node's own context,
        # so writing it explicitly would be redundant.
        assert "node_name" not in kwargs
        source = ast.unparse(with_stmts[0])
        assert "GriptapeNodes.ContextManager().node(node0_name)" in source


class TestGenerateConnectionsCode:
    """The statements that reconnect a Flow's nodes once every node in the subtree exists."""

    def test_emits_connection_referencing_both_endpoint_variables(self, engine: Engine) -> None:
        connection = SerializedFlowCommands.IndirectConnectionSerialization(
            source_node_uuid=SerializedNodeCommands.NodeUUID("a"),
            source_parameter_name="out",
            target_node_uuid=SerializedNodeCommands.NodeUUID("b"),
            target_parameter_name="in",
        )
        statements = engine.workflow_manager._generate_connections_code(
            serialized_connections=[connection],
            node_uuid_to_node_variable_name={
                SerializedNodeCommands.NodeUUID("a"): "node0_name",
                SerializedNodeCommands.NodeUUID("b"): "node1_name",
            },
            import_recorder=ImportRecorder(),
        )
        module = ast.Module(body=statements, type_ignores=[])
        call = _calls_named(module, "CreateConnectionRequest")[0]
        kwargs = _kwargs_of(call)
        assert isinstance(kwargs["source_node_name"], ast.Name)
        assert kwargs["source_node_name"].id == "node0_name"
        assert isinstance(kwargs["target_node_name"], ast.Name)
        assert kwargs["target_node_name"].id == "node1_name"
        assert _constant_value(kwargs["source_parameter_name"]) == "out"
        assert _constant_value(kwargs["target_parameter_name"]) == "in"

    def test_missing_endpoint_is_skipped_and_logged_not_raised(
        self, engine: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A connection naming a node this Flow never wrote must not fail the whole save.

        Which Flow writes a given edge depends on traversal order, so losing one edge is a smaller
        harm than failing the save outright.
        """
        connection = SerializedFlowCommands.IndirectConnectionSerialization(
            source_node_uuid=SerializedNodeCommands.NodeUUID("missing"),
            source_parameter_name="out",
            target_node_uuid=SerializedNodeCommands.NodeUUID("b"),
            target_parameter_name="in",
        )
        with caplog.at_level(logging.ERROR, logger="griptape_nodes"):
            statements = engine.workflow_manager._generate_connections_code(
                serialized_connections=[connection],
                node_uuid_to_node_variable_name={SerializedNodeCommands.NodeUUID("b"): "node1_name"},
                import_recorder=ImportRecorder(),
            )
        assert statements == []
        assert any("were never written to the file" in record.getMessage() for record in caplog.records)


class TestWorkflowCodegenStateConnectionDedup:
    """A boundary-crossing edge is reported by every ancestor Flow; only one may emit it."""

    @staticmethod
    def _connection() -> SerializedFlowCommands.IndirectConnectionSerialization:
        return SerializedFlowCommands.IndirectConnectionSerialization(
            source_node_uuid=SerializedNodeCommands.NodeUUID("a"),
            source_parameter_name="out",
            target_node_uuid=SerializedNodeCommands.NodeUUID("b"),
            target_parameter_name="in",
        )

    def test_first_claim_returns_the_connection(self) -> None:
        codegen_state = WorkflowCodegenState()
        claimed = codegen_state.take_unemitted_connections([self._connection()])
        assert len(claimed) == 1

    def test_second_claim_of_the_same_edge_returns_nothing(self) -> None:
        codegen_state = WorkflowCodegenState()
        codegen_state.take_unemitted_connections([self._connection()])
        claimed_again = codegen_state.take_unemitted_connections([self._connection()])
        assert claimed_again == []


class TestGenerateUniqueValuesCode:
    """The pickled value pool shared by every SetParameterValueRequest in the file."""

    def test_empty_dict_returns_empty_module(self, engine: Engine) -> None:
        module = engine.workflow_manager._generate_unique_values_code(
            unique_parameter_uuid_to_values={}, prefix="top_level", import_recorder=ImportRecorder()
        )
        assert module.body == []

    def test_emits_pickle_loads_call_per_uuid_and_records_pickle_import(self, engine: Engine) -> None:
        uuid_a = SerializedNodeCommands.UniqueParameterValueUUID(str(uuid4()))
        uuid_b = SerializedNodeCommands.UniqueParameterValueUUID(str(uuid4()))
        import_recorder = ImportRecorder()
        module = engine.workflow_manager._generate_unique_values_code(
            unique_parameter_uuid_to_values={uuid_a: "value-a", uuid_b: 42},
            prefix="top_level",
            import_recorder=import_recorder,
        )
        source = ast.unparse(module)
        expected_pickle_calls = 2
        assert source.count("pickle.loads(") == expected_pickle_calls
        assert "pickle" in import_recorder.imports

        dict_assign = next(stmt for stmt in module.body if isinstance(stmt, ast.Assign))
        assert isinstance(dict_assign.targets[0], ast.Name)
        assert dict_assign.targets[0].id == "top_level_unique_values_dict"
        keys = [key.value for key in dict_assign.value.keys]  # type: ignore[union-attr]
        assert set(keys) == {uuid_a, uuid_b}

    def test_comment_lines_survive_rewrite_as_real_comments(self, engine: Engine) -> None:
        """ast.unparse cannot emit `#` comments directly; they are smuggled in as bare string statements.

        rewrite_string_comments unwraps them afterward, same as the save pipeline does.
        """
        module = engine.workflow_manager._generate_unique_values_code(
            unique_parameter_uuid_to_values={SerializedNodeCommands.UniqueParameterValueUUID(str(uuid4())): "value"},
            prefix="top_level",
            import_recorder=ImportRecorder(),
        )
        raw_source = ast.unparse(module)
        assert not any(line.strip().startswith("#") for line in raw_source.splitlines()), (
            "ast.unparse cannot emit real comments; they must still be smuggled in as quoted strings"
        )
        rewritten = rewrite_string_comments(raw_source)
        assert "# 1. We've collated all of the unique parameter values into a dictionary" in rewritten


class TestGenerateSetParameterValueForNode:
    """The per-node `with` block that restores saved values and lock state."""

    def test_no_values_and_no_lock_emits_nothing(self, engine: Engine) -> None:
        statements = engine.workflow_manager._generate_set_parameter_value_for_node(
            "node0_name", [], "top_level_unique_values_dict", ImportRecorder(), lock_node_command=None
        )
        assert statements == []

    def test_lock_only_node_still_gets_a_context_block(self, engine: Engine) -> None:
        lock_command = SetLockNodeStateRequest(node_name=None, lock=True)
        statements = engine.workflow_manager._generate_set_parameter_value_for_node(
            "node0_name", [], "top_level_unique_values_dict", ImportRecorder(), lock_node_command=lock_command
        )
        assert len(statements) == 1
        with_stmt = statements[0]
        assert isinstance(with_stmt, ast.With)
        source = ast.unparse(with_stmt)
        assert "GriptapeNodes.ContextManager().node(node0_name)" in source
        assert "SetLockNodeStateRequest(node_name=None, lock=True)" in source

    def test_lock_command_is_emitted_after_parameter_values_in_the_same_block(self, engine: Engine) -> None:
        indirect_command = SerializedNodeCommands.IndirectSetParameterValueCommand(
            set_parameter_value_command=SetParameterValueRequest(parameter_name="text", value=None),
            unique_value_uuid=SerializedNodeCommands.UniqueParameterValueUUID(str(uuid4())),
        )
        lock_command = SetLockNodeStateRequest(node_name=None, lock=False)
        statements = engine.workflow_manager._generate_set_parameter_value_for_node(
            "node0_name",
            [indirect_command],
            "top_level_unique_values_dict",
            ImportRecorder(),
            lock_node_command=lock_command,
        )
        with_stmt = statements[0]
        assert isinstance(with_stmt, ast.With)
        expected_body_length = 2
        assert len(with_stmt.body) == expected_body_length
        assert _calls_named(with_stmt.body[0], "SetParameterValueRequest")
        assert _calls_named(with_stmt.body[1], "SetLockNodeStateRequest")


class TestGenerateSetParameterValueCode:
    """The dict-wide dispatcher that fans set-parameter-value work out per node."""

    def test_missing_node_is_skipped_and_logged_not_raised(
        self, engine: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        missing_uuid = SerializedNodeCommands.NodeUUID("missing")
        indirect_command = SerializedNodeCommands.IndirectSetParameterValueCommand(
            set_parameter_value_command=SetParameterValueRequest(parameter_name="text", value=None),
            unique_value_uuid=SerializedNodeCommands.UniqueParameterValueUUID(str(uuid4())),
        )
        with caplog.at_level(logging.ERROR, logger="griptape_nodes"):
            statements = engine.workflow_manager._generate_set_parameter_value_code(
                set_parameter_value_commands={missing_uuid: [indirect_command]},
                lock_commands={},
                node_uuid_to_node_variable_name={},
                unique_values_dict_name="top_level_unique_values_dict",
                import_recorder=ImportRecorder(),
            )
        assert statements == []
        assert any("was never written to the file" in record.getMessage() for record in caplog.records)


class TestPatchAndPickleObject:
    """Pickling must reference a stable importable module name, never the throwaway dynamic one."""

    def test_dynamic_module_is_patched_to_stable_namespace_in_the_pickle_bytes(self, engine: Engine) -> None:
        original_module = _DynamicLibraryClass.__module__
        fake_dynamic_name = "gtn_dynamic_module_fake_for_test"
        sys.modules[fake_dynamic_name] = types.ModuleType(fake_dynamic_name)
        _DynamicLibraryClass.__module__ = fake_dynamic_name
        instance = _DynamicLibraryClass()
        try:
            with (
                patch.object(
                    engine.library_manager,
                    "is_dynamic_module",
                    side_effect=lambda name: name == fake_dynamic_name,
                ),
                patch.object(
                    engine.library_manager,
                    "get_stable_namespace_for_dynamic_module",
                    side_effect=lambda name: original_module if name == fake_dynamic_name else None,
                ),
            ):
                pickled_bytes = engine.workflow_manager._patch_and_pickle_object(instance)
        finally:
            del sys.modules[fake_dynamic_name]
            _DynamicLibraryClass.__module__ = original_module

        assert original_module.encode() in pickled_bytes
        assert fake_dynamic_name.encode() not in pickled_bytes

    def test_original_module_is_restored_after_pickling(self, engine: Engine) -> None:
        original_module = _DynamicLibraryClass.__module__
        fake_dynamic_name = "gtn_dynamic_module_fake_for_restore_test"
        sys.modules[fake_dynamic_name] = types.ModuleType(fake_dynamic_name)
        _DynamicLibraryClass.__module__ = fake_dynamic_name
        instance = _DynamicLibraryClass()
        try:
            with (
                patch.object(
                    engine.library_manager,
                    "is_dynamic_module",
                    side_effect=lambda name: name == fake_dynamic_name,
                ),
                patch.object(
                    engine.library_manager,
                    "get_stable_namespace_for_dynamic_module",
                    side_effect=lambda name: original_module if name == fake_dynamic_name else None,
                ),
            ):
                engine.workflow_manager._patch_and_pickle_object(instance)
            assert _DynamicLibraryClass.__module__ == fake_dynamic_name
        finally:
            del sys.modules[fake_dynamic_name]
            _DynamicLibraryClass.__module__ = original_module


class TestGenerateWorkflowFileContentIntegration:
    """End-to-end codegen: a hand-built serialized flow tree must produce one coherent, valid file."""

    @staticmethod
    def _composite_flow_commands() -> SerializedFlowCommands:
        shared_uuid = SerializedNodeCommands.UniqueParameterValueUUID(str(uuid4()))
        node_a = _node_command("node_a", node_uuid="uuid-a")
        node_b = _node_command("node_b", node_uuid="uuid-b")
        child_node = _node_command("child_node", node_uuid="uuid-c")

        connection = SerializedFlowCommands.IndirectConnectionSerialization(
            source_node_uuid=SerializedNodeCommands.NodeUUID("uuid-a"),
            source_parameter_name="out",
            target_node_uuid=SerializedNodeCommands.NodeUUID("uuid-b"),
            target_parameter_name="in",
        )
        indirect_value_a = SerializedNodeCommands.IndirectSetParameterValueCommand(
            set_parameter_value_command=SetParameterValueRequest(parameter_name="text", value=None),
            unique_value_uuid=shared_uuid,
        )
        indirect_value_b = SerializedNodeCommands.IndirectSetParameterValueCommand(
            set_parameter_value_command=SetParameterValueRequest(parameter_name="text", value=None),
            unique_value_uuid=shared_uuid,
        )

        child_flow = _empty_flow_commands(
            flow_initialization_command=CreateFlowRequest(parent_flow_name="parent_flow", flow_name="child_flow"),
            serialized_node_commands=[child_node],
        )

        return _empty_flow_commands(
            flow_initialization_command=CreateFlowRequest(parent_flow_name=None, flow_name="parent_flow"),
            serialized_node_commands=[node_a, node_b],
            serialized_connections=[connection],
            unique_parameter_uuid_to_values={shared_uuid: "shared-value"},
            set_parameter_value_commands={
                SerializedNodeCommands.NodeUUID("uuid-a"): [indirect_value_a],
                SerializedNodeCommands.NodeUUID("uuid-b"): [indirect_value_b],
            },
            sub_flows_commands=[child_flow],
        )

    def test_composite_shape_generates_valid_python(self, engine: Engine) -> None:
        content = engine.workflow_manager._generate_workflow_file_content(
            serialized_flow_commands=self._composite_flow_commands(),
            workflow_metadata=_minimal_metadata(),
        )
        ast.parse(content)  # raises SyntaxError if codegen left invalid source behind

    def test_parent_flow_is_created_before_its_child_flow(self, engine: Engine) -> None:
        content = engine.workflow_manager._generate_workflow_file_content(
            serialized_flow_commands=self._composite_flow_commands(),
            workflow_metadata=_minimal_metadata(),
        )
        assert content.index("flow0_name = ") < content.index("flow1_name = ")
        assert "parent_flow_name=flow0_name" in content

    def test_shared_value_is_pickled_once_and_referenced_by_both_nodes(self, engine: Engine) -> None:
        content = engine.workflow_manager._generate_workflow_file_content(
            serialized_flow_commands=self._composite_flow_commands(),
            workflow_metadata=_minimal_metadata(),
        )
        assert content.count("pickle.loads(") == 1
        module = ast.parse(content)
        subscripts = [
            node
            for node in ast.walk(module)
            if isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "top_level_unique_values_dict"
        ]
        expected_subscript_count = 2
        assert len(subscripts) == expected_subscript_count

    def test_generating_twice_from_the_same_commands_is_byte_identical(self, engine: Engine) -> None:
        """Re-saving an unchanged graph must not churn the diff."""
        flow_commands = self._composite_flow_commands()
        metadata = _minimal_metadata()
        first = engine.workflow_manager._generate_workflow_file_content(
            serialized_flow_commands=flow_commands, workflow_metadata=metadata
        )
        second = engine.workflow_manager._generate_workflow_file_content(
            serialized_flow_commands=flow_commands, workflow_metadata=metadata
        )
        assert first == second


class TestImportRecorder:
    """Tracks module-level imports for the generated file, deduped and deterministically ordered."""

    def test_generate_imports_sorts_modules_and_names(self) -> None:
        import_recorder = ImportRecorder()
        import_recorder.add_import("zeta")
        import_recorder.add_import("alpha")
        import_recorder.add_from_import("some.module", "Zeta")
        import_recorder.add_from_import("some.module", "Alpha")
        output = import_recorder.generate_imports()
        assert output == "import alpha\nimport zeta\nfrom some.module import Alpha, Zeta"

    def test_duplicate_imports_are_deduped(self) -> None:
        import_recorder = ImportRecorder()
        import_recorder.add_import("alpha")
        import_recorder.add_import("alpha")
        import_recorder.add_from_import("some.module", "Alpha")
        import_recorder.add_from_import("some.module", "Alpha")
        output = import_recorder.generate_imports()
        assert output == "import alpha\nfrom some.module import Alpha"


class TestBuildDeferredImportStatements:
    """Deferred library imports must land inside build_workflow(), sorted for stable output."""

    def test_emits_one_import_from_per_module_sorted_by_module_and_class(self, engine: Engine) -> None:
        deferred_imports = {
            "zeta.module": {"ZetaClass"},
            "alpha.module": {"BClass", "AClass"},
        }
        statements = engine.workflow_manager._build_deferred_import_statements(deferred_imports)
        expected_statement_count = 2
        assert len(statements) == expected_statement_count
        assert all(isinstance(stmt, ast.ImportFrom) for stmt in statements)
        first_statement, second_statement = statements
        assert isinstance(first_statement, ast.ImportFrom)
        assert isinstance(second_statement, ast.ImportFrom)
        assert first_statement.module == "alpha.module"
        assert [alias.name for alias in first_statement.names] == ["AClass", "BClass"]
        assert second_statement.module == "zeta.module"

    def test_empty_deferred_imports_yields_no_statements(self, engine: Engine) -> None:
        assert engine.workflow_manager._build_deferred_import_statements({}) == []


class TestRewriteStringComments:
    """ast.unparse cannot emit `#` comments, so codegen smuggles them as bare strings and unwraps them."""

    def test_single_quoted_comment_line_becomes_a_real_comment(self) -> None:
        source = "x = 1\n'# a comment'\ny = 2"
        rewritten = rewrite_string_comments(source)
        assert "# a comment" in rewritten.splitlines()
        assert "'# a comment'" not in rewritten

    def test_double_quoted_comment_line_becomes_a_real_comment(self) -> None:
        source = 'x = 1\n"# a comment"\ny = 2'
        rewritten = rewrite_string_comments(source)
        assert "# a comment" in rewritten.splitlines()

    def test_indentation_is_preserved(self) -> None:
        source = "if True:\n    '# indented comment'\n    pass"
        rewritten = rewrite_string_comments(source)
        assert "    # indented comment" in rewritten.splitlines()

    def test_non_comment_string_statement_is_left_alone(self) -> None:
        source = "'just a string, not a comment'"
        rewritten = rewrite_string_comments(source)
        assert rewritten == source

    def test_ordinary_code_lines_are_left_alone(self) -> None:
        source = "x = 1\ny = 2"
        assert rewrite_string_comments(source) == source
