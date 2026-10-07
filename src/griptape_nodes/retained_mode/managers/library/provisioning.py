from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion
from packaging.version import Version as PackagingVersion

from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.library_events import (
    DownloadLibraryRequest,
    DownloadLibraryResultSuccess,
    LibraryProvisioningAction,
    LibraryProvisioningActionKind,
    PreviewProjectProvisioningRequest,
    PreviewProjectProvisioningResultFailure,
    PreviewProjectProvisioningResultSuccess,
)
from griptape_nodes.retained_mode.managers.library.common import LIBRARY_CONFIG_GLOB_PATTERN
from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY
from griptape_nodes.retained_mode.managers.settings import (
    LIBRARIES_DIRECTORY_KEY,
    LIBRARIES_TO_DOWNLOAD_KEY,
    REQUIRES_ENGINE_KEY,
    LibraryDownload,
)
from griptape_nodes.retained_mode.request_handlers import handles
from griptape_nodes.utils.dict_utils import get_dot_value
from griptape_nodes.utils.file_utils import find_file_in_directory, find_files_recursive
from griptape_nodes.utils.git_utils import (
    extract_repo_name_from_url,
    parse_git_url_with_ref,
)
from griptape_nodes.utils.library_utils import (
    normalize_library_downloads,
)
from griptape_nodes.utils.version_utils import (
    engine_version_failure_detail,
)

if TYPE_CHECKING:
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.managers.event_manager import EventManager

logger = logging.getLogger("griptape_nodes")


def library_version_from_manifest(manifest_path: Path | None) -> str | None:
    """Read `metadata.library_version` from a manifest path, or None when unreadable/absent."""
    if manifest_path is None:
        return None

    try:
        content = manifest_path.read_text(encoding="utf-8")
        manifest = json.loads(content)
    except (OSError, json.JSONDecodeError):
        return None

    metadata = manifest.get("metadata")
    if not isinstance(metadata, dict):
        return None
    version = metadata.get("library_version")
    return str(version) if version is not None else None


def registration_satisfied_by_installed(download: LibraryDownload, installed_version: str | None) -> bool:
    """Decide whether the installed library already satisfies the entry.

    Nothing installed is never satisfied. An entry without a version spec is
    satisfied by any installed version (source-only entry). Otherwise the
    installed version must fall within the PEP 440 specifier; a malformed
    spec or version is treated as unsatisfied so provisioning re-runs.
    """
    if installed_version is None:
        return False
    if download.version is None:
        return True
    try:
        specifier_set = SpecifierSet(download.version)
        return PackagingVersion(installed_version) in specifier_set
    except (InvalidSpecifier, InvalidVersion):
        return False


class LibraryProvisioning(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    @handles(PreviewProjectProvisioningRequest)
    async def on_preview_project_provisioning_request(
        self, request: PreviewProjectProvisioningRequest
    ) -> PreviewProjectProvisioningResultSuccess | PreviewProjectProvisioningResultFailure:
        """Compute the library provisioning plan for a loaded project, read-only.

        Sync and side-effect-free. To match what activation will actually do, it
        reconstructs the same effective config the live reconcile reads:
        ProjectManager resolves the project (canonically, like
        on_set_current_project_request) plus its workspace dir, then ConfigManager
        merges every layer (defaults -> user -> project-adjacent -> workspace ->
        override -> env) without mutating live state. Reading the project-adjacent
        file alone diverges whenever a higher-priority layer sets
        `libraries_to_download`/`engine_version`. It also runs the same
        engine_version gate (`engine_version_failure_detail`) on that merged config
        so the preview can warn before the user approves a plan that activation
        would reject. The GUI calls this before committing to a switch so the user
        can approve or refuse the changes. System defaults are previewable too:
        switching to them merges defaults -> user -> env (no project-adjacent or
        workspace-file layer), and that merged config can still carry a user-config
        library pin or engine_version, so it gets the same plan + gate. A non-loaded
        file-backed project (or one with no adjacent config dir) is a Failure.
        """
        config_mgr = self.engine.config_manager

        # System defaults is a synthetic id, not a path, so match it verbatim before
        # any canonicalization (mirroring on_set_current_project_request). Its activation
        # reads no project-adjacent or workspace-file config layer, so the preview must
        # not either, or it would drift from what the reconcile actually provisions.
        if request.project_id == SYSTEM_DEFAULTS_KEY:
            merged = config_mgr.compute_system_defaults_provisioning_config()
        else:
            # Mirror the activation gate: a project whose declared workspace_dir/libraries_dir
            # cannot be resolved will be refused by activation, so previewing a plan built from
            # fallback locations would show the user changes that can never be applied.
            project_info = self.engine.project_manager.project_info_for_request(request.project_id)
            if project_info is not None:
                unresolvable = self.engine.project_manager.unresolvable_declared_path_messages(project_info)
                if unresolvable:
                    return PreviewProjectProvisioningResultFailure(
                        result_details=f"Attempted to preview provisioning for project '{request.project_id}'. "
                        f"Failed because its declared paths cannot be resolved, so activation would refuse it. "
                        f"{' '.join(unresolvable)}",
                    )
            dirs = await self.engine.project_manager.resolve_provisioning_config_dirs(request.project_id)
            if dirs is None:
                return PreviewProjectProvisioningResultFailure(
                    result_details=f"Attempted to preview provisioning for project '{request.project_id}'. "
                    f"Failed because the project is not loaded or has no project-adjacent config directory",
                )
            merged = config_mgr.compute_project_provisioning_config(
                dirs.project_dir, dirs.workspace_dir, apply_override=dirs.apply_override
            )

        engine_version_failure = engine_version_failure_detail(get_dot_value(merged, REQUIRES_ENGINE_KEY, default=None))

        raw_libraries = get_dot_value(merged, LIBRARIES_TO_DOWNLOAD_KEY, default=[])
        downloads = normalize_library_downloads(raw_libraries)

        # Probe the installed versions against the TARGET project's libraries dir, not the
        # live one, so the plan matches what activation would reconcile in that workspace.
        # A project's own/inherited libraries_dir (resolved offline so an unloaded target is
        # honored) takes precedence; otherwise fall back to libraries_directory resolved against
        # the GLOBAL configured workspace (NOT the target's own workspace_dir), mirroring the live
        # ConfigManager.resolved_libraries_root fallback so the previewed plan cannot diverge from
        # activation for a self-contained project. libraries_directory still comes from `merged`
        # (a layer may re-point it); only the base dir is the shared global workspace.
        libraries_root = None
        if request.project_id != SYSTEM_DEFAULTS_KEY:
            libraries_root = await self.engine.project_manager.resolve_libraries_root_for_project_id(request.project_id)
        if libraries_root is not None:
            libraries_path = libraries_root
        else:
            libraries_path = config_mgr.default_libraries_root(get_dot_value(merged, LIBRARIES_DIRECTORY_KEY))

        actions = await asyncio.gather(
            *(self._plan_one_library_provisioning(download, libraries_path) for download in downloads)
        )
        destructive_count = sum(1 for action in actions if action.destructive)
        change_count = sum(1 for action in actions if action.kind != LibraryProvisioningActionKind.SKIP)
        return PreviewProjectProvisioningResultSuccess(
            actions=actions,
            engine_version_failure=engine_version_failure,
            result_details=f"Computed provisioning plan for project '{request.project_id}': "
            f"{change_count} change(s), {destructive_count} destructive",
        )

    async def reconcile_libraries_from_config(self) -> list[str]:
        """Enforce the engine_version gate and provision libraries_to_download from config.

        The project (its adjacent config, already merged into the live config by
        the time this runs) is the source of truth for the libraries it needs:
        each `libraries_to_download` entry is provisioned to match its version,
        which may overwrite a wrong installed copy. A library listed only in
        `libraries_to_register` is left to normal discovery and is never
        overwritten. The engine_version gate runs first, before any disk
        mutation, so a version mismatch blocks provisioning entirely rather than
        half-applying it.

        Returns a list of failure detail strings (empty on success). Callers
        decide whether to log-and-continue (boot) or fail (interactive reload).
        """
        engine_version_failure = self._check_engine_version()
        if engine_version_failure is not None:
            return [engine_version_failure]

        config_mgr = self.engine.config_manager
        raw_libraries = config_mgr.get_config_value(LIBRARIES_TO_DOWNLOAD_KEY, default=[])
        downloads = normalize_library_downloads(raw_libraries)

        failures: list[str] = []
        for download in downloads:
            failure = await self._provision_one_library(download)
            if failure is not None:
                failures.append(failure)

        return failures

    async def installed_manifest_path_for_download(
        self, download: LibraryDownload, libraries_path: Path
    ) -> Path | None:
        """Return the on-disk manifest path for a download entry, or None when absent.

        Locates the installed copy the same way the download handler lands it:
        by the repo-name directory `libraries_directory/<repo-name>/`. This keeps
        the provisioning version-check consistent with the clone/skip/overwrite
        logic, so a `version` pin works without requiring `name`. An explicit
        `name` overrides the directory match for a library installed under a
        differently-named directory, resolving by manifest name instead. None when
        the directory is unconfigured/missing or no manifest is found.

        `libraries_path` lets the provisioning preview probe the TARGET project's
        libraries directory; the real reconcile passes the live config's resolved
        libraries root (correct post-activation).
        """
        if download.name is not None:
            return await self._installed_library_manifest_path(download.name, libraries_path)

        repo_name = extract_repo_name_from_url(download.git_url)
        repo_directory = libraries_path / repo_name
        return find_file_in_directory(repo_directory, LIBRARY_CONFIG_GLOB_PATTERN)

    async def ensure_libraries_from_config(self) -> None:
        """Ensure libraries from git URLs specified in config are downloaded.

        This method:
        1. Reads libraries_to_download from config
        2. Downloads any missing libraries concurrently
        3. Logs summary of successful/failed operations

        Supports URL format with @ref suffix (e.g., "https://github.com/user/repo@stable").
        Libraries are registered later by load_all_libraries_from_config().
        """
        config_mgr = self.engine.config_manager
        raw_downloads = config_mgr.get_config_value(LIBRARIES_TO_DOWNLOAD_KEY, default=[])
        git_urls = [download.git_url for download in normalize_library_downloads(raw_downloads)]

        if not git_urls:
            logger.debug("No libraries to download from config")
            return

        logger.debug("Starting download of %d libraries from config", len(git_urls))

        # Use shared download method
        results = await self.download_libraries_from_git_urls(git_urls)

        # Count successes, skipped, and failures
        successful = sum(1 for r in results.values() if r["success"])
        skipped = sum(1 for r in results.values() if r.get("skipped"))
        failed = len(results) - successful - skipped

        if failed:
            logger.info(
                "Completed automatic library downloads: %d successful, %d skipped, %d failed",
                successful,
                skipped,
                failed,
            )
        else:
            logger.debug(
                "Completed automatic library downloads: %d successful, %d skipped",
                successful,
                skipped,
            )

    async def download_libraries_from_git_urls(
        self,
        git_urls_with_refs: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Download multiple libraries from git URLs concurrently.

        Args:
            git_urls_with_refs: List of git URLs with optional @ref suffix (e.g., "url@v1.0")

        Returns:
            Dictionary mapping git_url_with_ref to result info:
            {
                "url@ref": {
                    "success": bool,
                    "library_name": str | None,
                    "error": str | None,
                    "skipped": bool (optional, True if already exists),
                }
            }
        """
        config_mgr = self.engine.config_manager
        libraries_path = config_mgr.resolved_libraries_root()

        async def download_one(git_url_with_ref: str) -> tuple[str, dict[str, Any]]:
            """Download a single library if not already present."""
            # Parse URL to extract git URL and optional ref
            git_url, ref = parse_git_url_with_ref(git_url_with_ref)
            target_directory_name = extract_repo_name_from_url(git_url)
            target_path = libraries_path / target_directory_name

            # Skip if already exists
            if target_path.exists():
                logger.debug("Library at '%s' already exists, skipping", target_path)
                return git_url_with_ref, {
                    "success": False,
                    "library_name": None,
                    "error": None,
                    "skipped": True,
                }

            logger.info("Downloading library from '%s'", git_url_with_ref)
            download_result = await self.engine.ahandle_request(
                DownloadLibraryRequest(
                    git_url=git_url,
                    branch_tag_commit=ref,
                    fail_on_exists=False,
                    auto_register=False,
                )
            )

            if isinstance(download_result, DownloadLibraryResultSuccess):
                logger.info("Downloaded library '%s'", download_result.library_name)
                return git_url_with_ref, {
                    "success": True,
                    "library_name": download_result.library_name,
                    "error": None,
                }

            error = str(download_result.result_details)
            logger.warning("Failed to download '%s': %s", git_url_with_ref, error)
            return git_url_with_ref, {
                "success": False,
                "library_name": None,
                "error": error,
            }

        # Download all concurrently
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(download_one(url)) for url in git_urls_with_refs]

        # Collect results
        return dict(task.result() for task in tasks)

    def _check_engine_version(self) -> str | None:
        """Return a failure detail when the running engine fails the configured spec.

        Reads the merged `requires_engine` config key and delegates the PEP 440
        compare to `engine_version_failure_detail`. No key means no constraint.
        """
        spec_string = self.engine.config_manager.get_config_value(REQUIRES_ENGINE_KEY, default=None)
        return engine_version_failure_detail(spec_string)

    async def _plan_one_library_provisioning(
        self, download: LibraryDownload, libraries_path: Path | None = None
    ) -> LibraryProvisioningAction:
        """Decide what provisioning will do to one download entry, reading only.

        A manifest read for the installed version (under `libraries_directory`)
        plus a PEP 440 compare. The preview lists this output and the real
        provisioning path re-runs it at execution time; both read the same
        on-disk state, which survives the reload's registry unload, so the two
        cannot drift. Branch order mirrors `_provision_one_library`: the
        already-satisfied SKIP first, then the git clone/overwrite.

        `libraries_path` is the TARGET project's resolved libraries directory.
        The preview passes it so the installed-version probe reads the workspace
        the switch would land in, not the currently-active one; activation runs
        after the config layers switch, so its reconcile leaves this None and
        resolves from this engine's (now-target) live config.

        `destructive` is True ONLY for a git OVERWRITE, matching the
        `overwrite_existing = installed_version is not None` decision in
        `_provision_git_library` that triggers the local directory delete.

        A LibraryDownload always carries a `git_url`; `name` is optional. When
        `name` is absent the installed copy is located by its repo-name directory
        (`installed_manifest_path_for_download`), the same place the download
        handler lands it, so a `version` pin is enforced without requiring `name`.
        The action's `library_name` falls back to the repo name for display.
        """
        if libraries_path is None:
            libraries_path = self.engine.config_manager.resolved_libraries_root()

        library_name = download.name if download.name is not None else extract_repo_name_from_url(download.git_url)
        installed_version = await self._installed_download_version(download, libraries_path)
        parsed = parse_git_url_with_ref(download.git_url)
        satisfied = registration_satisfied_by_installed(download, installed_version)
        if satisfied:
            return LibraryProvisioningAction(
                library_name=library_name,
                kind=LibraryProvisioningActionKind.SKIP,
                installed_version=installed_version,
                pinned_version=download.version,
                git_url=parsed.url,
                git_ref=parsed.ref,
                destructive=False,
                reason=f"Installed version {installed_version} already satisfies the entry",
            )

        if installed_version is None:
            kind = LibraryProvisioningActionKind.INSTALL
            reason = f"Not installed; will clone from {download.git_url}"
        else:
            kind = LibraryProvisioningActionKind.OVERWRITE
            reason = (
                f"Installed version {installed_version} does not satisfy the entry; "
                f"will delete the local library directory and re-clone from {download.git_url}"
            )
        return LibraryProvisioningAction(
            library_name=library_name,
            kind=kind,
            installed_version=installed_version,
            pinned_version=download.version,
            git_url=parsed.url,
            git_ref=parsed.ref,
            destructive=kind == LibraryProvisioningActionKind.OVERWRITE,
            reason=reason,
        )

    async def _provision_one_library(self, download: LibraryDownload) -> str | None:
        """Provision a single git-sourced library, skipping when already satisfied.

        Computes the plan with the same pure decision function the preview uses
        (`_plan_one_library_provisioning`), then clones/overwrites via the
        download handler, which recomputes its own `overwrite_existing` so the
        wire payload is unchanged. Returns a failure detail string, or None on
        success/skip. A LibraryDownload always carries a `git_url`.
        """
        action = await self._plan_one_library_provisioning(download)
        if action.kind == LibraryProvisioningActionKind.SKIP:
            return None

        return await self._provision_git_library(
            download, git_url=download.git_url, installed_version=action.installed_version
        )

    async def _provision_git_library(
        self, download: LibraryDownload, *, git_url: str, installed_version: str | None
    ) -> str | None:
        """Download a git-sourced entry, overwriting only a wrong installed version.

        The download handler skips its clone when the target directory exists,
        which would silently keep a stale checkout. So overwrite_existing is set
        only when a wrong version is already installed; a fresh install lets the
        handler land the library normally.

        When overwriting, the destructive delete must target the directory the
        installed library actually lives in. The handler's default guess is
        `libraries_path/<git-repo-name>`, which is correct for any library this
        engine downloaded. An explicit `name` overrides that for a library
        installed under a differently-named directory: the installed manifest is
        resolved by `name` and its parent directory is passed so the delete hits
        the stale dir and the re-clone does not orphan the old copy. Without a
        `name` (the common case) both hints stay None and the handler's repo-name
        default applies. A fresh install (installed_version is None) also leaves
        both hints None.
        """
        parsed = parse_git_url_with_ref(git_url)
        overwrite_existing = installed_version is not None

        download_directory: str | None = None
        target_directory_name: str | None = None
        if overwrite_existing and download.name is not None:
            manifest_path = await self._installed_library_manifest_path(
                download.name, self.engine.config_manager.resolved_libraries_root()
            )
            if manifest_path is not None:
                download_directory = str(manifest_path.parent.parent)
                target_directory_name = manifest_path.parent.name

        download_request = DownloadLibraryRequest(
            git_url=parsed.url,
            branch_tag_commit=parsed.ref,
            auto_register=False,
            overwrite_existing=overwrite_existing,
            fail_on_exists=False,
            download_directory=download_directory,
            target_directory_name=target_directory_name,
        )
        download_result = await self.engine.ahandle_request(download_request)
        if not isinstance(download_result, DownloadLibraryResultSuccess):
            library_label = download.name if download.name is not None else extract_repo_name_from_url(git_url)
            return f"Failed to provision library '{library_label}' from '{git_url}': {download_result.result_details}"
        return None

    async def _installed_library_manifest_path(self, library_name: str, libraries_path: Path) -> Path | None:
        """Return the on-disk manifest path for a provisioned library by name, or None.

        Scans the manifests under `libraries_directory` (where reconcile clones
        git-sourced libraries) rather than the in-memory `LibraryRegistry`,
        because the reload path
        unregisters every library before reconcile runs (see
        `reload_libraries_request`). Returns the first manifest whose `name`
        matches; None when the directory is unconfigured or missing, or no
        manifest matches.

        `libraries_path` lets the provisioning preview probe the TARGET project's
        libraries directory rather than the live one; the real reconcile passes
        the live config's resolved libraries root (it runs after activation has
        switched the config layers to the target).

        This is the single source of truth for the provisioning planner
        (`_installed_library_version`, which decides SKIP/INSTALL/OVERWRITE) and
        the overwrite path (`_provision_git_library`, which deletes the manifest's
        directory before re-cloning), guaranteeing the file the planner reasoned
        about is exactly the file overwrite targets.
        """
        for manifest_path in await find_files_recursive(
            libraries_path,
            LIBRARY_CONFIG_GLOB_PATTERN,
            max_depth=self.engine.config_manager.discovery_max_depth,
        ):
            try:
                content = manifest_path.read_text(encoding="utf-8")
                manifest = json.loads(content)
            except (OSError, json.JSONDecodeError):
                continue
            if manifest.get("name") == library_name:
                return manifest_path

        return None

    async def _installed_library_version(self, library_name: str, libraries_path: Path) -> str | None:
        """Return the on-disk version of a library by manifest name, or None when absent.

        Locates the provisioned manifest via `_installed_library_manifest_path`
        (the shared resolver), then reads `metadata.library_version`. None when no
        manifest matches or the version is absent/unreadable.
        """
        manifest_path = await self._installed_library_manifest_path(library_name, libraries_path)
        return library_version_from_manifest(manifest_path)

    async def _installed_download_version(self, download: LibraryDownload, libraries_path: Path) -> str | None:
        """Return the on-disk version for a download entry, or None when absent.

        Resolves the installed manifest via `installed_manifest_path_for_download`
        (repo-name directory, or `name` override), then reads
        `metadata.library_version`. `libraries_path` threads the TARGET project's
        libraries directory through for the preview, or the live config's
        resolved root for the real reconcile.
        """
        manifest_path = await self.installed_manifest_path_for_download(download, libraries_path)
        return library_version_from_manifest(manifest_path)
