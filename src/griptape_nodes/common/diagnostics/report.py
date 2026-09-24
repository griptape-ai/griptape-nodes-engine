"""Diagnostics report data model.

A serializable snapshot of everything about an engine worth knowing when something has gone
wrong: what version is running, on what machine, with which settings from which files, which
libraries loaded and which failed, and where the logs are.

Schema-versioned so a support tool can read a report from an older engine. Every value has
already passed through ``Redactor``, and the redaction counts travel with it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# Schema version for the report envelope. Bump when the shape changes so consumers
# can branch on it.
DIAGNOSTICS_REPORT_SCHEMA_VERSION = "0.1.0"


class RedactionSummary(BaseModel):
    """What was removed from this report before it was written.

    Attributes:
        identity_normalized: Whether the home directory and username were replaced.
            Defaults to False because it is a claim about what was removed: a summary that
            arrived without the field must not assert normalization nothing checked.
        counts: Number of values removed per reason (see ``RedactionReason``), omitting
            reasons that never fired.
    """

    identity_normalized: bool = False
    total: int = 0
    counts: dict[str, int] = Field(default_factory=dict)


class EngineDiagnostics(BaseModel):
    """The engine and the interpreter running it.

    Attributes:
        python_executable: Path to the interpreter, which reveals whether the engine runs
            from a virtual environment, a uv tool install, or a system Python.
        install_source: How the engine was installed (``pypi``, ``git``, ``file``, or
            ``unknown``). ``file`` means a local checkout, so the code running may not match
            any released version.
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
        workspace_disk_free_gb: Free space on the volume holding the workspace. The most
            common cause of a save or install failure.
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

    Every path is recorded whether or not it exists, because a path that is missing when it
    should not be is itself the answer.

    Attributes:
        workspace_env_file: The workspace-level ``.env`` file, which overrides the global one.
        workspace_writable: Whether the workspace directory can be written to. None when it
            does not exist, so "cannot be written to" is never confused with "is not there".
            A read-only workspace turns every save into a failure.
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
        contributes: Whether this layer feeds the merge. Lower than ``exists`` when the
            workspace directory *is* the project directory: both layers name the same file,
            and it is read once, as ``project``.
        parse_error: Why the file failed to parse, when it exists but is not valid JSON. None
            for a missing file, which tells "no such file" apart from "file is broken" -- a
            broken file is silently skipped, so nothing else reveals that a layer the user
            edited never applied.
    """

    path: str
    layer: str
    exists: bool
    contributes: bool = True
    parse_error: str | None = None
    size_bytes: int | None = None


class ConfigDiagnostics(BaseModel):
    """The settings the engine is actually running with, and where they came from.

    Attributes:
        files: The config files contributing to the merged settings, in ascending priority
            order. The order is the point: a setting that appears not to apply is usually
            being overridden by a later file.
        runtime_workspace_pin: The workspace directory the active project pinned, when that
            pin is a layer of its own rather than a value read out of the config files. No
            file holds it, so a settings write cannot reach it.
        environment_overrides: Names of the ``GTN_CONFIG_`` environment variables in effect,
            which override every file. Names only, never values.
    """

    files: list[ConfigFileDiagnostics] = Field(default_factory=list)
    runtime_workspace_pin: str | None = None
    environment_overrides: list[str] = Field(default_factory=list)
    merged: dict[str, Any] = Field(default_factory=dict)


class SecretDiagnostics(BaseModel):
    """Whether a secret is set, never what it is set to.

    Attributes:
        is_set: Whether the value the engine will actually use is non-empty. Follows the
            engine's own precedence, so a key set in one place and blanked in a
            higher-priority place reads as not set.
        sources: Every place holding a value for this key, highest priority first. More than
            one means a value is being shadowed -- a common cause of "I updated my key and
            nothing changed".
        declared_in_config: Whether the key is listed in the ``secrets_to_register`` setting.
            A key declared with no source is one the engine expects and cannot find.
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
        registered_path: The path as written in the user's ``libraries_to_register`` setting,
            before it was resolved, so a problem traces back to the config line behind it.
        problems: Everything that went wrong while loading, as the engine already reports it
            elsewhere. None when the library loaded cleanly.
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
        field_path: Which part of the template is at fault (e.g.
            ``situations.copy_external_file.macro``).
    """

    severity: str
    field_path: str
    message: str
    line_number: int | None = None


class ProjectDiagnostics(BaseModel):
    """One project template the engine tried to load.

    Attributes:
        path: Location of the template file, or None for templates that are not file-backed.
        validation_status: ``GOOD``, ``FLAWED``, ``UNUSABLE``, or ``MISSING``.
        engine_version_compatible: False when the project requires an engine version this one
            does not satisfy, which blocks activation.
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
        active_log_directory: Where the engine is actually writing, set only when that is
            somewhere else. It differs when the configured directory could not be created or
            opened and the engine kept the log file it already had open, which is worth saying
            out loud: the configured directory is unusable.
        retention_days: How long files are kept. 0 means forever.
        files: The log files present, newest first.
    """

    log_level: str | None = None
    log_to_file: bool = True
    log_directory: str | None = None
    active_log_directory: str | None = None
    retention_days: int = 0
    session_buffer_lines: int = 0
    session_lines_captured: int = 0
    files: list[LogFileDiagnostics] = Field(default_factory=list)


class SessionDiagnostics(BaseModel):
    """What the engine currently has open."""

    current_workflow_name: str | None = None
    current_workflow_path: str | None = None
    flow_count: int = 0
    node_count: int = 0
    registered_workflow_count: int = 0


class DiagnosticsReport(BaseModel):
    """A redacted snapshot of an engine's state, for troubleshooting.

    Attributes:
        secrets: Which secrets are set. Names and presence only.
        collection_warnings: Anything that could not be collected. A report is always produced
            even when part of it could not be gathered, so the reader is told which sections
            are incomplete rather than silently seeing them empty.
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
