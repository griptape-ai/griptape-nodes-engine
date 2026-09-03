"""Tests for which log files reach a bundle, and what is said when none do.

Two failures hide here, and both look like an empty log directory.

The first is that the configured log directory is not always the one being written to. When
it cannot be created or opened, `log_capture` keeps the file sink it already has rather than
dropping file logging altogether — so the engine goes on writing somewhere the config no
longer names. Searching only the configured directory left the current session's log out of
the very bundle collected to explain it.

The second is the warning. "No log files were found" is true in three unrelated situations
— file logging switched off, a directory that cannot be written to, an engine that has not
logged yet — and naming the wrong one sends someone to change a setting that is already on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import Mock, patch

from griptape_nodes.common.diagnostics.redaction import Redactor
from griptape_nodes.common.log_capture import DEFAULT_BUFFER_LINES, DEFAULT_RETENTION_DAYS
from griptape_nodes.retained_mode.managers.diagnostics_manager import DiagnosticsManager
from griptape_nodes.retained_mode.managers.settings import (
    LOG_RETENTION_DAYS_KEY,
    LOG_TO_FILE_KEY,
    SESSION_LOG_BUFFER_LINES_KEY,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine

_MODULE = "griptape_nodes.retained_mode.managers.diagnostics_manager"

_LOG_FILE_NAME = "engine-20260101-000000-1.log"


def _config_reader(values: dict[str, object]) -> Callable[..., object]:
    """Return a ``get_config_value`` stand-in that only answers the keys it was given.

    Any other key raises, so a warning that starts reading a second setting is caught here
    rather than being handed whatever this test set up for the first one.
    """

    def read(key: str, **_kwargs: object) -> object:
        if key not in values:
            msg = f"the logs section read config key '{key}', which this test does not define"
            raise AssertionError(msg)
        return values[key]

    return read


def _manager(
    log_directory: Path, *, log_to_file: bool = True, buffer_lines: int = DEFAULT_BUFFER_LINES
) -> DiagnosticsManager:
    """A manager whose engine reports one log directory and the logging settings."""
    engine = Mock()
    engine.config_manager.log_directory = log_directory
    engine.config_manager.get_config_value.side_effect = _config_reader(
        {
            LOG_TO_FILE_KEY: log_to_file,
            SESSION_LOG_BUFFER_LINES_KEY: buffer_lines,
            LOG_RETENTION_DAYS_KEY: DEFAULT_RETENTION_DAYS,
            "log_level": "INFO",
        }
    )
    return DiagnosticsManager(Mock(), engine=cast("Engine", engine))


def _redactor() -> Redactor:
    """A redactor with identity normalization off, so paths stay literal in assertions."""
    return Redactor(normalize_identity=False)


def _write_log(directory: Path, name: str = _LOG_FILE_NAME) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("a line from this session\n", encoding="utf-8")
    return path


class TestLogDirectories:
    def test_the_configured_directory_is_the_one_searched(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)

        with patch(f"{_MODULE}.active_log_file", return_value=None):
            assert manager._log_directories() == [tmp_path]

    def test_the_directory_being_written_to_is_searched_as_well(self, tmp_path: Path) -> None:
        """The configured one stays first, because that is the one the report is about."""
        configured = tmp_path / "configured"
        actual = tmp_path / "actual"
        manager = _manager(configured)

        with patch(f"{_MODULE}.active_log_file", return_value=actual / _LOG_FILE_NAME):
            assert manager._log_directories() == [configured, actual]

    def test_one_directory_spelled_two_ways_is_searched_once(self, tmp_path: Path) -> None:
        """The usual case, where both answers are the same place spelled differently.

        Searched twice, every log file in it is staged twice -- which the bundle then reports
        as two files fighting over one name.
        """
        logs = tmp_path / "logs"
        logs.mkdir()
        manager = _manager(tmp_path / "elsewhere" / ".." / "logs")

        with patch(f"{_MODULE}.active_log_file", return_value=logs / _LOG_FILE_NAME):
            assert manager._log_directories() == [logs]


class TestStageLogs:
    def test_stages_the_log_files_in_the_configured_directory(self, tmp_path: Path) -> None:
        log_file = _write_log(tmp_path)
        manager = _manager(tmp_path)
        bundle = Mock()

        with (
            patch(f"{_MODULE}.active_log_file", return_value=None),
            patch(f"{_MODULE}.session_log_lines", return_value=["a line"]),
        ):
            manager._stage_logs(bundle, [])

        assert bundle.add_log_files.call_args.args[0] == [log_file]

    def test_stages_the_log_the_engine_is_writing_even_when_the_config_names_elsewhere(self, tmp_path: Path) -> None:
        """The regression: the one log that explains this session, missing from this bundle.

        The configured directory exists and is empty, which is exactly how it looks after the
        engine failed to open a file there and carried on writing to the file it had.
        """
        configured = tmp_path / "configured"
        configured.mkdir()
        active = _write_log(tmp_path / "actual")
        manager = _manager(configured)
        bundle = Mock()

        with (
            patch(f"{_MODULE}.active_log_file", return_value=active),
            patch(f"{_MODULE}.session_log_lines", return_value=["a line"]),
        ):
            manager._stage_logs(bundle, [])

        assert bundle.add_log_files.call_args.args[0] == [active]

    def test_stages_this_sessions_log_without_anyone_having_enabled_a_setting(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        bundle = Mock()

        with (
            patch(f"{_MODULE}.active_log_file", return_value=None),
            patch(f"{_MODULE}.session_log_lines", return_value=["a line worth keeping"]),
        ):
            manager._stage_logs(bundle, [])

        assert bundle.add_session_log.call_args.args[0] == ["a line worth keeping"]


class TestWhyThereIsNoSessionLog:
    """Absent because it was switched off, or absent because nothing has happened yet."""

    def test_a_buffer_switched_off_is_named_as_the_reason(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path, buffer_lines=0)
        warnings: list[str] = []

        with (
            patch(f"{_MODULE}.active_log_file", return_value=None),
            patch(f"{_MODULE}.session_log_lines", return_value=[]),
        ):
            manager._stage_logs(Mock(), warnings)

        assert any(SESSION_LOG_BUFFER_LINES_KEY in warning for warning in warnings)
        # What to set it back to, since the reader is being told to change it.
        assert any(str(DEFAULT_BUFFER_LINES) in warning for warning in warnings)

    def test_an_engine_that_has_not_logged_yet_is_not_blamed_on_the_setting(self, tmp_path: Path) -> None:
        """The buffer is on and empty, which is any engine that has just started.

        Naming the setting here would send someone to change one that is already correct.
        """
        manager = _manager(tmp_path)
        warnings: list[str] = []

        with (
            patch(f"{_MODULE}.active_log_file", return_value=None),
            patch(f"{_MODULE}.session_log_lines", return_value=[]),
        ):
            manager._stage_logs(Mock(), warnings)

        assert any("has not logged anything yet" in warning for warning in warnings)
        assert not any(SESSION_LOG_BUFFER_LINES_KEY in warning for warning in warnings)


class TestWhyThereAreNoLogFiles:
    """Switched off, or on and not working -- opposite instructions to the person reading."""

    def test_file_logging_switched_off_is_named_as_the_reason(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path, log_to_file=False)
        warnings: list[str] = []

        with (
            patch(f"{_MODULE}.active_log_file", return_value=None),
            patch(f"{_MODULE}.session_log_lines", return_value=["a line"]),
        ):
            manager._stage_logs(Mock(), warnings)

        assert any("is off" in warning and LOG_TO_FILE_KEY in warning for warning in warnings)

    def test_file_logging_switched_on_with_nothing_written_points_at_the_directory(self, tmp_path: Path) -> None:
        """The setting is on, so the answer is the directory rather than the setting.

        This is what an unwritable log directory looks like from inside a bundle, and it is
        the reason someone is reading one.
        """
        manager = _manager(tmp_path)
        warnings: list[str] = []

        with (
            patch(f"{_MODULE}.active_log_file", return_value=None),
            patch(f"{_MODULE}.session_log_lines", return_value=["a line"]),
        ):
            manager._stage_logs(Mock(), warnings)

        assert any("cannot be written to" in warning for warning in warnings)
        assert not any("is off" in warning for warning in warnings)

    def test_nothing_is_staged_when_there_are_no_log_files(self, tmp_path: Path) -> None:
        """An empty list would have the bundle record a log section holding nothing."""
        manager = _manager(tmp_path)
        bundle = Mock()

        with (
            patch(f"{_MODULE}.active_log_file", return_value=None),
            patch(f"{_MODULE}.session_log_lines", return_value=["a line"]),
        ):
            manager._stage_logs(bundle, [])

        bundle.add_log_files.assert_not_called()


class TestLogsSection:
    """What the report says about where logs are, which is read alongside the files above."""

    def test_names_the_directory_being_written_to_only_when_it_is_somewhere_else(self, tmp_path: Path) -> None:
        """Worth saying out loud: the configured directory is the one that does not work."""
        configured = tmp_path / "configured"
        actual = tmp_path / "actual"
        _write_log(actual)
        manager = _manager(configured)

        with patch(f"{_MODULE}.active_log_file", return_value=actual / _LOG_FILE_NAME):
            section = manager._build_logs_section(_redactor(), [])

        assert section.log_directory == str(configured)
        assert section.active_log_directory == str(actual)

    def test_says_nothing_about_a_second_directory_when_there_is_only_one(self, tmp_path: Path) -> None:
        """Set on every report, the field would read as a misconfiguration on all of them."""
        _write_log(tmp_path)
        manager = _manager(tmp_path)

        with patch(f"{_MODULE}.active_log_file", return_value=tmp_path / _LOG_FILE_NAME):
            section = manager._build_logs_section(_redactor(), [])

        assert section.active_log_directory is None

    def test_lists_a_log_file_in_the_directory_being_written_to(self, tmp_path: Path) -> None:
        """The files and the warnings have to agree, or the report contradicts itself."""
        configured = tmp_path / "configured"
        configured.mkdir()
        active = _write_log(tmp_path / "actual")
        manager = _manager(configured)

        with patch(f"{_MODULE}.active_log_file", return_value=active):
            section = manager._build_logs_section(_redactor(), [])

        assert [file.name for file in section.files] == [_LOG_FILE_NAME]
        assert section.files[0].is_active is True
