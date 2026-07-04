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
import sys
import tempfile
import unicodedata
from collections.abc import Generator
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from griptape_nodes.common.macro_parser import ParsedMacro
from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
from griptape_nodes.common.project_templates.situation import SituationFilePolicy
from griptape_nodes.files.file import File, FileDestination, FileLoadError, FileWriteError
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
    MacroPath,
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

# Mirrors the constant in tests/unit/retained_mode/managers/test_os_manager.py.
# Used by the long-path stress test below so the magic 260 doesn't bare-appear.
WINDOWS_MAX_PATH = 260


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
        assert any("destination has been adjusted" in r.message for r in caplog.records)

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
        assert any("Could not recognize the bytes as a known file format" in r.message for r in caplog.records)

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


class TestOSWritePermissionVet:
    """Provider write-permission vet runs on every recognized-bytes write.

    After sniff picks a format, OSManager asks the format's provider whether
    the write is permitted. A denial produces WriteFileResultFailure(
    CODEC_NOT_PERMITTED) BEFORE any bytes reach disk. Format sniffing itself
    is unchanged -- only the vet is new. Unregistered / unrecognized bytes
    bypass the vet entirely (there's no provider to ask).
    """

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir).resolve()

    @pytest.fixture(autouse=True)
    def setup_workspace(self, temp_dir: Path, griptape_nodes: GriptapeNodes) -> Generator[None, None, None]:
        # Register the image provider so PNG/JPEG bytes are recognized; the
        # vet under test is provider.check_write_permission on the returned
        # provider instance.
        original_workspace = griptape_nodes.ConfigManager().workspace_path
        griptape_nodes.ConfigManager().workspace_path = temp_dir

        griptape_nodes.ArtifactManager().on_handle_register_artifact_provider_request(
            RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider)
        )

        yield

        griptape_nodes.ConfigManager().workspace_path = original_workspace

    def test_deny_returns_codec_not_permitted_and_no_file(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A provider that denies must abort the write before any bytes hit disk."""
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure

        expected_denial = CheckpointDenial(failures=(CheckpointFailure(detail="This codec is not licensed."),))

        # Force the ImageArtifactProvider instance's check_write_permission to
        # deny. We patch on the instance retrieved through the same lookup path
        # the manager uses so we intercept the exact object it will call.
        provider_classes = griptape_nodes.ArtifactManager()._registry.get_provider_classes_by_format("png")
        assert provider_classes, "PNG must resolve to the registered image provider"
        provider = griptape_nodes.ArtifactManager()._registry.get_or_create_provider_instance(provider_classes[0])
        monkeypatch.setattr(
            provider,
            "check_write_permission",
            lambda data, detected_format, *, file_name, caller_variables=None: expected_denial,  # noqa: ARG005
        )

        os_manager = griptape_nodes.OSManager()
        requested_path = temp_dir / "image.png"
        request = WriteFileRequest(file_path=str(requested_path), content=_png_bytes())

        result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.CODEC_NOT_PERMITTED
        # The user-facing message must include the denial's own reason string
        # (owned by the policy) so an artist sees plain English, not "sniffed".
        assert "This codec is not licensed." in str(result.result_details)
        assert "sniffed" not in str(result.result_details).lower()
        assert not requested_path.exists()

    def test_allow_writes_normally(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """The default BaseArtifactProvider.check_write_permission returns None -> allow."""
        os_manager = griptape_nodes.OSManager()
        requested_path = temp_dir / "image.png"

        request = WriteFileRequest(file_path=str(requested_path), content=_png_bytes())
        result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultSuccess)
        assert Path(result.final_file_path) == requested_path
        assert requested_path.exists()

    def test_unrecognized_bytes_skip_vet_entirely(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If sniff returns None (no provider claims the bytes), the vet is never invoked."""
        called: list[bool] = []

        def spy(data: bytes, detected_format: str, *, file_name: str, caller_variables: object = None) -> None:  # noqa: ARG001
            called.append(True)

        # Attach the spy to any provider instance we might reach; if the vet
        # were ever invoked for unrecognized bytes we'd catch it here. Simpler:
        # spy on ArtifactManager.check_write_permission at the dispatch layer.
        monkeypatch.setattr(griptape_nodes.ArtifactManager(), "check_write_permission", spy)

        os_manager = griptape_nodes.OSManager()
        requested_path = temp_dir / "blob.dat"

        request = WriteFileRequest(file_path=str(requested_path), content=b"\x00\x01 not a known format")
        result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultSuccess)
        assert called == []
        assert requested_path.exists()

    def test_deny_returns_codec_not_permitted_and_no_file_for_extensionless_destination(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: extension-less destinations must not bypass the codec vet.

        Before the fix, ``_apply_extension_coercion`` returned early when the
        destination had no suffix and the vet inside it never fired, letting
        gated bytes reach disk simply by omitting the extension. The vet now
        lives in ``on_write_file_request`` and runs BEFORE coercion, so an
        extensionless destination is protected.
        """
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure

        expected_denial = CheckpointDenial(failures=(CheckpointFailure(detail="This codec is not licensed."),))

        provider_classes = griptape_nodes.ArtifactManager()._registry.get_provider_classes_by_format("png")
        assert provider_classes, "PNG must resolve to the registered image provider"
        provider = griptape_nodes.ArtifactManager()._registry.get_or_create_provider_instance(provider_classes[0])
        monkeypatch.setattr(
            provider,
            "check_write_permission",
            lambda data, detected_format, *, file_name, caller_variables=None: expected_denial,  # noqa: ARG005
        )

        os_manager = griptape_nodes.OSManager()
        # Destination has NO extension. Sniff on bytes classifies as PNG,
        # provider denies, OSManager must refuse the write. Before finding 1
        # this would return Success and leave the file on disk.
        requested_path = temp_dir / "movie"
        request = WriteFileRequest(file_path=str(requested_path), content=_png_bytes())

        result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.CODEC_NOT_PERMITTED
        assert "This codec is not licensed." in str(result.result_details)
        assert not requested_path.exists()

    def test_append_writes_are_not_vetted(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Appends skip the codec vet: the tail alone has no container header to classify."""
        called: list[bool] = []

        def spy(data: bytes, detected_format: str, *, file_name: str, caller_variables: object = None) -> None:  # noqa: ARG001
            called.append(True)

        monkeypatch.setattr(griptape_nodes.ArtifactManager(), "check_write_permission", spy)

        os_manager = griptape_nodes.OSManager()
        requested_path = temp_dir / "image.png"
        # Seed the file so append has something to append to.
        requested_path.write_bytes(_png_bytes())

        request = WriteFileRequest(file_path=str(requested_path), content=_png_bytes(), append=True)
        os_manager.on_write_file_request(request)

        assert called == []


class TestExtensionCoercionDoesNotClobberPriorSave:
    """Regression for issue #4924.

    Before the fix, CREATE_NEW saves whose bytes coerced to a different suffix than the
    template's would silently overwrite a prior save: the index scan globbed the template's
    suffix, missed the previously-coerced file, returned the same index, and the post-write
    rename clobbered it. After the fix the planner sniffs once up front and every candidate
    path (try-first, scan glob, indexed-walk) ends at the sniffed suffix, so each save
    lands on a distinct index.
    """

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir).resolve()

    @pytest.fixture(autouse=True)
    def setup_project(self, temp_dir: Path, griptape_nodes: GriptapeNodes) -> Generator[None, None, None]:
        original_workspace = griptape_nodes.ConfigManager().workspace_path

        project_yml = temp_dir / "project_template.yml"
        project_yml.write_text(DEFAULT_PROJECT_TEMPLATE.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE))
        load_result = GriptapeNodes.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
        if isinstance(load_result, LoadProjectTemplateResultSuccess):
            GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=load_result.project_id))

        griptape_nodes.ArtifactManager().on_handle_register_artifact_provider_request(
            RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider)
        )

        griptape_nodes.ConfigManager().workspace_path = temp_dir

        yield

        GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=None))
        griptape_nodes.ConfigManager().workspace_path = original_workspace

    @pytest.fixture
    def outputs_dir(self, temp_dir: Path, setup_project: None) -> Path:  # noqa: ARG002
        outputs = temp_dir / "outputs"
        outputs.mkdir(parents=True, exist_ok=True)
        return outputs

    def test_padded_index_macro_does_not_clobber_previously_coerced_save(self, outputs_dir: Path) -> None:
        """The exact scenario from issue #4924.

        Macro declares ``.png`` but the bytes sniff as JPEG. Two CREATE_NEW saves with the
        same JPEG bytes should produce ``render_v001.jpg`` AND ``render_v002.jpg``, with
        the first save intact. Before the fix the second save overwrote the first.
        """
        macro_path = MacroPath(ParsedMacro("{outputs}/render_v{_index:03}.png"), {})

        first_payload = _jpeg_bytes()
        FileDestination(macro_path, existing_file_policy=ExistingFilePolicy.CREATE_NEW).write_bytes(first_payload)

        second_payload = _jpeg_bytes()
        FileDestination(macro_path, existing_file_policy=ExistingFilePolicy.CREATE_NEW).write_bytes(second_payload)

        v001 = outputs_dir / "render_v001.jpg"
        v002 = outputs_dir / "render_v002.jpg"

        assert v001.exists(), "First coerced save must still exist after the second save"
        assert v002.exists(), "Second save must land at v002, not clobber v001"
        # The template's .png suffix must never appear on disk for these saves.
        assert not (outputs_dir / "render_v001.png").exists()
        assert not (outputs_dir / "render_v002.png").exists()

    def test_plain_path_create_new_with_mismatched_bytes_does_not_clobber(
        self, griptape_nodes: GriptapeNodes, outputs_dir: Path
    ) -> None:
        """Plain string path variant: two JPEG saves to ``output.png`` produce two .jpg files."""
        os_manager = griptape_nodes.OSManager()
        requested_path = outputs_dir / "output.png"

        first_result = os_manager.on_write_file_request(
            WriteFileRequest(
                file_path=str(requested_path),
                content=_jpeg_bytes(),
                existing_file_policy=ExistingFilePolicy.CREATE_NEW,
            )
        )
        second_result = os_manager.on_write_file_request(
            WriteFileRequest(
                file_path=str(requested_path),
                content=_jpeg_bytes(),
                existing_file_policy=ExistingFilePolicy.CREATE_NEW,
            )
        )

        assert isinstance(first_result, WriteFileResultSuccess)
        assert isinstance(second_result, WriteFileResultSuccess)
        first_path = Path(first_result.final_file_path)
        second_path = Path(second_result.final_file_path)
        assert first_path != second_path, "Second save must not reuse the first save's path"
        assert first_path.exists()
        assert second_path.exists()
        assert first_path.suffix == ".jpg"
        assert second_path.suffix == ".jpg"


class TestCreateNewMacroIndexSeed:
    """A required ``{x:NN}`` slot in a CREATE_NEW save macro auto-allocates the next index.

    Drives the production write path (``FileDestination(macro_path).write_bytes(...)``).
    The seed assigns ``1`` to the padded slot in ``on_get_path_for_macro_request`` (gated
    on ``existing_file_policy=CREATE_NEW``); on collision OSManager walks forward against
    the user's original macro through the project resolver each iteration.

    The padding spec (``NumericPaddingFormat``) is the macro author's opt-in: a single
    unresolved required variable WITH padding is treated as an auto-index slot. Without
    padding the request must fail loudly so callers can't silently auto-fill a variable
    they forgot to bind (e.g. ``{shot}`` on its own).
    """

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        with tempfile.TemporaryDirectory() as tmpdir:
            # On Windows tempfile may return the 8.3 short form; resolve so the test's
            # comparisons match what the macro resolver canonicalizes to.
            yield Path(tmpdir).resolve()

    @pytest.fixture(autouse=True)
    def setup_project(self, temp_dir: Path, griptape_nodes: GriptapeNodes) -> Generator[None, None, None]:
        # Production macro resolution requires a loaded project so `{outputs}` from the
        # default template can resolve to <workspace>/outputs.
        #
        # Critical ordering: Load the project FIRST, activate it, THEN force the
        # workspace_path. SetCurrentProjectRequest internally re-derives workspace_path
        # from the project/workspace config layers (see ProjectManager._activate_project),
        # which would clobber any earlier workspace_path assignment. Setting it AFTER
        # activation makes our temp_dir stick for the duration of the test.
        original_workspace = griptape_nodes.ConfigManager().workspace_path

        project_yml = temp_dir / "project_template.yml"
        project_yml.write_text(DEFAULT_PROJECT_TEMPLATE.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE))
        load_result = GriptapeNodes.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
        if isinstance(load_result, LoadProjectTemplateResultSuccess):
            GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=load_result.project_id))

        griptape_nodes.ConfigManager().workspace_path = temp_dir

        yield

        GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=None))
        griptape_nodes.ConfigManager().workspace_path = original_workspace

    @pytest.fixture
    def outputs_dir(self, temp_dir: Path, setup_project: None) -> Path:  # noqa: ARG002
        # Default project template binds {outputs} to the relative path "outputs",
        # so saved files land under <workspace>/outputs/. Pre-create AFTER setup_project
        # has finished mutating workspace_path, so the directory we create matches the
        # workspace the resolver will use during the test.
        outputs = temp_dir / "outputs"
        outputs.mkdir(parents=True, exist_ok=True)
        return outputs

    @staticmethod
    def _save(macro_path: MacroPath, content: bytes) -> None:
        FileDestination(
            macro_path,
            existing_file_policy=ExistingFilePolicy.CREATE_NEW,
        ).write_bytes(content)

    def test_first_save_with_padded_required_index_writes_v001(self, outputs_dir: Path) -> None:
        """Bug #4875: the first save must produce v001, not fail with MISSING_REQUIRED."""
        macro_path = MacroPath(ParsedMacro("{outputs}/render_v{_index:03}.png"), {})

        self._save(macro_path, b"first")

        assert (outputs_dir / "render_v001.png").exists()

    def test_subsequent_saves_increment_with_padding_preserved(self, outputs_dir: Path) -> None:
        """Three saves in a row must produce v001, v002, v003 — not v001, v001_1, v001_2."""
        macro_path = MacroPath(ParsedMacro("{outputs}/render_v{_index:03}.png"), {})

        for _ in range(3):
            self._save(macro_path, b"x")

        assert (outputs_dir / "render_v001.png").exists()
        assert (outputs_dir / "render_v002.png").exists()
        assert (outputs_dir / "render_v003.png").exists()

    def test_gap_fill_picks_lowest_unused_padded_index(self, outputs_dir: Path) -> None:
        """If v001 and v003 exist, next save must take v002 (gap-fill)."""
        (outputs_dir / "render_v001.png").write_bytes(b"existing")
        (outputs_dir / "render_v003.png").write_bytes(b"existing")

        macro_path = MacroPath(ParsedMacro("{outputs}/render_v{_index:03}.png"), {})

        self._save(macro_path, b"new")

        assert (outputs_dir / "render_v002.png").exists()
        assert (outputs_dir / "render_v002.png").read_bytes() == b"new"

    def test_hash_shorthand_macro_auto_allocates_like_padded_legacy(self, outputs_dir: Path) -> None:
        """A `{###}` sequence-slot macro auto-allocates v001/v002/v003 just like the legacy `{_index:03}` form.

        Drives the production write path through the new explicit syntax. The
        parser emits a ``SequenceFormat`` slot which ``_has_sequence_slot_marker``
        recognizes through the seed gate and the collision-walk gate — so the
        ``{###}`` macro produces the same v001/v002/v003 sequence as the legacy
        ``{_index:03}`` form would. (The glob-builder's ``SequenceFormat`` →
        `*` branch is a separate code path exercised by
        ``test_sequence_format_glob_uses_permissive_wildcard`` in
        ``test_os_manager.py``.)
        """
        macro_path = MacroPath(ParsedMacro("{outputs}/render_v{###}.png"), {})

        for _ in range(3):
            self._save(macro_path, b"x")

        assert (outputs_dir / "render_v001.png").exists()
        assert (outputs_dir / "render_v002.png").exists()
        assert (outputs_dir / "render_v003.png").exists()

    def test_unbound_required_var_without_padding_fails_loudly(self, outputs_dir: Path) -> None:
        """Safety: an unbound `{shot}` must NOT silently auto-allocate as 1, 2, 3, ….

        Without the `:NN` padding marker the macro author hasn't opted into auto-index
        semantics. A single unresolved required var here is a configuration mistake (the
        user forgot to wire `{shot}` to a node parameter), and we must surface it as
        MISSING_MACRO_VARIABLES rather than write `1_render.png`, `2_render.png`, … to
        disk under a name the user never intended.
        """
        macro_path = MacroPath(ParsedMacro("{outputs}/{shot}_render.png"), {})

        # Macro resolution happens inside OSManager during WriteFileRequest, so the
        # missing-variables failure surfaces as FileWriteError (not FileLoadError).
        with pytest.raises(FileWriteError) as excinfo:
            self._save(macro_path, b"should not write")

        assert excinfo.value.failure_reason == FileIOFailureReason.MISSING_MACRO_VARIABLES
        # And nothing was silently auto-allocated.
        assert not any(outputs_dir.iterdir())

    # ---------------------------------------------------------------------
    # Stress tests — Tier 1 (must ship with this PR)
    # ---------------------------------------------------------------------

    def test_user_var_with_glob_metacharacters_does_not_match_siblings(self, outputs_dir: Path) -> None:
        """User-supplied values with glob metacharacters must not match siblings during the scan.

        ``partial_resolve`` substitutes user variable values directly into the static segments
        of the glob pattern. If a user binds ``file_name_base="render[final]"`` (or contains
        ``?`` / ``*``), those characters become glob metacharacters and the scan could match
        unintended files — e.g. a sibling literal-bracket name might shadow the real one.

        Pre-create a sibling that *would* false-positive against an unescaped glob, then drive
        a CREATE_NEW save and assert: (a) the new file lands at the literal name, and (b) the
        index allocated reflects only files whose name actually matches the literal template,
        not the false-positive. If this fails the fix lives in
        ``_build_glob_pattern_from_partially_resolved`` (or its caller) and tracks under a
        follow-up issue — see the plan file.
        """
        # Bracket class would match any single char in the class — `renderx_v001.png` would
        # match `render[final]_v???.png` if the brackets aren't escaped at the glob level.
        (outputs_dir / "renderf_v001.png").write_bytes(b"trap")
        # The literal-named existing file we DO want the scan to honor.
        (outputs_dir / "render[final]_v001.png").write_bytes(b"existing")

        macro_path = MacroPath(
            ParsedMacro("{outputs}/{file_name_base}_v{_index:03}.png"),
            {"file_name_base": "render[final]"},
        )

        self._save(macro_path, b"new")

        # Either (a) the scan correctly ignores `renderf_v001.png` and gap-fills/extends from
        # the real `render[final]_v001.png`, or (b) the test surfaces a real escaping bug —
        # in which case we fix it or xfail with a follow-up issue link.
        assert (outputs_dir / "render[final]_v002.png").exists()
        assert (outputs_dir / "render[final]_v002.png").read_bytes() == b"new"

    @pytest.mark.skipif(
        sys.platform != "darwin",
        reason="NFC/NFD divergence is most acute on macOS HFS+/APFS where readdir may return either form",
    )
    def test_unicode_nfd_filename_round_trip(self, outputs_dir: Path) -> None:
        """Existing files written with one Unicode normal form must be recognized by the scan.

        ``parsed_macro.extract_variables`` matches static segments byte-for-byte. If a user
        binds ``file_name_base="café"`` (NFC: ``café`` precomposed → ``café``) but the
        on-disk readdir returns the NFD form (``café`` decomposed), extraction fails
        silently and the scan returns 1 even though v001 already exists. macOS is the most
        likely place this bites; gate the test there.
        """
        # Pre-create a v001 in the form macOS may store internally (NFD).
        nfd_name = unicodedata.normalize("NFD", "café") + "_v001.png"
        (outputs_dir / nfd_name).write_bytes(b"existing")

        # Macro author binds the NFC form (the form their text editor / API likely produced).
        macro_path = MacroPath(
            ParsedMacro("{outputs}/{file_name_base}_v{_index:03}.png"),
            {"file_name_base": unicodedata.normalize("NFC", "café")},
        )

        self._save(macro_path, b"new")

        # Whichever form ends up on disk, the next index must be 002 — not 001 (which would
        # collide with the existing file via the convert-on-collision fallback). We can't
        # name the expected file directly since we don't know which normal form the FS will
        # use; glob for either NFC or NFD form of the cafe-with-accent prefix.
        v002_candidates = [p for p in outputs_dir.iterdir() if p.name.endswith("_v002.png")]
        assert len(v002_candidates) == 1, f"Expected exactly one v002, got {v002_candidates}"
        assert v002_candidates[0].read_bytes() == b"new"

    @pytest.mark.parametrize("policy", [ExistingFilePolicy.OVERWRITE, ExistingFilePolicy.FAIL])
    def test_non_create_new_policies_do_not_seed_padded_index(
        self, outputs_dir: Path, policy: ExistingFilePolicy
    ) -> None:
        """OVERWRITE and FAIL must NOT auto-seed `{_index:03}` — that's CREATE_NEW's contract.

        ``GetPathForMacroRequest`` only fires the auto-seed when ``existing_file_policy``
        is ``CREATE_NEW``. Other policies surface MISSING_REQUIRED_VARIABLES so callers
        see a real configuration error rather than silently allocating an indexed path.
        """
        macro_path = MacroPath(ParsedMacro("{outputs}/render_v{_index:03}.png"), {})

        # Write surfaces the macro-resolution failure as a FileWriteError (the failure
        # happens inside OSManager during WriteFileRequest, not in file.py's resolver).
        # Same failure_reason either way: MISSING_MACRO_VARIABLES.
        with pytest.raises(FileWriteError) as excinfo:
            FileDestination(macro_path, existing_file_policy=policy).write_bytes(b"should not write")

        assert excinfo.value.failure_reason == FileIOFailureReason.MISSING_MACRO_VARIABLES
        assert not any(outputs_dir.iterdir())

    def test_read_path_does_not_seed_padded_index(self, outputs_dir: Path) -> None:
        """`File.read()` against a padded `{_index:03}` macro must fail loudly, not auto-allocate.

        The seed-on-retry only fires when ``_resolve_file_path`` is called with
        ``existing_file_policy=CREATE_NEW``. Read paths leave the policy as ``None`` so an
        unbound index variable surfaces as MISSING_REQUIRED_VARIABLES. Critical: a silent
        auto-fill on a read would point reads at non-existent indexed files and quietly
        return wrong data.
        """
        # Pre-populate so even if the seed mistakenly fires, there's something to "find".
        (outputs_dir / "render_v001.png").write_bytes(b"existing")

        macro_path = MacroPath(ParsedMacro("{outputs}/render_v{_index:03}.png"), {})

        with pytest.raises(FileLoadError) as excinfo:
            File(macro_path).read()

        assert excinfo.value.failure_reason == FileIOFailureReason.MISSING_MACRO_VARIABLES

    def test_same_index_variable_used_in_directory_and_filename(self, outputs_dir: Path) -> None:
        """Same `{_index:03}` referenced twice (as dir AND filename component) — both seeded equally.

        ``_find_padded_unresolved_required`` picks the first ParsedVariable matching the
        missing name; ``GetPathForMacroRequest.resolve`` then renders BOTH occurrences with
        the same value. Pre-create a v001 sibling so the scan must gap-fill / extend.

        Currently xfail — see issue link in the marker. Promote to expected-pass when
        #4915 lands.
        """
        # Pre-existing v001 directory + v001 file inside.
        (outputs_dir / "v001").mkdir()
        (outputs_dir / "v001" / "render_v001.png").write_bytes(b"existing")

        macro_path = MacroPath(ParsedMacro("{outputs}/v{_index:03}/render_v{_index:03}.png"), {})

        self._save(macro_path, b"new")

        # Same index value applied to both directory and filename → v002/render_v002.png.
        assert (outputs_dir / "v002" / "render_v002.png").exists()
        assert (outputs_dir / "v002" / "render_v002.png").read_bytes() == b"new"


class TestOptionalPaddedIndexCollision:
    """Regression tests for #4544 / #4092: `{_index?:03}` padding preserved on collision.

    Optional padded slots (`{x?:NN}`) were rendering as `_1`, `_2`, … on collision
    because the convert-on-collision branch synthesized `{stem}_{_index}` from the
    resolved string — losing the format spec. The fix walks the user's ORIGINAL
    macro's padded slot (any padding, required OR optional) so the format spec is
    preserved across the entire sequence.

    Tests cover the required `{x:03}` and optional `{x?:03}` shapes side-by-side so
    regressions in either direction are caught.
    """

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir).resolve()

    @pytest.fixture(autouse=True)
    def setup_project(self, temp_dir: Path, griptape_nodes: GriptapeNodes) -> Generator[None, None, None]:
        # Same load-then-force ordering as TestCreateNewMacroIndexSeed: SetCurrentProject
        # remerges workspace_path from project config layers, so we set it AFTER.
        original_workspace = griptape_nodes.ConfigManager().workspace_path
        project_yml = temp_dir / "project_template.yml"
        project_yml.write_text(DEFAULT_PROJECT_TEMPLATE.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE))
        load_result = GriptapeNodes.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
        if isinstance(load_result, LoadProjectTemplateResultSuccess):
            GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=load_result.project_id))
        griptape_nodes.ConfigManager().workspace_path = temp_dir
        yield
        GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=None))
        griptape_nodes.ConfigManager().workspace_path = original_workspace

    @pytest.fixture
    def outputs_dir(self, temp_dir: Path, setup_project: None) -> Path:  # noqa: ARG002
        outputs = temp_dir / "outputs"
        outputs.mkdir(parents=True, exist_ok=True)
        return outputs

    @staticmethod
    def _save(macro_path: MacroPath, content: bytes) -> None:
        FileDestination(macro_path, existing_file_policy=ExistingFilePolicy.CREATE_NEW).write_bytes(content)

    # --- Required `{x:03}` shape ----------------------------------------------------

    def test_required_padded_first_save_writes_001(self, outputs_dir: Path) -> None:
        """Required `{_index:03}` first save: seed assigns 1, lands at v001."""
        macro_path = MacroPath(ParsedMacro("{outputs}/render_v{_index:03}.png"), {})
        self._save(macro_path, b"first")
        assert (outputs_dir / "render_v001.png").exists()

    def test_required_padded_collision_walks_to_002(self, outputs_dir: Path) -> None:
        """Required `{_index:03}` second save: walks to v002 with padding preserved."""
        (outputs_dir / "render_v001.png").write_bytes(b"existing")
        macro_path = MacroPath(ParsedMacro("{outputs}/render_v{_index:03}.png"), {})
        self._save(macro_path, b"second")
        assert (outputs_dir / "render_v002.png").exists()
        # Negative: the buggy convert-on-collision shape produced this name.
        assert not (outputs_dir / "render_v001_1.png").exists()

    # --- Optional `{x?:03}` shape (the #4544 / #4092 regression tests) --------------

    def test_optional_padded_first_save_writes_unindexed(self, outputs_dir: Path) -> None:
        """Optional `{_index?:03}` first save: index slot is OMITTED (no padding rendered).

        This is the optional-`?` contract: the slot disappears entirely when no value
        is supplied. The auto-index seed only fires for REQUIRED missing vars, so the
        first save resolves with `_index` simply not present in the output.
        """
        macro_path = MacroPath(ParsedMacro("{outputs}/file{_index?:03}.png"), {})
        self._save(macro_path, b"first")
        # No padded suffix on the first save — the optional slot vanishes.
        assert (outputs_dir / "file.png").exists()
        # Specifically, NO index-bearing variants exist.
        index_files = sorted(
            p.name for p in outputs_dir.iterdir() if p.name.startswith("file") and p.name != "file.png"
        )
        assert index_files == [], f"Unexpected index files: {index_files}"

    def test_optional_padded_collision_walks_to_001_padded(self, outputs_dir: Path) -> None:
        """Optional `{_index?:03}` SECOND save: walks to padded `_001`, NOT unpadded `_1`.

        This is the bug #4544 / #4092 regression test. Pre-fix behavior:
        `_convert_str_path_to_macro_with_index("file.png")` synthesized
        `file_{_index}.png` (no format spec) → produced `file_1.png`. The fix walks
        the user's ORIGINAL macro's `{_index?:03}` slot, preserving the `:03` format.
        """
        # Pre-create the un-indexed base so the next save MUST walk to a padded index.
        (outputs_dir / "file.png").write_bytes(b"existing un-indexed")

        macro_path = MacroPath(ParsedMacro("{outputs}/file{_index?:03}.png"), {})
        self._save(macro_path, b"second")

        # Correct outcome with the fix: padded index from the original macro.
        assert (outputs_dir / "file001.png").exists()
        assert (outputs_dir / "file001.png").read_bytes() == b"second"
        # Buggy outcomes (#4544 / #4092): unpadded suffix injection.
        assert not (outputs_dir / "file_1.png").exists()
        assert not (outputs_dir / "file_001.png").exists()  # different separator shape

    def test_optional_padded_third_save_walks_to_002(self, outputs_dir: Path) -> None:
        """Optional `{_index?:03}` THIRD save: continues padded — 002, not _1 or _2."""
        (outputs_dir / "file.png").write_bytes(b"existing un-indexed")
        (outputs_dir / "file001.png").write_bytes(b"existing v001")

        macro_path = MacroPath(ParsedMacro("{outputs}/file{_index?:03}.png"), {})
        self._save(macro_path, b"third")

        assert (outputs_dir / "file002.png").exists()
        assert (outputs_dir / "file002.png").read_bytes() == b"third"
        assert not (outputs_dir / "file_1.png").exists()
        assert not (outputs_dir / "file_2.png").exists()

    # Note: the separator-then-padding stack `{_index?:_:03}` is mentioned in the
    # macros.md docs but currently fails in the macro parser (NumericPaddingFormat
    # tries to apply `:03` to the separator-prepended string `1_` and errors). That's
    # a parser-level bug, not the collision-walk bug this PR fixes — out of scope here.


class TestCreateNewMacroIndexSeedDefensiveFallthrough:
    """Defensive paths around the auto-index seed and the collision-walk loop.

    Covers the heuristic refusing ambiguous macros, race-loss correctness (the walk
    increments the user's ORIGINAL macro's padded slot — never suffix-injects ``_1``
    on the resolved string), and budget exhaustion when the candidate space is full.
    """

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir).resolve()

    @pytest.fixture(autouse=True)
    def setup_project(self, temp_dir: Path, griptape_nodes: GriptapeNodes) -> Generator[None, None, None]:
        # Same ordering as TestCreateNewMacroIndexSeed.setup_project — load + activate
        # FIRST, then force workspace_path. SetCurrentProjectRequest re-derives the
        # workspace from project config layers and would otherwise clobber temp_dir.
        original_workspace = griptape_nodes.ConfigManager().workspace_path
        project_yml = temp_dir / "project_template.yml"
        project_yml.write_text(DEFAULT_PROJECT_TEMPLATE.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE))
        load_result = GriptapeNodes.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
        if isinstance(load_result, LoadProjectTemplateResultSuccess):
            GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=load_result.project_id))
        griptape_nodes.ConfigManager().workspace_path = temp_dir
        yield
        GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=None))
        griptape_nodes.ConfigManager().workspace_path = original_workspace

    @pytest.fixture
    def outputs_dir(self, temp_dir: Path, setup_project: None) -> Path:  # noqa: ARG002
        # Pre-create AFTER setup_project so the {outputs} directory matches the
        # workspace_path that resolver will see during the test.
        outputs = temp_dir / "outputs"
        outputs.mkdir(parents=True, exist_ok=True)
        return outputs

    def test_two_distinct_padded_required_vars_fail_loudly(self, temp_dir: Path) -> None:
        """Macro with TWO distinct padded unresolved required vars must fail, not auto-pick.

        ``_find_padded_unresolved_required`` only fires for a SINGLE missing required
        variable. With two (``{a:03}`` and ``{b:03}``), the heuristic refuses to guess —
        the user sees ``MISSING_REQUIRED_VARIABLES`` naming both, no silent allocation.
        """
        outputs = temp_dir / "outputs"
        outputs.mkdir(parents=True, exist_ok=True)

        macro_path = MacroPath(ParsedMacro("{outputs}/{a:03}_{b:03}.png"), {})

        with pytest.raises(FileWriteError) as excinfo:
            FileDestination(macro_path, existing_file_policy=ExistingFilePolicy.CREATE_NEW).write_bytes(
                b"should not write"
            )

        assert excinfo.value.failure_reason == FileIOFailureReason.MISSING_MACRO_VARIABLES
        assert not any(outputs.iterdir())

    # ---------------------------------------------------------------------
    # Race-loss correctness — collision-walk against the ORIGINAL macro
    # ---------------------------------------------------------------------
    # The "assign 1, walk on collision" model means race-loss is handled inside
    # OSManager's CREATE_NEW collision-fallback: when the seeded write at v001 fails
    # (because a racer wrote that slot, or because a previous save in the same series
    # used it), the candidate loop increments the user's ORIGINAL macro's padded slot
    # forward (v002, v003, …) and re-resolves through the project resolver each
    # iteration — never synthesizing a `_1` suffix on the resolved string.
    #
    # The natural CREATE_NEW save path produces these outcomes; no helper-level mocking
    # required.

    def test_race_loss_picks_next_index_not_suffix(self, outputs_dir: Path) -> None:
        """A pre-existing v003 must steer the next save to v004, not v003_1."""
        # Pre-create v003 to simulate a racer's win (or a prior save in this series).
        (outputs_dir / "render_v003.png").write_bytes(b"existing race winner")

        macro_path = MacroPath(ParsedMacro("{outputs}/render_v{_index:03}.png"), {})

        # The seed assigns 1 → resolves to v001 → write succeeds (no v001 yet).
        # To exercise the collision-walk we need to land on a slot that already exists.
        # Pre-creating v001 forces the seed-then-fail path → walk increments to v002,
        # which is also free, so v002 lands. Pre-creating up to v003 leaves v004 as the
        # first free slot → that's what should land.
        (outputs_dir / "render_v001.png").write_bytes(b"earlier save")
        (outputs_dir / "render_v002.png").write_bytes(b"earlier save")

        FileDestination(macro_path, existing_file_policy=ExistingFilePolicy.CREATE_NEW).write_bytes(
            b"new save lands at v004"
        )

        assert (outputs_dir / "render_v004.png").exists()
        assert (outputs_dir / "render_v004.png").read_bytes() == b"new save lands at v004"
        # Wrong outcome from the previous suffix-injection bug.
        assert not (outputs_dir / "render_v003_1.png").exists()
        # The pre-existing v003 is untouched.
        assert (outputs_dir / "render_v003.png").read_bytes() == b"existing race winner"

    def test_persistent_contention_exhausts_budget(self, outputs_dir: Path) -> None:
        """If every candidate up to MAX_INDEXED_CANDIDATES is taken, raise FileWriteError.

        Defensive contract: rather than busy-loop forever, the candidate loop gives up
        and the caller sees a real error.
        """
        from griptape_nodes.retained_mode.managers.os_manager import MAX_INDEXED_CANDIDATES

        # Pre-create v001..vMAX so every candidate the walk tries is taken. The walk
        # starts at 2 (the seed already tried 1 and saw it exist), so we need
        # MAX_INDEXED_CANDIDATES + 1 files to fully exhaust.
        for i in range(1, MAX_INDEXED_CANDIDATES + 2):
            (outputs_dir / f"render_v{i:03}.png").write_bytes(b"taken")

        macro_path = MacroPath(ParsedMacro("{outputs}/render_v{_index:03}.png"), {})

        with pytest.raises(FileWriteError) as excinfo:
            FileDestination(macro_path, existing_file_policy=ExistingFilePolicy.CREATE_NEW).write_bytes(
                b"should not land"
            )

        # Diagnostic: error message describes the exhaustion so an oncall can locate it.
        # OSManager's existing collision-loop exhaustion message names the candidate count.
        details = str(excinfo.value.result_details).lower()
        assert "could not find available filename" in details or "exhausted" in details
        # No new files were created — the pre-populated set is exactly what's there.
        survivors = sorted(p.name for p in outputs_dir.iterdir())
        expected = sorted(f"render_v{i:03}.png" for i in range(1, MAX_INDEXED_CANDIDATES + 2))
        assert survivors == expected, f"Unexpected files: {set(survivors) - set(expected)}"


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows-specific path stressors")
class TestCreateNewMacroIndexSeedWindowsPaths:
    """Tier 2 stress tests: Windows-specific path shapes (long paths, native separators).

    These run only on Windows CI — the long-path and mixed-separator failure modes are
    Windows-only by definition. The first test guards #4908 (workspace anchor); the
    second guards the regression that broke the prior Windows CI run (mixed separators
    in the macro template / glob round-trip).
    """

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir).resolve()

    @pytest.fixture(autouse=True)
    def setup_project(self, temp_dir: Path, griptape_nodes: GriptapeNodes) -> Generator[None, None, None]:
        # Same ordering as TestCreateNewMacroIndexSeed.setup_project — load + activate
        # FIRST, then force workspace_path so SetCurrentProject's project-config remerge
        # doesn't clobber temp_dir.
        original_workspace = griptape_nodes.ConfigManager().workspace_path
        project_yml = temp_dir / "project_template.yml"
        project_yml.write_text(DEFAULT_PROJECT_TEMPLATE.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE))
        load_result = GriptapeNodes.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
        if isinstance(load_result, LoadProjectTemplateResultSuccess):
            GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=load_result.project_id))
        griptape_nodes.ConfigManager().workspace_path = temp_dir
        yield
        GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=None))
        griptape_nodes.ConfigManager().workspace_path = original_workspace

    def test_long_windows_path_gap_fill(self, temp_dir: Path) -> None:
        r"""Workspace + nested dirs + filename combine to >260 chars; gap-fill must still work.

        ``_apply_windows_long_path_prefix`` (``\\?\``) is added by ``canonicalize_for_io``
        only at the OS-level write boundary. The scan in ``_scan_for_next_available_index``
        uses ``Path.glob()`` directly. If pathlib can't enumerate long-path entries on this
        runner, the scan returns 1 and the test fails — surfacing a real bug to fix or
        track.
        """
        # Build a deep enough subtree to push past WINDOWS_MAX_PATH: each level adds ~20
        # chars, workspace tempdir is already ~50 chars, so 12 levels of 20 chars plus
        # the filename should comfortably exceed 260.
        deep_segment = "a" * 20
        deep_outputs = temp_dir / "outputs"
        for _ in range(12):
            deep_outputs = deep_outputs / deep_segment
        deep_outputs.mkdir(parents=True, exist_ok=True)
        # Sanity: confirm we actually crossed the threshold for this test environment.
        assert len(str(deep_outputs / "render_v001.png")) > WINDOWS_MAX_PATH, (
            "Test setup didn't produce a long-enough path"
        )

        # Pre-populate v001 and v003 — gap-fill should pick v002.
        (deep_outputs / "render_v001.png").write_bytes(b"existing")
        (deep_outputs / "render_v003.png").write_bytes(b"existing")

        # Match the deep template — repeat the segment 12 times under {outputs}.
        sub = "/".join([deep_segment] * 12)
        macro_path = MacroPath(ParsedMacro(f"{{outputs}}/{sub}/render_v{{_index:03}}.png"), {})

        FileDestination(macro_path, existing_file_policy=ExistingFilePolicy.CREATE_NEW).write_bytes(b"new")

        assert (deep_outputs / "render_v002.png").exists()
        assert (deep_outputs / "render_v002.png").read_bytes() == b"new"

    def test_mixed_separator_round_trip_windows(self, temp_dir: Path) -> None:
        """Backslash-laden glob results round-trip through extract_variables against a POSIX template.

        On Windows: ``Path.glob()`` returns absolute ``WindowsPath`` objects whose ``str()``
        uses backslashes. The macro template uses forward slashes. Without a clean
        round-trip in ``_extract_index_from_filename``, indices won't extract and the scan
        returns 1 every call — the same failure mode that broke the prior Windows CI run.
        """
        outputs_dir = temp_dir / "outputs"
        outputs_dir.mkdir(parents=True, exist_ok=True)
        (outputs_dir / "render_v001.png").write_bytes(b"existing")
        (outputs_dir / "render_v002.png").write_bytes(b"existing")

        # POSIX-style template; production macros always use `/`.
        macro_path = MacroPath(ParsedMacro("{outputs}/render_v{_index:03}.png"), {})

        FileDestination(macro_path, existing_file_policy=ExistingFilePolicy.CREATE_NEW).write_bytes(b"new")

        # Must extend to v003 — if the round-trip fails, the scan returns 1 and the
        # convert-on-collision branch produces `render_v001_1.png` instead.
        assert (outputs_dir / "render_v003.png").exists()
        assert (outputs_dir / "render_v003.png").read_bytes() == b"new"
        # Negative assertion: the convert-on-collision unpadded fallback must NOT have fired.
        assert not (outputs_dir / "render_v001_1.png").exists()


class TestWriteTempFileRequest:
    """``WriteTempFileRequest`` stages bytes at the project's SAVE_TEMP_FILE macro path.

    Distinct from ``WriteFileRequest``: caller does not pick the destination,
    the situation's macro does. Used by artifact providers that need bytes
    on disk for one-shot inspection (e.g. ffprobe for codec extraction).
    """

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir).resolve()

    @pytest.fixture(autouse=True)
    def setup_workspace(self, temp_dir: Path, griptape_nodes: GriptapeNodes) -> Generator[None, None, None]:
        """Load the default project template so SAVE_TEMP_FILE resolves."""
        config_manager = griptape_nodes.ConfigManager()
        original_workspace = config_manager.workspace_path
        config_manager.set_config_value("workspace_directory", str(temp_dir))

        project_yml = temp_dir / "project_template.yml"
        project_yml.write_text(DEFAULT_PROJECT_TEMPLATE.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE))
        load_result = GriptapeNodes.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
        if isinstance(load_result, LoadProjectTemplateResultSuccess):
            GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=load_result.project_id))

        yield

        GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=None))
        config_manager.set_config_value("workspace_directory", str(original_workspace))

    def test_stages_bytes_at_project_temp_path(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.os_events import (
            WriteTempFileRequest,
            WriteTempFileResultSuccess,
        )

        os_manager = griptape_nodes.OSManager()
        payload = b"probe target bytes"

        result = os_manager.on_write_temp_file_request(
            WriteTempFileRequest(
                content=payload,
                variables={"file_name_base": "probe", "file_extension": "mp4"},
            )
        )

        assert isinstance(result, WriteTempFileResultSuccess)
        staged = Path(result.staged_path)
        # File must exist and carry the caller's bytes verbatim.
        assert staged.exists()
        assert staged.read_bytes() == payload
        assert result.bytes_written == len(payload)
        # Suffix must reflect ``file_extension`` so ffprobe & tools recognize
        # the type from the filename.
        assert staged.suffix == ".mp4"
        assert "probe" in staged.stem

    def test_caller_controls_uniqueness_via_variables(self, griptape_nodes: GriptapeNodes) -> None:
        """The handler does not inject uuids -- callers who want uniqueness supply their own.

        SAVE_TEMP_FILE's on-collision policy is OVERWRITE, so two callers passing
        the same ``file_name_base`` land at the same path and the second wins.
        Callers who need collision safety (e.g. the codec vet racing across
        multiple provider instances) must include a uuid in ``file_name_base``.
        """
        from griptape_nodes.retained_mode.events.os_events import (
            WriteTempFileRequest,
            WriteTempFileResultSuccess,
        )

        os_manager = griptape_nodes.OSManager()

        result_a = os_manager.on_write_temp_file_request(
            WriteTempFileRequest(content=b"a", variables={"file_name_base": "unique-a", "file_extension": "mp4"})
        )
        result_b = os_manager.on_write_temp_file_request(
            WriteTempFileRequest(content=b"b", variables={"file_name_base": "unique-b", "file_extension": "mp4"})
        )

        assert isinstance(result_a, WriteTempFileResultSuccess)
        assert isinstance(result_b, WriteTempFileResultSuccess)
        assert result_a.staged_path != result_b.staged_path
        assert Path(result_a.staged_path).read_bytes() == b"a"
        assert Path(result_b.staged_path).read_bytes() == b"b"

    def test_lands_under_project_temp_directory(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """The staged path is under the project's ``{temp}`` directory, not the OS temp dir.

        Confirms we're using the SAVE_TEMP_FILE situation's project-scoped
        macro rather than a system-wide tempfile, which is the whole point of
        the request type (avoid exhausting a small OS temp partition when
        staging multi-GB video bytes).
        """
        from griptape_nodes.retained_mode.events.os_events import (
            WriteTempFileRequest,
            WriteTempFileResultSuccess,
        )

        os_manager = griptape_nodes.OSManager()

        result = os_manager.on_write_temp_file_request(
            WriteTempFileRequest(content=b"payload", variables={"file_name_base": "probe", "file_extension": "mp4"})
        )

        assert isinstance(result, WriteTempFileResultSuccess)
        staged = Path(result.staged_path).resolve()
        # Staged path must live under the workspace root, not /tmp or the
        # system temp dir. Using ``resolve()`` on both sides handles symlink
        # differences (e.g. macOS ``/var`` vs ``/private/var``).
        assert temp_dir.resolve() in staged.parents

    def test_returns_failure_when_variables_do_not_resolve_macro(self, griptape_nodes: GriptapeNodes) -> None:
        """Caller omitting a required macro variable surfaces as a Failure.

        The SAVE_TEMP_FILE macro references ``{file_name_base}`` and
        ``{file_extension}``. Passing an empty ``variables`` dict leaves those
        slots unresolved; the handler surfaces the ``GetPathForMacroRequest``
        failure verbatim as ``INVALID_PATH``.
        """
        from griptape_nodes.retained_mode.events.os_events import (
            FileIOFailureReason,
            WriteTempFileRequest,
            WriteTempFileResultFailure,
        )

        os_manager = griptape_nodes.OSManager()
        result = os_manager.on_write_temp_file_request(WriteTempFileRequest(content=b"payload", variables={}))

        assert isinstance(result, WriteTempFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.INVALID_PATH

    def test_returns_failure_when_situation_registry_missing_save_temp_file(
        self, griptape_nodes: GriptapeNodes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SAVE_TEMP_FILE absent from the situation registry -> Failure result.

        SAVE_TEMP_FILE ships in DEFAULT_PROJECT_TEMPLATE so this failure
        branch is unreachable in normal operation. Test it by monkeypatching
        the GetSituationRequest handler to return Failure, simulating a
        project template that removed the situation.
        """
        from griptape_nodes.retained_mode.events.os_events import (
            FileIOFailureReason,
            WriteTempFileRequest,
            WriteTempFileResultFailure,
        )
        from griptape_nodes.retained_mode.events.project_events import (
            GetSituationRequest,
            GetSituationResultFailure,
        )

        def situation_missing_handler(request: GetSituationRequest) -> GetSituationResultFailure:  # noqa: ARG001
            return GetSituationResultFailure(result_details="situation missing (test)")

        event_manager = griptape_nodes.EventManager()
        registry = event_manager._request_type_to_manager
        monkeypatch.setitem(registry, GetSituationRequest, situation_missing_handler)

        os_manager = griptape_nodes.OSManager()
        result = os_manager.on_write_temp_file_request(
            WriteTempFileRequest(content=b"payload", variables={"file_name_base": "probe", "file_extension": "mp4"})
        )

        assert isinstance(result, WriteTempFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.INVALID_PATH
        assert "save_temp_file" in str(result.result_details).lower()
