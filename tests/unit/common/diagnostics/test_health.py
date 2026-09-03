"""Tests for the health checks.

Each check turns facts into a verdict, so these pin the verdicts: what counts as broken,
what counts as merely worth warning about, and that a broken check never takes the run
down with it.
"""

from __future__ import annotations

import pytest

from griptape_nodes.common.diagnostics.health import (
    CLOUD_API_KEY_NAME,
    CloudConnectionCheck,
    DiskSpaceCheck,
    HealthCheck,
    HealthCheckContext,
    HealthCheckResult,
    HealthStatus,
    LibraryCheck,
    LogCaptureCheck,
    SecretsCheck,
    WorkspaceCheck,
    run_health_checks,
    worst_status,
)
from griptape_nodes.common.diagnostics.report import (
    DiagnosticsReport,
    EngineDiagnostics,
    HostDiagnostics,
    LibraryDiagnostics,
    LogDiagnostics,
    LogFileDiagnostics,
    PathDiagnostics,
    SecretDiagnostics,
)


def _report(**overrides: object) -> DiagnosticsReport:
    """Build a report that every check passes, so a test can break exactly one thing."""
    defaults: dict[str, object] = {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "engine": EngineDiagnostics(python_version="3.12.0", python_executable="/usr/bin/python", process_id=1),
        "host": HostDiagnostics(
            system="Darwin",
            release="25.0.0",
            version="Darwin Kernel",
            machine="arm64",
            workspace_disk_free_gb=100.0,
            workspace_disk_total_gb=500.0,
        ),
        "paths": PathDiagnostics(workspace_directory="~/workspace", workspace_writable=True),
        "libraries": [LibraryDiagnostics(name="Griptape Nodes Library", fitness="GOOD")],
        "secrets": [SecretDiagnostics(name="OPENAI_API_KEY", is_set=True, declared_in_config=True)],
        "logs": LogDiagnostics(
            log_to_file=True,
            log_directory="~/logs",
            session_buffer_lines=5000,
            files=[LogFileDiagnostics(name="engine-1.log", size_bytes=10, modified_at="2026-01-01T00:00:00+00:00")],
        ),
    }
    return DiagnosticsReport(**{**defaults, **overrides})  # type: ignore[arg-type]


def _check_context(**overrides: object) -> HealthCheckContext:
    return HealthCheckContext(report=_report(**overrides), cloud_api_key="test-key")


class TestWorkspaceCheck:
    @pytest.mark.asyncio
    async def test_passes_when_workspace_exists_and_is_writable(self) -> None:
        result = await WorkspaceCheck().run(_check_context())

        assert result.status is HealthStatus.PASS
        assert result.remedy is None

    @pytest.mark.asyncio
    async def test_fails_when_no_workspace_is_configured(self) -> None:
        result = await WorkspaceCheck().run(_check_context(paths=PathDiagnostics()))

        assert result.status is HealthStatus.FAIL
        assert result.remedy is not None

    @pytest.mark.asyncio
    async def test_fails_when_workspace_is_missing(self) -> None:
        """A workspace on a disconnected drive is the case this catches."""
        paths = PathDiagnostics(workspace_directory="~/workspace", missing_paths=["~/workspace"])

        result = await WorkspaceCheck().run(_check_context(paths=paths))

        assert result.status is HealthStatus.FAIL
        assert "does not exist" in result.summary

    @pytest.mark.asyncio
    async def test_fails_when_workspace_is_not_writable(self) -> None:
        paths = PathDiagnostics(workspace_directory="~/workspace", workspace_writable=False)

        result = await WorkspaceCheck().run(_check_context(paths=paths))

        assert result.status is HealthStatus.FAIL
        assert "cannot be written to" in result.summary

    @pytest.mark.asyncio
    async def test_passes_when_writability_is_unknown(self) -> None:
        """None means "not checked", which must not read as "not writable"."""
        paths = PathDiagnostics(workspace_directory="~/workspace", workspace_writable=None)

        result = await WorkspaceCheck().run(_check_context(paths=paths))

        assert result.status is HealthStatus.PASS


class TestDiskSpaceCheck:
    @pytest.mark.asyncio
    async def test_passes_with_plenty_of_space(self) -> None:
        result = await DiskSpaceCheck().run(_check_context())

        assert result.status is HealthStatus.PASS

    @pytest.mark.asyncio
    async def test_fails_when_nearly_full(self) -> None:
        host = HostDiagnostics(
            system="Darwin", release="25.0.0", version="Darwin Kernel", machine="arm64", workspace_disk_free_gb=0.4
        )

        result = await DiskSpaceCheck().run(_check_context(host=host))

        assert result.status is HealthStatus.FAIL

    @pytest.mark.asyncio
    async def test_warns_when_getting_low(self) -> None:
        host = HostDiagnostics(
            system="Darwin", release="25.0.0", version="Darwin Kernel", machine="arm64", workspace_disk_free_gb=3.0
        )

        result = await DiskSpaceCheck().run(_check_context(host=host))

        assert result.status is HealthStatus.WARN

    @pytest.mark.asyncio
    async def test_warns_when_free_space_is_unknown(self) -> None:
        host = HostDiagnostics(system="Darwin", release="25.0.0", version="Darwin Kernel", machine="arm64")

        result = await DiskSpaceCheck().run(_check_context(host=host))

        assert result.status is HealthStatus.WARN


class TestLibraryCheck:
    @pytest.mark.asyncio
    async def test_passes_when_every_library_is_good(self) -> None:
        result = await LibraryCheck().run(_check_context())

        assert result.status is HealthStatus.PASS

    @pytest.mark.asyncio
    async def test_warns_when_no_libraries_are_registered(self) -> None:
        result = await LibraryCheck().run(_check_context(libraries=[]))

        assert result.status is HealthStatus.WARN

    @pytest.mark.asyncio
    async def test_fails_and_names_unusable_libraries(self) -> None:
        libraries = [
            LibraryDiagnostics(name="Good One", fitness="GOOD"),
            LibraryDiagnostics(name="Broken One", fitness="UNUSABLE"),
        ]

        result = await LibraryCheck().run(_check_context(libraries=libraries))

        assert result.status is HealthStatus.FAIL
        assert "Broken One" in result.summary

    @pytest.mark.asyncio
    async def test_warns_on_a_flawed_library(self) -> None:
        """FLAWED means it registered, so some of its nodes work: a warning, not a failure."""
        libraries = [LibraryDiagnostics(name="Partly Broken", fitness="FLAWED")]

        result = await LibraryCheck().run(_check_context(libraries=libraries))

        assert result.status is HealthStatus.WARN
        assert "Partly Broken" in result.summary


class TestSecretsCheck:
    @pytest.mark.asyncio
    async def test_passes_when_every_expected_secret_is_set(self) -> None:
        result = await SecretsCheck().run(_check_context())

        assert result.status is HealthStatus.PASS

    @pytest.mark.asyncio
    async def test_warns_only_about_declared_secrets(self) -> None:
        """A key nothing asked for is not a problem; a declared key with no value is."""
        secrets = [
            SecretDiagnostics(name="DECLARED_AND_MISSING", is_set=False, declared_in_config=True),
            SecretDiagnostics(name="UNDECLARED_AND_MISSING", is_set=False, declared_in_config=False),
        ]

        result = await SecretsCheck().run(_check_context(secrets=secrets))

        assert result.status is HealthStatus.WARN
        assert "DECLARED_AND_MISSING" in result.summary
        assert "UNDECLARED_AND_MISSING" not in result.summary

    @pytest.mark.asyncio
    async def test_summarizes_a_long_list_rather_than_printing_all_of_it(self) -> None:
        secrets = [SecretDiagnostics(name=f"KEY_{i}", is_set=False, declared_in_config=True) for i in range(8)]

        result = await SecretsCheck().run(_check_context(secrets=secrets))

        assert result.status is HealthStatus.WARN
        assert "and 3 more" in result.summary


class TestLogCaptureCheck:
    @pytest.mark.asyncio
    async def test_passes_when_log_files_exist(self) -> None:
        result = await LogCaptureCheck().run(_check_context())

        assert result.status is HealthStatus.PASS

    @pytest.mark.asyncio
    async def test_warns_when_file_logging_is_off(self) -> None:
        logs = LogDiagnostics(log_to_file=False, session_buffer_lines=5000)

        result = await LogCaptureCheck().run(_check_context(logs=logs))

        assert result.status is HealthStatus.WARN
        assert result.remedy is not None

    @pytest.mark.asyncio
    async def test_warns_when_logging_is_on_but_nothing_was_written(self) -> None:
        """The signal that the log directory cannot be written to."""
        logs = LogDiagnostics(log_to_file=True, log_directory="~/logs", session_buffer_lines=5000, files=[])

        result = await LogCaptureCheck().run(_check_context(logs=logs))

        assert result.status is HealthStatus.WARN

    @pytest.mark.asyncio
    async def test_warns_when_the_session_buffer_is_disabled(self) -> None:
        """Log files exist, but the session that is about to be bundled is not being kept."""
        logs = LogDiagnostics(
            log_to_file=True,
            log_directory="~/logs",
            session_buffer_lines=0,
            files=[LogFileDiagnostics(name="engine-1.log", size_bytes=10, modified_at="2026-01-01T00:00:00+00:00")],
        )

        result = await LogCaptureCheck().run(_check_context(logs=logs))

        assert result.status is HealthStatus.WARN
        assert "not being recorded" in result.summary

    @pytest.mark.asyncio
    async def test_fails_when_nothing_is_being_kept_anywhere(self) -> None:
        """No buffer and no files means the next bundle carries no logs at all."""
        logs = LogDiagnostics(log_to_file=False, session_buffer_lines=0)

        result = await LogCaptureCheck().run(_check_context(logs=logs))

        assert result.status is HealthStatus.FAIL
        assert result.remedy is not None


class TestCloudConnectionCheck:
    @pytest.mark.asyncio
    async def test_fails_without_an_api_key_and_never_touches_the_network(self) -> None:
        context = HealthCheckContext(report=_report(), cloud_api_key=None)

        result = await CloudConnectionCheck().run(context)

        assert result.status is HealthStatus.FAIL
        assert CLOUD_API_KEY_NAME in result.summary

    @pytest.mark.asyncio
    async def test_fails_when_the_connection_cannot_be_made(self, monkeypatch: pytest.MonkeyPatch) -> None:
        check = CloudConnectionCheck()

        async def refuse(_url: str, _headers: dict[str, str]) -> None:
            msg = "no route to host"
            raise OSError(msg)

        monkeypatch.setattr(check, "_connect_and_disconnect", refuse)
        result = await check.run(_check_context())

        assert result.status is HealthStatus.FAIL
        assert "no route to host" in result.summary

    @pytest.mark.asyncio
    async def test_passes_when_the_connection_opens(self, monkeypatch: pytest.MonkeyPatch) -> None:
        check = CloudConnectionCheck()

        async def accept(_url: str, _headers: dict[str, str]) -> None:
            return

        monkeypatch.setattr(check, "_connect_and_disconnect", accept)
        result = await check.run(_check_context())

        assert result.status is HealthStatus.PASS

    def test_uses_the_production_events_endpoint_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GRIPTAPE_NODES_API_BASE_URL", raising=False)

        assert CloudConnectionCheck()._websocket_url() == "wss://api.nodes.griptape.ai/ws/engines/events?version=v2"

    def test_honors_the_api_base_url_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A check run against staging must report on staging, not on production."""
        monkeypatch.setenv("GRIPTAPE_NODES_API_BASE_URL", "https://custom.example.com")

        assert CloudConnectionCheck()._websocket_url() == "wss://custom.example.com/ws/engines/events?version=v2"


class _ExplodingCheck(HealthCheck):
    name = "Exploding Check"

    async def run(self, _context: HealthCheckContext) -> HealthCheckResult:
        msg = "this check is itself broken"
        raise RuntimeError(msg)


class _UnconstructableCheck(HealthCheck):
    name = "Unconstructable Check"

    def __init__(self) -> None:
        msg = "this check cannot even be built"
        raise RuntimeError(msg)

    async def run(self, _context: HealthCheckContext) -> HealthCheckResult:
        return HealthCheckResult(name=self.name, status=HealthStatus.PASS, summary="never reached")


class _PassingCheck(HealthCheck):
    name = "Passing Check"

    async def run(self, _context: HealthCheckContext) -> HealthCheckResult:
        return HealthCheckResult(name=self.name, status=HealthStatus.PASS, summary="fine")


class TestRunHealthChecks:
    @pytest.mark.asyncio
    async def test_a_broken_check_does_not_stop_the_others(self) -> None:
        report = await run_health_checks(_check_context(), checks=(_ExplodingCheck, _PassingCheck))

        assert [result.name for result in report.results] == ["Exploding Check", "Passing Check"]
        assert report.results[0].status is HealthStatus.FAIL
        assert "this check is itself broken" in report.results[0].summary
        assert report.results[1].status is HealthStatus.PASS

    @pytest.mark.asyncio
    async def test_a_check_that_cannot_be_constructed_does_not_stop_the_others(self) -> None:
        """A check is free to do its setup in __init__, so that has to be guarded too."""
        report = await run_health_checks(_check_context(), checks=(_UnconstructableCheck, _PassingCheck))

        assert [result.name for result in report.results] == ["Unconstructable Check", "Passing Check"]
        assert report.results[0].status is HealthStatus.FAIL
        assert "this check cannot even be built" in report.results[0].summary
        assert report.results[1].status is HealthStatus.PASS

    @pytest.mark.asyncio
    async def test_overall_status_is_the_worst_result(self) -> None:
        report = await run_health_checks(_check_context(), checks=(_ExplodingCheck, _PassingCheck))

        assert report.status is HealthStatus.FAIL

    @pytest.mark.asyncio
    async def test_results_keep_the_order_the_checks_ran_in(self) -> None:
        report = await run_health_checks(_check_context(), checks=(_PassingCheck, _ExplodingCheck))

        assert [result.name for result in report.results] == ["Passing Check", "Exploding Check"]


class TestWorstStatus:
    def test_no_results_is_a_pass(self) -> None:
        assert worst_status([]) is HealthStatus.PASS

    def test_a_warning_beats_a_pass(self) -> None:
        results = [
            HealthCheckResult(name="a", status=HealthStatus.PASS, summary=""),
            HealthCheckResult(name="b", status=HealthStatus.WARN, summary=""),
        ]

        assert worst_status(results) is HealthStatus.WARN

    def test_a_failure_beats_a_warning(self) -> None:
        results = [
            HealthCheckResult(name="a", status=HealthStatus.WARN, summary=""),
            HealthCheckResult(name="b", status=HealthStatus.FAIL, summary=""),
            HealthCheckResult(name="c", status=HealthStatus.PASS, summary=""),
        ]

        assert worst_status(results) is HealthStatus.FAIL
