"""Tests for bundle assembly.

Two things matter here. Every file copied in has to pass through the redactor, because a
log line is the most likely place for a credential to be sitting in plain text. And the
manifest has to be honest: a log that was shortened or a file that could not be read must
say so, since a silently truncated log reads exactly like a log with nothing in it.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.common.diagnostics.bundle import (
    HEALTH_FILE_NAME,
    LOGS_DIRECTORY_NAME,
    MANIFEST_FILE_NAME,
    README_FILE_NAME,
    REPORT_FILE_NAME,
    SESSION_LOG_FILE_NAME,
    TRUNCATION_NOTICE,
    WORKFLOW_DIRECTORY_NAME,
    DiagnosticsBundle,
    DiagnosticsBundleManifest,
)
from griptape_nodes.common.diagnostics.health import HealthCheckResult, HealthReport, HealthStatus
from griptape_nodes.common.diagnostics.redaction import REDACTED, Redactor
from griptape_nodes.common.diagnostics.report import (
    DiagnosticsReport,
    EngineDiagnostics,
    HostDiagnostics,
)

if TYPE_CHECKING:
    from pathlib import Path

_GENERATED_AT = "2026-01-01T00:00:00+00:00"
# A stand-in for a value the engine holds, not a real credential.
_SECRET = "s3cr3t-value-12345"  # noqa: S105


class _AssemblyError(Exception):
    """Stands in for something failing partway through building a bundle."""


def _redactor(secret_values: tuple[str, ...] = ()) -> Redactor:
    """A redactor with identity normalization off, so paths stay literal in assertions."""
    return Redactor(secret_values=secret_values, normalize_identity=False)


def _report() -> DiagnosticsReport:
    return DiagnosticsReport(
        generated_at=_GENERATED_AT,
        engine=EngineDiagnostics(python_version="3.12.0", python_executable="/usr/bin/python", process_id=1),
        host=HostDiagnostics(system="Darwin", release="25.0.0", version="Kernel", machine="arm64"),
    )


def _health_report() -> HealthReport:
    return HealthReport(
        generated_at=_GENERATED_AT,
        status=HealthStatus.WARN,
        results=[HealthCheckResult(name="Secrets", status=HealthStatus.WARN, summary="one is missing")],
    )


def _manifest(bundle: DiagnosticsBundle, warnings: list[str] | None = None) -> DiagnosticsBundleManifest:
    return bundle.write_manifest(
        generated_at=_GENERATED_AT,
        engine_version="0.1.0",
        identity_normalized=False,
        warnings=warnings if warnings is not None else [],
    )


def _entry_paths(bundle: DiagnosticsBundle) -> list[str]:
    return [entry.path for entry in _manifest(bundle).entries]


def _read(bundle: DiagnosticsBundle, path: str) -> str:
    with zipfile.ZipFile(io.BytesIO(bundle.to_zip_bytes())) as archive:
        return archive.read(path).decode("utf-8")


class TestSessionLog:
    def test_writes_the_captured_lines(self) -> None:
        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_session_log(["first line", "second line"])

            contents = _read(bundle, f"{LOGS_DIRECTORY_NAME}/{SESSION_LOG_FILE_NAME}")

        assert contents == "first line\nsecond line\n"

    def test_scrubs_a_credential_out_of_a_log_line(self) -> None:
        """The likeliest place for a leak: a library logging its own error message."""
        with DiagnosticsBundle(_redactor((_SECRET,))) as bundle:
            bundle.add_session_log([f"call failed with key {_SECRET}"])

            contents = _read(bundle, f"{LOGS_DIRECTORY_NAME}/{SESSION_LOG_FILE_NAME}")

        assert _SECRET not in contents
        assert REDACTED in contents

    def test_no_file_is_written_when_nothing_was_captured(self) -> None:
        """An empty file would read as "the engine logged nothing", a different claim."""
        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_session_log([])

            assert _entry_paths(bundle) == []


class TestLogFiles:
    def test_copies_log_files_from_disk(self, tmp_path: Path) -> None:
        log_file = tmp_path / "engine-1.log"
        log_file.write_text("from an earlier session\n", encoding="utf-8")
        warnings: list[str] = []

        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_log_files([log_file], warnings)

            assert _read(bundle, f"{LOGS_DIRECTORY_NAME}/engine-1.log") == "from an earlier session\n"

        assert warnings == []

    def test_scrubs_credentials_out_of_a_copied_file(self, tmp_path: Path) -> None:
        log_file = tmp_path / "engine-1.log"
        log_file.write_text("token sk-abcdefghijklmnop used\n", encoding="utf-8")

        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_log_files([log_file], [])

            contents = _read(bundle, f"{LOGS_DIRECTORY_NAME}/engine-1.log")

        assert "sk-abcdefghijklmnop" not in contents

    def test_keeps_the_end_of_a_file_that_exceeds_the_budget(self, tmp_path: Path) -> None:
        """The most recent lines are the ones describing whatever went wrong."""
        log_file = tmp_path / "engine-1.log"
        log_file.write_text("".join(f"line-{index}\n" for index in range(500)), encoding="utf-8")
        warnings: list[str] = []

        with DiagnosticsBundle(_redactor(), max_log_bytes=200) as bundle:
            bundle.add_log_files([log_file], warnings)

            contents = _read(bundle, f"{LOGS_DIRECTORY_NAME}/engine-1.log")

        assert contents.startswith(TRUNCATION_NOTICE)
        assert "line-499" in contents
        assert "line-0\n" not in contents
        assert any("earlier lines were left out" in warning for warning in warnings)

    def test_drops_a_partial_first_line_rather_than_emitting_a_fragment(self, tmp_path: Path) -> None:
        """Seeking by byte lands mid-line, and a fragment reads like a real record."""
        log_file = tmp_path / "engine-1.log"
        log_file.write_text("".join(f"line-{index:04d}\n" for index in range(100)), encoding="utf-8")

        with DiagnosticsBundle(_redactor(), max_log_bytes=95) as bundle:
            bundle.add_log_files([log_file], [])

            contents = _read(bundle, f"{LOGS_DIRECTORY_NAME}/engine-1.log")

        body = contents.removeprefix(TRUNCATION_NOTICE)
        for line in body.splitlines():
            assert re.fullmatch(r"line-\d{4}", line), line

    def test_names_the_files_the_budget_left_out(self, tmp_path: Path) -> None:
        """A log missing without explanation is indistinguishable from an empty one."""
        first = tmp_path / "engine-1.log"
        second = tmp_path / "engine-2.log"
        first.write_text("x" * 200, encoding="utf-8")
        second.write_text("y" * 200, encoding="utf-8")
        warnings: list[str] = []

        with DiagnosticsBundle(_redactor(), max_log_bytes=200) as bundle:
            bundle.add_log_files([first, second], warnings)

            staged = _entry_paths(bundle)

        assert f"{LOGS_DIRECTORY_NAME}/engine-1.log" in staged
        assert f"{LOGS_DIRECTORY_NAME}/engine-2.log" not in staged
        assert any("engine-2.log" in warning for warning in warnings)

    def test_an_unreadable_file_is_reported_rather_than_skipped_silently(self, tmp_path: Path) -> None:
        warnings: list[str] = []

        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_log_files([tmp_path / "not-there.log"], warnings)

            assert _entry_paths(bundle) == []

        assert any("not-there.log" in warning for warning in warnings)

    def test_one_unreadable_file_does_not_stop_the_others(self, tmp_path: Path) -> None:
        readable = tmp_path / "engine-1.log"
        readable.write_text("kept\n", encoding="utf-8")

        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_log_files([tmp_path / "gone.log", readable], [])

            assert f"{LOGS_DIRECTORY_NAME}/engine-1.log" in _entry_paths(bundle)


class TestWorkflow:
    def test_copies_the_saved_workflow(self, tmp_path: Path) -> None:
        workflow = tmp_path / "my_flow.py"
        workflow.write_text("# a workflow\n", encoding="utf-8")

        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_workflow(workflow, [])

            assert _read(bundle, f"{WORKFLOW_DIRECTORY_NAME}/my_flow.py") == "# a workflow\n"

    def test_scrubs_a_credential_pasted_into_a_workflow(self, tmp_path: Path) -> None:
        workflow = tmp_path / "my_flow.py"
        workflow.write_text('api_key = "sk-abcdefghijklmnop"\n', encoding="utf-8")

        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_workflow(workflow, [])

            assert "sk-abcdefghijklmnop" not in _read(bundle, f"{WORKFLOW_DIRECTORY_NAME}/my_flow.py")

    def test_says_so_when_the_workflow_could_not_be_read(self, tmp_path: Path) -> None:
        warnings: list[str] = []

        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_workflow(tmp_path / "gone.py", warnings)

            assert _entry_paths(bundle) == []

        assert any("gone.py" in warning for warning in warnings)


class TestReportAndHealth:
    def test_writes_the_report_as_json(self) -> None:
        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_report(_report())

            parsed = json.loads(_read(bundle, REPORT_FILE_NAME))

        assert parsed["generated_at"] == _GENERATED_AT
        assert parsed["engine"]["process_id"] == 1

    def test_writes_the_health_report_as_json(self) -> None:
        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_health_report(_health_report())

            parsed = json.loads(_read(bundle, HEALTH_FILE_NAME))

        assert parsed["status"] == "warn"
        assert parsed["results"][0]["name"] == "Secrets"

    def test_scrubs_a_quoted_error_message_in_a_health_result(self) -> None:
        """A failing check may be quoting the network stack, not writing its own text."""
        health = HealthReport(
            generated_at=_GENERATED_AT,
            status=HealthStatus.FAIL,
            results=[
                HealthCheckResult(
                    name="Cloud Connection",
                    status=HealthStatus.FAIL,
                    summary="refused: Bearer abcdefghijklmnopqrst",
                )
            ],
        )

        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_health_report(health)

            assert "abcdefghijklmnopqrst" not in _read(bundle, HEALTH_FILE_NAME)


class TestReadme:
    def test_points_at_the_health_report_when_one_was_staged(self) -> None:
        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_health_report(_health_report())
            bundle.add_readme()

            assert HEALTH_FILE_NAME in _read(bundle, README_FILE_NAME)

    def test_does_not_point_at_a_health_report_that_was_skipped(self) -> None:
        """Naming a file that is not here sends the reader looking for nothing."""
        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_report(_report())
            bundle.add_readme()

            assert HEALTH_FILE_NAME not in _read(bundle, README_FILE_NAME)


class TestManifest:
    def test_lists_every_staged_file_with_its_size(self) -> None:
        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_session_log(["a line"])
            bundle.add_report(_report())

            manifest = _manifest(bundle)

        assert [entry.path for entry in manifest.entries] == [
            f"{LOGS_DIRECTORY_NAME}/{SESSION_LOG_FILE_NAME}",
            REPORT_FILE_NAME,
        ]
        assert all(entry.size_bytes > 0 for entry in manifest.entries)
        assert all(entry.description for entry in manifest.entries)

    def test_counts_every_redaction_made_across_the_whole_bundle(self) -> None:
        """One redactor covers the report and the logs, so one count covers both."""
        with DiagnosticsBundle(_redactor((_SECRET,))) as bundle:
            bundle.add_session_log([f"key {_SECRET}", "token sk-abcdefghijklmnop"])

            manifest = _manifest(bundle)

        assert manifest.redaction.total == 2  # noqa: PLR2004
        assert manifest.redaction.identity_normalized is False

    def test_repeats_a_warning_only_once(self) -> None:
        """The same log file can be reported by more than one stage of collection."""
        with DiagnosticsBundle(_redactor()) as bundle:
            manifest = _manifest(bundle, ["a problem", "a problem", "another problem"])

        assert manifest.warnings == ["a problem", "another problem"]

    def test_records_the_versions_that_produced_the_bundle(self) -> None:
        with DiagnosticsBundle(_redactor()) as bundle:
            manifest = _manifest(bundle)

        assert manifest.engine_version == "0.1.0"
        assert manifest.schema_version
        assert manifest.report_schema_version


class TestZipAndCleanup:
    def test_the_zip_holds_the_manifest_and_every_staged_file(self) -> None:
        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_session_log(["a line"])
            bundle.add_report(_report())
            bundle.add_readme()
            _manifest(bundle)

            with zipfile.ZipFile(io.BytesIO(bundle.to_zip_bytes())) as archive:
                names = sorted(archive.namelist())

        assert names == sorted(
            [
                MANIFEST_FILE_NAME,
                README_FILE_NAME,
                REPORT_FILE_NAME,
                f"{LOGS_DIRECTORY_NAME}/{SESSION_LOG_FILE_NAME}",
            ]
        )

    def test_the_staging_directory_is_gone_afterwards(self) -> None:
        """Nothing is left behind on the machine of someone who only filed a bug."""
        with DiagnosticsBundle(_redactor()) as bundle:
            staging = bundle._staging_dir
            bundle.add_report(_report())

        assert not staging.exists()

    def test_the_staging_directory_is_removed_even_when_assembly_raises(self) -> None:
        bundle = DiagnosticsBundle(_redactor())
        staging = bundle._staging_dir

        with pytest.raises(_AssemblyError), bundle:
            raise _AssemblyError

        assert not staging.exists()
