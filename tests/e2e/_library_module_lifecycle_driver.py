"""Subprocess driver for tests/e2e/test_library_module_lifecycle.py.

Not a test module (no ``test_*`` prefix, not collected by pytest). Each function below
implements one step of a stable-library-module-namespace lifecycle scenario and is invoked in
a brand-new Python process by the test file, so it always starts from a bare, unregistered
``GriptapeNodes`` singleton and an empty ``sys.modules``. That is the point: several of the
scenarios this suite covers (pickled values surviving a process restart, hot reload leaving no
volatile module names behind) only mean something when proven across a real process boundary,
not by resetting in-memory state within the same interpreter.

Invocation: ``python _library_module_lifecycle_driver.py <command> <request.json> <response.json>``.
``request.json`` is a plain JSON object of command-specific arguments; the command writes a
plain JSON object of diagnostics (always including ``"ok"``) to ``response.json``. On an
unhandled exception the response file still gets a best-effort ``{"ok": False, "error": ...}``
payload and the process exits non-zero with the traceback on stderr, so the parent test gets a
useful failure either way.

All engine imports live at module scope: the parent test sets the isolated environment
(``XDG_CONFIG_HOME`` and friends) on the child process before its interpreter starts, so
import-time configuration reads are already isolated by the time this module loads.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import shutil
import sys
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image

from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.retained_mode.events.artifact_events import (
    RegisterArtifactProviderRequest,
    RegisterArtifactProviderResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    ExtractFlowCommandsFromImageMetadataRequest,
    ExtractFlowCommandsFromImageMetadataResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
    RegisterSandboxNodeFromSourceRequest,
    RegisterSandboxNodeFromSourceResultFailure,
    RegisterSandboxNodeFromSourceResultSuccess,
    ReloadAllLibrariesRequest,
    ReloadAllLibrariesResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.os_events import WriteFileRequest, WriteFileResultSuccess
from griptape_nodes.retained_mode.events.parameter_events import (
    GetParameterValueRequest,
    GetParameterValueResultSuccess,
    SetParameterValueRequest,
)
from griptape_nodes.retained_mode.file_metadata.workflow_metadata import FLOW_COMMANDS_KEY
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider import (
    ImageArtifactProvider,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.events.base_events import ResultPayload


def _register_library(file_path: str) -> None:
    result = GriptapeNodes.handle_request(RegisterLibraryFromFileRequest(file_path=file_path))
    assert isinstance(result, RegisterLibraryFromFileResultSuccess), (
        f"Failed to register library from '{file_path}': {result.result_details}"
    )


def _create_node(node_type: str, library_name: str, node_name: str, flow_name: str) -> str:
    result = GriptapeNodes.handle_request(
        CreateNodeRequest(
            node_type=node_type,
            specific_library_name=library_name,
            node_name=node_name,
            override_parent_flow_name=flow_name,
        )
    )
    assert isinstance(result, CreateNodeResultSuccess), (
        f"Failed to create node '{node_type}' from library '{library_name}': {result.result_details}"
    )
    assert result.node_type == node_type, (
        f"Expected node type '{node_type}' but engine substituted '{result.node_type}' (likely an ErrorProxyNode)."
    )
    return result.node_name


def _get_parameter_value(node_name: str, parameter_name: str) -> Any:
    result = GriptapeNodes.handle_request(GetParameterValueRequest(node_name=node_name, parameter_name=parameter_name))
    assert isinstance(result, GetParameterValueResultSuccess), (
        f"Failed to get parameter '{parameter_name}' on node '{node_name}': {result.result_details}"
    )
    return result.value


def _node_module_name(node_name: str) -> str:
    node = GriptapeNodes.NodeManager().get_node_by_name(node_name)
    return type(node).__module__


def _push_workflow(workflow_name: str) -> None:
    GriptapeNodes.ContextManager().push_workflow(workflow_name=workflow_name)


def _create_flow(flow_name: str) -> None:
    # ReloadAllLibrariesRequest tears down every active workflow context (via
    # ClearAllObjectStateRequest), so a command that creates a flow after reloading needs a
    # fresh workflow pushed first; CreateFlowRequest requires one to be active.
    if not GriptapeNodes.ContextManager().has_current_workflow():
        _push_workflow(f"{flow_name}_workflow")

    result = GriptapeNodes.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name=flow_name, set_as_new_context=False)
    )
    assert isinstance(result, CreateFlowResultSuccess), f"Failed to create flow '{flow_name}': {result.result_details}"


async def _reload_all_libraries() -> None:
    result = await GriptapeNodes.ahandle_request(ReloadAllLibrariesRequest())
    assert isinstance(result, ReloadAllLibrariesResultSuccess), (
        f"ReloadAllLibrariesRequest failed: {result.result_details}"
    )


def cmd_write_image(request: dict[str, Any]) -> dict[str, Any]:
    """Process A: register the fixture library, build a flow, and save a PNG.

    Writes raw PNG bytes through ``WriteFileRequest`` so the production OSManager,
    ArtifactManager, image artifact provider, and workflow metadata collector inject the
    flow commands exactly as they do for application-generated images.
    """
    _register_library(request["library_json"])
    provider_result = GriptapeNodes.handle_request(
        RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider)
    )
    assert isinstance(provider_result, RegisterArtifactProviderResultSuccess), (
        f"Failed to register production image provider: {provider_result.result_details}"
    )

    flow_name = "LifecycleFlow"
    _create_flow(flow_name)
    node_name = _create_node("LifecycleNode", "Lifecycle Library", "Lifecycle_1", flow_name)

    # Use the real enum member (not a plain string) as the parameter value, so the pickled
    # payload embeds a class reference under the stable namespace, the exact shape of the
    # regression this test guards against.
    node_module = _node_module_name(node_name)
    trigger_behavior = sys.modules[node_module].TriggerBehavior
    trigger_member = getattr(trigger_behavior, request["trigger_member"])
    set_result = GriptapeNodes.handle_request(
        SetParameterValueRequest(node_name=node_name, parameter_name="trigger", value=trigger_member)
    )
    assert set_result.succeeded(), f"Failed to set 'trigger' parameter: {set_result.result_details}"

    raw_png = BytesIO()
    Image.new("RGB", (4, 4), color="blue").save(raw_png, format="PNG")
    image_path = Path(request["image_path"])
    with GriptapeNodes.ContextManager().flow(flow_name):
        write_result = GriptapeNodes.handle_request(
            WriteFileRequest(file_path=str(image_path), content=raw_png.getvalue())
        )
    assert isinstance(write_result, WriteFileResultSuccess), (
        f"Production PNG write failed: {write_result.result_details}"
    )

    with Image.open(write_result.final_file_path) as saved_image:
        encoded_flow_commands = saved_image.info.get(FLOW_COMMANDS_KEY)
    assert isinstance(encoded_flow_commands, str), "Production PNG write did not inject gtn_flow_commands metadata."

    return {
        "ok": True,
        "node_name": node_name,
        "node_module": node_module,
        "encoded_flow_commands": encoded_flow_commands,
    }


def cmd_read_image(request: dict[str, Any]) -> dict[str, Any]:
    """Process B: register the fixture library fresh, then extract+deserialize the PNG's flow.

    Runs entirely through the public request surface: ``ExtractFlowCommandsFromImageMetadataRequest``
    with ``deserialize=True`` drives ``FlowManager.on_deserialize_flow_from_commands`` internally.
    Because the embedded commands were serialized with ``include_create_flow_command=False``
    (what ``workflow_metadata._serialize_flow`` always does), a flow must already be the current
    context for deserialization to land in.
    """
    _register_library(request["library_json"])

    flow_name = "ReceivedFlow"
    _create_flow(flow_name)

    with GriptapeNodes.ContextManager().flow(flow_name):
        extract_result = GriptapeNodes.handle_request(
            ExtractFlowCommandsFromImageMetadataRequest(file_url_or_path=request["image_path"], deserialize=True)
        )
        assert isinstance(extract_result, ExtractFlowCommandsFromImageMetadataResultSuccess), (
            f"Failed to extract flow commands from image: {extract_result.result_details}"
        )

        original_node_name = str(request["node_name"])
        deserialized_node_name = extract_result.node_name_mappings.get(original_node_name, original_node_name)
        trigger_value = _get_parameter_value(deserialized_node_name, "trigger")
        node_module = _node_module_name(deserialized_node_name)

    return {
        "ok": True,
        "flow_name": extract_result.flow_name,
        "deserialized_node_name": deserialized_node_name,
        "node_module": node_module,
        "trigger_value": str(trigger_value),
    }


def cmd_legacy_image(request: dict[str, Any]) -> dict[str, Any]:
    """Fresh process: register the stable fixture, then extract a legacy-format PNG.

    The PNG was crafted by the test process using a volatile ``gtn_dynamic_module_..._<hash>``
    module name that never existed here. Goes through the public
    ``ExtractFlowCommandsFromImageMetadataRequest`` only; the volatile-name resolution inside
    ``_FlowCommandsUnpickler``/``LibraryManager.resolve_volatile_dynamic_class`` runs as an
    implementation detail of that request, never called directly.
    """
    _register_library(request["library_json"])

    result = GriptapeNodes.handle_request(
        ExtractFlowCommandsFromImageMetadataRequest(file_url_or_path=request["image_path"], deserialize=False)
    )
    assert isinstance(result, ExtractFlowCommandsFromImageMetadataResultSuccess), (
        f"Failed to extract legacy flow commands: {result.result_details}"
    )

    return {"ok": True, "recovered_value": str(result.serialized_flow_commands)}


def cmd_public_reload(request: dict[str, Any]) -> dict[str, Any]:
    """Isolated-config process: load v1 via ReloadAllLibrariesRequest, edit on disk, reload to v2.

    ``libraries_to_register`` in the isolated XDG config (written by the test before this
    process starts) already points at the materialized fixture library, so the very first
    ``ReloadAllLibrariesRequest`` performs the initial load, not just a reload.
    """

    def _create_and_read() -> tuple[str, str, str]:
        flow_name = "LifecycleFlow"
        _create_flow(flow_name)
        node_name = _create_node("LifecycleNode", "Lifecycle Library", "Lifecycle_1", flow_name)
        module_name = _node_module_name(node_name)
        marker = str(_get_parameter_value(node_name, "class_marker"))
        return node_name, module_name, marker

    asyncio.run(_reload_all_libraries())
    _node_v1, namespace_v1, marker_v1 = _create_and_read()

    node_file = Path(request["node_file"])
    node_file.write_text(request["v2_source"])
    shutil.rmtree(node_file.parent / "__pycache__", ignore_errors=True)
    importlib.invalidate_caches()

    asyncio.run(_reload_all_libraries())
    _node_v2, namespace_v2, marker_v2 = _create_and_read()

    volatile_names = [name for name in sys.modules if name.startswith("gtn_dynamic_module")]

    return {
        "ok": True,
        "namespace_v1": namespace_v1,
        "namespace_v2": namespace_v2,
        "marker_v1": marker_v1,
        "marker_v2": marker_v2,
        "volatile_names": volatile_names,
        "namespace_in_sys_modules": namespace_v1 in sys.modules,
    }


def cmd_sandbox_lifecycle(request: dict[str, Any]) -> dict[str, Any]:
    """Isolated-config process: exercise RegisterSandboxNodeFromSourceRequest end to end.

    The isolated config (written by the test) sets ``sandbox_library_directory`` to an empty,
    already-created directory. The first ``ReloadAllLibrariesRequest`` stands up a real, empty
    Sandbox Library the same way a normal engine start would; everything after that runs
    through the public ``RegisterSandboxNodeFromSourceRequest``.
    """
    asyncio.run(_reload_all_libraries())

    class_name = request["class_name"]
    node_file = Path(request["sandbox_dir"]) / request["file_name"]

    def _write_and_invalidate(source: str) -> None:
        node_file.write_text(source)
        shutil.rmtree(node_file.parent / "__pycache__", ignore_errors=True)
        importlib.invalidate_caches()

    def _register() -> ResultPayload:
        return GriptapeNodes.handle_request(
            RegisterSandboxNodeFromSourceRequest(file_path=str(node_file), replace_if_exists=True)
        )

    def _create_and_read_marker(node_name: str) -> str:
        created_name = _create_node(class_name, "Sandbox Library", node_name, "SandboxFlow")
        return str(_get_parameter_value(created_name, "marker"))

    def _registered_class() -> type:
        return LibraryRegistry.get_library("Sandbox Library").get_node_class(class_name)

    _create_flow("SandboxFlow")

    # v1: first registration, nothing replaced.
    _write_and_invalidate(request["v1_source"])
    v1_register = _register()
    assert isinstance(v1_register, RegisterSandboxNodeFromSourceResultSuccess), (
        f"v1 registration failed: {v1_register.result_details}"
    )
    v1_marker = _create_and_read_marker("SandboxProbe_1")
    namespace_v1 = _registered_class().__module__

    # v2: same class name, replace_if_exists swaps the old class for the new one.
    _write_and_invalidate(request["v2_source"])
    v2_register = _register()
    assert isinstance(v2_register, RegisterSandboxNodeFromSourceResultSuccess), (
        f"v2 registration failed: {v2_register.result_details}"
    )
    v2_marker = _create_and_read_marker("SandboxProbe_2")
    v2_class = _registered_class()
    namespace_v2 = v2_class.__module__
    v2_module = sys.modules[namespace_v2]

    # invalid: a syntax error must fail the registration and leave v2 usable.
    _write_and_invalidate(request["invalid_source"])
    invalid_register = _register()
    invalid_failed = isinstance(invalid_register, RegisterSandboxNodeFromSourceResultFailure)
    module_rollback_preserved = sys.modules.get(namespace_v2) is v2_module and _registered_class() is v2_class
    post_invalid_marker = _create_and_read_marker("SandboxProbe_3")

    # v3: fixing the source recovers registration.
    _write_and_invalidate(request["v3_source"])
    v3_register = _register()
    assert isinstance(v3_register, RegisterSandboxNodeFromSourceResultSuccess), (
        f"v3 registration failed: {v3_register.result_details}"
    )
    v3_marker = _create_and_read_marker("SandboxProbe_4")
    namespace_v3 = _registered_class().__module__

    return {
        "ok": True,
        "v1_replaced": v1_register.replaced_class_names,
        "v1_marker": v1_marker,
        "namespace_v1": namespace_v1,
        "v2_replaced": v2_register.replaced_class_names,
        "v2_marker": v2_marker,
        "namespace_v2": namespace_v2,
        "invalid_failed": invalid_failed,
        "module_rollback_preserved": module_rollback_preserved,
        "post_invalid_marker": post_invalid_marker,
        "v3_replaced": v3_register.replaced_class_names,
        "v3_marker": v3_marker,
        "namespace_v3": namespace_v3,
    }


def cmd_collision(request: dict[str, Any]) -> dict[str, Any]:
    """Two libraries whose sanitized names collide: the first registers, the second is reported.

    "Collision Library" and "Collision-Library" both sanitize to the safe module segment
    "collision_library" and each declare a node file named "collide.py", so the second
    library's node file always maps to the namespace the first already claimed. The first
    library registers and its node type is usable; the second fails registration and reports
    a node-module namespace collision problem instead of registering its node type.
    """
    library_json_a = request["library_json_a"]
    library_json_b = request["library_json_b"]

    _create_flow("CollisionFlow")

    _register_library(library_json_a)
    node_a = _create_node("CollisionNodeA", "Collision Library", "CollisionNodeA_probe", "CollisionFlow")
    namespace_a = _node_module_name(node_a)

    register_b = GriptapeNodes.handle_request(RegisterLibraryFromFileRequest(file_path=library_json_b))
    library_b = LibraryRegistry.get_library("Collision-Library")
    collision_problem = GriptapeNodes.LibraryManager().get_collated_problems_for_library("Collision-Library")

    return {
        "ok": True,
        "namespace_a": namespace_a,
        "register_b_succeeded": register_b.succeeded(),
        "register_b_details": str(register_b.result_details),
        "library_b_has_node_type": library_b.has_node_type("CollisionNodeB"),
        "collision_problem": collision_problem,
    }


COMMANDS = {
    "write-image": cmd_write_image,
    "read-image": cmd_read_image,
    "legacy-image": cmd_legacy_image,
    "public-reload": cmd_public_reload,
    "sandbox-lifecycle": cmd_sandbox_lifecycle,
    "collision": cmd_collision,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("request_path", type=Path)
    parser.add_argument("response_path", type=Path)
    args = parser.parse_args()

    request = json.loads(args.request_path.read_text())
    handler = COMMANDS[args.command]

    try:
        response = handler(request)
    except Exception as exc:
        args.response_path.write_text(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        raise
    args.response_path.write_text(json.dumps(response, indent=2))


if __name__ == "__main__":
    main()
