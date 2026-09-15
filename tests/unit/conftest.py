"""Shared fixtures for unit tests."""

import json
import os
import tempfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from xdg_base_dirs import xdg_data_home

from griptape_nodes.common import log_capture
from griptape_nodes.retained_mode.engine import Engine, current_engine, reset_root_engine

# The redirect has to be in place before the first test module is imported, which is
# earlier than a fixture of any scope can run. `agent_manager` and `servers.mcp` build a
# `ConfigManager` at module level, and building one applies the logging settings, so merely
# collecting the test module that imports either of them wrote a log file to the real XDG
# data directory and pruned the files a week old out of it.
_session_log_home = tempfile.TemporaryDirectory(prefix="griptape-nodes-test-log-home-")
_session_log_home_patch = patch.object(log_capture, "xdg_data_home", lambda: Path(_session_log_home.name))


def _real_log_directory() -> Path | None:
    """The developer's real engine log directory, or None on a machine that has no home.

    Deliberately the real one, computed from the unpatched ``xdg_data_home``, so the guard
    below can check the suite against it. Read, never written.

    Worked out on demand rather than at import time because ``xdg_data_home`` raises when
    there is no home directory to find -- a Windows service account has none -- and at
    import that failure takes down collection of every test in the suite rather than
    skipping the one guard that needs it.
    """
    try:
        return xdg_data_home() / "griptape_nodes" / "logs"
    except RuntimeError:
        return None


# Only this process's own log files are the suite's to answer for. A developer running the
# engine while the suite runs has that engine writing into the same directory, and counting
# its files as a leak would fail the guard below for a reason nobody can act on. Log file
# names carry the pid, so ownership is read off the name rather than guessed. Under
# `-n auto` each xdist worker is its own process and snapshots its own pid.
_own_log_file_glob = f"{log_capture.LOG_FILE_PREFIX}*-{os.getpid()}.log*"
_own_names_in_real_log_directory: set[str] = set()


def pytest_configure() -> None:
    """Point engine logging at a temporary directory before any test module is imported."""
    real_log_directory = _real_log_directory()
    if real_log_directory is not None:
        _own_names_in_real_log_directory.update(path.name for path in real_log_directory.glob(_own_log_file_glob))
    _session_log_home_patch.start()


def pytest_unconfigure() -> None:
    """Detach the log sinks before the directory holding their files goes away."""
    log_capture.configure_diagnostic_logging(buffer_lines=0, log_to_file=False)
    _session_log_home_patch.stop()
    _session_log_home.cleanup()


@pytest.fixture
def own_log_files_in_real_log_directory() -> list[str]:
    """Log files this process has written into the developer's real engine log directory.

    Empty unless the isolation above stopped working. Snapshotted in ``pytest_configure``
    rather than in this fixture because the leak it catches happened during collection,
    when a test module imported one of the modules that builds a ``ConfigManager`` at
    import time -- earlier than a fixture of any scope can run.

    Pruning needs no guard of its own. Files are only aged out of a directory the file sink
    was pointed at, and pointing it at one creates a file of this process's own there
    first, so anything the suite deleted it is also listed here for having created.
    """
    real_log_directory = _real_log_directory()
    if real_log_directory is None:
        return []

    current = {path.name for path in real_log_directory.glob(_own_log_file_glob)}
    return sorted(current - _own_names_in_real_log_directory)


@pytest.fixture(autouse=True)
def isolate_user_config() -> Generator[Path, None, None]:
    """Isolate the user config file during tests to prevent pollution of the real config."""
    import griptape_nodes.retained_mode.managers.config_manager as config_manager_module

    # Drop the root engine so managers re-initialize against the patched config below.
    reset_root_engine()

    # Create a temporary directory for the test config
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_config_path = Path(temp_dir) / "griptape_nodes_config.json"

        # Initialize with an empty config
        temp_config_path.write_text(json.dumps({}, indent=2))

        # Patch the USER_CONFIG_PATH constant to point to our temp file
        with patch.object(config_manager_module, "USER_CONFIG_PATH", temp_config_path):
            yield temp_config_path

            # Drop it again so the next test doesn't inherit this one's object graph.
            reset_root_engine()


@pytest.fixture(autouse=True)
def isolate_engine_logs() -> Generator[Path, None, None]:
    """Give each test its own engine log directory.

    ``logging.log_to_file`` is on by default, so every engine a test builds attaches a
    rotating file sink to the shared logger and ages out anything older than
    ``log_retention_days``. Per test, so one test's log files are never what another test
    finds; ``pytest_configure`` above is what keeps the real directory out of reach.

    ``xdg_data_home`` is the seam rather than ``default_log_directory``, because tests
    import that function directly and would otherwise compare a temporary directory
    against the real one.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        data_home = Path(temp_dir)
        with patch.object(log_capture, "xdg_data_home", lambda: data_home):
            yield data_home / "griptape_nodes" / "logs"

            # Detach the sinks while the directory still exists. They live on the
            # process-global logger, so a handler left behind holds an open file in a
            # directory that is about to be deleted and keeps writing into it for the rest
            # of the session.
            log_capture.configure_diagnostic_logging(buffer_lines=0, log_to_file=False)


@pytest.fixture
def engine() -> Engine:
    """Provide the engine for this test, building it on first use."""
    return current_engine()
