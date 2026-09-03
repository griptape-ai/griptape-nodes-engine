"""Tests for the health checks.

Each check turns facts into a verdict, so these pin the verdicts: what counts as broken,
what counts as merely worth warning about, and that a broken check never takes the run
down with it.
"""

from __future__ import annotations

import asyncio

import pytest
from websockets.exceptions import InvalidHandshake

from griptape_nodes.common.diagnostics.health import (
    CLOUD_API_KEY_NAME,
    CloudConnectionCheck,
    DiskSpaceCheck,
    HealthCheck,
    HealthCheckContext,
    HealthCheckResult,
    HealthReport,
    HealthStatus,
    LibraryCheck,
    LogCaptureCheck,
    SecretsCheck,
    WorkspaceCheck,
    redact_health_report,
    run_health_checks,
    worst_status,
)
from griptape_nodes.common.diagnostics.redaction import RedactionReason, Redactor
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
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager


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


def _health_report(*results: HealthCheckResult) -> HealthReport:
    """Build a health report around given results, taking the overall status from them."""
    checks = list(results)
    return HealthReport(generated_at="2026-01-01T00:00:00+00:00", status=worst_status(checks), results=checks)


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

    @pytest.mark.asyncio
    async def test_warns_on_a_library_whose_state_it_cannot_interpret(self) -> None:
        """A fitness this check has never heard of must not read as a clean load.

        `NOT_EVALUATED` is one the engine really sets, and a value a newer engine invents
        lands here too. Either way the library the report is being collected about is the
        one nobody can say anything about.
        """
        libraries = [
            LibraryDiagnostics(name="Never Evaluated", fitness="NOT_EVALUATED"),
            LibraryDiagnostics(name="From The Future", fitness="SOMETHING_NEW"),
            LibraryDiagnostics(name="No Fitness At All", fitness=None),
        ]

        result = await LibraryCheck().run(_check_context(libraries=libraries))

        assert result.status is HealthStatus.WARN
        assert "Never Evaluated" in result.summary
        assert "From The Future" in result.summary
        assert "No Fitness At All" in result.summary

    @pytest.mark.asyncio
    async def test_a_library_that_was_turned_off_is_not_a_problem_to_report(self) -> None:
        """A disabled library is never loaded, so it never becomes GOOD.

        Reported as one that did not load cleanly, it sends someone to fix a library they
        turned off deliberately -- and it downgrades an otherwise healthy engine to a
        warning, which is the state people stop reading.
        """
        libraries = [
            LibraryDiagnostics(name="In Use", fitness="GOOD"),
            LibraryDiagnostics(name="Turned Off", fitness="NOT_EVALUATED", enabled=False),
        ]

        result = await LibraryCheck().run(_check_context(libraries=libraries))

        assert result.status is HealthStatus.PASS
        assert "Turned Off" not in result.summary

    @pytest.mark.asyncio
    async def test_every_library_turned_off_says_that_rather_than_that_there_are_none(self) -> None:
        """Two different things to fix: install libraries, or turn the ones there back on."""
        libraries = [LibraryDiagnostics(name="Turned Off", fitness="NOT_EVALUATED", enabled=False)]

        result = await LibraryCheck().run(_check_context(libraries=libraries))

        assert result.status is HealthStatus.WARN
        assert "turned off" in result.summary
        assert result.remedy is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fitness", list(LibraryManager.LibraryFitness))
    async def test_only_a_good_library_reads_as_an_all_clear(self, fitness: LibraryManager.LibraryFitness) -> None:
        """Every fitness the engine can report, and only GOOD is allowed to pass.

        The check compares strings so a verdict stays readable from a bundle written by a
        different engine version, which means a new `LibraryFitness` member compiles fine
        and would otherwise be discovered by a user whose broken library reported an
        all-clear. Driven off the enum so adding a member fails here first.
        """
        libraries = [LibraryDiagnostics(name="Under Test", fitness=fitness.value)]

        result = await LibraryCheck().run(_check_context(libraries=libraries))

        if fitness is LibraryManager.LibraryFitness.GOOD:
            assert result.status is HealthStatus.PASS
            return

        assert result.status is not HealthStatus.PASS
        assert "Under Test" in result.summary


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
    async def test_the_all_clear_counts_only_the_secrets_something_asked_for(self) -> None:
        """The report lists every key it found, and most of them nothing is expecting.

        Counting those made the all-clear claim to have checked keys it never looked at --
        "every one of the 14 expected secrets has a value" on an engine where one library
        declared one key and the rest of the environment happened to hold thirteen.
        """
        secrets = [
            SecretDiagnostics(name="DECLARED", is_set=True, declared_in_config=True),
            SecretDiagnostics(name="JUST_LYING_AROUND", is_set=True, declared_in_config=False),
        ]

        result = await SecretsCheck().run(_check_context(secrets=secrets))

        assert result.status is HealthStatus.PASS
        assert "1 expected secrets" in result.summary

    @pytest.mark.asyncio
    async def test_no_secrets_expected_at_all_says_so_rather_than_counting_to_zero(self) -> None:
        """`every one of the 0 expected secrets` reads like something went wrong."""
        secrets = [SecretDiagnostics(name="JUST_LYING_AROUND", is_set=True, declared_in_config=False)]

        result = await SecretsCheck().run(_check_context(secrets=secrets))

        assert result.status is HealthStatus.PASS
        assert "No secrets are expected" in result.summary

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
    async def test_fails_without_an_api_key_and_never_touches_the_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The no-key answer is decided before a socket is opened.

        The connect call is replaced with one that fails the test, so the second half of the
        name is enforced rather than asserted in prose. Without this, a check that dialled
        Griptape Cloud with an empty Authorization header and reported the refusal would
        still produce a FAIL naming the key, and pass.
        """
        check = CloudConnectionCheck()

        async def refuse_to_be_called(_url: str, _headers: dict[str, str]) -> None:
            pytest.fail("the connection check opened a socket with no API key to authenticate with")

        monkeypatch.setattr(check, "_connect_and_disconnect", refuse_to_be_called)
        context = HealthCheckContext(report=_report(), cloud_api_key=None)

        result = await check.run(context)

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
    async def test_a_connection_that_never_answers_is_reported_as_a_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A distinct verdict from "could not be reached", and easily lost.

        `TimeoutError` is a subclass of `OSError`, so moving the `OSError` branch above it
        compiles, passes every other test here, and turns "a firewall is swallowing this"
        into "check your network connection" -- the wrong thing to go and look at. The
        timeout is shortened rather than waited out; what is being pinned is which branch
        catches it.
        """
        check = CloudConnectionCheck()

        async def never_answer(_url: str, _headers: dict[str, str]) -> None:
            await asyncio.sleep(30)

        monkeypatch.setattr(check, "_connect_and_disconnect", never_answer)
        monkeypatch.setattr(check, "_CONNECTION_TIMEOUT", 0.01)

        result = await check.run(_check_context())

        assert result.status is HealthStatus.FAIL
        assert "timed out" in result.summary
        assert result.remedy is not None
        assert "firewall" in result.remedy

    @pytest.mark.asyncio
    async def test_a_rejected_handshake_is_reported_as_a_bad_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The server answered, so the network is fine and the key is what to go and change."""
        check = CloudConnectionCheck()

        async def reject(_url: str, _headers: dict[str, str]) -> None:
            msg = "server rejected WebSocket connection: HTTP 401"
            raise InvalidHandshake(msg)

        monkeypatch.setattr(check, "_connect_and_disconnect", reject)

        result = await check.run(_check_context())

        assert result.status is HealthStatus.FAIL
        assert "HTTP 401" in result.summary
        assert result.remedy is not None
        assert CLOUD_API_KEY_NAME in result.remedy

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


class TestRedactHealthReport:
    """A verdict is mostly quoted from an already-clean report, but a failed check writes its own.

    That text can be an exception from the network stack or an OS error carrying the path it
    failed on, so it is redacted here rather than by whoever writes the report out -- the
    terminal a user pastes into a ticket needs it as much as a bundle does.
    """

    def test_removes_a_secret_a_failing_check_quoted(self) -> None:
        report = _health_report(
            HealthCheckResult(
                name="Cloud",
                status=HealthStatus.FAIL,
                summary="rejected the key sk-abcdefgh12345678",
                remedy="set it again with sk-abcdefgh12345678",
            )
        )
        redactor = Redactor(normalize_identity=False)

        redacted = redact_health_report(report, redactor)

        assert "sk-abcdefgh12345678" not in redacted.results[0].summary
        assert redacted.results[0].remedy is not None
        assert "sk-abcdefgh12345678" not in redacted.results[0].remedy
        assert redactor.counts() == {RedactionReason.API_KEY_PATTERN: 2}

    def test_leaves_the_original_report_alone(self) -> None:
        """The caller keeps holding the report it built, and the checks run against it."""
        report = _health_report(
            HealthCheckResult(name="Cloud", status=HealthStatus.FAIL, summary="key sk-abcdefgh12345678 refused")
        )

        redact_health_report(report, Redactor(normalize_identity=False))

        assert report.results[0].summary == "key sk-abcdefgh12345678 refused"

    def test_keeps_a_result_with_nothing_to_suggest(self) -> None:
        """A remedy of None must stay None rather than becoming the string "None"."""
        report = _health_report(HealthCheckResult(name="Workspace", status=HealthStatus.PASS, summary="fine"))

        redacted = redact_health_report(report, Redactor(normalize_identity=False))

        assert redacted.results[0].remedy is None
        assert redacted.results[0].name == "Workspace"
        assert redacted.status is HealthStatus.PASS
