"""Tests for the doctor CLI command.

The command is a view over `RunHealthChecksRequest`, so these pin what a view can get
wrong: the exit code it chooses, whether it survives a failed request, and whether the
verdict it prints under the table agrees with the table.
"""

from __future__ import annotations

import io
from unittest.mock import AsyncMock, patch

import typer
from rich.console import Console

from griptape_nodes.cli.commands.doctor import _print_health_report, doctor_command
from griptape_nodes.common.diagnostics.health import HealthCheckResult, HealthReport, HealthStatus
from griptape_nodes.retained_mode.events.diagnostics_events import (
    RunHealthChecksRequest,
    RunHealthChecksResultFailure,
    RunHealthChecksResultSuccess,
)

_MODULE = "griptape_nodes.cli.commands.doctor"

_ALL_CLEAR = "Everything checks out."

# The table is `expand=True`, so cells wrap to the width it is given. Wide enough here that
# the short strings these tests assert on are never broken across two lines.
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


class _Run:
    """One invocation of the command, with the requests it made and the text it printed.

    Attributes:
        requests: Every request the command dispatched, in order.
        printed: Everything it printed, as the text a user would see.
        exit_code: The code the command exited with, or None when it returned normally.
            None is the zero exit: Typer takes a normal return as success and only a
            `typer.Exit` carries a code, so "exits zero" is asserted as the absence of one.
    """

    def __init__(self, requests: list[object], printed: str, exit_code: int | None) -> None:
        self.requests = requests
        self.printed = printed
        self.exit_code = exit_code

    def request_type_names(self) -> list[str]:
        return [type(request).__name__ for request in self.requests]


def _run(result: object, *, library_load: object = None) -> _Run:
    """Invoke the command with a stubbed engine and capture what it did.

    Printed into a string buffer rather than a Mock console, so a test can assert the
    command got as far as printing its verdict instead of only that it did not raise.

    Args:
        result: What the health-check request returns, or an exception for it to raise.
            Raising stands for an engine that could not be built at all, since dispatching
            the request is what builds one.
        library_load: The same, for the library load that runs before it.
    """
    console = Console(
        file=io.StringIO(), record=True, width=_WIDE_ENOUGH_NOT_TO_WRAP, no_color=True, legacy_windows=False
    )

    def dispatch(request: object, **_kwargs: object) -> object:
        # Answered by request type, not call order: a positional list would hand the library load's
        # answer to the health checks the day the command dispatches one more request.
        if isinstance(request, RunHealthChecksRequest):
            if isinstance(result, Exception):
                raise result
            return result
        if isinstance(library_load, Exception):
            raise library_load
        return library_load

    exit_code: int | None = None
    with (
        patch(f"{_MODULE}.GriptapeNodes.ahandle_request", new_callable=AsyncMock) as handle,
        patch(f"{_MODULE}.console", console),
    ):
        handle.side_effect = dispatch
        try:
            doctor_command()
        except typer.Exit as exit_request:
            exit_code = exit_request.exit_code
        requests = [call.args[0] for call in handle.call_args_list]

    return _Run(requests, console.export_text(), exit_code)


class TestDoctorCommand:
    def test_exits_zero_when_every_check_passes(self) -> None:
        """A clean bill of health is a zero exit, with the verdict still printed."""
        run = _run(_success(HealthStatus.PASS))

        assert run.exit_code is None
        assert _ALL_CLEAR in run.printed

    def test_exits_zero_when_a_check_only_warns(self) -> None:
        """A warning is something to fix eventually, so scripts calling this must not break."""
        run = _run(_success(HealthStatus.WARN))

        assert run.exit_code is None
        # Printed rather than only exited over: a warning nobody is shown is a warning
        # nobody acts on.
        assert "Test Check: what to do" in run.printed

    def test_exits_one_when_a_check_fails(self) -> None:
        """A failing check exits nonzero, which is what the documented script usage relies on."""
        run = _run(_success(HealthStatus.FAIL))

        assert run.exit_code == 1

    def test_exits_one_when_the_checks_could_not_run(self) -> None:
        """A request that fails outright is reported rather than crashing the command."""
        run = _run(RunHealthChecksResultFailure(result_details="no report"))

        assert run.exit_code == 1
        assert "no report" in run.printed

    def test_loads_libraries_before_checking(self) -> None:
        """Libraries are loaded first, or the library check would report none registered."""
        run = _run(_success(HealthStatus.PASS))

        assert run.request_type_names() == ["LoadLibrariesRequest", "RunHealthChecksRequest"]


class TestAnEngineThatWillNotStart:
    """The most likely reason somebody is running this command is that something is broken.

    Both of the requests it makes are what build the engine in the first place, so a config
    file the engine cannot get past, or a node library that raises on import, surfaces as an
    exception out of a dispatch. Unguarded, the tool that exists to explain that answered
    with a traceback -- the same thing the user had already seen, and the reason they came
    here.
    """

    def test_an_engine_that_cannot_be_built_is_explained_rather_than_traced(self) -> None:
        run = _run(RuntimeError("the config file could not be parsed"))

        assert run.exit_code == 1
        assert "The engine itself could not be started" in run.printed
        assert "the config file could not be parsed" in run.printed

    def test_libraries_that_will_not_load_do_not_stop_the_checks(self) -> None:
        """Which libraries are broken is one of the checks, so this is a finding, not a stop.

        The library check reports what failed to arrive, which is more use than the import
        error on its own -- and every other check still has something to say about a machine
        whose libraries are broken.
        """
        run = _run(_success(HealthStatus.PASS), library_load=RuntimeError("a node library raised on import"))

        assert run.exit_code is None
        assert "a node library raised on import" in run.printed
        assert _ALL_CLEAR in run.printed

    def test_the_text_of_a_failure_holding_markup_is_shown_as_written(self) -> None:
        """These messages quote an exception raised while reading the user's own files.

        A config path or a library name can hold square brackets, which Rich reads as a style
        tag: unescaped, the part of the message that names what broke is dropped silently, or
        an unknown tag raises a second error on top of the first.
        """
        run = _run(RuntimeError("could not read [beta] settings"))

        assert "could not read [beta] settings" in run.printed


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
