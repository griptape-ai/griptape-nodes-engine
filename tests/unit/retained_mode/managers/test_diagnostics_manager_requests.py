"""The three diagnostics requests, dispatched through a real engine.

Every other test in this area builds one section, or hands the manager a stand-in engine.
These go through `engine.ahandle_request` the way the editor and the CLI do, because some
of what a user depends on only exists once the pieces are assembled: that the file written
is a zip that opens, that the manifest describes the archive it is inside, that a secret
the engine logged a second ago is not in it, and that a destination which cannot be written
to comes back as a failure result rather than an exception.

Nothing here touches the network. `CloudConnectionCheck` is the only check that would, and
the tests that run the checks replace the call that opens the socket.
"""

from __future__ import annotations

import io
import json
import logging
import os
import platform
import sys
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from griptape_nodes.common.diagnostics.bundle import (
    HEALTH_FILE_NAME,
    LOGS_DIRECTORY_NAME,
    MANIFEST_FILE_NAME,
    README_FILE_NAME,
    REPORT_FILE_NAME,
    SESSION_LOG_FILE_NAME,
)
from griptape_nodes.common.diagnostics.health import CLOUD_API_KEY_NAME, CloudConnectionCheck, HealthStatus
from griptape_nodes.common.diagnostics.redaction import REDACTED, RedactionReason
from griptape_nodes.common.log_capture import LOGGER_NAME, active_log_file
from griptape_nodes.retained_mode.events.diagnostics_events import (
    CollectDiagnosticsRequest,
    CollectDiagnosticsResultFailure,
    CollectDiagnosticsResultSuccess,
    GetDiagnosticsReportRequest,
    GetDiagnosticsReportResultFailure,
    GetDiagnosticsReportResultSuccess,
    RunHealthChecksRequest,
    RunHealthChecksResultFailure,
    RunHealthChecksResultSuccess,
)
from griptape_nodes.retained_mode.managers.diagnostics_manager import DiagnosticsManager
from griptape_nodes.retained_mode.managers.settings import SECRETS_TO_REGISTER_KEY

if TYPE_CHECKING:
    from griptape_nodes.common.diagnostics.report import DiagnosticsReport
    from griptape_nodes.retained_mode.engine import Engine

# A secret name no library uses, so the entry these tests find in a report is theirs.
# S105 reads any name ending in KEY as a credential; this is the key's name, not its value.
_SECRET_NAME = "GTN_TEST_DIAGNOSTICS_KEY"  # noqa: S105

# A canary, not a credential: a made-up value planted where a real key would be, long
# enough to be searched for in free text and distinctive enough that finding it anywhere in
# a bundle is unambiguous rather than a coincidence of the machine it ran on. Named for what
# it is rather than as a secret, because a value with `secret` in its name flowing into a
# `logger.warning` below is read as a credential being logged in the clear -- by a reader,
# and by the security scanner that runs on every pull request.
_CANARY_VALUE = "gtn-test-canary-value-4b17e9c0"

# Stands in for the real Griptape Cloud key so the connection check has something to
# authenticate with. Never sent anywhere: every test that runs the checks replaces the
# call that would open the socket.
_CLOUD_KEY = "gtn-test-cloud-key-not-a-real-one"

_HOST_UNIDENTIFIABLE = "the platform could not be identified"


@pytest.fixture
def declared_canary(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> str:
    """Give the engine one secret it knows about, and return the canary set as its value.

    Declared in the config so the report has a name to report, and set in the environment
    so there is a real value for the redactor to find in text. Both halves are needed: the
    report lists names it was told to expect, and it scrubs values it was able to read.
    """
    engine.config_manager.set_config_value(SECRETS_TO_REGISTER_KEY, [_SECRET_NAME])
    monkeypatch.setenv(_SECRET_NAME, _CANARY_VALUE)
    return _CANARY_VALUE


@pytest.fixture
def something_in_the_log(engine: Engine) -> str:  # noqa: ARG001 - the sinks are installed with the engine
    """Log one line and return it, so there is a session log for a bundle to carry.

    An engine that has logged nothing has no session log, and says so in the manifest
    rather than staging an empty file. Real, but it makes every assertion about a bundle's
    logs vacuous, so a test that is about the logs puts something in them first.
    """
    message = "the engine did something worth keeping a record of"
    logging.getLogger(LOGGER_NAME).warning(message)
    return message


@pytest.fixture
def logs_under_a_fake_home(engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the engine's log directory inside a home directory this test owns.

    The paths section is where the home directory reaches a report, and a real home
    directory is not something a test can assert against -- `tmp_path` is not inside one,
    so with the real `Path.home` there would be nothing to replace and a report that
    normalized nothing would pass. So `Path.home` is answered with a directory built here,
    and a setting that really ends up in the report is pointed inside it.
    """
    home = tmp_path / "home"
    log_directory = home / "logs"
    monkeypatch.setattr(Path, "home", lambda: home)
    engine.config_manager.set_config_value("logging.log_directory", str(log_directory))
    return log_directory


async def _report(engine: Engine, *, normalize_identity: bool = True) -> DiagnosticsReport:
    """Collect a report and return it, failing the test when the request did not succeed."""
    result = await engine.ahandle_request(GetDiagnosticsReportRequest(normalize_identity=normalize_identity))

    assert isinstance(result, GetDiagnosticsReportResultSuccess), result.result_details
    return result.report


async def _collect(engine: Engine, output: Path, **overrides: object) -> CollectDiagnosticsResultSuccess:
    """Collect a bundle into `output` and return the successful result.

    Health checks are off unless a test asks for them, because one of them opens a socket.
    The open workflow is off because a test engine has none, and asking for one only adds a
    warning to the manifest.
    """
    options: dict[str, Any] = {
        "output_path": str(output),
        "include_health_checks": False,
        "include_current_workflow": False,
    }
    options.update(overrides)

    result = await engine.ahandle_request(CollectDiagnosticsRequest(**options))

    assert isinstance(result, CollectDiagnosticsResultSuccess), result.result_details
    return result


def _members(result: CollectDiagnosticsResultSuccess) -> dict[str, str]:
    """Return every file in a written bundle, decoded, keyed by its path inside the zip.

    Read back off the disk rather than from the result, because where the file landed is
    half of what a collection promises. Read as members rather than as raw zip bytes: the
    archive is deflated, so a plaintext secret in a member is not plaintext in the bytes,
    and a search over the bytes would report a clean bundle for a leaking one.
    """
    assert result.path is not None, "a bundle written to a path reports where it went"

    with zipfile.ZipFile(io.BytesIO(Path(result.path).read_bytes())) as archive:
        return {
            name: archive.read(name).decode("utf-8", errors="replace")
            for name in archive.namelist()
            if not name.endswith("/")
        }


def _size_on_disk(path: str) -> int:
    """Return the size of the file at `path`.

    Sync, like `_members` above, so an async test body performs no direct Path I/O
    (ASYNC240).
    """
    return Path(path).stat().st_size


def _zips_written_under(directory: Path) -> list[Path]:
    """Return every zip anywhere under `directory`, for asserting a failure wrote none.

    Sync for the same reason as `_size_on_disk`. Searched recursively rather than at the top
    level: a bundle that escaped into a subdirectory is exactly the outcome being ruled out.
    """
    return list(directory.rglob("*.zip"))


class TestReport:
    """`GetDiagnosticsReportRequest`: what the engine says about itself, redacted."""

    @pytest.mark.asyncio
    async def test_says_what_is_running_and_on_what(self, engine: Engine) -> None:
        """The two sections a report cannot do without: lacking them it identifies nothing."""
        report = await _report(engine)

        assert report.engine.process_id == os.getpid()
        assert report.engine.python_version == sys.version
        assert report.host.system == platform.system()
        assert report.host.machine == platform.machine()

    @pytest.mark.asyncio
    async def test_every_section_is_there_on_an_engine_with_nothing_loaded(self, engine: Engine) -> None:
        """A section nothing could be gathered for is empty, never absent.

        This is the shape `report.json` is written in, and a support tool reads it by name.
        An engine with no libraries and no open workflow is an ordinary state -- it is what
        `gtn doctor` runs against -- so it must not be the state that drops a key.
        """
        report = await _report(engine)

        assert set(json.loads(report.model_dump_json())) >= {
            "engine",
            "host",
            "paths",
            "config",
            "secrets",
            "libraries",
            "projects",
            "logs",
            "session",
            "collection_warnings",
            "redaction",
        }

    @pytest.mark.asyncio
    async def test_the_log_file_this_engine_is_writing_to_is_marked_as_the_active_one(
        self, engine: Engine, something_in_the_log: str
    ) -> None:
        """Which file holds the session being asked about, out of a directory of them.

        The engine's log directory and the file the sink opened are the same location
        spelled two ways -- on macOS a temporary directory is reached through `/var`, which
        is a symlink to `/private/var`. Compared as spellings, nothing was ever marked, and
        a reader with six log files had no way to tell which one was live.
        """
        assert something_in_the_log

        report = await _report(engine)

        active = [entry.name for entry in report.logs.files if entry.is_active]
        assert active == [Path(str(active_log_file())).name]

    @pytest.mark.asyncio
    async def test_the_home_directory_is_replaced_with_a_tilde(
        self, engine: Engine, logs_under_a_fake_home: Path
    ) -> None:
        """What `normalize_identity` is for: a report is about to be pasted somewhere public."""
        report = await _report(engine)

        assert report.paths.log_directory == f"~{os.sep}logs"
        assert report.redaction.identity_normalized is True
        assert report.redaction.counts[RedactionReason.HOME_DIRECTORY] >= 1
        assert str(logs_under_a_fake_home) not in report.model_dump_json()

    @pytest.mark.asyncio
    async def test_the_real_paths_are_kept_when_normalizing_is_turned_off(
        self, engine: Engine, logs_under_a_fake_home: Path
    ) -> None:
        """The contrast to the test above, and the case where a reader needs a path they can open."""
        report = await _report(engine, normalize_identity=False)

        assert report.paths.log_directory == str(logs_under_a_fake_home)
        assert report.redaction.identity_normalized is False

    @pytest.mark.asyncio
    async def test_a_secret_is_reported_by_name_and_by_where_it_came_from(
        self, engine: Engine, declared_canary: str
    ) -> None:
        """Which keys are set, and which source won, is the whole diagnostic signal here."""
        report = await _report(engine)

        entry = next(secret for secret in report.secrets if secret.name == _SECRET_NAME)
        assert entry.is_set is True
        assert entry.declared_in_config is True
        assert entry.effective_source == "environment variable"
        assert declared_canary not in report.model_dump_json()

    @pytest.mark.asyncio
    async def test_a_default_value_written_into_the_settings_is_hidden(self, engine: Engine) -> None:
        """`secrets_to_register` is the one setting whose values are credentials by design.

        Given a mapping, the engine registers each value as that secret's starting value,
        so a report that published the setting verbatim would publish live keys. The names
        stay, because which secrets an installation expects is a thing support needs.
        """
        engine.config_manager.set_config_value(SECRETS_TO_REGISTER_KEY, {_SECRET_NAME: _CANARY_VALUE})

        report = await _report(engine)

        serialized = report.model_dump_json()
        assert _SECRET_NAME in serialized
        assert _CANARY_VALUE not in serialized
        assert REDACTED in serialized

    @pytest.mark.asyncio
    async def test_a_host_that_cannot_be_identified_is_a_failure_result_not_an_exception(
        self, engine: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A request handler answers with a result. Raising reaches the user as a traceback."""
        with (
            patch.object(DiagnosticsManager, "_build_host_section", side_effect=OSError(_HOST_UNIDENTIFIABLE)),
            caplog.at_level(logging.ERROR, logger=LOGGER_NAME),
        ):
            result = await engine.ahandle_request(GetDiagnosticsReportRequest())

        assert isinstance(result, GetDiagnosticsReportResultFailure)
        assert "Attempted to collect a diagnostics report" in str(result.result_details)
        assert "Attempted to collect a diagnostics report" in caplog.text


class TestBundle:
    """`CollectDiagnosticsRequest`: the one file a user attaches to a bug report."""

    @pytest.mark.asyncio
    async def test_writes_a_zip_holding_the_files_support_opens(
        self, engine: Engine, tmp_path: Path, something_in_the_log: str
    ) -> None:
        result = await _collect(engine, tmp_path)

        members = _members(result)
        assert README_FILE_NAME in members
        assert MANIFEST_FILE_NAME in members
        assert REPORT_FILE_NAME in members
        assert something_in_the_log in members[f"{LOGS_DIRECTORY_NAME}/{SESSION_LOG_FILE_NAME}"]
        # The files on disk as well as the session buffer: they are separate sources, and
        # they are what covers the sessions before the one being collected from.
        assert [name for name in members if name.startswith(f"{LOGS_DIRECTORY_NAME}/engine-")] != []

    @pytest.mark.asyncio
    async def test_the_reported_size_is_the_size_of_the_file_on_disk(self, engine: Engine, tmp_path: Path) -> None:
        """Read out loud when someone is deciding whether they can email it."""
        result = await _collect(engine, tmp_path)

        assert result.path is not None
        assert result.size_bytes == _size_on_disk(result.path)

    @pytest.mark.asyncio
    async def test_the_manifest_describes_the_archive_it_is_inside(self, engine: Engine, tmp_path: Path) -> None:
        """The manifest is the file support reads first, so a name in it has to open."""
        result = await _collect(engine, tmp_path)

        members = _members(result)
        listed = [entry.path for entry in result.manifest.entries]
        assert REPORT_FILE_NAME in listed
        assert [path for path in listed if path not in members] == []

    @pytest.mark.asyncio
    async def test_turning_logs_off_leaves_every_log_out(self, engine: Engine, tmp_path: Path) -> None:
        """`--skip-logs` exists for a workflow nobody may read, so half-off is not an option."""
        result = await _collect(engine, tmp_path, include_logs=False)

        members = _members(result)
        assert [name for name in members if name.startswith(f"{LOGS_DIRECTORY_NAME}/")] == []
        assert REPORT_FILE_NAME in members

    @pytest.mark.asyncio
    async def test_a_bundle_already_there_is_kept_rather_than_overwritten(self, engine: Engine, tmp_path: Path) -> None:
        """A bundle is evidence, and the one already there may be what someone is waiting for."""
        existing = tmp_path / "bundle.zip"
        existing.write_bytes(b"the bundle from the first attempt")

        result = await _collect(engine, tmp_path, file_name="bundle.zip")

        assert existing.read_bytes() == b"the bundle from the first attempt"
        # Reported under the name it was really written as, not the name that was asked
        # for, or support is pointed at the older bundle.
        assert result.file_name != "bundle.zip"
        assert result.path is not None
        assert Path(result.path).name == result.file_name

    @pytest.mark.asyncio
    async def test_a_destination_that_cannot_be_written_to_is_a_failure_result(
        self, engine: Engine, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The failure a user actually hits: a full disk, a read-only volume, a typo.

        Spelled as a path underneath a file, which no platform will create a directory
        for, so this is the same failure on all of them.
        """
        blocking_file = tmp_path / "not-a-directory"
        blocking_file.write_bytes(b"")

        with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
            result = await engine.ahandle_request(
                CollectDiagnosticsRequest(
                    output_path=str(blocking_file / "bundle.zip"),
                    include_health_checks=False,
                    include_current_workflow=False,
                )
            )

        assert isinstance(result, CollectDiagnosticsResultFailure)
        assert "Attempted to write the diagnostics bundle" in str(result.result_details)
        assert "Attempted to write the diagnostics bundle" in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("file_name", ["../escaped.zip", "sub/bundle.zip", "/absolute.zip"])
    async def test_a_file_name_that_is_really_a_path_is_refused(
        self, engine: Engine, tmp_path: Path, file_name: str
    ) -> None:
        """The name is joined onto the output directory, so a path in it lands outside.

        `output_path` is the caller's statement of where the bundle may go, and a request
        arriving over the event bus supplies both halves. Refused rather than sanitized so
        nobody is told a bundle went one place and finds it in another.
        """
        result = await engine.ahandle_request(
            CollectDiagnosticsRequest(
                output_path=str(tmp_path),
                file_name=file_name,
                include_health_checks=False,
                include_current_workflow=False,
            )
        )

        assert isinstance(result, CollectDiagnosticsResultFailure)
        assert "is a path rather than a file name" in str(result.result_details)
        assert _zips_written_under(tmp_path) == []

    @pytest.mark.asyncio
    async def test_the_health_checks_are_written_in_when_asked_for(self, engine: Engine, tmp_path: Path) -> None:
        """`doctor.json` is how a support engineer sees the verdicts the user saw."""
        with patch.object(CloudConnectionCheck, "_connect_and_disconnect", new_callable=AsyncMock):
            result = await _collect(engine, tmp_path, include_health_checks=True)

        members = _members(result)
        assert HEALTH_FILE_NAME in members
        assert "Workspace" in members[HEALTH_FILE_NAME]

    @pytest.mark.asyncio
    async def test_a_secret_the_engine_logged_is_not_in_the_bundle(
        self, engine: Engine, tmp_path: Path, declared_canary: str
    ) -> None:
        """The promise a bundle cannot break, over the whole path from log line to zip.

        A library logging a credential into an error message is the ordinary way one ends
        up in a log file, and the engine is the only thing that knows enough to take it
        back out again. Asserted over every file in the archive rather than the one it was
        planted in, because staging copies text into more than one of them.
        """
        logging.getLogger(LOGGER_NAME).warning("Request rejected while authenticating with %s", declared_canary)

        result = await _collect(engine, tmp_path)

        members = _members(result)
        session_log = members[f"{LOGS_DIRECTORY_NAME}/{SESSION_LOG_FILE_NAME}"]
        assert "authenticating with" in session_log, "the line was never captured, so nothing was redacted"
        assert REDACTED in session_log
        assert [name for name, text in members.items() if declared_canary in text] == []
        assert result.manifest.redaction.counts[RedactionReason.KNOWN_SECRET_VALUE] >= 1

    @pytest.mark.asyncio
    async def test_a_report_that_cannot_be_built_is_a_failure_result_not_an_exception(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        """Staging has already written files by this point, so this path has cleanup to do."""
        with patch.object(DiagnosticsManager, "_build_host_section", side_effect=OSError(_HOST_UNIDENTIFIABLE)):
            result = await engine.ahandle_request(
                CollectDiagnosticsRequest(
                    output_path=str(tmp_path),
                    include_health_checks=False,
                    include_current_workflow=False,
                )
            )

        assert isinstance(result, CollectDiagnosticsResultFailure)
        assert "Attempted to collect a diagnostics bundle" in str(result.result_details)
        assert _zips_written_under(tmp_path) == []


class TestTheOpenWorkflow:
    """What `include_current_workflow` puts in a bundle, and what it says when it cannot.

    Only what is on disk can be included, so the three ways that goes wrong -- nothing
    open, never saved, saved and since deleted -- each have to say which one happened.
    Whoever opens the bundle is looking for the workflow that broke, and "not here" without
    a reason sends them back to ask.
    """

    @pytest.fixture
    def open_workflow(self, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Open a saved workflow in the engine, and return the file it was saved to."""
        workflow = tmp_path / "the-flow-that-broke.py"
        workflow.write_text("# a saved workflow\n", encoding="utf-8")
        self._open(monkeypatch, engine, name="the-flow-that-broke", file_path=str(workflow))
        return workflow

    @pytest.mark.asyncio
    async def test_the_saved_file_is_copied_into_the_bundle(
        self, engine: Engine, tmp_path: Path, open_workflow: Path
    ) -> None:
        result = await _collect(engine, tmp_path, include_current_workflow=True)

        assert f"workflow/{open_workflow.name}" in _members(result)

    @pytest.mark.asyncio
    async def test_nothing_open_is_reported_rather_than_left_blank(self, engine: Engine, tmp_path: Path) -> None:
        """Asked for and absent is a different thing from never asked for."""
        result = await _collect(engine, tmp_path, include_current_workflow=True)

        assert [warning for warning in result.manifest.warnings if "No workflow was open" in warning] != []

    @pytest.mark.asyncio
    async def test_an_unsaved_workflow_is_reported_with_what_to_do_about_it(
        self, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The likeliest case of all: the workflow being reported on has never been saved."""
        self._open(monkeypatch, engine, name="untitled", file_path=None)

        result = await _collect(engine, tmp_path, include_current_workflow=True)

        assert [warning for warning in result.manifest.warnings if "never been saved" in warning] != []

    @pytest.mark.asyncio
    async def test_a_workflow_whose_file_is_gone_is_reported_by_name(
        self, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deleted from under the engine, which is itself worth knowing about."""
        self._open(monkeypatch, engine, name="deleted-since", file_path=str(tmp_path / "deleted-since.py"))

        result = await _collect(engine, tmp_path, include_current_workflow=True)

        assert [warning for warning in result.manifest.warnings if "deleted-since.py" in warning] != []

    @staticmethod
    def _open(monkeypatch: pytest.MonkeyPatch, engine: Engine, *, name: str, file_path: str | None) -> None:
        """Answer the context manager as though this workflow were open.

        Patched rather than really opened: opening one runs its generated module, which
        registers nodes and libraries this has nothing to do with. The three methods
        replaced here are everything the diagnostics code asks about the open workflow.
        """
        context_manager = engine.context_manager
        monkeypatch.setattr(context_manager, "has_current_workflow", lambda: True)
        monkeypatch.setattr(context_manager, "get_current_workflow_name", lambda: name)
        monkeypatch.setattr(context_manager, "get_current_workflow_file_path", lambda: file_path)


class TestBundleWrittenToStaticFiles:
    """`output_path=None`: the route the editor uses, where the engine may be another machine.

    The bundle goes to the static files manager and comes back as a link. The name it was
    saved under is read back off that link, because bundles are saved without overwriting
    and the name asked for is not always the name written.
    """

    @pytest.mark.asyncio
    async def test_returns_a_link_rather_than_a_path(self, engine: Engine) -> None:
        saved_as = "https://static.example.com/files/griptape-nodes-diagnostics-1.2.3-20260101-000000_1.zip"

        with patch.object(engine.static_files_manager, "save_static_file", return_value=saved_as) as save:
            result = await engine.ahandle_request(
                CollectDiagnosticsRequest(include_health_checks=False, include_current_workflow=False)
            )

        assert isinstance(result, CollectDiagnosticsResultSuccess), result.result_details
        assert result.url == saved_as
        assert result.path is None
        # The name on the link, not the name that was asked for, or support opens the
        # bundle from the previous attempt.
        assert result.file_name.endswith("_1.zip")
        # The size reported is the size of what was handed over, not of what was staged.
        assert len(save.call_args.args[0]) == result.size_bytes

    @pytest.mark.asyncio
    async def test_a_save_that_fails_is_a_failure_result_not_an_exception(
        self, engine: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No disk space, or a static files directory that is not there any more."""
        with (
            patch.object(
                engine.static_files_manager, "save_static_file", side_effect=OSError("No space left on device")
            ),
            caplog.at_level(logging.ERROR, logger=LOGGER_NAME),
        ):
            result = await engine.ahandle_request(
                CollectDiagnosticsRequest(include_health_checks=False, include_current_workflow=False)
            )

        assert isinstance(result, CollectDiagnosticsResultFailure)
        assert "No space left on device" in str(result.result_details)
        assert "Attempted to save the diagnostics bundle" in caplog.text

    @pytest.mark.asyncio
    async def test_a_save_that_cannot_reach_the_cloud_is_a_failure_result_not_an_exception(
        self, engine: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        """This route is the one that uploads, so the network is one of the ways it fails.

        With the cloud storage backend configured, saving a static file uploads it and then
        asks for a download URL over HTTP. `httpx` errors are not `OSError`s, so a refused
        connection came out of the request handler as a raw exception -- and this handler is
        reached from the editor, which has nothing to show for one.
        """
        with (
            patch.object(
                engine.static_files_manager,
                "save_static_file",
                side_effect=httpx.ConnectError("All connection attempts failed"),
            ),
            caplog.at_level(logging.ERROR, logger=LOGGER_NAME),
        ):
            result = await engine.ahandle_request(
                CollectDiagnosticsRequest(include_health_checks=False, include_current_workflow=False)
            )

        assert isinstance(result, CollectDiagnosticsResultFailure)
        assert "All connection attempts failed" in str(result.result_details)
        assert "Attempted to save the diagnostics bundle" in caplog.text


class TestHealthChecks:
    """`RunHealthChecksRequest`: what `gtn doctor` prints, and what to do about it."""

    @pytest.fixture
    def cloud_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Give the connection check a key, so its verdict is not decided by the machine.

        Without one the check fails before it opens anything, which is a different code
        path from the one these tests are about -- and whether a developer's own key is in
        the environment would otherwise decide which of the two runs.
        """
        monkeypatch.setenv(CLOUD_API_KEY_NAME, _CLOUD_KEY)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("cloud_key")
    async def test_every_check_reports_a_verdict(self, engine: Engine) -> None:
        with patch.object(CloudConnectionCheck, "_connect_and_disconnect", new_callable=AsyncMock):
            result = await engine.ahandle_request(RunHealthChecksRequest())

        assert isinstance(result, RunHealthChecksResultSuccess), result.result_details
        assert [check.name for check in result.health.results] == [
            "Workspace",
            "Disk Space",
            "Libraries",
            "Secrets",
            "Log Capture",
            "Cloud Connection",
        ]

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("cloud_key")
    async def test_a_failing_check_is_still_a_successful_request(self, engine: Engine) -> None:
        """A broken installation is what these are for, so a FAIL is an answer, not an error."""
        with patch.object(
            CloudConnectionCheck,
            "_connect_and_disconnect",
            new_callable=AsyncMock,
            side_effect=OSError("no route to host"),
        ):
            result = await engine.ahandle_request(RunHealthChecksRequest())

        assert isinstance(result, RunHealthChecksResultSuccess), result.result_details
        assert result.health.status is HealthStatus.FAIL
        cloud = next(check for check in result.health.results if check.name == CloudConnectionCheck.name)
        assert cloud.status is HealthStatus.FAIL
        assert cloud.remedy is not None

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("cloud_key")
    async def test_the_key_the_connection_check_used_is_not_in_the_verdicts(self, engine: Engine) -> None:
        """The one value in this request that a report is never allowed to hold.

        It is handed to the checks so a socket can be opened with it, which puts it one
        interpolated exception message away from `doctor.json`.
        """
        with patch.object(CloudConnectionCheck, "_connect_and_disconnect", new_callable=AsyncMock):
            result = await engine.ahandle_request(RunHealthChecksRequest())

        assert isinstance(result, RunHealthChecksResultSuccess), result.result_details
        assert _CLOUD_KEY not in result.health.model_dump_json()

    @pytest.mark.asyncio
    async def test_a_report_that_cannot_be_built_is_a_failure_result_not_an_exception(self, engine: Engine) -> None:
        """Every check reads the report, so there is nothing to judge and nothing to say."""
        with patch.object(DiagnosticsManager, "_build_host_section", side_effect=OSError(_HOST_UNIDENTIFIABLE)):
            result = await engine.ahandle_request(RunHealthChecksRequest())

        assert isinstance(result, RunHealthChecksResultFailure)
        assert "Attempted to run health checks" in str(result.result_details)
