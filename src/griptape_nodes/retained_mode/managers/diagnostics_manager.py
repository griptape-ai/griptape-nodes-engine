"""DiagnosticsManager - Collects a redacted snapshot of the engine for troubleshooting."""

from __future__ import annotations

import logging
import os
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import unquote, urlparse

from dotenv import dotenv_values

from griptape_nodes.common.diagnostics.bundle import DiagnosticsBundle, DiagnosticsBundleManifest
from griptape_nodes.common.diagnostics.health import (
    CLOUD_API_KEY_NAME,
    HealthCheckContext,
    HealthReport,
    HealthStatus,
    redact_health_report,
    run_health_checks,
)
from griptape_nodes.common.diagnostics.redaction import Redactor
from griptape_nodes.common.diagnostics.report import (
    ConfigDiagnostics,
    ConfigFileDiagnostics,
    DiagnosticsReport,
    EngineDiagnostics,
    HostDiagnostics,
    LibraryDiagnostics,
    LogDiagnostics,
    LogFileDiagnostics,
    PathDiagnostics,
    ProjectDiagnostics,
    ProjectProblemDiagnostics,
    RedactionSummary,
    SecretDiagnostics,
    SessionDiagnostics,
)
from griptape_nodes.common.log_capture import (
    DEFAULT_BUFFER_LINES,
    active_log_file,
    find_log_files,
    session_log_lines,
)
from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.files.path_utils import (
    canonicalize_for_identity,
    canonicalize_for_io,
    strip_windows_long_path_prefix,
)
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.app_events import (
    GetEngineVersionRequest,
    GetEngineVersionResultSuccess,
)
from griptape_nodes.retained_mode.events.diagnostics_events import (
    CollectDiagnosticsRequest,
    CollectDiagnosticsResultFailure,
    CollectDiagnosticsResultSuccess,
    GetDiagnosticsReportRequest,
    GetDiagnosticsReportResultFailure,
    GetDiagnosticsReportResultSuccess,
    RunHealthChecksRequest,
    RunHealthChecksResultFailure,
    RunHealthChecksResultSuccess,
)
from griptape_nodes.retained_mode.events.os_events import (
    ExistingFilePolicy,
    WriteFileRequest,
    WriteFileResultSuccess,
)
from griptape_nodes.retained_mode.events.project_events import (
    GetCurrentProjectRequest,
    GetCurrentProjectResultSuccess,
    ListProjectTemplatesRequest,
    ListProjectTemplatesResultSuccess,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    ListAllWorkflowsRequest,
    ListAllWorkflowsResultSuccess,
)
from griptape_nodes.retained_mode.managers.config_manager import USER_CONFIG_PATH
from griptape_nodes.retained_mode.managers.secrets_manager import ENV_VAR_PATH
from griptape_nodes.retained_mode.managers.settings import (
    LOG_RETENTION_DAYS_KEY,
    LOG_TO_FILE_KEY,
    SECRETS_TO_REGISTER_KEY,
    SESSION_LOG_BUFFER_LINES_KEY,
)
from griptape_nodes.utils.dict_utils import normalize_secrets_to_register
from griptape_nodes.utils.version_utils import get_install_source

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.project_events import ProjectTemplateInfo
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

logger = logging.getLogger("griptape_nodes")

_BYTES_PER_GB = 1024 * 1024 * 1024

# Environment variables that override every config file. Their names are reported so a
# setting that ignores the config file has a visible cause; their values are not,
# because any of them can carry a credential.
CONFIG_ENV_VAR_PREFIX = "GTN_CONFIG_"


class SecretLayers(NamedTuple):
    """Which secret keys each source holds, mapped to whether the value there is non-empty.

    One entry per place the engine looks for a secret, so shadowing between them can be
    reported. Booleans only: no secret value survives past collection.
    """

    os_environment: dict[str, bool]
    workspace_env_file: dict[str, bool]
    global_env_file: dict[str, bool]


class SecretLayer(NamedTuple):
    """One place the engine looks for a secret, named as a user would recognize it.

    Attributes:
        label: What to call this source in the report.
        keys_set: The secret keys the source holds, mapped to whether the value there is
            non-empty. A key present with an empty value still shadows a lower layer.
    """

    label: str
    keys_set: dict[str, bool]


class DiagnosticsManager(EngineScoped):
    """Assembles the troubleshooting snapshot described by ``DiagnosticsReport``.

    Owns no persistent state. Every section is gathered on demand from the peer
    managers and passed through a single ``Redactor``, so a report is safe to hand to
    someone else and says what it removed.

    A section that cannot be gathered is recorded in ``collection_warnings`` and the
    report is still returned. The reason a section is missing is frequently the problem
    being investigated, and a partial report beats no report.
    """

    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        """Initialize the DiagnosticsManager.

        Args:
            event_manager: The EventManager instance to use for event handling.
            engine: The owning Engine, used to resolve peer managers.
        """
        super().__init__(engine)
        event_manager.assign_manager_to_request_type(
            GetDiagnosticsReportRequest, self.on_get_diagnostics_report_request
        )
        event_manager.assign_manager_to_request_type(CollectDiagnosticsRequest, self.on_collect_diagnostics_request)
        event_manager.assign_manager_to_request_type(RunHealthChecksRequest, self.on_run_health_checks_request)

    async def on_get_diagnostics_report_request(
        self, request: GetDiagnosticsReportRequest
    ) -> GetDiagnosticsReportResultSuccess | GetDiagnosticsReportResultFailure:
        """Collect a redacted snapshot of the engine's state."""
        redactor = Redactor(
            secret_values=self._known_secret_values(),
            normalize_identity=request.normalize_identity,
        )
        warnings: list[str] = []

        report = await self._build_report(redactor, warnings, normalize_identity=request.normalize_identity)
        if report is None:
            details = (
                "Attempted to collect a diagnostics report. Failed because the engine's host could not be identified."
            )
            logger.error(details)
            return GetDiagnosticsReportResultFailure(result_details=details)

        return GetDiagnosticsReportResultSuccess(
            report=report,
            result_details=(
                f"Successfully collected a diagnostics report covering {len(report.libraries)} library/libraries "
                f"and {len(report.projects)} project(s), with {report.redaction.total} value(s) redacted."
            ),
        )

    async def on_run_health_checks_request(
        self,
        request: RunHealthChecksRequest,  # noqa: ARG002 - the checks take no options yet
    ) -> RunHealthChecksResultSuccess | RunHealthChecksResultFailure:
        """Judge the engine's setup and say what to fix."""
        # Identity is normalized because a verdict quotes paths back to the user, who is
        # about to paste it somewhere.
        redactor = Redactor(secret_values=self._known_secret_values(), normalize_identity=True)
        warnings: list[str] = []

        report = await self._build_report(redactor, warnings, normalize_identity=True)
        if report is None:
            details = (
                "Attempted to run health checks. Failed because the engine's own state could not be collected first."
            )
            logger.error(details)
            return RunHealthChecksResultFailure(result_details=details)

        # Redacted like the bundle's copy. Most of a verdict is quoted from the report,
        # which is already clean, but a check that failed writes its own text and that text
        # can be an exception from the network stack or the OS.
        health = redact_health_report(await self._run_health_checks(report), redactor)
        failed = [result.name for result in health.results if result.status is HealthStatus.FAIL]
        warned = [result.name for result in health.results if result.status is HealthStatus.WARN]

        return RunHealthChecksResultSuccess(
            health=health,
            result_details=(
                f"Successfully ran {len(health.results)} health check(s): {len(failed)} failed, "
                f"{len(warned)} raised a warning."
            ),
        )

    async def on_collect_diagnostics_request(
        self, request: CollectDiagnosticsRequest
    ) -> CollectDiagnosticsResultSuccess | CollectDiagnosticsResultFailure:
        """Build a diagnostics bundle and return a link to download it."""
        redactor = Redactor(
            secret_values=self._known_secret_values(),
            normalize_identity=request.normalize_identity,
        )
        warnings: list[str] = []

        # Every part of assembly is inside the guard, not only the zip. Staging writes files
        # to a temporary directory, and a temporary directory that is full or unwritable
        # fails on the first file rather than at the end. A handler that let that OSError
        # out would reach the user as raw exception text instead of a result they can act on.
        try:
            with DiagnosticsBundle(redactor) as bundle:
                # Logs and the workflow are staged before the report so the redaction counts
                # the manifest reports cover every file, not just the report.
                if request.include_logs:
                    self._stage_logs(bundle, warnings)
                if request.include_current_workflow:
                    self._stage_current_workflow(bundle, warnings)

                report = await self._build_report(redactor, warnings, normalize_identity=request.normalize_identity)
                if report is None:
                    details = "Attempted to collect a diagnostics bundle. Failed because the engine's host could not be identified."
                    logger.error(details)
                    return CollectDiagnosticsResultFailure(result_details=details)

                if request.include_health_checks:
                    bundle.add_health_report(await self._run_health_checks(report))
                    # The health checks are read out of the report but staged after it, so
                    # the report's own count is restated here to cover them too.
                    report.redaction = self._redaction_summary(redactor, normalize_identity=request.normalize_identity)

                bundle.add_report(report)
                bundle.add_readme()
                manifest = bundle.write_manifest(
                    generated_at=report.generated_at,
                    engine_version=report.engine.engine_version,
                    identity_normalized=request.normalize_identity,
                    # The report's copy, which is already redacted and deduplicated. The
                    # bundle redacts warnings itself as well, and handing it the raw list
                    # would have the same removals counted twice.
                    warnings=report.collection_warnings,
                )
                data = bundle.to_zip_bytes()
        except OSError as err:
            details = f"Attempted to collect a diagnostics bundle. Failed because it could not be assembled: {err}"
            logger.error(details)
            return CollectDiagnosticsResultFailure(result_details=details)

        file_name = request.file_name or self._default_bundle_file_name(report)
        if request.output_path is not None:
            return await self._write_bundle_to_path(request.output_path, file_name, data, manifest)
        return self._write_bundle_to_static_files(file_name, data, manifest)

    async def _write_bundle_to_path(
        self, output_path: str, file_name: str, data: bytes, manifest: DiagnosticsBundleManifest
    ) -> CollectDiagnosticsResultSuccess | CollectDiagnosticsResultFailure:
        r"""Write a bundle to a path on the engine's machine.

        A directory takes the generated file name; anything else is treated as the file
        name to write. An existing file is never overwritten, because a bundle is
        evidence and the one already there may be the one someone is waiting for.

        Every path that reaches the user has the Windows long-path prefix taken off it.
        Canonicalizing for I/O adds ``\\?\`` on Windows, and a user told their bundle is at
        ``\\?\C:\Users\sam\bundle.zip`` cannot paste that anywhere useful.
        """
        destination = self._resolve_bundle_destination(output_path, file_name)

        # Extension coercion off: a zip is written as a zip, and a diagnostics bundle
        # renamed by format sniffing would break whatever the user is about to attach it to.
        result = await self.engine.ahandle_request(
            WriteFileRequest(
                file_path=str(destination),
                content=data,
                existing_file_policy=ExistingFilePolicy.CREATE_NEW,
                skip_metadata_injection=True,
                coerce_extension_to_match_bytes=False,
            )
        )
        if not isinstance(result, WriteFileResultSuccess):
            details = (
                f"Attempted to write the diagnostics bundle to '{strip_windows_long_path_prefix(destination)}'. "
                f"Failed because the file could not be written: {result.result_details}"
            )
            logger.error(details)
            return CollectDiagnosticsResultFailure(result_details=details)

        written_path = strip_windows_long_path_prefix(result.final_file_path)
        written_name = Path(written_path).name
        return CollectDiagnosticsResultSuccess(
            file_name=written_name,
            size_bytes=len(data),
            manifest=manifest,
            path=written_path,
            result_details=self._bundle_success_details(written_name, manifest, written_path),
        )

    def _resolve_bundle_destination(self, output_path: str, file_name: str) -> Path:
        """Turn a requested output path into the file the bundle is written as.

        Kept out of the async caller because it touches the filesystem to tell a
        directory from a file name.

        Canonicalized here even though ``WriteFileRequest`` would do it again, because a
        relative path has to be anchored before the directory test below and the write
        can agree on what it means. Left relative, ``-o .`` is tested against the working
        directory and then written relative to the workspace, so the bundle lands
        somewhere the user was not told about.
        """
        destination = canonicalize_for_io(output_path)
        if destination.is_dir():
            return destination / file_name
        return destination

    def _write_bundle_to_static_files(
        self, file_name: str, data: bytes, manifest: DiagnosticsBundleManifest
    ) -> CollectDiagnosticsResultSuccess | CollectDiagnosticsResultFailure:
        """Write a bundle to the engine's static files and return a link to it.

        Goes through the static files manager rather than writing a path directly, so a
        bundle collected from an engine running on another machine is still downloadable.
        """
        try:
            url = self.engine.static_files_manager.save_static_file(
                data,
                file_name,
                ExistingFilePolicy.CREATE_NEW,
                skip_metadata_injection=True,
            )
        except (OSError, RuntimeError) as err:
            details = (
                f"Attempted to save the diagnostics bundle as '{file_name}'. "
                f"Failed because it could not be written: {err}"
            )
            logger.error(details)
            return CollectDiagnosticsResultFailure(result_details=details)

        written_name = self._file_name_from_url(url, fallback=file_name)

        return CollectDiagnosticsResultSuccess(
            file_name=written_name,
            size_bytes=len(data),
            manifest=manifest,
            url=url,
            result_details=self._bundle_success_details(written_name, manifest, url),
        )

    def _file_name_from_url(self, url: str, fallback: str) -> str:
        """Return the file name a download URL points at, or ``fallback`` when it has none.

        Needed because the static files manager hands back a URL rather than the name it
        wrote, and the name it wrote is not always the name it was given: bundles are
        saved with ``CREATE_NEW``, so an existing one is kept and this one becomes
        ``..._1.zip``. Reporting the requested name would point support at the older
        bundle.
        """
        path = PurePosixPath(unquote(urlparse(url).path))
        if not path.name:
            return fallback
        return path.name

    def _bundle_success_details(self, file_name: str, manifest: DiagnosticsBundleManifest, location: str) -> str:
        """Describe a collected bundle, including how much of it was hidden."""
        return (
            f"Successfully collected a diagnostics bundle '{file_name}' holding {len(manifest.entries)} file(s) "
            f"with {manifest.redaction.total} value(s) redacted, and wrote it to {location}."
        )

    async def _build_report(
        self, redactor: Redactor, warnings: list[str], *, normalize_identity: bool
    ) -> DiagnosticsReport | None:
        """Assemble the report, or None when the engine's own identity cannot be established.

        Shared by both handlers so a bundle and a bare report can never disagree. The
        redactor and warning list are the caller's, so a bundle's counts and warnings cover
        the files it staged before calling this.
        """
        # Engine and host are the only sections that can fail the whole thing: a report
        # that cannot say what is running, on what, identifies nothing.
        try:
            engine_section = await self._build_engine_section(redactor)
            host_section = self._build_host_section(redactor)
        except OSError:
            logger.warning("Could not identify the engine's host while building a diagnostics report.", exc_info=True)
            return None

        report = DiagnosticsReport(
            generated_at=datetime.now(UTC).isoformat(),
            engine=engine_section,
            host=host_section,
            paths=self._build_paths_section(redactor, warnings),
            config=self._build_config_section(redactor),
            secrets=self._build_secrets_section(warnings),
            libraries=self._build_libraries_section(redactor, warnings),
            projects=await self._build_projects_section(redactor, warnings),
            logs=self._build_logs_section(redactor, warnings),
            session=await self._build_session_section(redactor, warnings),
        )

        # Both set last: the counts have to cover everything above, and a warning can be
        # raised by any section. Deduplicated because one unresolvable path can be hit by
        # more than one section, and a repeated warning reads as more than one problem.
        #
        # Redacted like anything else, and before the counts are read. Most of these
        # warnings quote an OSError, whose text carries the absolute path it failed on --
        # which is how the home directory would otherwise reach a report that promises it
        # has been replaced with `~`.
        report.collection_warnings = [redactor.redact_text(warning) for warning in dict.fromkeys(warnings)]
        report.redaction = self._redaction_summary(redactor, normalize_identity=normalize_identity)
        return report

    def _redaction_summary(self, redactor: Redactor, *, normalize_identity: bool) -> RedactionSummary:
        """Snapshot what a redactor has removed so far."""
        return RedactionSummary(
            identity_normalized=normalize_identity,
            total=redactor.total_redactions(),
            counts=redactor.counts(),
        )

    async def _run_health_checks(self, report: DiagnosticsReport) -> HealthReport:
        """Judge a report, handing the checks the one thing a report cannot hold.

        The Griptape Cloud key is passed in so the connection check can actually connect.
        It is used to open a socket and never written to the health report.
        """
        context = HealthCheckContext(report=report, cloud_api_key=self._cloud_api_key())
        return await run_health_checks(context)

    def _cloud_api_key(self) -> str | None:
        """Return the Griptape Cloud key for the connection check, or None when it cannot be read.

        Reading a secret resolves the workspace, so a workspace that has gone missing
        raises here. It also parses both ``.env`` files, and a file saved in another
        encoding raises ``UnicodeDecodeError`` rather than an ``OSError``. Both are exactly
        the situation these checks exist to report, so either must cost the connection
        check rather than the whole run.
        """
        try:
            return self.engine.secrets_manager.get_secret(CLOUD_API_KEY_NAME, should_error_on_not_found=False)
        except (OSError, UnicodeDecodeError):
            logger.warning(
                "Could not read the Griptape Cloud API key while running health checks.",
                exc_info=True,
            )
            return None

    def _stage_logs(self, bundle: DiagnosticsBundle, warnings: list[str]) -> None:
        """Add this session's log and the log files on disk to a bundle."""
        session_lines = session_log_lines()
        if session_lines:
            bundle.add_session_log(session_lines)
        else:
            warnings.append(self._missing_session_log_warning())

        log_files = [path for directory in self._log_directories() for path in find_log_files(directory)]
        if not log_files:
            warnings.append(self._missing_log_files_warning())
            return

        bundle.add_log_files(log_files, warnings)

    def _log_directories(self) -> list[Path]:
        """Return every directory holding this engine's logs, the configured one first.

        Normally just the one. The two differ when the configured directory could not be
        created or opened: ``log_capture`` keeps the sink it already has rather than dropping
        file logging altogether, so the file the engine is writing right now lives somewhere
        the config no longer names. Searching only the configured directory left the current
        session's log out of the bundle collected to explain it.
        """
        directories = [canonicalize_for_identity(self.engine.config_manager.log_directory)]

        active = active_log_file()
        if active is not None:
            directories.append(canonicalize_for_identity(active.parent))

        # Canonicalized so the usual case -- the same directory, spelled two ways -- collapses
        # to one entry rather than staging every log file twice.
        return list(dict.fromkeys(directories))

    def _missing_session_log_warning(self) -> str:
        """Explain an absent session log in terms of which of its two causes applies."""
        buffer_lines = self.engine.config_manager.get_config_value(
            SESSION_LOG_BUFFER_LINES_KEY, default=DEFAULT_BUFFER_LINES, cast_type=int
        )

        if buffer_lines <= 0:
            return (
                "There is no session log in this bundle, because the 'logging.session_log_buffer_lines' "
                f"setting is {buffer_lines} and nothing this engine logged was kept. Set it to "
                f"{DEFAULT_BUFFER_LINES} to have future sessions recorded."
            )

        return (
            "This engine has not logged anything yet, so there is no session log in this bundle. "
            "Any log files from earlier sessions are still included."
        )

    def _missing_log_files_warning(self) -> str:
        """Explain an empty log directory without blaming a setting that is switched on."""
        log_to_file = self.engine.config_manager.get_config_value(LOG_TO_FILE_KEY, default=True, cast_type=bool)

        if not log_to_file:
            return (
                "No engine log files are in this bundle, because the 'logging.log_to_file' setting is off. "
                "Turn it on to have future sessions written to file."
            )

        return (
            "The 'logging.log_to_file' setting is on, but no engine log files were found. The log directory "
            "most likely cannot be written to."
        )

    def _stage_current_workflow(self, bundle: DiagnosticsBundle, warnings: list[str]) -> None:
        """Add the open workflow's saved file to a bundle, when there is one."""
        context_manager = self.engine.context_manager
        if not context_manager.has_current_workflow():
            warnings.append("No workflow was open, so none is in this bundle.")
            return

        file_path = context_manager.get_current_workflow_file_path()
        if file_path is None:
            warnings.append(
                "The open workflow has never been saved, so it is not in this bundle. "
                "Save it and collect again to include it."
            )
            return

        workflow_path = Path(file_path)
        if not workflow_path.exists():
            warnings.append(
                f"The open workflow's file '{workflow_path.name}' is no longer on disk, so it is not in this bundle."
            )
            return

        bundle.add_workflow(workflow_path, warnings)

    def _default_bundle_file_name(self, report: DiagnosticsReport) -> str:
        """Return a bundle file name that sorts by time and names the engine version.

        Deliberately short: this is a name someone reads out loud and attaches to an
        email, so the report's full ISO timestamp is reduced to date and time in UTC.
        """
        stamp = datetime.fromisoformat(report.generated_at).strftime("%Y%m%d-%H%M%S")
        version = report.engine.engine_version or "unknown"
        return f"griptape-nodes-diagnostics-{version}-{stamp}.zip"

    async def _build_engine_section(self, redactor: Redactor) -> EngineDiagnostics:
        """Describe the engine and the interpreter running it."""
        install_source, commit_id = get_install_source()

        return EngineDiagnostics(
            engine_id=self.engine.engine_identity_manager.engine_id,
            # Free text the user chose, and people name an engine after themselves or the
            # machine it runs on, so it is redacted like every other field they authored.
            engine_name=redactor.redact_text(self.engine.engine_identity_manager.engine_name),
            engine_version=await self._resolve_engine_version(),
            session_id=self.engine.session_manager.active_session_id,
            python_version=sys.version,
            python_executable=redactor.redact_path(sys.executable),
            process_id=os.getpid(),
            install_source=install_source,
            commit_id=commit_id,
        )

    def _build_host_section(self, redactor: Redactor) -> HostDiagnostics:
        """Describe the machine, including free space on the workspace volume."""
        disk_free_gb = None
        disk_total_gb = None
        workspace = self._workspace_path()
        if workspace is not None:
            # Walk up to an existing ancestor: the workspace directory may not have been
            # created yet, and the volume is what is being measured either way.
            probe = workspace
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            try:
                disk_info = self.engine.os_manager.get_disk_space_info(probe)
            except OSError:
                logger.debug("Could not read disk space for '%s'.", probe, exc_info=True)
            else:
                disk_free_gb = round(disk_info.free / _BYTES_PER_GB, 2)
                disk_total_gb = round(disk_info.total / _BYTES_PER_GB, 2)

        return HostDiagnostics(
            system=platform.system(),
            release=platform.release(),
            # The version string embeds the build host on some platforms, so it is
            # redacted like any other free text.
            version=redactor.redact_text(platform.version()),
            machine=platform.machine(),
            processor=platform.processor() or None,
            cpu_count=os.cpu_count(),
            workspace_disk_free_gb=disk_free_gb,
            workspace_disk_total_gb=disk_total_gb,
        )

    def _build_paths_section(self, redactor: Redactor, warnings: list[str]) -> PathDiagnostics:
        """List where the engine reads and writes, noting which of those do not exist."""
        candidates: dict[str, Path | None] = {
            "workspace_directory": self._workspace_path(),
            "config_directory": USER_CONFIG_PATH.parent,
            "user_config_file": USER_CONFIG_PATH,
            "global_env_file": ENV_VAR_PATH,
            "workspace_env_file": self._workspace_env_path(warnings),
            "libraries_directory": self._resolved_path_setting("libraries_directory", warnings),
            "static_files_directory": self._resolved_path_setting("static_files_directory", warnings),
            "log_directory": self.engine.config_manager.log_directory,
        }

        missing = [redactor.redact_path(path) for path in candidates.values() if path is not None and not path.exists()]

        redacted = {name: redactor.redact_path(path) if path is not None else None for name, path in candidates.items()}
        return PathDiagnostics(
            **redacted,
            missing_paths=missing,
            workspace_writable=self._is_workspace_writable(candidates["workspace_directory"]),
        )

    def _is_workspace_writable(self, workspace: Path | None) -> bool | None:
        """Report whether the workspace can be written to, or None when it does not exist.

        Checked with an access test rather than a trial write: a diagnostics collection
        must not create anything in the user's workspace.
        """
        if workspace is None or not workspace.exists():
            return None
        return os.access(workspace, os.W_OK)

    def _build_config_section(self, redactor: Redactor) -> ConfigDiagnostics:
        """Report the merged settings, the files behind them, and the env vars overriding them."""
        config_manager = self.engine.config_manager

        files: list[ConfigFileDiagnostics] = []
        for entry in config_manager.config_file_layers:
            size_bytes = None
            exists = entry.path.exists()
            if exists:
                try:
                    size_bytes = entry.path.stat().st_size
                except OSError:
                    logger.debug("Could not read the size of config file '%s'.", entry.path, exc_info=True)
            files.append(
                ConfigFileDiagnostics(
                    path=redactor.redact_path(entry.path),
                    layer=entry.layer,
                    exists=exists,
                    size_bytes=size_bytes,
                )
            )

        return ConfigDiagnostics(
            files=files,
            environment_overrides=sorted(name for name in os.environ if name.startswith(CONFIG_ENV_VAR_PREFIX)),
            merged=redactor.redact_config(config_manager.merged_config),
        )

    def _build_secrets_section(self, warnings: list[str]) -> list[SecretDiagnostics]:
        """Report which secrets exist and where, never what they are.

        The redactor is deliberately not involved: nothing here is derived from a secret
        value, only from its key name and whether a value is present. That is the whole
        guarantee, so it is enforced by never reading the value into the report at all.
        """
        declared_names = self._declared_secret_names()
        layers_by_source = self._collect_secret_layers(declared_names, warnings)

        # Highest priority first, matching the order SecretsManager.get_secret searches.
        layers = [
            SecretLayer(label="environment variable", keys_set=layers_by_source.os_environment),
            SecretLayer(label="workspace .env", keys_set=layers_by_source.workspace_env_file),
            SecretLayer(label="global .env", keys_set=layers_by_source.global_env_file),
        ]

        entries: list[SecretDiagnostics] = []
        for name in sorted({name for layer in layers for name in layer.keys_set} | declared_names):
            sources: list[str] = []
            effective_source = None
            is_set = False

            for layer in layers:
                if name not in layer.keys_set:
                    continue
                is_first_source = not sources
                sources.append(layer.label)
                # The highest-priority source wins outright, even when its value is empty:
                # a key blanked in the workspace .env really does shadow a working one in
                # the global .env, and saying so is the point of this section.
                if is_first_source:
                    is_set = layer.keys_set[name]
                    if is_set:
                        effective_source = layer.label

            entries.append(
                SecretDiagnostics(
                    name=name,
                    is_set=is_set,
                    effective_source=effective_source,
                    sources=sources,
                    declared_in_config=name in declared_names,
                )
            )

        return entries

    def _build_libraries_section(self, redactor: Redactor, warnings: list[str]) -> list[LibraryDiagnostics]:
        """Report every library the engine tried to load, and everything that went wrong."""
        library_manager = self.engine.library_manager

        entries: list[LibraryDiagnostics] = []
        for library_file_path in library_manager.get_libraries_attempted_to_load():
            try:
                lib_info = library_manager.get_library_info_for_attempted_load(library_file_path)
            except KeyError:
                # The library was unregistered between listing and reading. One library
                # dropping out must not cost the rest of the section.
                warnings.append(f"Library at '{library_file_path}' could not be read; it is missing from the report.")
                continue

            entries.append(
                LibraryDiagnostics(
                    # Falls back to the path: a library that failed before its metadata
                    # was parsed has no name, and that library is usually the reason a
                    # report is being collected.
                    name=lib_info.library_name or redactor.redact_path(lib_info.library_path),
                    version=lib_info.library_version,
                    path=redactor.redact_path(lib_info.library_path),
                    fitness=lib_info.fitness.value,
                    lifecycle_state=lib_info.lifecycle_state.value,
                    enabled=lib_info.enabled,
                    is_sandbox=lib_info.is_sandbox,
                    requires_worker=lib_info.requires_worker,
                    # None means "no worker was ever started for this library", which is
                    # different from "a worker was started and has not come up".
                    worker_ready=lib_info.worker_ready.is_set() if lib_info.worker_ready is not None else None,
                    registered_path=(
                        redactor.redact_path(lib_info.registered_path) if lib_info.registered_path else None
                    ),
                    problems=self._collated_problems(lib_info, redactor),
                )
            )

        return sorted(entries, key=lambda entry: entry.name)

    async def _build_projects_section(self, redactor: Redactor, warnings: list[str]) -> list[ProjectDiagnostics]:
        """Report every project template the engine tried to load, and which one is active."""
        list_result = await self.engine.ahandle_request(
            ListProjectTemplatesRequest(include_system_builtins=True, broadcast_result=False)
        )
        if not isinstance(list_result, ListProjectTemplatesResultSuccess):
            warnings.append(f"Project templates could not be listed: {list_result.result_details}")
            return []

        current_project_id = await self._resolve_current_project_id()

        entries = [
            self._build_project_entry(info, redactor, current_project_id=current_project_id, loaded=True)
            for info in list_result.successfully_loaded
        ]
        entries.extend(
            self._build_project_entry(info, redactor, current_project_id=current_project_id, loaded=False)
            for info in list_result.failed_to_load
        )
        return entries

    def _build_logs_section(self, redactor: Redactor, warnings: list[str]) -> LogDiagnostics:
        """Report how logging is configured and which log files exist."""
        config_manager = self.engine.config_manager
        log_directory = config_manager.log_directory
        directories = self._log_directories()

        files: list[LogFileDiagnostics] = []
        current_file = active_log_file()
        for directory in directories:
            for path in find_log_files(directory):
                try:
                    stat = path.stat()
                except OSError:
                    warnings.append(f"Log file '{path.name}' could not be read; it is missing from the report.")
                    continue
                files.append(
                    LogFileDiagnostics(
                        # Name only: the directory is reported once above, and repeating it
                        # per file would just be more path to redact.
                        name=path.name,
                        size_bytes=stat.st_size,
                        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                        is_active=current_file is not None and path == current_file,
                    )
                )

        # Only when it is somewhere other than the configured directory, which is the whole
        # reason to mention it. `_log_directories` puts the configured one first.
        active_directory = None
        if len(directories) > 1:
            active_directory = redactor.redact_path(directories[1])

        return LogDiagnostics(
            log_level=str(config_manager.get_config_value("log_level", default="INFO")),
            log_to_file=config_manager.get_config_value(LOG_TO_FILE_KEY, default=True, cast_type=bool),
            log_directory=redactor.redact_path(log_directory),
            active_log_directory=active_directory,
            retention_days=config_manager.get_config_value(LOG_RETENTION_DAYS_KEY, default=7, cast_type=int),
            session_buffer_lines=config_manager.get_config_value(
                SESSION_LOG_BUFFER_LINES_KEY, default=DEFAULT_BUFFER_LINES, cast_type=int
            ),
            session_lines_captured=len(session_log_lines()),
            files=files,
        )

    async def _build_session_section(self, redactor: Redactor, warnings: list[str]) -> SessionDiagnostics:
        """Report what the engine currently has open."""
        context_manager = self.engine.context_manager
        object_manager = self.engine.object_manager

        workflow_name = None
        workflow_path = None
        if context_manager.has_current_workflow():
            # Redacted even though it is a "name": a workflow's registry key is derived from
            # its path, and `WorkflowManager._workspace_relative_path` falls back to the
            # absolute path for a workflow saved outside the workspace. So the name of a
            # workflow saved to the desktop is `/Users/sam/Desktop/flow`, and it would carry
            # the home directory into a report that promises to have taken it out.
            workflow_name = redactor.redact_text(context_manager.get_current_workflow_name())
            current_path = context_manager.get_current_workflow_file_path()
            if current_path is not None:
                workflow_path = redactor.redact_path(current_path)

        registered_count = 0
        workflows_result = await self.engine.ahandle_request(ListAllWorkflowsRequest(broadcast_result=False))
        if isinstance(workflows_result, ListAllWorkflowsResultSuccess):
            registered_count = len(workflows_result.workflows)
        else:
            warnings.append(f"Registered workflows could not be counted: {workflows_result.result_details}")

        return SessionDiagnostics(
            current_workflow_name=workflow_name,
            current_workflow_path=workflow_path,
            flow_count=len(object_manager.get_filtered_subset(type=ControlFlow)),
            node_count=len(object_manager.get_filtered_subset(type=BaseNode)),
            registered_workflow_count=registered_count,
        )

    def _build_project_entry(
        self,
        info: ProjectTemplateInfo,
        redactor: Redactor,
        *,
        current_project_id: str | None,
        loaded: bool,
    ) -> ProjectDiagnostics:
        """Convert one project template's info into a report entry."""
        problems = [
            ProjectProblemDiagnostics(
                severity=str(problem.severity),
                field_path=problem.field_path,
                message=redactor.redact_text(problem.message),
                line_number=problem.line_number,
            )
            for problem in info.validation.problems
        ]

        return ProjectDiagnostics(
            project_id=info.project_id,
            # A project's name is the user's own words, like the engine's. Absent for a
            # template whose body could not be read, which is not the same as an empty name.
            name=redactor.redact_text(info.name) if info.name else None,
            parent_project_id=info.parent_project_id,
            path=redactor.redact_path(info.project_file_path) if info.project_file_path else None,
            is_current=current_project_id is not None and info.project_id == current_project_id,
            loaded=loaded,
            validation_status=str(info.validation.status),
            engine_version_compatible=info.engine_version_compatible,
            required_engine_version=info.required_engine_version,
            workspace_directory=redactor.redact_path(info.workspace_dir) if info.workspace_dir else None,
            libraries_root=redactor.redact_path(info.libraries_root) if info.libraries_root else None,
            problems=problems,
        )

    def _known_secret_values(self) -> list[str]:
        """Return the secret values the engine holds, so they can be found in free text.

        These are used to build search patterns and are never written anywhere. The
        engine is the only thing that knows them, which makes it the only thing that can
        scrub them out of a log line a library wrote.

        Both places a value can live are read: the ``.env`` files, and the environment for
        the names the engine expects to find. A key exported in a shell or set by a
        container never appears in a ``.env`` file, and it is the same credential -- so
        reading only the files would scrub the keys of a user who stores them the usual way
        and miss the keys of one who does not.

        Only names the engine already knows about are looked up in the environment. Taking
        every variable would add hundreds of ordinary values -- ``PATH``, ``HOME``,
        ``TERM`` -- and each one becomes another search-and-replace over every log line in
        the bundle.
        """
        file_values = self._env_file_secret_values()
        expected_names = set(file_values) | self._declared_secret_names()

        # A set because the engine copies .env entries into the environment at startup, so
        # most values are found twice, and one search pattern per value is enough. Sorted so
        # a report built twice from the same state redacts in the same order.
        values = {value for value in file_values.values() if value}
        values.update(value for name in expected_names if (value := os.environ.get(name)))
        return sorted(values)

    def _env_file_secret_values(self) -> dict[str, str]:
        """Return the merged ``.env`` contents, or an empty mapping when they cannot be read."""
        try:
            return self.engine.secrets_manager._read_merged_env_files()
        except (OSError, UnicodeDecodeError):
            # An unreadable .env file means less thorough scrubbing, not a failed report.
            # The generic credential patterns still apply.
            logger.warning(
                "Could not read the .env files while preparing redaction. Known secret values will not be "
                "scrubbed from log text; pattern-based redaction still applies.",
                exc_info=True,
            )
            return {}

    def _declared_secret_names(self) -> set[str]:
        """Return the secret key names the config asks the engine to register."""
        declared = self.engine.config_manager.get_config_value(SECRETS_TO_REGISTER_KEY, default={})
        return set(normalize_secrets_to_register(declared))

    def _collect_secret_layers(self, declared_names: set[str], warnings: list[str]) -> SecretLayers:
        """Return, per source, which secret keys are present and whether each has a value.

        The only method in this manager that holds a secret value, and it reduces every
        one to a boolean before returning. Values are read because there is no way to
        know whether a key is set without reading it, and because the environment layer
        can only be told apart by comparing.

        The engine copies every ``.env`` entry into ``os.environ`` at startup, so a key's
        mere presence there means nothing. An environment variable is only reported when
        its value differs from what the files say, which is exactly the case worth
        reporting: a shell export or container variable silently beating the ``.env`` the
        user has been editing.
        """
        workspace_values = self._read_env_file(self._workspace_env_path(warnings), warnings)
        global_values = self._read_env_file(ENV_VAR_PATH, warnings)

        # Workspace beats global, matching SecretsManager._read_merged_env_files.
        file_values = {**global_values, **workspace_values}

        candidate_names = {*file_values, *declared_names}
        os_values = {
            name: bool(os.environ[name])
            for name in candidate_names
            if name in os.environ and os.environ[name] != file_values.get(name)
        }

        return SecretLayers(
            os_environment=os_values,
            workspace_env_file={name: bool(value) for name, value in workspace_values.items()},
            global_env_file={name: bool(value) for name, value in global_values.items()},
        )

    def _read_env_file(self, path: Path | None, warnings: list[str]) -> dict[str, str]:
        """Return the contents of a ``.env`` file, or an empty mapping when it cannot be read."""
        if path is None or not path.exists():
            return {}

        try:
            values = dotenv_values(path)
        except (OSError, UnicodeDecodeError) as err:
            # dotenv decodes as UTF-8, so a file saved in another encoding raises rather
            # than reading as mojibake. That is a real state for a hand-edited `.env`.
            warnings.append(f"Secrets file '{path.name}' could not be read: {err}")
            logger.warning("Could not read secrets file '%s' for the diagnostics report.", path, exc_info=True)
            return {}

        # dotenv reports a bare `FOO` (no `=`) as None. Treat it as declared-but-empty.
        return {name: value or "" for name, value in values.items()}

    def _collated_problems(self, lib_info: LibraryManager.LibraryInfo, redactor: Redactor) -> str | None:
        """Return a library's problems as the engine already formats them, redacted."""
        collated = self.engine.library_manager.collate_problems_for_lib_info(lib_info)
        if collated is None:
            return None
        return redactor.redact_text(collated)

    def _workspace_path(self) -> Path | None:
        """Return the workspace directory, or None when it cannot be resolved."""
        try:
            return self.engine.config_manager.workspace_path
        except OSError:
            logger.debug("Could not resolve the workspace path for the diagnostics report.", exc_info=True)
            return None

    def _workspace_env_path(self, warnings: list[str]) -> Path | None:
        """Return the workspace-level ``.env`` path, or None when it cannot be resolved."""
        try:
            return self.engine.secrets_manager.workspace_env_path
        except OSError as err:
            warnings.append(f"The workspace .env path could not be resolved: {err}")
            return None

    def _resolved_path_setting(self, key: str, warnings: list[str]) -> Path | None:
        """Resolve a workspace-relative directory setting to an absolute path.

        The read is guarded because a setting written as ``$SOME_VAR`` is resolved through
        the secrets manager, which resolves the workspace to find the ``.env`` beside it --
        and a workspace that has gone missing is one of the things this report is collected
        to explain.
        """
        try:
            configured = self.engine.config_manager.get_config_value(key, default=None)
        except OSError as err:
            warnings.append(f"The '{key}' setting could not be read: {err}")
            logger.warning("Could not read the '%s' setting for the diagnostics report.", key, exc_info=True)
            return None

        if configured is None:
            return None

        workspace = self._workspace_path()
        if workspace is None:
            warnings.append(f"The '{key}' setting could not be resolved because the workspace path is unavailable.")
            return None

        configured_path = Path(str(configured)).expanduser()
        if configured_path.is_absolute():
            return configured_path
        return workspace / configured_path

    async def _resolve_engine_version(self) -> str | None:
        """Resolve the engine version string, or None when it cannot be determined."""
        version_result = await self.engine.ahandle_request(GetEngineVersionRequest(broadcast_result=False))
        if isinstance(version_result, GetEngineVersionResultSuccess):
            return f"{version_result.major}.{version_result.minor}.{version_result.patch}"
        logger.warning(
            "Could not resolve engine version for the diagnostics report: %s",
            version_result.result_details,
        )
        return None

    async def _resolve_current_project_id(self) -> str | None:
        """Resolve the id of the project the engine is running under, or None when unknown."""
        result = await self.engine.ahandle_request(GetCurrentProjectRequest(broadcast_result=False))
        if isinstance(result, GetCurrentProjectResultSuccess):
            return result.project_info.project_id
        logger.debug("Could not resolve the current project for the diagnostics report: %s", result.result_details)
        return None
