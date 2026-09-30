"""Generates the Python source of a saved workflow file."""

from __future__ import annotations

import ast
import logging
import pickle
import re
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from inspect import getmodule, isclass
from typing import TYPE_CHECKING, Any, NamedTuple, cast

import tomlkit

from griptape_nodes.node_library.workflow_registry import (
    WorkflowMetadata,
    WorkflowShape,
)
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.app_events import (
    GetEngineVersionRequest,
    GetEngineVersionResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    SerializedConnectionKey,
    SerializedFlowCommands,
)
from griptape_nodes.retained_mode.events.workflow_events import ImportWorkflowAsReferencedSubFlowRequest
from griptape_nodes.retained_mode.managers.workflow.shape import WorkflowShapeType
from griptape_nodes.utils.ast_utils import rewrite_string_comments

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.node_library.library_registry import LibraryNameAndVersion
    from griptape_nodes.retained_mode.events.node_events import SerializedNodeCommands, SetLockNodeStateRequest


logger = logging.getLogger("griptape_nodes")

WORKFLOW_METADATA_HEADER = "script"


@dataclass
class WorkflowCodegenState:
    """Variable names and counters shared by every Flow written into one generated workflow file.

    A generated file is a single flat script, so node and flow variable names have to be unique
    across the whole file rather than per Flow. Nested node groups also break any per-Flow view of
    the graph: a group's ``node_names_to_add`` can name a child that was created inside a deeper
    subflow, and a connection can join nodes that live in different subflows. Threading one of these
    through the whole recursion keeps every level looking at the same names.
    """

    node_uuid_to_node_variable_name: dict[SerializedNodeCommands.NodeUUID, str] = field(default_factory=dict)
    subflow_name_to_variable_name: dict[str, str] = field(default_factory=dict)
    next_node_index: int = 0
    next_flow_index: int = 0
    emitted_connection_keys: set[SerializedConnectionKey] = field(default_factory=set)

    def reserve_node_index(self) -> int:
        """Claim the next unused node variable index."""
        node_index = self.next_node_index
        self.next_node_index += 1
        return node_index

    def reserve_flow_index(self) -> int:
        """Claim the next unused flow variable index."""
        flow_index = self.next_flow_index
        self.next_flow_index += 1
        return flow_index

    def take_unemitted_connections(
        self, connections: list[SerializedFlowCommands.IndirectConnectionSerialization]
    ) -> list[SerializedFlowCommands.IndirectConnectionSerialization]:
        """Filter out connections already written elsewhere in the file, claiming the rest.

        Each Flow's serialized connections include those of its subflows, so the same edge is
        offered once per level of nesting. Emitting it every time would re-create the connection
        repeatedly, which for a node group means tearing down and rebuilding the proxy parameter
        the edge was routed through.

        Args:
            connections: The connections the current Flow would like to emit

        Returns:
            Only the connections no other Flow has emitted yet
        """
        unemitted_connections = []
        for connection in connections:
            connection_key = connection.key()
            if connection_key in self.emitted_connection_keys:
                continue
            self.emitted_connection_keys.add(connection_key)
            unemitted_connections.append(connection)
        return unemitted_connections


class ASTContainer:
    """ASTContainer is a helper class to keep track of AST nodes and generate final code from them."""

    def __init__(self) -> None:
        """Initialize an empty list to store AST nodes."""
        self.nodes = []

    def add_node(self, node: ast.AST) -> None:
        self.nodes.append(node)

    def get_ast(self) -> list[ast.AST]:
        return self.nodes


@dataclass
class ImportRecorder:
    """Recorder to keep track of imports and generate code for them."""

    imports: set[str]
    from_imports: dict[str, set[str]]

    def __init__(self) -> None:
        """Initialize the recorder."""
        self.imports = set()
        self.from_imports = {}

    def add_import(self, module_name: str) -> None:
        """Add an import to the recorder.

        Args:
            module_name (str): The module name to import.
        """
        self.imports.add(module_name)

    def add_from_import(self, module_name: str, class_name: str) -> None:
        """Add a from-import to the recorder.

        Args:
            module_name (str): The module name to import from.
            class_name (str): The class name to import.
        """
        if module_name not in self.from_imports:
            self.from_imports[module_name] = set()
        self.from_imports[module_name].add(class_name)

    def generate_imports(self) -> str:
        """Generate the import code from the recorded imports.

        Returns:
            str: The generated code.
        """
        import_lines = []
        for module_name in sorted(self.imports):
            import_lines.append(f"import {module_name}")  # noqa: PERF401

        for module_name, class_names in sorted(self.from_imports.items()):
            sorted_class_names = sorted(class_names)
            import_lines.append(f"from {module_name} import {', '.join(sorted_class_names)}")

        return "\n".join(import_lines)


class _ScrubResult(NamedTuple):
    """Result of scrubbing a value for inline AST emission."""

    value: Any
    dropped: bool


class WorkflowCodeGenerator(EngineScoped):
    """Turns serialized flow commands into a runnable workflow script."""

    def generate_workflow_metadata_from_commands(  # noqa: PLR0913
        self,
        serialized_flow_commands: SerializedFlowCommands,
        file_name: str,
        creation_date: datetime,
        *,
        display_name: str | None = None,
        image_path: str | None = None,
        description: str | None = None,
        is_template: bool | None = None,
        branched_from: str | None = None,
        workflow_shape: WorkflowShape | None = None,
    ) -> WorkflowMetadata:
        """Generate workflow metadata from serialized commands."""
        # Get the engine version
        engine_version_request = GetEngineVersionRequest()
        engine_version_result = self.engine.handle_request(request=engine_version_request)
        if not isinstance(engine_version_result, GetEngineVersionResultSuccess):
            details = f"Failed getting the engine version for workflow '{file_name}'."
            raise TypeError(details)

        engine_version_success = cast("GetEngineVersionResultSuccess", engine_version_result)
        engine_version = f"{engine_version_success.major}.{engine_version_success.minor}.{engine_version_success.patch}"

        # Create the Workflow Metadata header
        workflows_referenced = None
        if serialized_flow_commands.node_dependencies.referenced_workflows:
            workflows_referenced = list(serialized_flow_commands.node_dependencies.referenced_workflows)

        # display_name is the human-readable label (metadata.name); falls back to file_name if not provided.
        metadata_name = display_name if display_name is not None else str(file_name)

        direct_libs: list[LibraryNameAndVersion] = list(serialized_flow_commands.node_dependencies.libraries)
        all_libs = self.engine.library_manager.resolve_transitive_library_deps(direct_libs)

        return WorkflowMetadata(
            name=metadata_name,
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with=engine_version,
            node_libraries_referenced=all_libs,
            node_types_used=serialized_flow_commands.node_types_used,
            workflows_referenced=workflows_referenced,
            creation_date=creation_date,
            last_modified_date=datetime.now(tz=UTC),
            branched_from=branched_from,
            workflow_shape=workflow_shape,
            image=image_path,
            description=description,
            is_template=is_template,
        )

    def generate_workflow_file_content(
        self,
        serialized_flow_commands: SerializedFlowCommands,
        workflow_metadata: WorkflowMetadata,
        *,
        pickle_control_flow_result: bool = False,
    ) -> str:
        """Generate workflow file content from serialized commands and metadata."""
        metadata_block = self._generate_workflow_metadata_header(workflow_metadata=workflow_metadata)
        if metadata_block is None:
            details = f"Failed to generate metadata block for workflow '{workflow_metadata.name}'."
            raise ValueError(details)

        import_recorder = ImportRecorder()
        import_recorder.add_from_import("griptape_nodes.retained_mode.griptape_nodes", "GriptapeNodes")

        # Add imports from node dependencies
        for import_dep in serialized_flow_commands.node_dependencies.imports:
            if import_dep.class_name:
                import_recorder.add_from_import(import_dep.module, import_dep.class_name)
            else:
                import_recorder.add_import(import_dep.module)

        ast_container = ASTContainer()

        # Graph-building statements accumulate into the body of `async def build_workflow()`,
        # so the emitted workflow file only mutates engine state when build_workflow() is awaited.
        # build_workflow() also registers every library named in the workflow metadata header so
        # the file is self-sufficient: it works whether it is loaded by WorkflowManager.run_workflow
        # (which also calls _ensure_libraries_for_workflow before exec) or executed directly as a
        # standalone script via LocalWorkflowExecutor (which has no equivalent pre-exec hook).
        # RegisterLibraryFromFileRequest is idempotent, so the redundant engine-side call is safe.
        library_names = [lib.library_name for lib in workflow_metadata.node_libraries_referenced]

        main_body: list[ast.stmt] = []

        # Snapshot the flag now (at save time) so it's baked into build_workflow().
        variable_substitution_enabled = self.engine.workflow_manager.variable_substitution.is_enabled()

        prereq_code = self._generate_workflow_run_prerequisite_code(
            import_recorder=import_recorder,
            library_names=library_names,
            variable_substitution_enabled=variable_substitution_enabled,
        )
        main_body.extend(cast("ast.stmt", node) for node in prereq_code)

        # Collect library-derived imports separately so they can be emitted inside
        # build_workflow() after the RegisterLibraryFromFileRequest calls — those calls
        # are what add the library directory and venv site-packages to sys.path, so the
        # imports must come after them, not at module top level.
        deferred_imports: dict[str, set[str]] = {}

        # Generate unique values code AST node
        unique_values_node = self._generate_unique_values_code(
            unique_parameter_uuid_to_values=serialized_flow_commands.unique_parameter_uuid_to_values,
            prefix="top_level",
            import_recorder=import_recorder,
            deferred_imports=deferred_imports,
        )
        # Emit deferred library imports inside build_workflow(), after sys.path is set up.
        main_body.extend(self._build_deferred_import_statements(deferred_imports))
        # Helper returns an ast.Module; unpack its body into statements.
        main_body.extend(cast("ast.stmt", stmt) for stmt in unique_values_node.body)

        # Names are shared by every Flow in the file, at any nesting depth.
        codegen_state = WorkflowCodegenState()

        main_body.extend(
            self._generate_flow_code(
                serialized_flow_commands=serialized_flow_commands,
                import_recorder=import_recorder,
                codegen_state=codegen_state,
                parent_flow_creation_index=None,
            )
        )

        # Wrap all graph-building statements in `async def build_workflow()` so the file is
        # inert until build_workflow() is awaited (by the engine loader or the CLI entrypoint).
        # The name pairs with the executor entrypoints (execute_workflow / aexecute_workflow)
        # to make the two phases — graph construction vs execution — visually distinct.
        main_func_def = ast.AsyncFunctionDef(
            name="build_workflow",
            args=ast.arguments(
                posonlyargs=[],
                args=[],
                vararg=None,
                kwonlyargs=[],
                kw_defaults=[],
                kwarg=None,
                defaults=[],
            ),
            body=main_body or [ast.Pass()],
            decorator_list=[],
            returns=ast.Constant(value=None),
            type_params=[],
        )
        ast.fix_missing_locations(main_func_def)
        ast_container.add_node(main_func_def)

        # Generate workflow execution code. Only emitted when the workflow has a Start/End
        # shape — that's what makes the file runnable as a CLI program. Shapeless workflows
        # have no input/output surface and are loaded by the engine via `await build_workflow()`
        # (see WorkflowManager.run_workflow), so they don't need a `__main__` guard.
        # TODO: https://github.com/griptape-ai/griptape-nodes/issues/4205 — decide how shapeless
        # workflows should behave when invoked directly (build-only vs build + StartFlowRequest)
        # and emit an appropriate `__main__` guard.
        workflow_execution_code = self._generate_workflow_execution(
            import_recorder=import_recorder,
            workflow_metadata=workflow_metadata,
            pickle_control_flow_result=pickle_control_flow_result,
        )
        if workflow_execution_code is not None:
            for node in workflow_execution_code:
                ast_container.add_node(node)

        # Generate final code from ASTContainer.
        ast_output = "\n\n".join([ast.unparse(node) for node in ast_container.get_ast()])
        # Rewrite string-literal lines of the form `'# foo'` or `"# foo"` into real `# foo`
        # comments. `ast.unparse` cannot emit comments directly, so the generators above
        # smuggle them in as bare-string statements and we unwrap them here.
        ast_output = rewrite_string_comments(ast_output)
        import_output = import_recorder.generate_imports()
        return f"{metadata_block}\n\n{import_output}\n\n{ast_output}\n"

    def replace_workflow_metadata_header(self, workflow_content: str, new_metadata: WorkflowMetadata) -> str | None:
        """Replace the metadata header in a workflow file with new metadata.

        Args:
            workflow_content: The full content of the workflow file
            new_metadata: The new metadata to replace the existing header with

        Returns:
            The workflow content with updated metadata header, or None if replacement failed
        """
        import re

        # Generate the new metadata header
        new_metadata_header = self._generate_workflow_metadata_header(new_metadata)
        if new_metadata_header is None:
            return None

        # Replace the metadata block using regex
        metadata_pattern = r"(# /// script\n)(.*?)(# ///)"
        updated_content = re.sub(metadata_pattern, new_metadata_header, workflow_content, flags=re.DOTALL)

        return updated_content

    def patch_and_pickle_object(self, obj: Any) -> bytes:
        """Patch dynamic module references to stable namespaces, pickle object, then restore.

        This solves the "pickle data was truncated" error that occurs when workflows containing
        objects from dynamically loaded modules (like VideoUrlArtifact, ReferenceImageArtifact)
        are serialized and later reloaded in a fresh Python process.

        The Problem:
            Dynamic modules get names like "gtn_dynamic_module_image_to_video_py_123456789"
            When pickle serializes objects, it embeds these module names in the binary data
            When workflows run later, Python can't import these non-existent module names

        The Solution:
            1. Recursively find all objects from dynamic modules (even nested in containers)
            2. Temporarily patch their __module__ and module_name to stable namespaces
            3. Pickle with stable references like "griptape_nodes.node_libraries.runwayml_library.image_to_video"
            4. Restore original names to avoid side effects

        Args:
            obj: Object to patch and pickle (may contain nested structures)

        Returns:
            Pickled bytes with stable module references

        Example:
            Before: pickle contains "gtn_dynamic_module_image_to_video_py_123456789.VideoUrlArtifact"
            After:  pickle contains "griptape_nodes.node_libraries.runwayml_library.image_to_video.VideoUrlArtifact"
        """
        patched_classes: list[tuple[type, str]] = []
        patched_instances: list[tuple[Any, str]] = []

        def patch_class(class_type: type, instance: Any) -> None:
            """Patch a single class instance to use stable namespace."""
            module = getmodule(class_type)
            if module and self.engine.library_manager.is_dynamic_module(module.__name__):
                stable_namespace = self.engine.library_manager.get_stable_namespace_for_dynamic_module(module.__name__)
                if stable_namespace:
                    # Patch class __module__ (affects pickle class reference)
                    if class_type.__module__ != stable_namespace:
                        patched_classes.append((class_type, class_type.__module__))
                        class_type.__module__ = stable_namespace

                    # Patch instance module_name field (affects SerializableMixin serialization)
                    if hasattr(instance, "module_name") and instance.module_name != stable_namespace:
                        patched_instances.append((instance, instance.module_name))
                        instance.module_name = stable_namespace

        try:
            # Apply patches to entire object tree
            self.walk_object_tree(obj, patch_class)
            return pickle.dumps(obj)
        finally:
            # Always restore original names to avoid affecting other code
            for class_obj, original_name in patched_classes:
                class_obj.__module__ = original_name
            for instance_obj, original_name in patched_instances:
                instance_obj.module_name = original_name

    def walk_object_tree(
        self, obj: Any, process_class_fn: Callable[[type, Any], None], visited: set[int] | None = None
    ) -> None:
        """Recursively walk through object tree, calling process_class_fn for each class found.

        This unified helper handles the common pattern of recursively traversing nested objects
        to find all class instances. Used by both patching and import collection.

        Args:
            obj: Object to traverse (can contain nested lists, dicts, class instances)
            process_class_fn: Function to call for each class found, signature: (class_type, instance)
            visited: Set of object IDs already visited (for circular reference protection)

        Example:
            # Collect all class types in a nested structure
            def collect_type(cls, instance):
                print(f"Found {cls.__name__} instance")

            data = [SomeClass(), {"key": AnotherClass()}]
            self.walk_object_tree(data, collect_type)
        """
        if visited is None:
            visited = set()

        obj_id = id(obj)
        if obj_id in visited:
            return
        visited.add(obj_id)

        # Process the object if it's a class instance
        obj_type = type(obj)
        if isclass(obj_type):
            process_class_fn(obj_type, obj)

        # Recursively traverse containers
        if isinstance(obj, (list, tuple)):
            for item in obj:
                self.walk_object_tree(item, process_class_fn, visited)
        elif isinstance(obj, dict):
            for key, value in obj.items():
                self.walk_object_tree(key, process_class_fn, visited)
                self.walk_object_tree(value, process_class_fn, visited)
        elif hasattr(obj, "__dict__"):
            for attr_value in obj.__dict__.values():
                self.walk_object_tree(attr_value, process_class_fn, visited)

    def _generate_workflow_metadata_header(self, workflow_metadata: WorkflowMetadata) -> str | None:
        try:
            toml_doc = tomlkit.document()
            toml_doc.add("dependencies", tomlkit.item([]))
            griptape_tool_table = tomlkit.table()
            # Strip out the Nones since TOML doesn't like those
            # WorkflowShape is now serialized as JSON string by Pydantic field_serializer;
            # this preserves the nil/null/None values that we WANT, but for all of the
            # Python-related Nones, TOML will flip out if they are not stripped.
            metadata_dict = workflow_metadata.model_dump(exclude_none=True)
            for key, value in metadata_dict.items():
                griptape_tool_table.add(key=key, value=value)
            toml_doc["tool"] = tomlkit.table()
            toml_doc["tool"]["griptape-nodes"] = griptape_tool_table  # type: ignore (this is the only way I could find to get tomlkit to do the dotted notation correctly)
        except Exception as err:
            details = f"Failed to get metadata into TOML format: {err}."
            logger.error(details)
            return None

        # Format the metadata block with comment markers for each line
        toml_lines = tomlkit.dumps(toml_doc).split("\n")
        commented_toml_lines = ["# " + line for line in toml_lines]

        # Create the complete metadata block
        header = f"# /// {WORKFLOW_METADATA_HEADER}"
        metadata_lines = [header]
        metadata_lines.extend(commented_toml_lines)
        metadata_lines.append("# ///")
        metadata_block = "\n".join(metadata_lines)

        return metadata_block

    def _generate_workflow_execution(
        self,
        import_recorder: ImportRecorder,
        workflow_metadata: WorkflowMetadata,
        *,
        pickle_control_flow_result: bool = False,
    ) -> list[ast.AST] | None:
        """Generates execute_workflow(...) and the __main__ guard."""
        # Use workflow shape from metadata if available, otherwise skip execution block
        if workflow_metadata.workflow_shape is None:
            logger.debug("Workflow shape does not have required Start or End Nodes. Skipping local execution block.")
            return None

        # Convert WorkflowShape to dict format expected by the rest of the method
        workflow_shape = {
            WorkflowShapeType.INPUT: workflow_metadata.workflow_shape.inputs,
            WorkflowShapeType.OUTPUT: workflow_metadata.workflow_shape.outputs,
        }

        # === imports ===
        import_recorder.add_import("argparse")
        import_recorder.add_import("asyncio")
        import_recorder.add_import("json")
        import_recorder.add_import("logging")
        import_recorder.add_from_import("typing", "Any")
        import_recorder.add_from_import(
            "griptape_nodes.bootstrap.workflow_executors.local_workflow_executor", "LocalWorkflowExecutor"
        )
        import_recorder.add_from_import(
            "griptape_nodes.bootstrap.workflow_executors.workflow_executor", "WorkflowExecutor"
        )

        # === 1) build the `def execute_workflow(input: dict, *, workflow_executor: WorkflowExecutor | None = None, **kwargs: Any) -> dict | None:` ===
        # `**kwargs` carries through to `LocalWorkflowExecutor(**kwargs)` on the fallback
        # construction path (when no `workflow_executor` is supplied) and to `executor.arun(**kwargs)`
        # in all paths. This keeps the helper signature stable as new executor-level options
        # are added without requiring a workflow file schema bump (issue #4599).
        arg_input = ast.arg(arg="input", annotation=ast.Name(id="dict", ctx=ast.Load()))
        arg_workflow_executor = ast.arg(
            arg="workflow_executor",
            annotation=ast.BinOp(
                left=ast.Name(id="WorkflowExecutor", ctx=ast.Load()),
                op=ast.BitOr(),
                right=ast.Constant(value=None),
            ),
        )
        kwargs_arg = ast.arg(arg="kwargs", annotation=ast.Name(id="Any", ctx=ast.Load()))
        args = ast.arguments(
            posonlyargs=[],
            args=[arg_input],
            vararg=None,
            kwonlyargs=[arg_workflow_executor],
            kw_defaults=[ast.Constant(value=None)],
            kwarg=kwargs_arg,
            defaults=[],
        )
        #   return annotation: dict | None
        return_annotation = ast.BinOp(
            left=ast.Name(id="dict", ctx=ast.Load()),
            op=ast.BitOr(),
            right=ast.Constant(value=None),
        )

        # Generate the ensure flow context function call
        ensure_context_call = self._generate_ensure_flow_context_call()

        # Construct a default LocalWorkflowExecutor only when the caller did not supply one.
        # Inside the `if`, seed `kwargs["pickle_control_flow_result"]` with the save-time
        # default via `setdefault` so direct importers who don't pass it explicitly inherit
        # the publisher's choice. `**kwargs` is then splatted into the constructor; a typo'd
        # kwarg surfaces as a TypeError from LocalWorkflowExecutor.__init__.
        # TODO: https://github.com/griptape-ai/griptape-nodes/issues/3771 Update for workflows that call other workflows - need to include referenced workflows in the list
        pickle_setdefault_stmt = ast.Expr(
            value=ast.Call(
                func=ast.Attribute(
                    value=ast.Name(id="kwargs", ctx=ast.Load()),
                    attr="setdefault",
                    ctx=ast.Load(),
                ),
                args=[
                    ast.Constant(value="pickle_control_flow_result"),
                    ast.Constant(value=pickle_control_flow_result),
                ],
                keywords=[],
            )
        )
        executor_assign = ast.If(
            test=ast.Compare(
                left=ast.Name(id="workflow_executor", ctx=ast.Load()),
                ops=[ast.Is()],
                comparators=[ast.Constant(value=None)],
            ),
            body=[
                pickle_setdefault_stmt,
                ast.Assign(
                    targets=[ast.Name(id="workflow_executor", ctx=ast.Store())],
                    value=ast.Call(
                        func=ast.Name(id="LocalWorkflowExecutor", ctx=ast.Load()),
                        args=[],
                        keywords=[
                            ast.keyword(arg="skip_library_loading", value=ast.Constant(value=True)),
                            ast.keyword(
                                arg="workflows_to_register",
                                value=ast.List(elts=[ast.Name(id="__file__", ctx=ast.Load())], ctx=ast.Load()),
                            ),
                            ast.keyword(arg=None, value=ast.Name(id="kwargs", ctx=ast.Load())),
                        ],
                    ),
                ),
            ],
            orelse=[],
        )
        # Use async context manager for workflow execution. Any leftover `**kwargs` flow
        # through to `arun` (e.g. `pickle_control_flow_result` if the caller wants to
        # override the executor's instance default for this run only).
        with_stmt = ast.AsyncWith(
            items=[
                ast.withitem(
                    context_expr=ast.Name(id="workflow_executor", ctx=ast.Load()),
                    optional_vars=ast.Name(id="executor", ctx=ast.Store()),
                )
            ],
            body=[
                ast.Expr(
                    value=ast.Await(
                        value=ast.Call(
                            func=ast.Attribute(
                                value=ast.Name(id="executor", ctx=ast.Load()),
                                attr="arun",
                                ctx=ast.Load(),
                            ),
                            args=[],
                            keywords=[
                                ast.keyword(arg="flow_input", value=ast.Name(id="input", ctx=ast.Load())),
                                ast.keyword(arg=None, value=ast.Name(id="kwargs", ctx=ast.Load())),
                            ],
                        )
                    )
                )
            ],
        )
        return_stmt = ast.Return(
            value=ast.Attribute(
                value=ast.Name(id="executor", ctx=ast.Load()),
                attr="output",
                ctx=ast.Load(),
            )
        )

        # build_workflow() is the async function emitted by generate_workflow_file_content
        # that contains all graph-building requests.
        await_main_call = ast.Expr(
            value=ast.Await(
                value=ast.Call(
                    func=ast.Name(id="build_workflow", ctx=ast.Load()),
                    args=[],
                    keywords=[],
                )
            )
        )

        # Build the graph inside the executor's context manager. Entering it activates
        # the project passed via --project-file-path, and only then is a bundle's own
        # griptape_nodes_config.json read, which is what registers the bundle's required
        # node libraries.
        with_stmt.body = [await_main_call, ensure_context_call, *with_stmt.body]

        # === Generate async aexecute_workflow function ===
        async_func_def = ast.AsyncFunctionDef(
            name="aexecute_workflow",
            args=args,
            body=[
                executor_assign,
                with_stmt,
                return_stmt,
            ],
            decorator_list=[],
            returns=return_annotation,
            type_params=[],
        )
        ast.fix_missing_locations(async_func_def)

        # === Generate sync execute_workflow function (backward compatibility wrapper) ===
        sync_func_def = ast.FunctionDef(
            name="execute_workflow",
            args=args,
            body=[
                ast.Return(
                    value=ast.Call(
                        func=ast.Attribute(
                            value=ast.Name(id="asyncio", ctx=ast.Load()),
                            attr="run",
                            ctx=ast.Load(),
                        ),
                        args=[
                            ast.Call(
                                func=ast.Name(id="aexecute_workflow", ctx=ast.Load()),
                                args=[],
                                keywords=[
                                    ast.keyword(arg="input", value=ast.Name(id="input", ctx=ast.Load())),
                                    ast.keyword(
                                        arg="workflow_executor", value=ast.Name(id="workflow_executor", ctx=ast.Load())
                                    ),
                                    ast.keyword(arg=None, value=ast.Name(id="kwargs", ctx=ast.Load())),
                                ],
                            )
                        ],
                        keywords=[],
                    )
                )
            ],
            decorator_list=[],
            returns=return_annotation,
            type_params=[],
        )
        ast.fix_missing_locations(sync_func_def)

        # === 2) build the `if __name__ == "__main__":` block ===
        if_node = self._generate_main_block(workflow_shape, pickle_control_flow_result=pickle_control_flow_result)

        # Generate the ensure flow context function
        ensure_context_func = self._generate_ensure_flow_context_function(import_recorder)

        return [ensure_context_func, sync_func_def, async_func_def, if_node]

    def _generate_main_block(self, workflow_shape: dict, *, pickle_control_flow_result: bool = False) -> ast.If:
        """Generates the `if __name__ == '__main__':` block for the serialized workflow file."""
        main_test = ast.Compare(
            left=ast.Name(id="__name__", ctx=ast.Load()),
            ops=[ast.Eq()],
            comparators=[ast.Constant(value="__main__")],
        )

        parser_assign = ast.Assign(
            targets=[ast.Name(id="parser", ctx=ast.Store())],
            value=ast.Call(
                func=ast.Attribute(
                    value=ast.Name(id="argparse", ctx=ast.Load()),
                    attr="ArgumentParser",
                    ctx=ast.Load(),
                ),
                args=[],
                keywords=[],
            ),
        )

        # Generate parser.add_argument(...) calls for each parameter in workflow_shape
        add_arg_calls = []

        # Delegate executor-level CLI flags to LocalWorkflowExecutor.add_cli_arguments(parser).
        # This replaces hand-rolled --storage-backend / --project-file-path / --save-on-failure
        # add_argument calls so that future executor-level flags can be added without bumping
        # the workflow file schema. See https://github.com/griptape-ai/griptape-nodes/issues/4599.
        add_arg_calls.append(
            ast.Expr(
                value=ast.Call(
                    func=ast.Attribute(
                        value=ast.Name(id="LocalWorkflowExecutor", ctx=ast.Load()),
                        attr="add_cli_arguments",
                        ctx=ast.Load(),
                    ),
                    args=[ast.Name(id="parser", ctx=ast.Load())],
                    keywords=[
                        ast.keyword(
                            arg="pickle_control_flow_result_default",
                            value=ast.Constant(value=pickle_control_flow_result),
                        ),
                    ],
                )
            )
        )

        # Add json input argument (workflow-file concern, not executor concern)
        add_arg_calls.append(
            ast.Expr(
                value=ast.Call(
                    func=ast.Attribute(
                        value=ast.Name(id="parser", ctx=ast.Load()),
                        attr="add_argument",
                        ctx=ast.Load(),
                    ),
                    args=[ast.Constant("--json-input")],
                    keywords=[
                        ast.keyword(arg="default", value=ast.Constant(None)),
                        ast.keyword(
                            arg="help",
                            value=ast.Constant(
                                "JSON string containing parameter values. Takes precedence over individual parameter arguments if provided."
                            ),
                        ),
                    ],
                )
            )
        )

        # Generate individual arguments for each parameter in workflow_shape[WorkflowShapeType.INPUT]
        if WorkflowShapeType.INPUT in workflow_shape:
            for node_name, node_params in workflow_shape[WorkflowShapeType.INPUT].items():
                if isinstance(node_params, dict):
                    for param_name, param_info in node_params.items():
                        # Create CLI argument name: --{param_name}
                        arg_name = f"--{param_name}".lower()

                        # Derive an explicit, identifier-safe dest. Without this, argparse
                        # derives the dest from the flag name, which can contain characters
                        # (e.g. parentheses from a node name) that are not valid in a Python
                        # identifier, making the emitted `args.<dest>` access invalid Python.
                        arg_dest = self._safe_arg_dest(param_name)

                        # Get help text from parameter info
                        help_text = param_info.get("tooltip", f"Parameter {param_name} for node {node_name}")

                        add_arg_calls.append(
                            ast.Expr(
                                value=ast.Call(
                                    func=ast.Attribute(
                                        value=ast.Name(id="parser", ctx=ast.Load()),
                                        attr="add_argument",
                                        ctx=ast.Load(),
                                    ),
                                    args=[ast.Constant(arg_name)],
                                    keywords=[
                                        ast.keyword(arg="dest", value=ast.Constant(arg_dest)),
                                        ast.keyword(arg="default", value=ast.Constant(None)),
                                        ast.keyword(arg="help", value=ast.Constant(help_text)),
                                    ],
                                )
                            )
                        )

        parse_args = ast.Assign(
            targets=[ast.Name(id="args", ctx=ast.Store())],
            value=ast.Call(
                func=ast.Attribute(
                    value=ast.Name(id="parser", ctx=ast.Load()),
                    attr="parse_args",
                    ctx=ast.Load(),
                ),
                args=[],
                keywords=[],
            ),
        )

        # Build flow_input dictionary from JSON input or individual CLI arguments
        flow_input_init = ast.Assign(
            targets=[ast.Name(id="flow_input", ctx=ast.Store())],
            value=ast.Dict(keys=[], values=[]),
        )

        # Check if json_input is provided and parse it
        json_input_if = ast.If(
            test=ast.Compare(
                left=ast.Attribute(
                    value=ast.Name(id="args", ctx=ast.Load()),
                    attr="json_input",
                    ctx=ast.Load(),
                ),
                ops=[ast.IsNot()],
                comparators=[ast.Constant(value=None)],
            ),
            body=[
                ast.Assign(
                    targets=[ast.Name(id="flow_input", ctx=ast.Store())],
                    value=ast.Call(
                        func=ast.Attribute(
                            value=ast.Name(id="json", ctx=ast.Load()),
                            attr="loads",
                            ctx=ast.Load(),
                        ),
                        args=[
                            ast.Attribute(
                                value=ast.Name(id="args", ctx=ast.Load()),
                                attr="json_input",
                                ctx=ast.Load(),
                            )
                        ],
                        keywords=[],
                    ),
                )
            ],
            orelse=[],
        )

        # Build the flow_input dict structure from individual arguments (fallback when no JSON input)
        build_flow_input_stmts = []

        # For each node, ensure it exists in flow_input
        build_flow_input_stmts.extend(
            [
                ast.If(
                    test=ast.Compare(
                        left=ast.Constant(value=node_name),
                        ops=[ast.NotIn()],
                        comparators=[ast.Name(id="flow_input", ctx=ast.Load())],
                    ),
                    body=[
                        ast.Assign(
                            targets=[
                                ast.Subscript(
                                    value=ast.Name(id="flow_input", ctx=ast.Load()),
                                    slice=ast.Constant(value=node_name),
                                    ctx=ast.Store(),
                                )
                            ],
                            value=ast.Dict(keys=[], values=[]),
                        )
                    ],
                    orelse=[],
                )
                for node_name in workflow_shape.get(WorkflowShapeType.INPUT, {})
            ]
        )

        # For each parameter, get its value from args and add to flow_input
        build_flow_input_stmts.extend(
            [
                ast.If(
                    test=ast.Compare(
                        left=ast.Attribute(
                            value=ast.Name(id="args", ctx=ast.Load()),
                            attr=self._safe_arg_dest(param_name),
                            ctx=ast.Load(),
                        ),
                        ops=[ast.IsNot()],
                        comparators=[ast.Constant(value=None)],
                    ),
                    body=[
                        ast.Assign(
                            targets=[
                                ast.Subscript(
                                    value=ast.Subscript(
                                        value=ast.Name(id="flow_input", ctx=ast.Load()),
                                        slice=ast.Constant(value=node_name),
                                        ctx=ast.Load(),
                                    ),
                                    slice=ast.Constant(value=param_name),
                                    ctx=ast.Store(),
                                )
                            ],
                            value=ast.Attribute(
                                value=ast.Name(id="args", ctx=ast.Load()),
                                attr=self._safe_arg_dest(param_name),
                                ctx=ast.Load(),
                            ),
                        )
                    ],
                    orelse=[],
                )
                for node_name, node_params in workflow_shape.get(WorkflowShapeType.INPUT, {}).items()
                if isinstance(node_params, dict)
                for param_name in node_params
            ]
        )

        # Ensure body is not empty - add pass statement if no input parameters
        if not build_flow_input_stmts:
            build_flow_input_stmts = [
                ast.Expr(
                    value=ast.Constant(
                        value="This workflow has no input parameters defined, so there's nothing necessary to supply"
                    )
                ),
                ast.Pass(),
            ]

        # Wrap the individual argument processing in an else clause
        individual_args_else = ast.If(
            test=ast.Compare(
                left=ast.Attribute(
                    value=ast.Name(id="args", ctx=ast.Load()),
                    attr="json_input",
                    ctx=ast.Load(),
                ),
                ops=[ast.Is()],
                comparators=[ast.Constant(value=None)],
            ),
            body=build_flow_input_stmts,
            orelse=[],
        )

        # Construct the default executor in __main__ via LocalWorkflowExecutor.from_cli_args,
        # passing skip_library_loading and workflows_to_register as constructor overrides
        # since they are not exposed on the CLI surface.
        executor_assign_main = ast.Assign(
            targets=[ast.Name(id="executor", ctx=ast.Store())],
            value=ast.Call(
                func=ast.Attribute(
                    value=ast.Name(id="LocalWorkflowExecutor", ctx=ast.Load()),
                    attr="from_cli_args",
                    ctx=ast.Load(),
                ),
                args=[ast.Name(id="args", ctx=ast.Load())],
                keywords=[
                    ast.keyword(arg="skip_library_loading", value=ast.Constant(value=True)),
                    ast.keyword(
                        arg="workflows_to_register",
                        value=ast.List(elts=[ast.Name(id="__file__", ctx=ast.Load())], ctx=ast.Load()),
                    ),
                ],
            ),
        )
        workflow_output = ast.Assign(
            targets=[ast.Name(id="workflow_output", ctx=ast.Store())],
            value=ast.Call(
                func=ast.Name(id="execute_workflow", ctx=ast.Load()),
                args=[],
                keywords=[
                    ast.keyword(arg="input", value=ast.Name(id="flow_input", ctx=ast.Load())),
                    ast.keyword(arg="workflow_executor", value=ast.Name(id="executor", ctx=ast.Load())),
                ],
            ),
        )
        print_output = ast.Expr(
            value=ast.Call(
                func=ast.Name(id="print", ctx=ast.Load()),
                args=[ast.Name(id="workflow_output", ctx=ast.Load())],
                keywords=[],
            )
        )

        # logging.basicConfig(level=logging.INFO) — ensures a handler exists on the root logger
        # so that log records from the "griptape_nodes" logger (whose level is set by ConfigManager)
        # actually have somewhere to go when running workflows from the CLI.
        logging_basic_config = ast.Expr(
            value=ast.Call(
                func=ast.Attribute(
                    value=ast.Name(id="logging", ctx=ast.Load()),
                    attr="basicConfig",
                    ctx=ast.Load(),
                ),
                args=[],
                keywords=[
                    ast.keyword(
                        arg="level",
                        value=ast.Attribute(
                            value=ast.Name(id="logging", ctx=ast.Load()),
                            attr="INFO",
                            ctx=ast.Load(),
                        ),
                    ),
                ],
            )
        )

        if_node = ast.If(
            test=main_test,
            body=[
                logging_basic_config,
                parser_assign,
                *add_arg_calls,
                parse_args,
                flow_input_init,
                json_input_if,
                individual_args_else,
                executor_assign_main,
                workflow_output,
                print_output,
            ],
            orelse=[],
        )
        ast.fix_missing_locations(if_node)

        return if_node

    def _generate_ensure_flow_context_function(
        self,
        import_recorder: ImportRecorder,
    ) -> ast.AsyncFunctionDef:
        """Generates the async _ensure_workflow_context function for the serialized workflow file."""
        import_recorder.add_from_import("griptape_nodes.retained_mode.events.flow_events", "GetTopLevelFlowRequest")
        import_recorder.add_from_import(
            "griptape_nodes.retained_mode.events.flow_events", "GetTopLevelFlowResultSuccess"
        )

        # Function signature: async def _ensure_workflow_context():
        func_def = ast.AsyncFunctionDef(
            name="_ensure_workflow_context",
            args=ast.arguments(
                posonlyargs=[],
                args=[],
                vararg=None,
                kwonlyargs=[],
                kw_defaults=[],
                kwarg=None,
                defaults=[],
            ),
            body=[],
            decorator_list=[],
            returns=None,
            type_params=[],
        )

        context_manager_assign = ast.Assign(
            targets=[ast.Name(id="context_manager", ctx=ast.Store())],
            value=ast.Call(
                func=ast.Attribute(
                    value=ast.Name(id="GriptapeNodes", ctx=ast.Load()),
                    attr="ContextManager",
                    ctx=ast.Load(),
                ),
                args=[],
                keywords=[],
            ),
        )

        # if not context_manager.has_current_flow():
        has_flow_check = ast.UnaryOp(
            op=ast.Not(),
            operand=ast.Call(
                func=ast.Attribute(
                    value=ast.Name(id="context_manager", ctx=ast.Load()),
                    attr="has_current_flow",
                    ctx=ast.Load(),
                ),
                args=[],
                keywords=[],
            ),
        )

        # top_level_flow_request = GetTopLevelFlowRequest()  # noqa: ERA001
        flow_request_assign = ast.Assign(
            targets=[ast.Name(id="top_level_flow_request", ctx=ast.Store())],
            value=ast.Call(
                func=ast.Name(id="GetTopLevelFlowRequest", ctx=ast.Load()),
                args=[],
                keywords=[],
            ),
        )

        # top_level_flow_result = await GriptapeNodes.ahandle_request(top_level_flow_request)  # noqa: ERA001
        flow_result_assign = ast.Assign(
            targets=[ast.Name(id="top_level_flow_result", ctx=ast.Store())],
            value=ast.Await(
                value=ast.Call(
                    func=ast.Attribute(
                        value=ast.Name(id="GriptapeNodes", ctx=ast.Load()),
                        attr="ahandle_request",
                        ctx=ast.Load(),
                    ),
                    args=[ast.Name(id="top_level_flow_request", ctx=ast.Load())],
                    keywords=[],
                ),
            ),
        )

        # isinstance check and flow_name is not None
        isinstance_check = ast.Call(
            func=ast.Name(id="isinstance", ctx=ast.Load()),
            args=[
                ast.Name(id="top_level_flow_result", ctx=ast.Load()),
                ast.Name(id="GetTopLevelFlowResultSuccess", ctx=ast.Load()),
            ],
            keywords=[],
        )

        flow_name_check = ast.Compare(
            left=ast.Attribute(
                value=ast.Name(id="top_level_flow_result", ctx=ast.Load()),
                attr="flow_name",
                ctx=ast.Load(),
            ),
            ops=[ast.IsNot()],
            comparators=[ast.Constant(value=None)],
        )

        success_condition = ast.BoolOp(
            op=ast.And(),
            values=[isinstance_check, flow_name_check],
        )

        # flow_manager = GriptapeNodes.FlowManager()  # noqa: ERA001
        flow_manager_assign = ast.Assign(
            targets=[ast.Name(id="flow_manager", ctx=ast.Store())],
            value=ast.Call(
                func=ast.Attribute(
                    value=ast.Name(id="GriptapeNodes", ctx=ast.Load()),
                    attr="FlowManager",
                    ctx=ast.Load(),
                ),
                args=[],
                keywords=[],
            ),
        )

        # flow_obj = flow_manager.get_flow_by_name(top_level_flow_result.flow_name)  # noqa: ERA001
        flow_obj_assign = ast.Assign(
            targets=[ast.Name(id="flow_obj", ctx=ast.Store())],
            value=ast.Call(
                func=ast.Attribute(
                    value=ast.Name(id="flow_manager", ctx=ast.Load()),
                    attr="get_flow_by_name",
                    ctx=ast.Load(),
                ),
                args=[
                    ast.Attribute(
                        value=ast.Name(id="top_level_flow_result", ctx=ast.Load()),
                        attr="flow_name",
                        ctx=ast.Load(),
                    )
                ],
                keywords=[],
            ),
        )

        # context_manager.push_flow(flow_obj)  # noqa: ERA001
        push_flow_call = ast.Expr(
            value=ast.Call(
                func=ast.Attribute(
                    value=ast.Name(id="context_manager", ctx=ast.Load()),
                    attr="push_flow",
                    ctx=ast.Load(),
                ),
                args=[ast.Name(id="flow_obj", ctx=ast.Load())],
                keywords=[],
            ),
        )

        # Build the inner if statement for success condition
        success_if = ast.If(
            test=success_condition,
            body=[
                flow_manager_assign,
                flow_obj_assign,
                push_flow_call,
            ],
            orelse=[],
        )

        # Build the main if statement
        main_if = ast.If(
            test=has_flow_check,
            body=[
                flow_request_assign,
                flow_result_assign,
                success_if,
            ],
            orelse=[],
        )

        # Set the function body
        func_def.body = [context_manager_assign, main_if]
        ast.fix_missing_locations(func_def)

        return func_def

    def _generate_ensure_flow_context_call(
        self,
    ) -> ast.Expr:
        """Generates the call to await _ensure_workflow_context() function."""
        return ast.Expr(
            value=ast.Await(
                value=ast.Call(
                    func=ast.Name(id="_ensure_workflow_context", ctx=ast.Load()),
                    args=[],
                    keywords=[],
                )
            )
        )

    def _generate_workflow_run_prerequisite_code(
        self,
        import_recorder: ImportRecorder,
        library_names: list[str],
        *,
        variable_substitution_enabled: bool = True,
    ) -> list[ast.AST]:
        code_blocks: list[ast.AST] = []

        # Emit `await GriptapeNodes.ahandle_request(RegisterLibraryFromFileRequest(...))` once
        # per declared library so build_workflow() registers its own dependencies before any
        # CreateNodeRequest runs. Without this, running the workflow file as a standalone script
        # (uv run workflow.py) would have no libraries registered when nodes are created:
        # LocalWorkflowExecutor is constructed with skip_library_loading=True, so app
        # initialization deliberately loads none. perform_discovery_if_not_found=True lets the
        # registration find the library JSON via the engine's normal config-driven discovery
        # path, which resolves against the bundle's own config layer -- activated by entering
        # the executor's context manager, which build_workflow() now runs inside.
        if library_names:
            import_recorder.add_from_import(
                "griptape_nodes.retained_mode.events.library_events", "RegisterLibraryFromFileRequest"
            )
        for library_name in library_names:
            register_call = ast.Expr(
                value=ast.Await(
                    value=ast.Call(
                        func=ast.Attribute(
                            value=ast.Name(id="GriptapeNodes", ctx=ast.Load()),
                            attr="ahandle_request",
                            ctx=ast.Load(),
                        ),
                        args=[
                            ast.Call(
                                func=ast.Name(id="RegisterLibraryFromFileRequest", ctx=ast.Load()),
                                args=[],
                                keywords=[
                                    ast.keyword(arg="library_name", value=ast.Constant(value=library_name)),
                                    ast.keyword(arg="perform_discovery_if_not_found", value=ast.Constant(value=True)),
                                ],
                            )
                        ],
                        keywords=[],
                    )
                )
            )
            ast.fix_missing_locations(register_call)
            code_blocks.append(register_call)

        # Generate context manager assignment
        assign_context_manager = ast.Assign(
            targets=[ast.Name(id="context_manager", ctx=ast.Store())],
            value=ast.Call(
                func=ast.Attribute(
                    value=ast.Name(id="GriptapeNodes", ctx=ast.Load()),
                    attr="ContextManager",
                    ctx=ast.Load(),
                ),
                args=[],
                keywords=[],
            ),
        )
        ast.fix_missing_locations(assign_context_manager)
        code_blocks.append(assign_context_manager)

        has_check = ast.Call(
            func=ast.Attribute(
                value=ast.Name(id="context_manager", ctx=ast.Load()),
                attr="has_current_workflow",
                ctx=ast.Load(),
            ),
            args=[],
            keywords=[],
        )
        test = ast.UnaryOp(op=ast.Not(), operand=has_check)

        push_call = ast.Expr(
            value=ast.Call(
                func=ast.Attribute(
                    value=ast.Name(id="context_manager", ctx=ast.Load()),
                    attr="push_workflow",
                    ctx=ast.Load(),
                ),
                args=[],
                keywords=[ast.keyword(arg="file_path", value=ast.Name(id="__file__", ctx=ast.Load()))],
            )
        )
        ast.fix_missing_locations(push_call)

        if_stmt = ast.If(
            test=test,
            body=[push_call],
            orelse=[],
        )
        ast.fix_missing_locations(if_stmt)
        code_blocks.append(if_stmt)

        # When variable substitution is disabled, bake a request call into build_workflow()
        # so the setting is restored on every load — including running the file as a script.
        # We only emit the call when disabled (False) because True is the default; omitting
        # the call for enabled workflows keeps the generated code clean.
        if not variable_substitution_enabled:
            import_recorder.add_from_import(
                "griptape_nodes.retained_mode.events.workflow_events",
                "SetVariableSubstitutionEnabledRequest",
            )
            disable_substitution_call = ast.Expr(
                value=ast.Await(
                    value=ast.Call(
                        func=ast.Attribute(
                            value=ast.Name(id="GriptapeNodes", ctx=ast.Load()),
                            attr="ahandle_request",
                            ctx=ast.Load(),
                        ),
                        args=[
                            ast.Call(
                                func=ast.Name(id="SetVariableSubstitutionEnabledRequest", ctx=ast.Load()),
                                args=[],
                                keywords=[
                                    ast.keyword(arg="enabled", value=ast.Constant(value=False)),
                                    ast.keyword(arg="initial_setup", value=ast.Constant(value=True)),
                                ],
                            )
                        ],
                        keywords=[],
                    )
                )
            )
            ast.fix_missing_locations(disable_substitution_call)
            code_blocks.append(disable_substitution_call)

        return code_blocks

    def _generate_unique_values_code(
        self,
        unique_parameter_uuid_to_values: dict[SerializedNodeCommands.UniqueParameterValueUUID, Any],
        prefix: str,
        import_recorder: ImportRecorder,
        deferred_imports: dict[str, set[str]] | None = None,
    ) -> ast.Module:
        if len(unique_parameter_uuid_to_values) == 0:
            return ast.Module(body=[], type_ignores=[])

        import_recorder.add_import("pickle")

        # Get the list of manually-curated, globally available modules
        global_modules_set = {"builtins", "__main__"}

        # Serialize the unique values as pickled strings.
        # IMPORTANT: We patch dynamic module names to stable namespaces before pickling
        # to ensure generated workflows can reliably import the required classes.
        unique_parameter_dict = {}

        for uuid, unique_parameter_value in unique_parameter_uuid_to_values.items():
            # Dynamic Module Patching Strategy:
            # When we pickle objects from dynamically loaded modules (like VideoUrlArtifact),
            # pickle stores the class's __module__ attribute in the binary data. If we don't
            # patch this, the pickle data would contain something like:
            #   "gtn_dynamic_module_image_to_video_py_123456789.VideoUrlArtifact"
            #
            # When the workflow runs later, Python tries to import this module name, which
            # fails because dynamic modules don't exist in fresh Python processes.
            #
            # Our solution: Temporarily patch the class's __module__ to use the stable namespace
            # before pickling, so the pickle data contains:
            #   "griptape_nodes.node_libraries.runwayml_library.image_to_video.VideoUrlArtifact"
            #
            # This includes recursive patching for nested objects in containers (lists, tuples, dicts)

            # Apply recursive dynamic module patching, pickle, then restore
            unique_parameter_bytes = self.patch_and_pickle_object(unique_parameter_value)

            # Encode the bytes as a string using latin1
            unique_parameter_byte_str = unique_parameter_bytes.decode("latin1")
            unique_parameter_dict[uuid] = unique_parameter_byte_str

            # Collect import statements for all classes in the object tree
            self._collect_object_imports(unique_parameter_value, import_recorder, global_modules_set, deferred_imports)

        # Comment lines explaining what we're doing. Each line is emitted as its own bare-string
        # statement so that it unparses onto a single source line. A post-process pass in
        # generate_workflow_file_content (via rewrite_string_comments) then strips the surrounding
        # quotes to turn each line into a real Python `#` comment.
        comment_lines = [
            "# 1. We've collated all of the unique parameter values into a dictionary so that we do not have to duplicate them.",
            "#    This minimizes the size of the code, especially for large objects like serialized image files.",
            "# 2. We're using a prefix so that it's clear which Flow these values are associated with.",
            "# 3. The values are serialized using pickle, which is a binary format. This makes them harder to read, but makes",
            "#    them consistently save and load. It allows us to serialize complex objects like custom classes, which otherwise",
            "#    would be difficult to serialize.",
        ]

        # Generate the dictionary of unique values
        unique_values_dict_name = f"{prefix}_unique_values_dict"
        unique_values_ast = ast.Assign(
            targets=[ast.Name(id=unique_values_dict_name, ctx=ast.Store(), lineno=1, col_offset=0)],
            value=ast.Dict(
                keys=[ast.Constant(value=str(uuid), lineno=1, col_offset=0) for uuid in unique_parameter_dict],
                values=[
                    ast.Call(
                        func=ast.Attribute(
                            value=ast.Name(id="pickle", ctx=ast.Load(), lineno=1, col_offset=0),
                            attr="loads",
                            ctx=ast.Load(),
                            lineno=1,
                            col_offset=0,
                        ),
                        args=[ast.Constant(value=byte_str.encode("latin1"), lineno=1, col_offset=0)],
                        keywords=[],
                        lineno=1,
                        col_offset=0,
                    )
                    for byte_str in unique_parameter_dict.values()
                ],
                lineno=1,
                col_offset=0,
            ),
            lineno=1,
            col_offset=0,
        )

        # Create the final AST with comment lines followed by the dict assignment.
        comment_exprs = [
            ast.Expr(value=ast.Constant(value=line, lineno=1, col_offset=0), lineno=1, col_offset=0)
            for line in comment_lines
        ]
        module_body: list[ast.stmt] = [*comment_exprs, unique_values_ast]
        full_ast = ast.Module(body=module_body, type_ignores=[])
        return full_ast

    def _build_deferred_import_statements(self, deferred_imports: dict[str, set[str]]) -> list[ast.stmt]:
        """Convert deferred library imports into ast.ImportFrom statements for insertion into build_workflow().

        Sorted by module name (and class names within each module) for deterministic output.
        """
        stmts: list[ast.stmt] = []
        for module, classes in sorted(deferred_imports.items()):
            node = ast.ImportFrom(
                module=module,
                names=[ast.alias(name=cls) for cls in sorted(classes)],
                level=0,
            )
            ast.fix_missing_locations(node)
            stmts.append(node)
        return stmts

    def _generate_create_flow(
        self,
        create_flow_command: CreateFlowRequest,
        import_recorder: ImportRecorder,
        flow_creation_index: int,
        parent_flow_creation_index: int | None = None,
    ) -> ast.Module:
        import_recorder.add_from_import("griptape_nodes.retained_mode.events.flow_events", "CreateFlowRequest")

        # Prepare arguments for CreateFlowRequest
        create_flow_request_args = []

        # Omit values that match default values.
        if is_dataclass(create_flow_command):
            for field in fields(create_flow_command):
                field_value = getattr(create_flow_command, field.name)
                if field_value != field.default:
                    # Special handling for parent_flow_name - use variable reference if parent index provided
                    if field.name == "parent_flow_name" and parent_flow_creation_index is not None:
                        parent_flow_variable = f"flow{parent_flow_creation_index}_name"
                        create_flow_request_args.append(
                            ast.keyword(
                                arg=field.name,
                                value=ast.Name(id=parent_flow_variable, ctx=ast.Load(), lineno=1, col_offset=0),
                            )
                        )
                    else:
                        create_flow_request_args.append(
                            self._keyword_from_field_value(field.name, field_value, create_flow_command)
                        )

        # Create a comment explaining the behavior
        comment_ast = ast.Expr(
            value=ast.Constant(
                value="# Create the Flow, then do work within it as context.",
                lineno=1,
                col_offset=0,
            ),
            lineno=1,
            col_offset=0,
        )

        # Construct the AST for creating the flow
        flow_variable_name = f"flow{flow_creation_index}_name"
        create_flow_result = ast.Assign(
            targets=[ast.Name(id=flow_variable_name, ctx=ast.Store(), lineno=1, col_offset=0)],
            value=ast.Attribute(
                value=ast.Await(
                    value=ast.Call(
                        func=ast.Attribute(
                            value=ast.Name(id="GriptapeNodes", ctx=ast.Load(), lineno=1, col_offset=0),
                            attr="ahandle_request",
                            ctx=ast.Load(),
                            lineno=1,
                            col_offset=0,
                        ),
                        args=[
                            ast.Call(
                                func=ast.Name(id="CreateFlowRequest", ctx=ast.Load(), lineno=1, col_offset=0),
                                args=[],
                                keywords=create_flow_request_args,
                                lineno=1,
                                col_offset=0,
                            )
                        ],
                        keywords=[],
                        lineno=1,
                        col_offset=0,
                    ),
                    lineno=1,
                    col_offset=0,
                ),
                attr="flow_name",
                ctx=ast.Load(),
                lineno=1,
                col_offset=0,
            ),
            lineno=1,
            col_offset=0,
        )

        # Return both the comment and the assignment as a module
        return ast.Module(body=[comment_ast, create_flow_result], type_ignores=[])

    def _generate_import_workflow(
        self,
        import_workflow_command: ImportWorkflowAsReferencedSubFlowRequest,
        import_recorder: ImportRecorder,
        flow_creation_index: int,
    ) -> ast.Module:
        """Generate AST code for importing a referenced workflow.

        Creates an assignment statement that executes an ImportWorkflowAsReferencedSubFlowRequest
        and stores the resulting flow name in a variable.

        Args:
            import_workflow_command: The import request containing the workflow file path
            import_recorder: Tracks imports needed for the generated code
            flow_creation_index: Index used to generate unique variable names

        Returns:
            AST assignment node representing the import workflow command

        Example output:
            flow1_name = (await GriptapeNodes.ahandle_request(ImportWorkflowAsReferencedSubFlowRequest(
                file_path='/path/to/workflow.py'
            ))).created_flow_name
        """
        import_recorder.add_from_import(
            "griptape_nodes.retained_mode.events.workflow_events", "ImportWorkflowAsReferencedSubFlowRequest"
        )

        # Prepare arguments for ImportWorkflowAsReferencedSubFlowRequest
        import_workflow_request_args = []

        # Omit values that match default values.
        if is_dataclass(import_workflow_command):
            for field in fields(import_workflow_command):
                field_value = getattr(import_workflow_command, field.name)
                if field_value != field.default:
                    import_workflow_request_args.append(
                        self._keyword_from_field_value(field.name, field_value, import_workflow_command)
                    )

        # Construct the AST for importing the workflow
        flow_variable_name = f"flow{flow_creation_index}_name"
        import_workflow_result = ast.Assign(
            targets=[ast.Name(id=flow_variable_name, ctx=ast.Store(), lineno=1, col_offset=0)],
            value=ast.Attribute(
                value=ast.Await(
                    value=ast.Call(
                        func=ast.Attribute(
                            value=ast.Name(id="GriptapeNodes", ctx=ast.Load(), lineno=1, col_offset=0),
                            attr="ahandle_request",
                            ctx=ast.Load(),
                            lineno=1,
                            col_offset=0,
                        ),
                        args=[
                            ast.Call(
                                func=ast.Name(
                                    id="ImportWorkflowAsReferencedSubFlowRequest",
                                    ctx=ast.Load(),
                                    lineno=1,
                                    col_offset=0,
                                ),
                                args=[],
                                keywords=import_workflow_request_args,
                                lineno=1,
                                col_offset=0,
                            )
                        ],
                        keywords=[],
                        lineno=1,
                        col_offset=0,
                    ),
                    lineno=1,
                    col_offset=0,
                ),
                attr="created_flow_name",
                ctx=ast.Load(),
                lineno=1,
                col_offset=0,
            ),
            lineno=1,
            col_offset=0,
        )

        return ast.Module(body=[import_workflow_result], type_ignores=[])

    def _generate_assign_flow_context(
        self,
        flow_initialization_command: CreateFlowRequest | ImportWorkflowAsReferencedSubFlowRequest | None,
        flow_creation_index: int,
    ) -> ast.With:
        context_manager = ast.Attribute(
            value=ast.Name(id="GriptapeNodes", ctx=ast.Load(), lineno=1, col_offset=0),
            attr="ContextManager",
            ctx=ast.Load(),
            lineno=1,
            col_offset=0,
        )

        if flow_initialization_command is None:
            # Construct AST for "GriptapeNodes.ContextManager().flow(GriptapeNodes.ContextManager().get_current_flow().flow_name)"
            flow_call = ast.Call(
                func=ast.Attribute(
                    value=ast.Call(func=context_manager, args=[], keywords=[], lineno=1, col_offset=0),
                    attr="flow",
                    ctx=ast.Load(),
                    lineno=1,
                    col_offset=0,
                ),
                args=[
                    ast.Attribute(
                        value=ast.Call(
                            func=ast.Attribute(
                                value=ast.Call(func=context_manager, args=[], keywords=[], lineno=1, col_offset=0),
                                attr="get_current_flow",
                                ctx=ast.Load(),
                                lineno=1,
                                col_offset=0,
                            ),
                            args=[],
                            keywords=[],
                            lineno=1,
                            col_offset=0,
                        ),
                        attr="flow_name",
                        ctx=ast.Load(),
                        lineno=1,
                        col_offset=0,
                    )
                ],
                keywords=[],
                lineno=1,
                col_offset=0,
            )
        else:
            # Construct AST for "GriptapeNodes.ContextManager().flow(flow{flow_creation_index}_name)"
            flow_variable_name = f"flow{flow_creation_index}_name"
            flow_call = ast.Call(
                func=ast.Attribute(
                    value=ast.Call(func=context_manager, args=[], keywords=[], lineno=1, col_offset=0),
                    attr="flow",
                    ctx=ast.Load(),
                    lineno=1,
                    col_offset=0,
                ),
                args=[ast.Name(id=flow_variable_name, ctx=ast.Load(), lineno=1, col_offset=0)],
                keywords=[],
                lineno=1,
                col_offset=0,
            )

        # Construct the "with" statement with an empty body
        with_stmt = ast.With(
            items=[ast.withitem(context_expr=flow_call, optional_vars=None)],
            body=[],  # Initialize the body as an empty list
            type_comment=None,
            lineno=1,
            col_offset=0,
        )

        return with_stmt

    def _generate_flow_code(
        self,
        serialized_flow_commands: SerializedFlowCommands,
        import_recorder: ImportRecorder,
        codegen_state: WorkflowCodegenState,
        parent_flow_creation_index: int | None,
    ) -> list[ast.stmt]:
        """Generate the code that rebuilds one Flow, then recurse into its subflows.

        Recursion is what makes nested node groups work: a group's subflow can itself hold another
        group with its own subflow, to any depth. Handling only the first level of subflows left the
        deeper nodes out of the file entirely, so the groups that owned them were rebuilt empty and
        any connection reaching one of them could not be written at all.

        Args:
            serialized_flow_commands: Commands for the Flow being generated
            import_recorder: Import recorder for tracking imports
            codegen_state: Variable names and counters shared across the whole file
            parent_flow_creation_index: Index of the enclosing Flow's variable, or None at the top

        Returns:
            The statements that recreate this Flow and everything inside it
        """
        flow_initialization_command = serialized_flow_commands.flow_initialization_command
        flow_creation_index = codegen_state.reserve_flow_index()

        flow_statements = self._generate_flow_initialization_code(
            flow_initialization_command=flow_initialization_command,
            import_recorder=import_recorder,
            codegen_state=codegen_state,
            flow_creation_index=flow_creation_index,
            parent_flow_creation_index=parent_flow_creation_index,
        )

        # A referenced workflow carries its own file, so only the import belongs here.
        if isinstance(flow_initialization_command, ImportWorkflowAsReferencedSubFlowRequest):
            return flow_statements

        if not self._flow_has_content_to_generate(serialized_flow_commands):
            return flow_statements

        flow_context_node = self._generate_assign_flow_context(
            flow_initialization_command=flow_initialization_command, flow_creation_index=flow_creation_index
        )

        # Emit flow-scoped variable creation INSIDE the flow "with" block, BEFORE any
        # node creation. Ordering matters: SetVariable nodes' before_value_set hook fires
        # during initial_setup and calls has_variable(); having the variable already
        # present ensures that hook is a no-op adopt rather than a duplicate create.
        flow_context_node.body.extend(
            self._generate_create_variable_code(
                serialized_variable_commands=serialized_flow_commands.serialized_variable_commands,
                unique_values_dict_name="top_level_unique_values_dict",
                import_recorder=import_recorder,
            )
        )

        # A node group has to be created after the nodes it claims as members, and its members can
        # live in this Flow's subflows, so groups are held back until the subflows are written.
        regular_node_commands = []
        node_group_commands = []
        for serialized_node_command in serialized_flow_commands.serialized_node_commands:
            if serialized_node_command.is_node_group:
                node_group_commands.append(serialized_node_command)
            else:
                regular_node_commands.append(serialized_node_command)

        for serialized_node_command in regular_node_commands:
            flow_context_node.body.extend(
                self._generate_node_creation_code(
                    serialized_node_command,
                    codegen_state.reserve_node_index(),
                    import_recorder,
                    node_uuid_to_node_variable_name=codegen_state.node_uuid_to_node_variable_name,
                    subflow_name_to_variable_name=codegen_state.subflow_name_to_variable_name,
                )
            )

        for sub_flow_commands in serialized_flow_commands.sub_flows_commands:
            flow_context_node.body.extend(
                self._generate_flow_code(
                    serialized_flow_commands=sub_flow_commands,
                    import_recorder=import_recorder,
                    codegen_state=codegen_state,
                    parent_flow_creation_index=flow_creation_index,
                )
            )

        for serialized_node_command in node_group_commands:
            flow_context_node.body.extend(
                self._generate_node_creation_code(
                    serialized_node_command,
                    codegen_state.reserve_node_index(),
                    import_recorder,
                    node_uuid_to_node_variable_name=codegen_state.node_uuid_to_node_variable_name,
                    subflow_name_to_variable_name=codegen_state.subflow_name_to_variable_name,
                )
            )

        # Connections come last, once every node in this Flow's whole subtree exists — including the
        # groups written just above, whose proxy parameters are what boundary-crossing edges attach to.
        # Claiming edges here, after the subflows were already written, is safe: an edge is only ever
        # collected by the Flow holding both endpoints or one level above them (_get_connections_for_flow),
        # so a subflow can never claim an edge that reaches a group node declared out here.
        flow_context_node.body.extend(
            self._generate_connections_code(
                serialized_connections=codegen_state.take_unemitted_connections(
                    serialized_flow_commands.serialized_connections
                ),
                node_uuid_to_node_variable_name=codegen_state.node_uuid_to_node_variable_name,
                import_recorder=import_recorder,
            )
        )

        flow_context_node.body.extend(
            self._generate_set_parameter_value_code(
                set_parameter_value_commands=serialized_flow_commands.set_parameter_value_commands,
                lock_commands=serialized_flow_commands.set_lock_commands_per_node,
                node_uuid_to_node_variable_name=codegen_state.node_uuid_to_node_variable_name,
                unique_values_dict_name="top_level_unique_values_dict",
                import_recorder=import_recorder,
            )
        )

        flow_statements.append(cast("ast.stmt", flow_context_node))
        return flow_statements

    def _generate_flow_initialization_code(
        self,
        flow_initialization_command: CreateFlowRequest | ImportWorkflowAsReferencedSubFlowRequest | None,
        import_recorder: ImportRecorder,
        codegen_state: WorkflowCodegenState,
        flow_creation_index: int,
        parent_flow_creation_index: int | None,
    ) -> list[ast.stmt]:
        """Generate the statements that bring one Flow into existence.

        Args:
            flow_initialization_command: How the Flow is created, or None to reuse the current one
            import_recorder: Import recorder for tracking imports
            codegen_state: Variable names and counters shared across the whole file
            flow_creation_index: Index of this Flow's own variable
            parent_flow_creation_index: Index of the enclosing Flow's variable, or None at the top

        Returns:
            The statements that create the Flow (empty when it already exists)

        Raises:
            TypeError: If the Flow is created by a command this generator does not know how to write
        """
        match flow_initialization_command:
            case CreateFlowRequest():
                # A subflow names its parent by variable so it lands in the right spot in the tree.
                create_flow_module = self._generate_create_flow(
                    flow_initialization_command,
                    import_recorder,
                    flow_creation_index,
                    parent_flow_creation_index=parent_flow_creation_index,
                )
                if flow_initialization_command.flow_name:
                    codegen_state.subflow_name_to_variable_name[flow_initialization_command.flow_name] = (
                        f"flow{flow_creation_index}_name"
                    )
                return [cast("ast.stmt", node) for node in create_flow_module.body]
            case ImportWorkflowAsReferencedSubFlowRequest():
                import_workflow_module = self._generate_import_workflow(
                    flow_initialization_command, import_recorder, flow_creation_index
                )
                return [cast("ast.stmt", node) for node in import_workflow_module.body]
            case None:
                # No initialization command; the contents are rebuilt into the current context.
                return []
            case _:
                # A new way of creating a Flow was added without teaching this generator to write it.
                # This one does fail the save, unlike the skip-and-log guards elsewhere in codegen:
                # emitting nothing writes a file whose Flow is never created, so every node inside it
                # lands wherever the script happened to be pointing. That is a wrong graph that loads
                # without complaint, which is worse than a save the artist knows did not happen.
                msg = f"Attempted to save a workflow. Failed because a flow is created in a way this version cannot write out: {type(flow_initialization_command).__name__}."
                raise TypeError(msg)

    def _generate_node_creation_code(  # noqa: C901, PLR0912
        self,
        serialized_node_command: SerializedNodeCommands,
        node_index: int,
        import_recorder: ImportRecorder,
        node_uuid_to_node_variable_name: dict[SerializedNodeCommands.NodeUUID, str],
        subflow_name_to_variable_name: dict[str, str],
    ) -> list[ast.stmt]:
        # Ensure necessary imports are recorded
        import_recorder.add_from_import("griptape_nodes.node_library.library_registry", "NodeMetadata")
        import_recorder.add_from_import("griptape_nodes.node_library.library_registry", "NodeDeprecationMetadata")
        import_recorder.add_from_import("griptape_nodes.node_library.library_registry", "IconVariant")
        import_recorder.add_from_import("griptape_nodes.retained_mode.events.node_events", "CreateNodeRequest")
        import_recorder.add_from_import(
            "griptape_nodes.retained_mode.events.parameter_events", "AddParameterToNodeRequest"
        )
        import_recorder.add_from_import(
            "griptape_nodes.retained_mode.events.parameter_events", "AlterParameterDetailsRequest"
        )

        # Generate the VARIABLE name that codegen will use for this node.
        node_variable_name = f"node{node_index}_name"

        # Construct AST for the function body
        node_creation_ast = []

        # Create the CreateNodeRequest parameters
        create_node_request = serialized_node_command.create_node_command
        create_node_request_args = []

        # Extract subflow_name from metadata if it exists (only for nodes that use subflows)
        # This will be added as a parameter with a variable reference if found in mapping
        subflow_name_from_metadata = None
        if create_node_request.metadata:
            subflow_name_from_metadata = create_node_request.metadata.get("subflow_name")

        if is_dataclass(create_node_request):
            for field in fields(create_node_request):
                field_value = getattr(create_node_request, field.name)
                if field_value != field.default:
                    # Skip subflow_name field - we'll handle it separately from metadata
                    if field.name == "subflow_name":
                        continue
                    # Special handling for node_names_to_add - these are now UUIDs, convert to variable references
                    if field_value is create_node_request.node_names_to_add and field_value:
                        # field_value is now a list of UUIDs (converted in _serialize_package_nodes_for_local_execution)
                        # Convert each UUID to an AST Name node referencing the generated variable.
                        # Every member was written by now: members live in this group's subflow, and
                        # a Flow's groups are written after its subflows (see _generate_flow_code).
                        # Dropping the ones we cannot name would write the group out empty, which
                        # saves cleanly and then loads as a group the artist has to refill by hand,
                        # so fail the save instead of quietly losing the grouping.
                        unnamed_member_uuids = [
                            node_uuid for node_uuid in field_value if node_uuid not in node_uuid_to_node_variable_name
                        ]
                        if unnamed_member_uuids:
                            group_name = create_node_request.node_name or create_node_request.node_type
                            logger.error(
                                "Cannot write the members of group '%s': node(s) %s were never written to the file",
                                group_name,
                                ", ".join(unnamed_member_uuids),
                            )
                            msg = f"Attempted to save a workflow. Failed because {len(unnamed_member_uuids)} of the {len(field_value)} nodes in the group '{group_name}' had not been written to the file yet."
                            raise ValueError(msg)
                        create_node_request_args.append(
                            ast.keyword(
                                arg=field.name,
                                value=ast.List(
                                    elts=[
                                        ast.Name(id=node_uuid_to_node_variable_name[node_uuid], ctx=ast.Load())
                                        for node_uuid in field_value
                                    ],
                                    ctx=ast.Load(),
                                ),
                            )
                        )
                    else:
                        create_node_request_args.append(
                            self._keyword_from_field_value(field.name, field_value, create_node_request)
                        )

        # After processing all fields, handle subflow_name from metadata
        # If subflow_name exists in metadata and is in our mapping, add it as a parameter with variable reference
        if subflow_name_from_metadata and subflow_name_from_metadata in subflow_name_to_variable_name:
            variable_name = subflow_name_to_variable_name[subflow_name_from_metadata]
            create_node_request_args.append(
                ast.keyword(arg="subflow_name", value=ast.Name(id=variable_name, ctx=ast.Load()))
            )

        # Get the actual request class name (CreateNodeRequest)
        request_class_name = type(create_node_request).__name__
        # Handle the create node command and assign to node name
        create_node_call_ast = ast.Assign(
            targets=[ast.Name(id=node_variable_name, ctx=ast.Store(), lineno=1, col_offset=0)],
            value=ast.Attribute(
                value=ast.Await(
                    value=ast.Call(
                        func=ast.Attribute(
                            value=ast.Name(id="GriptapeNodes", ctx=ast.Load(), lineno=1, col_offset=0),
                            attr="ahandle_request",
                            ctx=ast.Load(),
                            lineno=1,
                            col_offset=0,
                        ),
                        args=[
                            ast.Call(
                                func=ast.Name(id=request_class_name, ctx=ast.Load(), lineno=1, col_offset=0),
                                args=[],
                                keywords=create_node_request_args,
                                lineno=1,
                                col_offset=0,
                            )
                        ],
                        keywords=[],
                        lineno=1,
                        col_offset=0,
                    ),
                    lineno=1,
                    col_offset=0,
                ),
                attr="node_name" if request_class_name == "CreateNodeRequest" else "node_group_name",
                ctx=ast.Load(),
                lineno=1,
                col_offset=0,
            ),
            lineno=1,
            col_offset=0,
        )

        node_creation_ast.append(create_node_call_ast)

        # Only add the 'with' statement if there are element_modification_commands
        if serialized_node_command.element_modification_commands:
            # Create the 'with' statement for the node context
            with_stmt = ast.With(
                items=[
                    ast.withitem(
                        context_expr=ast.Call(
                            func=ast.Attribute(
                                value=ast.Call(
                                    func=ast.Attribute(
                                        value=ast.Name(id="GriptapeNodes", ctx=ast.Load(), lineno=1, col_offset=0),
                                        attr="ContextManager",
                                        ctx=ast.Load(),
                                        lineno=1,
                                        col_offset=0,
                                    ),
                                    args=[],
                                    keywords=[],
                                    lineno=1,
                                    col_offset=0,
                                ),
                                attr="node",
                                ctx=ast.Load(),
                                lineno=1,
                                col_offset=0,
                            ),
                            args=[ast.Name(id=f"node{node_index}_name", ctx=ast.Load(), lineno=1, col_offset=0)],
                            keywords=[],
                            lineno=1,
                            col_offset=0,
                        ),
                        optional_vars=None,
                    )
                ],
                body=[],
                type_comment=None,
                lineno=1,
                col_offset=0,
            )

            # Generate handle_request calls for element_modification_commands
            for element_command in serialized_node_command.element_modification_commands:
                # Add import for this element command type
                element_command_class_name = element_command.__class__.__name__
                element_command_module = element_command.__class__.__module__
                import_recorder.add_from_import(element_command_module, element_command_class_name)

                # Strip default values from element_command
                element_command_args = []
                if is_dataclass(element_command):
                    for field in fields(element_command):
                        field_value = getattr(element_command, field.name)
                        if field_value != field.default:
                            element_command_args.append(
                                self._keyword_from_field_value(field.name, field_value, element_command)
                            )

                # Create the await ahandle_request call
                handle_request_call = ast.Expr(
                    value=ast.Await(
                        value=ast.Call(
                            func=ast.Attribute(
                                value=ast.Name(id="GriptapeNodes", ctx=ast.Load(), lineno=1, col_offset=0),
                                attr="ahandle_request",
                                ctx=ast.Load(),
                                lineno=1,
                                col_offset=0,
                            ),
                            args=[
                                ast.Call(
                                    func=ast.Name(
                                        id=element_command.__class__.__name__, ctx=ast.Load(), lineno=1, col_offset=0
                                    ),
                                    args=[],
                                    keywords=element_command_args,
                                    lineno=1,
                                    col_offset=0,
                                )
                            ],
                            keywords=[],
                            lineno=1,
                            col_offset=0,
                        ),
                        lineno=1,
                        col_offset=0,
                    ),
                    lineno=1,
                    col_offset=0,
                )
                with_stmt.body.append(handle_request_call)

            node_creation_ast.append(with_stmt)

        # Populate the dictionary with the node VARIABLE name and the node's UUID.
        node_uuid_to_node_variable_name[serialized_node_command.node_uuid] = node_variable_name

        return node_creation_ast

    def _generate_connections_code(
        self,
        serialized_connections: list[SerializedFlowCommands.IndirectConnectionSerialization],
        node_uuid_to_node_variable_name: dict[SerializedNodeCommands.NodeUUID, str],
        import_recorder: ImportRecorder,
    ) -> list[ast.stmt]:
        """Write the statements that reconnect a Flow's nodes.

        Args:
            serialized_connections: The connections this Flow is responsible for writing
            node_uuid_to_node_variable_name: Variable name written for each node so far, file-wide
            import_recorder: Import recorder for tracking imports

        Returns:
            The statements creating each connection

        Raises:
            ValueError: If an endpoint's node has not been written into the file yet
        """
        # Ensure necessary imports are recorded
        import_recorder.add_from_import(
            "griptape_nodes.retained_mode.events.connection_events", "CreateConnectionRequest"
        )

        connection_asts = []

        for connection in serialized_connections:
            # Match the connection's node UUID back to its variable name. Both endpoints must already
            # have been written, since the generated file refers to them by variable. Which Flow
            # writes a given edge depends on the traversal (see _generate_flow_code), so say what
            # went missing if that ever slips rather than letting a bare KeyError escape mid-save.
            missing_endpoints = [
                endpoint_uuid
                for endpoint_uuid in (connection.source_node_uuid, connection.target_node_uuid)
                if endpoint_uuid not in node_uuid_to_node_variable_name
            ]
            if missing_endpoints:
                # Skip rather than raise: raising here fails the save outright, and losing one edge
                # is a smaller harm than an artist being unable to persist their work at all. Which
                # Flow writes a given edge depends on the traversal, so a graph shape nobody has
                # tried yet is a likelier cause than real corruption -- _get_connections_for_flow
                # already carries one such case (transient loop-body flows). Logged at error so it
                # surfaces as a bug rather than passing silently.
                logger.error(
                    "Cannot write the connection to '%s': node(s) %s were never written to the file. "
                    "Skipping this connection; the saved workflow will be missing it.",
                    connection.target_parameter_name,
                    ", ".join(missing_endpoints),
                )
                continue
            source_node_variable_name = node_uuid_to_node_variable_name[connection.source_node_uuid]
            target_node_variable_name = node_uuid_to_node_variable_name[connection.target_node_uuid]

            create_connection_request_args = [
                ast.keyword(
                    arg="source_node_name",
                    value=ast.Name(id=source_node_variable_name, ctx=ast.Load()),
                ),
                ast.keyword(arg="source_parameter_name", value=ast.Constant(value=connection.source_parameter_name)),
                ast.keyword(
                    arg="target_node_name",
                    value=ast.Name(id=target_node_variable_name, ctx=ast.Load()),
                ),
                ast.keyword(arg="target_parameter_name", value=ast.Constant(value=connection.target_parameter_name)),
                ast.keyword(arg="initial_setup", value=ast.Constant(value=True)),
            ]

            create_connection_call = ast.Expr(
                value=ast.Await(
                    value=ast.Call(
                        func=ast.Attribute(
                            value=ast.Name(id="GriptapeNodes", ctx=ast.Load()),
                            attr="ahandle_request",
                            ctx=ast.Load(),
                        ),
                        args=[
                            ast.Call(
                                func=ast.Name(id="CreateConnectionRequest", ctx=ast.Load()),
                                args=[],
                                keywords=create_connection_request_args,
                            )
                        ],
                        keywords=[],
                    )
                )
            )

            connection_asts.append(create_connection_call)

        return connection_asts

    def _generate_create_variable_code(
        self,
        serialized_variable_commands: list[SerializedFlowCommands.SerializedVariableCommand],
        unique_values_dict_name: str,
        import_recorder: ImportRecorder,
    ) -> list[ast.stmt]:
        """Generate AST for CreateVariableRequest calls, one per serialized variable command.

        Each variable's value is looked up in the shared unique-values dict by UUID, mirroring
        the pattern used for parameter values.
        """
        if not serialized_variable_commands:
            return []

        import_recorder.add_from_import("griptape_nodes.retained_mode.events.variable_events", "CreateVariableRequest")

        create_variable_asts: list[ast.stmt] = []
        for serialized_command in serialized_variable_commands:
            create_variable_request = serialized_command.create_variable_command
            value_lookup = ast.Subscript(
                value=ast.Name(id=unique_values_dict_name, ctx=ast.Load(), lineno=1, col_offset=0),
                slice=ast.Constant(value=str(serialized_command.unique_value_uuid), lineno=1, col_offset=0),
                ctx=ast.Load(),
                lineno=1,
                col_offset=0,
            )

            create_variable_call = ast.Expr(
                value=ast.Call(
                    func=ast.Attribute(
                        value=ast.Name(id="GriptapeNodes", ctx=ast.Load(), lineno=1, col_offset=0),
                        attr="handle_request",
                        ctx=ast.Load(),
                        lineno=1,
                        col_offset=0,
                    ),
                    args=[
                        ast.Call(
                            func=ast.Name(id="CreateVariableRequest", ctx=ast.Load(), lineno=1, col_offset=0),
                            args=[],
                            keywords=[
                                ast.keyword(
                                    arg="name",
                                    value=ast.Constant(value=create_variable_request.name, lineno=1, col_offset=0),
                                ),
                                ast.keyword(
                                    arg="type",
                                    value=ast.Constant(value=create_variable_request.type, lineno=1, col_offset=0),
                                ),
                                ast.keyword(
                                    arg="is_global",
                                    value=ast.Constant(value=create_variable_request.is_global, lineno=1, col_offset=0),
                                ),
                                ast.keyword(arg="value", value=value_lookup, lineno=1, col_offset=0),
                                ast.keyword(
                                    arg="owning_flow",
                                    value=ast.Constant(
                                        value=create_variable_request.owning_flow, lineno=1, col_offset=0
                                    ),
                                ),
                                ast.keyword(
                                    arg="initial_setup", value=ast.Constant(value=True, lineno=1, col_offset=0)
                                ),
                            ],
                            lineno=1,
                            col_offset=0,
                        )
                    ],
                    keywords=[],
                    lineno=1,
                    col_offset=0,
                ),
                lineno=1,
                col_offset=0,
            )
            create_variable_asts.append(create_variable_call)

        return create_variable_asts

    def _generate_set_parameter_value_code(
        self,
        set_parameter_value_commands: dict[
            SerializedNodeCommands.NodeUUID, list[SerializedNodeCommands.IndirectSetParameterValueCommand]
        ],
        lock_commands: dict[SerializedNodeCommands.NodeUUID, SetLockNodeStateRequest],
        node_uuid_to_node_variable_name: dict[SerializedNodeCommands.NodeUUID, str],
        unique_values_dict_name: str,
        import_recorder: ImportRecorder,
    ) -> list[ast.stmt]:
        """Write the statements that restore saved parameter values and lock states.

        Args:
            set_parameter_value_commands: Value commands for the nodes of one Flow, keyed by node
            lock_commands: Lock-state commands for those same nodes
            node_uuid_to_node_variable_name: Variable name written for each node so far, file-wide
            unique_values_dict_name: Name of the generated dict holding the pickled values
            import_recorder: Import recorder for tracking imports

        Returns:
            The statements setting each value

        Raises:
            ValueError: If a node carrying values has not been written into the file yet
        """
        parameter_value_asts = []
        for node_uuid, indirect_set_parameter_value_commands in set_parameter_value_commands.items():
            # Values are keyed per Flow and written after that Flow's nodes and subflows, so the node
            # is always named by now. Say which node went missing if that ever stops holding, rather
            # than letting a bare KeyError escape halfway through writing the file.
            if node_uuid not in node_uuid_to_node_variable_name:
                # Skip rather than raise, for the same reason as the connection case above: this
                # guards a traversal invariant, and failing the save means the artist cannot persist
                # anything, which is worse than a saved file missing one node's values.
                logger.error(
                    "Cannot write saved values: node '%s' was never written to the file. "
                    "Skipping its values; the saved workflow will not restore them.",
                    node_uuid,
                )
                continue
            node_variable_name = node_uuid_to_node_variable_name[node_uuid]
            lock_node_command = lock_commands.get(node_uuid)
            parameter_value_asts.extend(
                self._generate_set_parameter_value_for_node(
                    node_variable_name,
                    indirect_set_parameter_value_commands,
                    unique_values_dict_name,
                    import_recorder,
                    lock_node_command,
                )
            )
        return parameter_value_asts

    def _generate_set_parameter_value_for_node(
        self,
        node_variable_name: str,
        indirect_set_parameter_value_commands: list[SerializedNodeCommands.IndirectSetParameterValueCommand],
        unique_values_dict_name: str,
        import_recorder: ImportRecorder,
        lock_node_command: SetLockNodeStateRequest | None = None,
    ) -> list[ast.stmt]:
        if not indirect_set_parameter_value_commands and lock_node_command is None:
            return []

        if indirect_set_parameter_value_commands:
            import_recorder.add_from_import(
                "griptape_nodes.retained_mode.events.parameter_events", "SetParameterValueRequest"
            )

        set_parameter_value_asts = []
        with_node_context = ast.With(
            items=[
                ast.withitem(
                    context_expr=ast.Call(
                        func=ast.Attribute(
                            value=ast.Name(id="GriptapeNodes", ctx=ast.Load(), lineno=1, col_offset=0),
                            attr="ContextManager().node",
                            ctx=ast.Load(),
                            lineno=1,
                            col_offset=0,
                        ),
                        args=[ast.Name(id=node_variable_name, ctx=ast.Load(), lineno=1, col_offset=0)],
                        keywords=[],
                        lineno=1,
                        col_offset=0,
                    ),
                    optional_vars=None,
                )
            ],
            body=[],
            lineno=1,
            col_offset=0,
        )

        for command in indirect_set_parameter_value_commands:
            value_lookup = ast.Subscript(
                value=ast.Name(id=unique_values_dict_name, ctx=ast.Load(), lineno=1, col_offset=0),
                slice=ast.Constant(value=str(command.unique_value_uuid), lineno=1, col_offset=0),
                ctx=ast.Load(),
                lineno=1,
                col_offset=0,
            )

            set_parameter_value_request_call = ast.Expr(
                value=ast.Await(
                    value=ast.Call(
                        func=ast.Attribute(
                            value=ast.Name(id="GriptapeNodes", ctx=ast.Load(), lineno=1, col_offset=0),
                            attr="ahandle_request",
                            ctx=ast.Load(),
                            lineno=1,
                            col_offset=0,
                        ),
                        args=[
                            ast.Call(
                                func=ast.Name(id="SetParameterValueRequest", ctx=ast.Load(), lineno=1, col_offset=0),
                                args=[],
                                keywords=[
                                    ast.keyword(
                                        arg="parameter_name",
                                        value=ast.Constant(
                                            value=command.set_parameter_value_command.parameter_name,
                                            lineno=1,
                                            col_offset=0,
                                        ),
                                    ),
                                    ast.keyword(
                                        arg="node_name",
                                        value=ast.Name(id=node_variable_name, ctx=ast.Load(), lineno=1, col_offset=0),
                                    ),
                                    ast.keyword(arg="value", value=value_lookup, lineno=1, col_offset=0),
                                    ast.keyword(
                                        arg="initial_setup", value=ast.Constant(value=True, lineno=1, col_offset=0)
                                    ),
                                    ast.keyword(
                                        arg="is_output",
                                        value=ast.Constant(
                                            value=command.set_parameter_value_command.is_output,
                                            lineno=1,
                                            col_offset=0,
                                        ),
                                    ),
                                ],
                                lineno=1,
                                col_offset=0,
                            )
                        ],
                        keywords=[],
                        lineno=1,
                        col_offset=0,
                    ),
                    lineno=1,
                    col_offset=0,
                ),
                lineno=1,
                col_offset=0,
            )
            with_node_context.body.append(set_parameter_value_request_call)

        # Add lock command as the LAST command in the with context
        if lock_node_command is not None:
            import_recorder.add_from_import(
                "griptape_nodes.retained_mode.events.node_events", "SetLockNodeStateRequest"
            )

            lock_node_call_ast = ast.Expr(
                value=ast.Await(
                    value=ast.Call(
                        func=ast.Attribute(
                            value=ast.Name(id="GriptapeNodes", ctx=ast.Load(), lineno=1, col_offset=0),
                            attr="ahandle_request",
                            ctx=ast.Load(),
                            lineno=1,
                            col_offset=0,
                        ),
                        args=[
                            ast.Call(
                                func=ast.Name(id="SetLockNodeStateRequest", ctx=ast.Load(), lineno=1, col_offset=0),
                                args=[],
                                keywords=[
                                    ast.keyword(
                                        arg="node_name", value=ast.Constant(value=None, lineno=1, col_offset=0)
                                    ),
                                    ast.keyword(
                                        arg="lock",
                                        value=ast.Constant(value=lock_node_command.lock, lineno=1, col_offset=0),
                                    ),
                                ],
                                lineno=1,
                                col_offset=0,
                            )
                        ],
                        keywords=[],
                        lineno=1,
                        col_offset=0,
                    ),
                    lineno=1,
                    col_offset=0,
                ),
                lineno=1,
                col_offset=0,
            )
            with_node_context.body.append(lock_node_call_ast)

        set_parameter_value_asts.append(with_node_context)
        return set_parameter_value_asts

    def _collect_object_imports(
        self,
        obj: Any,
        import_recorder: Any,
        global_modules_set: set[str],
        deferred_imports: dict[str, set[str]] | None = None,
    ) -> None:
        """Recursively collect import statements needed for all classes in object tree.

        This ensures that generated workflows have all necessary import statements,
        including for classes nested deep within containers like ParameterArrays.

        The Process:
            1. Walk through entire object tree (lists, dicts, object attributes)
            2. For each class found, determine the correct import statement
            3. For dynamic modules, use stable namespace imports
            4. For regular modules, use standard imports
            5. Record all imports for workflow generation

        Args:
            obj: Object tree to analyze for required imports
            import_recorder: Collector that will generate the import statements
            global_modules_set: Built-in modules that don't need explicit imports
            deferred_imports: If provided, dynamic library imports are collected here instead
                of import_recorder so the caller can emit them inside build_workflow() after
                sys.path has been set up by RegisterLibraryFromFileRequest.

        Example:
            Input object tree: [ReferenceImageArtifact(), {"data": ImageUrlArtifact()}]
            Generated imports:
                from griptape_nodes.node_libraries.runwayml_library.create_reference_image import ReferenceImageArtifact
                from griptape.artifacts.image_url_artifact import ImageUrlArtifact
        """

        def collect_class_import(class_type: type, _instance: Any) -> None:
            """Collect import statement for a single class."""
            module = getmodule(class_type)
            if module and module.__name__ not in global_modules_set:
                if self.engine.library_manager.is_dynamic_module(module.__name__):
                    # Use stable namespace for dynamic modules. Route into deferred_imports
                    # so the caller can emit these inside build_workflow() after
                    # RegisterLibraryFromFileRequest has added the library to sys.path.
                    stable_namespace = self.engine.library_manager.get_stable_namespace_for_dynamic_module(
                        module.__name__
                    )
                    if stable_namespace:
                        if deferred_imports is not None:
                            deferred_imports.setdefault(stable_namespace, set()).add(class_type.__name__)
                        else:
                            import_recorder.add_from_import(stable_namespace, class_type.__name__)
                    else:
                        msg = f"Missing stable namespace for {module.__name__} type {class_type.__name__}"
                        logger.error(msg)
                        raise RuntimeError(msg)
                else:
                    # Use regular module name for standard modules
                    import_recorder.add_from_import(module.__name__, class_type.__name__)

        self.walk_object_tree(obj, collect_class_import)

    @staticmethod
    def _is_ast_constant_safe(value: Any) -> bool:
        """True if value is composed only of literals ``ast.unparse`` can round-trip.

        Anything else (e.g. a Button trait object mistakenly placed in ``ui_options``)
        would be rendered by ``ast.Constant`` via its ``repr()``, which is not valid
        Python and breaks reopening the saved workflow.
        """
        primitives = (str, bytes, bool, int, float, complex, type(None))
        if isinstance(value, primitives):
            return True
        if isinstance(value, (list, tuple, set, frozenset)):
            return all(WorkflowCodeGenerator._is_ast_constant_safe(item) for item in value)
        if isinstance(value, dict):
            return all(
                WorkflowCodeGenerator._is_ast_constant_safe(key) and WorkflowCodeGenerator._is_ast_constant_safe(item)
                for key, item in value.items()
            )
        return False

    @staticmethod
    def _scrub_for_ast_constant(value: Any) -> _ScrubResult:
        """Return a copy of value with any non-literal leaves removed.

        Recurses through dict/list/tuple/set, dropping keys (for dicts) or elements
        (for sequences) whose values cannot be emitted as an ``ast.Constant``. The
        boolean flags whether anything was dropped so callers can warn.

        A bare unsafe scalar with no surrounding container is replaced with ``None``.
        """
        if WorkflowCodeGenerator._is_ast_constant_safe(value):
            return _ScrubResult(value=value, dropped=False)

        if isinstance(value, dict):
            return WorkflowCodeGenerator._scrub_dict(value)

        if isinstance(value, (list, tuple, set, frozenset)):
            return WorkflowCodeGenerator._scrub_sequence(value)

        # A bare unsafe scalar (e.g. a callable or trait object) with no container to
        # prune: signal that it was dropped and return None as a safe placeholder.
        return _ScrubResult(value=None, dropped=True)

    @staticmethod
    def _scrub_dict(value: dict) -> _ScrubResult:
        """Scrub a dict, dropping entries whose key or (non-container) value is unsafe."""
        containers = (dict, list, tuple, set, frozenset)
        scrubbed_dict = {}
        dropped = False
        for key, item in value.items():
            # An unsafe key or an unsafe non-container value means we drop the entry entirely.
            if not WorkflowCodeGenerator._is_ast_constant_safe(key) or (
                not isinstance(item, containers) and not WorkflowCodeGenerator._is_ast_constant_safe(item)
            ):
                dropped = True
                continue
            item_result = WorkflowCodeGenerator._scrub_for_ast_constant(item)
            scrubbed_dict[key] = item_result.value
            dropped = dropped or item_result.dropped
        return _ScrubResult(value=scrubbed_dict, dropped=dropped)

    @staticmethod
    def _scrub_sequence(value: list | tuple | set | frozenset) -> _ScrubResult:
        """Scrub a sequence, dropping unsafe non-container elements and recursing into containers."""
        containers = (dict, list, tuple, set, frozenset)
        scrubbed_items = []
        dropped = False
        for item in value:
            # An unsafe non-container element is dropped; containers are recursed into.
            if not isinstance(item, containers) and not WorkflowCodeGenerator._is_ast_constant_safe(item):
                dropped = True
                continue
            item_result = WorkflowCodeGenerator._scrub_for_ast_constant(item)
            scrubbed_items.append(item_result.value)
            dropped = dropped or item_result.dropped
        # Rebuild using the nearest builtin constructor rather than type(value)(...),
        # so a tuple subclass like a namedtuple (whose __new__ takes positional fields,
        # not an iterable) becomes a plain tuple instead of raising TypeError.
        if isinstance(value, list):
            rebuilt: Any = scrubbed_items
        elif isinstance(value, tuple):
            rebuilt = tuple(scrubbed_items)
        elif isinstance(value, frozenset):
            rebuilt = frozenset(scrubbed_items)
        else:
            rebuilt = set(scrubbed_items)
        return _ScrubResult(value=rebuilt, dropped=dropped)

    @staticmethod
    def _safe_arg_dest(param_name: str) -> str:
        """Derive a Python-identifier-safe argparse ``dest`` from a parameter name.

        Workflow-input parameter names can contain characters that are invalid in a
        Python identifier (e.g. ``(``, ``)``, ``.``, ``-``) — this happens when a node
        name contains them (e.g. ``Generate Media (Diffusion Pipeline)``). argparse
        would otherwise store the parsed value under an attribute that cannot be read
        back as ``args.<name>``, and the ``ast.Attribute`` access emitted into the
        generated workflow is not valid Python, so the file fails to import with
        ``SyntaxError``. Non-word characters are collapsed to underscores, and a
        leading digit is prefixed so the result is always a valid identifier.
        """
        dest = re.sub(r"\W+", "_", param_name.lower())
        if dest and dest[0].isdigit():
            dest = f"_{dest}"
        return dest

    @staticmethod
    def _keyword_from_field_value(arg_name: str, field_value: Any, command: Any) -> ast.keyword:
        """Build an ``ast.keyword`` for a command field, scrubbing unsafe content.

        Non-serializable content (e.g. a Button trait object mistakenly placed in
        ``ui_options`` instead of attached via ``traits=``) is dropped so the saved
        workflow stays valid Python and can be reopened.
        """
        scrub_result = WorkflowCodeGenerator._scrub_for_ast_constant(field_value)
        if scrub_result.dropped:
            logger.warning(
                "Omitting non-serializable content from field '%s' of %s while saving the workflow. "
                "This usually means a UI object (e.g. a Button) was placed in ui_options instead of "
                "being attached as a trait.",
                arg_name,
                type(command).__name__,
            )
        return ast.keyword(arg=arg_name, value=ast.Constant(value=scrub_result.value, lineno=1, col_offset=0))

    @staticmethod
    def _flow_has_content_to_generate(serialized_flow_commands: SerializedFlowCommands) -> bool:
        """Whether a Flow holds anything worth emitting a context block for.

        Args:
            serialized_flow_commands: Commands for the Flow being generated

        Returns:
            True if the Flow has nodes, connections, values, locks, variables, or subflows
        """
        return (
            len(serialized_flow_commands.serialized_node_commands) > 0
            or len(serialized_flow_commands.serialized_connections) > 0
            or len(serialized_flow_commands.set_parameter_value_commands) > 0
            or len(serialized_flow_commands.sub_flows_commands) > 0
            or len(serialized_flow_commands.set_lock_commands_per_node) > 0
            or len(serialized_flow_commands.serialized_variable_commands) > 0
        )
