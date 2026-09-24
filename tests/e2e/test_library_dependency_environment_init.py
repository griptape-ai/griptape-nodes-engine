"""End-to-end tests for library dependency-environment initialization.

The fixture library (`fixtures/worker_dep_library`) reproduces the shape that broke when
the engine dropped pygit2 (engine PR #5265): an advanced module that imports a third-party
package declared only in the manifest's ``pip_dependencies``. Real libraries with this
shape include Depth Anything 3 and every diffusers-style model library.

The contract these tests pin, across the ways a library arrives:

- **Added to a running orchestrator, worker-delegated**: registration succeeds WITHOUT the
  dependency being importable in the orchestrator process. The orchestrator must not
  import the advanced module at all -- it deliberately never creates the library venv, so
  the import could only fail (and before the skip landed, that failure hard-killed the
  registration).
- **Initial install on a worker**: the worker creates the library venv, installs the
  manifest deps into it, puts its site-packages on ``sys.path``, and only then imports the
  advanced module -- so the module-scope third-party import resolves.
- **Cold engine boot with the venv already on disk**: a fresh engine re-registers the
  library against the persisted venv and the advanced module loads again.
- **In-process (non-worker) library on the orchestrator**: same install-before-import
  ordering holds in the default mode.

The third-party dependency is a hand-built wheel (``fakegit``) installed from a local
``--find-links`` directory with ``--no-index``, so the tests are fully offline. Because
``fakegit`` exists nowhere except that wheel, a successful import IS proof the library
venv was populated and wired onto ``sys.path``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.node_library.library_registry import LibraryRegistry, LibrarySchema
from griptape_nodes.retained_mode.engine import current_engine, reset_root_engine
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.utils.version_utils import engine_version

if TYPE_CHECKING:
    from collections.abc import Iterator

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "worker_dep_library"
FIXTURE_FILES = ("worker_dep_library_advanced.py", "dep_node.py")
DEP_NAME = "fakegit"
DEP_VERSION = "1.0.0"


@pytest.fixture(autouse=True)
def _forget_dep_module() -> Iterator[None]:
    """Drop the fixture dependency from sys.modules around each test.

    A successful import in one test would otherwise satisfy the next test's advanced
    module from the module cache, hiding a broken venv wiring.
    """
    sys.modules.pop(DEP_NAME, None)
    yield
    sys.modules.pop(DEP_NAME, None)


def _build_dep_wheel(wheel_dir: Path) -> None:
    """Hand-build a minimal ``fakegit`` wheel so installs need no network or build backend."""
    wheel_dir.mkdir(parents=True, exist_ok=True)
    wheel_path = wheel_dir / f"{DEP_NAME}-{DEP_VERSION}-py3-none-any.whl"
    dist_info = f"{DEP_NAME}-{DEP_VERSION}.dist-info"
    files = {
        f"{DEP_NAME}/__init__.py": f'__version__ = "{DEP_VERSION}"\n',
        f"{dist_info}/METADATA": f"Metadata-Version: 2.1\nName: {DEP_NAME}\nVersion: {DEP_VERSION}\n",
        f"{dist_info}/WHEEL": "Wheel-Version: 1.0\nGenerator: test-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record_rows = []
    with zipfile.ZipFile(wheel_path, "w") as zf:
        for name, content in files.items():
            data = content.encode()
            zf.writestr(name, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            record_rows.append(f"{name},sha256={digest},{len(data)}")
        record_rows.append(f"{dist_info}/RECORD,,")
        zf.writestr(f"{dist_info}/RECORD", "\n".join(record_rows) + "\n")


def _materialize_dep_library(target_dir: Path, *, wheel_dir: Path, name: str, worker_mode: bool) -> Path:
    """Write a registrable copy of the fixture library into ``target_dir``.

    Injects the offline install flags and, for ``worker_mode``, the manifest declaration
    that makes the library worker-delegated -- the same mechanism a real library uses.
    """
    schema = json.loads((FIXTURE_DIR / "griptape_nodes_library.json").read_text())
    schema["name"] = name
    schema["library_schema_version"] = LibrarySchema.LATEST_SCHEMA_VERSION
    schema["metadata"]["engine_version"] = engine_version
    schema["metadata"]["dependencies"] = {
        "pip_dependencies": [DEP_NAME],
        "pip_install_flags": ["--no-index", "--find-links", str(wheel_dir)],
    }
    if worker_mode:
        schema["metadata"]["declarations"] = [{"type": "suggested_worker_mode", "mode": "WORKER"}]
    target_dir.mkdir(parents=True, exist_ok=True)
    library_json = target_dir / "griptape_nodes_library.json"
    library_json.write_text(json.dumps(schema, indent=2))
    for file_name in FIXTURE_FILES:
        (target_dir / file_name).write_text((FIXTURE_DIR / file_name).read_text())
    return library_json


def _register(library_json: Path) -> RegisterLibraryFromFileResultSuccess:
    # Re-resolved on each call, rather than threaded from a fixture: the cold-boot test below
    # calls `reset_root_engine()` mid-test, and a fixture-captured Engine would go stale then.
    result = current_engine().handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(result, RegisterLibraryFromFileResultSuccess), getattr(result, "result_details", result)
    return result


def _library_info(library_json: Path) -> LibraryManager.LibraryInfo:
    return current_engine().library_manager._library_file_path_to_info[str(library_json)]


def _hooks_seen(library_name: str) -> list[str]:
    advanced = LibraryRegistry.get_library(library_name).get_advanced_library()
    assert advanced is not None
    return sys.modules[type(advanced).__module__].HOOKS_SEEN


class TestWorkerDelegatedOnOrchestrator:
    def test_adding_to_a_running_orchestrator_defers_the_dependency_environment(self, tmp_path: Path) -> None:
        """The orchestrator registers a worker-delegated library without its deps.

        Regression for the pygit2 breakage: before the advanced-module skip, this
        registration hard-failed with ModuleNotFoundError because the orchestrator
        imported the advanced module without ever creating the library venv.
        """
        wheel_dir = tmp_path / "wheels"
        _build_dep_wheel(wheel_dir)
        library_dir = tmp_path / "library"
        library_json = _materialize_dep_library(
            library_dir, wheel_dir=wheel_dir, name="Worker Dep Library Orchestrator", worker_mode=True
        )

        _register(library_json)

        info = _library_info(library_json)
        assert info.requires_worker is True
        assert info.lifecycle_state is LibraryManager.LibraryLifecycleState.WORKER_PENDING
        # The dependency environment is the worker's job: no venv, no dep import here.
        assert not (library_dir / ".venv").exists()
        assert DEP_NAME not in sys.modules
        # The library still registered (editor/workflow loading see it), with no advanced instance.
        library = LibraryRegistry.get_library("Worker Dep Library Orchestrator")
        assert library.get_advanced_library() is None


class TestWorkerEnvironmentInitialization:
    def test_initial_install_on_a_worker_populates_the_library_env(self, tmp_path: Path) -> None:
        """The worker's first registration creates the venv, installs deps, then imports."""
        wheel_dir = tmp_path / "wheels"
        _build_dep_wheel(wheel_dir)
        library_dir = tmp_path / "library"
        library_json = _materialize_dep_library(
            library_dir, wheel_dir=wheel_dir, name="Worker Dep Library Worker", worker_mode=True
        )
        current_engine().library_manager._is_worker = True

        _register(library_json)

        info = _library_info(library_json)
        assert info.lifecycle_state is LibraryManager.LibraryLifecycleState.LOADED
        assert info.fitness is LibraryManager.LibraryFitness.GOOD
        assert (library_dir / ".venv").exists()
        # fakegit exists nowhere but the fixture wheel, so a successful import proves the
        # venv was populated and its site-packages wired onto sys.path before the
        # advanced module loaded.
        assert DEP_NAME in sys.modules
        assert _hooks_seen("Worker Dep Library Worker") == [f"before:{DEP_VERSION}", "after"]

    def test_cold_engine_boot_reloads_against_the_persisted_venv(self, tmp_path: Path) -> None:
        """A fresh engine (new process boot, simulated) re-registers with the venv on disk."""
        wheel_dir = tmp_path / "wheels"
        _build_dep_wheel(wheel_dir)
        library_dir = tmp_path / "library"
        library_json = _materialize_dep_library(
            library_dir, wheel_dir=wheel_dir, name="Worker Dep Library Reboot", worker_mode=True
        )
        current_engine().library_manager._is_worker = True
        _register(library_json)
        advanced = LibraryRegistry.get_library("Worker Dep Library Reboot").get_advanced_library()
        assert advanced is not None
        advanced_module_name = type(advanced).__module__

        # Cold boot: fresh engine and registries, empty module cache, venv persisted on disk.
        reset_root_engine()
        LibraryRegistry._clear()
        sys.modules.pop(DEP_NAME, None)
        sys.modules.pop(advanced_module_name, None)
        current_engine().library_manager._is_worker = True

        _register(library_json)

        info = _library_info(library_json)
        assert info.lifecycle_state is LibraryManager.LibraryLifecycleState.LOADED
        assert info.fitness is LibraryManager.LibraryFitness.GOOD
        assert DEP_NAME in sys.modules


class TestInProcessLibraryOnOrchestrator:
    def test_dependencies_install_before_the_advanced_module_imports(self, tmp_path: Path) -> None:
        """Default (non-worker) mode: the same install-before-import ordering holds."""
        wheel_dir = tmp_path / "wheels"
        _build_dep_wheel(wheel_dir)
        library_dir = tmp_path / "library"
        library_json = _materialize_dep_library(
            library_dir, wheel_dir=wheel_dir, name="Worker Dep Library InProcess", worker_mode=False
        )

        _register(library_json)

        info = _library_info(library_json)
        assert info.requires_worker is False
        assert info.lifecycle_state is LibraryManager.LibraryLifecycleState.LOADED
        assert info.fitness is LibraryManager.LibraryFitness.GOOD
        assert (library_dir / ".venv").exists()
        assert DEP_NAME in sys.modules
        assert _hooks_seen("Worker Dep Library InProcess") == [f"before:{DEP_VERSION}", "after"]
