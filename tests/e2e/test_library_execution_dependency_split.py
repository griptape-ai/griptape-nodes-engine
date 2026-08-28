"""End-to-end tests for the edit-time / execution dependency split.

A library declares its dependencies in two sets. Edit-time dependencies are what the engine
needs to import node modules and instantiate nodes; execution dependencies are the heavy
packages only ``process`` needs. The split exists so the orchestrator can hold real node
classes for every library without ever putting those heavy packages on its import path, which
is what stops one library's pins from shadowing another's.

The contract these tests pin:

- **On the orchestrator**: edit-time dependencies are importable and nodes instantiate, while
  execution dependencies are installed on disk but deliberately absent from ``sys.path``.
- **In a worker**: both environments are on ``sys.path``, so ``process`` runs.
- **For a library that declares no execution dependencies**: nothing changes, and no second
  environment appears on disk.

Both dependencies are hand-built wheels installed from a local ``--find-links`` directory with
``--no-index``, so the tests are fully offline. Because ``fakeedit`` and ``fakeexec`` exist
nowhere except those wheels, a successful import IS proof the matching environment was created
and wired onto ``sys.path`` -- and an ImportError is proof it was not.
"""

from __future__ import annotations

import importlib
import json
import sys
import sysconfig
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.node_library.library_registry import LibraryRegistry, LibrarySchema
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.utils.version_utils import engine_version
from tests.e2e.offline_wheels import build_wheel, offline_install_flags

if TYPE_CHECKING:
    from collections.abc import Iterator

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "exec_dep_library"
FIXTURE_FILES = ("exec_dep_node.py",)
EDIT_DEP = "fakeedit"
EXEC_DEP = "fakeexec"
EDIT_DEP_VERSION = "1.0.0"
EXEC_DEP_VERSION = "2.0.0"


@pytest.fixture(autouse=True)
def _isolate_import_state() -> Iterator[None]:
    """Keep sys.path and sys.modules from leaking between tests.

    Every registration inserts paths and imports modules. Without this, a dependency imported
    by one test would satisfy the next test's import from the module cache, which would hide
    exactly the wiring these tests exist to check.
    """
    original_path = list(sys.path)
    original_is_worker = GriptapeNodes.LibraryManager()._is_worker
    for dep in (EDIT_DEP, EXEC_DEP):
        sys.modules.pop(dep, None)
    yield
    sys.path[:] = original_path
    GriptapeNodes.LibraryManager()._is_worker = original_is_worker
    for dep in (EDIT_DEP, EXEC_DEP):
        sys.modules.pop(dep, None)


def _materialize_library(
    target_dir: Path,
    *,
    wheel_dir: Path,
    name: str,
    exec_dependencies: list[str],
) -> Path:
    """Write a registrable copy of the fixture library, declaring the given dependency split."""
    schema = json.loads((FIXTURE_DIR / "griptape_nodes_library.json").read_text())
    schema["name"] = name
    schema["library_schema_version"] = LibrarySchema.LATEST_SCHEMA_VERSION
    schema["metadata"]["engine_version"] = engine_version
    schema["metadata"]["dependencies"] = {
        "pip_dependencies": [EDIT_DEP],
        "pip_dependencies_exec": exec_dependencies,
        "pip_install_flags": offline_install_flags(wheel_dir),
    }
    target_dir.mkdir(parents=True, exist_ok=True)
    library_json = target_dir / "griptape_nodes_library.json"
    library_json.write_text(json.dumps(schema, indent=2))
    for file_name in FIXTURE_FILES:
        (target_dir / file_name).write_text((FIXTURE_DIR / file_name).read_text())
    return library_json


def _build_both_wheels(wheel_dir: Path) -> None:
    build_wheel(wheel_dir, EDIT_DEP, EDIT_DEP_VERSION)
    build_wheel(wheel_dir, EXEC_DEP, EXEC_DEP_VERSION)


def _register(library_json: Path) -> RegisterLibraryFromFileResultSuccess:
    result = GriptapeNodes.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(result, RegisterLibraryFromFileResultSuccess), getattr(result, "result_details", result)
    return result


def _library_info(library_json: Path) -> LibraryManager.LibraryInfo:
    return GriptapeNodes.LibraryManager()._library_file_path_to_info[str(library_json)]


def _site_packages(venv_path: Path) -> str:
    return str(Path(sysconfig.get_path("purelib", vars={"base": str(venv_path), "platbase": str(venv_path)})))


def _import_fresh(module_name: str) -> object:
    """Import a module without letting a previous test's cache answer for it."""
    sys.modules.pop(module_name, None)
    importlib.invalidate_caches()
    return importlib.import_module(module_name)


class TestOrchestratorHoldsEditTimeDepsOnly:
    def test_execution_dependencies_are_installed_but_absent_from_sys_path(self, tmp_path: Path) -> None:
        """The orchestrator installs both sets but only imports against the edit-time one.

        This is the isolation the whole design rests on: the heavy packages exist on disk, ready
        for a worker, and cannot be reached from the process that runs the editor.
        """
        wheel_dir = tmp_path / "wheels"
        _build_both_wheels(wheel_dir)
        library_dir = tmp_path / "library"
        library_json = _materialize_library(
            library_dir, wheel_dir=wheel_dir, name="Exec Dep Orchestrator", exec_dependencies=[EXEC_DEP]
        )

        _register(library_json)

        info = _library_info(library_json)
        assert info.lifecycle_state is LibraryManager.LibraryLifecycleState.LOADED
        assert info.fitness is LibraryManager.LibraryFitness.GOOD

        # Both environments were built on disk.
        edit_venv = library_dir / ".venv"
        exec_venv = library_dir / ".venv-exec"
        assert edit_venv.exists()
        assert exec_venv.exists()

        # Only the edit-time one is importable from here.
        assert _site_packages(edit_venv) in sys.path
        assert _site_packages(exec_venv) not in sys.path
        assert _import_fresh(EDIT_DEP).__version__ == EDIT_DEP_VERSION  # type: ignore[attr-defined]
        with pytest.raises(ImportError):
            _import_fresh(EXEC_DEP)

    def test_node_instantiates_on_the_orchestrator_without_execution_dependencies(self, tmp_path: Path) -> None:
        """A real node class, instantiated for real, with the heavy dependency unreachable.

        This is what lets the orchestrator drop stub nodes: it holds the actual class, and the
        actual class only needs edit-time dependencies to exist.
        """
        wheel_dir = tmp_path / "wheels"
        _build_both_wheels(wheel_dir)
        library_dir = tmp_path / "library"
        library_json = _materialize_library(
            library_dir, wheel_dir=wheel_dir, name="Exec Dep Instantiate", exec_dependencies=[EXEC_DEP]
        )
        _register(library_json)

        node = LibraryRegistry.create_node(
            node_type="ExecDepNode", name="exec_dep_node", specific_library_name="Exec Dep Instantiate"
        )

        assert node.get_parameter_value("edit_dep_version") == EDIT_DEP_VERSION
        with pytest.raises(ImportError):
            _import_fresh(EXEC_DEP)


class TestWorkerHoldsBothEnvironments:
    def test_worker_puts_both_environments_on_sys_path(self, tmp_path: Path) -> None:
        wheel_dir = tmp_path / "wheels"
        _build_both_wheels(wheel_dir)
        library_dir = tmp_path / "library"
        library_json = _materialize_library(
            library_dir, wheel_dir=wheel_dir, name="Exec Dep Worker", exec_dependencies=[EXEC_DEP]
        )
        GriptapeNodes.LibraryManager()._is_worker = True

        _register(library_json)

        assert _site_packages(library_dir / ".venv") in sys.path
        assert _site_packages(library_dir / ".venv-exec") in sys.path
        assert _import_fresh(EDIT_DEP).__version__ == EDIT_DEP_VERSION  # type: ignore[attr-defined]
        assert _import_fresh(EXEC_DEP).__version__ == EXEC_DEP_VERSION  # type: ignore[attr-defined]

    def test_process_runs_in_a_worker(self, tmp_path: Path) -> None:
        """The payoff: the same node whose process is unrunnable on the orchestrator runs here."""
        wheel_dir = tmp_path / "wheels"
        _build_both_wheels(wheel_dir)
        library_dir = tmp_path / "library"
        library_json = _materialize_library(
            library_dir, wheel_dir=wheel_dir, name="Exec Dep Worker Process", exec_dependencies=[EXEC_DEP]
        )
        GriptapeNodes.LibraryManager()._is_worker = True
        _register(library_json)
        node = LibraryRegistry.create_node(
            node_type="ExecDepNode", name="exec_dep_node", specific_library_name="Exec Dep Worker Process"
        )

        node.process()

        assert node.parameter_output_values["edit_dep_version"] == EDIT_DEP_VERSION
        assert node.parameter_output_values["exec_dep_version"] == EXEC_DEP_VERSION


class TestLibrariesWithoutExecutionDependencies:
    def test_no_execution_environment_is_created(self, tmp_path: Path) -> None:
        """A library that declares no execution dependencies gets exactly today's layout.

        Backward compatibility is the point: the split is opt-in, and opting out leaves no new
        artifacts on disk.
        """
        wheel_dir = tmp_path / "wheels"
        _build_both_wheels(wheel_dir)
        library_dir = tmp_path / "library"
        library_json = _materialize_library(
            library_dir, wheel_dir=wheel_dir, name="Exec Dep None Declared", exec_dependencies=[]
        )

        _register(library_json)

        info = _library_info(library_json)
        assert info.lifecycle_state is LibraryManager.LibraryLifecycleState.LOADED
        assert (library_dir / ".venv").exists()
        assert not (library_dir / ".venv-exec").exists()
        assert _import_fresh(EDIT_DEP).__version__ == EDIT_DEP_VERSION  # type: ignore[attr-defined]

    def test_manifest_without_the_field_still_validates(self) -> None:
        """An older manifest that never heard of execution dependencies loads unchanged."""
        schema = json.loads((FIXTURE_DIR / "griptape_nodes_library.json").read_text())
        schema["library_schema_version"] = "0.11.0"
        schema["metadata"]["dependencies"] = {"pip_dependencies": ["something"]}

        parsed = LibrarySchema.model_validate(schema)

        assert parsed.metadata.dependencies is not None
        assert parsed.metadata.dependencies.pip_dependencies == ["something"]
        assert parsed.metadata.dependencies.pip_dependencies_exec is None
