"""Shared fixtures for unit tests."""

import json
import os
import tempfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from xdg_base_dirs import xdg_state_home

from griptape_nodes.common import log_capture
from griptape_nodes.retained_mode.engine import Engine, current_engine, reset_root_engine
from griptape_nodes.retained_mode.managers import settings as settings_module

# The redirect must be in place before the first test module is imported, earlier than any
# fixture can run: `agent_manager` and `servers.mcp` build a `ConfigManager` at module
# level, so merely collecting them wrote to the real XDG state directory and pruned it.
_session_log_home = tempfile.TemporaryDirectory(prefix="griptape-nodes-test-log-home-")
_session_log_home_patch = patch.object(log_capture, "xdg_state_home", lambda: Path(_session_log_home.name))


def _real_log_directory() -> Path | None:
    """The developer's real engine log directory, or None on a machine that has no home.

    The real one, from the unpatched ``xdg_state_home``, so the guard below can check the suite
    against it. Read, never written. Worked out on demand because ``xdg_state_home`` raises with
    no home to find, and at import that takes down collection of the whole suite.
    """
    try:
        return xdg_state_home() / "griptape_nodes" / "logs"
    except RuntimeError:
        return None


# Only this process's own log files are the suite's to answer for: a developer running the
# engine alongside the suite writes into the same directory. Names carry the pid, so
# ownership is read off the name. Under `-n auto` each xdist worker has its own pid.
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

    Empty unless the isolation above stopped working. Snapshotted in ``pytest_configure`` because
    the leak it catches happened during collection, earlier than a fixture of any scope can run.
    Pruning needs no guard of its own: a directory the sink was pointed at already holds a file
    of this process's own, so anything the suite deleted is listed here for having created.
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
def reset_beta_feature_warnings() -> None:
    """Forget which bad beta feature values were already warned about.

    Settings warns once per (key, value) per process, so a test asserting on that warning would
    otherwise fail whenever an earlier test in the same process hit the same value.
    """
    settings_module._reported_invalid_beta_features.clear()


@pytest.fixture(autouse=True)
def isolate_engine_logs() -> Generator[Path, None, None]:
    """Give each test its own engine log directory.

    ``logging.log_to_file`` is on by default, so every engine a test builds attaches a rotating
    file sink to the shared logger and ages out old files. Per test, so one test's log files are
    never what another finds; ``pytest_configure`` is what keeps the real directory out of reach.

    ``xdg_state_home`` is the seam rather than ``default_log_directory``, which tests import
    directly and would otherwise compare a temporary directory against the real one.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        state_home = Path(temp_dir)
        with patch.object(log_capture, "xdg_state_home", lambda: state_home):
            yield state_home / "griptape_nodes" / "logs"

            # Detach the sinks while the directory still exists: they live on the process-global
            # logger, so one left behind holds an open file in a directory about to be deleted.
            log_capture.configure_diagnostic_logging(buffer_lines=0, log_to_file=False)


@pytest.fixture
def engine() -> Engine:
    """Provide the engine for this test, building it on first use."""
    return current_engine()
