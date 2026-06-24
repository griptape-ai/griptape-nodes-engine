"""Tests for enhanced WriteFileRequest functionality.

Tests cover:
- CREATE_NEW with WARNING-level ResultDetails when falling back to indexed filename
- CREATE_NEW first-try success without fallback warning
- Blanket exception handling for unexpected errors
- Match/case error message formatting for parent directory failures
- On-demand candidate generation (doesn't pre-generate all MAX_INDEXED_CANDIDATES)
- Extension coercion: rename suffix to match sniffed bytes, or fail when strict
"""

import logging
import tempfile
from collections.abc import Generator
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from griptape_nodes.common.macro_parser import ParsedMacro
from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
from griptape_nodes.common.project_templates.situation import SituationFilePolicy
from griptape_nodes.retained_mode.events.artifact_events import RegisterArtifactProviderRequest
from griptape_nodes.retained_mode.events.base_events import ResultDetails
from griptape_nodes.retained_mode.events.os_events import (
    ExistingFilePolicy,
    FileIOFailureReason,
    WriteFileRequest,
    WriteFileResultFailure,
    WriteFileResultSuccess,
)
from griptape_nodes.retained_mode.events.project_events import (
    LoadProjectTemplateRequest,
    LoadProjectTemplateResultSuccess,
    SetCurrentProjectRequest,
)
from griptape_nodes.retained_mode.file_metadata.sidecar_metadata import (
    SidecarContent,
    SituationMetadata,
    SituationPolicy,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider import (
    ImageArtifactProvider,
)


class TestCreateNewWarningLevelResultDetails:
    """Test CREATE_NEW policy with WARNING-level ResultDetails on fallback."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture(autouse=True)
    def setup_workspace(self, temp_dir: Path, griptape_nodes: GriptapeNodes) -> Generator[None, None, None]:
        """Automatically set workspace to temp_dir for all tests."""
        original_workspace = griptape_nodes.ConfigManager().workspace_path
        griptape_nodes.ConfigManager().workspace_path = temp_dir
        yield
        griptape_nodes.ConfigManager().workspace_path = original_workspace

    def test_create_new_fallback_warning_level(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test CREATE_NEW returns WARNING-level ResultDetails when falling back to indexed path."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "test.txt"

        # Create the originally requested file so CREATE_NEW must fall back
        file_path.write_text("Original file")

        request = WriteFileRequest(
            file_path=str(file_path),
            content="New content",
            existing_file_policy=ExistingFilePolicy.CREATE_NEW,
        )

        result = os_manager.on_write_file_request(request)

        # Should succeed but with indexed filename
        assert isinstance(result, WriteFileResultSuccess)
        assert result.final_file_path != str(file_path)  # Different from requested
        assert (temp_dir / "test_1.txt").exists()

        # Check ResultDetails is DEBUG level
        assert isinstance(result.result_details, ResultDetails)
        assert len(result.result_details.result_details) == 1
        detail = result.result_details.result_details[0]
        assert detail.level == logging.DEBUG
        assert "indexed path" in detail.message.lower()
        assert "already existed" in detail.message.lower()
        assert str(file_path) in detail.message or "test.txt" in detail.message

    def test_create_new_first_try_no_warning(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test CREATE_NEW returns normal success (not WARNING) when first-try succeeds."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "newfile.txt"

        # Don't create the file - let CREATE_NEW succeed on first try
        request = WriteFileRequest(
            file_path=str(file_path),
            content="Content",
            existing_file_policy=ExistingFilePolicy.CREATE_NEW,
        )

        result = os_manager.on_write_file_request(request)

        # Should succeed with requested filename
        assert isinstance(result, WriteFileResultSuccess)
        assert Path(result.final_file_path).resolve() == file_path.resolve()

        # result_details should be DEBUG level (not WARNING)
        assert isinstance(result.result_details, ResultDetails)
        assert len(result.result_details.result_details) == 1
        detail = result.result_details.result_details[0]
        assert detail.level == logging.DEBUG  # Normal success is DEBUG, not WARNING
        assert "successfully" in detail.message.lower()

    def test_create_new_fallback_multiple_times(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test CREATE_NEW generates multiple DEBUG messages for multiple fallbacks."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "output.txt"

        # Create original file
        file_path.write_text("Original")

        # First fallback - should get test_1.txt with DEBUG
        request1 = WriteFileRequest(
            file_path=str(file_path),
            content="Content 1",
            existing_file_policy=ExistingFilePolicy.CREATE_NEW,
        )
        result1 = os_manager.on_write_file_request(request1)
        assert isinstance(result1, WriteFileResultSuccess)
        assert isinstance(result1.result_details, ResultDetails)
        assert result1.result_details.result_details[0].level == logging.DEBUG

        # Second fallback - should get test_2.txt with DEBUG
        request2 = WriteFileRequest(
            file_path=str(file_path),
            content="Content 2",
            existing_file_policy=ExistingFilePolicy.CREATE_NEW,
        )
        result2 = os_manager.on_write_file_request(request2)
        assert isinstance(result2, WriteFileResultSuccess)
        assert isinstance(result2.result_details, ResultDetails)
        assert result2.result_details.result_details[0].level == logging.DEBUG


class TestBlanketExceptionHandling:
    """Test blanket exception handlers catch unexpected errors."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture(autouse=True)
    def setup_workspace(self, temp_dir: Path, griptape_nodes: GriptapeNodes) -> Generator[None, None, None]:
        """Automatically set workspace to temp_dir for all tests."""
        original_workspace = griptape_nodes.ConfigManager().workspace_path
        griptape_nodes.ConfigManager().workspace_path = temp_dir
        yield
        griptape_nodes.ConfigManager().workspace_path = original_workspace

    def test_blanket_exception_on_path_resolution(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test blanket exception handler for unexpected error during path resolution."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "test.txt"

        request = WriteFileRequest(file_path=str(file_path), content="Content")

        # Mock _resolve_file_path to raise unexpected exception (not ValueError/RuntimeError)
        with patch.object(os_manager, "_resolve_file_path", side_effect=TypeError("Unexpected type error")):
            result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.IO_ERROR
        assert isinstance(result.result_details, ResultDetails)
        assert "unexpected error" in result.result_details.result_details[0].message.lower()

    def test_blanket_exception_on_write_operation(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test blanket exception handler for unexpected error during write."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "test.txt"

        request = WriteFileRequest(file_path=str(file_path), content="Content")

        # Mock _write_with_portalocker to raise unexpected exception (not FileExistsError or LockException)
        with patch.object(os_manager, "_write_with_portalocker", side_effect=OSError("Unexpected I/O error")):
            result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.IO_ERROR
        assert isinstance(result.result_details, ResultDetails)
        assert "unexpected error" in result.result_details.result_details[0].message.lower()

    def test_blanket_exception_on_macro_resolution_in_candidate_loop(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Test blanket exception handler for unexpected error during CREATE_NEW macro resolution."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "output.txt"

        # Create original file to trigger CREATE_NEW fallback
        file_path.write_text("Original")

        request = WriteFileRequest(
            file_path=str(file_path),
            content="Content",
            existing_file_policy=ExistingFilePolicy.CREATE_NEW,
        )

        # Mock ParsedMacro.resolve to raise unexpected exception during candidate generation

        def mock_resolve(_self: ParsedMacro, *_args: object, **_kwargs: object) -> str:
            # Raise unexpected exception on first call (during candidate generation)
            msg = "Unexpected type error in macro resolution"
            raise TypeError(msg)

        with patch.object(ParsedMacro, "resolve", mock_resolve):
            result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.IO_ERROR
        assert isinstance(result.result_details, ResultDetails)
        assert "unexpected error" in result.result_details.result_details[0].message.lower()


class TestParentDirectoryMatchCase:
    """Test match/case error messages for parent directory failures."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture(autouse=True)
    def setup_workspace(self, temp_dir: Path, griptape_nodes: GriptapeNodes) -> Generator[None, None, None]:
        """Automatically set workspace to temp_dir for all tests."""
        original_workspace = griptape_nodes.ConfigManager().workspace_path
        griptape_nodes.ConfigManager().workspace_path = temp_dir
        yield
        griptape_nodes.ConfigManager().workspace_path = original_workspace

    def test_parent_directory_permission_denied_message(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test match/case generates correct message for PERMISSION_DENIED."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "subdir" / "test.txt"

        request = WriteFileRequest(file_path=str(file_path), content="Content", create_parents=True)

        # Mock _ensure_parent_directory_ready to return PERMISSION_DENIED
        with patch.object(
            os_manager, "_ensure_parent_directory_ready", return_value=FileIOFailureReason.PERMISSION_DENIED
        ):
            result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.PERMISSION_DENIED
        assert isinstance(result.result_details, ResultDetails)
        message = result.result_details.result_details[0].message
        assert "permission denied" in message.lower()
        assert "parent directory" in message.lower()

    def test_parent_directory_no_create_message(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test match/case generates correct message for POLICY_NO_CREATE_PARENT_DIRS."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "nonexistent" / "test.txt"

        request = WriteFileRequest(file_path=str(file_path), content="Content", create_parents=False)

        result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.POLICY_NO_CREATE_PARENT_DIRS
        assert isinstance(result.result_details, ResultDetails)
        message = result.result_details.result_details[0].message
        # Message includes "parent directory does not exist" or similar phrasing
        assert "parent directory" in message.lower()
        assert "not exist" in message.lower() or "does not exist" in message.lower()

    def test_parent_directory_generic_io_error_message(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test match/case default case generates generic error message."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "subdir" / "test.txt"

        request = WriteFileRequest(file_path=str(file_path), content="Content", create_parents=True)

        # Mock _ensure_parent_directory_ready to return IO_ERROR (not permission or policy)
        with patch.object(os_manager, "_ensure_parent_directory_ready", return_value=FileIOFailureReason.IO_ERROR):
            result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.IO_ERROR
        assert isinstance(result.result_details, ResultDetails)
        message = result.result_details.result_details[0].message
        assert "error creating parent directory" in message.lower()


class TestOnDemandCandidateGeneration:
    """Test that CREATE_NEW generates candidates on-demand, not all upfront."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture(autouse=True)
    def setup_workspace(self, temp_dir: Path, griptape_nodes: GriptapeNodes) -> Generator[None, None, None]:
        """Automatically set workspace to temp_dir for all tests."""
        original_workspace = griptape_nodes.ConfigManager().workspace_path
        griptape_nodes.ConfigManager().workspace_path = temp_dir
        yield
        griptape_nodes.ConfigManager().workspace_path = original_workspace

    def test_on_demand_generation_early_success(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test CREATE_NEW only resolves macros until it finds available filename."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "output.txt"

        # CREATE_NEW always tries the first-try path first (without index)
        # So create files output.txt, output_1.txt, output_2.txt, output_3.txt, output_4.txt
        # Leave output_5.txt available
        file_path.write_text("Original")  # output.txt
        for i in range(1, 5):
            (temp_dir / f"output_{i}.txt").write_text(f"File {i}")

        request = WriteFileRequest(
            file_path=str(file_path),
            content="File 5",
            existing_file_policy=ExistingFilePolicy.CREATE_NEW,
        )

        # Track how many times macro resolution is called
        resolve_call_count = 0
        original_resolve = ParsedMacro.resolve

        def counting_resolve(self: ParsedMacro, *args: object, **kwargs: object) -> str:  # type: ignore[misc]
            nonlocal resolve_call_count
            resolve_call_count += 1
            return original_resolve(self, *args, **kwargs)  # type: ignore[arg-type]

        with patch.object(ParsedMacro, "resolve", counting_resolve):
            result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultSuccess)
        assert (temp_dir / "output_5.txt").exists()

        # Should only resolve macros for candidates we actually need to try, not all 1000
        # (May include scanning calls, but definitely not 1000)
        max_expected_resolutions = 100
        min_expected_resolutions = 1
        assert resolve_call_count < max_expected_resolutions, (
            f"Expected < {max_expected_resolutions} macro resolutions, got {resolve_call_count}"
        )
        assert resolve_call_count >= min_expected_resolutions, (
            f"Expected >= {min_expected_resolutions} macro resolution, got {resolve_call_count}"
        )

    def test_on_demand_generation_stops_on_success(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test CREATE_NEW stops generating candidates immediately after successful write."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "test.txt"

        # Create original file to trigger fallback
        file_path.write_text("Original")

        request = WriteFileRequest(
            file_path=str(file_path),
            content="New content",
            existing_file_policy=ExistingFilePolicy.CREATE_NEW,
        )

        # Track _write_with_portalocker calls
        write_attempts = []
        original_write = os_manager._write_with_portalocker

        def tracking_write(*args: object, **kwargs: object) -> int:  # type: ignore[misc]
            write_attempts.append(args[0] if args else None)  # Track the path
            return original_write(*args, **kwargs)  # type: ignore[arg-type]

        with patch.object(os_manager, "_write_with_portalocker", tracking_write):
            result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultSuccess)

        # Should only have 2 write attempts: original path (fails), then test_1.txt (succeeds)
        expected_attempts = 2
        assert len(write_attempts) == expected_attempts, (
            f"Expected {expected_attempts} write attempts, got {len(write_attempts)}"
        )


class TestMetadataInjection:
    """Test workflow metadata injection in WriteFileRequest handler."""

    INJECT_PATH = "griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider.collect_workflow_metadata"

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture(autouse=True)
    def setup_workspace(self, temp_dir: Path, griptape_nodes: GriptapeNodes) -> Generator[None, None, None]:
        """Automatically set workspace to temp_dir for all tests."""
        original_workspace = griptape_nodes.ConfigManager().workspace_path
        griptape_nodes.ConfigManager().workspace_path = temp_dir
        yield
        griptape_nodes.ConfigManager().workspace_path = original_workspace

    def test_metadata_injected_for_bytes_content(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test that prepare_content_for_write is called for bytes content."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "image.png"
        original_bytes = b"fake png bytes"
        injected_bytes = b"fake png bytes with metadata"

        request = WriteFileRequest(file_path=str(file_path), content=original_bytes)

        with patch.object(
            griptape_nodes.ArtifactManager(), "prepare_content_for_write", return_value=injected_bytes
        ) as mock_prepare:
            result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultSuccess)
        mock_prepare.assert_called_once_with(original_bytes, "image.png")

    def test_metadata_not_injected_when_skip_flag_set(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test that injection is skipped when skip_metadata_injection=True."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "image.png"

        request = WriteFileRequest(
            file_path=str(file_path),
            content=b"fake png bytes",
            skip_metadata_injection=True,
        )

        with patch.object(griptape_nodes.ArtifactManager(), "prepare_content_for_write") as mock_prepare:
            result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultSuccess)
        mock_prepare.assert_not_called()

    def test_metadata_not_injected_when_config_disabled(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test that injection is skipped when auto_inject_workflow_metadata config is False."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "image.png"

        request = WriteFileRequest(file_path=str(file_path), content=b"fake png bytes")

        with (
            patch.object(
                griptape_nodes.ConfigManager(),
                "get_config_value",
                side_effect=lambda key, **kwargs: (
                    False if key == "auto_inject_workflow_metadata" else kwargs.get("default")
                ),
            ),
            patch.object(griptape_nodes.ArtifactManager(), "prepare_content_for_write") as mock_prepare,
        ):
            result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultSuccess)
        mock_prepare.assert_not_called()

    def test_metadata_not_injected_for_str_content(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test that injection is skipped for str content (only bytes are images)."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "text.txt"

        request = WriteFileRequest(file_path=str(file_path), content="text content")

        with patch.object(griptape_nodes.ArtifactManager(), "prepare_content_for_write") as mock_prepare:
            result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultSuccess)
        mock_prepare.assert_not_called()

    def test_injection_failure_logs_warning_and_write_succeeds(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that injection failure logs a warning and the write succeeds with original content."""
        griptape_nodes.ArtifactManager().on_handle_register_artifact_provider_request(
            RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider)
        )
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "image.png"
        original_bytes = b"fake png bytes"

        request = WriteFileRequest(file_path=str(file_path), content=original_bytes)

        with patch(self.INJECT_PATH, side_effect=RuntimeError("injection failed")), caplog.at_level(logging.WARNING):
            result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultSuccess)
        assert file_path.read_bytes() == original_bytes
        assert any("Attempted to collect workflow metadata" in record.message for record in caplog.records)

    def test_injected_content_is_written_to_disk(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test that the injected (modified) content is what gets written to disk."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "image.png"
        original_bytes = b"fake png bytes"
        injected_bytes = b"fake png bytes with metadata injected"

        request = WriteFileRequest(file_path=str(file_path), content=original_bytes)

        with patch.object(griptape_nodes.ArtifactManager(), "prepare_content_for_write", return_value=injected_bytes):
            result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultSuccess)
        assert file_path.read_bytes() == injected_bytes


class TestSidecarMetadata:
    """Test sidecar metadata file creation alongside file writes."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Resolve so the path matches what ConfigManager.workspace_path and
            # the canonicalized project_base_dir store. On Windows, tempfile
            # returns the 8.3 short form (C:\Users\RUNNER~1\...); both sides
            # must agree for decompose_source_path's relative_to() check to hold.
            yield Path(tmpdir).resolve()

    @pytest.fixture(autouse=True)
    def setup_workspace(self, temp_dir: Path, griptape_nodes: GriptapeNodes) -> Generator[None, None, None]:
        """Set workspace to temp_dir and load a project template for sidecar path resolution."""
        config_manager = griptape_nodes.ConfigManager()
        original_workspace = config_manager.workspace_path
        # Set the *configured* workspace_directory, not just the workspace_path property.
        # The project loaded below is parentless, so activation resolves its workspace via
        # decide_workspace's global-default branch (the configured workspace_directory) and
        # pins it with set_workspace_override. A bare workspace_path assignment would be
        # clobbered by that pin; setting the configured value makes the pin land on temp_dir.
        config_manager.set_config_value("workspace_directory", str(temp_dir))

        # Create a project template file so sidecar path resolution has a project to use
        project_yml = temp_dir / "project_template.yml"
        project_yml.write_text(DEFAULT_PROJECT_TEMPLATE.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE))
        load_result = GriptapeNodes.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
        if isinstance(load_result, LoadProjectTemplateResultSuccess):
            GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=load_result.project_id))

        yield

        GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=None))
        config_manager.set_config_value("workspace_directory", str(original_workspace))

    def test_sidecar_not_written_without_file_metadata(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test that no sidecar is written when file_metadata is not provided."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "output.txt"

        request = WriteFileRequest(file_path=str(file_path), content="hello")
        result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultSuccess)
        sidecar_path = temp_dir / ".griptape-nodes-metadata" / "output.txt.json"
        assert not sidecar_path.exists(), "Sidecar should not be written when file_metadata is not provided"

    def test_sidecar_written_when_file_metadata_provided(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test that a sidecar is written when file_metadata is explicitly provided."""
        import json as _json

        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "output.txt"
        file_metadata = SidecarContent(
            situation=SituationMetadata(name="save_node_output", macro="{outputs}/output.txt"),
        )

        request = WriteFileRequest(file_path=str(file_path), content="hello", file_metadata=file_metadata)
        result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultSuccess)
        sidecar_path = temp_dir / ".griptape-nodes-metadata" / "output.txt.json"
        assert sidecar_path.exists()
        data = _json.loads(sidecar_path.read_text())
        assert data["schema_version"] == "0.1.0"
        assert "saved_at" in data

    def test_sidecar_contains_situation_info_when_provided(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test sidecar includes situation block when file_metadata has situation info."""
        import json as _json

        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "image.png"
        file_metadata = SidecarContent(
            situation=SituationMetadata(
                name="save_node_output",
                macro="{outputs}/{node_name}.png",
                policy=SituationPolicy(on_collision=SituationFilePolicy.CREATE_NEW, create_dirs=True),
            ),
        )

        request = WriteFileRequest(file_path=str(file_path), content=b"", file_metadata=file_metadata)
        result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultSuccess)
        sidecar_path = temp_dir / ".griptape-nodes-metadata" / "image.png.json"
        data = _json.loads(sidecar_path.read_text())
        assert data["situation"]["name"] == "save_node_output"
        assert data["situation"]["macro"] == "{outputs}/{node_name}.png"
        assert data["situation"]["policy"]["on_collision"] == "create_new"
        assert data["situation"]["policy"]["create_dirs"] is True

    def test_sidecar_written_for_indexed_fallback_path(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test sidecar is written at the actual indexed fallback path, not the requested path."""
        import json as _json

        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "output.txt"
        file_path.write_text("Original")
        file_metadata = SidecarContent(
            situation=SituationMetadata(name="save_node_output", macro="{outputs}/output.txt"),
        )

        request = WriteFileRequest(
            file_path=str(file_path),
            content="New content",
            existing_file_policy=ExistingFilePolicy.CREATE_NEW,
            file_metadata=file_metadata,
        )
        result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultSuccess)
        actual_path = Path(result.final_file_path)
        sidecar_path = actual_path.parent / ".griptape-nodes-metadata" / (actual_path.name + ".json")
        assert sidecar_path.exists(), "Sidecar should be created at the actual indexed fallback path"
        data = _json.loads(sidecar_path.read_text())
        assert data["schema_version"] == "0.1.0"


def _png_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (1, 1), color="white").save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (1, 1), color="white").save(buf, format="JPEG")
    return buf.getvalue()


class TestExtensionCoercion:
    """Test extension coercion (rename suffix to match sniffed bytes) inside OSManager."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir).resolve()

    @pytest.fixture(autouse=True)
    def setup_workspace(self, temp_dir: Path, griptape_nodes: GriptapeNodes) -> Generator[None, None, None]:
        """Set workspace and register the image artifact provider so sniff_extension works."""
        original_workspace = griptape_nodes.ConfigManager().workspace_path
        griptape_nodes.ConfigManager().workspace_path = temp_dir

        griptape_nodes.ArtifactManager().on_handle_register_artifact_provider_request(
            RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider)
        )

        yield

        griptape_nodes.ConfigManager().workspace_path = original_workspace

    def test_default_coerce_renames_jpeg_when_destination_is_png(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """JPEG bytes saved to a .png path should be renamed to .jpeg by default."""
        os_manager = griptape_nodes.OSManager()
        requested_path = temp_dir / "image.png"

        request = WriteFileRequest(file_path=str(requested_path), content=_jpeg_bytes())
        with caplog.at_level(logging.WARNING):
            result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultSuccess)
        coerced_path = temp_dir / "image.jpg"
        assert Path(result.final_file_path) == coerced_path
        assert coerced_path.exists()
        assert not requested_path.exists()
        assert any("Coerced file extension" in r.message for r in caplog.records)

    def test_strict_mode_returns_extension_mismatch_failure(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """With coerce_extension_to_match_bytes=False, mismatched bytes should fail and leave no file."""
        os_manager = griptape_nodes.OSManager()
        requested_path = temp_dir / "image.png"

        request = WriteFileRequest(
            file_path=str(requested_path),
            content=_jpeg_bytes(),
            coerce_extension_to_match_bytes=False,
        )
        result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.EXTENSION_MISMATCH
        assert not requested_path.exists()
        assert not (temp_dir / "image.jpg").exists()

    def test_coerce_no_op_when_bytes_match_suffix(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """No rename, no warning when bytes already match the destination suffix."""
        os_manager = griptape_nodes.OSManager()
        requested_path = temp_dir / "image.png"

        request = WriteFileRequest(file_path=str(requested_path), content=_png_bytes())
        result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultSuccess)
        assert Path(result.final_file_path) == requested_path
        assert requested_path.exists()

    def test_coerce_no_op_when_sniff_returns_none(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unrecognized bytes log a 'Could not identify' warning and write through unchanged."""
        os_manager = griptape_nodes.OSManager()
        requested_path = temp_dir / "blob.png"

        request = WriteFileRequest(file_path=str(requested_path), content=b"\x00\x01\x02\x03 not a known format")
        with caplog.at_level(logging.WARNING):
            result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultSuccess)
        assert Path(result.final_file_path) == requested_path
        assert any("Could not identify byte content" in r.message for r in caplog.records)

    def test_coerce_updates_sidecar_file_extension_variable(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """When coercion fires, the sidecar's situation.variables.file_extension is rewritten to match."""
        os_manager = griptape_nodes.OSManager()
        requested_path = temp_dir / "image.png"

        file_metadata = SidecarContent(
            situation=SituationMetadata(
                name="save_node_output",
                macro="{outputs}/{file_name_base}.{file_extension}",
                variables={"file_name_base": "image", "file_extension": "png"},
            ),
        )

        request = WriteFileRequest(
            file_path=str(requested_path),
            content=_jpeg_bytes(),
            file_metadata=file_metadata,
        )
        result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultSuccess)
        # The mutation happens before write_sidecar runs, so the in-memory metadata
        # passed in should be updated. Verify by checking the request's metadata.
        assert request.file_metadata is not None
        assert request.file_metadata.situation is not None
        assert request.file_metadata.situation.variables is not None
        assert request.file_metadata.situation.variables["file_extension"] == "jpg"

    def test_coerce_skipped_for_text_content(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Text writes should never trigger sniffing/rename."""
        os_manager = griptape_nodes.OSManager()
        requested_path = temp_dir / "notes.png"

        request = WriteFileRequest(file_path=str(requested_path), content="just some text")
        result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultSuccess)
        assert Path(result.final_file_path) == requested_path
