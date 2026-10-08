from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import anyio

from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.resource_events import (
    GetResourceInstanceStatusRequest,
    GetResourceInstanceStatusResultSuccess,
    ListCompatibleResourceInstancesRequest,
    ListCompatibleResourceInstancesResultSuccess,
)
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import (
    DependencyInstallationFailedProblem,
    IncompatibleRequirementsProblem,
)
from griptape_nodes.retained_mode.managers.os_manager import OSManager
from griptape_nodes.utils.async_utils import subprocess_run
from griptape_nodes.utils.engine_dirs import engine_data_dir
from griptape_nodes.utils.uv_utils import find_uv_bin, is_venv_functional, venv_python_path

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine

logger = logging.getLogger("griptape_nodes")


class LibraryVenvInitResult(NamedTuple):
    """Result of initializing a library virtual environment."""

    python_path: Path
    reused: bool


def describe_unmet_requirements(problem: IncompatibleRequirementsProblem) -> str:
    """Phrase an unmet resource requirement for whoever has to act on it.

    The raw dicts read like internals ("{'compute': (['cuda'], 'has_any')}"), so the
    capability and what this machine actually reports are named plainly instead.
    """

    def render(value: Any) -> str:
        # Capability values arrive as enums and nested tuples; their reprs
        # ("<ComputeBackend.MPS: 'mps'>") have no business in a message an artist reads.
        if isinstance(value, (list, tuple)):
            return ", ".join(render(item) for item in value)
        return str(getattr(value, "value", value))

    wanted = ", ".join(
        f"{capability} {render(value[0] if isinstance(value, (list, tuple)) else value)}"
        for capability, value in problem.requirements.items()
    )
    have = (
        ", ".join(f"{key} {render(value)}" for key, value in problem.system_capabilities.items())
        or "nothing detectable"
    )
    return f"it needs {wanted}, and this machine has {have}."


class LibraryEnvironment(EngineScoped):
    def __init__(self, engine: Engine | None = None) -> None:
        super().__init__(engine)

    def execution_env_failure_reason(self, library_name: str) -> str | None:
        """Why this library's execution environment cannot be used, or None when it can.

        Read by the spawn path: a failed build records its reason and leaves the venv directory
        behind, so directory existence alone says nothing. Spawning a
        worker whose PYTHONPATH fronts a partial or stale site-packages would trade the recorded
        uv error for a raw ModuleNotFoundError deep inside library load.
        """
        library_info = self.engine.library_manager.get_library_info_by_library_name(library_name)
        if library_info is None:
            return f"library '{library_name}' is not registered here."
        if library_info.execution_env_failure is not None:
            return library_info.execution_env_failure
        # The edit-time install failing is just as disqualifying: the exec build resolves both
        # sets together, but registration stops before reaching it when the edit set fails, leaving
        # only this marker behind.
        if any(isinstance(problem, DependencyInstallationFailedProblem) for problem in library_info.problems):
            return library_info.execution_unavailable_reason or (
                f"the execution environment build for library '{library_name}' failed; details are in the engine log."
            )
        return None

    def execution_site_packages(self, library_name: str) -> str | None:
        """The library's execution site-packages directory, if it has been built.

        Handed to a worker as PYTHONPATH at spawn so the library's dependency versions are on
        sys.path BEFORE the process imports anything. Splicing the same directory later cannot
        achieve this: a module already in sys.modules is never reconsidered, and a package that
        probed for an optional dependency at import time has already cached the answer -- which is
        how a library that ships `safetensors` still hit `NameError: name 'safetensors' is not
        defined` from inside huggingface_hub.

        Returns None when the directory does not exist. A failed build leaves it behind, so
        callers must consult `execution_env_failure_reason` before treating a path as usable.
        """
        library_info = self.engine.library_manager.get_library_info_by_library_name(library_name)
        if library_info is None:
            return None
        venv_path = self.get_library_venv_path(library_name, library_info.library_path, execution=True)
        if not venv_path.exists():
            return None
        return sysconfig.get_path("purelib", vars={"base": str(venv_path), "platbase": str(venv_path)})

    async def init_library_venv(self, library_venv_path: Path) -> LibraryVenvInitResult:
        """Initialize a virtual environment for the library.

        If a functional virtual environment already exists at the path, it is reused.
        If a directory exists at the path but is not a functional venv (e.g. missing
        ``pyvenv.cfg`` or Python executable, or referencing a Python interpreter that
        has since been removed), it is deleted and recreated.

        Args:
            library_venv_path: Path to the virtual environment directory

        Returns:
            The Python executable path and whether an existing functional venv was reused.

        Raises:
            RuntimeError: If the virtual environment cannot be created.
        """
        python_version = platform.python_version()

        if is_venv_functional(library_venv_path):
            logger.debug("Reusing existing virtual environment at %s", library_venv_path)
            return LibraryVenvInitResult(python_path=venv_python_path(library_venv_path), reused=True)

        if await anyio.Path(library_venv_path).exists():
            logger.warning(
                "Existing path at %s is not a functional virtual environment; recreating it", library_venv_path
            )
            try:
                await asyncio.to_thread(shutil.rmtree, library_venv_path, onexc=OSManager.remove_readonly)
            except OSError as e:
                msg = f"Failed to remove broken virtual environment at {library_venv_path}: {e}"
                raise RuntimeError(msg) from e

        # Check disk space before creating virtual environment
        config_manager = self.engine.config_manager
        min_space_gb = config_manager.get_config_value("minimum_disk_space_gb_libraries")
        if not OSManager.check_available_disk_space(library_venv_path.parent, min_space_gb):
            error_msg = OSManager.format_disk_space_error(library_venv_path.parent)
            error_message = f"Disk space error creating virtual environment (requires {min_space_gb} GB): {error_msg}"
            raise RuntimeError(error_message)

        try:
            uv_path = find_uv_bin()
            logger.info("Creating virtual environment at %s with Python %s", library_venv_path, python_version)
            is_debug = config_manager.get_config_value("log_level").upper() == "DEBUG"
            await subprocess_run(
                [uv_path, "venv", str(library_venv_path), "--python", python_version],
                check=True,
                capture_output=not is_debug,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            msg = f"Failed to create virtual environment at {library_venv_path} with Python {python_version}: return code={e.returncode}, stdout={e.stdout}, stderr={e.stderr}"
            raise RuntimeError(msg) from e
        logger.debug("Created virtual environment at %s", library_venv_path)

        return LibraryVenvInitResult(python_path=venv_python_path(library_venv_path), reused=False)

    def check_library_requirements(
        self, requirements: dict[str, Any], library_name: str
    ) -> IncompatibleRequirementsProblem | None:
        """Check if the current system meets the library's resource requirements.

        Args:
            requirements: Dictionary of requirements in the format used by resource_instance.Requirements
            library_name: Name of the library being checked (for logging)

        Returns:
            IncompatibleRequirementsProblem if requirements are not met, None if they are met
        """
        logger.debug("Checking requirements for library '%s': %s", library_name, requirements)

        os_keys = {"platform", "arch", "version"}
        compute_keys = {"compute"}

        os_requirements = {k: v for k, v in requirements.items() if k in os_keys}
        compute_requirements = {k: v for k, v in requirements.items() if k in compute_keys}

        if os_requirements:
            list_request = ListCompatibleResourceInstancesRequest(
                resource_type_name="OSResourceType",
                requirements=os_requirements,
                include_locked=True,
            )
            result = self.engine.handle_request(list_request)

            if isinstance(result, ListCompatibleResourceInstancesResultSuccess) and not result.instance_ids:
                system_capabilities = self._get_system_capabilities()
                logger.warning(
                    "Library '%s' required OS resources not met. Wanted: %s, System: %s",
                    library_name,
                    os_requirements,
                    system_capabilities,
                )
                return IncompatibleRequirementsProblem(
                    requirements=requirements,
                    system_capabilities=system_capabilities,
                )

        if compute_requirements:
            list_request = ListCompatibleResourceInstancesRequest(
                resource_type_name="ComputeResourceType",
                requirements=compute_requirements,
                include_locked=True,
            )
            result = self.engine.handle_request(list_request)

            if isinstance(result, ListCompatibleResourceInstancesResultSuccess) and not result.instance_ids:
                system_capabilities = self._get_system_capabilities()
                logger.warning(
                    "Library '%s' required compute resources not met. Wanted: %s, System: %s",
                    library_name,
                    compute_requirements,
                    system_capabilities,
                )
                return IncompatibleRequirementsProblem(
                    requirements=requirements,
                    system_capabilities=system_capabilities,
                )

        return None

    def get_library_venv_path(
        self, library_name: str, library_file_path: str | None = None, *, execution: bool = False
    ) -> Path:
        """Get the path to a virtual environment directory for a library.

        A library has up to two environments. The edit-time environment (``.venv``) holds the
        dependencies needed to import and instantiate nodes, and is the one a developer's
        ``uv sync`` already produces, so it is deliberately left at the conventional name. The
        execution environment (``.venv-exec``) holds the heavy dependencies only ``process``
        needs and is put on ``sys.path`` solely where nodes execute.

        Args:
            library_name: Name of the library
            library_file_path: Optional path to the library JSON file
            execution: Whether to return the execution environment instead of the edit-time one

        Returns:
            Path to the virtual environment directory
        """
        venv_dir_name = ".venv-exec" if execution else ".venv"
        clean_library_name = library_name.replace(" ", "_").strip()

        if library_file_path is not None:
            # Create venv relative to the library.json file
            library_dir = Path(library_file_path).parent.absolute()
            return library_dir / venv_dir_name

        # Create venv relative to the engine data directory
        return engine_data_dir() / "libraries" / clean_library_name / venv_dir_name

    async def add_library_paths_to_sys_path(self, library_name: str, library_file_path: str, base_dir: Path) -> None:
        """Add a library's directory and edit-time venv site-packages to sys.path.

        The edit-time environment is added in every process, because importing node modules and
        instantiating nodes needs it. The execution environment is never added here, in either
        process -- a worker receives it as PYTHONPATH at spawn, before it imports anything. See the
        body for why splicing it into a running interpreter would not work.

        Where both exist, the execution environment must keep precedence: it is the one resolved
        over both dependency sets, so it holds the only versions of a shared package that one
        resolver agreed on. PYTHONPATH sits at `sys.path[1]`, which any `insert(0, ...)` would
        overtake, so `_add_library_edit_venv_to_sys_path` declines rather than ordering around it.

        Args:
            library_name: Name of the library (for venv lookup)
            library_file_path: Path to the library JSON file (for venv lookup)
            base_dir: Library base directory to add for relative imports
        """
        sys.path.insert(0, str(base_dir))

        await self._add_library_edit_venv_to_sys_path(library_name, library_file_path)

    def can_write_to_venv_location(self, venv_python_path: Path) -> bool:
        """Check if we can write to the venv location (either create it or modify existing).

        Args:
            venv_python_path: Path to the python executable in the virtual environment

        Returns:
            True if we can write to the location, False otherwise
        """
        # On Windows, permission checks are hard. Assume we can write
        if OSManager.is_windows():
            return True

        venv_path = venv_python_path.parent.parent

        # If venv doesn't exist, check if parent directory is writable
        if not venv_path.exists():
            parent_dir = venv_path.parent
            try:
                return parent_dir.exists() and os.access(parent_dir, os.W_OK)
            except (OSError, AttributeError) as e:
                logger.debug("Could not check parent directory permissions for %s: %s", parent_dir, e)
                return False

        # If venv exists, check if we can write to it
        try:
            return os.access(venv_path, os.W_OK)
        except (OSError, AttributeError) as e:
            logger.debug("Could not check venv write permissions for %s: %s", venv_path, e)
            return False

    def _get_system_capabilities(self) -> dict[str, Any]:
        """Get the current system's capabilities for error reporting.

        Returns:
            Dictionary of combined OS and compute capabilities or empty dict if unavailable
        """
        capabilities: dict[str, Any] = {}

        os_list_request = ListCompatibleResourceInstancesRequest(
            resource_type_name="OSResourceType",
            requirements=None,
            include_locked=True,
        )
        os_result = self.engine.handle_request(os_list_request)

        if isinstance(os_result, ListCompatibleResourceInstancesResultSuccess) and os_result.instance_ids:
            status_request = GetResourceInstanceStatusRequest(instance_id=os_result.instance_ids[0])
            status_result = self.engine.handle_request(status_request)
            if isinstance(status_result, GetResourceInstanceStatusResultSuccess):
                capabilities.update(status_result.status.capabilities)

        compute_list_request = ListCompatibleResourceInstancesRequest(
            resource_type_name="ComputeResourceType",
            requirements=None,
            include_locked=True,
        )
        compute_result = self.engine.handle_request(compute_list_request)

        if isinstance(compute_result, ListCompatibleResourceInstancesResultSuccess) and compute_result.instance_ids:
            status_request = GetResourceInstanceStatusRequest(instance_id=compute_result.instance_ids[0])
            status_result = self.engine.handle_request(status_request)
            if isinstance(status_result, GetResourceInstanceStatusResultSuccess):
                capabilities.update(status_result.status.capabilities)

        return capabilities

    def _execution_env_is_already_on_sys_path(self, library_name: str) -> bool:
        """Whether this library's execution site-packages is on `sys.path` already.

        True in the worker this library was spawned for, where the engine passed that directory as
        PYTHONPATH. Compared as resolved paths because the value on `sys.path` came from the
        environment and need not be spelled the way `execution_site_packages` spells it.
        """
        execution_site_packages = self.execution_site_packages(library_name)
        if execution_site_packages is None:
            return False
        target = Path(execution_site_packages).resolve()
        return any(Path(entry).resolve() == target for entry in sys.path if entry)

    async def _add_library_edit_venv_to_sys_path(self, library_name: str, library_file_path: str) -> None:
        """Add a library's EDIT-time venv site-packages to sys.path, if it exists.

        Only the edit-time environment is ever spliced. The execution environment reaches a worker
        as PYTHONPATH at spawn, because adding it to a running interpreter cannot give the library
        its own versions of anything already imported.

        Skipped entirely when PYTHONPATH already carries this library's execution environment. That
        environment is resolved over BOTH dependency sets, so it provides everything the edit-time
        set does, at the versions one resolver chose -- and splicing lands at `sys.path[0]`, ahead
        of PYTHONPATH, so a package present in both would otherwise bind the edit-time version that
        the combined resolver rejected. A library the worker was not spawned for is unaffected: its
        execution directory is not on the path, so its edit-time venv is still spliced.
        """
        if self._execution_env_is_already_on_sys_path(library_name):
            return

        venv_path = self.get_library_venv_path(library_name, library_file_path, execution=False)
        if not await anyio.Path(venv_path).exists():
            return

        site_packages = str(
            Path(
                sysconfig.get_path(
                    "purelib",
                    vars={"base": str(venv_path), "platbase": str(venv_path)},
                )
            )
        )
        sys.path.insert(0, site_packages)
        logger.debug("Added library '%s' edit-time venv to sys.path: %s", library_name, site_packages)
