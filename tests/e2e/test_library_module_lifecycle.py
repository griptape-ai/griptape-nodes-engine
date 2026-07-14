"""End-to-end coverage for the stable library-module namespace changes.

Library node files load under a stable, deterministic module name
(``griptape_nodes.node_libraries.<lib>.<file>``) rather than a volatile per-process name derived
from ``hash(str(path))``. This is what makes pickled parameter values (including objects
embedded in saved-image metadata) unpickle reliably across an engine restart, and what lets two
libraries whose sanitized names collide coexist instead of clobbering each other.

The unit tests in ``tests/unit/retained_mode/managers/test_library_manager_stable_namespace.py``
and ``test_flow_manager.py`` cover the mechanism in isolation, within one process. This suite
drives the same behaviour end to end through the public request surface, using fresh
subprocesses wherever a real process boundary matters:

1. New image round trip: process A saves an image, process B (bare interpreter) loads it.
2. Legacy image recovery: a PNG saved by an engine using the old volatile module-name scheme
   still loads in a fresh process with the stable-namespace fixture registered.
3. Public full reload: ``ReloadAllLibrariesRequest`` hot-swaps a library's source on disk
   without changing its stable namespace or leaving volatile modules behind.
4. Sandbox lifecycle: ``RegisterSandboxNodeFromSourceRequest`` register/replace/fail/recover.
5. Namespace collision: two differently-named libraries that sanitize to the same namespace
   segment survive disambiguation, unload, and reverse load order.

Every subprocess gets its own isolated ``XDG_CONFIG_HOME`` and workspace so nothing here reads
or writes the developer's real ``~/.config/griptape_nodes`` or depends on any sibling repo.
"""

from __future__ import annotations

import base64
import json
import os
import pickle
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from griptape_nodes.retained_mode.file_metadata.workflow_metadata import FLOW_COMMANDS_KEY

DRIVER_PATH = Path(__file__).parent / "_library_module_lifecycle_driver.py"
FIXTURES_DIR = Path(__file__).parent / "fixtures"

LIFECYCLE_LIBRARY_DIR = FIXTURES_DIR / "lifecycle_library"
LIFECYCLE_LIBRARY_JSON_TEMPLATE = LIFECYCLE_LIBRARY_DIR / "griptape_nodes_library.json"
LIFECYCLE_NODE_FILE = LIFECYCLE_LIBRARY_DIR / "lifecycle_node.py"
LIFECYCLE_STABLE_NAMESPACE = "griptape_nodes.node_libraries.lifecycle_library.lifecycle_node"

COLLISION_LIBRARY_A_DIR = FIXTURES_DIR / "collision_library_a"
COLLISION_LIBRARY_B_DIR = FIXTURES_DIR / "collision_library_b"
COLLISION_STABLE_NAMESPACE = "griptape_nodes.node_libraries.collision_library.collide"

pytestmark = pytest.mark.skipif(
    not LIFECYCLE_LIBRARY_JSON_TEMPLATE.exists(),
    reason=f"Lifecycle Library fixture missing at {LIFECYCLE_LIBRARY_JSON_TEMPLATE}",
)


def _materialize_library(fixture_dir: Path, target_dir: Path) -> Path:
    """Copy a fixture library onto disk and stamp the running engine's version.

    Mirrors ``test_standalone_workflow_execution.py``'s materializer: the checked-in JSON
    keeps a placeholder ``engine_version`` that would otherwise mark the library UNUSABLE the
    moment the real engine version moves past it.
    """
    from griptape_nodes.utils.version_utils import engine_version

    target_dir.mkdir(parents=True, exist_ok=True)
    schema = json.loads((fixture_dir / "griptape_nodes_library.json").read_text())
    schema["metadata"]["engine_version"] = engine_version
    library_json = target_dir / "griptape_nodes_library.json"
    library_json.write_text(json.dumps(schema, indent=2))
    for py_file in fixture_dir.glob("*.py"):
        (target_dir / py_file.name).write_text(py_file.read_text())
    return library_json


def _isolated_env(config_root: Path, workspace: Path, *, extra_config: dict[str, Any] | None = None) -> dict[str, str]:
    """Build a subprocess environment pointed at an isolated config, never the real user's.

    ``ConfigManager.USER_CONFIG_PATH`` is computed once, at import time, from
    ``XDG_CONFIG_HOME``. Setting the environment variable only works if it is set before the
    subprocess's Python interpreter imports that module, which is exactly what happens here:
    the variable is set on the child's environment before it starts.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    config_dir = config_root / "griptape_nodes"
    config_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"workspace_directory": str(workspace), "log_level": "WARNING"}
    if extra_config:
        payload.update(extra_config)
    (config_dir / "griptape_nodes_config.json").write_text(json.dumps(payload, indent=2))

    env = os.environ.copy()
    for key in list(env):
        if key.startswith(("GTN_CONFIG_", "GT_CLOUD_", "GRIPTAPE_CLOUD")) or key in {
            "GTN_ENGINE_ID",
            "GTN_ORCHESTRATOR_ENGINE_ID",
            "GTN_STORAGE_BACKEND",
            "GTN_WORKSPACE_DIRECTORY",
        }:
            env.pop(key)

    data_root = config_root / "xdg_data"
    cache_root = config_root / "xdg_cache"
    data_root.mkdir(exist_ok=True)
    cache_root.mkdir(exist_ok=True)
    env["XDG_CONFIG_HOME"] = str(config_root)
    env["XDG_DATA_HOME"] = str(data_root)
    env["XDG_CACHE_HOME"] = str(cache_root)
    # Engine bootstrap requires GT_CLOUD_API_KEY to be set; the value never leaves the
    # subprocess so a placeholder is fine.
    env["GT_CLOUD_API_KEY"] = "fake-test-key-for-bootstrap"
    return env


def _run_driver(
    command: str, request: dict[str, Any], *, env: dict[str, str], tmp_path: Path, timeout: int = 120
) -> dict[str, Any]:
    """Run one driver command in a fresh subprocess and return its parsed response."""
    request_path = tmp_path / f"{command}_request.json"
    response_path = tmp_path / f"{command}_response.json"
    request_path.write_text(json.dumps(request))

    result = subprocess.run(  # noqa: S603 - subprocess input is constructed inside the test
        [sys.executable, str(DRIVER_PATH), command, str(request_path), str(response_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    diagnostic = (
        f"driver command={command!r} exit code: {result.returncode}\n"
        f"=== stdout ===\n{result.stdout}\n=== stderr ===\n{result.stderr}"
    )
    assert result.returncode == 0, diagnostic
    assert response_path.exists(), diagnostic
    response = json.loads(response_path.read_text())
    assert response.get("ok") is True, f"{diagnostic}\nresponse={response}"
    return response


def test_new_image_round_trip_survives_process_restart(tmp_path: Path) -> None:
    """A workflow image saved by one engine process must load correctly in a fresh one.

    Process A registers the fixture library, builds a flow around a node whose parameter
    defaults to an enum member, and writes raw PNG bytes through ``WriteFileRequest``. The
    production image artifact provider collects and injects ``gtn_flow_commands`` metadata.
    The decoded pickle is checked directly here for the stable namespace and the absence of
    any volatile token. Process B
    starts from a bare interpreter (empty ``sys.modules``, unregistered ``GriptapeNodes``),
    registers the same library fresh, and recovers the node and its enum-typed parameter value
    through the public ``ExtractFlowCommandsFromImageMetadataRequest`` (with ``deserialize=True``,
    so it exercises ``FlowManager.on_deserialize_flow_from_commands`` too).
    """
    library_json_a = _materialize_library(LIFECYCLE_LIBRARY_DIR, tmp_path / "library_a")
    library_json_b = _materialize_library(LIFECYCLE_LIBRARY_DIR, tmp_path / "library_b")
    image_path = tmp_path / "lifecycle.png"
    trigger_member = "PRESERVE"
    trigger_value = "Preserve existing"

    env_a = _isolated_env(
        tmp_path / "xdg_a",
        tmp_path / "workspace_a",
        extra_config={"auto_inject_workflow_metadata": True},
    )
    write_response = _run_driver(
        "write-image",
        {"library_json": str(library_json_a), "image_path": str(image_path), "trigger_member": trigger_member},
        env=env_a,
        tmp_path=tmp_path,
    )

    assert write_response["node_module"] == LIFECYCLE_STABLE_NAMESPACE
    pickled = base64.b64decode(write_response["encoded_flow_commands"])
    assert LIFECYCLE_STABLE_NAMESPACE.encode() in pickled, "pickle must embed the stable namespace"
    assert b"gtn_dynamic_module" not in pickled, "pickle must never embed a volatile module name"

    env_b = _isolated_env(tmp_path / "xdg_b", tmp_path / "workspace_b")
    read_response = _run_driver(
        "read-image",
        {
            "library_json": str(library_json_b),
            "image_path": str(image_path),
            "node_name": write_response["node_name"],
        },
        env=env_b,
        tmp_path=tmp_path,
    )

    assert read_response["node_module"] == LIFECYCLE_STABLE_NAMESPACE
    assert read_response["trigger_value"] == trigger_value


def test_legacy_dynamic_module_image_recovers_in_fresh_process(tmp_path: Path) -> None:
    """A PNG saved by an engine that used volatile per-process module names still loads.

    Crafts the pickle the way an old engine would have: a real module registered under a
    ``gtn_dynamic_module_lifecycle_node_py_<hash>`` name (exec'ing the checked-in fixture
    source, so the enum class is real), pickle a member of its enum, then drop the module.
    A fresh engine process never has this exact volatile name. The subprocess below never
    calls ``LibraryManager.resolve_volatile_dynamic_module`` itself; it only issues
    ``ExtractFlowCommandsFromImageMetadataRequest`` and relies on the real handler
    (``_FlowCommandsUnpickler``) to do the remapping as an implementation detail.
    """
    volatile_name = "gtn_dynamic_module_lifecycle_node_py_4816193767510271467"
    volatile_module = types.ModuleType(volatile_name)
    exec(LIFECYCLE_NODE_FILE.read_text(), volatile_module.__dict__)  # noqa: S102 - fixture source, not user input
    sys.modules[volatile_name] = volatile_module
    try:
        pickled = pickle.dumps(volatile_module.TriggerBehavior.PRESERVE)
    finally:
        del sys.modules[volatile_name]
    assert volatile_name.encode() in pickled, "sanity: the crafted pickle must reference the volatile name"

    image_path = tmp_path / "legacy.png"
    info = PngInfo()
    info.add_text(FLOW_COMMANDS_KEY, base64.b64encode(pickled).decode("ascii"))
    Image.new("RGB", (4, 4), color="red").save(image_path, format="PNG", pnginfo=info)

    library_json = _materialize_library(LIFECYCLE_LIBRARY_DIR, tmp_path / "library")
    env = _isolated_env(tmp_path / "xdg", tmp_path / "workspace")
    response = _run_driver(
        "legacy-image",
        {"library_json": str(library_json), "image_path": str(image_path)},
        env=env,
        tmp_path=tmp_path,
    )

    assert response["recovered_value"] == "Preserve existing"


def test_public_full_reload_hot_swaps_source_under_same_namespace(tmp_path: Path) -> None:
    """ReloadAllLibrariesRequest hot-swaps edited source without disturbing the stable namespace.

    The isolated config's ``libraries_to_register`` points straight at the materialized
    fixture, so the very first ``ReloadAllLibrariesRequest`` in the subprocess performs the
    initial load (v1), not just a reload. The driver then rewrites the same file on disk to
    v2, clears ``__pycache__`` and calls ``importlib.invalidate_caches()`` (no sleeps needed:
    ``_load_module_from_file`` always builds a fresh spec and execs it, so the only staleness
    risk is Python's bytecode cache, not application-level caching), and reloads again. Both
    versions must resolve to the exact same stable namespace, and no volatile
    ``gtn_dynamic_module_...`` name may appear in ``sys.modules`` at any point.
    """
    library_json = _materialize_library(LIFECYCLE_LIBRARY_DIR, tmp_path / "library")
    node_file = library_json.parent / "lifecycle_node.py"
    v1_source = node_file.read_text()
    v2_source = v1_source.replace('CLASS_MARKER = "v1"', 'CLASS_MARKER = "v2"')
    assert v2_source != v1_source, "sanity: the v1->v2 marker substitution must actually change the source"

    env = _isolated_env(
        tmp_path / "xdg",
        tmp_path / "workspace",
        extra_config={"app_events": {"on_app_initialization_complete": {"libraries_to_register": [str(library_json)]}}},
    )

    response = _run_driver(
        "public-reload",
        {"node_file": str(node_file), "v2_source": v2_source},
        env=env,
        tmp_path=tmp_path,
    )

    assert response["marker_v1"] == "v1"
    assert response["marker_v2"] == "v2"
    assert response["namespace_v1"] == LIFECYCLE_STABLE_NAMESPACE
    assert response["namespace_v2"] == response["namespace_v1"], "stable namespace must survive the reload"
    assert response["volatile_names"] == [], "no gtn_dynamic_module_* name may ever appear"
    assert response["namespace_in_sys_modules"] is True


def _sandbox_source(marker: str) -> str:
    """A minimal sandbox node source exposing ``marker`` as an observable output parameter."""
    return f'''"""Generated sandbox node source for tests/e2e/test_library_module_lifecycle.py."""

from __future__ import annotations

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode


class SandboxLifecycleNode(DataNode):
    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="marker",
                tooltip="Version marker",
                type="str",
                default_value="{marker}",
                allowed_modes={{ParameterMode.OUTPUT, ParameterMode.PROPERTY}},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["marker"] = "{marker}"
'''


def test_sandbox_lifecycle_register_replace_fail_recover(tmp_path: Path) -> None:
    """RegisterSandboxNodeFromSourceRequest across a full register/replace/fail/recover cycle.

    The isolated config sets ``sandbox_library_directory`` to an empty, already-created
    directory; the first ``ReloadAllLibrariesRequest`` stands up a real, empty Sandbox Library
    the same way engine startup would, entirely through public requests, with no direct
    ``LibraryRegistry`` access. Everything after that runs through
    ``RegisterSandboxNodeFromSourceRequest(replace_if_exists=True)``: v1 registers cleanly, v2
    replaces it, an invalid (syntax error) rewrite must fail registration while leaving v2's
    class and module fully usable, and v3 (valid again) recovers.
    """
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()

    env = _isolated_env(
        tmp_path / "xdg", tmp_path / "workspace", extra_config={"sandbox_library_directory": str(sandbox_dir)}
    )

    invalid_source = _sandbox_source("v2") + "\ndef _broken(:\n    pass\n"

    response = _run_driver(
        "sandbox-lifecycle",
        {
            "sandbox_dir": str(sandbox_dir),
            "file_name": "sandbox_lifecycle_node.py",
            "class_name": "SandboxLifecycleNode",
            "v1_source": _sandbox_source("v1"),
            "v2_source": _sandbox_source("v2"),
            "invalid_source": invalid_source,
            "v3_source": _sandbox_source("v3"),
        },
        env=env,
        tmp_path=tmp_path,
    )

    assert response["v1_replaced"] == [], "first registration must not report a replacement"
    assert response["v1_marker"] == "v1"
    assert response["v2_replaced"] == ["SandboxLifecycleNode"]
    assert response["v2_marker"] == "v2"
    assert response["invalid_failed"] is True
    assert response["post_invalid_marker"] == "v2", (
        "the previous (v2) class/module must remain usable after a failed reload"
    )
    sandbox_namespace = "griptape_nodes.node_libraries.sandbox_library.sandbox_lifecycle_node"
    assert response["namespace_v1"] == sandbox_namespace
    assert response["namespace_v2"] == sandbox_namespace
    assert response["module_rollback_preserved"] is True
    assert response["v3_replaced"] == ["SandboxLifecycleNode"]
    assert response["v3_marker"] == "v3"
    assert response["namespace_v3"] == sandbox_namespace


def test_namespace_collision_across_libraries_survives_unload_and_reverse_reload(tmp_path: Path) -> None:
    """Two libraries whose sanitized names collide coexist, unload cleanly, and reload reversed.

    "Collision Library" and "Collision-Library" both sanitize to the safe module segment
    "collision_library"; both declare a node file named "collide.py". Loading them in order
    gives the first library the plain stable namespace and disambiguates the second with a
    hash suffix. Unloading the base-namespace winner must not disturb the loser's module.
    Unloading both and reloading in the opposite order must flip who gets the plain name, and
    must never leave a stale leaf module behind from either lifecycle.
    """
    library_json_a = _materialize_library(COLLISION_LIBRARY_A_DIR, tmp_path / "library_a")
    library_json_b = _materialize_library(COLLISION_LIBRARY_B_DIR, tmp_path / "library_b")

    env = _isolated_env(tmp_path / "xdg", tmp_path / "workspace")
    response = _run_driver(
        "collision",
        {"library_json_a": str(library_json_a), "library_json_b": str(library_json_b)},
        env=env,
        tmp_path=tmp_path,
    )

    assert response["namespace_a1"] == COLLISION_STABLE_NAMESPACE, "first-loaded library keeps the plain namespace"
    assert response["namespace_b1"] != COLLISION_STABLE_NAMESPACE
    assert response["namespace_b1"].startswith(COLLISION_STABLE_NAMESPACE + "_"), "second load is disambiguated"
    assert set(response["modules_after_first_load"]) == {response["namespace_a1"], response["namespace_b1"]}

    assert response["namespace_b_after_a_unload"] == response["namespace_b1"], (
        "unloading the winner must not disturb the loser's module"
    )
    assert response["modules_after_a_unload"] == [response["namespace_b1"]], (
        "unloading the winner must leave only the loser's leaf module registered"
    )
    assert response["modules_after_both_unloaded"] == [], "unloading both must leave no leaf modules behind"

    assert response["namespace_b2"] == COLLISION_STABLE_NAMESPACE, "reverse order flips who wins the plain namespace"
    assert response["namespace_a2"] != COLLISION_STABLE_NAMESPACE
    assert response["namespace_a2"].startswith(COLLISION_STABLE_NAMESPACE + "_")
    assert set(response["modules_after_reverse_load"]) == {response["namespace_a2"], response["namespace_b2"]}
