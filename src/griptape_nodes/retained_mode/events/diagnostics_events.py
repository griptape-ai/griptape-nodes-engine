"""Events for collecting troubleshooting context about a running engine."""

from __future__ import annotations

from dataclasses import dataclass

from griptape_nodes.common.diagnostics.bundle import DiagnosticsBundleManifest
from griptape_nodes.common.diagnostics.health import HealthReport
from griptape_nodes.common.diagnostics.report import DiagnosticsReport
from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry


@dataclass
@PayloadRegistry.register
class GetDiagnosticsReportRequest(RequestPayload):
    """Collect a snapshot of the engine's state for troubleshooting.

    Use when: Someone is reporting a problem and needs to say what version they are
    running, on what machine, with which settings, and which libraries failed to
    load. Also used by `gtn self info` to display that state, and by
    CollectDiagnosticsRequest as the manifest of a bundle.

    Everything in the result has already been redacted: secret values are never
    included, credential-shaped config values are removed, and the home directory
    and username are normalized unless `normalize_identity` is turned off. What was
    removed is reported as counts, so a reader can tell a hidden value from an
    absent one.

    Nothing is written to disk. Use CollectDiagnosticsRequest for that.

    Args:
        normalize_identity: Replace the home directory with `~` and the username with
            `<user>`. Leave on unless you are reading the report yourself and need the
            real paths.

    Results: GetDiagnosticsReportResultSuccess | GetDiagnosticsReportResultFailure
    """

    normalize_identity: bool = True


@dataclass
@PayloadRegistry.register
class GetDiagnosticsReportResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Diagnostics report collected successfully.

    A report succeeds even when parts of it could not be gathered; the sections that
    failed are named in `report.collection_warnings`. A report that is missing a
    section is still worth having, and the reason a section is missing is often the
    problem being investigated.

    Args:
        report: The redacted snapshot of the engine's state.
    """

    report: DiagnosticsReport


@dataclass
@PayloadRegistry.register
class GetDiagnosticsReportResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Diagnostics report collection failed.

    Common causes: the engine's configuration could not be read at all, or the host
    platform could not be identified. Individual sections failing does not cause
    this; they are reported as warnings on a successful result instead.
    """


@dataclass
@PayloadRegistry.register
class CollectDiagnosticsRequest(RequestPayload):
    """Collect a diagnostics bundle: one zip holding everything needed to troubleshoot.

    Use when: A user is reporting a problem and should send one file rather than
    assembling logs, settings, and a workflow by hand. This is the request behind the
    editor's "collect troubleshooting info" action.

    The bundle contains the diagnostics report, the engine's logs, and the workflow that
    was open. Everything in it is redacted the same way GetDiagnosticsReportRequest
    redacts the report: no secret values, credential-shaped settings removed, home
    directory and username normalized, and every removal counted in the manifest.

    The bundle is written as a file. Nothing is sent anywhere; where it goes next is
    the user's decision.

    Args:
        include_logs: Include the engine's log files and this session's log. Turn off
            only when the problem has nothing to do with what the engine did, since the
            logs are usually the answer.
        include_current_workflow: Include the workflow that is open. Only what is saved
            on disk can be included, and the manifest says so when there are unsaved
            edits. Turn off when the workflow itself should not be shared.
        include_health_checks: Run the health checks and include their verdicts. Turn off
            to keep collection instant; one of the checks opens a connection to Griptape
            Cloud and waits for it.
        normalize_identity: Replace the home directory with `~` and the username with
            `<user>`. Leave on unless the bundle is only for your own machine.
        file_name: Name for the zip. None generates one from the timestamp.
        output_path: Directory or file path on the engine's machine to write the bundle
            to. None writes it to the engine's static files and returns a download link
            instead, which is what the editor wants: the engine may not be on the same
            machine as the person collecting.

    Results: CollectDiagnosticsResultSuccess | CollectDiagnosticsResultFailure
    """

    include_logs: bool = True
    include_current_workflow: bool = True
    include_health_checks: bool = True
    normalize_identity: bool = True
    file_name: str | None = None
    output_path: str | None = None


@dataclass
@PayloadRegistry.register
class CollectDiagnosticsResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Diagnostics bundle collected successfully.

    A bundle succeeds even when parts of it could not be gathered; those are named in
    `manifest.warnings`. Show them to the user, because a shortened log or a workflow
    that could not be read changes what the bundle can prove.

    Exactly one of `url` and `path` is set, depending on whether the request asked for
    an `output_path`.

    Args:
        file_name: Name the bundle was written as. May differ from the requested name
            when a file of that name already existed.
        size_bytes: Size of the zip.
        manifest: What the bundle contains, what was removed, and what is missing.
        url: Link the bundle can be downloaded from, when it went to static files.
        path: Where the bundle was written on the engine's machine, when an
            `output_path` was requested.
    """

    file_name: str
    size_bytes: int
    manifest: DiagnosticsBundleManifest
    url: str | None = None
    path: str | None = None


@dataclass
@PayloadRegistry.register
class CollectDiagnosticsResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Diagnostics bundle collection failed.

    Common causes: the report underneath could not be built, or the bundle could not be
    written (no disk space, or no permission to write to the requested `output_path`).
    """


@dataclass
@PayloadRegistry.register
class RunHealthChecksRequest(RequestPayload):
    """Check whether this installation is set up correctly and say what to fix.

    Use when: Someone says "it isn't working" and the specific symptom is not clear yet.
    Where GetDiagnosticsReportRequest reports facts, this interprets them: each check
    returns a pass, a warning, or a failure, and a failure comes with what to do about
    it. This is what `gtn doctor` prints, and the same verdicts are written into a
    diagnostics bundle as `doctor.json`.

    One check opens a connection to Griptape Cloud, so this request can take several
    seconds when the network is the problem.

    Results: RunHealthChecksResultSuccess | RunHealthChecksResultFailure
    """


@dataclass
@PayloadRegistry.register
class RunHealthChecksResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Health checks ran.

    Succeeds whatever the checks found: a failing check is a successful result whose
    `health.status` is `fail`. Read `health.status` to decide whether to act.

    Args:
        health: Every check that ran, and the worst thing any of them found.
    """

    health: HealthReport


@dataclass
@PayloadRegistry.register
class RunHealthChecksResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Health checks could not be run.

    Common cause: the diagnostics report the checks read could not be built at all. An
    individual check failing does not cause this; it is reported as a failing check on a
    successful result instead.
    """
