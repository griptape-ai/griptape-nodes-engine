"""Tests for the `gtn diagnostics collect` CLI command.

The command is a view over `CollectDiagnosticsRequest`, so what it can get wrong is the request
it builds from its flags and what it does when collection fails. Every flag turns a piece of the
bundle *off*, so one wired to the wrong field silently produces a bundle missing the part someone
is waiting for; the failure path is what a user hits when the destination is not writable.
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import AsyncMock, patch

import typer
from rich.console import Console

from griptape_nodes.cli.commands.diagnostics import collect
from griptape_nodes.common.diagnostics.bundle import BundleEntry, DiagnosticsBundleManifest
from griptape_nodes.common.diagnostics.report import RedactionSummary
from griptape_nodes.retained_mode.events.diagnostics_events import (
    CollectDiagnosticsRequest,
    CollectDiagnosticsResultFailure,
    CollectDiagnosticsResultSuccess,
)

_MODULE = "griptape_nodes.cli.commands.diagnostics"

# Wide enough that the paths and messages these tests look for are never wrapped mid-word.
_WIDE_ENOUGH_NOT_TO_WRAP = 200

# Where a stubbed collection claims the bundle landed. A fixed string to assert on, never
# written to and never opened.
_WRITTEN_TO = "/tmp/bundle.zip"  # noqa: S108


def _recording_console() -> Console:
    """A console that keeps what was printed and shows it to nobody.

    Written to a string buffer rather than the terminal so a test that exercises the
    failure path does not print an alarming red message into the suite's own output.
    """
    return Console(
        file=io.StringIO(),
        record=True,
        width=_WIDE_ENOUGH_NOT_TO_WRAP,
        no_color=True,
        legacy_windows=False,
    )


def _manifest(*, warnings: list[str] | None = None) -> DiagnosticsBundleManifest:
    return DiagnosticsBundleManifest(
        generated_at="2026-01-01T00:00:00+00:00",
        engine_version="1.2.3",
        redaction=RedactionSummary(identity_normalized=True, total=4, counts={"config_key": 4}),
        entries=[BundleEntry(path="report.json", size_bytes=10, description="the report")],
        warnings=warnings or [],
    )


def _success(*, warnings: list[str] | None = None) -> CollectDiagnosticsResultSuccess:
    return CollectDiagnosticsResultSuccess(
        file_name="bundle.zip",
        size_bytes=2048,
        manifest=_manifest(warnings=warnings),
        path=_WRITTEN_TO,
        result_details="collected",
    )


class _Run:
    """One invocation of the command, with the requests it made and the text it printed.

    Attributes:
        requests: Every request the command dispatched, in order.
        printed: Everything it printed, as the text a user would see.
        exit_code: The code it exited with, or None when it returned normally. Recorded rather
            than left to propagate, since a nonzero exit with nothing said is what is tested.
    """

    def __init__(self, requests: list[object], printed: str, exit_code: int | None) -> None:
        self.requests = requests
        self.printed = printed
        self.exit_code = exit_code

    def collect_request(self) -> CollectDiagnosticsRequest:
        """Return the one collection request, failing if the command made none."""
        matches = [request for request in self.requests if isinstance(request, CollectDiagnosticsRequest)]
        assert len(matches) == 1, f"expected exactly one collection request, got {self.requests}"
        return matches[0]

    def request_type_names(self) -> list[str]:
        return [type(request).__name__ for request in self.requests]


def _run(result: object, *, library_load: object = None, **kwargs: object) -> _Run:
    """Invoke the command with a stubbed engine and capture what it did.

    ``collect`` is called as a plain function rather than through Typer's runner: the defaults are
    ``OptionInfo`` objects, so every unset keyword has to be passed anyway, and this keeps the
    request objects within reach instead of only their effect on an exit code.

    Args:
        result: What the collection request returns, or an exception for it to raise. Raising
            stands for an engine that could not be built, since dispatching is what builds one.
        library_load: The same, for the library load that runs before it.
        kwargs: Flags to override on the command.
    """
    console = _recording_console()
    options: dict[str, object] = {
        "output": Path("/tmp/bundles"),  # noqa: S108 - never written to; the request is stubbed
        "skip_logs": False,
        "skip_libraries": False,
        "show_identity": False,
    }
    options.update(kwargs)

    def dispatch(request: object, **_kwargs: object) -> object:
        # Answered by request type, not call order: `--skip-libraries` changes how many requests
        # there are, and a positional list would hand the library load's answer to the collection.
        if isinstance(request, CollectDiagnosticsRequest):
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
            collect(**options)  # type: ignore[arg-type]
        except typer.Exit as exit_request:
            exit_code = exit_request.exit_code
        requests = [call.args[0] for call in handle.call_args_list]

    return _Run(requests, console.export_text(), exit_code)


class TestRequestBuiltFromFlags:
    """Each flag turns off one part of the bundle, and only that part."""

    def test_collects_everything_but_the_open_workflow_by_default(self) -> None:
        """Nothing is open in a CLI-launched engine, so asking for a workflow only adds a warning."""
        request = _run(_success()).collect_request()

        assert request.include_logs is True
        assert request.normalize_identity is True
        assert request.include_current_workflow is False

    def test_skip_logs_leaves_the_logs_out(self) -> None:
        request = _run(_success(), skip_logs=True).collect_request()

        assert request.include_logs is False
        # The other switches are independent of it, which is the wiring a flag gets wrong.
        assert request.normalize_identity is True

    def test_show_identity_stops_the_home_directory_being_replaced(self) -> None:
        """The flag is stated as the opposite of the field, so an inverted wiring is silent."""
        request = _run(_success(), show_identity=True).collect_request()

        assert request.normalize_identity is False
        assert request.include_logs is True

    def test_writes_to_the_requested_path_rather_than_uploading(self, tmp_path: Path) -> None:
        """An output path is what keeps the bundle on this machine; None uploads it.

        Compared against `tmp_path` rather than a literal because the command hands the request a
        string: spelled `/tmp/somewhere`, the expected value gets the wrong separator on Windows.
        """
        request = _run(_success(), output=tmp_path).collect_request()

        assert request.output_path == str(tmp_path)

    def test_the_default_output_is_the_current_directory(self) -> None:
        """Never None: None hands the bundle to static files, which can leave the machine."""
        request = _run(_success(), output=Path()).collect_request()

        assert request.output_path == "."

    def test_loads_libraries_before_collecting(self) -> None:
        """Which libraries failed to load is usually the answer, and an unloaded engine has none."""
        run = _run(_success())

        assert run.request_type_names() == ["LoadLibrariesRequest", "CollectDiagnosticsRequest"]

    def test_skip_libraries_collects_without_loading_them(self) -> None:
        run = _run(_success(), skip_libraries=True)

        assert run.request_type_names() == ["CollectDiagnosticsRequest"]


class TestOutput:
    def test_reports_where_the_bundle_landed_and_what_was_hidden(self) -> None:
        printed = _run(_success()).printed

        assert _WRITTEN_TO in printed
        # The count is the point of reporting it: "hidden" and "absent" look identical
        # in a bundle otherwise.
        assert "Values hidden: 4" in printed

    def test_shows_what_was_left_out(self) -> None:
        """A shortened log changes what a bundle can prove, so it is never only in the manifest."""
        printed = _run(_success(warnings=["Log file 'engine-1.log' was left out."])).printed

        assert "Left out of this bundle:" in printed
        assert "engine-1.log" in printed

    def test_a_warning_holding_markup_is_shown_as_written(self) -> None:
        """Warnings quote file names and OSError text, so a user's own `[...]` reaches here."""
        printed = _run(_success(warnings=["The workflow '[draft] flow.py' could not be read."])).printed

        assert "[draft] flow.py" in printed


class TestFailure:
    """What a user sees when the bundle could not be written -- a full disk, or no permission."""

    def test_exits_one_so_a_script_can_tell(self) -> None:
        run = _run(CollectDiagnosticsResultFailure(result_details="no permission to write to '/tmp/bundles'"))

        assert run.exit_code == 1

    def test_says_why_rather_than_only_exiting(self) -> None:
        """A bare nonzero exit leaves a user with nothing to fix."""
        run = _run(CollectDiagnosticsResultFailure(result_details="no permission to write to '/tmp/bundles'"))

        assert "Failed to write it." in run.printed
        assert "no permission to write to" in run.printed

    def test_does_not_claim_a_bundle_was_written(self) -> None:
        """The success block reads `result.manifest`, which a failure result does not have."""
        run = _run(CollectDiagnosticsResultFailure(result_details="the bundle could not be assembled"))

        assert "Diagnostics bundle written to:" not in run.printed

    def test_a_failure_reason_holding_markup_is_shown_as_written(self) -> None:
        """The reason quotes a path, and `[` is legal in one on every platform."""
        run = _run(CollectDiagnosticsResultFailure(result_details="could not write to '/tmp/[archive] logs'"))

        assert "[archive] logs" in run.printed


class TestAnEngineThatWillNotStart:
    """A broken engine is the usual reason somebody is collecting a bundle at all.

    Either request can be the one that builds the engine, so a config file it cannot get past, or
    a library that raises on import, surfaces as an exception out of a dispatch. Unguarded, the
    command answered with a traceback -- the thing the user already had.
    """

    def test_an_engine_that_cannot_be_built_is_explained_rather_than_traced(self) -> None:
        run = _run(RuntimeError("the config file could not be parsed"))

        assert run.exit_code == 1
        assert "The engine itself could not be started" in run.printed
        assert "the config file could not be parsed" in run.printed

    def test_libraries_that_will_not_load_do_not_stop_the_collection(self) -> None:
        """Which libraries are broken is what the bundle is being collected to show.

        The failure is recorded in the bundle, every other section still describes the machine
        somebody is asking about, and the collection goes ahead.
        """
        run = _run(_success(), library_load=RuntimeError("a node library raised on import"))

        assert run.exit_code is None
        assert "a node library raised on import" in run.printed
        assert "Diagnostics bundle written to:" in run.printed

    def test_the_text_of_a_failure_holding_markup_is_shown_as_written(self) -> None:
        """These messages quote an exception raised while reading the user's own files.

        A config path or library name can hold square brackets, which Rich reads as a style tag:
        unescaped, the part naming what broke is dropped silently, or an unknown tag raises.
        """
        run = _run(RuntimeError("could not read [beta] settings"))

        assert "could not read [beta] settings" in run.printed

    def test_a_library_load_failure_holding_markup_is_shown_as_written(self) -> None:
        run = _run(_success(), library_load=RuntimeError("library [v2] raised on import"))

        assert "library [v2] raised on import" in run.printed
