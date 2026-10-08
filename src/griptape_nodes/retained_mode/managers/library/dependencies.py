from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import anyio

from griptape_nodes.node_library.library_declarations import (
    LibraryDependencyDeclaration,
)
from griptape_nodes.node_library.library_registry import (
    LibraryNameAndVersion,
    LibraryRegistry,
    LibrarySchema,
)
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.library_events import (
    InstallLibraryDependenciesRequest,
    InstallLibraryDependenciesResultFailure,
    InstallLibraryDependenciesResultSuccess,
    LoadLibraryMetadataFromFileRequest,
    LoadLibraryMetadataFromFileResultFailure,
    LoadLibraryMetadataFromFileResultSuccess,
)
from griptape_nodes.retained_mode.managers.os_manager import OSManager
from griptape_nodes.retained_mode.request_handlers import handles
from griptape_nodes.utils.async_utils import subprocess_run
from griptape_nodes.utils.git_utils import (
    extract_repo_name_from_url,
    normalize_github_url,
    parse_git_url_with_ref,
)
from griptape_nodes.utils.version_utils import (
    ShadowedPackage,
    engine_package_floors,
    packages_shadowing_the_engine,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.managers.library.common import LibraryInfo

logger = logging.getLogger("griptape_nodes")


class DependencyInstallError(Exception):
    """A library's dependency set could not be installed into its environment.

    Carries the artist-facing detail as its message, so a caller turning it into a result can pass
    it straight through rather than rewording it.
    """


class ParsedDependencyUrl(NamedTuple):
    """A library-dependency declaration's URL, interpreted once for every caller."""

    repo_name: str
    normalized_url: str
    ref: str | None


class DiscoveredLibraryDependency(NamedTuple):
    """A library reached through another library's dependency declarations."""

    library_name: str
    schema: LibrarySchema


class DependencyInstallCounts(NamedTuple):
    """What a library declared against what THIS process installed, per environment.

    The two differ whenever another process owns an environment, so the message a caller sees is
    built from both rather than from the manifest alone.
    """

    declared_edit: int
    declared_exec: int
    installed_edit: int
    installed_exec: int


def parse_dependency_url(url: str) -> ParsedDependencyUrl:
    """Interpret a dependency declaration's URL, once, for every caller.

    A declaration URL may carry an `@ref` suffix and a `.git` extension, and both change the
    final path segment the repo name comes from. Call sites normalizing differently meant one
    resolved a declaration the other missed, and a miss only logs -- so a worker's target list
    quietly disagreed with the transitive resolver about the same manifest.
    """
    parsed = parse_git_url_with_ref(url)
    normalized_url = normalize_github_url(parsed.url)
    return ParsedDependencyUrl(
        repo_name=extract_repo_name_from_url(normalized_url),
        normalized_url=normalized_url,
        ref=parsed.ref,
    )


def describe_dependency_install(
    library_name: str,
    counts: DependencyInstallCounts,
    execution_failure: str | None,
) -> str:
    """Describe what THIS process installed, which is not always what the library declares.

    The orchestrator owns both environments for an execution-dependency library: it builds the
    execution venv because the worker receives that directory as PYTHONPATH and so cannot be
    the process that creates it. A worker installs neither set, so a count read off the
    manifest reported dependencies nobody here installed.
    """
    declared_edit, declared_exec, installed_edit, installed_exec = counts
    installed_total = installed_edit + installed_exec
    if installed_total == 0 and (declared_edit or declared_exec):
        return (
            f"Library '{library_name}' declares dependencies, none of which install in this "
            f"process; whichever process owns each environment installs it"
        )
    if installed_total == 0:
        return f"Library '{library_name}' has no dependencies to install"
    if installed_exec:
        return (
            f"Installed {installed_edit} edit-time and {installed_exec} execution dependencies "
            f"for library '{library_name}'"
        )
    # Reported before the no-execution-set cases below, because a failed build leaves
    # installed_exec at 0 and would otherwise read as one that had not been attempted here.
    if execution_failure is not None:
        return (
            f"Installed {installed_edit} edit-time dependencies for library '{library_name}', but {execution_failure}"
        )
    if declared_exec:
        return (
            f"Installed {installed_edit} edit-time dependencies for library "
            f"'{library_name}'; its {declared_exec} execution dependencies belong to the "
            f"execution environment the orchestrator builds"
        )
    return f"Installed {installed_edit} dependencies for library '{library_name}'"


@asynccontextmanager
async def engine_version_constraints() -> AsyncIterator[list[str]]:
    """Yield uv flags constraining an install to versions the engine can still import.

    A library environment sits ahead of the engine's own on the import path, so any package it
    resolves below the engine's version is the one engine code binds. The floors make a library
    resolve such a package newer or not at all, in place of an import error somewhere else in
    the engine that nothing connects back to this library.
    """
    with tempfile.TemporaryDirectory() as constraint_dir:
        constraint_file = Path(constraint_dir) / "engine-constraints.txt"
        await anyio.Path(constraint_file).write_text("\n".join(engine_package_floors()) + "\n")
        yield ["--constraint", str(constraint_file)]


class LibraryDependencies(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    def resolve_transitive_library_deps(
        self,
        initial: list[LibraryNameAndVersion],
    ) -> list[LibraryNameAndVersion]:
        """Expand an initial library set by following each library's library_dependencies.

        BFS walks declared library_dependencies until no new libraries are found.
        Unregistered deps are logged and skipped. Cycle-safe via a visited set.
        """
        resolved: dict[str, LibraryNameAndVersion] = {ref.library_name: ref for ref in initial}
        queue = list(initial)

        while queue:
            library_ref = queue.pop(0)
            try:
                library_data = LibraryRegistry.get_library(library_ref.library_name).get_library_data()
            except KeyError:
                logger.warning(
                    "Library '%s' not found in registry during transitive dep resolution, skipping",
                    library_ref.library_name,
                )
                continue

            if not library_data.metadata:
                continue
            lib_deps = [
                d for d in (library_data.metadata.declarations or []) if isinstance(d, LibraryDependencyDeclaration)
            ]
            if not lib_deps:
                continue

            for dep in lib_deps:
                repo_name = parse_dependency_url(dep.url).repo_name
                dep_info = self._library_info_for_repo_name(repo_name)
                if dep_info is None:
                    logger.warning(
                        "Library dependency '%s' (resolved as '%s') is not registered; skipping",
                        dep.url,
                        repo_name,
                    )
                    continue
                dep_library_name = dep_info.library_name
                if dep_library_name is None:
                    logger.warning("Library dependency '%s' has no library_name; skipping", dep.url)
                    continue
                if dep_library_name not in resolved:
                    lib_nav = LibraryNameAndVersion(
                        library_name=dep_library_name,
                        library_version=dep_info.library_version or "unknown",
                    )
                    resolved[dep_library_name] = lib_nav
                    queue.append(lib_nav)

        return list(resolved.values())

    def expand_targets_with_library_dependencies(self, target_library_names: list[str]) -> list[str]:
        """Add each target library's declared library dependencies, transitively.

        A worker is told which library it serves, but that library's declared dependencies are part
        of what it needs to run, so they load here too.
        """
        roots = list(dict.fromkeys(target_library_names))
        root_libraries = [library for library in (self._discovered_library(name) for name in roots) if library]
        dependencies = [dep.library_name for dep in self._walk_declared_library_dependencies(root_libraries)]
        return list(dict.fromkeys([*roots, *dependencies]))

    @handles(InstallLibraryDependenciesRequest)
    async def install_library_dependencies_request(self, request: InstallLibraryDependenciesRequest) -> ResultPayload:
        """Install a library's dependencies into its edit-time and execution environments.

        Edit-time dependencies go into ``.venv``, which is always created even when there is
        nothing to install, because advanced library hooks (before_library_nodes_loaded) expect
        it to exist. Execution dependencies go into ``.venv-exec``, which exists only while the
        library declares any: declaring none removes it.
        """
        library_file_path = request.library_file_path

        # Load library metadata from file
        metadata_request = LoadLibraryMetadataFromFileRequest(file_path=library_file_path)
        metadata_result = self.engine.library_manager.metadata_loading.load_library_metadata_from_file_request(
            metadata_request
        )

        if not isinstance(metadata_result, LoadLibraryMetadataFromFileResultSuccess):
            details = f"Attempted to read the library configuration at {library_file_path}. Failed due to: {metadata_result.result_details}"
            return InstallLibraryDependenciesResultFailure(result_details=details)

        library_data = metadata_result.library_schema
        library_name = library_data.name
        library_metadata = library_data.metadata

        pip_dependencies = []
        pip_dependencies_exec = []
        pip_install_flags = []
        if library_metadata.dependencies:
            pip_dependencies = library_metadata.dependencies.pip_dependencies or []
            pip_dependencies_exec = library_metadata.dependencies.pip_dependencies_exec or []
            pip_install_flags = library_metadata.dependencies.pip_install_flags or []

        # A declared dependency's execution set belongs to THIS environment, so every decision
        # below reads the combined set. A library that declares no execution dependencies of its
        # own still needs one built when something it depends on does.
        execution_dependencies = list(
            dict.fromkeys([*pip_dependencies_exec, *self._execution_dependencies_of_declared_libraries(library_data)])
        )

        # Ahead of either install and outside every gate below: a manifest that stopped declaring
        # execution dependencies must leave no execution environment behind. Which process installs
        # that set, and when, changed as this stack grew -- so keying the removal to one of those
        # gates made it unreachable in precisely the case it exists for.
        if not execution_dependencies:
            await self._retire_execution_env(library_name, library_file_path)

        owns_edit_venv = self._this_process_owns_the_edit_venv(library_file_path)
        if owns_edit_venv:
            try:
                await self._install_dependency_set(
                    library_name=library_name,
                    library_file_path=library_file_path,
                    pip_dependencies=pip_dependencies,
                    pip_install_flags=pip_install_flags,
                    execution=False,
                )
            except DependencyInstallError as e:
                return InstallLibraryDependenciesResultFailure(result_details=str(e))

        # The orchestrator builds the execution environment but never imports from it: nothing on
        # its sys.path comes from .venv-exec, so a heavy pin cannot shadow anything it has loaded.
        # It has to be the builder, because the worker receives that directory as PYTHONPATH and so
        # cannot be the process that creates it.
        #
        # A failed build costs execution and nothing else: the library keeps its node types.
        installed_exec_count = 0
        execution_failure: str | None = None
        # Gated on the execution set alone -- never the edit-time one -- so a library that needs
        # nothing heavy produces no .venv-exec at all. A declared dependency's execution pins count
        # toward it and resolve alongside this library's own: apart, uv can choose different
        # versions of anything they share, and a worker with both on sys.path binds whichever
        # landed first.
        #
        # Awaited rather than backgrounded: a spawn needs the directory to exist, so anything that
        # let startup continue would have to make an unfinished install look finished.
        if not self.engine.library_manager.is_worker and execution_dependencies:
            try:
                await self._install_dependency_set(
                    library_name=library_name,
                    library_file_path=library_file_path,
                    pip_dependencies=[*pip_dependencies, *execution_dependencies],
                    pip_install_flags=pip_install_flags,
                    execution=True,
                )
            except DependencyInstallError as e:
                # Recorded on execution_env_failure, not execution_unavailable_reason, which
                # _start_workers clears before every attempt -- the spawn refusal reads this one.
                execution_failure = f"its execution dependencies could not be installed ({e})."
                library_info = self.engine.library_manager.get_library_info_by_library_name(library_name)
                if library_info is not None:
                    library_info.execution_env_failure = execution_failure
                logger.error("Execution environment for library '%s' failed to build: %s", library_name, e)
            else:
                installed_exec_count = len(execution_dependencies)

        # Only count the edit-time set if this process actually installed it: a worker for an
        # exec-deps library skips it (the orchestrator owns that venv), so counting it here
        # reported dependencies nobody installed.
        installed_edit_count = len(pip_dependencies) if owns_edit_venv else 0
        installed_count = installed_edit_count + installed_exec_count
        details = describe_dependency_install(
            library_name,
            DependencyInstallCounts(
                declared_edit=len(pip_dependencies),
                declared_exec=len(execution_dependencies),
                installed_edit=installed_edit_count,
                installed_exec=installed_exec_count,
            ),
            execution_failure,
        )
        logger.debug(details)
        return InstallLibraryDependenciesResultSuccess(
            library_name=library_name, dependencies_installed=installed_count, result_details=details
        )

    async def shadowed_engine_packages(self, library_name: str | None, library_file_path: str) -> list[ShadowedPackage]:
        """Return the components a library holds at an older version than the engine's own.

        Both of its environments, because both precede the engine's where they are used: the
        edit-time one is spliced into this process, and the execution one reaches a worker as
        PYTHONPATH.
        """
        site_packages_paths = []
        for execution in (False, True):
            # A library whose name did not parse still has a path, and the path is what locates
            # both environments -- the name only places one for a library installed under xdg.
            venv_path = self.engine.library_manager.environment.get_library_venv_path(
                library_name or "", library_file_path, execution=execution
            )
            site_packages = Path(
                sysconfig.get_path("purelib", vars={"base": str(venv_path), "platbase": str(venv_path)})
            )
            if await anyio.Path(site_packages).exists():
                site_packages_paths.append(site_packages)

        return list(await asyncio.to_thread(packages_shadowing_the_engine, site_packages_paths))

    async def install_under_engine_floors(
        self, argv: list[str], library_venv_python_path: Path, *, capture_output: bool
    ) -> None:
        """Run a ``uv pip install`` argv under the engine's own versions as floors.

        Runs the same argv without them if that cannot resolve. A library whose dependencies
        genuinely need an older copy of something the engine also has still installs: the shadowing
        it leaves behind is reported against the library by `shadowed_engine_packages`, which an
        artist can act on, where a refused install would only have left them a library that does not
        work.

        So this raises in exactly the cases the install raised before the floors existed, which is
        what `_install_deps_with_recovery` depends on: it reads a failure here as a corrupt
        environment and deletes the venv, and a version conflict is not that.

        Known limitation: a floor uv satisfies by backtracking the library's own requirements to an
        older release exits 0, so it takes neither path and nothing reports it. Catching that costs a
        second unconstrained resolve on every install, which this trades away.

        Raises:
            subprocess.CalledProcessError: If uv exits with a non-zero status without the floors.
        """
        async with engine_version_constraints() as constraint_flags:
            try:
                await subprocess_run([*argv, *constraint_flags], check=True, capture_output=capture_output, text=True)
            except subprocess.CalledProcessError as constrained_error:
                # stderr is None when output was not captured (debug mode), where uv already printed it.
                reason = (constrained_error.stderr or "").strip()
                if not reason:
                    reason = f"the installer exited with code {constrained_error.returncode}"
                # The library report names any components this leaves older than the engine's own.
                logger.debug(
                    "Attempted to install dependencies into the environment at %s under the versions this engine runs "
                    "on. Installing without them; the result may hold components older than the engine's own. "
                    "Failed due to: %s",
                    library_venv_python_path,
                    reason,
                )
            else:
                return

        await subprocess_run(argv, check=True, capture_output=capture_output, text=True)

    def _execution_dependencies_of_declared_libraries(self, library_data: LibrarySchema) -> list[str]:
        """The execution dependencies declared by the libraries `library_data` depends on.

        These resolve into the depending library's OWN execution environment rather than each
        dependency's, for the same reason the combined install below already gives: two
        environments resolved apart can choose different versions of anything they share, and a
        worker with both on sys.path binds whichever landed first. One resolution cannot disagree
        with itself. It also keeps a worker from writing a venv that another library owns.
        """
        root = DiscoveredLibraryDependency(library_name=library_data.name, schema=library_data)
        dependencies: list[str] = []
        for dep in self._walk_declared_library_dependencies([root]):
            declared = dep.schema.metadata.dependencies
            if declared is not None:
                dependencies.extend(declared.pip_dependencies_exec or [])
        return list(dict.fromkeys(dependencies))

    def _walk_declared_library_dependencies(
        self, roots: list[DiscoveredLibraryDependency]
    ) -> Iterator[DiscoveredLibraryDependency]:
        """Yield every library reachable from `roots` through dependency declarations.

        Reads declarations from the discovered manifests rather than from LibraryRegistry, so this
        is usable before anything has loaded. Roots are not yielded, only what they depend on. A
        dependency that cannot be resolved to a discovered library is skipped and logged:
        declarations are `required: false` in practice, and a worker that refused to start because
        an optional companion library was absent would be a worse failure than the feature that
        companion powers being unavailable.

        Roots arrive with their manifest already read, so a caller holding one does not pay for it
        twice, and no manifest in the graph is read more than once.
        """
        seen = {root.library_name for root in roots}
        queue = list(roots)
        while queue:
            library = queue.pop(0)
            for dep in library.schema.metadata.declarations or []:
                if not isinstance(dep, LibraryDependencyDeclaration):
                    continue
                repo_name = parse_dependency_url(dep.url).repo_name
                dep_info = self._library_info_for_repo_name(repo_name)
                if dep_info is None or dep_info.library_name is None:
                    logger.info(
                        "Library '%s' declares a dependency on '%s', which is not installed here; "
                        "features that need it will be unavailable in this process.",
                        library.library_name,
                        repo_name,
                    )
                    continue
                if dep_info.library_name in seen:
                    continue
                seen.add(dep_info.library_name)
                discovered = self._discovered_library(dep_info.library_name)
                if discovered is None:
                    continue
                queue.append(discovered)
                yield discovered

    def _discovered_library(self, library_name: str) -> DiscoveredLibraryDependency | None:
        """`library_name` paired with its discovered manifest, or None when it cannot be read."""
        schema = self._library_schema_for_name(library_name)
        if schema is None:
            return None
        return DiscoveredLibraryDependency(library_name=library_name, schema=schema)

    def _library_schema_for_name(self, library_name: str) -> LibrarySchema | None:
        """The discovered manifest for `library_name`, or None when it cannot be read.

        A manifest that will not parse contributes no declarations, which looks identical to a
        library that declares none -- so the difference is logged rather than left silent.
        """
        info = self.engine.library_manager.get_library_info_by_library_name(library_name)
        if info is None:
            return None
        metadata_result = self.engine.library_manager.metadata_loading.load_library_metadata_from_file_request(
            LoadLibraryMetadataFromFileRequest(file_path=info.library_path)
        )
        if isinstance(metadata_result, LoadLibraryMetadataFromFileResultFailure):
            logger.warning(
                "Could not read library '%s' manifest at %s, so its declared library dependencies "
                "are not visible in this process: %s",
                library_name,
                info.library_path,
                metadata_result.result_details,
            )
            return None
        return metadata_result.library_schema

    def _library_info_for_repo_name(self, repo_name: str) -> LibraryInfo | None:
        """Resolve a library-dependency URL's repo name to a discovered library.

        A dependency declaration carries a git URL, so the only name it yields is the REPOSITORY
        name. That is not the library's name: `griptape-nodes-library-openexr` publishes itself as
        `OpenEXR Library`. Matching the repo name against library names therefore missed every
        library that does not happen to name itself after its repo -- silently, since a miss only
        warns and skips. Provisioning installs each download under a repo-name directory, so the
        path is where the repo name actually appears; the lifecycle's own dependency check already
        matches this way.
        """
        by_name = self.engine.library_manager.get_library_info_by_library_name(repo_name)
        if by_name is not None:
            return by_name
        # Both callers need library_name, and one path can hold more than one entry -- a FAILURE
        # record alongside the copy that loaded -- so a match on the failed one answers "not
        # installed here" for a library that is. Prefer an entry that actually names itself.
        matches = [
            info
            for info in self.engine.library_manager._library_file_path_to_info.values()
            if repo_name in Path(info.library_path).parts
        ]
        named = next((info for info in matches if info.library_name is not None), None)
        if named is not None:
            return named
        return matches[0] if matches else None

    def _is_one_of_my_target_libraries(self, library_name: str) -> bool:
        """Whether this worker was spawned to serve `library_name`.

        A worker is scoped to its target libraries by a filter in
        `load_all_libraries_from_config`, but nested registration paths bypass that filter --
        an unregistered library dependency reaches `download_library_request(auto_register=True)`
        and runs a full registration for a DIFFERENT library inside this worker. The orchestrator
        has no target list and legitimately loads for anyone.

        A nested dependency may still get its EDIT-time venv built here -- see
        `_this_process_owns_the_edit_venv`, which claims it only when nothing has built it yet.
        What it never gets is this worker's execution environment, which belongs to the library
        this worker was spawned for and reaches it as PYTHONPATH.
        """
        if self.engine.library_manager._target_library_names is None:
            return True
        return library_name in self.engine.library_manager._target_library_names

    def _this_process_owns_the_edit_venv(self, library_file_path: str) -> bool:
        """Whether this process may write `<library>/.venv`. Exactly one process may.

        An execution-dependency library is loaded on the ORCHESTRATOR (real node classes, not
        stubs), so the orchestrator builds that venv and keeps it on its own sys.path for the
        session. A worker for such a library must not touch it: two concurrent `uv pip install`
        runs at one target is the mild version, and the corrupt-install recovery path rmtrees
        the directory outright -- so an execution-side retry would delete the environment the
        orchestrator is importing from, which is the exact inverse of the isolation this design
        exists to provide. The worker does not need it either way: `.venv-exec` is resolved
        over BOTH dependency sets, so everything the edit-time set provides is already there.

        A legacy worker-mode library is the other case: the orchestrator skips loading it
        entirely (WORKER_DELEGATED), so nobody else creates `.venv` and the worker must.

        A worker with NO record for the library cannot tell which case it is in, and guessing
        wrong re-opens the double-writer hazard -- so it refuses, loudly.
        """
        if not self.engine.library_manager.is_worker:
            # The worker-mode case above, from the orchestrator's side: it never loads such a
            # library, so building the edit-time venv here would write a directory this process
            # has no use for and put two writers on it the moment the worker builds its own.
            library_info = self.engine.library_manager._library_file_path_to_info.get(library_file_path)
            return not (library_info is not None and library_info.requires_worker)
        library_info = self.engine.library_manager._library_file_path_to_info.get(library_file_path)
        # A library this worker was NOT spawned for arrives through a nested registration (one
        # library declaring another as a dependency), and registration continues here into module
        # imports -- which resolve against `<library>/.venv`. If nothing has built it, this
        # process must, or the import fails and takes the outer library down as UNUSABLE.
        #
        # Only when nothing has built it, though: the orchestrator runs the same nested loop and
        # may have built this venv already (and be importing from it). Claiming ownership of an
        # existing directory would put this process on `_install_deps_with_recovery`, which
        # rmtrees on a failed install -- the exact hazard this predicate exists to prevent, just
        # reached from the other side.
        if (
            library_info is not None
            and library_info.library_name is not None
            and not self._is_one_of_my_target_libraries(library_info.library_name)
        ):
            venv_path = self.engine.library_manager.environment.get_library_venv_path(
                library_info.library_name, library_file_path, execution=False
            )
            return not Path(venv_path).exists()
        if library_info is None:
            logger.warning(
                "No library record found for '%s' in this worker; skipping the edit-time "
                "environment install rather than risk writing a venv another process owns.",
                library_file_path,
            )
            return False
        return library_info.requires_worker

    async def _retire_execution_env(self, library_name: str, library_file_path: str) -> None:
        """Remove an execution environment the library's manifest no longer declares.

        The worker receives this directory as PYTHONPATH purely because it is there, so one built by
        an earlier manifest would still be imported from, carrying pins nothing declares any more.
        On-disk layout follows the manifest rather than the install history.
        """
        venv_path = self.engine.library_manager.environment.get_library_venv_path(
            library_name, library_file_path, execution=True
        )
        if not venv_path.exists():
            return
        try:
            await asyncio.to_thread(shutil.rmtree, venv_path, onexc=OSManager.remove_readonly)
        except OSError as e:
            # Leaving it costs execution correctness; raising costs editing, and this runs on the
            # registration path where that would cost the library its node types.
            logger.warning(
                "Attempted to remove the execution environment for library '%s' at %s, which its "
                "manifest no longer declares. Failed due to: %s. Its nodes stay editable, but a "
                "worker would still import from that directory.",
                library_name,
                venv_path,
                e,
            )

    async def _install_dependency_set(
        self,
        *,
        library_name: str,
        library_file_path: str,
        pip_dependencies: list[str],
        pip_install_flags: list[str],
        execution: bool,
    ) -> None:
        """Install one dependency set into its environment, creating the environment first.

        Raises:
            DependencyInstallError: With the artist-facing detail of what failed.

        The edit-time environment is created even with nothing to install, because advanced
        library hooks expect it to exist. Retiring an execution environment the manifest no longer
        declares is the caller's job, since it is the caller that decides whether to build one.
        """
        venv_kind = "execution" if execution else "edit-time"
        if execution and not pip_dependencies:
            return

        venv_path = self.engine.library_manager.environment.get_library_venv_path(
            library_name, library_file_path, execution=execution
        )

        try:
            venv_init = await self.engine.library_manager.environment.init_library_venv(venv_path)
        except RuntimeError as e:
            msg = f"Attempted to prepare the {venv_kind} environment for library '{library_name}'. Failed due to: {e}"
            raise DependencyInstallError(msg) from e
        library_venv_python_path = venv_init.python_path

        if not self.engine.library_manager.environment.can_write_to_venv_location(library_venv_python_path):
            msg = f"Attempted to set up the {venv_kind} environment for library '{library_name}' at {venv_path}. Failed due to: the location is not writable."
            raise DependencyInstallError(msg)

        config_manager = self.engine.config_manager
        min_space_gb = config_manager.get_config_value("minimum_disk_space_gb_libraries")
        if not OSManager.check_available_disk_space(Path(venv_path), min_space_gb):
            error_msg = OSManager.format_disk_space_error(Path(venv_path))
            msg = f"Attempted to install the components required by library '{library_name}'. Failed due to insufficient disk space (requires {min_space_gb} GB): {error_msg}"
            raise DependencyInstallError(msg)

        if not pip_dependencies:
            return

        logger.debug("Installing %d %s dependencies for library '%s'", len(pip_dependencies), venv_kind, library_name)
        is_debug = config_manager.get_config_value("log_level").upper() == "DEBUG"

        try:
            if venv_init.reused:
                # A reused venv may be corrupt (e.g. a dist-info directory missing its
                # METADATA file), which makes uv fail while planning the install. Rebuild it
                # once and retry against a clean environment.
                await self._install_deps_with_recovery(
                    venv_path=venv_path,
                    library_venv_python_path=library_venv_python_path,
                    pip_dependencies=pip_dependencies,
                    pip_install_flags=pip_install_flags,
                    capture_output=not is_debug,
                )
            else:
                # A freshly built venv cannot be corrupt, so an install failure is a genuine
                # problem (bad package, version conflict, network). Fail fast instead of
                # destroying and rebuilding a brand-new environment.
                await self._run_uv_pip_install(
                    library_venv_python_path, pip_dependencies, pip_install_flags, capture_output=not is_debug
                )
        except subprocess.CalledProcessError as e:
            reason = e.stderr or f"the installer exited with code {e.returncode}"
            msg = f"Attempted to install the components required by library '{library_name}'. Failed due to: {reason}"
            raise DependencyInstallError(msg) from e
        except RuntimeError as e:
            msg = f"Attempted to rebuild the {venv_kind} environment for library '{library_name}'. Failed due to: {e}"
            raise DependencyInstallError(msg) from e

    async def _install_deps_with_recovery(
        self,
        *,
        venv_path: Path,
        library_venv_python_path: Path,
        pip_dependencies: list[str],
        pip_install_flags: list[str],
        capture_output: bool,
    ) -> None:
        """Install pip dependencies into the venv, rebuilding it once on failure.

        A plain ``uv pip install`` fails hard when the reused venv is corrupt (e.g. a
        dist-info directory missing its METADATA file), because uv reads installed package
        metadata while planning the install. Retrying into the same venv would hit the same
        broken files, so on the first failure the venv is recreated from scratch and the
        install is attempted once more against the clean environment.

        Raises:
            subprocess.CalledProcessError: If the install fails again after the rebuild.
            RuntimeError: If the venv cannot be rebuilt.
        """
        try:
            await self._run_uv_pip_install(
                library_venv_python_path, pip_dependencies, pip_install_flags, capture_output=capture_output
            )
        except subprocess.CalledProcessError as first_error:
            logger.warning(
                "Dependency install into %s failed (return code=%s); rebuilding the venv and retrying once.",
                venv_path,
                first_error.returncode,
            )
        else:
            return

        library_venv_python_path = await self._reset_and_init_library_venv(venv_path)
        await self._run_uv_pip_install(
            library_venv_python_path, pip_dependencies, pip_install_flags, capture_output=capture_output
        )

    async def _reset_and_init_library_venv(self, venv_path: Path) -> Path:
        """Delete the venv (if present) and recreate it from scratch.

        Used when a reused venv cannot be trusted: an in-place dependency install failed in a
        way that indicates a corrupt environment.

        Args:
            venv_path: Path to the virtual environment directory

        Returns:
            Path to the Python executable in the freshly created virtual environment

        Raises:
            RuntimeError: If the existing venv cannot be removed or the new one cannot be created.
        """
        if await anyio.Path(venv_path).exists():
            logger.info("Rebuilding virtual environment at %s", venv_path)
            try:
                await asyncio.to_thread(shutil.rmtree, venv_path, onexc=OSManager.remove_readonly)
            except OSError as e:
                msg = f"the existing environment at {venv_path} could not be removed: {e}"
                raise RuntimeError(msg) from e
        return (await self.engine.library_manager.environment.init_library_venv(venv_path)).python_path

    async def _run_uv_pip_install(
        self,
        library_venv_python_path: Path,
        pip_dependencies: list[str],
        pip_install_flags: list[str],
        *,
        capture_output: bool,
    ) -> None:
        """Run ``uv pip install`` for the given dependencies against a venv.

        Raises:
            subprocess.CalledProcessError: If uv exits with a non-zero status without the floors.
        """
        argv = [
            sys.executable,
            "-m",
            "uv",
            "pip",
            "install",
            *pip_dependencies,
            *pip_install_flags,
            "--python",
            str(library_venv_python_path),
        ]

        await self.install_under_engine_floors(argv, library_venv_python_path, capture_output=capture_output)
