"""Environment mode: the libraries the environment provides, and the rules that hold while it does.

When `library.provisioned_by` is 'environment', the environment the engine was started in decides
which libraries load (`GTN_LIBRARY_PATHS`) and holds their packages. The engine then loads nothing
else and downloads, updates, and builds nothing. The request handlers refuse those changes with an
artist-readable message; `ensure_engine_provisions` backs that up at the shared helpers that do the
work, so a handler that forgets its own check still cannot download, build, or install.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from griptape_nodes.files.path_utils import canonicalize_for_identity
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.library_events import (
    LoadLibraryMetadataFromFileRequest,
    LoadLibraryMetadataFromFileResultSuccess,
)
from griptape_nodes.retained_mode.managers.external_environment import (
    provisioned_by_environment,
    sandbox_enabled,
)
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import LibraryNotProvidedByEnvironmentProblem
from griptape_nodes.retained_mode.managers.library.common import (
    LibraryFitness,
    LibraryInfo,
    LibraryLifecycleState,
)
from griptape_nodes.retained_mode.managers.library.sandbox import SANDBOX_LIBRARY_NAME
from griptape_nodes.retained_mode.managers.settings import LIBRARY_SANDBOX_ENABLED_KEY

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.managers.library.discovery import DiscoveredLibraryEntry


class LibrariesProvidedByEnvironmentError(RuntimeError):
    """Raised by a shared download, build, or install helper when the environment provides the libraries."""


class LibraryManagedEnvironment(EngineScoped):
    def __init__(self, engine: Engine | None = None) -> None:
        super().__init__(engine)
        # Canonical manifest paths (canonicalize_for_identity) of the libraries GTN_LIBRARY_PATHS
        # provides, from the last discovery; None until one has run (see is_provided_by_environment).
        self.environment_library_paths: set[str] | None = None

    def provisioned_by_environment(self) -> bool:
        """Whether library.provisioned_by is 'environment'."""
        return provisioned_by_environment(self.engine.config_manager)

    def sandbox_enabled(self) -> bool:
        """Whether the sandbox library loads (library.sandbox_enabled, or the mode's default)."""
        return sandbox_enabled(self.engine.config_manager)

    def sandbox_off_message(self, attempted: str) -> str:
        """Why a sandbox action was refused, for the mode the engine runs in."""
        if (
            self.provisioned_by_environment()
            and self.engine.config_manager.get_config_value(LIBRARY_SANDBOX_ENABLED_KEY, default=None) is None
        ):
            return (
                f"Attempted to {attempted}. Failed because the engine is running in an environment that provides "
                f"its libraries, and this environment does not include a sandbox library. Set "
                f"{LIBRARY_SANDBOX_ENABLED_KEY} to true to allow one."
            )
        return (
            f"Attempted to {attempted}. Failed because the sandbox library is turned off "
            f"({LIBRARY_SANDBOX_ENABLED_KEY} is false)."
        )

    async def is_allowed_in_environment(self, library_info: LibraryInfo) -> bool:
        """Whether `library_info` may load when the environment provides the libraries.

        A library the environment lists always may; the sandbox library may when it is enabled.
        """
        if await self.is_provided_by_environment(library_info.library_path):
            return True
        return library_info.is_sandbox and self.sandbox_enabled()

    def ensure_engine_provisions(self, attempted: str) -> None:
        """Refuse a download, build, or install when the environment provides the libraries.

        Called at the top of the shared helpers that do that work, so the rule holds for every
        caller, including one added later that has no environment-mode check of its own.

        Raises:
            LibrariesProvidedByEnvironmentError: The environment provides the libraries.
        """
        if self.provisioned_by_environment():
            raise LibrariesProvidedByEnvironmentError(self.environment_provides_libraries_message(attempted))

    async def is_provided_by_environment(self, library_path: str) -> bool:
        """Whether `library_path` is one of the manifests GTN_LIBRARY_PATHS provides.

        Paths are compared by canonical identity, so relative and symlinked spellings of a listed
        manifest match. A library registered by path before any discovery has run has nothing to
        compare against yet, so the environment's list is discovered first.
        """
        if self.environment_library_paths is None:
            entries = await self.engine.library_manager.discovery.discover_library_files()
            self.environment_library_paths = self.environment_paths_from(entries)
        return str(canonicalize_for_identity(library_path)) in self.environment_library_paths

    def is_known_environment_library(self, library_path: str) -> bool:
        """`is_provided_by_environment` against the last discovery, for callers that cannot await."""
        if self.environment_library_paths is None:
            return False
        return str(canonicalize_for_identity(library_path)) in self.environment_library_paths

    def create_not_provided_library_info_entry(
        self,
        file_path_str: str,
        *,
        is_sandbox: bool,
        enabled: bool,
        registered_path: str | None,
    ) -> None:
        """Record a configured library that is not loaded because the environment does not provide it.

        Used when library.provisioned_by is 'environment'. The entry is replaced on every
        discovery, because the same path may have loaded before the setting changed. A disabled
        entry stays disabled without a problem: it was not going to load either way. The manifest
        is read only for the library's name and version, so the problem names what the artist knows.
        """
        library_name = None
        library_version = None
        if is_sandbox:
            library_name = SANDBOX_LIBRARY_NAME
        else:
            metadata_result = self.engine.library_manager.metadata_loading.load_library_metadata_from_file_request(
                LoadLibraryMetadataFromFileRequest(file_path=file_path_str)
            )
            if isinstance(metadata_result, LoadLibraryMetadataFromFileResultSuccess):
                library_name = metadata_result.library_schema.name
                library_version = metadata_result.library_schema.metadata.library_version

        library_info = LibraryInfo(
            lifecycle_state=LibraryLifecycleState.DISABLED,
            fitness=LibraryFitness.NOT_EVALUATED,
            library_path=file_path_str,
            is_sandbox=is_sandbox,
            enabled=enabled,
            library_name=library_name,
            library_version=library_version,
            registered_path=registered_path,
        )
        if enabled:
            self.mark_not_provided_by_environment(library_info)
        self.engine.library_manager._library_file_path_to_info[file_path_str] = library_info

    @staticmethod
    def mark_not_provided_by_environment(library_info: LibraryInfo) -> None:
        """Fail `library_info` because the environment does not provide it, reporting that once."""
        library_info.lifecycle_state = LibraryLifecycleState.FAILURE
        library_info.fitness = LibraryFitness.UNUSABLE
        if not any(isinstance(problem, LibraryNotProvidedByEnvironmentProblem) for problem in library_info.problems):
            library_info.problems.append(LibraryNotProvidedByEnvironmentProblem(library_path=library_info.library_path))

    @staticmethod
    def is_not_provided_by_environment(library_info: LibraryInfo) -> bool:
        return any(isinstance(problem, LibraryNotProvidedByEnvironmentProblem) for problem in library_info.problems)

    @staticmethod
    def environment_provides_libraries_message(attempted: str) -> str:
        """The failure for a library change the environment, not the engine, is responsible for."""
        return (
            f"Attempted to {attempted}. Failed because the engine is running in an environment that "
            f"provides its libraries, so libraries are added and updated by whoever set up that environment."
        )

    @classmethod
    def library_path_names_repo(cls, library_path: str, repo_name: str, *, environment_mode: bool) -> bool:
        """Whether a folder in `library_path` is named after the repository `repo_name`.

        Provisioning clones a download into a folder with the repository's exact name. An
        environment lays its libraries out itself, and a tool that normalizes package names spells
        `griptape-nodes-library-openexr` as `griptape_nodes_library_openexr`, so when the
        environment provides the libraries the comparison also ignores letter case and treats `-`
        and `_` alike.
        """
        parts = Path(library_path).parts
        if repo_name in parts:
            return True
        if not environment_mode:
            return False
        normalized_repo_name = cls.normalize_repo_name(repo_name)
        return any(cls.normalize_repo_name(part) == normalized_repo_name for part in parts)

    @staticmethod
    def normalize_repo_name(name: str) -> str:
        return name.lower().replace("-", "_")

    @staticmethod
    def environment_paths_from(entries: list[DiscoveredLibraryEntry]) -> set[str]:
        return {str(canonicalize_for_identity(entry.registration.path)) for entry in entries if entry.from_environment}
