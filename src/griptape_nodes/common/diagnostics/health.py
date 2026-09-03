"""Health checks: the engine's verdict on its own setup, with a remedy for each problem.

A ``DiagnosticsReport`` states facts. A health report interprets them: it says which of
those facts is a problem and what to do about it. That interpretation is what ``gtn
doctor`` prints and what ``doctor.json`` carries into a diagnostics bundle, so a support
engineer opening a bundle sees the same verdicts the user saw.

Almost every check reads the report and nothing else, which keeps them cheap and
testable: build a report once, judge it many times. ``CloudConnectionCheck`` is the
exception, because "can this machine reach the API" cannot be answered from a snapshot.

Statuses are deliberately coarse. ``FAIL`` means something is broken and Griptape Nodes
will not work properly until it is fixed. ``WARN`` means it works but something will bite
later, or a bundle collected now will be less useful than it could be. Anything else is
``PASS``.
"""

from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urljoin

from pydantic import BaseModel, Field
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidHandshake

if TYPE_CHECKING:
    from griptape_nodes.common.diagnostics.redaction import Redactor
    from griptape_nodes.common.diagnostics.report import DiagnosticsReport

# Schema version for the health report. Bump when the shape changes so a support tool
# reading a bundle from an older engine knows what to expect.
HEALTH_REPORT_SCHEMA_VERSION = "0.1.0"

CLOUD_API_KEY_NAME = "GT_CLOUD_API_KEY"

# Below this much free space, saving a workflow or installing a library is likely to fail
# outright. Between the two, it will start failing soon.
CRITICAL_FREE_DISK_GB = 1.0
LOW_FREE_DISK_GB = 5.0


class HealthStatus(StrEnum):
    """How a check turned out, worst last so the values can be ordered."""

    PASS = "pass"  # noqa: S105 - a verdict, not a credential
    WARN = "warn"
    FAIL = "fail"


# Ordering for "worst status wins" when summarizing a run.
_STATUS_SEVERITY: dict[HealthStatus, int] = {
    HealthStatus.PASS: 0,
    HealthStatus.WARN: 1,
    HealthStatus.FAIL: 2,
}


class HealthCheckResult(BaseModel):
    """The outcome of one check.

    Attributes:
        name: What was checked, in the words a user would use.
        status: How it turned out.
        summary: What was found. Written to be read by whoever ran the check, not only
            by an engineer.
        remedy: What to do about it, when there is something to do. None on a pass.
    """

    name: str
    status: HealthStatus
    summary: str
    remedy: str | None = None


class HealthReport(BaseModel):
    """Every check that ran, and the worst thing any of them found.

    Attributes:
        schema_version: Version of this envelope.
        generated_at: ISO 8601 timestamp (UTC) of when the checks ran.
        status: The worst status among the results. ``PASS`` only when nothing was found.
        results: One entry per check, in the order they ran.
    """

    schema_version: str = HEALTH_REPORT_SCHEMA_VERSION
    generated_at: str
    status: HealthStatus = HealthStatus.PASS
    results: list[HealthCheckResult] = Field(default_factory=list)


@dataclass
class HealthCheckContext:
    """Everything the checks are allowed to look at.

    Attributes:
        report: The engine snapshot the report-reading checks judge.
        cloud_api_key: The Griptape Cloud key, supplied by the caller because it cannot
            appear in a report. Used to open a connection and never recorded anywhere.
    """

    report: DiagnosticsReport
    cloud_api_key: str | None = None


class HealthCheck(ABC):
    """One thing worth checking about an installation.

    Subclasses state their own ``name`` and return a single result. A check reports what
    it found rather than raising: a check that cannot run is itself a finding, and one
    broken check must not stop the rest from running.
    """

    name: ClassVar[str]

    @abstractmethod
    async def run(self, context: HealthCheckContext) -> HealthCheckResult: ...


class CloudConnectionCheck(HealthCheck):
    """Opens a connection to the Griptape Nodes API and closes it again.

    The one check that touches the network. Without this connection the editor cannot
    talk to the engine, which is the single most common reason nothing works at all.
    """

    name: ClassVar[str] = "Cloud Connection"

    _CONNECTION_TIMEOUT = 10.0
    _DEFAULT_API_BASE_URL = "https://api.nodes.griptape.ai"
    _WEBSOCKET_PATH = "/ws/engines/events?version=v2"

    async def run(self, context: HealthCheckContext) -> HealthCheckResult:
        if not context.cloud_api_key:
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.FAIL,
                summary=f"No {CLOUD_API_KEY_NAME} is set, so the engine cannot connect to Griptape Cloud.",
                remedy=f"Run 'gtn init' to set your API key, or set {CLOUD_API_KEY_NAME} in your environment.",
            )

        headers = {"Authorization": f"Bearer {context.cloud_api_key}"}

        try:
            await asyncio.wait_for(
                self._connect_and_disconnect(self._websocket_url(), headers), timeout=self._CONNECTION_TIMEOUT
            )
        except TimeoutError:
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.FAIL,
                summary="Connecting to Griptape Cloud timed out.",
                remedy="Check your network connection, and whether a firewall or VPN is blocking the connection.",
            )
        except InvalidHandshake as err:
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.FAIL,
                summary=f"Griptape Cloud refused the connection, which usually means the API key is wrong: {err}",
                remedy=f"Run 'gtn init' to set a new API key, or check the value of {CLOUD_API_KEY_NAME}.",
            )
        except OSError as err:
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.FAIL,
                summary=f"Griptape Cloud could not be reached: {err}",
                remedy="Check your network connection.",
            )

        return HealthCheckResult(
            name=self.name,
            status=HealthStatus.PASS,
            summary="Connected to Griptape Cloud.",
        )

    def _websocket_url(self) -> str:
        """Return the events endpoint this engine's connection would actually use.

        Honors the same ``GRIPTAPE_NODES_API_BASE_URL`` override the engine's own client
        reads, so a check run against a staging API reports on staging rather than always
        reporting on production.
        """
        base_url = os.getenv("GRIPTAPE_NODES_API_BASE_URL", self._DEFAULT_API_BASE_URL)
        return urljoin(base_url.replace("http", "ws"), self._WEBSOCKET_PATH)

    async def _connect_and_disconnect(self, url: str, headers: dict[str, str]) -> None:
        async with connect(url, additional_headers=headers):
            pass


class LibraryCheck(HealthCheck):
    """Reports libraries that did not load cleanly.

    A missing node, a node that will not instantiate, and a library that installed no
    dependencies all look identical from the editor: the node is simply not there.
    """

    name: ClassVar[str] = "Libraries"

    _BROKEN_FITNESS = frozenset({"UNUSABLE", "MISSING"})
    # The only fitness that means "loaded cleanly". Stated as what passes rather than as
    # what fails, so anything else -- `NOT_EVALUATED`, or a value a newer engine added
    # that this reader has never heard of -- is reported instead of falling through to an
    # all-clear. A library whose state cannot be interpreted is exactly the one the report
    # is being collected about.
    _HEALTHY_FITNESS = frozenset({"GOOD"})

    async def run(self, context: HealthCheckContext) -> HealthCheckResult:
        libraries = context.report.libraries

        if not libraries:
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.WARN,
                summary="No libraries are registered, so no nodes are available.",
                remedy="Run 'gtn libraries sync' to install the default libraries.",
            )

        broken = [library.name for library in libraries if library.fitness in self._BROKEN_FITNESS]
        degraded = [
            library.name
            for library in libraries
            if library.fitness not in self._BROKEN_FITNESS and library.fitness not in self._HEALTHY_FITNESS
        ]

        if broken:
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.FAIL,
                summary=f"{len(broken)} of {len(libraries)} libraries could not be loaded: {', '.join(broken)}.",
                remedy=(
                    "Open the Libraries panel to see what went wrong with each one, or read the 'libraries' "
                    "section of report.json."
                ),
            )

        if degraded:
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.WARN,
                summary=(
                    f"{len(degraded)} of {len(libraries)} libraries did not load cleanly: {', '.join(degraded)}. "
                    "Some of their nodes may be missing."
                ),
                remedy="Open the Libraries panel to see which nodes are affected.",
            )

        return HealthCheckResult(
            name=self.name,
            status=HealthStatus.PASS,
            summary=f"All {len(libraries)} registered libraries loaded cleanly.",
        )


class WorkspaceCheck(HealthCheck):
    """Checks that the workspace directory exists and can be written to.

    Everything a user makes is saved here. A workspace that has been moved, deleted, or
    is on a disconnected network drive turns every save into a failure.
    """

    name: ClassVar[str] = "Workspace"

    async def run(self, context: HealthCheckContext) -> HealthCheckResult:
        paths = context.report.paths
        workspace = paths.workspace_directory

        if workspace is None:
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.FAIL,
                summary="No workspace directory is configured.",
                remedy="Run 'gtn init' to choose a workspace directory.",
            )

        if workspace in paths.missing_paths:
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.FAIL,
                summary=f"The workspace directory '{workspace}' does not exist.",
                remedy=(
                    "Create that directory, or run 'gtn init' to point the workspace somewhere that exists. "
                    "If it is on an external or network drive, reconnect the drive."
                ),
            )

        if paths.workspace_writable is False:
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.FAIL,
                summary=f"The workspace directory '{workspace}' cannot be written to.",
                remedy="Check the folder's permissions, or run 'gtn init' to choose a different workspace.",
            )

        return HealthCheckResult(
            name=self.name,
            status=HealthStatus.PASS,
            summary=f"The workspace directory '{workspace}' exists and can be written to.",
        )


class DiskSpaceCheck(HealthCheck):
    """Checks free space on the drive holding the workspace.

    Model downloads and library installs are large. A full disk shows up as a save that
    silently produced nothing, which is very hard to recognize from the editor.
    """

    name: ClassVar[str] = "Disk Space"

    async def run(self, context: HealthCheckContext) -> HealthCheckResult:
        free_gb = context.report.host.workspace_disk_free_gb

        if free_gb is None:
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.WARN,
                summary="Free space on the workspace drive could not be read.",
                remedy="Check that the workspace drive is connected.",
            )

        if free_gb < CRITICAL_FREE_DISK_GB:
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.FAIL,
                summary=f"Only {free_gb} GB is free on the workspace drive.",
                remedy="Free up space. Saving workflows, downloading models, and installing libraries will fail.",
            )

        if free_gb < LOW_FREE_DISK_GB:
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.WARN,
                summary=f"{free_gb} GB is free on the workspace drive, which is not much.",
                remedy="Free up space before downloading models or installing libraries.",
            )

        return HealthCheckResult(
            name=self.name,
            status=HealthStatus.PASS,
            summary=f"{free_gb} GB is free on the workspace drive.",
        )


class LogCaptureCheck(HealthCheck):
    """Checks that logs are being kept.

    Logs are what a bundle is for. This check exists so the absence of logs is discovered
    now, rather than after someone has already tried to reproduce a problem.
    """

    name: ClassVar[str] = "Log Capture"

    async def run(self, context: HealthCheckContext) -> HealthCheckResult:
        logs = context.report.logs
        session_recorded = logs.session_buffer_lines > 0

        if not session_recorded and not logs.log_to_file:
            # Nothing is being kept anywhere, which makes the next bundle from this engine
            # empty of the one thing a bundle is collected for.
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.FAIL,
                summary="Nothing this engine logs is being kept, so a diagnostics bundle will contain no logs.",
                remedy=(
                    "Set 'logging.session_log_buffer_lines' to a number above zero, and turn on the "
                    "'logging.log_to_file' setting so logs survive a restart."
                ),
            )

        if not session_recorded:
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.WARN,
                summary="This session is not being recorded, so a bundle collected now holds only earlier log files.",
                remedy=(
                    "Set 'logging.session_log_buffer_lines' to a number above zero to record the session a "
                    "bundle is collected from."
                ),
            )

        if not logs.log_to_file:
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.WARN,
                summary="Engine logs are not being written to file, so only the current session can be collected.",
                remedy="Turn on the 'logging.log_to_file' setting so logs survive a restart.",
            )

        if not logs.files:
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.WARN,
                summary=f"Log files are turned on, but none were found in '{logs.log_directory}'.",
                remedy=(
                    "Check that the log directory can be written to, or set 'logging.log_directory' to a "
                    "folder you own."
                ),
            )

        return HealthCheckResult(
            name=self.name,
            status=HealthStatus.PASS,
            summary=f"{len(logs.files)} log file(s) are available, and this session is being recorded.",
        )


class SecretsCheck(HealthCheck):
    """Reports secrets a library asked for that have no value.

    This is the check behind "the node says my API key is missing". The key is declared,
    so the engine knows to look for it, and nothing anywhere provides it.
    """

    name: ClassVar[str] = "Secrets"

    _MAX_NAMES_LISTED = 5

    async def run(self, context: HealthCheckContext) -> HealthCheckResult:
        secrets = context.report.secrets
        missing = [secret.name for secret in secrets if secret.declared_in_config and not secret.is_set]

        if not missing:
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.PASS,
                summary=f"Every one of the {len(secrets)} expected secrets has a value.",
            )

        listed = ", ".join(missing[: self._MAX_NAMES_LISTED])
        if len(missing) > self._MAX_NAMES_LISTED:
            listed = f"{listed}, and {len(missing) - self._MAX_NAMES_LISTED} more"

        return HealthCheckResult(
            name=self.name,
            status=HealthStatus.WARN,
            summary=f"{len(missing)} expected secret(s) have no value: {listed}.",
            remedy=(
                "Set them in the editor's Settings under API Keys. Nodes that need one of these will fail "
                "when they run."
            ),
        )


# Every check that runs, in the order their results are shown. Cheapest and most
# fundamental first, so a reader hits "your workspace is gone" before the network result.
DEFAULT_HEALTH_CHECKS: tuple[type[HealthCheck], ...] = (
    WorkspaceCheck,
    DiskSpaceCheck,
    LibraryCheck,
    SecretsCheck,
    LogCaptureCheck,
    CloudConnectionCheck,
)


async def run_health_checks(
    context: HealthCheckContext, checks: tuple[type[HealthCheck], ...] = DEFAULT_HEALTH_CHECKS
) -> HealthReport:
    """Run every check and summarize the results.

    A check that raises is reported as a failure of that check rather than taking the run
    down with it: the other verdicts are still worth having, and a check that cannot
    answer is itself something to look at.

    Args:
        context: What the checks are allowed to look at.
        checks: The checks to run. Defaults to all of them.

    Returns:
        A report whose status is the worst status any check returned.
    """
    results: list[HealthCheckResult] = []
    for check_class in checks:
        try:
            # Construction is inside the guard as well as the call. A check is free to do
            # its setup in __init__, and one that fails there would otherwise take down
            # every check after it.
            results.append(await check_class().run(context))
        except Exception as err:
            results.append(
                HealthCheckResult(
                    name=check_class.name,
                    status=HealthStatus.FAIL,
                    summary=f"This check could not be completed: {err}",
                    remedy="Include this bundle in your bug report; the check itself is at fault.",
                )
            )

    return HealthReport(
        generated_at=datetime.now(UTC).isoformat(),
        status=worst_status(results),
        results=results,
    )


def worst_status(results: list[HealthCheckResult]) -> HealthStatus:
    """Return the most severe status among ``results``, or ``PASS`` when there are none."""
    if not results:
        return HealthStatus.PASS
    return max((result.status for result in results), key=lambda status: _STATUS_SEVERITY[status])


def redact_health_report(health: HealthReport, redactor: Redactor) -> HealthReport:
    """Return a copy of a health report with the checks' free text redacted.

    A check that failed is often quoting an error message from somewhere else -- the
    network stack, a library, an OS error carrying the path it failed on -- and none of
    that text is the engine's own. It is redacted here rather than by whoever writes the
    report out, because every destination needs it: the terminal a user pastes into a
    ticket as readily as the ``doctor.json`` in a bundle.

    Field by field, before serializing. Redacting finished JSON instead would miss any
    secret whose text has to be escaped to live in a JSON string, since the escaped
    spelling is not what the redactor is looking for.

    Args:
        health: The report to redact.
        redactor: Applied to each result's summary and remedy. Its counts advance, so pass
            the same one used for the rest of a bundle.

    Returns:
        A copy of the report. The original is left alone.
    """
    return health.model_copy(update={"results": [_redact_health_result(result, redactor) for result in health.results]})


def _redact_health_result(result: HealthCheckResult, redactor: Redactor) -> HealthCheckResult:
    """Return a check result with its free text redacted."""
    remedy = result.remedy
    if remedy is not None:
        remedy = redactor.redact_text(remedy)

    return result.model_copy(update={"summary": redactor.redact_text(result.summary), "remedy": remedy})
