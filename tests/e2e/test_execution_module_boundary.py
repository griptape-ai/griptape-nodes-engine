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

import json
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

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
def _isolate_import_state() -> Iterator[None]:
    """Keep sys.path and the fixture's own modules from leaking between tests.

    Scoped to what these tests introduce -- the execution dependency and the fixture's own
    module names. Clearing every newly-imported module instead would take engine and beartype
    internals with it and break the next test's imports.
    """
    original_path = list(sys.path)
    original_is_worker = current_engine().library_manager._is_worker
    fixture_modules = (EXEC_DEP, "execution", "execution.runner", "leak_helper")
    for name in fixture_modules:
        sys.modules.pop(name, None)
    yield
    sys.path[:] = original_path
    current_engine().library_manager._is_worker = original_is_worker
    for name in fixture_modules:
        sys.modules.pop(name, None)


def _register(tmp_path: Path, *, extra_nodes: list[dict] | None = None) -> str:
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
