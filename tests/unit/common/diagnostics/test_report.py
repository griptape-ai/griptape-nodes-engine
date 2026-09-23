"""Tests for the diagnostics report model.

A report is written to a file by one engine and read back by something else -- a support
tool, a newer engine, a person -- so the model's defaults are not cosmetic. Two of them
carry a claim: `identity_normalized` says the home directory was taken out, and
`worker_ready` says whether a worker is up. If either one defaults the wrong way, a report
that never checked asserts something anyway, and the reader believes it.

The rest of the defaults exist so a report is still produced when a section could not be
gathered. That is the whole reason `collection_warnings` is there, and it only works if
every section can be left out without the envelope refusing to build.
"""

from __future__ import annotations

from typing import Any

from griptape_nodes.common.diagnostics.report import (
    DIAGNOSTICS_REPORT_SCHEMA_VERSION,
    ConfigDiagnostics,
    ConfigFileDiagnostics,
    DiagnosticsReport,
    EngineDiagnostics,
    HostDiagnostics,
    LibraryDiagnostics,
    LogDiagnostics,
    LogFileDiagnostics,
    PathDiagnostics,
    ProjectDiagnostics,
    ProjectProblemDiagnostics,
    RedactionSummary,
    SecretDiagnostics,
    SessionDiagnostics,
)


def _engine() -> EngineDiagnostics:
    return EngineDiagnostics(python_version="3.12.11", python_executable="/venv/bin/python", process_id=42)


def _host() -> HostDiagnostics:
    return HostDiagnostics(system="Darwin", release="25.6.0", version="Darwin Kernel 25.6.0", machine="arm64")


def _report(**overrides: Any) -> DiagnosticsReport:
    fields: dict[str, Any] = {
        "generated_at": "2026-09-23T12:00:00Z",
        "engine": _engine(),
        "host": _host(),
    }
    fields.update(overrides)
    return DiagnosticsReport(**fields)


class TestClaimsThatDefaultAgainstThemselves:
    def test_a_summary_that_never_said_it_normalized_identity_does_not_claim_to_have(self) -> None:
        summary = RedactionSummary.model_validate({"total": 3, "counts": {"config_key": 3}})

        assert summary.identity_normalized is False

    def test_a_library_that_never_said_a_worker_was_up_does_not_claim_one_is(self) -> None:
        library = LibraryDiagnostics(name="Painter")

        assert library.worker_ready is None
        assert library.requires_worker is False

    def test_a_workspace_that_was_never_checked_is_not_reported_as_unwritable(self) -> None:
        paths = PathDiagnostics()

        assert paths.workspace_writable is None

    def test_a_log_directory_is_only_named_when_the_engine_writes_somewhere_else(self) -> None:
        logs = LogDiagnostics(log_directory="~/.griptape_nodes/logs")

        assert logs.active_log_directory is None

    def test_a_config_file_that_exists_is_assumed_to_contribute_until_told_otherwise(self) -> None:
        contributing = ConfigFileDiagnostics(path="~/config.json", layer="user", exists=True)
        shadowed = ConfigFileDiagnostics(path="~/config.json", layer="workspace", exists=True, contributes=False)

        assert contributing.contributes is True
        assert shadowed.contributes is False

    def test_a_missing_config_file_is_not_reported_as_broken(self) -> None:
        missing = ConfigFileDiagnostics(path="~/config.json", layer="project", exists=False)

        assert missing.parse_error is None
        assert missing.size_bytes is None


class TestAReportSurvivesSectionsItCouldNotGather:
    def test_only_the_engine_the_host_and_the_timestamp_are_required(self) -> None:
        report = _report()

        assert report.paths == PathDiagnostics()
        assert report.config == ConfigDiagnostics()
        assert report.logs == LogDiagnostics()
        assert report.session == SessionDiagnostics()
        assert report.redaction == RedactionSummary()
        assert report.secrets == []
        assert report.libraries == []
        assert report.projects == []

    def test_an_empty_section_is_told_apart_from_one_that_failed_to_collect(self) -> None:
        report = _report(collection_warnings=["Libraries could not be collected."])

        assert report.libraries == []
        assert report.collection_warnings == ["Libraries could not be collected."]

    def test_every_default_section_is_its_own_instance(self) -> None:
        first = _report()
        second = _report()

        first.paths.missing_paths.append("~/.griptape_nodes/logs")

        assert second.paths.missing_paths == []


class TestReadingAReportBackFromAFile:
    def test_a_report_is_stamped_with_the_schema_version_that_wrote_it(self) -> None:
        report = _report()

        assert report.schema_version == DIAGNOSTICS_REPORT_SCHEMA_VERSION

    def test_a_full_report_round_trips_through_json_unchanged(self) -> None:
        report = _report(
            redaction=RedactionSummary(identity_normalized=True, total=2, counts={"config_key": 2}),
            paths=PathDiagnostics(
                workspace_directory="~/GriptapeNodes",
                missing_paths=["~/GriptapeNodes/logs"],
                workspace_writable=False,
            ),
            config=ConfigDiagnostics(
                files=[ConfigFileDiagnostics(path="~/config.json", layer="user", exists=True, size_bytes=118)],
                runtime_workspace_pin="~/GriptapeNodes",
                environment_overrides=["GTN_CONFIG_LOG_LEVEL"],
                merged={"log_level": "DEBUG"},
            ),
            secrets=[SecretDiagnostics(name="OPENAI_API_KEY", is_set=True, sources=["global .env"])],
            libraries=[LibraryDiagnostics(name="Painter", requires_worker=True, worker_ready=False)],
            projects=[
                ProjectDiagnostics(
                    project_id="proj-1",
                    is_current=True,
                    problems=[
                        ProjectProblemDiagnostics(severity="warning", field_path="situations", message="Unknown key.")
                    ],
                )
            ],
            logs=LogDiagnostics(
                log_level="DEBUG",
                files=[
                    LogFileDiagnostics(
                        name="engine.log", size_bytes=2048, modified_at="2026-09-23T11:59:00Z", is_active=True
                    )
                ],
            ),
            session=SessionDiagnostics(current_workflow_name="my_flow", flow_count=1, node_count=4),
            collection_warnings=["Host disk usage could not be read."],
        )

        assert DiagnosticsReport.model_validate_json(report.model_dump_json()) == report

    def test_a_report_from_a_newer_engine_still_parses(self) -> None:
        payload = {
            "schema_version": "99.0.0",
            "generated_at": "2026-09-23T12:00:00Z",
            "engine": {
                "python_version": "3.14.0",
                "python_executable": "/venv/bin/python",
                "process_id": 42,
                "container_runtime": "podman",
            },
            "host": {
                "system": "Linux",
                "release": "6.9.0",
                "version": "#1 SMP",
                "machine": "x86_64",
            },
            "gpus": [{"name": "unknown to this engine"}],
        }

        report = DiagnosticsReport.model_validate(payload)

        assert report.schema_version == "99.0.0"
        assert report.engine.python_version == "3.14.0"
