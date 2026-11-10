"""Uninstalling a library: removing its config entry, unloading it, and deleting its folder.

See `PreviewUninstallLibraryRequest` and `UninstallLibraryRequest` in `library_events.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePath, PureWindowsPath
from typing import TYPE_CHECKING

import anyio

from griptape_nodes.files.path_utils import canonicalize_for_identity, resolve_workspace_path
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.base_events import ResultDetails
from griptape_nodes.retained_mode.events.library_events import (
    DownloadEntryRemaining,
    EnvironmentManagedBlocker,
    NotFoundBlocker,
    PreviewUninstallLibraryRequest,
    PreviewUninstallLibraryResultFailure,
    PreviewUninstallLibraryResultSuccess,
    RemainingRegistration,
    SandboxLibraryBlocker,
    ShadowedConfigRemaining,
    SharedDirectoryEntryRemaining,
    UninstallBlocker,
    UninstallLibraryRequest,
    UninstallLibraryResultFailure,
    UninstallLibraryResultSuccess,
    UnloadLibraryFromRegistryRequest,
    UnloadLibraryFromRegistryResultFailure,
    UnmatchedEntryRemaining,
    UnsafePathBlocker,
)
from griptape_nodes.retained_mode.events.os_events import (
    DeleteFileRequest,
    DeleteFileResultFailure,
    DeletionBehavior,
)
from griptape_nodes.retained_mode.managers.library.common import LIBRARY_CONFIG_GLOB_PATTERN, LibraryLifecycleState
from griptape_nodes.retained_mode.managers.library.metadata_loading import is_library_name_registered
from griptape_nodes.retained_mode.managers.settings import (
    LIBRARIES_TO_DOWNLOAD_KEY,
    LIBRARIES_TO_REGISTER_KEY,
    LibraryDownload,
)
from griptape_nodes.retained_mode.request_handlers import handles
from griptape_nodes.utils.file_utils import find_files_recursive
from griptape_nodes.utils.library_utils import normalize_library_downloads, normalize_library_registrations

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.events.config_events import ConfigValueSource
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.managers.library.common import LibraryInfo

logger = logging.getLogger("griptape_nodes")


@dataclass
class _Classification:
    """Which config array, if any, registered the library's manifest."""

    registered_in: str | None  # "register" | "download" | None
    registered_path: str | None  # verbatim config text
    is_directory_entry: bool = False
    directory_path: Path | None = None  # set only when is_directory_entry
    raw_register_entry: object = None  # the exact raw libraries_to_register item, for removal
    raw_download_entry: object = None  # the exact raw libraries_to_download item, for removal
    download: LibraryDownload | None = None  # set only when registered_in == "download"


@dataclass
class _Resolution:
    """Everything `PreviewUninstallLibraryRequest` and `UninstallLibraryRequest` report.

    Shared by both so they cannot disagree about which folder is in play.
    """

    library_name: str
    library_path: str
    install_directory: Path | None
    classification: _Classification
    collateral_library_names: list[str] = field(default_factory=list)
    blocked_by: UninstallBlocker | None = None
    remaining_registrations: list[RemainingRegistration] = field(default_factory=list)


@dataclass
class _ResolutionError:
    """The request named no library this engine knows about."""

    details: str


class LibraryUninstall(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        # Serializes UninstallLibraryRequest: two in flight would both read the same config
        # array, and the second write would restore the entry the first one just removed.
        self._uninstall_lock = asyncio.Lock()
        event_manager.register_request_handlers(self)

    @handles(PreviewUninstallLibraryRequest)
    async def preview_uninstall_library_request(self, request: PreviewUninstallLibraryRequest) -> ResultPayload:
        resolved = await self._resolve(request.library_name, request.library_path)
        if isinstance(resolved, _ResolutionError):
            return PreviewUninstallLibraryResultFailure(result_details=resolved.details)

        details = f"Resolved uninstall target for Library '{resolved.library_name}'."
        if resolved.blocked_by is not None:
            details = (
                f"Library '{resolved.library_name}' cannot be uninstalled from here "
                f"({type(resolved.blocked_by).__name__})."
            )
        return PreviewUninstallLibraryResultSuccess(
            library_name=resolved.library_name,
            library_path=resolved.library_path,
            install_directory=str(resolved.install_directory) if resolved.install_directory is not None else None,
            registered_path=resolved.classification.registered_path,
            registered_in=resolved.classification.registered_in,
            collateral_library_names=resolved.collateral_library_names,
            blocked_by=resolved.blocked_by,
            remaining_registrations=resolved.remaining_registrations,
            result_details=details,
        )

    @handles(UninstallLibraryRequest)
    async def uninstall_library_request(self, request: UninstallLibraryRequest) -> ResultPayload:
        async with self._uninstall_lock:
            resolved = await self._resolve(request.library_name, request.library_path)
            if isinstance(resolved, _ResolutionError):
                return UninstallLibraryResultFailure(
                    result_details=resolved.details,
                    failure_reason="not_found",
                    blocked_by=NotFoundBlocker(),
                )

            if resolved.blocked_by is not None:
                details = (
                    f"Attempted to uninstall Library '{resolved.library_name}'. Refused: "
                    f"{type(resolved.blocked_by).__name__}."
                )
                return UninstallLibraryResultFailure(
                    result_details=details, failure_reason="blocked", blocked_by=resolved.blocked_by
                )

            config_entry_removed, config_failure = self._uninstall_remove_config_entry(resolved)
            if config_failure is not None:
                return config_failure

            unloaded, unload_failure = self._uninstall_unload_from_registry(resolved)
            if unload_failure is not None:
                return unload_failure

            restart_required = self.engine.library_manager.reset.reset_requires_restart(
                resolved.library_name, resolved.library_path
            )

            directory_deleted, delete_failure = await self._uninstall_delete_directory(resolved)
            if delete_failure is not None:
                return delete_failure

            return self._uninstall_success(
                resolved,
                unloaded=unloaded,
                config_entry_removed=config_entry_removed,
                directory_deleted=directory_deleted,
                restart_required=restart_required,
            )

    def _uninstall_remove_config_entry(
        self, resolved: _Resolution
    ) -> tuple[bool, UninstallLibraryResultFailure | None]:
        """Step 1 (least destructive): remove the config entry, if any names this exact copy."""
        if resolved.classification.registered_in is None or resolved.classification.is_directory_entry:
            return False, None
        if any(isinstance(entry, ShadowedConfigRemaining) for entry in resolved.remaining_registrations):
            return False, None
        removed = self._remove_config_entry(resolved.classification)
        if not removed:
            details = (
                f"Attempted to uninstall Library '{resolved.library_name}'. Failed to remove its entry from "
                f"config; nothing was changed."
            )
            return False, UninstallLibraryResultFailure(result_details=details, failure_reason="config_write_failed")
        return True, None

    def _uninstall_unload_from_registry(
        self, resolved: _Resolution
    ) -> tuple[bool, UninstallLibraryResultFailure | None]:
        """Step 2: unload from the in-memory registry, but only when THIS copy is the loaded one."""
        library_manager = self.engine.library_manager
        library_name = resolved.library_name
        tracked = library_manager.find_library_info_for_attempted_load(resolved.library_path)
        this_copy_is_loaded = tracked is not None and tracked.lifecycle_state == LibraryLifecycleState.LOADED

        if not is_library_name_registered(library_name) or not this_copy_is_loaded:
            library_manager.forget_attempted_load(resolved.library_path)
            return False, None
        unload_result = library_manager.registration.unload_library_from_registry_request(
            UnloadLibraryFromRegistryRequest(library_name=library_name)
        )
        if isinstance(unload_result, UnloadLibraryFromRegistryResultFailure):
            details = (
                f"Attempted to uninstall Library '{library_name}'. Its config entry was removed, but "
                f"it failed to unload from the registry: {unload_result.result_details}"
            )
            return False, UninstallLibraryResultFailure(result_details=details, failure_reason="unload_failed")
        return True, None

    async def _uninstall_delete_directory(
        self, resolved: _Resolution
    ) -> tuple[bool, UninstallLibraryResultFailure | None]:
        """Step 3 (most destructive): delete the install directory, or treat its absence as success."""
        if resolved.install_directory is None or _has_shared_directory_entry(resolved.remaining_registrations):
            return False, None
        if not await anyio.Path(resolved.install_directory).exists():
            # Already gone is success -- the goal (no install on disk) is met.
            return True, None
        delete_result = await self.engine.ahandle_request(
            DeleteFileRequest(
                path=str(resolved.install_directory),
                workspace_only=False,
                deletion_behavior=DeletionBehavior.PREFER_RECYCLE_BIN,
            )
        )
        if isinstance(delete_result, DeleteFileResultFailure):
            details = (
                f"Attempted to uninstall Library '{resolved.library_name}'. Its config entry and registry "
                f"load were removed, but its folder at {resolved.install_directory} could not be "
                f"deleted: {delete_result.result_details}"
            )
            return False, UninstallLibraryResultFailure(
                result_details=details, failure_reason="directory_delete_failed"
            )
        return True, None

    def _uninstall_success(
        self,
        resolved: _Resolution,
        *,
        unloaded: bool,
        config_entry_removed: bool,
        directory_deleted: bool,
        restart_required: bool,
    ) -> UninstallLibraryResultSuccess:
        message = f"Successfully uninstalled Library '{resolved.library_name}'."
        level = logging.INFO
        if resolved.remaining_registrations:
            kinds = ", ".join(sorted({type(entry).__name__ for entry in resolved.remaining_registrations}))
            message += f" Something still registers it ({kinds})."
            level = logging.WARNING
        if restart_required:
            message += " Restart the engine to finish removing it from memory."
            level = logging.WARNING

        return UninstallLibraryResultSuccess(
            library_name=resolved.library_name,
            library_path=resolved.library_path,
            install_directory=str(resolved.install_directory) if resolved.install_directory is not None else None,
            registered_path=resolved.classification.registered_path,
            registered_in=resolved.classification.registered_in,
            collateral_library_names=resolved.collateral_library_names,
            remaining_registrations=resolved.remaining_registrations,
            unloaded=unloaded,
            config_entry_removed=config_entry_removed,
            directory_deleted=directory_deleted,
            restart_required=restart_required,
            result_details=ResultDetails(message=message, level=level),
        )

    async def _resolve(self, library_name: str, library_path: str) -> _Resolution | _ResolutionError:
        """Find the target, classify its registration, and decide whether it is safe to delete.

        Shared by preview and uninstall so the two cannot derive different answers.
        """
        library_info = self._find_library_info(library_name, library_path)
        if library_info is None:
            details = (
                f"Attempted to uninstall Library '{library_name}' at '{library_path}'. Failed because no "
                f"Library with that name is installed there."
            )
            return _ResolutionError(details=details)

        if library_info.is_sandbox:
            return self._blocked_resolution(library_name, library_info, blocked_by=SandboxLibraryBlocker())

        managed = self.engine.library_manager.managed_environment
        if managed.provisioned_by_environment():
            return self._blocked_resolution(library_name, library_info, blocked_by=EnvironmentManagedBlocker())

        resolved_library_path = Path(library_info.library_path)
        classification = await self._classify_registration(canonicalize_for_identity(resolved_library_path))

        # `.`/`..` segments in a hand-typed libraries_to_register entry do not survive to the
        # resolved path, because resolve_workspace_path normalizes them away; check the raw text
        # before that happens, or "C:\libs\Nuke\.." would quietly resolve to (and later delete)
        # the parent "libs".
        if classification.registered_in == "register" and classification.registered_path is not None:
            raw_registered_path = classification.registered_path
            if _path_text_has_traversal_segment(raw_registered_path):
                return self._blocked_resolution(
                    library_name,
                    library_info,
                    blocked_by=UnsafePathBlocker(path=raw_registered_path),
                    classification=classification,
                )

        install_directory = self._install_directory_for(classification, resolved_library_path)

        if _unsafe_install_directory(install_directory) or self._is_protected_directory(install_directory):
            return self._blocked_resolution(
                library_name,
                library_info,
                blocked_by=UnsafePathBlocker(path=str(install_directory)),
                classification=classification,
                install_directory=install_directory,
            )

        collateral_library_names = await self._collect_collateral_library_names(
            install_directory, exclude_manifest=resolved_library_path
        )
        remaining_registrations = self._remaining_registrations_for(
            classification, exclude_manifest=resolved_library_path
        )

        return _Resolution(
            library_name=library_name,
            library_path=str(resolved_library_path),
            install_directory=install_directory,
            classification=classification,
            collateral_library_names=collateral_library_names,
            blocked_by=None,
            remaining_registrations=remaining_registrations,
        )

    def _remaining_registrations_for(
        self, classification: _Classification, *, exclude_manifest: Path
    ) -> list[RemainingRegistration]:
        """What would still register this library once its files are gone."""
        remaining: list[RemainingRegistration] = []

        if classification.registered_in is None:
            return [UnmatchedEntryRemaining()]

        if classification.registered_in == "register" and classification.is_directory_entry:
            remaining.append(
                SharedDirectoryEntryRemaining(
                    entry_path=classification.registered_path or "",
                    library_names=self._sibling_library_names(classification, exclude_manifest=exclude_manifest),
                )
            )

        if classification.registered_in == "download" and classification.download is not None:
            remaining.append(DownloadEntryRemaining(git_url=classification.download.git_url))

        shadowed_by = self._shadowed_config_source(classification)
        if shadowed_by is not None:
            remaining.append(ShadowedConfigRemaining(layer=shadowed_by.layer))

        return remaining

    def _sibling_library_names(self, classification: _Classification, *, exclude_manifest: Path) -> list[str]:
        """The OTHER libraries a shared directory entry registers, by name where one is known."""
        if classification.directory_path is None:
            return []
        directory_identity = canonicalize_for_identity(classification.directory_path)
        exclude_identity = canonicalize_for_identity(exclude_manifest)
        names: list[str] = []
        library_manager = self.engine.library_manager
        for manifest_path in library_manager.get_libraries_attempted_to_load():
            library_info = library_manager.get_library_info_for_attempted_load(manifest_path)
            manifest_identity = canonicalize_for_identity(library_info.library_path)
            if manifest_identity == exclude_identity or not manifest_identity.is_relative_to(directory_identity):
                continue
            if library_info.library_name is not None:
                names.append(library_info.library_name)
        return sorted(set(names))

    def _shadowed_config_source(self, classification: _Classification) -> ConfigValueSource | None:
        """The layer supplying this entry's config list, when the engine cannot write that layer."""
        if classification.registered_in is None:
            return None
        config_key = (
            LIBRARIES_TO_DOWNLOAD_KEY if classification.registered_in == "download" else LIBRARIES_TO_REGISTER_KEY
        )
        return self.engine.config_manager.shadowed_by(config_key)

    def _is_protected_directory(self, install_directory: Path) -> bool:
        """Whether deleting `install_directory` would take the workspace, libraries root, or home with it."""
        config_mgr = self.engine.config_manager
        protected = [config_mgr.workspace_path, config_mgr.resolved_libraries_root()]
        # No home directory resolvable on this machine; the other two still apply.
        with contextlib.suppress(RuntimeError):
            protected.append(Path.home())

        target = canonicalize_for_identity(install_directory)
        for directory in protected:
            guarded = canonicalize_for_identity(directory)
            # `guarded.is_relative_to(target)` covers the target being a PARENT of a protected
            # folder (deleting it takes the protected folder too); equality is the common case.
            if guarded == target or guarded.is_relative_to(target):
                return True
        return False

    def _install_directory_for(self, classification: _Classification, resolved_library_path: Path) -> Path:
        """The folder an uninstall would delete for this classification."""
        if (
            classification.registered_in == "register"
            and classification.is_directory_entry
            and classification.directory_path is not None
        ):
            return classification.directory_path
        if classification.registered_in == "download" and classification.download is not None:
            if classification.download.name is not None:
                return resolved_library_path.parent
            return self.engine.library_manager.provisioning.clone_target_directory(
                classification.download.git_url, self.engine.config_manager.resolved_libraries_root()
            )
        return resolved_library_path.parent

    def _find_library_info(self, library_name: str, library_path: str) -> LibraryInfo | None:
        """The tracked copy at `library_path`, when its name matches."""
        library_manager = self.engine.library_manager
        library_info = library_manager.find_library_info_for_attempted_load(library_path)
        if library_info is not None and library_info.library_name == library_name:
            return library_info
        # The tracked key is the path as discovery recorded it; a client that canonicalized
        # differently (a symlinked or `~`-spelled libraries dir) still names the same file.
        wanted = canonicalize_for_identity(library_path)
        for manifest_path in library_manager.get_libraries_attempted_to_load():
            candidate = library_manager.get_library_info_for_attempted_load(manifest_path)
            if candidate.library_name != library_name:
                continue
            if canonicalize_for_identity(candidate.library_path) == wanted:
                return candidate
        return None

    def _blocked_resolution(
        self,
        library_name: str,
        library_info: LibraryInfo,
        *,
        blocked_by: UninstallBlocker,
        classification: _Classification | None = None,
        install_directory: Path | None = None,
    ) -> _Resolution:
        return _Resolution(
            library_name=library_name,
            library_path=library_info.library_path,
            install_directory=install_directory,
            classification=classification
            if classification is not None
            else _Classification(registered_in=None, registered_path=None),
            blocked_by=blocked_by,
        )

    async def _classify_registration(self, identity: Path) -> _Classification:
        """Which config array registered this manifest, re-run per entry (not by index).

        Each raw entry is normalized on its own, so a malformed entry elsewhere cannot shift
        which raw item a later match is removed from.
        """
        config_mgr = self.engine.config_manager

        # Downloads first: a download-provisioned library's registered_path is the installed
        # manifest's own absolute path (discovery.py), indistinguishable by string alone from a
        # libraries_to_register entry that happens to name the same absolute path. Checking
        # libraries_to_download independently resolves the ambiguity instead of guessing from it.
        raw_downloads = config_mgr.get_config_value(LIBRARIES_TO_DOWNLOAD_KEY, default=[]) or []
        libraries_root = config_mgr.resolved_libraries_root()
        for raw_entry in raw_downloads:
            downloads = normalize_library_downloads([raw_entry])
            if not downloads:
                continue
            manifest_path = await self.engine.library_manager.provisioning.installed_manifest_path_for_download(
                downloads[0], libraries_root
            )
            if manifest_path is not None and canonicalize_for_identity(manifest_path) == identity:
                return _Classification(
                    registered_in="download",
                    registered_path=str(manifest_path),
                    raw_download_entry=raw_entry,
                    download=downloads[0],
                )

        raw_registrations = config_mgr.get_config_value(LIBRARIES_TO_REGISTER_KEY, default=[]) or []
        for raw_entry in raw_registrations:
            registrations = normalize_library_registrations([raw_entry])
            if not registrations:
                continue
            entry = registrations[0]
            # Resolved without requiring the path to exist, unlike `resolve_discovery_path`
            # (which backs live discovery and returns None for a missing path): a manifest whose
            # folder is already gone must still match its config entry, or an already-missing
            # install could never clean up the stale entry pointing at it, which is exactly the
            # broken state this request exists to avoid creating.
            resolved_path = resolve_workspace_path(Path(entry.path), config_mgr.workspace_path)
            if await anyio.Path(resolved_path).is_dir():
                children = await find_files_recursive(
                    resolved_path, LIBRARY_CONFIG_GLOB_PATTERN, max_depth=config_mgr.discovery_max_depth
                )
                if any(canonicalize_for_identity(child) == identity for child in children):
                    return _Classification(
                        registered_in="register",
                        registered_path=entry.path,
                        is_directory_entry=True,
                        directory_path=resolved_path,
                        raw_register_entry=raw_entry,
                    )
            elif resolved_path.suffix == ".json" and canonicalize_for_identity(resolved_path) == identity:
                return _Classification(
                    registered_in="register",
                    registered_path=entry.path,
                    raw_register_entry=raw_entry,
                )

        return _Classification(registered_in=None, registered_path=None)

    def _remove_config_entry(self, classification: _Classification) -> bool:
        """Remove the exact raw entry `_classify_registration` matched, by equality not index."""
        config_mgr = self.engine.config_manager
        if classification.registered_in == "download":
            raw = list(config_mgr.get_config_value(LIBRARIES_TO_DOWNLOAD_KEY, default=[]) or [])
            if classification.raw_download_entry not in raw:
                return False
            raw.remove(classification.raw_download_entry)
            return config_mgr.set_config_value(LIBRARIES_TO_DOWNLOAD_KEY, raw)
        if classification.registered_in == "register":
            raw = list(config_mgr.get_config_value(LIBRARIES_TO_REGISTER_KEY, default=[]) or [])
            if classification.raw_register_entry not in raw:
                return False
            raw.remove(classification.raw_register_entry)
            return config_mgr.set_config_value(LIBRARIES_TO_REGISTER_KEY, raw)
        return True

    async def _collect_collateral_library_names(
        self, install_directory: Path | None, *, exclude_manifest: Path
    ) -> list[str]:
        """Other library manifests under `install_directory`, including ones that fail to parse."""
        if install_directory is None or not await anyio.Path(install_directory).is_dir():
            return []
        exclude_identity = canonicalize_for_identity(exclude_manifest)
        manifests = await find_files_recursive(
            install_directory, LIBRARY_CONFIG_GLOB_PATTERN, max_depth=self.engine.config_manager.discovery_max_depth
        )
        names: list[str] = []
        for manifest_path in manifests:
            if canonicalize_for_identity(manifest_path) == exclude_identity:
                continue
            names.append(_read_raw_library_name(manifest_path) or str(manifest_path))
        return names


def _has_shared_directory_entry(remaining: list[RemainingRegistration]) -> bool:
    return any(isinstance(entry, SharedDirectoryEntryRemaining) for entry in remaining)


def _read_raw_library_name(manifest_path: Path) -> str | None:
    """A manifest's declared `name`, read as raw JSON so a schema-invalid sibling still reports one."""
    try:
        content = manifest_path.read_text(encoding="utf-8")
        data = json.loads(content)
    except (OSError, json.JSONDecodeError):
        return None
    name = data.get("name") if isinstance(data, dict) else None
    return name if isinstance(name, str) else None


def _path_text_has_traversal_segment(path_text: str) -> bool:
    """Whether raw path text contains a literal `.` or `..` segment."""
    normalized = path_text.replace("\\", "/")
    for part in normalized.split("/"):
        if not part:
            continue
        if part.rstrip(" ") in (".", ".."):
            return True
    return False


_WINDOWS_DRIVE_OR_UNC_PATTERN = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/]{2})")


def _unsafe_install_directory(install_directory: PurePath) -> bool:
    """Whether `install_directory` must not be deleted: a filesystem root, drive root, or UNC share."""
    path_str = str(install_directory)
    backslash_form = path_str.replace("/", "\\")
    if backslash_form.lower().startswith("\\\\?\\unc\\"):
        return True
    if backslash_form.startswith("\\\\") and not backslash_form.startswith("\\\\?\\"):
        return True

    parts = install_directory.parts
    # A foreign-flavored path collapses to one component under the host's parser while its text
    # plainly names a drive or UNC root; re-read it under the flavor it was written in.
    if len(parts) <= 1 and _WINDOWS_DRIVE_OR_UNC_PATTERN.match(path_str):
        parts = PureWindowsPath(path_str).parts
    return len(parts) <= 1
