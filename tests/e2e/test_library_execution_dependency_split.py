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
from griptape_nodes.retained_mode.engine import current_engine
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
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
    original_is_worker = current_engine().library_manager._is_worker
    for dep in (EDIT_DEP, EXEC_DEP):
        sys.modules.pop(dep, None)
    yield
    sys.path[:] = original_path
    current_engine().library_manager._is_worker = original_is_worker
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
    result = current_engine().handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(result, RegisterLibraryFromFileResultSuccess), getattr(result, "result_details", result)
    return result


def _library_info(library_json: Path) -> LibraryManager.LibraryInfo:
    return current_engine().library_manager._library_file_path_to_info[str(library_json)]


def _site_packages(venv_path: Path) -> str:
    return str(Path(sysconfig.get_path("purelib", vars={"base": str(venv_path), "platbase": str(venv_path)})))


def _import_fresh(module_name: str) -> object:
    """Import a module without letting a previous test's cache answer for it."""
    sys.modules.pop(module_name, None)
    importlib.invalidate_caches()
    return importlib.import_module(module_name)


class TestOrchestratorHoldsEditTimeDepsOnly:
    def test_the_orchestrator_does_not_install_execution_dependencies_at_all(self, tmp_path: Path) -> None:
        """The orchestrator builds the edit-time environment and stops there.

        It never imports the execution set -- only a worker splices .venv-exec onto sys.path --
        so installing it here bought nothing and cost everything the split exists to save: the
        whole weight of every heavy library, downloaded and stored by the process that merely
        draws the nodes. Half a gigabyte for a single torch pin, measured.

        The library still loads, with real node classes and GOOD fitness, because defining and
        editing a node needs the edit-time set alone. That is the same reason a broken
        execution dependency must not stop a library from loading.
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

        edit_venv = library_dir / ".venv"
        exec_venv = library_dir / ".venv-exec"
        assert edit_venv.exists()
        # The worker that runs the nodes builds this one, in its own process.
        assert not exec_venv.exists()

        # And the heavy dependency is unreachable from here either way.
        assert _site_packages(edit_venv) in sys.path
        assert _site_packages(exec_venv) not in sys.path
        assert _import_fresh(EDIT_DEP).__version__ == EDIT_DEP_VERSION  # type: ignore[attr-defined]
        with pytest.raises(ImportError):
            _import_fresh(EXEC_DEP)

    def test_the_execution_environment_also_holds_the_edit_time_set(self, tmp_path: Path) -> None:
        """One resolution over both sets, so a shared package cannot end up at two versions.

        The two venvs are isolated, and .venv-exec is spliced AHEAD of .venv in a worker. Resolved
        separately, anything present in both (numpy as an edit dep, numpy pulled in by torch) could
        differ, and the execution copy would win -- so a node module would bind one version when
        the orchestrator built the node and another when the worker ran it.

        Asserted on disk rather than by import: .venv is on sys.path too, so an import proves
        nothing about which environment satisfied it.
        """
        wheel_dir = tmp_path / "wheels"
        _build_both_wheels(wheel_dir)
        library_dir = tmp_path / "library"
        library_json = _materialize_library(
            library_dir, wheel_dir=wheel_dir, name="Exec Dep Union", exec_dependencies=[EXEC_DEP]
        )
        current_engine().library_manager._is_worker = True

        _register(library_json)

        exec_site_packages = Path(_site_packages(library_dir / ".venv-exec"))
        assert (exec_site_packages / EXEC_DEP).exists(), "execution dependency missing from .venv-exec"
        assert (exec_site_packages / EDIT_DEP).exists(), (
            "edit-time dependency missing from .venv-exec: the two environments were resolved "
            "separately, so a shared package can differ between them"
        )

    def test_an_unresolvable_execution_dependency_still_leaves_the_library_editable(self, tmp_path: Path) -> None:
        """A broken execution dependency must cost execution and nothing else.

        Before, it failed registration outright: no node types, and placeholder nodes reading
        "Library not found" in every workflow that used the library -- even though the
        orchestrator needs nothing but the edit-time set to define and edit those nodes. An
        artist who could not run a node also could not open their workflow.
        """
        wheel_dir = tmp_path / "wheels"
        _build_both_wheels(wheel_dir)
        library_dir = tmp_path / "library"
        library_json = _materialize_library(
            library_dir,
            wheel_dir=wheel_dir,
            name="Exec Dep Unresolvable",
            exec_dependencies=["gtn-nonexistent-exec-dependency-2f9a1c"],
        )

        _register(library_json)

        info = _library_info(library_json)
        assert info.lifecycle_state is LibraryManager.LibraryLifecycleState.LOADED
        # The node class is real and instantiable, which is what editing needs.
        node = LibraryRegistry.create_node(
            node_type="ExecDepNode", name="unresolvable_dep_node", specific_library_name="Exec Dep Unresolvable"
        )
        assert node is not None

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
    def test_a_worker_can_import_both_dependency_sets(self, tmp_path: Path) -> None:
        """What a worker needs is both sets importable -- not two directories on sys.path.

        Asserted as a capability because the ownership changed: `<library>/.venv` belongs to the
        ORCHESTRATOR, which loads exec-dependency libraries itself and keeps that venv on its own
        sys.path for the session. A worker building it too gave one directory two writers, and the
        corrupt-install recovery path rmtrees it -- so an execution-side retry could delete the
        environment the orchestrator was importing from.

        The worker instead gets everything from `.venv-exec`, which is resolved over both sets.
        """
        wheel_dir = tmp_path / "wheels"
        _build_both_wheels(wheel_dir)
        library_dir = tmp_path / "library"
        library_json = _materialize_library(
            library_dir, wheel_dir=wheel_dir, name="Exec Dep Worker", exec_dependencies=[EXEC_DEP]
        )
        current_engine().library_manager._is_worker = True

        _register(library_json)

        assert _site_packages(library_dir / ".venv-exec") in sys.path
        assert _import_fresh(EDIT_DEP).__version__ == EDIT_DEP_VERSION  # type: ignore[attr-defined]
        assert _import_fresh(EXEC_DEP).__version__ == EXEC_DEP_VERSION  # type: ignore[attr-defined]

    def test_a_worker_does_not_build_the_orchestrators_edit_venv(self, tmp_path: Path) -> None:
        """One writer per directory. With two, an execution-side retry deletes an edit-time env."""
        wheel_dir = tmp_path / "wheels"
        _build_both_wheels(wheel_dir)
        library_dir = tmp_path / "library"
        library_json = _materialize_library(
            library_dir, wheel_dir=wheel_dir, name="Exec Dep Single Writer", exec_dependencies=[EXEC_DEP]
        )
        current_engine().library_manager._is_worker = True

        _register(library_json)

        assert not (library_dir / ".venv").exists()

    def test_a_worker_serves_its_own_library_when_scoped_to_it(self, tmp_path: Path) -> None:
        """The gate on target libraries must not lock a worker out of the library it exists for.

        The other tests here leave `_target_library_names` unset, which short-circuits that gate
        to True -- so they traverse it without exercising it, and a regression that skipped the
        worker's OWN execution environment would pass them all and surface as an ImportError at
        node execution. This one sets the scope a real spawn sets.
        """
        wheel_dir = tmp_path / "wheels"
        _build_both_wheels(wheel_dir)
        library_dir = tmp_path / "library"
        library_json = _materialize_library(
            library_dir, wheel_dir=wheel_dir, name="Exec Dep Scoped", exec_dependencies=[EXEC_DEP]
        )
        library_manager = current_engine().library_manager
        library_manager._is_worker = True
        library_manager._target_library_names = ["Exec Dep Scoped"]

        _register(library_json)

        assert (library_dir / ".venv-exec").exists()
        assert _site_packages(library_dir / ".venv-exec") in sys.path
        assert _import_fresh(EXEC_DEP).__version__ == EXEC_DEP_VERSION  # type: ignore[attr-defined]

    def test_a_worker_builds_no_execution_environment_for_a_library_it_does_not_serve(self, tmp_path: Path) -> None:
        """A nested registration must not put another library's heavy pins on this sys.path.

        One library declaring another as a dependency registers it inside whichever process is
        loading -- so without this gate a worker built and spliced a second library's `.venv-exec`
        at position 0 of its own sys.path, which is the shadowing the edit/execution split exists
        to end. The edit-time venv IS still built here, because nobody else will: the orchestrator
        never loaded this library.
        """
        wheel_dir = tmp_path / "wheels"
        _build_both_wheels(wheel_dir)
        library_dir = tmp_path / "library"
        library_json = _materialize_library(
            library_dir, wheel_dir=wheel_dir, name="Exec Dep Foreign", exec_dependencies=[EXEC_DEP]
        )
        library_manager = current_engine().library_manager
        library_manager._is_worker = True
        library_manager._target_library_names = ["Some Other Library"]

        _register(library_json)

        assert not (library_dir / ".venv-exec").exists()
        assert _site_packages(library_dir / ".venv-exec") not in sys.path
        assert (library_dir / ".venv").exists(), "the edit-time venv has no other owner here"

    def test_process_runs_in_a_worker(self, tmp_path: Path) -> None:
        """The payoff: the same node whose process is unrunnable on the orchestrator runs here."""
        wheel_dir = tmp_path / "wheels"
        _build_both_wheels(wheel_dir)
        library_dir = tmp_path / "library"
        library_json = _materialize_library(
            library_dir, wheel_dir=wheel_dir, name="Exec Dep Worker Process", exec_dependencies=[EXEC_DEP]
        )
        current_engine().library_manager._is_worker = True
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
