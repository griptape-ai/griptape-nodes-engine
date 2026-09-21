"""Unit tests for sidecar_metadata.py."""

import json
import logging
from pathlib import Path
from unittest.mock import Mock

import pytest

from griptape_nodes.retained_mode.events.project_events import (
    GetCurrentProjectResultFailure,
    GetPathForMacroResultFailure,
    GetSituationResultFailure,
    PathResolutionFailureReason,
)
from griptape_nodes.retained_mode.file_metadata.sidecar_metadata import (
    SidecarContent,
    SituationMetadata,
    SituationPolicy,
    write_sidecar,
)


class TestSidecarContentModel:
    """Tests for SidecarContent and nested model serialization."""

    def test_model_dump_excludes_none_fields(self) -> None:
        content = SidecarContent()
        result = content.model_dump(exclude_none=True)
        assert result == {}

    def test_model_dump_includes_populated_situation(self) -> None:
        content = SidecarContent(
            situation=SituationMetadata(name="save_node_output", macro="{outputs}/file.txt"),
        )
        result = content.model_dump(exclude_none=True)
        assert result["situation"]["name"] == "save_node_output"
        assert result["situation"]["macro"] == "{outputs}/file.txt"
        assert "policy" not in result["situation"]
        assert "variables" not in result["situation"]

    def test_situation_policy_excludes_none(self) -> None:
        content = SidecarContent(
            situation=SituationMetadata(
                policy=SituationPolicy(create_dirs=True),
            ),
        )
        result = content.model_dump(exclude_none=True)
        assert result["situation"]["policy"]["create_dirs"] is True
        assert "on_collision" not in result["situation"]["policy"]


class TestWriteSidecarFailurePaths:
    """Tests that write_sidecar handles failures gracefully (best-effort)."""

    def test_no_project_loaded_logs_warning_and_does_not_raise(self, caplog: pytest.LogCaptureFixture) -> None:
        """write_sidecar logs a warning and swallows the exception when no project is loaded."""
        file_path = Path("/workspace/output.txt")
        metadata = SidecarContent()
        mock_engine = Mock()
        mock_engine.handle_request.return_value = GetCurrentProjectResultFailure(result_details="no project")

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            write_sidecar(file_path, metadata, mock_engine)

        assert "Failed to write sidecar metadata" in caplog.text
        assert "output.txt" in caplog.text

    def test_situation_not_found_logs_warning_and_does_not_raise(self, caplog: pytest.LogCaptureFixture) -> None:
        """write_sidecar logs a warning and swallows the exception when situation is missing."""
        from griptape_nodes.retained_mode.events.project_events import (
            GetCurrentProjectResultSuccess,
        )

        mock_project_info = Mock()
        mock_project_info.project_base_dir = Path("/workspace")
        file_path = Path("/workspace/output.txt")
        metadata = SidecarContent()

        def handle_request(request: object) -> object:
            from griptape_nodes.retained_mode.events.project_events import (
                GetCurrentProjectRequest,
                GetSituationRequest,
            )

            if isinstance(request, GetCurrentProjectRequest):
                return GetCurrentProjectResultSuccess(project_info=mock_project_info, result_details="ok")
            if isinstance(request, GetSituationRequest):
                return GetSituationResultFailure(result_details="not found")
            msg = f"Unexpected request: {request}"
            raise AssertionError(msg)

        mock_engine = Mock()
        mock_engine.handle_request.side_effect = handle_request

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            write_sidecar(file_path, metadata, mock_engine)

        assert "Failed to write sidecar metadata" in caplog.text

    def test_path_resolution_failure_logs_warning_and_does_not_raise(self, caplog: pytest.LogCaptureFixture) -> None:
        """write_sidecar logs a warning when the sidecar path macro cannot be resolved."""
        from griptape_nodes.common.project_templates.situation import (
            SituationFilePolicy,
            SituationTemplate,
        )
        from griptape_nodes.common.project_templates.situation import (
            SituationPolicy as SitPolicy,
        )
        from griptape_nodes.retained_mode.events.project_events import (
            GetCurrentProjectResultSuccess,
            GetSituationResultSuccess,
        )

        mock_project_info = Mock()
        mock_project_info.project_base_dir = Path("/workspace")
        situation = SituationTemplate(
            name="save_griptape_nodes_metadata",
            macro="{griptape-nodes-metadata}/{source_file_name}.json",
            policy=SitPolicy(on_collision=SituationFilePolicy.OVERWRITE, create_dirs=True),
        )
        file_path = Path("/workspace/output.txt")
        metadata = SidecarContent()

        def handle_request(request: object) -> object:
            from griptape_nodes.retained_mode.events.project_events import (
                GetCurrentProjectRequest,
                GetPathForMacroRequest,
                GetSituationRequest,
            )

            if isinstance(request, GetCurrentProjectRequest):
                return GetCurrentProjectResultSuccess(project_info=mock_project_info, result_details="ok")
            if isinstance(request, GetSituationRequest):
                return GetSituationResultSuccess(situation=situation, result_details="ok")
            if isinstance(request, GetPathForMacroRequest):
                return GetPathForMacroResultFailure(
                    result_details="missing variables",
                    failure_reason=PathResolutionFailureReason.MISSING_REQUIRED_VARIABLES,
                    missing_variables={"griptape-nodes-metadata"},
                )
            msg = f"Unexpected request: {request}"
            raise AssertionError(msg)

        mock_engine = Mock()
        mock_engine.handle_request.side_effect = handle_request

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            write_sidecar(file_path, metadata, mock_engine)

        assert "Failed to write sidecar metadata" in caplog.text

    def test_none_metadata_writes_empty_sidecar(self, tmp_path: Path) -> None:
        """write_sidecar with None metadata writes an empty SidecarContent."""
        from griptape_nodes.common.project_templates.situation import (
            SituationFilePolicy,
            SituationTemplate,
        )
        from griptape_nodes.common.project_templates.situation import (
            SituationPolicy as SitPolicy,
        )
        from griptape_nodes.retained_mode.events.project_events import (
            GetCurrentProjectResultSuccess,
            GetPathForMacroResultSuccess,
            GetSituationResultSuccess,
        )

        sidecar_path = tmp_path / ".griptape-nodes-metadata" / "output.txt.json"
        mock_project_info = Mock()
        mock_project_info.project_base_dir = tmp_path
        situation = SituationTemplate(
            name="save_griptape_nodes_metadata",
            macro="{griptape-nodes-metadata}/{source_file_name}.json",
            policy=SitPolicy(on_collision=SituationFilePolicy.OVERWRITE, create_dirs=True),
        )
        file_path = tmp_path / "output.txt"

        def handle_request(request: object) -> object:
            from griptape_nodes.retained_mode.events.project_events import (
                GetCurrentProjectRequest,
                GetPathForMacroRequest,
                GetSituationRequest,
            )

            if isinstance(request, GetCurrentProjectRequest):
                return GetCurrentProjectResultSuccess(project_info=mock_project_info, result_details="ok")
            if isinstance(request, GetSituationRequest):
                return GetSituationResultSuccess(situation=situation, result_details="ok")
            if isinstance(request, GetPathForMacroRequest):
                return GetPathForMacroResultSuccess(
                    resolved_path=sidecar_path,
                    absolute_path=sidecar_path,
                    result_details="ok",
                )
            msg = f"Unexpected request: {request}"
            raise AssertionError(msg)

        mock_engine = Mock()
        mock_engine.handle_request.side_effect = handle_request
        mock_engine.context_manager.has_current_workflow.return_value = False

        write_sidecar(file_path, None, mock_engine)

        assert sidecar_path.exists()
        data = json.loads(sidecar_path.read_text())
        assert data["schema_version"] == "0.2.0"
        assert "saved_at" in data
        # No situation field because metadata was empty SidecarContent()
        assert "situation" not in data
