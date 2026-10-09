"""What a library's dependency install is doing, told to the log and to the editor while it runs."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from packaging.requirements import InvalidRequirement, Requirement

from griptape_nodes.retained_mode.events.app_events import (
    EngineInitializationProgress,
    InitializationPhase,
    InitializationStatus,
)
from griptape_nodes.retained_mode.events.base_events import AppEvent

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine

logger = logging.getLogger("griptape_nodes")

# How many package names an install summary lists before counting the rest.
_SUMMARY_PACKAGE_LIMIT = 3

# The uv lines that say where an install is. uv writes them to stderr when it is not attached to a
# terminal, one per event, with no progress bars: "Downloading torch (2.0GiB)" only for packages
# large enough to be worth announcing, " Downloaded torch" when that download finishes, and
# "Prepared 35 packages in 4m 12s" once everything is downloaded and built.
_DOWNLOADING_LINE = re.compile(r"^Downloading (?P<name>\S+) \((?P<size>[\d.]+\s*[KMGT]?i?B)\)$")
_DOWNLOADED_LINE = re.compile(r"^Downloaded (?P<name>\S+)$")
_PREPARED_LINE = re.compile(r"^Prepared (?P<count>\d+) packages?\b")
# Printed at the start of every uv run, including the retry without the engine's version floors.
_RESOLVED_LINE = re.compile(r"^Resolved \d+ packages?\b")
# Downloads at least this large are logged at INFO: they are what makes an install take minutes
# (torch, the CUDA wheels), and there are only a few per install. Smaller ones go to DEBUG, so a
# console at the default level is not filled with every package. The editor gets them all.
_INFO_DOWNLOAD_BYTES = 100 * 1024**2
_SIZE = re.compile(r"^(?P<amount>[\d.]+)\s*(?P<unit>[KMGT]?)i?B$")
_UNIT_EXPONENTS = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4}


def summarize_dependencies(pip_dependencies: list[str]) -> str:
    """Name the first few packages of an install, without version specifiers, and count the rest.

    For example "torch, diffusers, transformers, and 11 more". A requirement that does not parse
    (a bare URL, say) is shown as written.
    """
    names = []
    for requirement in pip_dependencies[:_SUMMARY_PACKAGE_LIMIT]:
        try:
            names.append(Requirement(requirement).name)
        except InvalidRequirement:
            names.append(requirement)
    remaining = len(pip_dependencies) - len(names)
    if remaining > 0:
        names.append(f"and {remaining} more")
    return ", ".join(names)


class LibraryInstallProgress:
    """Reports one dependency-set install of a library, from announcement to finish.

    Logs what is installing, and while a library load is registering the library, sends the
    load's LOADING progress event with a `detail` an artist can read. The editor shows that
    detail against the library's place in the load, so an install outside a load only logs.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        library_name: str,
        library_file_path: str,
        pip_dependencies: list[str],
        execution: bool,
    ) -> None:
        self._engine = engine
        self._library_name = library_name
        self._library_file_path = library_file_path
        # The execution set repeats the edit-time set and can overlap the declared execution pins.
        self._dependencies = list(dict.fromkeys(pip_dependencies))
        # A library with execution dependencies gets two installs, its edit-time environment and
        # then its execution environment, so the execution one says it is for running the nodes.
        if execution:
            self._venv_kind = "execution"
            self._purpose = f"running nodes from {library_name}"
        else:
            self._venv_kind = "edit-time"
            self._purpose = library_name
        # Downloads uv has started and not finished, by package name, with the size it announced.
        self._pending_downloads: dict[str, str] = {}
        self._last_detail: str | None = None

    def announce(self, *, fresh_venv: bool) -> None:
        """Log the install that is about to run and report it.

        A fresh environment gets every package installed, which can take minutes, so it is
        announced at INFO with a warning about the wait. A reused one usually only needs a quick
        check that its packages are still there, so it is logged at DEBUG. Its detail still warns
        about the wait, because a library update can add packages that take as long to install.
        """
        # A rebuild announces again after a failed attempt, whose downloads will not finish.
        self._pending_downloads.clear()
        package_count = len(self._dependencies)
        package_noun = _package_noun(package_count)
        summary = summarize_dependencies(self._dependencies)
        if fresh_venv:
            logger.info(
                "Installing %d %s for library '%s' (%s environment): %s",
                package_count,
                package_noun,
                self._library_name,
                self._venv_kind,
                summary,
            )
            detail = (
                f"Installing {package_count} {package_noun} for {self._purpose}: {summary}. "
                "This can take several minutes."
            )
        else:
            logger.debug(
                "Checking %d %s for library '%s' (%s environment): %s",
                package_count,
                package_noun,
                self._library_name,
                self._venv_kind,
                summary,
            )
            detail = f"Checking packages for {self._purpose}. Installing any that are new can take several minutes."
        self._report(detail)

    def on_installer_line(self, line: str) -> None:
        """Turn one line of uv's output into an updated detail, when it says where the install is.

        A large download starting (100 MiB or more) and uv having every package ready (downloaded or
        taken from its cache) are logged at INFO, since they only happen during a real install and
        are what a console user waits on. Every other line, smaller downloads included, is logged
        at DEBUG, so a DEBUG log still shows the installer's own output.
        """
        text = line.strip()
        if not text:
            return

        downloading = _DOWNLOADING_LINE.match(text)
        if downloading is not None:
            name = downloading.group("name")
            size = downloading.group("size")
            if _size_in_bytes(size) >= _INFO_DOWNLOAD_BYTES:
                logger.info(
                    "Downloading %s (%s) for library '%s' (%s environment)",
                    name,
                    size,
                    self._library_name,
                    self._venv_kind,
                )
            else:
                logger.debug("Installer (%s, %s environment): %s", self._library_name, self._venv_kind, text)
            self._pending_downloads[name] = size
            self._report(self._detail_for_downloads())
            return

        prepared = _PREPARED_LINE.match(text)
        if prepared is not None:
            package_count = int(prepared.group("count"))
            package_noun = _package_noun(package_count)
            logger.info(
                "Ready to install %d %s for library '%s' (%s environment)",
                package_count,
                package_noun,
                self._library_name,
                self._venv_kind,
            )
            self._pending_downloads.clear()
            self._report(f"Installing {package_count} {package_noun} for {self._purpose}...")
            return

        logger.debug("Installer (%s, %s environment): %s", self._library_name, self._venv_kind, text)
        if _RESOLVED_LINE.match(text) is not None:
            # A new uv run: anything a failed earlier run was downloading will not finish.
            self._pending_downloads.clear()
            return

        downloaded = _DOWNLOADED_LINE.match(text)
        if downloaded is not None:
            self._pending_downloads.pop(downloaded.group("name"), None)
            if self._pending_downloads:
                self._report(self._detail_for_downloads())

    def clear(self) -> None:
        """Clear the install's detail once the installer has stopped, whether it worked or not.

        Registration continues after the install (a failed execution install does not stop the
        library loading), and the editor would otherwise keep showing the install message until
        the library finishes.
        """
        self._pending_downloads.clear()
        self._last_detail = None
        self._send(detail=None, dependencies=None)

    def _detail_for_downloads(self) -> str:
        """Describe the downloads in flight, largest first, since that is the one the wait is for."""
        by_size = sorted(self._pending_downloads.items(), key=lambda item: _size_in_bytes(item[1]), reverse=True)
        largest_name, largest_size = by_size[0]
        others = len(by_size) - 1
        if others == 0:
            return f"Downloading {largest_name} ({largest_size}) for {self._purpose}..."
        return f"Downloading {largest_name} ({largest_size}) and {others} more for {self._purpose}..."

    def _report(self, detail: str) -> None:
        """Send a new detail, skipping one that says the same as the last."""
        if detail == self._last_detail:
            return
        self._last_detail = detail
        self._send(detail=detail, dependencies=self._dependencies)

    def _send(self, *, detail: str | None, dependencies: list[str] | None) -> None:
        """Send the library's LOADING progress event, only while a load is registering it.

        A library dependency downloaded and registered from inside another library's
        registration is not tracked by the load, so its installs only log.
        """
        progress = self._engine.library_manager.load_progress_for(self._library_file_path)
        if progress is None:
            return

        self._engine.event_manager.put_event(
            AppEvent(
                payload=EngineInitializationProgress(
                    phase=InitializationPhase.LIBRARIES,
                    item_name=self._library_name,
                    status=InitializationStatus.LOADING,
                    current=progress.current,
                    total=progress.total,
                    is_worker=self._engine.library_manager.is_worker,
                    detail=detail,
                    dependencies=dependencies,
                )
            )
        )


def _package_noun(package_count: int) -> str:
    if package_count == 1:
        return "package"
    return "packages"


def _size_in_bytes(size: str) -> float:
    """Read a uv download size such as "2.0GiB" or "512KiB" as a byte count, for sorting only."""
    match = _SIZE.match(size.replace(" ", ""))
    if match is None:
        return 0.0
    return float(match.group("amount")) * 1024 ** _UNIT_EXPONENTS[match.group("unit")]
