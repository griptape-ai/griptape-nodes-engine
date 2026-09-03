"""Assembles a diagnostics bundle: one zip holding everything needed to troubleshoot an engine.

A bundle is the shareable artifact behind the report. It stages files in a temporary
directory, redacts every piece of text it copies, and zips the result. Nothing is written
to the user's workspace by this module; the caller decides where the bytes go.

Layout of a bundle:

- ``manifest.json`` -- what this bundle is and what is in it, including how many values
  were hidden.
- ``README.md`` -- a plain-language guide to the other files.
- ``report.json`` -- the ``DiagnosticsReport``: versions, machine, settings, libraries,
  projects, logging configuration.
- ``doctor.json`` -- the health checks' verdict on that report, and what to do about
  each problem found.
- ``logs/session.log`` -- everything this engine logged since it started.
- ``logs/<rotated>.log`` -- the log files on disk, newest first, up to a size budget.
- ``workflow/<name>.py`` -- the workflow that was open, when there is a saved file for it.

Everything copied in passes through the caller's ``Redactor`` first, so the same
redaction counts cover the whole bundle rather than the report alone.
"""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Self

from pydantic import BaseModel, Field

from griptape_nodes.common.diagnostics.report import (
    DIAGNOSTICS_REPORT_SCHEMA_VERSION,
    RedactionSummary,
)

if TYPE_CHECKING:
    from griptape_nodes.common.diagnostics.health import HealthReport
    from griptape_nodes.common.diagnostics.redaction import Redactor
    from griptape_nodes.common.diagnostics.report import DiagnosticsReport

# Schema version for the bundle layout. Bump when files are added, removed, or renamed so
# a support tool reading an older bundle knows what to expect.
DIAGNOSTICS_BUNDLE_SCHEMA_VERSION = "0.1.0"

MANIFEST_FILE_NAME = "manifest.json"
README_FILE_NAME = "README.md"
REPORT_FILE_NAME = "report.json"
HEALTH_FILE_NAME = "doctor.json"
LOGS_DIRECTORY_NAME = "logs"
SESSION_LOG_FILE_NAME = "session.log"
WORKFLOW_DIRECTORY_NAME = "workflow"

# Total budget for log files copied out of the log directory, newest first. Log history can
# reach 60 MB once rotation has been running a while, and every byte has to be scanned for
# credentials on the way in. Whatever is dropped is named in the manifest, because a
# silently shortened log reads as a log that simply has nothing in it.
DEFAULT_MAX_LOG_BYTES = 20 * 1024 * 1024

TRUNCATION_NOTICE = "... earlier lines in this file were left out to keep the bundle small ...\n"


class BundleEntry(BaseModel):
    """One file inside a bundle.

    Attributes:
        path: Location inside the zip, using forward slashes.
        size_bytes: Size of the file as written.
        description: What the file is, in plain language.
    """

    path: str
    size_bytes: int
    description: str


class DiagnosticsBundleManifest(BaseModel):
    """What a bundle is and what it contains.

    Read first by anything opening a bundle: it names every other file, states which
    schema versions were used to write them, and reports how much was hidden.

    Attributes:
        schema_version: Version of the bundle layout.
        report_schema_version: Version of the report envelope inside ``report.json``.
        generated_at: ISO 8601 timestamp (UTC) of when the bundle was built.
        engine_version: Version of the engine that produced it.
        redaction: What was removed, across every file in the bundle.
        entries: Every file in the bundle.
        warnings: Anything that could not be collected or had to be shortened. Always
            read this before concluding something is absent.
    """

    schema_version: str = DIAGNOSTICS_BUNDLE_SCHEMA_VERSION
    report_schema_version: str = DIAGNOSTICS_REPORT_SCHEMA_VERSION
    generated_at: str
    engine_version: str | None = None
    redaction: RedactionSummary = Field(default_factory=RedactionSummary)
    entries: list[BundleEntry] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class DiagnosticsBundle:
    """Stages the files of a bundle in a temporary directory, then zips them.

    Use as a context manager so the staging directory is always removed::

        with DiagnosticsBundle(redactor) as bundle:
            bundle.add_session_log(lines)
            bundle.add_log_files(paths, warnings)
            bundle.add_health_report(health)
            bundle.add_report(report)
            bundle.add_readme()
            manifest = bundle.write_manifest(generated_at, engine_version, warnings)
            data = bundle.to_zip_bytes()

    Add the files that carry free text before building the report, so the redaction
    counts in the manifest cover the whole bundle. Not thread-safe.
    """

    def __init__(self, redactor: Redactor, *, max_log_bytes: int = DEFAULT_MAX_LOG_BYTES) -> None:
        """Create a staging directory for a bundle.

        Args:
            redactor: Applied to every piece of text copied in. Shared with the caller so
                one set of counts covers the report and the bundled files together.
            max_log_bytes: Total budget for log files copied out of the log directory.
        """
        self._redactor = redactor
        self._max_log_bytes = max_log_bytes
        self._staging_dir = Path(tempfile.mkdtemp(prefix="gtn-diagnostics-"))
        self._entries: list[BundleEntry] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.cleanup()

    def add_session_log(self, lines: list[str]) -> None:
        """Write everything the engine logged this session, redacted."""
        if not lines:
            return

        text = "\n".join(lines) + "\n"
        self._write(
            f"{LOGS_DIRECTORY_NAME}/{SESSION_LOG_FILE_NAME}",
            self._redactor.redact_text(text),
            "Everything this engine logged since it started.",
        )

    def add_log_files(self, log_files: list[Path], warnings: list[str]) -> None:
        """Copy log files from disk, newest first, until the size budget runs out.

        Args:
            log_files: Log files to copy, newest first.
            warnings: Appended to when a file is shortened, skipped, or cannot be read,
                so nothing is dropped silently.
        """
        remaining = self._max_log_bytes

        for path in log_files:
            try:
                size = path.stat().st_size
            except OSError as err:
                warnings.append(f"Log file '{path.name}' could not be read and is not in this bundle: {err}")
                continue

            if remaining <= 0:
                warnings.append(
                    f"Log file '{path.name}' was left out because the bundle already holds "
                    f"{self._max_log_bytes // (1024 * 1024)} MB of logs."
                )
                continue

            text = self._read_log_tail(path, remaining, warnings)
            if text is None:
                continue

            if size > remaining:
                warnings.append(
                    f"Only the most recent part of log file '{path.name}' is in this bundle; "
                    "earlier lines were left out to keep the bundle small."
                )
                text = TRUNCATION_NOTICE + text

            self._write(
                f"{LOGS_DIRECTORY_NAME}/{path.name}",
                self._redactor.redact_text(text),
                "An engine log file from the log directory.",
            )
            # Charged for what was actually copied, so a truncated file spends the rest of
            # the budget rather than borrowing against it.
            remaining -= min(size, remaining)

    def add_workflow(self, workflow_path: Path, warnings: list[str]) -> None:
        """Copy the saved file of the workflow that was open, redacted.

        Only what is on disk. A workflow with unsaved edits is bundled as it was last
        saved, which is stated in ``warnings`` so nobody debugs against the wrong graph.
        """
        try:
            text = workflow_path.read_text(encoding="utf-8", errors="replace")
        except OSError as err:
            warnings.append(
                f"The open workflow '{workflow_path.name}' could not be read and is not in this bundle: {err}"
            )
            return

        self._write(
            f"{WORKFLOW_DIRECTORY_NAME}/{workflow_path.name}",
            self._redactor.redact_text(text),
            "The workflow that was open, as it was last saved. Unsaved edits are not included.",
        )

    def add_report(self, report: DiagnosticsReport) -> None:
        """Write the diagnostics report.

        Already redacted by the caller when it was built, so it is written as-is.
        """
        self._write(
            REPORT_FILE_NAME,
            report.model_dump_json(indent=2) + "\n",
            "Engine version, machine, settings, libraries, projects, and logging setup.",
        )

    def add_health_report(self, health: HealthReport) -> None:
        """Write the health checks' verdicts.

        Redacted on the way in: a check that failed may be quoting an error message from
        the network stack, and that message is not the engine's own text.
        """
        self._write(
            HEALTH_FILE_NAME,
            self._redactor.redact_text(health.model_dump_json(indent=2)) + "\n",
            "What the health checks found, and what to do about each problem.",
        )

    def add_readme(self) -> None:
        """Write a plain-language guide to the rest of the bundle.

        Call after everything else is staged: the guide only points at files that are
        actually here, so it never sends a reader looking for one that was skipped.
        """
        self._write(README_FILE_NAME, self._readme_text(), "This guide.")

    def write_manifest(
        self,
        *,
        generated_at: str,
        engine_version: str | None,
        identity_normalized: bool,
        warnings: list[str],
    ) -> DiagnosticsBundleManifest:
        """Write the manifest and return it.

        Call last: the redaction counts are read from the redactor here, so everything
        else has to be staged first for them to be complete.
        """
        manifest = DiagnosticsBundleManifest(
            generated_at=generated_at,
            engine_version=engine_version,
            redaction=RedactionSummary(
                identity_normalized=identity_normalized,
                total=self._redactor.total_redactions(),
                counts=self._redactor.counts(),
            ),
            entries=sorted(self._entries, key=lambda entry: entry.path),
            warnings=list(dict.fromkeys(warnings)),
        )

        # Written directly rather than through _write: the manifest lists the bundle's
        # files, and listing itself would make its own recorded size wrong.
        path = self._staging_dir / MANIFEST_FILE_NAME
        path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return manifest

    def to_zip_bytes(self) -> bytes:
        """Zip the staged files and return the archive as bytes."""
        archive_path = self._staging_dir.parent / f"{self._staging_dir.name}.zip"
        try:
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(self._staging_dir.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(self._staging_dir).as_posix())
            return archive_path.read_bytes()
        finally:
            archive_path.unlink(missing_ok=True)

    def cleanup(self) -> None:
        """Remove the staging directory."""
        shutil.rmtree(self._staging_dir, ignore_errors=True)

    def _write(self, relative_path: str, text: str, description: str) -> None:
        """Write one staged file and record it as an entry."""
        path = self._staging_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self._entries.append(BundleEntry(path=relative_path, size_bytes=path.stat().st_size, description=description))

    def _read_log_tail(self, path: Path, max_bytes: int, warnings: list[str]) -> str | None:
        """Return the last ``max_bytes`` of a log file as text, or None when it cannot be read.

        The tail rather than the head, because the most recent lines are the ones that
        describe whatever went wrong. Reading binary and decoding with replacement keeps a
        log written by a library in an unexpected encoding from failing the whole bundle.
        """
        try:
            with path.open("rb") as handle:
                size = handle.seek(0, 2)
                start = max(0, size - max_bytes)
                handle.seek(start)
                raw = handle.read()
        except OSError as err:
            warnings.append(f"Log file '{path.name}' could not be read and is not in this bundle: {err}")
            return None

        text = raw.decode("utf-8", errors="replace")
        if start > 0:
            # Seeking by byte lands mid-line; drop the partial first line rather than
            # emitting a fragment that looks like a real record.
            _, newline, remainder = text.partition("\n")
            if newline:
                text = remainder
        return text

    def _readme_text(self) -> str:
        """Return the bundle's README."""
        health_line = ""
        if any(entry.path == HEALTH_FILE_NAME for entry in self._entries):
            health_line = (
                f"- `{HEALTH_FILE_NAME}` -- read this second. The engine's own verdict on its setup: what\n"
                "  is wrong, and what to do about each one.\n"
            )

        return f"""# Griptape Nodes diagnostics bundle

This folder holds everything needed to work out why Griptape Nodes behaved the way it
did on one machine. It was created by the engine itself, and it is safe to share.

## What is in here

- `{MANIFEST_FILE_NAME}` -- start here. Lists every file, and says how many values were
  hidden and why.
{health_line}- `{REPORT_FILE_NAME}` -- which version of the engine was running, on what kind of
  machine, with which settings, and how every library and project fared when it loaded.
- `{LOGS_DIRECTORY_NAME}/{SESSION_LOG_FILE_NAME}` -- everything the engine logged from
  the moment it started until this bundle was made.
- `{LOGS_DIRECTORY_NAME}/` -- the log files kept on disk, newest first. Useful when the
  problem happened in an earlier session.
- `{WORKFLOW_DIRECTORY_NAME}/` -- the workflow that was open, if it had been saved.

## What was taken out

No API keys, passwords, or other secrets are in here. The engine knows its own secrets,
so it searched every file above and removed them. It also removed anything shaped like a
credential, and replaced the home directory with `~` and the username with `<user>`.

Anything removed shows up as `<redacted>`. `{MANIFEST_FILE_NAME}` counts every removal,
so a setting that looks empty can be told apart from one that was hidden.

## What might be missing

Check `warnings` in `{MANIFEST_FILE_NAME}`. It names anything that could not be
collected or had to be shortened. A section that is missing for a stated reason is very
often the reason this bundle was made.

Logs only contain what the engine was set to record. If `log_level` was `INFO`, debug
detail was never written down and is not recoverable after the fact. To capture it, set
the log level to `DEBUG`, reproduce the problem, and make a new bundle.
"""
