from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import anyio
from semver import Version

from griptape_nodes.node_library.library_registry import (
    LibraryRegistry,
    LibrarySchema,
)
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.app_events import (
    GetEngineVersionRequest,
    GetEngineVersionResultSuccess,
)
from griptape_nodes.retained_mode.events.base_events import ResultPayloadFailure
from griptape_nodes.retained_mode.events.library_events import (
    CheckLibraryUpdateRequest,
    CheckLibraryUpdateResultFailure,
    CheckLibraryUpdateResultSuccess,
    DownloadLibraryRequest,
    DownloadLibraryResultFailure,
    DownloadLibraryResultSuccess,
    InspectLibraryRepoRequest,
    InspectLibraryRepoResultFailure,
    InspectLibraryRepoResultSuccess,
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
    SwitchLibraryRefRequest,
    SwitchLibraryRefResultFailure,
    SwitchLibraryRefResultSuccess,
    UnloadLibraryFromRegistryRequest,
    UpdateLibraryRequest,
    UpdateLibraryResultFailure,
    UpdateLibraryResultSuccess,
)
from griptape_nodes.retained_mode.events.os_events import (
    DeleteFileRequest,
    DeleteFileResultFailure,
)
from griptape_nodes.retained_mode.managers.library.common import LibraryFitness, LibraryInfo, LibraryLifecycleState
from griptape_nodes.retained_mode.managers.settings import (
    LIBRARIES_TO_REGISTER_KEY,
    LIBRARY_MINIMUM_RELEASE_AGE_KEY,
)
from griptape_nodes.retained_mode.request_handlers import handles
from griptape_nodes.utils.file_utils import find_file_in_directory
from griptape_nodes.utils.git_utils import (
    GitError,
    clone_repository,
    get_current_ref,
    get_git_info,
    get_git_remote,
    get_local_commit_sha,
    is_on_tag,
    normalize_github_url,
    parse_git_url_with_ref,
    remote_ref_exists,
    sparse_checkout_library_json,
    switch_branch_or_tag,
    update_library_git,
)
from griptape_nodes.utils.library_utils import (
    clone_and_get_library_version,
    extract_library_path,
    is_monorepo,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager

logger = logging.getLogger("griptape_nodes")


class LibraryGitOperationContext(NamedTuple):
    """Context information for git operations on a library."""

    old_version: str
    library_file_path: str
    library_dir: Path


class LibraryUpdateInfo(NamedTuple):
    """Information about a library pending update."""

    library_name: str
    old_version: str
    new_version: str


class LibraryUpdateResult(NamedTuple):
    """Result of updating a single library."""

    library_name: str
    old_version: str
    new_version: str
    result: ResultPayload


class UpdateAgeGateDecision(NamedTuple):
    """Outcome of evaluating the library update age gate against a target commit.

    ``gated`` is True only when the gate is enabled and the target commit is younger than the
    configured minimum release age. ``age_hours`` is the target commit's age at evaluation time, or
    None when the commit timestamp could not be determined.
    """

    enabled: bool
    gated: bool
    age_hours: float | None
    minimum_release_age_hours: float


class MinimumReleaseAgeConfig(NamedTuple):
    """The minimum-release-age setting, read once so callers avoid duplicate config lookups.

    ``hours`` is the configured minimum release age in hours; 0 (or negative) disables the gate.
    ``enabled`` is derived so call sites can branch on intent without re-deriving it.
    """

    hours: float

    @property
    def enabled(self) -> bool:
        return self.hours > 0


class LibraryGitOperations(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    @handles(CheckLibraryUpdateRequest)
    async def check_library_update_request(self, request: CheckLibraryUpdateRequest) -> ResultPayload:  # noqa: C901, PLR0911, PLR0912, PLR0915
        """Check if a library has updates available via git."""
        library_name = request.library_name

        # A reload unregisters every library before re-registering them one at a time, so a
        # check landing in that window would report a perfectly healthy library as
        # unregistered. Wait for the rebuild to finish first.
        await self.engine.library_manager._libraries_loading_complete.wait()

        # Check if the library exists
        try:
            library = LibraryRegistry.get_library(name=library_name)
        except KeyError:
            details = f"Attempted to check for updates for Library '{library_name}'. Failed because no Library with that name was registered."
            return CheckLibraryUpdateResultFailure(result_details=details)

        # Find the library file path. Route through the shared resolver so the update path
        # (_validate_and_prepare_library_for_git_operation) and this check path can never
        # disagree about which on-disk copy a duplicately-registered library maps to.
        library_info = self.engine.library_manager.get_library_info_by_library_name(library_name)
        if library_info is None:
            details = f"Attempted to check for updates for Library '{library_name}'. Failed because no file path could be found for this library."
            return CheckLibraryUpdateResultFailure(result_details=details)
        library_file_path = library_info.library_path

        # Get the library directory (parent of the JSON file)
        library_dir = Path(library_file_path).parent.absolute()

        # Check if library is in a monorepo (multiple libraries in same git repository)
        try:
            in_monorepo = await asyncio.to_thread(is_monorepo, library_dir)
        except GitError as e:
            details = f"Attempted to check for updates for Library '{library_name}'. Failed because the repository layout could not be determined: {e}"
            return CheckLibraryUpdateResultFailure(result_details=details)

        if in_monorepo:
            details = (
                f"Library '{library_name}' is in a monorepo with multiple libraries. Updates must be managed manually."
            )
            logger.info(details)
            # Get git info for the response. Informational only here, so a git failure must not
            # turn the successful "monorepo, update manually" answer into a failure.
            git_remote, git_ref = await asyncio.to_thread(get_git_info, library_dir)
            current_version = library.get_metadata().library_version
            return CheckLibraryUpdateResultSuccess(
                has_update=False,
                current_version=current_version,
                latest_version=current_version,
                git_remote=git_remote,
                git_ref=git_ref,
                local_commit=None,
                remote_commit=None,
                result_details=details,
            )

        # Check if the library directory is a git repository and get remote URL and ref
        try:
            git_remote = await asyncio.to_thread(get_git_remote, library_dir)
            if git_remote is None:
                details = f"Library '{library_name}' is not a git repository or has no remote configured."
                return CheckLibraryUpdateResultFailure(result_details=details)
        except GitError as e:
            details = f"Failed to get git remote for Library '{library_name}': {e}"
            return CheckLibraryUpdateResultFailure(result_details=details)

        try:
            git_ref = await asyncio.to_thread(get_current_ref, library_dir)
        except GitError as e:
            details = f"Failed to get current git reference for Library '{library_name}': {e}"
            return CheckLibraryUpdateResultFailure(result_details=details)

        # Get current library version
        current_version = library.get_metadata().library_version
        if current_version is None:
            details = f"Library '{library_name}' has no version information."
            return CheckLibraryUpdateResultFailure(result_details=details)

        # Get local commit SHA
        try:
            local_commit = await asyncio.to_thread(get_local_commit_sha, library_dir)
        except GitError as e:
            details = f"Failed to read the local commit for Library '{library_name}': {e}"
            return CheckLibraryUpdateResultFailure(result_details=details)

        # If the current ref does not exist on the remote (e.g. a local-only branch that has
        # not been pushed, or a detached HEAD on a bare commit), there is nothing on the remote
        # to compare against. Report no update available instead of failing the check.
        if git_ref is not None:
            try:
                ref_on_remote = await asyncio.to_thread(remote_ref_exists, git_remote, git_ref)
            except GitError as e:
                details = f"Failed to query git remote for Library '{library_name}': {e}"
                return CheckLibraryUpdateResultFailure(result_details=details)

            if not ref_on_remote:
                details = (
                    f"Library '{library_name}' is on git ref '{git_ref}', which does not exist on remote "
                    f"'{git_remote}'. Updates can only be checked against refs that exist on the remote."
                )
                logger.info(details)
                return CheckLibraryUpdateResultSuccess(
                    has_update=False,
                    current_version=current_version,
                    latest_version=current_version,
                    git_remote=git_remote,
                    git_ref=git_ref,
                    local_commit=local_commit,
                    remote_commit=None,
                    result_details=details,
                )

        # Clone remote and get latest version and commit SHA (using current ref or HEAD if detached)
        try:
            ref_to_check = git_ref or "HEAD"
            version_info = await asyncio.to_thread(clone_and_get_library_version, git_remote, ref_to_check)
            latest_version = version_info.library_version
            remote_commit = version_info.commit_sha
        except GitError as e:
            details = f"Failed to retrieve latest version from git remote for Library '{library_name}': {e}"
            return CheckLibraryUpdateResultFailure(result_details=details)

        # Determine if update is available using version comparison and commit comparison
        try:
            current_ver = Version.parse(current_version)
            latest_ver = Version.parse(latest_version)

            # Update detection logic:
            # 1. If remote version > local version -> update available (semantic versioning)
            if latest_ver > current_ver:
                has_update = True
                update_reason = "version increased"
            # 2. If remote version < local version -> no update (prevent regression)
            elif latest_ver < current_ver:
                has_update = False
                update_reason = "version decreased (regression blocked)"
            # 3. If versions equal -> check commits
            elif local_commit is not None and remote_commit is not None and local_commit != remote_commit:
                has_update = True
                update_reason = "commits differ (same version)"
            else:
                has_update = False
                update_reason = "versions and commits match"

        except ValueError as e:
            details = f"Failed to parse version strings for Library '{library_name}': {e}"
            return CheckLibraryUpdateResultFailure(result_details=details)

        # Check engine version compatibility
        library_required_engine_version = version_info.engine_version
        is_compatible, current_engine_version = self._check_engine_version_compatibility(
            library_required_engine_version
        )

        if not is_compatible:
            details = (
                f"Cannot update Library '{library_name}'. "
                f"The update requires engine version {library_required_engine_version} "
                f"but the current engine version is {current_engine_version}. "
                f"Please update your engine first."
            )
            return CheckLibraryUpdateResultFailure(result_details=details)

        # Evaluate the age gate only when an update actually exists, so callers can surface a
        # "pending age gate" state. Skipping the evaluation when up to date avoids a spurious
        # "timestamp could not be determined" warning (there is simply nothing to gate) and the
        # cost of the decision on the common no-update path.
        if has_update:
            age_gate = self._evaluate_update_age_gate(version_info.commit_datetime)
            update_gated_by_age = age_gate.gated
            target_commit_age_hours = age_gate.age_hours
            minimum_release_age_hours = age_gate.minimum_release_age_hours if age_gate.enabled else None
        else:
            update_gated_by_age = False
            target_commit_age_hours = None
            minimum_release_age_hours = None

        if update_gated_by_age:
            details = (
                f"Update available for Library '{library_name}' ({current_version} -> {latest_version}), but the "
                f"target commit is {target_commit_age_hours:.1f}h old, younger than the required "
                f"{minimum_release_age_hours:.1f}h minimum release age. Update will be available once the target commit ages."
            )
            logger.info(details)
        else:
            details = f"Successfully checked for updates for Library '{library_name}'. Current version: {current_version}, Latest version: {latest_version}, Has update: {has_update} ({update_reason})"
            logger.info(details)

        return CheckLibraryUpdateResultSuccess(
            has_update=has_update,
            current_version=current_version,
            latest_version=latest_version,
            git_remote=git_remote,
            git_ref=git_ref,
            local_commit=local_commit,
            remote_commit=remote_commit,
            update_gated_by_age=update_gated_by_age,
            target_commit_age_hours=target_commit_age_hours,
            minimum_release_age_hours=minimum_release_age_hours,
            result_details=details,
        )

    @handles(UpdateLibraryRequest)
    async def update_library_request(self, request: UpdateLibraryRequest) -> ResultPayload:  # noqa: C901, PLR0911
        """Update a library to the latest version using the appropriate git strategy.

        Automatically detects whether the library uses branch-based or tag-based workflow:
        - Branch-based: Uses git fetch + git reset --hard (forces local to match remote)
        - Tag-based: Uses git fetch --tags --force + git checkout
        """
        library_name = request.library_name

        # Validate library and prepare for git operation
        validation_result = await self._validate_and_prepare_library_for_git_operation(
            library_name=library_name,
            failure_result_class=UpdateLibraryResultFailure,
            operation_description="update",
        )
        if isinstance(validation_result, ResultPayloadFailure):
            return validation_result

        old_version = validation_result.old_version
        library_file_path = validation_result.library_file_path
        library_dir = validation_result.library_dir

        # Check if library is in a monorepo (multiple libraries in same git repository)
        try:
            in_monorepo = await asyncio.to_thread(is_monorepo, library_dir)
        except GitError as e:
            details = f"Cannot update Library '{library_name}'. Failed because the repository layout could not be determined: {e}"
            return UpdateLibraryResultFailure(result_details=details)

        if in_monorepo:
            details = f"Cannot update Library '{library_name}'. Repository contains multiple libraries and must be updated manually."
            return UpdateLibraryResultFailure(result_details=details)

        # Enforce the update age gate before mutating the working tree. Only pay the
        # remote round-trip when gating is actually enabled, so the common (disabled) path is free.
        minimum_release_age_config = self._read_minimum_release_age_config()
        if minimum_release_age_config.enabled:
            target_commit_datetime = await self._get_remote_target_commit_datetime(library_dir)
            age_gate = self._evaluate_update_age_gate(target_commit_datetime, config=minimum_release_age_config)
            if age_gate.gated:
                details = (
                    f"Cannot update Library '{library_name}' yet: the target commit is "
                    f"{age_gate.age_hours:.1f}h old, younger than the required {age_gate.minimum_release_age_hours:.1f}h "
                    f"minimum release age (library.minimum_release_age). Try again once the target commit ages."
                )
                logger.info(details)
                return UpdateLibraryResultFailure(result_details=details, age_gated=True)

        # Perform git update (auto-detects branch vs tag workflow)
        try:
            await asyncio.to_thread(
                update_library_git,
                library_dir,
                overwrite_existing=request.overwrite_existing,
            )
        except GitError as e:
            error_msg = str(e).lower()

            # Check if error is retryable (uncommitted changes)
            retryable = "uncommitted changes" in error_msg or "unstaged changes" in error_msg

            details = f"Failed to update Library '{library_name}': {e}"
            return UpdateLibraryResultFailure(
                result_details=details,
                retryable=retryable,
                existing_path=str(library_dir) if retryable else None,
            )

        # After a moving-tag update, the local HEAD should match the commit the remote tag
        # resolves to. If it does not, the next update check will report "update available"
        # forever (the loop from issue #5039). Surface it loudly rather than looping silently.
        # This is diagnostic only and never fails the update. Restricted to tag-based (detached
        # HEAD) libraries: a branch update does `git reset --hard` to remote HEAD, so it always
        # converges and would only pay for an extra remote clone with nothing to report.
        try:
            if await asyncio.to_thread(is_on_tag, library_dir):
                local_commit = await asyncio.to_thread(get_local_commit_sha, library_dir)
                git_remote = await asyncio.to_thread(get_git_remote, library_dir)
                git_ref = await asyncio.to_thread(get_current_ref, library_dir)
                if local_commit is not None and git_remote is not None:
                    version_info = await asyncio.to_thread(clone_and_get_library_version, git_remote, git_ref or "HEAD")
                    if local_commit != version_info.commit_sha:
                        logger.warning(
                            "After updating Library '%s' on ref '%s', local commit %s does not match "
                            "remote commit %s. Update checks may keep reporting an available update.",
                            library_name,
                            git_ref,
                            local_commit,
                            version_info.commit_sha,
                        )
        except GitError as e:
            logger.debug("Skipped post-update commit verification for Library '%s': %s", library_name, e)

        # Reload library
        reload_result = await self._reload_library_after_git_operation(
            library_name=library_name,
            library_file_path=library_file_path,
            failure_result_class=UpdateLibraryResultFailure,
        )
        if isinstance(reload_result, ResultPayloadFailure):
            return reload_result

        new_version = reload_result

        return self._build_library_update_result(
            library_name=library_name, old_version=old_version, new_version=new_version
        )

    @handles(SwitchLibraryRefRequest)
    async def switch_library_ref_request(self, request: SwitchLibraryRefRequest) -> ResultPayload:
        """Switch a library to a different git branch or tag."""
        library_name = request.library_name
        ref_name = request.ref_name

        # Validate library and prepare for git operation
        validation_result = await self._validate_and_prepare_library_for_git_operation(
            library_name=library_name,
            failure_result_class=SwitchLibraryRefResultFailure,
            operation_description="switch branch/tag for",
        )
        if isinstance(validation_result, ResultPayloadFailure):
            return validation_result

        old_version = validation_result.old_version
        library_file_path = validation_result.library_file_path
        library_dir = validation_result.library_dir

        # Get current ref (branch or tag) before switch
        try:
            old_ref = await asyncio.to_thread(get_current_ref, library_dir)
            if old_ref is None:
                details = f"Library '{library_name}' is not on a branch/tag or is not a git repository."
                return SwitchLibraryRefResultFailure(result_details=details)
        except GitError as e:
            details = f"Failed to get current branch/tag for Library '{library_name}': {e}"
            return SwitchLibraryRefResultFailure(result_details=details)

        # Perform git ref switch (branch or tag)
        try:
            await asyncio.to_thread(switch_branch_or_tag, library_dir, ref_name)
        except GitError as e:
            details = f"Failed to switch to '{ref_name}' for Library '{library_name}': {e}"
            return SwitchLibraryRefResultFailure(result_details=details)

        # Reload library
        reload_result = await self._reload_library_after_git_operation(
            library_name=library_name,
            library_file_path=library_file_path,
            failure_result_class=SwitchLibraryRefResultFailure,
        )
        if isinstance(reload_result, ResultPayloadFailure):
            return reload_result

        new_version = reload_result

        # Get new ref (branch or tag) after switch
        try:
            new_ref = await asyncio.to_thread(get_current_ref, library_dir)
            if new_ref is None:
                new_ref = "unknown"
        except GitError:
            new_ref = "unknown"

        return self._build_library_ref_switch_result(
            library_name=library_name,
            old_ref=old_ref,
            new_ref=new_ref,
            old_version=old_version,
            new_version=new_version,
        )

    @handles(DownloadLibraryRequest)
    async def download_library_request(self, request: DownloadLibraryRequest) -> ResultPayload:  # noqa: PLR0911, PLR0912, PLR0915, C901
        """Download a library from a git repository."""
        parsed_url = parse_git_url_with_ref(normalize_github_url(request.git_url))
        git_url = parsed_url.url
        # Explicit branch_tag_commit wins over a url@ref suffix.
        branch_tag_commit = request.branch_tag_commit or parsed_url.ref
        target_directory_name = request.target_directory_name
        download_directory = request.download_directory

        # Determine the parent directory for the download
        config_mgr = self.engine.config_manager

        if download_directory is not None:
            # Use custom download directory if provided
            libraries_path = Path(download_directory)
        else:
            # Resolve the libraries root (a project's own/inherited libraries_dir override, else the
            # workspace-relative libraries_directory).
            libraries_path = config_mgr.resolved_libraries_root()

        # Ensure parent directory exists
        await anyio.Path(libraries_path).mkdir(parents=True, exist_ok=True)

        # Determine target directory name
        if target_directory_name is None:
            # Extract from git URL (e.g., "https://github.com/user/repo.git" -> "repo")
            target_directory_name = git_url.rstrip("/").split("/")[-1]
            target_directory_name = target_directory_name.removesuffix(".git")

        # Construct full target path
        target_path = libraries_path / target_directory_name

        # Check if target directory already exists
        skip_clone = False
        if await anyio.Path(target_path).exists():
            if request.overwrite_existing:
                # Delete existing directory before cloning
                delete_request = DeleteFileRequest(path=str(target_path), workspace_only=False)
                delete_result = await self.engine.ahandle_request(delete_request)

                if isinstance(delete_result, DeleteFileResultFailure):
                    details = f"Cannot delete existing directory at {target_path}: {delete_result.result_details}"
                    return DownloadLibraryResultFailure(result_details=details)

                logger.info("Deleted existing directory at %s for overwrite", target_path)
            else:
                # Check fail_on_exists flag
                if request.fail_on_exists:
                    # Fail with retryable error for interactive CLI
                    details = f"Cannot download library: target directory already exists at {target_path}"
                    return DownloadLibraryResultFailure(
                        result_details=details,
                        retryable=True,
                        existing_path=str(target_path),
                    )

                # Skip cloning since directory already exists, but continue with registration
                skip_clone = True
                logger.debug(
                    "Library directory already exists at %s, skipping download but will proceed with registration",
                    target_path,
                )

        # Clone the repository (unless skipping because it already exists)
        if skip_clone:
            logger.debug("Using existing library directory at %s", target_path)
        else:
            try:
                await asyncio.to_thread(clone_repository, git_url, target_path, branch_tag_commit)
            except GitError as e:
                details = f"Failed to clone repository from {git_url} to {target_path}: {e}"
                return DownloadLibraryResultFailure(result_details=details)

        # Recursively search for griptape_nodes_library.json file
        library_json_path = find_file_in_directory(target_path, "griptape[-_]nodes[-_]library.json")
        if library_json_path is None:
            details = f"Downloaded library from {git_url} but no library JSON file found in {target_path}"
            return DownloadLibraryResultFailure(result_details=details)

        try:
            content = await anyio.Path(library_json_path).read_text(encoding="utf-8")
            library_data = json.loads(content)
        except json.JSONDecodeError as e:
            details = f"Failed to parse griptape_nodes_library.json from downloaded library: {e}"
            return DownloadLibraryResultFailure(result_details=details)

        # Extract library name
        library_name = library_data.get("name")
        if library_name is None:
            details = "Downloaded library has no 'name' field in griptape_nodes_library.json"
            return DownloadLibraryResultFailure(result_details=details)

        # Automatically register the downloaded library (unless disabled for startup downloads)
        if request.auto_register:
            # Create LibraryInfo for tracking this downloaded library
            lib_info = LibraryInfo(
                lifecycle_state=LibraryLifecycleState.DISCOVERED,
                library_path=str(library_json_path),
                is_sandbox=False,
                library_name=library_name,
                fitness=LibraryFitness.NOT_EVALUATED,
                problems=[],
            )
            # Store lib_info in dict so register handler can find it
            self.engine.library_manager._library_file_path_to_info[str(library_json_path)] = lib_info

            register_request = RegisterLibraryFromFileRequest(file_path=str(library_json_path))
            register_result = await self.engine.ahandle_request(register_request)
            if not register_result.succeeded():
                return DownloadLibraryResultFailure(
                    result_details=f"Library '{library_name}' downloaded but failed to register: {register_result.result_details}"
                )
            logger.info("Library '%s' registered successfully", library_name)

        # Persist the path to libraries_to_register only when registering now. The
        # write lands in the GLOBAL user config (set_config_value -> user config),
        # so a project-reconcile download (auto_register=False) must NOT touch it:
        # the project's own libraries_to_download is the per-activation source of
        # truth, and persisting its clone path here would leak that library into
        # every other project's startup registration. Reconcile-downloaded
        # libraries instead reach discovery directly from libraries_to_download
        # (see discover_library_files), so they load scoped to the workspace that
        # declares them without any global config write.
        if request.auto_register:
            libraries_to_register = config_mgr.get_config_value(LIBRARIES_TO_REGISTER_KEY, default=[])
            library_json_str = str(library_json_path)
            existing_paths = {extract_library_path(entry) for entry in libraries_to_register}
            if library_json_str not in existing_paths:
                libraries_to_register.append(library_json_str)
                config_mgr.set_config_value(LIBRARIES_TO_REGISTER_KEY, libraries_to_register)
                logger.info("Added library '%s' to config for auto-registration on startup", library_name)

        if skip_clone:
            details = f"Library '{library_name}' already exists at {target_path} and has been registered"
        else:
            details = f"Successfully downloaded library '{library_name}' from {git_url} to {target_path}"
        return DownloadLibraryResultSuccess(
            library_name=library_name,
            library_path=str(library_json_path),
            result_details=details,
        )

    @handles(InspectLibraryRepoRequest)
    async def inspect_library_repo_request(self, request: InspectLibraryRepoRequest) -> ResultPayload:
        """Inspect a library's metadata from a git repository without downloading the full repository."""
        git_url = request.git_url
        ref = request.ref

        # Normalize GitHub shorthand to full URL
        normalized_url = normalize_github_url(git_url)
        logger.info("Inspecting library metadata from '%s' (ref: %s)", normalized_url, ref)

        # Perform sparse checkout to get library JSON
        try:
            checkout = sparse_checkout_library_json(normalized_url, ref)
        except GitError as e:
            details = f"Failed to inspect library from {normalized_url}: {e}"
            logger.error(details)
            return InspectLibraryRepoResultFailure(result_details=details)

        library_version = checkout.library_version
        commit_sha = checkout.commit_sha
        library_data_raw = checkout.library_data

        # Validate and create LibrarySchema
        try:
            library_schema = LibrarySchema(**library_data_raw)
        except Exception as e:
            details = f"Invalid library schema from {normalized_url}: {e}"
            logger.error(details)
            return InspectLibraryRepoResultFailure(result_details=details)

        # Return success with full library metadata
        details = f"Successfully inspected library '{library_schema.name}' (version {library_version}) from {normalized_url} at commit {commit_sha[:7]}"
        logger.info(details)
        return InspectLibraryRepoResultSuccess(
            library_schema=library_schema,
            commit_sha=commit_sha,
            git_url=normalized_url,
            ref=ref,
            result_details=details,
        )

    def _check_engine_version_compatibility(self, required_engine_version: str) -> tuple[bool, str]:
        """Check if a required engine version is compatible with the current engine.

        Args:
            required_engine_version: The engine version required by the library.

        Returns:
            A tuple of (is_compatible, current_engine_version).
            is_compatible is True if required_engine_version <= current_engine_version.
            If version comparison fails, returns (True, current_engine_version) to allow the operation.
        """
        engine_version_result = self.engine.handle_request(GetEngineVersionRequest())
        if not isinstance(engine_version_result, GetEngineVersionResultSuccess):
            logger.warning("Failed to get engine version for compatibility check, allowing operation to proceed")
            return True, ""

        current_engine_version = (
            f"{engine_version_result.major}.{engine_version_result.minor}.{engine_version_result.patch}"
        )

        if not required_engine_version:
            return True, current_engine_version

        try:
            required_ver = Version.parse(required_engine_version)
            current_ver = Version.parse(current_engine_version)
            is_compatible = required_ver <= current_ver
        except ValueError:
            # If version parsing fails, assume compatible
            return True, current_engine_version
        else:
            return is_compatible, current_engine_version

    def _read_minimum_release_age_config(self) -> MinimumReleaseAgeConfig:
        """Read the minimum-release-age setting once. Centralizes the key literal and default handling."""
        config_mgr = self.engine.config_manager
        # get_config_value returns None for an explicit `null` override (it bypasses both cast_type
        # and the default in that case), so coalesce None back to the default here to keep the gate
        # fail-open rather than raising when float(None) is attempted.
        hours = config_mgr.get_config_value(LIBRARY_MINIMUM_RELEASE_AGE_KEY, default=0.0, cast_type=float)
        if hours is None:
            hours = 0.0
        return MinimumReleaseAgeConfig(hours=float(hours))

    def _evaluate_update_age_gate(
        self, commit_datetime: datetime | None, config: MinimumReleaseAgeConfig | None = None
    ) -> UpdateAgeGateDecision:
        """Decide whether an update to a commit is withheld by the minimum release age.

        When the gate is disabled the update is never gated. When enabled but the commit timestamp is
        unknown, the update is allowed (age cannot be verified) and a warning is logged rather than
        wedging updates permanently.

        Args:
            commit_datetime: The timezone-aware timestamp of the commit the update would move to.
            config: Pre-read minimum-release-age config. When None, it is read from config. Callers
                that must inspect ``enabled`` before deciding whether to fetch the commit datetime
                should read it once via _read_minimum_release_age_config and pass it here to avoid a
                duplicate lookup.

        Returns:
            UpdateAgeGateDecision describing whether the gate is enabled, whether this update is
            gated, the commit's age in hours, and the configured minimum release age.
        """
        if config is None:
            config = self._read_minimum_release_age_config()
        enabled = config.enabled
        minimum_release_age_hours = config.hours

        if not enabled:
            return UpdateAgeGateDecision(
                enabled=False, gated=False, age_hours=None, minimum_release_age_hours=minimum_release_age_hours
            )

        if commit_datetime is None:
            logger.warning(
                "The library minimum release age is set but the target commit timestamp could not be "
                "determined. Allowing the update without an age check."
            )
            return UpdateAgeGateDecision(
                enabled=True, gated=False, age_hours=None, minimum_release_age_hours=minimum_release_age_hours
            )

        # Treat a naive timestamp as UTC so the subtraction below never raises.
        if commit_datetime.tzinfo is None:
            commit_datetime = commit_datetime.replace(tzinfo=UTC)

        age_hours = (datetime.now(tz=UTC) - commit_datetime).total_seconds() / 3600.0
        gated = age_hours < minimum_release_age_hours
        return UpdateAgeGateDecision(
            enabled=True, gated=gated, age_hours=age_hours, minimum_release_age_hours=minimum_release_age_hours
        )

    async def _get_remote_target_commit_datetime(self, library_dir: Path) -> datetime | None:
        """Fetch the timestamp of the commit an update would move a library to.

        Resolves the library's git remote and current ref, then reads the target commit's metadata
        from the remote. Returns None when the remote, ref, or timestamp cannot be determined; the
        age gate treats None as "cannot verify" and allows the update.
        """
        try:
            git_remote = await asyncio.to_thread(get_git_remote, library_dir)
            if git_remote is None:
                return None
            git_ref = await asyncio.to_thread(get_current_ref, library_dir)
            version_info = await asyncio.to_thread(clone_and_get_library_version, git_remote, git_ref or "HEAD")
        except GitError as e:
            logger.warning("Failed to determine target commit age for library at %s: %s", library_dir, e)
            return None
        return version_info.commit_datetime

    async def _validate_and_prepare_library_for_git_operation(
        self,
        library_name: str,
        failure_result_class: type[ResultPayloadFailure],
        operation_description: str,
    ) -> LibraryGitOperationContext | ResultPayloadFailure:
        """Validate library exists and prepare for git operation.

        Resolution goes through `get_library_info_by_library_name`, not the LibraryRegistry,
        which holds only libraries that finished loading. A library that failed to load is
        exactly the one a user needs to switch or update away from, so gating these operations
        on registration would put the repair out of reach of the libraries that need it.
        Routing through the shared resolver also keeps this path and
        check_library_update_request from disagreeing about which on-disk copy a duplicately
        registered library maps to.

        Args:
            library_name: Name of the library to validate
            failure_result_class: Class to use for failure results (e.g., UpdateLibraryResultFailure)
            operation_description: Description of operation for error messages (e.g., "update", "switch branch/tag for")

        Returns:
            On success: LibraryGitOperationContext with library info
            On failure: ResultPayloadFailure instance
        """
        library_info = self.engine.library_manager.get_library_info_by_library_name(library_name)
        if library_info is None:
            details = f"Attempted to {operation_description} Library '{library_name}'. Failed because no Library with that name was found."
            return failure_result_class(result_details=details)

        # Set once metadata loads, which happens before the engine-compatibility gate, so an
        # engine-incompatible library still reports the version it is pinned at.
        old_version = library_info.library_version
        if old_version is None:
            details = f"Library '{library_name}' has no version information."
            return failure_result_class(result_details=details)

        library_file_path = library_info.library_path

        # Get the library directory (parent of the JSON file)
        library_dir = Path(library_file_path).parent.absolute()

        return LibraryGitOperationContext(
            old_version=old_version,
            library_file_path=library_file_path,
            library_dir=library_dir,
        )

    async def _reload_library_after_git_operation(
        self,
        library_name: str,
        library_file_path: str,
        *,
        failure_result_class: type[ResultPayloadFailure],
    ) -> str | ResultPayloadFailure:
        """Reload library after git operation.

        Args:
            library_name: Name of the library to reload
            library_file_path: Path to the library JSON file
            failure_result_class: Class to use for failure results

        Returns:
            On success: new_version (str, may be "unknown")
            On failure: ResultPayloadFailure instance
        """
        # Unload the library. Only libraries that finished loading are in the registry, and
        # unregistering one that never got there raises, so skip the unload for a library the
        # git operation was meant to repair.
        if library_name in LibraryRegistry.list_libraries():
            unload_result = self.engine.handle_request(UnloadLibraryFromRegistryRequest(library_name=library_name))
            if not unload_result.succeeded():
                details = f"Failed to unload Library '{library_name}' after git operation."
                return failure_result_class(result_details=details)

        # Search for the library JSON file using flexible pattern to handle filename variations
        # (after git operations, the filename might change between griptape-nodes-library.json and griptape_nodes_library.json)
        library_dir = Path(library_file_path).parent
        actual_library_file = find_file_in_directory(library_dir, "griptape[-_]nodes[-_]library.json")

        if actual_library_file is None:
            details = (
                f"Failed to find library JSON file in {library_dir} after git operation for Library '{library_name}'."
            )
            return failure_result_class(result_details=details)

        # Use the found file path for reloading
        actual_library_file_path = str(actual_library_file)

        # Drop any lingering entries for this library before reinserting. The git operation may
        # resolve the JSON under a different filename (griptape_nodes_library.json vs
        # griptape-nodes-library.json), which would otherwise leave the pre-operation entry keyed
        # under the old filename alongside the new one. Two live entries for one name let the
        # update and update-check paths resolve different on-disk copies, producing a permanent
        # "update available" loop.
        stale_paths = [
            file_path
            for file_path, existing_info in self.engine.library_manager._library_file_path_to_info.items()
            if existing_info.library_name == library_name
        ]
        for file_path in stale_paths:
            del self.engine.library_manager._library_file_path_to_info[file_path]

        # Create LibraryInfo for tracking this library reload
        lib_info = LibraryInfo(
            lifecycle_state=LibraryLifecycleState.DISCOVERED,
            library_path=actual_library_file_path,
            is_sandbox=False,
            library_name=library_name,
            fitness=LibraryFitness.NOT_EVALUATED,
            problems=[],
        )
        # Store lib_info in dict so register handler can find it
        self.engine.library_manager._library_file_path_to_info[actual_library_file_path] = lib_info

        # Reload the library from file
        reload_result = await self.engine.ahandle_request(
            RegisterLibraryFromFileRequest(file_path=actual_library_file_path)
        )
        if not isinstance(reload_result, RegisterLibraryFromFileResultSuccess):
            details = f"Failed to reload Library '{library_name}' after git operation."
            return failure_result_class(result_details=details)

        # Get new version after reload
        try:
            updated_library = LibraryRegistry.get_library(name=library_name)
            new_version = updated_library.get_metadata().library_version
            if new_version is None:
                new_version = "unknown"
        except KeyError:
            new_version = "unknown"

        return new_version

    def _build_library_update_result(
        self, *, library_name: str, old_version: str, new_version: str
    ) -> UpdateLibraryResultSuccess:
        """Report an update that landed on disk, flagging a restart when this engine cannot run it.

        Args:
            library_name: Name of the updated library
            old_version: Version the library was on before the update
            new_version: Version now on disk
        """
        stale_module_explanation = self.engine.library_manager.catalog.explain_restart_after_reload(library_name)

        if stale_module_explanation is None:
            details = f"Successfully updated Library '{library_name}' from version {old_version} to {new_version}."
            return UpdateLibraryResultSuccess(old_version=old_version, new_version=new_version, result_details=details)

        details = (
            f"Updated Library '{library_name}' from version {old_version} to {new_version} on disk. "
            f"{stale_module_explanation}"
        )
        return UpdateLibraryResultSuccess(
            old_version=old_version,
            new_version=new_version,
            restart_required=True,
            result_details=details,
        )

    def _build_library_ref_switch_result(
        self, *, library_name: str, old_ref: str, new_ref: str, old_version: str, new_version: str
    ) -> SwitchLibraryRefResultSuccess:
        """Report a ref switch that landed on disk, flagging a restart when this engine cannot run it.

        Args:
            library_name: Name of the switched library
            old_ref: Branch or tag the library was on before the switch
            new_ref: Branch or tag now checked out
            old_version: Version the library was on before the switch
            new_version: Version now on disk
        """
        stale_module_explanation = self.engine.library_manager.catalog.explain_restart_after_reload(library_name)

        if stale_module_explanation is None:
            details = f"Successfully switched Library '{library_name}' from '{old_ref}' (version {old_version}) to '{new_ref}' (version {new_version})."
            return SwitchLibraryRefResultSuccess(
                old_ref=old_ref,
                new_ref=new_ref,
                old_version=old_version,
                new_version=new_version,
                result_details=details,
            )

        details = (
            f"Switched Library '{library_name}' from '{old_ref}' (version {old_version}) to '{new_ref}' "
            f"(version {new_version}) on disk. {stale_module_explanation}"
        )
        return SwitchLibraryRefResultSuccess(
            old_ref=old_ref,
            new_ref=new_ref,
            old_version=old_version,
            new_version=new_version,
            restart_required=True,
            result_details=details,
        )
