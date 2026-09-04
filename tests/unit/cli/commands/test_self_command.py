"""Tests for what `gtn self info` prints from a diagnostics report.

The command is what a user is asked to paste into a bug report, so the report it renders is
usually the only view of their machine anyone else gets. Two kinds of mistake matter here.
The first is a section that quietly says nothing: a value that is None, an empty list, or a
count of zero all render as reasonable-looking output, and the difference between "there are
no libraries" and "the libraries could not be read" is the answer someone is looking for.
The second is Rich markup. Nearly everything printed here is a string from the user's own
machine -- a path, a config value, the text of an `OSError` -- and any of them may contain
square brackets, which Rich eats as a style tag. Every test that prints borrowed text checks
it survived.

Nothing here builds a report from a real engine; `test_diagnostics_manager_requests.py` does
that. These hand the printing code a report and read what came out.
"""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import AsyncMock, patch

import typer
from rich.console import Console

from griptape_nodes.cli.commands.self import info
from griptape_nodes.common.diagnostics.report import (
    ConfigDiagnostics,
    ConfigFileDiagnostics,
    DiagnosticsReport,
    EngineDiagnostics,
    HostDiagnostics,
    LibraryDiagnostics,
    LogDiagnostics,
    PathDiagnostics,
    ProjectDiagnostics,
    ProjectProblemDiagnostics,
    RedactionSummary,
    SecretDiagnostics,
)
from griptape_nodes.retained_mode.events.diagnostics_events import (
    GetDiagnosticsReportRequest,
    GetDiagnosticsReportResultFailure,
    GetDiagnosticsReportResultSuccess,
)

_MODULE = "griptape_nodes.cli.commands.self"

# Wide enough that no path, version string, or table row these tests look for is wrapped
# mid-word, which would make a substring assertion fail for a reason a user never sees.
_WIDE_ENOUGH_NOT_TO_WRAP = 200


def _recording_console() -> Console:
    """A console that keeps what was printed and shows it to nobody.

    Written to a string buffer rather than the terminal so the tests covering the failure
    path do not print alarming red messages into the suite's own output.
    """
    return Console(
        file=io.StringIO(),
        record=True,
        width=_WIDE_ENOUGH_NOT_TO_WRAP,
        no_color=True,
        legacy_windows=False,
    )


def _engine(**overrides: Any) -> EngineDiagnostics:
    fields: dict[str, Any] = {
        "engine_version": "1.2.3",
        "install_source": "pypi",
        "python_version": "3.13.1",
        "python_executable": "~/.local/share/uv/tools/griptape-nodes/bin/python",
        "process_id": 4242,
    }
    fields.update(overrides)
    return EngineDiagnostics(**fields)


def _host(**overrides: Any) -> HostDiagnostics:
    fields: dict[str, Any] = {
        "system": "Darwin",
        "release": "25.6.0",
        "version": "Darwin Kernel Version 25.6.0",
        "machine": "arm64",
    }
    fields.update(overrides)
    return HostDiagnostics(**fields)


def _report(**overrides: Any) -> DiagnosticsReport:
    """A report with every section filled in, so a test only has to state what it is about.

    The defaults are deliberately unremarkable: nothing missing, nothing disabled, no
    problems, no warnings, nothing hidden. A test about any of those says so itself, and a
    test asserting that a section was left out is not fighting a default that filled it in.
    """
    fields: dict[str, Any] = {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "engine": _engine(),
        "host": _host(),
        "paths": PathDiagnostics(
            workspace_directory="~/GriptapeNodes",
            config_directory="~/.config/griptape_nodes",
            user_config_file="~/.config/griptape_nodes/griptape_nodes_config.json",
            libraries_directory="~/GriptapeNodes/libraries",
            log_directory="~/.local/share/griptape_nodes/logs",
        ),
        "config": ConfigDiagnostics(
            files=[
                ConfigFileDiagnostics(
                    path="~/.config/griptape_nodes/griptape_nodes_config.json", layer="user", exists=True
                )
            ],
            merged={"log_level": "INFO"},
        ),
        "secrets": [
            SecretDiagnostics(
                name="GT_CLOUD_API_KEY",
                is_set=True,
                effective_source="environment variable",
                sources=["environment variable"],
                declared_in_config=True,
            )
        ],
        "libraries": [
            LibraryDiagnostics(
                name="Griptape Nodes Library", version="0.9.0", fitness="USABLE", lifecycle_state="LOADED"
            )
        ],
        "logs": LogDiagnostics(log_level="INFO", log_to_file=True, session_lines_captured=17),
    }
    fields.update(overrides)
    return DiagnosticsReport(**fields)


class _Run:
    """One invocation of the command, with the requests it made and the text it printed.

    Attributes:
        requests: Every request the command dispatched, in order.
        printed: Everything it printed, as the text a user would see.
        exit_code: The code the command exited with, or None when it returned normally.
            Recorded rather than left to propagate so the printed text is still readable
            after a failure -- a nonzero exit with nothing said is the thing being tested.
    """

    def __init__(self, requests: list[object], printed: str, exit_code: int | None) -> None:
        self.requests = requests
        self.printed = printed
        self.exit_code = exit_code

    def report_request(self) -> GetDiagnosticsReportRequest:
        """Return the one report request, failing if the command made none."""
        matches = [request for request in self.requests if isinstance(request, GetDiagnosticsReportRequest)]
        assert len(matches) == 1, f"expected exactly one report request, got {self.requests}"
        return matches[0]

    def request_type_names(self) -> list[str]:
        return [type(request).__name__ for request in self.requests]


def _run(result: object, *, show_identity: bool = False) -> _Run:
    """Invoke `gtn self info` against a stubbed engine and capture what it did.

    `info` is called as a plain function rather than through Typer's runner: its default is
    a Typer `OptionInfo` object, so the flag has to be passed explicitly either way, and
    calling it directly keeps the request objects themselves within reach instead of only
    their effect on an exit code.
    """
    console = _recording_console()

    def dispatch(request: object, **_kwargs: object) -> object:
        # Answered by request type rather than by call order: the library load comes first
        # and a positional list of answers would hand its answer to the report request.
        if isinstance(request, GetDiagnosticsReportRequest):
            return result
        return None

    exit_code: int | None = None
    with (
        patch(f"{_MODULE}.GriptapeNodes.ahandle_request", new_callable=AsyncMock) as handle,
        patch(f"{_MODULE}.console", console),
    ):
        handle.side_effect = dispatch
        try:
            info(show_identity=show_identity)
        except typer.Exit as exit_request:
            exit_code = exit_request.exit_code
        requests = [call.args[0] for call in handle.call_args_list]

    return _Run(requests, console.export_text(), exit_code)


def _printed(report: DiagnosticsReport, *, show_identity: bool = False) -> str:
    """Return the text the command printed for `report`."""
    result = GetDiagnosticsReportResultSuccess(report=report, result_details="collected")
    return _run(result, show_identity=show_identity).printed


class TestRequestBuiltFromFlags:
    def test_asks_for_a_report_it_is_safe_to_paste_into_a_bug_report(self) -> None:
        """The home directory and username are replaced unless the user asks for them."""
        request = _run(GetDiagnosticsReportResultSuccess(report=_report(), result_details="ok")).report_request()

        assert request.normalize_identity is True

    def test_show_identity_asks_for_the_real_paths(self) -> None:
        """Stated as the opposite of the field it sets, so an inverted wiring is silent."""
        request = _run(
            GetDiagnosticsReportResultSuccess(report=_report(), result_details="ok"), show_identity=True
        ).report_request()

        assert request.normalize_identity is False

    def test_the_report_is_not_broadcast_to_a_connected_editor(self) -> None:
        """This report was asked for in a terminal; a whole report on the event bus is noise."""
        request = _run(GetDiagnosticsReportResultSuccess(report=_report(), result_details="ok")).report_request()

        assert request.broadcast_result is False

    def test_loads_libraries_before_collecting(self) -> None:
        """Which libraries failed to load is usually the answer, and an unloaded engine has none."""
        run = _run(GetDiagnosticsReportResultSuccess(report=_report(), result_details="ok"))

        assert run.request_type_names() == ["LoadLibrariesRequest", "GetDiagnosticsReportRequest"]


class TestFailure:
    """What a user sees when the report could not be built."""

    def test_exits_one_so_a_script_can_tell(self) -> None:
        run = _run(GetDiagnosticsReportResultFailure(result_details="the workspace could not be read"))

        assert run.exit_code == 1

    def test_says_why_rather_than_only_exiting(self) -> None:
        run = _run(GetDiagnosticsReportResultFailure(result_details="the workspace could not be read"))

        assert "Failed to build a diagnostics report." in run.printed
        assert "the workspace could not be read" in run.printed

    def test_does_not_print_half_a_report(self) -> None:
        """Every section below reads `result.report`, which a failure result does not have."""
        run = _run(GetDiagnosticsReportResultFailure(result_details="the workspace could not be read"))

        assert "Griptape Nodes System Information" not in run.printed

    def test_a_reason_holding_markup_is_shown_as_written(self) -> None:
        """The reason quotes a path, and `[` is legal in one on every platform."""
        run = _run(GetDiagnosticsReportResultFailure(result_details="could not read '~/[old] workspace'"))

        assert "[old] workspace" in run.printed


class TestEngineAndPlatform:
    def test_says_what_is_running_and_where_it_came_from(self) -> None:
        printed = _printed(_report(engine=_engine(install_source="git", commit_id="abc1234")))

        assert "Version: 1.2.3" in printed
        assert "Install Source: git" in printed
        assert "Commit ID: abc1234" in printed
        assert "Process ID: 4242" in printed

    def test_a_version_that_could_not_be_read_says_unknown(self) -> None:
        """Printed as a word rather than left blank, so the line is not read as a version."""
        printed = _printed(_report(engine=_engine(engine_version=None, install_source=None)))

        assert "Version: unknown" in printed
        assert "Install Source: unknown" in printed

    def test_the_identifiers_support_matches_a_report_against_are_printed_when_known(self) -> None:
        """An engine talking to the editor has all three, and a bug report needs them to be traced."""
        engine = _engine(engine_name="studio-mac", engine_id="e-9f3c", session_id="s-771a")

        printed = _printed(_report(engine=engine))

        assert "Engine Name: studio-mac" in printed
        assert "Engine ID: e-9f3c" in printed
        assert "Session ID: s-771a" in printed

    def test_the_identifiers_a_local_run_does_not_have_are_left_out(self) -> None:
        """A commit id, engine name, and session id only exist under some ways of running."""
        printed = _printed(_report())

        assert "Commit ID:" not in printed
        assert "Engine Name:" not in printed
        assert "Session ID:" not in printed

    def test_says_what_machine_it_is_running_on(self) -> None:
        printed = _printed(_report(host=_host(cpu_count=10)))

        assert "OS: Darwin" in printed
        assert "OS Release: 25.6.0" in printed
        assert "Architecture: arm64" in printed
        assert "CPUs: 10" in printed

    def test_the_disk_the_workspace_is_on_is_reported_when_it_could_be_measured(self) -> None:
        """A full volume is the most common cause of a save or an install failing."""
        printed = _printed(_report(host=_host(workspace_disk_free_gb=12.5, workspace_disk_total_gb=460.4)))

        assert "Workspace Disk: 12.5 GB free of 460.4 GB" in printed

    def test_a_disk_that_could_not_be_measured_is_left_out(self) -> None:
        """Rather than printed as `None GB free`, which reads like a measurement of zero."""
        printed = _printed(_report())

        assert "Workspace Disk:" not in printed

    def test_a_python_version_spanning_several_lines_is_collapsed_onto_one(self) -> None:
        """`sys.version` carries a newline on some builds, which breaks the aligned block."""
        verbose = "3.13.1 (main, Dec  3 2025, 10:00:00)\n[Clang 17.0.0]"

        printed = _printed(_report(engine=_engine(python_version=verbose)))

        assert "Python Version: 3.13.1 (main, Dec 3 2025, 10:00:00) [Clang 17.0.0]" in printed


class TestPaths:
    def test_a_path_that_is_not_there_is_marked(self) -> None:
        """A missing config file explains a setting that appears not to apply."""
        paths = PathDiagnostics(
            workspace_directory="~/GriptapeNodes",
            user_config_file="~/.config/griptape_nodes/griptape_nodes_config.json",
            missing_paths=["~/.config/griptape_nodes/griptape_nodes_config.json"],
        )

        printed = _printed(_report(paths=paths))

        assert "Config File: ~/.config/griptape_nodes/griptape_nodes_config.json (missing)" in printed
        assert "Workspace Directory: ~/GriptapeNodes\n" in printed

    def test_a_path_the_engine_could_not_work_out_is_left_out(self) -> None:
        """Printed as `None`, a label reads like a directory literally named that."""
        printed = _printed(_report(paths=PathDiagnostics(workspace_directory="~/GriptapeNodes")))

        assert "Libraries Directory:" not in printed
        assert "Log Directory:" not in printed

    def test_a_path_holding_brackets_is_printed_as_written(self) -> None:
        """`[` is legal in a directory name on every platform, and Rich eats it as a tag."""
        printed = _printed(_report(paths=PathDiagnostics(workspace_directory="~/[2026] projects")))

        assert "~/[2026] projects" in printed


class TestConfiguration:
    def test_the_files_are_listed_in_the_order_they_override_each_other(self) -> None:
        """A setting that appears not to apply is usually overridden by a later file."""
        files = [
            ConfigFileDiagnostics(
                path="~/.config/griptape_nodes/griptape_nodes_config.json", layer="user", exists=True
            ),
            ConfigFileDiagnostics(path="~/GriptapeNodes/griptape_nodes_config.json", layer="workspace", exists=False),
        ]

        printed = _printed(_report(config=ConfigDiagnostics(files=files)))

        user_line = printed.index("user: ~/.config/griptape_nodes/griptape_nodes_config.json (found)")
        workspace_line = printed.index("workspace: ~/GriptapeNodes/griptape_nodes_config.json (not present)")
        assert user_line < workspace_line

    def test_the_environment_variables_that_beat_every_file_are_named(self) -> None:
        """Names only. The values are whatever the user's shell holds, which may be a secret."""
        config = ConfigDiagnostics(environment_overrides=["GTN_CONFIG_LOG_LEVEL"], merged={"log_level": "DEBUG"})

        printed = _printed(_report(config=config))

        assert "Environment Overrides" in printed
        assert "GTN_CONFIG_LOG_LEVEL" in printed

    def test_nothing_is_said_about_environment_overrides_when_there_are_none(self) -> None:
        printed = _printed(_report())

        assert "Environment Overrides" not in printed

    def test_the_settings_are_printed_as_the_report_holds_them(self) -> None:
        """A credential-shaped setting arrives already replaced, and must stay replaced."""
        config = ConfigDiagnostics(merged={"nodes": {"Griptape": {"env": "<redacted>"}}})

        printed = _printed(_report(config=config))

        assert '"env": "<redacted>"' in printed

    def test_a_setting_whose_value_holds_markup_is_printed_as_written(self) -> None:
        """Settings hold arbitrary strings: a prompt, a file name, a template."""
        config = ConfigDiagnostics(merged={"default_prompt": "[bold]summarize this[/bold]"})

        printed = _printed(_report(config=config))

        assert "[bold]summarize this[/bold]" in printed


class TestSecrets:
    def test_a_secret_is_shown_by_name_and_by_whether_it_has_a_value(self) -> None:
        printed = _printed(_report())

        assert "GT_CLOUD_API_KEY" in printed
        assert "environment variable" in printed

    def test_a_secret_with_no_value_anywhere_says_so(self) -> None:
        """The engine expects this key and cannot find it, which is the whole answer."""
        secret = SecretDiagnostics(name="HF_TOKEN", is_set=False, declared_in_config=True)

        printed = _printed(_report(secrets=[secret]))

        assert "not set" in printed
        assert "HF_TOKEN" in printed

    def test_only_the_shadowed_sources_are_listed_as_also_found_in(self) -> None:
        """Repeating the winning source would make every row look like a shadowing problem."""
        secret = SecretDiagnostics(
            name="HF_TOKEN",
            is_set=True,
            effective_source="environment variable",
            sources=["environment variable", "global .env"],
        )

        printed = _printed(_report(secrets=[secret]))

        assert "global .env" in printed
        assert printed.count("environment variable") == 1

    def test_no_secrets_at_all_says_so_rather_than_printing_an_empty_table(self) -> None:
        printed = _printed(_report(secrets=[]))

        assert "No secrets found" in printed


class TestLibraries:
    def test_each_library_is_listed_with_how_usable_it_is(self) -> None:
        printed = _printed(_report())

        assert "Griptape Nodes Library" in printed
        assert "USABLE" in printed
        assert "LOADED" in printed

    def test_a_library_that_was_turned_off_is_marked_rather_than_looking_broken(self) -> None:
        """A disabled library did not fail; it was not asked to load."""
        library = LibraryDiagnostics(name="Old Library", enabled=False, lifecycle_state="UNLOADED")

        printed = _printed(_report(libraries=[library]))

        assert "Old Library (disabled)" in printed

    def test_a_version_that_could_not_be_read_says_unknown(self) -> None:
        printed = _printed(_report(libraries=[LibraryDiagnostics(name="Broken Library")]))

        assert "unknown" in printed

    def test_the_problems_are_printed_in_full_below_the_table(self) -> None:
        """The table has no room for them, and they are usually the reason for running this."""
        library = LibraryDiagnostics(
            name="Broken Library",
            problems="ModuleNotFoundError: No module named 'torch' [while importing nodes/gen.py]",
        )

        printed = _printed(_report(libraries=[library]))

        assert "Library Problems:" in printed
        assert "No module named 'torch' [while importing nodes/gen.py]" in printed

    def test_nothing_is_said_about_problems_when_every_library_loaded(self) -> None:
        printed = _printed(_report())

        assert "Library Problems:" not in printed

    def test_no_libraries_at_all_says_so_rather_than_printing_an_empty_table(self) -> None:
        """The most reported symptom there is: an editor with no nodes in it."""
        printed = _printed(_report(libraries=[]))

        assert "No libraries registered" in printed


class TestProjects:
    def test_the_section_is_left_out_when_no_project_templates_were_loaded(self) -> None:
        """Most installations have none, and an empty table reads like something is missing."""
        printed = _printed(_report())

        assert "Projects:" not in printed

    def test_the_project_the_engine_is_running_under_is_marked(self) -> None:
        project = ProjectDiagnostics(project_id="p-1", name="Studio", validation_status="GOOD", is_current=True)

        printed = _printed(_report(projects=[project]))

        assert "Studio" in printed
        assert "GOOD" in printed

    def test_a_template_that_could_not_be_loaded_says_so_next_to_its_status(self) -> None:
        """`UNUSABLE` describes the file; failing to load is what the engine did about it."""
        project = ProjectDiagnostics(project_id="p-2", name="Broken", validation_status="UNUSABLE", loaded=False)

        printed = _printed(_report(projects=[project]))

        assert "UNUSABLE (failed to load)" in printed

    def test_a_template_with_no_name_is_listed_by_its_id(self) -> None:
        """A template that failed to parse may not have got as far as having a name."""
        printed = _printed(_report(projects=[ProjectDiagnostics(project_id="p-3", validation_status="UNUSABLE")]))

        assert "p-3" in printed

    def test_a_problem_is_printed_with_where_in_the_template_it_was_found(self) -> None:
        problem = ProjectProblemDiagnostics(
            severity="error",
            field_path="situations.copy_external_file.macro",
            message="unknown macro '[workspace]'",
            line_number=42,
        )
        project = ProjectDiagnostics(project_id="p-4", name="Studio", problems=[problem])

        printed = _printed(_report(projects=[project]))

        assert "Project Problems:" in printed
        assert "error: situations.copy_external_file.macro:42: unknown macro '[workspace]'" in printed

    def test_a_problem_found_somewhere_with_no_line_number_still_names_the_field(self) -> None:
        problem = ProjectProblemDiagnostics(severity="warning", field_path="name", message="is empty")
        project = ProjectDiagnostics(project_id="p-5", problems=[problem])

        printed = _printed(_report(projects=[project]))

        assert "warning: name: is empty" in printed


class TestLogs:
    def test_says_how_logging_is_set_up_and_what_history_there_is(self) -> None:
        """The log level bounds what any log file can contain, so it is read before them."""
        printed = _printed(_report())

        assert "Log Level: INFO" in printed
        assert "Write Log Files: True" in printed
        assert "Log Files Available: 0" in printed
        assert "Lines Captured This Session: 17" in printed

    def test_keeping_log_files_forever_is_said_in_words(self) -> None:
        """Zero is the setting for it, and `0 day(s)` reads like they are deleted at once."""
        printed = _printed(_report(logs=LogDiagnostics(log_level="INFO", retention_days=0)))

        assert "Keep Log Files For: forever" in printed

    def test_a_retention_window_is_reported_in_days(self) -> None:
        printed = _printed(_report(logs=LogDiagnostics(log_level="INFO", retention_days=7)))

        assert "Keep Log Files For: 7 day(s)" in printed


class TestWhatCouldNotBeCollectedAndWhatWasHidden:
    def test_a_section_that_could_not_be_gathered_is_named(self) -> None:
        """Otherwise it reads as empty, and empty is a different answer from unreadable."""
        printed = _printed(_report(collection_warnings=["The libraries directory could not be read: [Errno 13]"]))

        assert "Could Not Be Collected:" in printed
        assert "The libraries directory could not be read: [Errno 13]" in printed

    def test_nothing_is_said_when_every_section_was_gathered(self) -> None:
        printed = _printed(_report())

        assert "Could Not Be Collected:" not in printed

    def test_the_number_of_hidden_values_is_printed_with_the_reason_for_each(self) -> None:
        """A hidden value and an absent one look identical, so the count is what tells them apart."""
        redaction = RedactionSummary(identity_normalized=True, total=3, counts={"config_key": 2, "home_directory": 1})

        printed = _printed(_report(redaction=redaction))

        assert "3 value(s) were hidden" in printed
        assert "config_key: 2" in printed
        assert "home_directory: 1" in printed

    def test_hiding_nothing_is_said_out_loud(self) -> None:
        """So a reader is never left wondering whether something was taken out silently."""
        printed = _printed(_report())

        assert "No values were hidden from this output." in printed
