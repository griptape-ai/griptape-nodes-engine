"""Process-wide log capture used to build diagnostics bundles.

Two sinks are attached to the ``griptape_nodes`` logger:

- A ring buffer of the most recent records, on by default. It exists so a
  diagnostics bundle carries this session's logs without the person reporting
  the problem having had to enable a setting and reproduce it first.
- An optional rotating file, so logs outlive the process and can be collected
  for a time range rather than only for the current session.

Both handlers sit at DEBUG so they add no filtering of their own, but the
``griptape_nodes`` logger's own level still gates what reaches them: at the
default ``log_level`` of INFO, DEBUG records are discarded before any handler
runs. Raising ``log_level`` to DEBUG is what puts debug detail in a bundle.

Both sinks are process-global, because the ``griptape_nodes`` logger is shared
by every ``Engine`` in the process. ``configure_diagnostic_logging`` is
therefore idempotent and last-call-wins, matching how ``ConfigManager`` already
sets the shared log level.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import NamedTuple

from xdg_base_dirs import xdg_data_home

LOGGER_NAME = "griptape_nodes"

# Rotation matches the desktop application's existing 10MB roll so the two never
# disagree about what a rotated engine log looks like.
MAX_LOG_FILE_BYTES = 10 * 1024 * 1024
LOG_FILE_BACKUP_COUNT = 5

# Every engine process writes its own file. Two engines on one machine is a
# supported (if discouraged) setup, and a shared handle would interleave their
# output and corrupt rotation.
LOG_FILE_PREFIX = "engine-"
LOG_FILE_GLOB = f"{LOG_FILE_PREFIX}*.log*"

DEFAULT_BUFFER_LINES = 5000
DEFAULT_RETENTION_DAYS = 7

_SECONDS_PER_DAY = 86400

logger = logging.getLogger(LOGGER_NAME)


class DiagnosticFormatter(logging.Formatter):
    """Renders a record as one plain, greppable line in UTC.

    Deliberately not the Rich console format: a diagnostics bundle is read with
    a text editor and grep, so it carries an unambiguous ISO timestamp and no
    escape codes. ``engine_prefix`` (set by the worker-designator filter) and
    ``node_name`` are included when present so a line can be attributed to the
    worker and node that produced it.
    """

    converter = time.gmtime

    def __init__(self) -> None:
        super().__init__(fmt="%(message)s", datefmt="%Y-%m-%dT%H:%M:%S")

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return f"{super().formatTime(record, datefmt)}.{int(record.msecs):03d}Z"

    def format(self, record: logging.LogRecord) -> str:
        # The base implementation renders the message and appends exception and
        # stack text, which is the part worth keeping verbatim.
        body = super().format(record)
        engine_prefix = getattr(record, "engine_prefix", "") or "-"
        node_name = getattr(record, "node_name", None) or "-"
        timestamp = self.formatTime(record, self.datefmt)
        return f"{timestamp} {record.levelname:<8} {engine_prefix} {node_name} {body}"


class SessionLogBuffer(logging.Handler):
    """Holds the most recent formatted log lines for this process in memory.

    Sits at DEBUG so it never filters anything the logger let through. What the
    logger lets through is set by ``log_level``.
    """

    def __init__(self, capacity: int) -> None:
        super().__init__(level=logging.DEBUG)
        self._lines: deque[str] = deque(maxlen=capacity)
        self.setFormatter(DiagnosticFormatter())

    @property
    def capacity(self) -> int:
        """Maximum number of lines retained before the oldest are dropped."""
        # A deque built with a maxlen always reports one; the cast keeps type
        # checkers from treating it as possibly-None.
        return self._lines.maxlen or 0

    def lines(self) -> list[str]:
        """Return the retained lines, oldest first."""
        # Taken under the handler's own lock, the one ``logging.Handler.handle`` holds
        # while ``emit`` runs. A bundle is collected on one thread while every other
        # thread in the engine is still logging into this deque.
        self.acquire()
        try:
            return list(self._lines)
        finally:
            self.release()

    def resize(self, capacity: int) -> None:
        """Change the retained line count, keeping the most recent lines."""
        if capacity == self.capacity:
            return

        # Same lock, for a stronger reason: the deque is replaced rather than mutated,
        # so a line emitted mid-swap would land in the deque about to be discarded.
        self.acquire()
        try:
            self._lines = deque(self._lines, maxlen=capacity)
        finally:
            self.release()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._lines.append(self.format(record))
        except Exception:
            # Anything at all, because anything at all can go wrong in formatting: args
            # that do not match the format string, a mapping missing a `%(name)s` key, a
            # `__str__` on a logged object that raises. Whatever it is, it would come out
            # of the `logger.*()` call that made it and turn a bad log line into a failed
            # operation. `logging.StreamHandler.emit` catches everything for this reason.
            self.handleError(record)


class _CaptureState:
    """Holds the process-wide handlers so reconfiguration can replace them."""

    def __init__(self) -> None:
        self.buffer: SessionLogBuffer | None = None
        self.file_handler: RotatingFileHandler | None = None
        self.file_path: Path | None = None
        self.file_name: str | None = None


_state = _CaptureState()

# Serializes reconfiguration. The state above is process-wide, so two engines configuring
# themselves at once are writing to the same handlers.
_configuration_lock = threading.Lock()


def default_log_directory() -> Path:
    """Return the directory engine logs are written to when none is configured.

    Never raises. This is reached from ``ConfigManager.__init__``, so a machine this cannot
    answer for would otherwise be a machine the engine refuses to start on.
    """
    # FAILURE CASE: the default location lives under the user's data directory, and finding
    # that means finding their home directory. A Windows service account or a container run
    # without `USERPROFILE` set has none, and the standard library raises rather than
    # returning a guess. Logs go to the temporary directory instead, which is the one place
    # every platform can be asked for without knowing who is logged in.
    try:
        data_home = xdg_data_home()
    except RuntimeError:
        fallback = Path(tempfile.gettempdir()) / "griptape_nodes" / "logs"
        logger.warning(
            "This machine has no home directory to keep engine logs in, so they are being written to '%s' "
            "instead. Files there are removed by the operating system from time to time. Set "
            "'logging.log_directory' to an absolute path to choose somewhere they will be kept.",
            fallback,
        )
        return fallback

    return data_home / "griptape_nodes" / "logs"


def resolve_log_directory(configured_directory: str) -> Path:
    """Resolve the ``logging.log_directory`` config value to a concrete path.

    Args:
        configured_directory: The configured value. Empty means "use the default".
            A non-empty value must be absolute; ``~`` is expanded first.

    Returns:
        The directory that should hold this process's log file.
    """
    default_directory = default_log_directory()

    if not configured_directory:
        return default_directory

    # FAILURE CASE: `~someone-else/logs` cannot be expanded when that account does not
    # exist on this machine, and `~/logs` cannot be expanded when the process has no home
    # directory at all (a service account, some containers). `expanduser` raises rather than
    # leaving the `~` in place, and this runs from `ConfigManager.__init__`, so a typo in one
    # setting would stop the engine from starting.
    try:
        configured_path = Path(configured_directory).expanduser()
    except RuntimeError:
        logger.warning(
            "The 'logging.log_directory' setting is '%s', whose leading '~' could not be expanded to a "
            "home directory on this machine. Using the default location '%s' instead.",
            configured_directory,
            default_directory,
        )
        return default_directory

    # FAILURE CASE: a relative value resolves against the process working directory,
    # which is not stable for the engine's lifetime. Logs would land wherever the
    # engine happened to boot, and a later collection from elsewhere would miss them.
    # The setting is documented as absolute, so refuse rather than scatter logs.
    if not configured_path.is_absolute():
        logger.warning(
            "The 'logging.log_directory' setting is '%s', which is not an absolute path. Using the "
            "default location '%s' instead. Set 'logging.log_directory' to an absolute path to choose "
            "where engine logs are written.",
            configured_directory,
            default_directory,
        )
        return default_directory

    return configured_path


def configure_diagnostic_logging(
    *,
    buffer_lines: int = DEFAULT_BUFFER_LINES,
    log_to_file: bool = True,
    log_directory: Path | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> bool:
    """Install or update the diagnostic log sinks on the ``griptape_nodes`` logger.

    Safe to call repeatedly: the ring buffer is resized in place so nothing
    already captured is lost, and the file sink is only reopened when its
    destination actually changes.

    Args:
        buffer_lines: Lines to retain in memory. Zero or less disables the buffer.
        log_to_file: Whether to also write a rotating log file.
        log_directory: Directory for the log file. None means the default.
        retention_days: Delete log files older than this many days. Zero or less
            keeps them forever.

    Returns:
        Whether the sinks now installed are the ones that were asked for. False means
        file logging was requested and the directory could not be created or the file
        could not be opened, both of which are logged as warnings here. Callers that
        remember what they applied must not remember a False: the condition is usually
        temporary, and the next call is the only chance to pick the setting up again.
    """
    # Held across the whole body. Two engines in one process load their configs on their
    # own threads, and the sinks they are installing are shared: interleaved, one call can
    # detach a handler the other has already replaced, leaking an open file, or both can
    # install a handler and split one session's log across two files.
    with _configuration_lock:
        _configure_buffer(buffer_lines)

        if not log_to_file:
            _remove_file_handler()
            return True

        directory = log_directory if log_directory is not None else default_log_directory()
        return _configure_file_handler(directory, retention_days)


def session_log_lines() -> list[str]:
    """Return this process's captured log lines, oldest first.

    Empty when the buffer is disabled or nothing has been logged yet.
    """
    if _state.buffer is None:
        return []
    return _state.buffer.lines()


def active_log_file() -> Path | None:
    """Return the log file this process is writing to, or None when not logging to file."""
    return _state.file_path


class _LogFileAge(NamedTuple):
    """A log file and its modification time, measured once so sorting cannot fail.

    Attributes:
        modified_at: Modification time in seconds since the epoch.
        path: The log file.
    """

    modified_at: float
    path: Path


def find_log_files(directory: Path | None = None) -> list[Path]:
    """Return every engine log file in a directory, newest first.

    Includes rotated backups (``engine-....log.1`` and friends). A missing or
    unreadable directory yields an empty list rather than raising, so a
    diagnostics collection never fails just because no log directory exists.
    """
    search_dir = directory if directory is not None else default_log_directory()
    if not search_dir.is_dir():
        return []

    try:
        matches = [path for path in search_dir.glob(LOG_FILE_GLOB) if path.is_file()]
    except OSError:
        logger.warning("Could not list log files in '%s'.", search_dir, exc_info=True)
        return []

    # Ages are read here rather than inside the sort key, so a file that rotation renamed
    # or removed between being listed and being measured costs one entry instead of
    # raising out of the sort. This runs while an engine is logging, and it runs during
    # engine construction, where an exception would stop the engine from starting.
    aged: list[_LogFileAge] = []
    for path in matches:
        try:
            aged.append(_LogFileAge(modified_at=path.stat().st_mtime, path=path))
        except OSError:
            logger.debug("Could not read the age of log file '%s'; leaving it out.", path, exc_info=True)

    return [entry.path for entry in sorted(aged, key=lambda entry: entry.modified_at, reverse=True)]


def prune_log_files(directory: Path, retention_days: int, *, protected_name: str | None = None) -> int:
    """Delete engine log files older than the retention window.

    The file this process is writing is never deleted, and neither is
    ``protected_name``. Age alone is not enough to keep them: an engine that has been
    running longer than the retention window has a log file whose modification time
    falls outside it, and on POSIX deleting a file another process holds open silently
    discards the rest of that engine's session while it keeps writing.

    A file open in a *different* process cannot be recognized from here, so a second
    engine's current log is still at risk once it ages out. Names carry the start time
    and pid, so the window for that is the whole retention period rather than a race.

    Args:
        directory: Directory to prune.
        retention_days: Delete files older than this many days. Zero or fewer keeps
            everything.
        protected_name: A file name in ``directory`` to keep whatever its age. Passed by
            the file sink for the log it is about to open.

    Returns:
        The number of files deleted.
    """
    if retention_days <= 0:
        return 0

    # Compared by name, not by path: a name identifies a file uniquely within the one
    # directory being pruned, and two spellings of the same directory would not compare
    # equal as paths.
    active = active_log_file()
    protected = {name for name in (protected_name, active.name if active is not None else None) if name is not None}

    cutoff = time.time() - (retention_days * _SECONDS_PER_DAY)
    deleted = 0
    for path in find_log_files(directory):
        if path.name in protected:
            continue

        try:
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
        except OSError:
            # A file held open by another engine, or removed underneath us. Aging
            # logs out is housekeeping, so it must never break startup.
            logger.debug("Could not delete aged-out log file '%s'.", path, exc_info=True)
            continue
        deleted += 1

    return deleted


def _configure_buffer(buffer_lines: int) -> None:
    """Create, resize, or remove the in-memory ring buffer."""
    if buffer_lines <= 0:
        if _state.buffer is not None:
            logger.removeHandler(_state.buffer)
            _state.buffer = None
        return

    if _state.buffer is None:
        _state.buffer = SessionLogBuffer(buffer_lines)
        logger.addHandler(_state.buffer)
        return

    _state.buffer.resize(buffer_lines)


def _configure_file_handler(directory: Path, retention_days: int) -> bool:
    """Point the rotating file sink at ``directory``, replacing any existing one.

    Returns whether the sink is now writing into ``directory``. A failure leaves any
    handler already installed alone, so logging keeps working, just somewhere else.
    """
    file_name = _log_file_name()
    target_path = directory / file_name
    if _state.file_handler is not None and _state.file_path == target_path:
        # Already writing exactly there. Re-pruning is still worth doing: a
        # retention change is one of the reasons to be called again.
        prune_log_files(directory, retention_days, protected_name=file_name)
        return True

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning(
            "Could not create the engine log directory '%s'. Engine logs will not be written to file.",
            directory,
            exc_info=True,
        )
        return False

    prune_log_files(directory, retention_days, protected_name=file_name)

    try:
        # Opened now rather than on the first record (`delay=True`). A directory that
        # exists but cannot be written to is a real setup -- a log directory pointed at a
        # read-only volume, or one owned by another user -- and deferring the open moves
        # that failure out of this guard and into the first log call, where it surfaces as
        # a handler error on stderr while `active_log_file()` goes on naming a file that
        # will never exist.
        handler = RotatingFileHandler(
            target_path,
            maxBytes=MAX_LOG_FILE_BYTES,
            backupCount=LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError:
        logger.warning(
            "Could not open the engine log file '%s'. Engine logs will not be written to file.",
            target_path,
            exc_info=True,
        )
        return False

    handler.setLevel(logging.DEBUG)
    handler.setFormatter(DiagnosticFormatter())

    _remove_file_handler()
    logger.addHandler(handler)
    _state.file_handler = handler
    _state.file_path = target_path
    return True


def _remove_file_handler() -> None:
    """Detach and close the rotating file sink, if one is installed."""
    if _state.file_handler is None:
        return
    logger.removeHandler(_state.file_handler)
    _state.file_handler.close()
    _state.file_handler = None
    _state.file_path = None


def _log_file_name() -> str:
    """Return this process's log file name, computing it only once.

    The start timestamp plus the pid keeps two engines on one machine (and two
    runs whose pids happen to collide) in separate files, while still sorting
    chronologically in a directory listing.

    Cached for the life of the process, because the settings are applied several
    times while an engine boots. A name that moved with the clock would open a new
    file on every reconfiguration and split one session's logs across all of them.
    """
    if _state.file_name is None:
        started = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        _state.file_name = f"{LOG_FILE_PREFIX}{started}-{os.getpid()}.log"
    return _state.file_name
