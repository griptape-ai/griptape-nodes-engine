"""Tests for the doctor CLI command.

The command is a view over `RunHealthChecksRequest`, so these pin the two things a view
can get wrong: the exit code it chooses, and whether it survives a failed request.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import typer

from griptape_nodes.cli.commands.doctor import doctor_command
from griptape_nodes.common.diagnostics.health import HealthCheckResult, HealthReport, HealthStatus
from griptape_nodes.retained_mode.events.diagnostics_events import (
    RunHealthChecksResultFailure,
    RunHealthChecksResultSuccess,
)

_MODULE = "griptape_nodes.cli.commands.doctor"


def _health_report(status: HealthStatus) -> HealthReport:
    """Build a report holding one check with the given status."""
    return HealthReport(
        generated_at="2026-01-01T00:00:00+00:00",
        status=status,
        results=[
            HealthCheckResult(
                name="Test Check",
                status=status,
                summary="what was found",
                remedy=None if status is HealthStatus.PASS else "what to do",
            )
        ],
    )


def _success(status: HealthStatus) -> RunHealthChecksResultSuccess:
    return RunHealthChecksResultSuccess(health=_health_report(status), result_details="ran")


class TestDoctorCommand:
    def test_exits_zero_when_every_check_passes(self) -> None:
        """A clean bill of health is a zero exit."""
        with (
            patch(f"{_MODULE}.GriptapeNodes.ahandle_request", new_callable=AsyncMock) as handle,
            patch(f"{_MODULE}.console"),
        ):
            handle.side_effect = [None, _success(HealthStatus.PASS)]
            doctor_command()

    def test_exits_zero_when_a_check_only_warns(self) -> None:
        """A warning is something to fix eventually, so scripts calling this must not break."""
        with (
            patch(f"{_MODULE}.GriptapeNodes.ahandle_request", new_callable=AsyncMock) as handle,
            patch(f"{_MODULE}.console"),
        ):
            handle.side_effect = [None, _success(HealthStatus.WARN)]
            doctor_command()

    def test_exits_one_when_a_check_fails(self) -> None:
        """A failing check exits nonzero, which is what the documented script usage relies on."""
        with (
            patch(f"{_MODULE}.GriptapeNodes.ahandle_request", new_callable=AsyncMock) as handle,
            patch(f"{_MODULE}.console"),
        ):
            handle.side_effect = [None, _success(HealthStatus.FAIL)]

            with pytest.raises(typer.Exit) as exc_info:
                doctor_command()

        assert exc_info.value.exit_code == 1

    def test_exits_one_when_the_checks_could_not_run(self) -> None:
        """A request that fails outright is reported rather than crashing the command."""
        with (
            patch(f"{_MODULE}.GriptapeNodes.ahandle_request", new_callable=AsyncMock) as handle,
            patch(f"{_MODULE}.console"),
        ):
            handle.side_effect = [None, RunHealthChecksResultFailure(result_details="no report")]

            with pytest.raises(typer.Exit) as exc_info:
                doctor_command()

        assert exc_info.value.exit_code == 1

    def test_loads_libraries_before_checking(self) -> None:
        """Libraries are loaded first, or the library check would report none registered."""
        with (
            patch(f"{_MODULE}.GriptapeNodes.ahandle_request", new_callable=AsyncMock) as handle,
            patch(f"{_MODULE}.console"),
        ):
            handle.side_effect = [None, _success(HealthStatus.PASS)]
            doctor_command()

        requests = [call.args[0] for call in handle.call_args_list]
        assert type(requests[0]).__name__ == "LoadLibrariesRequest"
        assert type(requests[1]).__name__ == "RunHealthChecksRequest"
