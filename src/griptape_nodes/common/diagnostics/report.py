"""Diagnostics report data model.

A diagnostics report is a serializable snapshot of everything about an engine that
is worth knowing when something has gone wrong: what version is running, on what
machine, with which settings from which files, which libraries loaded and which
failed, and where the logs are.

The report is schema-versioned so a support tool can read a report from an older
engine. It is built to be handed to someone else, so every value in it has already
passed through ``Redactor`` and the redaction counts travel with it: a reader can
always tell the difference between a setting that is empty and a setting that was
hidden from them.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# Schema version for the report envelope. Bump when the shape changes so consumers
# can branch on it.
DIAGNOSTICS_REPORT_SCHEMA_VERSION = "0.1.0"


class RedactionSummary(BaseModel):
    """What was removed from this report before it was written.

    Attributes:
        identity_normalized: Whether the home directory was replaced with ``~`` and
            the username with ``<user>``.
        total: Total number of values removed.
        counts: Number of values removed per reason (see ``RedactionReason``).
            A reason that never fired is omitted.
    """

    identity_normalized: bool = True
    total: int = 0
    counts: dict[str, int] = Field(default_factory=dict)


class EngineDiagnostics(BaseModel):
    """The engine and the interpreter running it.

    Attributes:
        engine_id: Identifier of the engine that produced this report, when known.
        engine_name: Human-readable engine name, when set.
        engine_version: Engine version string (``major.minor.patch``), when known.
        session_id: Identifier of the active editor session, when one is connected.
        python_version: Full Python version string.
        python_executable: Path to the interpreter, which reveals whether the engine
            is running from a virtual environment, a uv tool install, or a system
            Python.
        process_id: OS process id, for matching this report against a log file.
        install_source: How the engine was installed (``pypi``, ``git``, ``file``, or
            ``unknown``). ``file`` means a local checkout, so the code running may not
            match any released version.
        commit_id: Short commit the engine was installed from, when it came from git.
    """

    engine_id: str | None = None
    engine_name: str | None = None
    engine_version: str | None = None
    session_id: str | None = None
    python_version: str
    python_executable: str
    process_id: int
    install_source: str | None = None
    commit_id: str | None = None


class HostDiagnostics(BaseModel):
    """The machine the engine is running on.

    Attributes:
        system: OS name (``Darwin``, ``Windows``, ``Linux``).
        release: OS release string.
        version: Detailed OS version string.
        machine: CPU architecture (``arm64``, ``x86_64``).
        processor: Processor description, when the platform reports one.
        cpu_count: Number of usable CPUs, when the platform reports one.
        workspace_disk_free_gb: Free space on the volume holding the workspace.
            The most common cause of a save or install failure.
        workspace_disk_total_gb: Total size of that volume.
    """

    system: str
    release: str
    version: str
    machine: str
    processor: str | None = None
    cpu_count: int | None = None
    workspace_disk_free_gb: float | None = None
    workspace_disk_total_gb: float | None = None


class PathDiagnostics(BaseModel):
    """Where the engine reads and writes, with existence checked.

    Every path is recorded whether or not it exists, because a path that is missing
    when it should not be is itself the answer.

    Attributes:
        workspace_directory: Root of the current workspace.
        config_directory: Directory holding the user config file and the global ``.env``.
        user_config_file: The user config file.
        global_env_file: The global ``.env`` file holding secrets.
        workspace_env_file: The workspace-level ``.env`` file, which overrides the global one.
        libraries_directory: Where downloaded libraries are installed.
        static_files_directory: Where generated assets are written.
        log_directory: Where engine log files are written.
        missing_paths: The subset of the above that do not exist on disk.
        workspace_writable: Whether the workspace directory can be written to. None when
            it does not exist, so "cannot be written to" is never confused with "is not
            there". Everything a user makes is saved here, so a read-only workspace turns
            every save into a failure.
    """

    workspace_directory: str | None = None
    config_directory: str | None = None
    user_config_file: str | None = None
    global_env_file: str | None = None
    workspace_env_file: str | None = None
    libraries_directory: str | None = None
    static_files_directory: str | None = None
    log_directory: str | None = None
    missing_paths: list[str] = Field(default_factory=list)
    workspace_writable: bool | None = None


class ConfigFileDiagnostics(BaseModel):
    """One config file in the precedence chain.

    Attributes:
        path: Location of the file.
        layer: Which layer it supplies (``user``, ``project``, or ``workspace``).
        exists: Whether the file is present on disk.
        size_bytes: Size of the file, when it exists.
    """

    path: str
    layer: str
    exists: bool
    size_bytes: int | None = None


class ConfigDiagnostics(BaseModel):
    """The settings the engine is actually running with, and where they came from.

    Attributes:
        files: The config files contributing to the merged settings, in ascending
            priority order. The order is the point: a setting that appears not to
            apply is usually being overridden by a later file.
        environment_overrides: Names of the ``GTN_CONFIG_`` environment variables in
            effect, which override every file. Names only, never values.
        merged: The fully merged settings, with credential values removed.
    """

    files: list[ConfigFileDiagnostics] = Field(default_factory=list)
    environment_overrides: list[str] = Field(default_factory=list)
    merged: dict = Field(default_factory=dict)


class SecretDiagnostics(BaseModel):
    """Whether a secret is set, never what it is set to.

    Attributes:
        name: The secret's key name.
        is_set: Whether the value the engine will actually use is non-empty. Follows
            the same precedence the engine follows when it reads a secret, so a key
            set in one place and blanked in a higher-priority place reads as not set.
        effective_source: Where the value the engine will use comes from, or None when
            the key has no value anywhere.
        sources: Every place holding a value for this key, highest priority first
            (``environment variable``, ``workspace .env``, ``global .env``). More than
            one means a value is being shadowed, which is a common cause of "I updated
            my key and nothing changed".
        declared_in_config: Whether the key is listed in the ``secrets_to_register``
            setting. A key that is declared but has no source is one the engine expects
            and cannot find.
    """

    name: str
    is_set: bool
    effective_source: str | None = None
    sources: list[str] = Field(default_factory=list)
    declared_in_config: bool = False


class LibraryDiagnostics(BaseModel):
    """One library the engine tried to load, and how that went.

    Attributes:
        name: Registered library name, or the path when the name could not be read.
        version: Library version, when available.
        path: Path to the library's ``griptape_nodes_library.json``, when known.
        fitness: Whether the library is usable, and how usable.
        lifecycle_state: Where the library got to in the load sequence.
        enabled: Whether the library is enabled.
        is_sandbox: Whether this is the sandbox library rather than an installed one.
        requires_worker: Whether the library's nodes run in a separate worker process.
        worker_ready: Whether that worker is up, when one is required.
        registered_path: The path as written in the user's ``libraries_to_register``
            setting, before it was resolved. Lets a problem be traced back to the exact
            config line that caused it.
        problems: Everything that went wrong while loading, as the engine already
            reports it elsewhere. The single most useful field in the report. None when
            the library loaded cleanly.
    """

    name: str
    version: str | None = None
    path: str | None = None
    fitness: str | None = None
    lifecycle_state: str | None = None
    enabled: bool = True
    is_sandbox: bool = False
    requires_worker: bool = False
    worker_ready: bool | None = None
    registered_path: str | None = None
    problems: str | None = None


class ProjectProblemDiagnostics(BaseModel):
    """One validation problem found in a project template.

    Attributes:
        severity: ``error`` or ``warning``.
        field_path: Which part of the template is at fault (e.g.
            ``situations.copy_external_file.macro``).
        message: What is wrong with it.
        line_number: Line in the template file, when known.
    """

    severity: str
    field_path: str
    message: str
    line_number: int | None = None


class ProjectDiagnostics(BaseModel):
    """One project template the engine tried to load.

    Attributes:
        project_id: Opaque identifier for the template.
        name: Display name, when available.
        parent_project_id: Opaque id of the parent template, or None when it has no parent.
        path: Location of the template file, or None for templates that are not file-backed.
        is_current: Whether this is the project the engine is currently running under.
        loaded: Whether the template loaded successfully.
        validation_status: ``GOOD``, ``FLAWED``, ``UNUSABLE``, or ``MISSING``.
        engine_version_compatible: False when the project requires an engine version
            this one does not satisfy, which blocks activation.
        required_engine_version: The version specifier the project declares, when any.
        workspace_directory: Workspace the project activates with, when resolvable.
        libraries_root: Where the project's libraries install from, when resolvable.
        problems: Validation problems found in the template.
    """

    project_id: str
    name: str | None = None
    parent_project_id: str | None = None
    path: str | None = None
    is_current: bool = False
    loaded: bool = True
    validation_status: str | None = None
    engine_version_compatible: bool = True
    required_engine_version: str | None = None
    workspace_directory: str | None = None
    libraries_root: str | None = None
    problems: list[ProjectProblemDiagnostics] = Field(default_factory=list)


class LogFileDiagnostics(BaseModel):
    """One engine log file available for collection.

    Attributes:
        name: File name, without the directory.
        size_bytes: Size of the file.
        modified_at: ISO 8601 timestamp (UTC) of the last write.
        is_active: Whether this is the file the reporting engine is writing to.
    """

    name: str
    size_bytes: int
    modified_at: str
    is_active: bool = False


class LogDiagnostics(BaseModel):
    """How logging is configured and what log history exists.

    Attributes:
        log_level: The configured verbosity, which bounds what any log can contain.
        log_to_file: Whether the engine is writing log files.
        log_directory: Where those files go.
        retention_days: How long files are kept. 0 means forever.
        session_buffer_lines: How many lines are held in memory for this session.
        session_lines_captured: How many are held right now.
        files: The log files present, newest first.
    """

    log_level: str | None = None
    log_to_file: bool = True
    log_directory: str | None = None
    retention_days: int = 0
    session_buffer_lines: int = 0
    session_lines_captured: int = 0
    files: list[LogFileDiagnostics] = Field(default_factory=list)


class SessionDiagnostics(BaseModel):
    """What the engine currently has open.

    Attributes:
        current_workflow_name: The workflow in the current context, when there is one.
        current_workflow_path: The file that workflow was loaded from, when it has one.
        flow_count: Number of flows loaded.
        node_count: Number of nodes loaded.
        registered_workflow_count: Number of workflows registered with the engine.
    """

    current_workflow_name: str | None = None
    current_workflow_path: str | None = None
    flow_count: int = 0
    node_count: int = 0
    registered_workflow_count: int = 0


class DiagnosticsReport(BaseModel):
    """A redacted snapshot of an engine's state, for troubleshooting.

    Attributes:
        schema_version: Version of the report envelope.
        generated_at: ISO 8601 timestamp (UTC) of when the report was built.
        redaction: What was removed before this report was written.
        engine: The engine and interpreter.
        host: The machine.
        paths: Where the engine reads and writes.
        config: The settings in effect and the files they came from.
        secrets: Which secrets are set. Names and presence only.
        libraries: Every library the engine tried to load.
        projects: Loaded project templates.
        logs: Logging configuration and available log history.
        session: What the engine currently has open.
        collection_warnings: Anything that could not be collected. A report is always
            produced even when part of it could not be gathered, so the reader is told
            which sections are incomplete rather than silently seeing them empty.
    """

    schema_version: str = DIAGNOSTICS_REPORT_SCHEMA_VERSION
    generated_at: str
    redaction: RedactionSummary = Field(default_factory=RedactionSummary)
    engine: EngineDiagnostics
    host: HostDiagnostics
    paths: PathDiagnostics = Field(default_factory=PathDiagnostics)
    config: ConfigDiagnostics = Field(default_factory=ConfigDiagnostics)
    secrets: list[SecretDiagnostics] = Field(default_factory=list)
    libraries: list[LibraryDiagnostics] = Field(default_factory=list)
    projects: list[ProjectDiagnostics] = Field(default_factory=list)
    logs: LogDiagnostics = Field(default_factory=LogDiagnostics)
    session: SessionDiagnostics = Field(default_factory=SessionDiagnostics)
    collection_warnings: list[str] = Field(default_factory=list)
