"""Tests for the projects section of the diagnostics report.

Almost everything in this section is the user's own writing: a project's name, the paths it
declares, and the messages describing what is wrong with it. All of it goes through the
redactor, which leaves one thing to get right in both directions — a name that is there has
to be redacted, and a name that is absent has to stay absent rather than becoming an error.
The engine's own system defaults template is unnamed, and every collection includes it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import Mock

import pytest

from griptape_nodes.common.diagnostics.redaction import REDACTED, Redactor
from griptape_nodes.common.project_templates import (
    ProjectValidationInfo,
    ProjectValidationProblem,
    ProjectValidationProblemSeverity,
    ProjectValidationStatus,
)
from griptape_nodes.retained_mode.events.project_events import ProjectTemplateInfo
from griptape_nodes.retained_mode.managers.diagnostics_manager import DiagnosticsManager

if TYPE_CHECKING:
    from griptape_nodes.common.diagnostics.report import ProjectDiagnostics
    from griptape_nodes.retained_mode.engine import Engine

_USERNAME = "samantha"


@pytest.fixture
def manager() -> DiagnosticsManager:
    """A manager with a stand-in engine, since building one entry never reaches one."""
    return DiagnosticsManager(Mock(), engine=cast("Engine", Mock()))


def _entry(
    manager: DiagnosticsManager,
    redactor: Redactor,
    *,
    name: str | None = "a project",
    problems: list[ProjectValidationProblem] | None = None,
) -> ProjectDiagnostics:
    info = ProjectTemplateInfo(
        project_id="a-project-id",
        validation=ProjectValidationInfo(
            status=ProjectValidationStatus.GOOD,
            problems=problems if problems is not None else [],
        ),
        name=name,
    )
    return manager._build_project_entry(info, redactor, current_project_id=None, loaded=True)


class TestProjectName:
    def test_a_project_named_after_the_user_does_not_carry_their_name_into_the_report(
        self, manager: DiagnosticsManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A project's name is the user's own words, and often their own name."""
        monkeypatch.setattr("getpass.getuser", lambda: _USERNAME)

        entry = _entry(manager, Redactor(), name=f"{_USERNAME}-experiments")

        assert entry.name is not None
        assert _USERNAME not in entry.name

    def test_a_secret_pasted_into_a_project_name_is_removed(self, manager: DiagnosticsManager) -> None:
        secret = "gtn-test-secret-value-4b1e"  # noqa: S105

        entry = _entry(manager, Redactor(secret_values=[secret]), name=f"staging {secret}")

        assert entry.name == f"staging {REDACTED}"

    def test_a_project_with_no_name_is_reported_as_having_none(self, manager: DiagnosticsManager) -> None:
        """A template whose body could not be read has no name, which is not an empty name.

        The engine's own system defaults entry is one, so this is on every report. Absent has
        to stay absent: an empty string reads as a project someone named nothing, and sends a
        reader looking for a naming bug instead of an unreadable file.
        """
        assert _entry(manager, Redactor(), name=None).name is None


class TestValidationProblems:
    def test_a_problem_message_quoting_a_path_does_not_carry_the_home_directory(
        self, manager: DiagnosticsManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """These messages quote the value that was wrong, which is usually a path someone typed."""
        monkeypatch.setattr("getpass.getuser", lambda: _USERNAME)
        problem = ProjectValidationProblem(
            line_number=12,
            field_path="workspace_dir",
            message=f"'/Users/{_USERNAME}/projects/nowhere' does not exist",
            severity=ProjectValidationProblemSeverity.ERROR,
        )

        entry = _entry(manager, Redactor(), problems=[problem])

        assert _USERNAME not in entry.problems[0].message
        # What was wrong with it survives; only who it belongs to is taken out.
        assert "does not exist" in entry.problems[0].message

    def test_the_field_and_line_a_problem_is_about_are_kept(self, manager: DiagnosticsManager) -> None:
        """Without these the message names a value with nowhere to go and fix it."""
        problem = ProjectValidationProblem(
            line_number=12,
            field_path="workspace_dir",
            message="does not exist",
            severity=ProjectValidationProblemSeverity.ERROR,
        )

        entry = _entry(manager, Redactor(), problems=[problem])

        assert entry.problems[0].field_path == "workspace_dir"
        assert entry.problems[0].line_number == 12  # noqa: PLR2004
