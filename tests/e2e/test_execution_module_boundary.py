"""The execution-module boundary: where a library's heavy imports live.

Base-clean node modules used to be an author's discipline problem. An author with a heavy
dependency either scattered deferred imports through every function that touched it -- fragile,
because the next edit puts one back at module scope and nothing catches it until an orchestrator
import fails -- or kept it at module scope and lost orchestrator loading entirely.

A library now declares which modules are execution-only. Those may import anything at module
scope; the orchestrator never imports them, a worker imports them eagerly at library load, and
node code reaches them through `BaseNode.execution_module(...)`.
"""

from __future__ import annotations

import ast
import json
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

import griptape_nodes.retained_mode.managers.library_manager as library_manager_module
from griptape_nodes.node_library.library_registry import LibraryRegistry, LibrarySchema
from griptape_nodes.retained_mode.engine import current_engine
from griptape_nodes.retained_mode.events.execution_events import ExecuteNodeRequest, ExecuteNodeResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.utils.version_utils import engine_version
from tests.e2e.offline_wheels import build_wheel, offline_install_flags

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

FIXTURE = Path(__file__).parent / "fixtures" / "execution_module_library"
LIBRARY = "Execution Module Library"
EXEC_DEP = "fakeexec"
EXEC_DEP_VERSION = "1.0.0"


@pytest.fixture(autouse=True)
def _isolated_asset_root(tmp_path: Path) -> Iterator[Path]:
    """Point the engine-owned model-asset cache at a scratch dir.

    Its real root is under xdg_data_home, which is right for production and wrong for a suite: it
    persists across runs and it is the developer's own home directory.
    """
    root = tmp_path / "asset_root"
    root.mkdir(exist_ok=True)
    with patch.object(library_manager_module, "xdg_data_home", return_value=root):
        yield root


@pytest.fixture(autouse=True)
def _isolate_import_state() -> Iterator[None]:
    """Keep sys.path and the fixture's own modules from leaking between tests.

    Scoped to what these tests introduce -- the execution dependency and the fixture's own
    module names. Clearing every newly-imported module instead would take engine and beartype
    internals with it and break the next test's imports.
    """
    original_path = list(sys.path)
    original_is_worker = current_engine().library_manager._is_worker
    original_targets = current_engine().library_manager._target_library_names
    fixture_modules = (EXEC_DEP, "execution", "execution.runner", "leak_helper")
    for name in fixture_modules:
        sys.modules.pop(name, None)
    yield
    sys.path[:] = original_path
    current_engine().library_manager._is_worker = original_is_worker
    current_engine().library_manager._target_library_names = original_targets
    for name in fixture_modules:
        sys.modules.pop(name, None)


def _register(
    tmp_path: Path,
    *,
    extra_nodes: list[dict] | None = None,
    model_assets: dict[str, dict[str, object]] | None = None,
) -> str:
    """Materialise the fixture with a real installable execution dependency."""
    library_dir = tmp_path / "execution_module_library"
    shutil.copytree(FIXTURE, library_dir)
    wheel_dir = tmp_path / "wheels"
    build_wheel(wheel_dir, EXEC_DEP, EXEC_DEP_VERSION)

    manifest_path = library_dir / "griptape_nodes_library.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["library_schema_version"] = LibrarySchema.LATEST_SCHEMA_VERSION
    manifest["metadata"]["engine_version"] = engine_version
    manifest["metadata"]["dependencies"] = {
        "pip_dependencies": [],
        "pip_dependencies_exec": [EXEC_DEP],
        "pip_install_flags": offline_install_flags(wheel_dir),
    }
    if extra_nodes:
        manifest["nodes"].extend(extra_nodes)
    if model_assets is not None:
        manifest["model_assets"] = model_assets
    manifest_path.write_text(json.dumps(manifest, indent=2))

    result = current_engine().handle_request(RegisterLibraryFromFileRequest(file_path=str(manifest_path)))
    assert isinstance(result, RegisterLibraryFromFileResultSuccess), getattr(result, "result_details", result)
    return str(manifest_path)


def _library_info(manifest_path: str) -> LibraryManager.LibraryInfo:
    return current_engine().library_manager._library_file_path_to_info[manifest_path]


class TestTheOrchestratorNeverImportsExecutionModules:
    def test_the_library_loads_and_its_node_instantiates(self, tmp_path: Path) -> None:
        """The whole point: heavy dependencies exist, and the editing process is unaffected."""
        manifest_path = _register(tmp_path)

        info = _library_info(manifest_path)
        assert info.execution_modules == {}, "the orchestrator imported an execution module"
        node = LibraryRegistry.create_node(node_type="BoundaryNode", name="editable", specific_library_name=LIBRARY)
        assert node.get_parameter_by_name("reported_version") is not None

    def test_reaching_an_execution_module_here_says_why_and_what_to_do(self, tmp_path: Path) -> None:
        """An author who calls this on the orchestrator gets an instruction, not ImportError."""
        _register(tmp_path)
        node = LibraryRegistry.create_node(node_type="BoundaryNode", name="refused", specific_library_name=LIBRARY)

        with pytest.raises(RuntimeError) as excinfo:
            node.execution_module("runner")

        message = str(excinfo.value)
        assert "runner" in message
        assert "process()" in message, "the message must say where execution modules ARE available"


class TestAWorkerImportsThemEagerly:
    def test_the_worker_imports_them_at_library_load(self, tmp_path: Path) -> None:
        """Eagerly, so a broken execution module fails once at load rather than mid-graph."""
        current_engine().library_manager._is_worker = True
        manifest_path = _register(tmp_path)

        info = _library_info(manifest_path)
        assert "runner" in info.execution_modules
        assert info.execution_unavailable_reason is None

    @pytest.mark.asyncio
    async def test_the_node_reaches_its_heavy_dependency_through_the_boundary(self, tmp_path: Path) -> None:
        """End to end: node code with no heavy import of its own uses the execution dependency."""
        current_engine().library_manager._is_worker = True
        _register(tmp_path)
        node = LibraryRegistry.create_node(node_type="BoundaryNode", name="runs", specific_library_name=LIBRARY)
        current_engine().object_manager.add_object_by_name("runs", node)

        result = await current_engine().ahandle_request(
            ExecuteNodeRequest(
                node_name="runs",
                parameter_values={},
                node_metadata={"node_type": "BoundaryNode", "library": LIBRARY},
            )
        )

        assert isinstance(result, ExecuteNodeResultSuccess), getattr(result, "result_details", result)
        assert result.parameter_output_values["reported_version"] == EXEC_DEP_VERSION

    def test_an_unimportable_execution_module_costs_execution_not_editing(self, tmp_path: Path) -> None:
        """Same rule as a dependency that will not install: the nodes stay editable."""
        current_engine().library_manager._is_worker = True
        library_dir = tmp_path / "execution_module_library"
        shutil.copytree(FIXTURE, library_dir)
        (library_dir / "execution" / "runner.py").write_text("import a_module_that_does_not_exist\n")
        manifest_path = library_dir / "griptape_nodes_library.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["library_schema_version"] = LibrarySchema.LATEST_SCHEMA_VERSION
        manifest["metadata"]["engine_version"] = engine_version
        manifest_path.write_text(json.dumps(manifest, indent=2))

        result = current_engine().handle_request(RegisterLibraryFromFileRequest(file_path=str(manifest_path)))
        assert isinstance(result, RegisterLibraryFromFileResultSuccess), getattr(result, "result_details", result)

        info = _library_info(str(manifest_path))
        assert info.execution_unavailable_reason is not None
        assert "runner.py" in info.execution_unavailable_reason
        node = LibraryRegistry.create_node(
            node_type="BoundaryNode", name="still_editable", specific_library_name=LIBRARY
        )
        assert node.get_parameter_by_name("reported_version") is not None

    def test_two_execution_modules_with_one_name_is_refused(self, tmp_path: Path) -> None:
        """Modules are addressed by file name, and a directory declaration expands recursively.

        Two files sharing a stem left the later import winning and the earlier module unreachable,
        with the listing showing one name and no way to tell which had been bound. Only the author
        knows which was meant, so this is refused rather than resolved -- and the library stays
        editable, like any other execution-side failure.
        """
        current_engine().library_manager._is_worker = True
        library_dir = tmp_path / "execution_module_library"
        shutil.copytree(FIXTURE, library_dir)
        nested = library_dir / "execution" / "nested"
        nested.mkdir(parents=True, exist_ok=True)
        (nested / "runner.py").write_text("VALUE = 'the other one'\n")
        manifest_path = library_dir / "griptape_nodes_library.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["library_schema_version"] = LibrarySchema.LATEST_SCHEMA_VERSION
        manifest["metadata"]["engine_version"] = engine_version
        manifest_path.write_text(json.dumps(manifest, indent=2))

        result = current_engine().handle_request(RegisterLibraryFromFileRequest(file_path=str(manifest_path)))
        assert isinstance(result, RegisterLibraryFromFileResultSuccess), getattr(result, "result_details", result)

        info = _library_info(str(manifest_path))
        # Specifically the collision, not merely "some reason mentioning runner": this fixture's own
        # runner.py also fails to import here (its execution dependency is not installed in this
        # test), and asserting on the name alone passed with the guard removed.
        assert info.execution_unavailable_reason is not None
        assert "both named 'runner'" in info.execution_unavailable_reason, info.execution_unavailable_reason
        # Only one of the two was bound, and neither is reachable, so nothing was silently chosen.
        assert "runner" not in info.execution_modules
        node = LibraryRegistry.create_node(
            node_type="BoundaryNode", name="still_editable_on_collision", specific_library_name=LIBRARY
        )
        assert node.get_parameter_by_name("reported_version") is not None

    def test_reaching_a_module_that_failed_to_import_reports_the_import_failure(self, tmp_path: Path) -> None:
        """Not "no module by that name", which sends the author hunting for a manifest typo."""
        current_engine().library_manager._is_worker = True
        library_dir = tmp_path / "execution_module_library"
        shutil.copytree(FIXTURE, library_dir)
        (library_dir / "execution" / "runner.py").write_text("import a_module_that_does_not_exist\n")
        manifest_path = library_dir / "griptape_nodes_library.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["library_schema_version"] = LibrarySchema.LATEST_SCHEMA_VERSION
        manifest["metadata"]["engine_version"] = engine_version
        manifest_path.write_text(json.dumps(manifest, indent=2))
        current_engine().handle_request(RegisterLibraryFromFileRequest(file_path=str(manifest_path)))
        node = LibraryRegistry.create_node(node_type="BoundaryNode", name="asks", specific_library_name=LIBRARY)

        with pytest.raises(RuntimeError) as excinfo:
            node.execution_module("runner")

        message = str(excinfo.value)
        assert "runner.py" in message, "the recorded import failure must reach the caller"
        assert "no execution module named" not in message, "a typo is not the problem here"

    def test_a_library_this_worker_does_not_serve_is_left_alone(self, tmp_path: Path) -> None:
        """A worker also loads its library's DEPENDENCIES, which have no execution environment here.

        Importing their execution modules would either fail with a traceback for a perfectly
        healthy library, or succeed against this worker's pins -- silently binding someone else's
        heavy imports to them.
        """
        library_manager = current_engine().library_manager
        library_manager._is_worker = True
        library_manager._target_library_names = ["Some Other Library"]

        manifest_path = _register(tmp_path)

        info = _library_info(manifest_path)
        assert info.execution_modules == {}, "a worker imported an execution module it does not serve"
        # The discriminating assertion. Without the gate the import is ATTEMPTED and fails, because
        # this library got no execution environment here -- so the modules are empty either way,
        # and only the absence of a recorded reason says they were left alone rather than broken.
        assert info.execution_unavailable_reason is None, (
            "a library this worker does not serve was reported as unrunnable"
        )
        assert info.declared_execution_module_paths, "the declaration is still recorded, for the boundary check"


class TestTheBoundaryIsEnforced:
    def test_a_node_module_that_imports_an_execution_module_is_reported(self, tmp_path: Path) -> None:
        """Caught transitively, which is the case a source scan of node modules misses.

        `leaky_node` imports a helper, and the helper imports the execution module. Detection is
        by file location over what the import actually added to sys.modules, so the indirection
        does not hide it.
        """
        manifest_path = _register(
            tmp_path,
            extra_nodes=[
                {
                    "class_name": "LeakyNode",
                    "file_path": "leaky_node.py",
                    "metadata": {"category": "test", "description": "violates the boundary", "display_name": "Leaky"},
                }
            ],
        )

        # Node modules load lazily, so nothing imports until a class is resolved. Resolving it is
        # what triggers the violation -- and here it also RAISES, because the execution module's
        # dependency is (correctly) absent from this process. That failure path is the one authors
        # will actually hit, and it must still produce the boundary message rather than a bare
        # ImportError naming a module they did not write.
        with pytest.raises(Exception, match=r"fakeexec|leaky|Leaky"):
            LibraryRegistry.create_node(node_type="LeakyNode", name="leaks", specific_library_name=LIBRARY)

        info = _library_info(manifest_path)
        leaks = [p for p in info.problems if "execution module" in str(getattr(p, "error_message", ""))]
        assert leaks, "importing an execution module from a node module was not reported"
        message = str(getattr(leaks[0], "error_message", ""))
        assert "leaky_node.py" in message
        assert "runner.py" in message
        assert "self.execution_module" in message, "the message must name the sanctioned alternative"


class TestAllThreeMechanismsTogether:
    """One node, no heavy import in it, three answers it could not compute for itself.

    This is the arrangement the engine-owned mechanisms exist to produce: the framework import
    lives behind the boundary, the device comes from the engine's own detection, and the weights
    come from a declaration with an engine-owned cache. A node written this way contains no import
    that could fail on a machine that only edits.
    """

    def test_the_node_module_imports_nothing_heavy(self) -> None:
        """Asserted on the source, because this is the property the whole design is for."""
        source = (FIXTURE / "boundary_node.py").read_text()
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        assert EXEC_DEP not in imported, "the node module imports the execution dependency"
        assert "execution" not in imported, "the node module imports the execution module directly"
        # Not even deferred: no import statement anywhere in the file mentions them.
        assert EXEC_DEP not in source

    @pytest.mark.asyncio
    async def test_device_and_weights_reach_the_execution_module(self, tmp_path: Path) -> None:
        """The node hands the engine's answers to the execution module, which uses both."""
        current_engine().library_manager._is_worker = True
        _register(tmp_path, model_assets={"stand-in-weights": {"source": "hf:owner/repo", "revision": "pinned"}})

        # Stand in for a fetch: put files where the engine-owned cache would have written them.
        from griptape_nodes.node_library.library_registry import ModelAsset

        manager = current_engine().library_manager
        target = manager._model_asset_path(
            LIBRARY, "stand-in-weights", ModelAsset(source="hf:owner/repo", revision="pinned")
        )
        target.mkdir(parents=True, exist_ok=True)
        (target / "model.safetensors").write_text("weights")

        node = LibraryRegistry.create_node(node_type="BoundaryNode", name="all_three", specific_library_name=LIBRARY)
        current_engine().object_manager.add_object_by_name("all_three", node)
        result = await current_engine().ahandle_request(
            ExecuteNodeRequest(
                node_name="all_three",
                parameter_values={},
                node_metadata={"node_type": "BoundaryNode", "library": LIBRARY},
            )
        )

        assert isinstance(result, ExecuteNodeResultSuccess), getattr(result, "result_details", result)
        outputs = result.parameter_output_values
        assert outputs["reported_version"] == EXEC_DEP_VERSION
        assert outputs["chosen_device"] in {"cuda", "mps", "cpu"}
        # The execution module saw both the device and the weight file.
        assert outputs["chosen_device"] in outputs["run_summary"]
        assert "model.safetensors" in outputs["run_summary"]

    @pytest.mark.asyncio
    async def test_a_missing_asset_declaration_fails_the_node_with_a_reason(self, tmp_path: Path) -> None:
        """Weights that were never declared should say so, not fail somewhere far away."""
        current_engine().library_manager._is_worker = True
        _register(tmp_path)  # no model_assets declared

        node = LibraryRegistry.create_node(node_type="BoundaryNode", name="no_assets", specific_library_name=LIBRARY)
        current_engine().object_manager.add_object_by_name("no_assets", node)
        result = await current_engine().ahandle_request(
            ExecuteNodeRequest(
                node_name="no_assets",
                parameter_values={},
                node_metadata={"node_type": "BoundaryNode", "library": LIBRARY},
            )
        )

        assert isinstance(result, ExecuteNodeResultSuccess), getattr(result, "result_details", result)
        summary = result.parameter_output_values["run_summary"]
        assert "declares no model asset" in summary
        assert "stand-in-weights" in summary
