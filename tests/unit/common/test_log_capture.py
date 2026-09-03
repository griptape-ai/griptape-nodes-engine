"""Tests for the process-wide diagnostic log capture.

Both sinks hang off the shared ``griptape_nodes`` logger and are held in module state, so
the risky behavior here is not formatting but reconfiguration: an engine applies its
settings several times while it boots, and every one of those calls must leave the same
single file open and the already-captured lines intact.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from xdg_base_dirs import xdg_data_home

from griptape_nodes.common import log_capture
from griptape_nodes.common.log_capture import (
    LOG_FILE_PREFIX,
    DiagnosticFormatter,
    SessionLogBuffer,
    active_log_file,
    configure_diagnostic_logging,
    default_log_directory,
    find_log_files,
    prune_log_files,
    resolve_log_directory,
    session_log_lines,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_SECONDS_PER_DAY = 86400

# Directory permissions are not enforced against root, and `chmod` does not restrict writes
# to a directory on Windows, so a test relying on either would pass for the wrong reason.
_needs_posix_permissions = pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="directory write permissions are not enforced for root or on Windows",
)


@pytest.fixture
def isolated_capture() -> Iterator[None]:
    """Give a test its own capture state and put the process's back afterwards.

    The sinks and the logger they attach to are process-global, so a test that left a
    handler behind would write later tests' records into its own file.
    """
    previous_state = log_capture._state
    previous_handlers = list(log_capture.logger.handlers)
    previous_level = log_capture.logger.level

    log_capture._state = log_capture._CaptureState()
    log_capture.logger.handlers = []
    # The logger's own level gates what reaches either sink, so a test that expects to
    # capture a line has to let it through first.
    log_capture.logger.setLevel(logging.DEBUG)

    try:
        yield
    finally:
        log_capture._remove_file_handler()
        log_capture.logger.handlers = previous_handlers
        log_capture.logger.setLevel(previous_level)
        log_capture._state = previous_state


def _record(
    message: str = "hello", *, level: int = logging.INFO, args: tuple[object, ...] | None = None
) -> logging.LogRecord:
    return logging.LogRecord(
        name="griptape_nodes",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=args,
        exc_info=None,
    )


def _write_log(directory: Path, name: str, *, age_days: float = 0.0) -> Path:
    """Write a log file with a modification time ``age_days`` in the past."""
    path = directory / name
    path.write_text("a line\n", encoding="utf-8")
    when = time.time() - (age_days * _SECONDS_PER_DAY)
    os.utime(path, (when, when))
    return path


class TestDiagnosticFormatter:
    def test_renders_one_greppable_line_with_a_utc_timestamp(self) -> None:
        """A bundle is read with a text editor and grep, not with a Rich console."""
        line = DiagnosticFormatter().format(_record("hello %s", args=("world",)))

        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z INFO\s+- - hello world$", line), line

    def test_uses_a_dash_for_an_absent_engine_prefix_and_node_name(self) -> None:
        """A fixed column count keeps the file greppable when a line has no attribution."""
        line = DiagnosticFormatter().format(_record())

        assert " - - hello" in line

    def test_attributes_a_line_to_its_worker_and_node_when_they_are_known(self) -> None:
        record = _record()
        record.engine_prefix = "[worker-1]"
        record.node_name = "Load Image"

        line = DiagnosticFormatter().format(record)

        assert "[worker-1] Load Image hello" in line

    def test_keeps_the_traceback_of_a_logged_exception(self) -> None:
        """The traceback is the most useful thing in a bundle, so it is kept verbatim."""
        try:
            msg = "something broke"
            raise ValueError(msg)  # noqa: TRY301
        except ValueError:
            record = _record("failed", level=logging.ERROR)
            record.exc_info = sys.exc_info()

        line = DiagnosticFormatter().format(record)

        assert "ValueError: something broke" in line
        assert "Traceback" in line


class TestSessionLogBuffer:
    def test_returns_the_retained_lines_oldest_first(self) -> None:
        buffer = SessionLogBuffer(10)

        buffer.emit(_record("first"))
        buffer.emit(_record("second"))

        lines = buffer.lines()
        assert len(lines) == 2  # noqa: PLR2004
        assert "first" in lines[0]
        assert "second" in lines[1]

    def test_drops_the_oldest_line_once_it_is_full(self) -> None:
        buffer = SessionLogBuffer(2)

        for message in ("first", "second", "third"):
            buffer.emit(_record(message))

        lines = buffer.lines()
        assert len(lines) == 2  # noqa: PLR2004
        assert "first" not in "".join(lines)

    def test_reports_its_capacity(self) -> None:
        assert SessionLogBuffer(42).capacity == 42  # noqa: PLR2004

    def test_resizing_smaller_keeps_the_most_recent_lines(self) -> None:
        """The recent lines are the ones describing whatever just went wrong."""
        buffer = SessionLogBuffer(10)
        for index in range(5):
            buffer.emit(_record(f"line-{index}"))

        buffer.resize(2)

        lines = buffer.lines()
        assert len(lines) == 2  # noqa: PLR2004
        assert "line-3" in lines[0]
        assert "line-4" in lines[1]

    def test_resizing_larger_keeps_everything_already_captured(self) -> None:
        buffer = SessionLogBuffer(2)
        buffer.emit(_record("kept"))

        buffer.resize(10)

        assert buffer.capacity == 10  # noqa: PLR2004
        assert "kept" in buffer.lines()[0]

    def test_resizing_to_the_same_capacity_changes_nothing(self) -> None:
        buffer = SessionLogBuffer(5)
        buffer.emit(_record("kept"))

        buffer.resize(5)

        assert buffer.capacity == 5  # noqa: PLR2004
        assert len(buffer.lines()) == 1

    def test_a_bad_log_call_does_not_raise_out_of_whatever_logged_it(self) -> None:
        """A mismatched format string is a bug in a log line, not a reason to fail an operation."""
        buffer = SessionLogBuffer(10)
        handled: list[logging.LogRecord] = []
        buffer.handleError = handled.append  # type: ignore[method-assign]

        buffer.emit(_record("value %d", args=("not a number",)))

        assert len(handled) == 1
        assert buffer.lines() == []

    def test_a_missing_mapping_key_does_not_raise_out_of_whatever_logged_it(self) -> None:
        """Dict-style formatting raises KeyError, which is neither TypeError nor ValueError."""
        buffer = SessionLogBuffer(10)
        handled: list[logging.LogRecord] = []
        buffer.handleError = handled.append  # type: ignore[method-assign]

        buffer.emit(_record("%(missing)s happened", args=({"present": 1},)))

        assert len(handled) == 1
        assert buffer.lines() == []

    def test_a_logged_object_whose_str_raises_does_not_raise_out_of_the_log_call(self) -> None:
        """Anything at all can go wrong rendering a logged value, so anything at all is caught."""

        class Unprintable:
            def __str__(self) -> str:
                msg = "this object refuses to be rendered"
                raise RuntimeError(msg)

        buffer = SessionLogBuffer(10)
        handled: list[logging.LogRecord] = []
        buffer.handleError = handled.append  # type: ignore[method-assign]

        buffer.emit(_record("state is %s", args=(Unprintable(),)))

        assert len(handled) == 1
        assert buffer.lines() == []


class TestDefaultLogDirectory:
    """Where logs go when nobody chose, which is nearly every engine.

    Worth its own class for one reason: this is called from ``ConfigManager.__init__``, so a
    machine it cannot answer for is a machine the engine will not start on at all.
    """

    @staticmethod
    def _no_home() -> Path:
        """Stand in for ``xdg_data_home`` on a machine with no home directory to find."""
        msg = "Could not determine home directory."
        raise RuntimeError(msg)

    def test_a_machine_with_no_home_directory_still_gets_somewhere_to_log(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The default location sits under the user's data directory, so it needs a home.

        A Windows service account, or a container run without `USERPROFILE`, has none, and
        the standard library raises rather than returning a guess -- which came out of
        `ConfigManager.__init__` and stopped the engine starting.
        """
        monkeypatch.setattr(log_capture, "xdg_data_home", self._no_home)

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            directory = default_log_directory()

        assert directory == Path(tempfile.gettempdir()) / "griptape_nodes" / "logs"
        # Said out loud, because the operating system empties this directory when it likes.
        assert "no home directory" in caplog.text

    def test_the_setting_is_still_read_when_there_is_no_home_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A configured absolute path needs no home directory, and is used unchanged."""
        monkeypatch.setattr(log_capture, "xdg_data_home", self._no_home)

        assert resolve_log_directory(str(tmp_path)) == tmp_path


class TestResolveLogDirectory:
    def test_an_empty_setting_means_the_default_location(self) -> None:
        assert resolve_log_directory("") == default_log_directory()

    def test_an_absolute_path_is_used_as_given(self, tmp_path: Path) -> None:
        """``tmp_path`` rather than a literal, which has to be absolute on both platforms.

        A POSIX path like `/var/log/griptape` has no drive letter, so Windows reads it as
        relative and the fallback below is what a test asserting on it really exercises.
        """
        assert resolve_log_directory(str(tmp_path)) == tmp_path

    def test_a_tilde_is_expanded(self) -> None:
        assert resolve_log_directory("~/engine-logs") == Path.home() / "engine-logs"

    def test_a_relative_path_is_refused_with_an_explanation(self, caplog: pytest.LogCaptureFixture) -> None:
        """Relative resolves against the working directory, which would scatter logs per launch."""
        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            resolved = resolve_log_directory("logs")

        assert resolved == default_log_directory()
        assert "absolute path" in caplog.text

    def test_a_tilde_that_cannot_be_expanded_falls_back_instead_of_raising(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """This runs from `ConfigManager.__init__`, so raising here stops the engine starting.

        `expanduser` raises rather than leaving the `~` in place, on a machine with no home
        directory to expand to -- a service account, some containers -- and for `~someone`
        when that account is not on this machine. Both are reported by the standard library
        returning the path unchanged, which is what is arranged here, so the real `expanduser`
        raises for the real reason on every platform.
        """
        monkeypatch.setattr(os.path, "expanduser", lambda path: path)

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            resolved = resolve_log_directory("~/engine-logs")

        assert resolved == default_log_directory()
        assert "home directory" in caplog.text


class TestSuiteIsolation:
    """The developer's own log directory is not the suite's to write in, or to prune.

    ``log_to_file`` is on by default and the sinks are process-global, so this is on by
    accident rather than on purpose: nothing in a test has to ask for file logging to get
    it. Deleting a week-old file is the half that loses something, and it happens without a
    single assertion noticing.
    """

    def test_this_run_wrote_no_log_file_into_the_real_log_directory(
        self, own_log_files_in_real_log_directory: list[str]
    ) -> None:
        """Collection alone used to be enough, which is earlier than any fixture can reach.

        Covers pruning too: a log file is only aged out of a directory the sink was pointed
        at, and pointing it there writes a file of this process's own first.
        """
        assert own_log_files_in_real_log_directory == []

    def test_the_default_log_directory_is_somewhere_temporary(self) -> None:
        """A redirect is in place right now, which the guard above cannot tell on its own.

        It only reports leaks that have already happened, so on the first run after the
        isolation breaks it fails somewhere unrelated instead. This says so directly, though
        only for the moment a test runs: the per-test fixture patches the same seam, so
        passing here does not prove the session-wide patch that covers collection is on.
        """
        assert xdg_data_home() not in default_log_directory().parents


@pytest.mark.usefixtures("isolated_capture")
class TestConfigureDiagnosticLogging:
    def test_captures_this_session_without_anyone_turning_a_setting_on(self) -> None:
        """The whole point of the buffer: a first bundle is useful without a repro."""
        configure_diagnostic_logging(log_to_file=False)

        log_capture.logger.info("something happened")

        assert any("something happened" in line for line in session_log_lines())

    def test_no_lines_are_captured_before_it_is_configured(self) -> None:
        assert session_log_lines() == []

    def test_writes_one_log_file_and_reports_where(self, tmp_path: Path) -> None:
        configure_diagnostic_logging(log_directory=tmp_path, retention_days=0)

        log_capture.logger.info("written to file")

        active = active_log_file()
        assert active is not None
        assert active.parent == tmp_path
        assert active.name.startswith(LOG_FILE_PREFIX)
        assert "written to file" in active.read_text(encoding="utf-8")

    def test_repeated_configuration_keeps_writing_to_one_file(self, tmp_path: Path) -> None:
        """One session ends up in one file, however many times settings are applied.

        The regression this guards: an engine applies its settings several times while it
        boots, and a file name that moved with the clock split one session across files.
        """
        for index in range(3):
            configure_diagnostic_logging(log_directory=tmp_path, retention_days=0)
            log_capture.logger.info("line %d", index)

        files = list(tmp_path.glob(f"{LOG_FILE_PREFIX}*.log"))
        assert len(files) == 1
        contents = files[0].read_text(encoding="utf-8")
        assert "line 0" in contents
        assert "line 2" in contents

    def test_repeated_configuration_keeps_the_lines_already_captured(self) -> None:
        configure_diagnostic_logging(buffer_lines=10, log_to_file=False)
        log_capture.logger.info("captured early")

        configure_diagnostic_logging(buffer_lines=20, log_to_file=False)

        assert any("captured early" in line for line in session_log_lines())

    def test_turning_file_logging_off_detaches_the_file_sink(self, tmp_path: Path) -> None:
        configure_diagnostic_logging(log_directory=tmp_path, retention_days=0)

        configure_diagnostic_logging(log_to_file=False)

        assert active_log_file() is None

    def test_a_zero_line_buffer_disables_capture(self) -> None:
        configure_diagnostic_logging(buffer_lines=0, log_to_file=False)

        log_capture.logger.info("not captured")

        assert session_log_lines() == []

    def test_moving_the_directory_opens_a_file_in_the_new_one(self, tmp_path: Path) -> None:
        first = tmp_path / "first"
        second = tmp_path / "second"
        configure_diagnostic_logging(log_directory=first, retention_days=0)

        configure_diagnostic_logging(log_directory=second, retention_days=0)

        active = active_log_file()
        assert active is not None
        assert active.parent == second

    def test_a_directory_that_cannot_be_created_does_not_stop_the_engine(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Logging is a diagnostic aid; failing to open a file must never fail a startup.

        This is the `mkdir` guard specifically -- the parent is a regular file, so the
        directory can never be made. Asserted on the message that guard writes, since the
        one below it says something very similar about a different failure.
        """
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            configure_diagnostic_logging(log_directory=blocked / "logs", retention_days=0)

        assert active_log_file() is None
        assert "Could not create the engine log directory" in caplog.text

    @_needs_posix_permissions
    def test_a_directory_that_cannot_be_written_to_does_not_stop_the_engine(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The directory exists, so `mkdir` succeeds and only opening the file can fail.

        A log directory on a read-only volume, or one owned by another user, is a real
        setup. The file is opened while this guard is in scope rather than on the first
        record, so the failure is reported here instead of surfacing much later as a
        handler error on stderr.
        """
        read_only = tmp_path / "read-only"
        read_only.mkdir(mode=0o500)

        try:
            with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
                configure_diagnostic_logging(log_directory=read_only, retention_days=0)
        finally:
            # Restored so tmp_path can be torn down.
            read_only.chmod(0o700)

        # The point of opening eagerly: nothing goes on naming a file that will never exist.
        assert active_log_file() is None
        assert "Could not open the engine log file" in caplog.text

    @_needs_posix_permissions
    def test_the_buffer_still_works_when_the_directory_cannot_be_written_to(self, tmp_path: Path) -> None:
        read_only = tmp_path / "read-only"
        read_only.mkdir(mode=0o500)

        try:
            configure_diagnostic_logging(log_directory=read_only, retention_days=0)
            log_capture.logger.info("still captured")
        finally:
            read_only.chmod(0o700)

        assert any("still captured" in line for line in session_log_lines())

    def test_the_buffer_still_works_when_the_file_could_not_be_opened(self, tmp_path: Path) -> None:
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")

        configure_diagnostic_logging(log_directory=blocked / "logs", retention_days=0)
        log_capture.logger.info("still captured")

        assert any("still captured" in line for line in session_log_lines())


@pytest.mark.usefixtures("isolated_capture")
class TestWhetherTheSinksWereInstalled:
    """The return value is a caller's only way to tell a working file sink from a wished-for one.

    ``ConfigManager`` remembers the settings it applied so an unrelated config write does not
    re-scan the log directory. Remembering a failed attempt would short-circuit every later
    load, and the reasons this fails -- a volume not mounted yet, a permission fix on its way
    -- are the temporary kind, so the engine would never write a log file again.
    """

    def test_reports_success_when_the_file_sink_was_installed(self, tmp_path: Path) -> None:
        assert configure_diagnostic_logging(log_directory=tmp_path, retention_days=0) is True

    def test_reports_success_when_no_file_was_asked_for(self) -> None:
        """Nothing was wanted and nothing failed, so there is nothing to retry."""
        assert configure_diagnostic_logging(log_to_file=False) is True

    def test_reports_success_when_the_file_asked_for_is_already_open(self, tmp_path: Path) -> None:
        """The common case on reload: settings unchanged, so the sink is left alone."""
        configure_diagnostic_logging(log_directory=tmp_path, retention_days=0)

        assert configure_diagnostic_logging(log_directory=tmp_path, retention_days=0) is True

    def test_reports_failure_when_the_directory_could_not_be_created(self, tmp_path: Path) -> None:
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")

        assert configure_diagnostic_logging(log_directory=blocked / "logs", retention_days=0) is False

    @_needs_posix_permissions
    def test_reports_failure_when_the_file_could_not_be_opened(self, tmp_path: Path) -> None:
        """The directory exists, so only opening the file can fail -- a read-only volume."""
        read_only = tmp_path / "read-only"
        read_only.mkdir(mode=0o500)

        try:
            installed = configure_diagnostic_logging(log_directory=read_only, retention_days=0)
        finally:
            # Restored so tmp_path can be torn down.
            read_only.chmod(0o700)

        assert installed is False


class TestFindLogFiles:
    def test_returns_engine_logs_newest_first(self, tmp_path: Path) -> None:
        older = _write_log(tmp_path, f"{LOG_FILE_PREFIX}older.log", age_days=2)
        newer = _write_log(tmp_path, f"{LOG_FILE_PREFIX}newer.log", age_days=1)

        assert find_log_files(tmp_path) == [newer, older]

    def test_includes_rotated_backups(self, tmp_path: Path) -> None:
        """The problem being reported often happened before the current file rolled over."""
        _write_log(tmp_path, f"{LOG_FILE_PREFIX}a.log")
        _write_log(tmp_path, f"{LOG_FILE_PREFIX}a.log.1")

        assert len(find_log_files(tmp_path)) == 2  # noqa: PLR2004

    def test_ignores_files_that_are_not_engine_logs(self, tmp_path: Path) -> None:
        _write_log(tmp_path, f"{LOG_FILE_PREFIX}mine.log")
        _write_log(tmp_path, "something-else.log")

        assert [path.name for path in find_log_files(tmp_path)] == [f"{LOG_FILE_PREFIX}mine.log"]

    def test_a_missing_directory_is_empty_rather_than_an_error(self, tmp_path: Path) -> None:
        """A collection must not fail just because nothing has ever been logged."""
        assert find_log_files(tmp_path / "never-created") == []

    def test_a_file_rotation_removed_mid_scan_costs_one_entry_not_the_whole_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This runs while an engine is logging, and during engine construction."""
        kept = _write_log(tmp_path, f"{LOG_FILE_PREFIX}kept.log")
        vanishing = _write_log(tmp_path, f"{LOG_FILE_PREFIX}vanishing.log")

        real_stat = Path.stat
        seen: list[str] = []

        def stat_then_vanish(self: Path, **kwargs: object) -> os.stat_result:
            # Survives being listed, and is gone by the time its age is read.
            if self.name == vanishing.name:
                seen.append(self.name)
                if len(seen) > 1:
                    raise FileNotFoundError(self)
            return real_stat(self, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "stat", stat_then_vanish)

        assert find_log_files(tmp_path) == [kept]


class TestPruneLogFiles:
    def test_deletes_files_past_the_retention_window(self, tmp_path: Path) -> None:
        aged = _write_log(tmp_path, f"{LOG_FILE_PREFIX}aged.log", age_days=10)

        assert prune_log_files(tmp_path, retention_days=7) == 1
        assert not aged.exists()

    def test_keeps_files_inside_the_window(self, tmp_path: Path) -> None:
        recent = _write_log(tmp_path, f"{LOG_FILE_PREFIX}recent.log", age_days=1)

        assert prune_log_files(tmp_path, retention_days=7) == 0
        assert recent.exists()

    def test_zero_retention_keeps_everything_forever(self, tmp_path: Path) -> None:
        ancient = _write_log(tmp_path, f"{LOG_FILE_PREFIX}ancient.log", age_days=400)

        assert prune_log_files(tmp_path, retention_days=0) == 0
        assert ancient.exists()

    def test_leaves_files_that_are_not_engine_logs_alone(self, tmp_path: Path) -> None:
        """Pruning only owns the files it wrote, whatever else shares the directory."""
        other = _write_log(tmp_path, "someone-elses.log", age_days=400)

        prune_log_files(tmp_path, retention_days=1)

        assert other.exists()

    def test_a_missing_directory_is_not_an_error(self, tmp_path: Path) -> None:
        assert prune_log_files(tmp_path / "never-created", retention_days=7) == 0

    def test_keeps_the_file_the_sink_is_about_to_open(self, tmp_path: Path) -> None:
        """The sink prunes before opening, so its own target has to survive the sweep."""
        target = _write_log(tmp_path, f"{LOG_FILE_PREFIX}target.log", age_days=400)
        other = _write_log(tmp_path, f"{LOG_FILE_PREFIX}other.log", age_days=400)

        assert prune_log_files(tmp_path, retention_days=7, protected_name=target.name) == 1
        assert target.exists()
        assert not other.exists()

    @pytest.mark.usefixtures("isolated_capture")
    def test_never_deletes_the_log_this_process_is_writing(self, tmp_path: Path) -> None:
        """An engine running longer than the retention window still owns its own log file."""
        configure_diagnostic_logging(log_directory=tmp_path, retention_days=0)
        log_capture.logger.info("this session is still going")
        active = active_log_file()
        assert active is not None
        aged = time.time() - (30 * _SECONDS_PER_DAY)
        os.utime(active, (aged, aged))

        assert prune_log_files(tmp_path, retention_days=7) == 0
        assert active.exists()

    @pytest.mark.usefixtures("isolated_capture")
    def test_reconfiguring_does_not_prune_away_the_log_already_open(self, tmp_path: Path) -> None:
        """The regression: a long-running engine's own re-prune deleting the file it is writing."""
        configure_diagnostic_logging(log_directory=tmp_path, retention_days=7)
        active = active_log_file()
        assert active is not None
        log_capture.logger.info("earlier in this session")
        aged = time.time() - (30 * _SECONDS_PER_DAY)
        os.utime(active, (aged, aged))

        configure_diagnostic_logging(log_directory=tmp_path, retention_days=7)

        assert active_log_file() == active
        assert active.exists()
        assert "earlier in this session" in active.read_text(encoding="utf-8")
