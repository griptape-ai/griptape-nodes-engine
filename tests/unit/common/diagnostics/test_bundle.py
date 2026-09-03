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

# The README lines pointing at a whole directory. Matched as the start of the line rather
# than by the directory name, which also appears in `logs/session.log` a line above.
_LOGS_DIRECTORY_LINE = f"- `{LOGS_DIRECTORY_NAME}/` --"
_WORKFLOW_DIRECTORY_LINE = f"- `{WORKFLOW_DIRECTORY_NAME}/` --"


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


def _read_bytes(bundle: DiagnosticsBundle, path: str) -> bytes:
    """Read a staged file without decoding it, for asserting on line endings."""
    with zipfile.ZipFile(io.BytesIO(bundle.to_zip_bytes())) as archive:
        return archive.read(path)


def _write_source_log(path: Path, text: str) -> Path:
    r"""Write a log file for the bundle to copy, with the line endings given and no others.

    Bytes rather than ``write_text``, which translates every ``\n`` to the platform's line
    ending. Log files are copied into a bundle byte for byte, so a source written that way
    is a different file on Windows than on POSIX -- both the content a test then asserts on
    and, since the budget is counted in bytes, how much of it fits.
    """
    path.write_bytes(text.encode("utf-8"))
    return path


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
        log_file = _write_source_log(tmp_path / "engine-1.log", "from an earlier session\n")
        warnings: list[str] = []

        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_log_files([log_file], warnings)

            assert _read(bundle, f"{LOGS_DIRECTORY_NAME}/engine-1.log") == "from an earlier session\n"

        assert warnings == []

    def test_scrubs_credentials_out_of_a_copied_file(self, tmp_path: Path) -> None:
        log_file = _write_source_log(tmp_path / "engine-1.log", "token sk-abcdefghijklmnop used\n")

        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_log_files([log_file], [])

            contents = _read(bundle, f"{LOGS_DIRECTORY_NAME}/engine-1.log")

        assert "sk-abcdefghijklmnop" not in contents

    def test_keeps_the_end_of_a_file_that_exceeds_the_budget(self, tmp_path: Path) -> None:
        """The most recent lines are the ones describing whatever went wrong."""
        log_file = _write_source_log(tmp_path / "engine-1.log", "".join(f"line-{index}\n" for index in range(500)))
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
        log_file = _write_source_log(tmp_path / "engine-1.log", "".join(f"line-{index:04d}\n" for index in range(100)))

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
        readable = _write_source_log(tmp_path / "engine-1.log", "kept\n")

        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_log_files([tmp_path / "gone.log", readable], [])

            assert f"{LOGS_DIRECTORY_NAME}/engine-1.log" in _entry_paths(bundle)

    def test_a_file_exactly_at_the_budget_is_kept_whole(self, tmp_path: Path) -> None:
        """Off by one here would stamp a truncation notice on a complete file."""
        log_file = tmp_path / "engine-1.log"
        log_file.write_text("x" * 200, encoding="utf-8")
        warnings: list[str] = []

        with DiagnosticsBundle(_redactor(), max_log_bytes=200) as bundle:
            bundle.add_log_files([log_file], warnings)

            contents = _read(bundle, f"{LOGS_DIRECTORY_NAME}/engine-1.log")

        assert contents == "x" * 200
        assert warnings == []

    def test_a_second_log_with_the_same_name_is_reported_rather_than_overwriting_the_first(
        self, tmp_path: Path
    ) -> None:
        """Two log directories can hold the same file name; one staged path can hold one file."""
        first_dir = tmp_path / "current"
        second_dir = tmp_path / "older"
        first_dir.mkdir()
        second_dir.mkdir()
        first = first_dir / "engine-1.log"
        second = second_dir / "engine-1.log"
        _write_source_log(first, "from the current directory\n")
        _write_source_log(second, "from the older directory\n")
        warnings: list[str] = []

        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_log_files([first, second], warnings)

            staged = _entry_paths(bundle)
            contents = _read(bundle, f"{LOGS_DIRECTORY_NAME}/engine-1.log")

        assert staged == [f"{LOGS_DIRECTORY_NAME}/engine-1.log"]
        assert contents == "from the current directory\n"
        assert any("same name" in warning for warning in warnings)

    def test_a_log_written_with_windows_line_endings_is_copied_byte_for_byte(self, tmp_path: Path) -> None:
        r"""A bundle is evidence, so a copied log has to match the file it came from.

        Staging a file with the default newline handling translates every `\n` to the
        platform's line ending on the way out. On Windows that rewrites a line that already
        ended `\r\n` as `\r\r\n`, so every log copied into a bundle collected there differed
        from the file on disk. Asserted on the bytes rather than the text because that is
        where the difference is, and only fails on Windows: elsewhere the translation is a
        no-op.
        """
        log_file = tmp_path / "engine-1.log"
        source = b"first line\r\nsecond line\r\n"
        log_file.write_bytes(source)

        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_log_files([log_file], [])

            staged = _read_bytes(bundle, f"{LOGS_DIRECTORY_NAME}/engine-1.log")

        assert staged == source

    def test_a_small_budget_is_stated_in_bytes_rather_than_as_zero_megabytes(self, tmp_path: Path) -> None:
        """A budget rendered as `0 MB` reads as a bug in the message, not as a very small budget."""
        first = tmp_path / "engine-1.log"
        second = tmp_path / "engine-2.log"
        first.write_text("x" * 200, encoding="utf-8")
        second.write_text("y" * 200, encoding="utf-8")
        warnings: list[str] = []

        with DiagnosticsBundle(_redactor(), max_log_bytes=200) as bundle:
            bundle.add_log_files([first, second], warnings)

        assert any("200 bytes of logs" in warning for warning in warnings)


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

    def test_a_users_name_in_the_file_name_does_not_reach_the_archive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A workflow's file name is the user's own words, and often their own name.

        The manifest's entry list and the archive's directory are the one part of a bundle
        nothing else redacts: neither is written through `redact_text`.
        """
        monkeypatch.setattr("getpass.getuser", lambda: "samantha")
        workflow = tmp_path / "samantha-final.py"
        workflow.write_text("# a workflow\n", encoding="utf-8")

        with DiagnosticsBundle(Redactor()) as bundle:
            bundle.add_workflow(workflow, [])

            staged = _entry_paths(bundle)
            with zipfile.ZipFile(io.BytesIO(bundle.to_zip_bytes())) as archive:
                names = archive.namelist()

        assert staged == [f"{WORKFLOW_DIRECTORY_NAME}/user-final.py"]
        assert f"{WORKFLOW_DIRECTORY_NAME}/user-final.py" in names

    def test_the_replacement_stays_a_legal_file_name_on_windows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`<user>` extracts fine here and not at all on the machine reading the bundle.

        Angle brackets are legal on POSIX and illegal in a Windows file name, so leaving
        them in would make a bundle that only some of the people who need it can open.
        """
        monkeypatch.setattr("getpass.getuser", lambda: "samantha")
        workflow = tmp_path / "samantha-final.py"
        workflow.write_text("# a workflow\n", encoding="utf-8")

        with DiagnosticsBundle(Redactor()) as bundle:
            bundle.add_workflow(workflow, [])

            staged_path = _entry_paths(bundle)[0]

        assert "<" not in staged_path
        assert ">" not in staged_path


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
    """The guide describes the bundle it is in, not the bundle a full collection would make.

    Every section is optional: switched off by a request flag, or skipped because there was
    nothing to collect -- no log files on disk yet, a workflow that has never been saved.
    Naming a file that is not here sends its reader hunting for it, and reads as though the
    collection came back empty-handed when that part was never asked for.
    """

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

    def test_does_not_point_at_a_report_that_was_skipped(self) -> None:
        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_session_log(["a line"])
            bundle.add_readme()

            assert REPORT_FILE_NAME not in _read(bundle, README_FILE_NAME)

    def test_points_at_the_session_log_when_the_engine_had_logged_something(self) -> None:
        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_session_log(["a line"])
            bundle.add_readme()

            assert SESSION_LOG_FILE_NAME in _read(bundle, README_FILE_NAME)

    def test_does_not_point_at_a_session_log_the_engine_never_wrote(self) -> None:
        """A brand new engine has logged nothing, and `--skip-logs` leaves it out on purpose."""
        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_report(_report())
            bundle.add_readme()

            assert SESSION_LOG_FILE_NAME not in _read(bundle, README_FILE_NAME)

    def test_does_not_point_at_the_log_directory_when_only_the_session_log_is_here(self) -> None:
        """The two share a directory, so the second line is about the files from earlier runs.

        Nothing had rotated to disk yet, which is the ordinary state of a first session.
        """
        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_session_log(["a line"])
            bundle.add_readme()

            assert _LOGS_DIRECTORY_LINE not in _read(bundle, README_FILE_NAME)

    def test_points_at_the_log_directory_once_a_file_from_disk_is_here(self, tmp_path: Path) -> None:
        log_file = _write_source_log(tmp_path / "engine-1.log", "from an earlier session\n")

        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_log_files([log_file], [])
            bundle.add_readme()

            assert _LOGS_DIRECTORY_LINE in _read(bundle, README_FILE_NAME)

    def test_points_at_the_workflow_when_one_was_staged(self, tmp_path: Path) -> None:
        workflow = tmp_path / "my_flow.py"
        workflow.write_text("# a workflow\n", encoding="utf-8")

        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_workflow(workflow, [])
            bundle.add_readme()

            assert _WORKFLOW_DIRECTORY_LINE in _read(bundle, README_FILE_NAME)

    def test_does_not_point_at_a_workflow_that_is_not_here(self) -> None:
        """Nothing is open in a CLI-launched engine, so this is the usual case rather than a rare one."""
        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_report(_report())
            bundle.add_readme()

            assert _WORKFLOW_DIRECTORY_LINE not in _read(bundle, README_FILE_NAME)

    def test_always_points_at_the_manifest(self) -> None:
        """The one file every bundle has, and the one that says what the others are."""
        with DiagnosticsBundle(_redactor()) as bundle:
            bundle.add_readme()

            assert MANIFEST_FILE_NAME in _read(bundle, README_FILE_NAME)


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

    def test_scrubs_a_secret_out_of_a_warning(self) -> None:
        """Most warnings quote an OSError, and its text carries whatever it failed on."""
        with DiagnosticsBundle(_redactor((_SECRET,))) as bundle:
            manifest = _manifest(bundle, [f"could not read the file at {_SECRET}"])

        assert _SECRET not in manifest.warnings[0]
        assert REDACTED in manifest.warnings[0]

    def test_counts_a_redaction_made_in_a_warning(self) -> None:
        """The warnings are redacted before the counts are read, so the removal is visible."""
        with DiagnosticsBundle(_redactor((_SECRET,))) as bundle:
            manifest = _manifest(bundle, [f"could not read the file at {_SECRET}"])

        assert manifest.redaction.total == 1

    def test_records_the_versions_that_produced_the_bundle(self) -> None:
        with DiagnosticsBundle(_redactor()) as bundle:
            manifest = _manifest(bundle)

        assert manifest.engine_version == "0.1.0"
        assert manifest.schema_version
        assert manifest.report_schema_version

    def test_a_manifest_that_never_stated_normalization_does_not_claim_it(self) -> None:
        """The field is a claim about what was removed, so its default has to fail closed.

        An older bundle, or one round-tripped through a support tool that dropped the field,
        would otherwise assert that the home directory and username had been taken out when
        nothing checked.
        """
        assert DiagnosticsBundleManifest(generated_at=_GENERATED_AT).redaction.identity_normalized is False
        assert (
            DiagnosticsBundleManifest.model_validate_json(
                json.dumps({"generated_at": _GENERATED_AT, "redaction": {"total": 3}})
            ).redaction.identity_normalized
            is False
        )


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

        # The raise is inside a helper rather than directly under the `with`, so that the
        # assertion below is plainly reached rather than looking like code after a raise.
        # `with pytest.raises(...), bundle:` reads as a block that always ends by raising,
        # and static analysis calls the assertion unreachable and `staging` unused -- which
        # would mean the one thing this test checks is never checked.
        def assembly_that_fails() -> None:
            with bundle:
                raise _AssemblyError

        with pytest.raises(_AssemblyError):
            assembly_that_fails()

        assert not staging.exists()
