"""Tests for the doctor CLI command.

The command is a view over `RunHealthChecksRequest`, so these pin what a view can get
wrong: the exit code it chooses, whether it survives a failed request, and whether the
verdict it prints under the table agrees with the table.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import typer
from rich.console import Console

from griptape_nodes.cli.commands.doctor import _print_health_report, doctor_command
from griptape_nodes.common.diagnostics.health import HealthCheckResult, HealthReport, HealthStatus
from griptape_nodes.retained_mode.events.diagnostics_events import (
    RunHealthChecksResultFailure,
    RunHealthChecksResultSuccess,
)

_MODULE = "griptape_nodes.cli.commands.doctor"

_ALL_CLEAR = "Everything checks out."

# The table is `expand=True`, so it fills whatever width it is given and its cells wrap to
# fit. Wide enough here that the short strings these tests look for are never broken across
# two lines, which would fail a substring assertion on text the user can plainly read.
_WIDE_ENOUGH_NOT_TO_WRAP = 200


def _health_report(status: HealthStatus, *, remedy: str | None = "what to do") -> HealthReport:
    """Build a report holding one check with the given status."""
    return HealthReport(
        generated_at="2026-01-01T00:00:00+00:00",
        status=status,
        results=[
            HealthCheckResult(
                name="Test Check",
                status=status,
                summary="what was found",
                remedy=None if status is HealthStatus.PASS else remedy,
            )
        ],
    )


def _printed(health: HealthReport) -> str:
    """Return everything the command printed for a report, as the text a user would see.

    A real recording ``Console`` rather than a Mock whose calls are stringified: most of
    what this command prints is a ``Table``, and ``str()`` on one of those is its repr, so
    a Mock cannot see a single check name, status, or summary. Wide enough that a cell is
    not wrapped mid-word, which would break a substring assertion on text that is present.
    """
    console = Console(record=True, width=_WIDE_ENOUGH_NOT_TO_WRAP, no_color=True, legacy_windows=False)
    with patch(f"{_MODULE}.console", console):
        _print_health_report(health)
    return console.export_text()


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


class TestPrintedVerdict:
    """The line under the table has to agree with the table, whatever the checks returned."""

    def test_says_so_when_everything_passed(self) -> None:
        assert _ALL_CLEAR in _printed(_health_report(HealthStatus.PASS))

    def test_prints_the_remedies_for_anything_that_did_not_pass(self) -> None:
        printed = _printed(_health_report(HealthStatus.FAIL))

        assert "what to do" in printed
        assert _ALL_CLEAR not in printed

    def test_a_failure_with_nothing_to_suggest_is_not_an_all_clear(self) -> None:
        """A check that could not run reports a failure and has no advice to offer.

        Decided on the overall status rather than on whether any remedy exists, so the
        all-clear can never print underneath a red FAIL row.
        """
        printed = _printed(_health_report(HealthStatus.FAIL, remedy=None))

        assert _ALL_CLEAR not in printed
        assert "did not pass" in printed

    def test_a_warning_with_nothing_to_suggest_is_not_an_all_clear(self) -> None:
        printed = _printed(_health_report(HealthStatus.WARN, remedy=None))

        assert _ALL_CLEAR not in printed
        assert "did not pass" in printed

    def test_a_report_with_no_checks_at_all_is_not_an_all_clear(self) -> None:
        """Nothing ran, which is a different statement from everything passing."""
        health = HealthReport(generated_at="2026-01-01T00:00:00+00:00", status=HealthStatus.FAIL, results=[])

        assert _ALL_CLEAR not in _printed(health)


class TestPrintedTable:
    """What each check found reaches the screen, not just the verdict underneath it."""

    def test_every_check_gets_a_row_naming_it_and_what_it_found(self) -> None:
        printed = _printed(_health_report(HealthStatus.FAIL))

        assert "Test Check" in printed
        assert "what was found" in printed
        assert "FAIL" in printed

    def test_a_check_name_holding_markup_is_shown_as_written(self) -> None:
        """Check names come from library and project names, so a user's own text lands here.

        Rich reads `[...]` as a style tag: unescaped, a project called `[beta] pipeline`
        prints as `pipeline` with the reader never told anything was dropped, and a tag that
        is not a real style raises instead.
        """
        health = HealthReport(
            generated_at="2026-01-01T00:00:00+00:00",
            status=HealthStatus.WARN,
            results=[
                HealthCheckResult(
                    name="[beta] pipeline",
                    status=HealthStatus.WARN,
                    summary="library [v2] did not load",
                    remedy=None,
                )
            ],
        )

        printed = _printed(health)

        assert "[beta] pipeline" in printed
        assert "library [v2] did not load" in printed
