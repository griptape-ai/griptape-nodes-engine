import os
import platform
import tempfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import anyio
import pytest
import send2trash

from griptape_nodes.common.macro_parser import ParsedMacro
from griptape_nodes.common.sequences import MissingItemPolicy, NoTokenBehavior, SequenceScanOptions
from griptape_nodes.files.path_utils import normalize_path_for_platform, resolve_path_safely
from griptape_nodes.retained_mode.events.base_events import ResultDetails
from griptape_nodes.retained_mode.events.os_events import (
    CreateFileRequest,
    CreateFileResultFailure,
    CreateFileResultSuccess,
    DeduceSequencesFromFileListRequest,
    DeduceSequencesFromFileListResultFailure,
    DeduceSequencesFromFileListResultSuccess,
    DeleteFileRequest,
    DeleteFileResultFailure,
    DeleteFileResultSuccess,
    DeletionBehavior,
    DeletionOutcome,
    ExistingFilePolicy,
    FileIOFailureReason,
    GetFileInfoRequest,
    GetFileInfoResultFailure,
    GetFileInfoResultSuccess,
    GetNextUnusedFilenameRequest,
    GetNextUnusedFilenameResultFailure,
    GetNextUnusedFilenameResultSuccess,
    GetNextVersionIndexRequest,
    GetNextVersionIndexResultFailure,
    GetNextVersionIndexResultSuccess,
    ListDirectoryRequest,
    ListDirectoryResultFailure,
    ListDirectoryResultSuccess,
    ListDirectorySequencesRequest,
    ListDirectorySequencesResultSuccess,
    MakeDirectoryRequest,
    MakeDirectoryResultFailure,
    MakeDirectoryResultSuccess,
    ReadFileRequest,
    ReadFileResultFailure,
    ReadFileResultSuccess,
    RenameFileRequest,
    RenameFileResultFailure,
    RenameFileResultSuccess,
    SequenceScanFailureReason,
    WriteFileRequest,
    WriteFileResultFailure,
    WriteFileResultSuccess,
)
from griptape_nodes.retained_mode.events.project_events import MacroPath
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.os_manager import OSManager, WindowsSpecialFolderError

# Windows MAX_PATH constant for tests
WINDOWS_MAX_PATH = 260


class TestWriteFileRequest:
    """Test WriteFileRequest with various scenarios."""

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

    def test_write_text_file_success(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test successfully writing a text file."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "test.txt"
        request = WriteFileRequest(file_path=str(file_path), content="Hello, World!")

        result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultSuccess)
        # Compare resolved paths to handle symlinks (e.g., /var -> /private/var on macOS)
        assert Path(result.final_file_path).resolve() == file_path.resolve()
        assert result.bytes_written > 0
        assert file_path.read_text() == "Hello, World!"

    def test_write_binary_file_success(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test successfully writing a binary file."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "test.bin"
        content = b"\x00\x01\x02\x03"
        request = WriteFileRequest(file_path=str(file_path), content=content)

        result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultSuccess)
        # Compare resolved paths to handle symlinks (e.g., /var -> /private/var on macOS)
        assert Path(result.final_file_path).resolve() == file_path.resolve()
        assert result.bytes_written == len(content)
        assert file_path.read_bytes() == content

    def test_write_file_append_mode(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test appending to an existing file."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "test.txt"
        file_path.write_text("Initial content\n")

        request = WriteFileRequest(file_path=str(file_path), content="Appended content\n", append=True)
        result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultSuccess)
        assert file_path.read_text() == "Initial content\nAppended content\n"

    def test_write_file_overwrite_policy(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test overwriting an existing file with OVERWRITE policy."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "test.txt"
        file_path.write_text("Old content")

        request = WriteFileRequest(
            file_path=str(file_path), content="New content", existing_file_policy=ExistingFilePolicy.OVERWRITE
        )
        result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultSuccess)
        assert file_path.read_text() == "New content"

    def test_write_file_fail_policy(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test FAIL policy when file exists."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "test.txt"
        file_path.write_text("Existing content")

        request = WriteFileRequest(
            file_path=str(file_path), content="New content", existing_file_policy=ExistingFilePolicy.FAIL
        )
        result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.POLICY_NO_OVERWRITE
        # Type checker: result_details is always ResultDetails after __post_init__
        assert isinstance(result.result_details, ResultDetails)
        assert "exists" in result.result_details.result_details[0].message.lower()

    def test_write_file_create_parents_true(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test creating parent directories when create_parents=True."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "subdir" / "nested" / "test.txt"
        request = WriteFileRequest(file_path=str(file_path), content="Content", create_parents=True)

        result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultSuccess)
        assert file_path.exists()
        assert file_path.read_text() == "Content"

    def test_write_file_create_parents_false(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test failure when parent directory missing and create_parents=False."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "nonexistent" / "test.txt"
        request = WriteFileRequest(file_path=str(file_path), content="Content", create_parents=False)

        result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.POLICY_NO_CREATE_PARENT_DIRS

    def test_write_file_invalid_path(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test invalid path handling - attempting to write to a directory."""
        os_manager = griptape_nodes.OSManager()
        # Create a directory and try to write to it as if it were a file
        dir_path = temp_dir / "test_directory"
        dir_path.mkdir()

        request = WriteFileRequest(file_path=str(dir_path), content="Content")

        result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultFailure)
        # Attempting to write to a directory path should fail
        # Windows raises PermissionError, Unix/macOS raises IsADirectoryError
        if platform.system() == "Windows":
            assert result.failure_reason in (
                FileIOFailureReason.IS_DIRECTORY,
                FileIOFailureReason.PERMISSION_DENIED,
            )
        else:
            assert result.failure_reason == FileIOFailureReason.IS_DIRECTORY

    def test_write_file_permission_denied(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test permission denied error."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "test.txt"
        request = WriteFileRequest(file_path=str(file_path), content="Content")

        with patch("builtins.open", side_effect=PermissionError("Permission denied")):
            result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.PERMISSION_DENIED


class TestReadFileRequest:
    """Test ReadFileRequest with failure_reason support."""

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

    def test_read_text_file_success(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test successfully reading a text file."""
        file_path = temp_dir / "test.txt"
        file_path.write_text("Test content")

        request = ReadFileRequest(file_path=str(file_path))
        result = griptape_nodes.handle_request(request)

        assert isinstance(result, ReadFileResultSuccess)
        assert result.content == "Test content"
        assert result.encoding == "utf-8"

    def test_read_binary_file_success(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test successfully reading a binary file."""
        file_path = temp_dir / "test.bin"
        content = b"\x00\x01\x02\x03"
        file_path.write_bytes(content)

        request = ReadFileRequest(file_path=str(file_path))
        result = griptape_nodes.handle_request(request)

        assert isinstance(result, ReadFileResultSuccess)
        # Binary files might be returned as base64 or bytes
        assert result.content is not None

    def test_read_file_not_found(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test reading non-existent file returns FILE_NOT_FOUND."""
        request = ReadFileRequest(file_path=str(temp_dir / "nonexistent.txt"))

        result = griptape_nodes.handle_request(request)

        assert isinstance(result, ReadFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.FILE_NOT_FOUND

    def test_read_file_permission_denied(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test reading file without permission."""
        file_path = temp_dir / "test.txt"
        file_path.write_text("Content")

        request = ReadFileRequest(file_path=str(file_path))

        # Mock the file operation to raise PermissionError
        with patch.object(Path, "open", side_effect=PermissionError("Permission denied")):
            result = griptape_nodes.handle_request(request)

        assert isinstance(result, ReadFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.PERMISSION_DENIED

    def test_read_file_invalid_path(self, griptape_nodes: GriptapeNodes) -> None:
        """Test reading with invalid path - empty path."""
        # Empty path is invalid
        request = ReadFileRequest(file_path="")

        result = griptape_nodes.handle_request(request)

        assert isinstance(result, ReadFileResultFailure)
        # INVALID_PATH is returned for empty path
        assert result.failure_reason == FileIOFailureReason.INVALID_PATH


class TestCreateFileRequest:
    """Test CreateFileRequest with failure_reason support."""

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

    def test_create_empty_file_success(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test creating an empty file."""
        os_manager = griptape_nodes.OSManager()
        request = CreateFileRequest(path=str(temp_dir / "test.txt"), workspace_only=False)

        result = os_manager.on_create_file_request(request)

        assert isinstance(result, CreateFileResultSuccess)
        assert (temp_dir / "test.txt").exists()

    def test_create_file_with_content(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test creating a file with initial content."""
        os_manager = griptape_nodes.OSManager()
        request = CreateFileRequest(path=str(temp_dir / "test.txt"), content="Initial content", workspace_only=False)

        result = os_manager.on_create_file_request(request)

        assert isinstance(result, CreateFileResultSuccess)
        assert (temp_dir / "test.txt").read_text() == "Initial content"

    def test_create_directory_success(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test creating a directory."""
        os_manager = griptape_nodes.OSManager()
        request = CreateFileRequest(path=str(temp_dir / "testdir"), is_directory=True, workspace_only=False)

        result = os_manager.on_create_file_request(request)

        assert isinstance(result, CreateFileResultSuccess)
        assert (temp_dir / "testdir").is_dir()

    def test_create_file_already_exists(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test creating file that already exists returns success with warning."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "test.txt"
        file_path.write_text("Existing")

        request = CreateFileRequest(path=str(file_path), workspace_only=False)
        result = os_manager.on_create_file_request(request)

        assert isinstance(result, CreateFileResultSuccess)
        # Should contain warning in result_details
        # Type checker: result_details is always ResultDetails after __post_init__
        assert isinstance(result.result_details, ResultDetails)
        assert "exists" in result.result_details.result_details[0].message.lower()

    def test_create_file_permission_denied(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test permission denied when creating file."""
        os_manager = griptape_nodes.OSManager()
        request = CreateFileRequest(path=str(temp_dir / "test.txt"), workspace_only=False)

        # CreateFile uses Path.touch() for empty files, not Path.open()
        with patch.object(Path, "touch", side_effect=PermissionError("Permission denied")):
            result = os_manager.on_create_file_request(request)

        assert isinstance(result, CreateFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.PERMISSION_DENIED


class TestRenameFileRequest:
    """Test RenameFileRequest with failure_reason support."""

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

    def test_rename_file_success(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test successfully renaming a file."""
        os_manager = griptape_nodes.OSManager()
        old_path = temp_dir / "old.txt"
        new_path = temp_dir / "new.txt"
        old_path.write_text("Content")

        request = RenameFileRequest(old_path=str(old_path), new_path=str(new_path), workspace_only=False)
        result = os_manager.on_rename_file_request(request)

        assert isinstance(result, RenameFileResultSuccess)
        assert not old_path.exists()
        assert new_path.exists()
        assert new_path.read_text() == "Content"

    def test_rename_file_source_not_found(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test renaming non-existent file."""
        os_manager = griptape_nodes.OSManager()
        request = RenameFileRequest(
            old_path=str(temp_dir / "nonexistent.txt"), new_path=str(temp_dir / "new.txt"), workspace_only=False
        )

        result = os_manager.on_rename_file_request(request)

        assert isinstance(result, RenameFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.FILE_NOT_FOUND

    def test_rename_file_destination_exists(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test renaming when destination already exists."""
        os_manager = griptape_nodes.OSManager()
        old_path = temp_dir / "old.txt"
        new_path = temp_dir / "new.txt"
        old_path.write_text("Old content")
        new_path.write_text("New content")

        request = RenameFileRequest(old_path=str(old_path), new_path=str(new_path), workspace_only=False)
        result = os_manager.on_rename_file_request(request)

        assert isinstance(result, RenameFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.INVALID_PATH

    def test_rename_file_permission_denied(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test permission denied when renaming."""
        os_manager = griptape_nodes.OSManager()
        old_path = temp_dir / "old.txt"
        new_path = temp_dir / "new.txt"
        old_path.write_text("Content")

        request = RenameFileRequest(old_path=str(old_path), new_path=str(new_path), workspace_only=False)

        with patch.object(Path, "rename", side_effect=PermissionError("Permission denied")):
            result = os_manager.on_rename_file_request(request)

        assert isinstance(result, RenameFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.PERMISSION_DENIED


class TestListDirectoryRequest:
    """Test ListDirectoryRequest with failure_reason support."""

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

    def test_list_directory_success(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test successfully listing a directory with sequence grouping disabled."""
        os_manager = griptape_nodes.OSManager()
        # Create some test files
        (temp_dir / "file1.txt").write_text("Content 1")
        (temp_dir / "file2.txt").write_text("Content 2")
        (temp_dir / "subdir").mkdir()

        request = ListDirectoryRequest(directory_path=str(temp_dir), workspace_only=False)
        result = os_manager.on_list_directory_request(request)

        assert isinstance(result, ListDirectoryResultSuccess)
        assert len(result.entries) == 3  # noqa: PLR2004
        names = {entry.name for entry in result.entries}
        assert names == {"file1.txt", "file2.txt", "subdir"}
        assert result.sequences == []

    def test_bounds_clip_entire_sequence_files_stay_in_entries(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Files fully clipped by start_number/end_number remain in entries.

        Regression: consumed_filenames must not be updated before the active-range
        check — otherwise listing removes sequence-member files from entries even
        though no Sequence is returned for them.
        """
        os_manager = griptape_nodes.OSManager()
        (temp_dir / "render.0001.exr").write_text("f1")
        (temp_dir / "render.0002.exr").write_text("f2")

        request = ListDirectoryRequest(
            directory_path=str(temp_dir),
            workspace_only=False,
            group_sequences=True,
            sequence_options=SequenceScanOptions(start_number=100),
        )
        result = os_manager.on_list_directory_request(request)

        assert isinstance(result, ListDirectoryResultSuccess)
        assert result.sequences == []
        entry_names = {e.name for e in result.entries}
        assert "render.0001.exr" in entry_names
        assert "render.0002.exr" in entry_names

    def test_bounds_partial_clip_out_of_range_files_stay_in_entries(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Files outside the active range remain in entries even when a Sequence is returned.

        Regression: consumed_filenames was populated from the full bare_names set
        (all frames in the FileSequence) rather than only the frames within
        [active.first, active.last]. Files clipped by start_number/end_number were
        silently dropped from entries despite never appearing in any Sequence.
        """
        os_manager = griptape_nodes.OSManager()
        for i in range(1, 6):
            (temp_dir / f"render.{i:04d}.exr").write_text(f"f{i}")

        request = ListDirectoryRequest(
            directory_path=str(temp_dir),
            workspace_only=False,
            group_sequences=True,
            sequence_options=SequenceScanOptions(start_number=2),
        )
        result = os_manager.on_list_directory_request(request)

        assert isinstance(result, ListDirectoryResultSuccess)
        assert len(result.sequences) == 1
        seq = result.sequences[0]
        assert seq.first == 2  # noqa: PLR2004
        entry_names = {e.name for e in result.entries}
        # Frame 1 is outside the active range — it must not be consumed
        assert "render.0001.exr" in entry_names

    def test_list_directory_groups_sequences(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test that numbered files are grouped into Sequence objects by default."""
        os_manager = griptape_nodes.OSManager()
        (temp_dir / "render.0001.exr").write_text("frame 1")
        (temp_dir / "render.0002.exr").write_text("frame 2")
        (temp_dir / "render.0003.exr").write_text("frame 3")
        (temp_dir / "readme.txt").write_text("notes")
        (temp_dir / "subdir").mkdir()

        request = ListDirectoryRequest(directory_path=str(temp_dir), workspace_only=False, group_sequences=True)
        result = os_manager.on_list_directory_request(request)

        assert isinstance(result, ListDirectoryResultSuccess)
        # Non-sequence entries only
        entry_names = {e.name for e in result.entries}
        assert "readme.txt" in entry_names
        assert "subdir" in entry_names
        assert "render.0001.exr" not in entry_names
        # Sequence detected
        assert len(result.sequences) == 1
        seq = result.sequences[0]
        assert seq.first == 1
        assert seq.last == 3  # noqa: PLR2004

    def test_single_sequence_file_stays_in_entries(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """A lone file that looks like a sequence pattern must not be grouped into a Sequence."""
        os_manager = griptape_nodes.OSManager()
        (temp_dir / "render.0001.exr").write_text("frame 1")
        (temp_dir / "readme.txt").write_text("notes")

        request = ListDirectoryRequest(directory_path=str(temp_dir), workspace_only=False, group_sequences=True)
        result = os_manager.on_list_directory_request(request)

        assert isinstance(result, ListDirectoryResultSuccess)
        assert result.sequences == []
        entry_names = {e.name for e in result.entries}
        assert "render.0001.exr" in entry_names
        assert "readme.txt" in entry_names

    def test_list_directory_hidden_files(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test listing directory with hidden files."""
        os_manager = griptape_nodes.OSManager()
        (temp_dir / "visible.txt").write_text("Content")
        (temp_dir / ".hidden").write_text("Hidden")

        # Without show_hidden
        request = ListDirectoryRequest(directory_path=str(temp_dir), show_hidden=False, workspace_only=False)
        result = os_manager.on_list_directory_request(request)

        assert isinstance(result, ListDirectoryResultSuccess)
        assert len(result.entries) == 1
        assert result.entries[0].name == "visible.txt"

        # With show_hidden
        request = ListDirectoryRequest(directory_path=str(temp_dir), show_hidden=True, workspace_only=False)
        result = os_manager.on_list_directory_request(request)

        assert isinstance(result, ListDirectoryResultSuccess)
        assert len(result.entries) == 2  # noqa: PLR2004

    def test_list_directory_not_found(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test listing non-existent directory."""
        os_manager = griptape_nodes.OSManager()
        request = ListDirectoryRequest(directory_path=str(temp_dir / "nonexistent"), workspace_only=False)

        result = os_manager.on_list_directory_request(request)

        assert isinstance(result, ListDirectoryResultFailure)
        assert result.failure_reason == FileIOFailureReason.FILE_NOT_FOUND

    def test_list_directory_not_a_directory(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test listing a file instead of directory."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "file.txt"
        file_path.write_text("Content")

        request = ListDirectoryRequest(directory_path=str(file_path), workspace_only=False)
        result = os_manager.on_list_directory_request(request)

        assert isinstance(result, ListDirectoryResultFailure)
        assert result.failure_reason == FileIOFailureReason.INVALID_PATH

    def test_list_directory_permission_denied(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test permission denied when listing directory."""
        os_manager = griptape_nodes.OSManager()
        request = ListDirectoryRequest(directory_path=str(temp_dir), workspace_only=False)

        # Mock os.scandir() instead of Path.iterdir() since we now use os.scandir() for better performance
        # os.scandir() is used as a context manager, so we need to make it raise PermissionError when called
        with patch(
            "griptape_nodes.retained_mode.managers.os_manager.os.scandir",
            side_effect=PermissionError("Permission denied"),
        ):
            result = os_manager.on_list_directory_request(request)

        assert isinstance(result, ListDirectoryResultFailure)
        assert result.failure_reason == FileIOFailureReason.PERMISSION_DENIED

    def test_list_directory_symlink_to_file_preserves_symlink_path(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Symlink-to-file entries return the symlink path, not the resolved target."""
        os_manager = griptape_nodes.OSManager()
        target_file = temp_dir / "target.txt"
        target_file.write_text("content")
        symlink_file = temp_dir / "link.txt"
        try:
            symlink_file.symlink_to(target_file)
        except OSError:
            pytest.skip("Symlink creation not supported (e.g. Windows without Developer Mode)")

        request = ListDirectoryRequest(
            directory_path=str(temp_dir),
            workspace_only=False,
            include_absolute_path=True,
        )
        result = os_manager.on_list_directory_request(request)

        assert isinstance(result, ListDirectoryResultSuccess)
        link_entry = next(e for e in result.entries if e.name == "link.txt")
        symlink_path = Path(link_entry.absolute_path)
        assert symlink_path.is_symlink()
        assert symlink_path.resolve() == target_file.resolve()
        assert str(symlink_path) == str(symlink_file.absolute())

    def test_list_directory_symlink_to_directory_preserves_symlink_path(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Symlink-to-directory entries return the symlink path, not the resolved target."""
        os_manager = griptape_nodes.OSManager()
        target_dir = temp_dir / "target_dir"
        target_dir.mkdir()
        (target_dir / "nested.txt").write_text("nested")
        symlink_dir = temp_dir / "link_dir"
        try:
            symlink_dir.symlink_to(target_dir)
        except OSError:
            pytest.skip("Symlink creation not supported (e.g. Windows without Developer Mode)")

        request = ListDirectoryRequest(
            directory_path=str(temp_dir),
            workspace_only=False,
            include_absolute_path=True,
        )
        result = os_manager.on_list_directory_request(request)

        assert isinstance(result, ListDirectoryResultSuccess)
        link_entry = next(e for e in result.entries if e.name == "link_dir")
        symlink_path = Path(link_entry.absolute_path)
        assert symlink_path.is_symlink()
        assert symlink_path.resolve() == target_dir.resolve()
        assert str(symlink_path) == str(symlink_dir.absolute())


class TestListDirectorySequencesRequest:
    """Test ListDirectorySequencesRequest — sequences-only result."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture(autouse=True)
    def setup_workspace(self, temp_dir: Path, griptape_nodes: GriptapeNodes) -> Generator[None, None, None]:
        original_workspace = griptape_nodes.ConfigManager().workspace_path
        griptape_nodes.ConfigManager().workspace_path = temp_dir
        yield
        griptape_nodes.ConfigManager().workspace_path = original_workspace

    def test_returns_only_sequences(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Non-sequence files are absent from the result; sequences are present."""
        os_manager = griptape_nodes.OSManager()
        (temp_dir / "render.0001.exr").write_text("f1")
        (temp_dir / "render.0002.exr").write_text("f2")
        (temp_dir / "readme.txt").write_text("notes")
        (temp_dir / "subdir").mkdir()

        request = ListDirectorySequencesRequest(directory_path=str(temp_dir), workspace_only=False)
        result = os_manager.on_list_directory_sequences_request(request)

        assert isinstance(result, ListDirectorySequencesResultSuccess)
        assert len(result.sequences) == 1
        seq = result.sequences[0]
        assert seq.first == 1
        assert seq.last == 2  # noqa: PLR2004

    def test_empty_directory_returns_success_with_no_sequences(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """A directory with no sequences returns an empty success, not a failure."""
        os_manager = griptape_nodes.OSManager()
        (temp_dir / "readme.txt").write_text("notes")

        request = ListDirectorySequencesRequest(directory_path=str(temp_dir), workspace_only=False)
        result = os_manager.on_list_directory_sequences_request(request)

        assert isinstance(result, ListDirectorySequencesResultSuccess)
        assert result.sequences == []

    def test_padding_filter_excludes_mismatched_sequences(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """sequence_options.padding=4 keeps only #### sequences; ### sequences are excluded."""
        os_manager = griptape_nodes.OSManager()
        (temp_dir / "hi.0001.exr").write_text("f1")  # 4-digit
        (temp_dir / "hi.0002.exr").write_text("f2")
        (temp_dir / "lo.001.exr").write_text("f1")  # 3-digit
        (temp_dir / "lo.002.exr").write_text("f2")

        request = ListDirectorySequencesRequest(
            directory_path=str(temp_dir),
            workspace_only=False,
            sequence_options=SequenceScanOptions(padding=4),
        )
        result = os_manager.on_list_directory_sequences_request(request)

        assert isinstance(result, ListDirectorySequencesResultSuccess)
        assert len(result.sequences) == 1
        assert result.sequences[0].padding == 4  # noqa: PLR2004

    def test_delegates_failure_from_inner_request(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """A bad directory path surfaces as a failure result."""
        os_manager = griptape_nodes.OSManager()
        request = ListDirectorySequencesRequest(directory_path=str(temp_dir / "nonexistent"), workspace_only=False)
        from griptape_nodes.retained_mode.events.os_events import ListDirectorySequencesResultFailure

        result = os_manager.on_list_directory_sequences_request(request)

        assert isinstance(result, ListDirectorySequencesResultFailure)
        assert result.failure_reason == FileIOFailureReason.FILE_NOT_FOUND


class TestDeduceSequencesFromFileListRequest:
    """Test DeduceSequencesFromFileListRequest — no-I/O sequence detection."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture(autouse=True)
    def setup_workspace(self, temp_dir: Path, griptape_nodes: GriptapeNodes) -> Generator[None, None, None]:
        original_workspace = griptape_nodes.ConfigManager().workspace_path
        griptape_nodes.ConfigManager().workspace_path = temp_dir
        yield
        griptape_nodes.ConfigManager().workspace_path = original_workspace

    def _write_frames(self, directory: Path, basename: str, ext: str, frames: list[int]) -> list[str]:
        paths = []
        for n in frames:
            p = directory / f"{basename}.{n:04d}.{ext}"
            p.write_text(f"frame {n}")
            paths.append(str(p))
        return paths

    def test_detects_sequence_from_absolute_paths(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """A list of absolute file paths is grouped into a Sequence."""
        os_manager = griptape_nodes.OSManager()
        paths = self._write_frames(temp_dir, "render", "exr", [1, 2, 3])

        request = DeduceSequencesFromFileListRequest(file_paths=paths)
        result = os_manager.on_deduce_sequences_from_file_list_request(request)

        assert isinstance(result, DeduceSequencesFromFileListResultSuccess)
        assert len(result.sequences) == 1
        seq = result.sequences[0]
        assert seq.first == 1
        assert seq.last == 3  # noqa: PLR2004
        assert len(seq.entries) == 3  # noqa: PLR2004

    def test_empty_file_list_returns_empty_success(self, griptape_nodes: GriptapeNodes) -> None:
        """An empty file list returns success with no sequences."""
        os_manager = griptape_nodes.OSManager()
        request = DeduceSequencesFromFileListRequest(file_paths=[])
        result = os_manager.on_deduce_sequences_from_file_list_request(request)

        assert isinstance(result, DeduceSequencesFromFileListResultSuccess)
        assert result.sequences == []

    def test_bare_filenames_yield_empty_directory(self, griptape_nodes: GriptapeNodes) -> None:
        """Bare filenames (no directory component) produce Sequence.directory == ''.

        Path('render.0001.exr').parent is '.', which must be normalised to ''
        so that Sequence.directory and entry paths match the documented contract
        rather than emitting './render.0001.exr'.
        """
        os_manager = griptape_nodes.OSManager()
        request = DeduceSequencesFromFileListRequest(
            file_paths=["render.0001.exr", "render.0002.exr"],
        )
        result = os_manager.on_deduce_sequences_from_file_list_request(request)

        assert isinstance(result, DeduceSequencesFromFileListResultSuccess)
        assert len(result.sequences) == 1
        seq = result.sequences[0]
        assert seq.directory == ""
        assert not any(e.path.startswith("./") for e in seq.entries)

    def test_non_sequence_names_do_not_produce_sequences(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Plain names without numeric tokens produce no sequences (callers filter directories)."""
        os_manager = griptape_nodes.OSManager()
        paths = self._write_frames(temp_dir, "frame", "exr", [1, 2])
        paths.append(str(temp_dir / "subdir"))  # bare name with no sequence token

        request = DeduceSequencesFromFileListRequest(file_paths=paths)
        result = os_manager.on_deduce_sequences_from_file_list_request(request)

        assert isinstance(result, DeduceSequencesFromFileListResultSuccess)
        assert len(result.sequences) == 1  # subdir produces no sequence

    def test_files_from_multiple_directories(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Files from different parent directories are grouped independently."""
        os_manager = griptape_nodes.OSManager()
        dir_a = temp_dir / "a"
        dir_b = temp_dir / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        paths_a = self._write_frames(dir_a, "render", "exr", [1, 2])
        paths_b = self._write_frames(dir_b, "comp", "exr", [10, 11])

        request = DeduceSequencesFromFileListRequest(file_paths=paths_a + paths_b)
        result = os_manager.on_deduce_sequences_from_file_list_request(request)

        assert isinstance(result, DeduceSequencesFromFileListResultSuccess)
        assert len(result.sequences) == 2  # noqa: PLR2004

    def test_no_sequence_files_returns_empty_success(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Files with no sequence tokens return success with an empty sequences list."""
        os_manager = griptape_nodes.OSManager()
        p = temp_dir / "readme.txt"
        p.write_text("notes")

        request = DeduceSequencesFromFileListRequest(file_paths=[str(p)])
        result = os_manager.on_deduce_sequences_from_file_list_request(request)

        assert isinstance(result, DeduceSequencesFromFileListResultSuccess)
        assert result.sequences == []

    def test_padding_filter(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """sequence_options.padding filters to only matching zero-fill width."""
        os_manager = griptape_nodes.OSManager()
        paths_4 = self._write_frames(temp_dir, "hi", "exr", [1, 2])  # 4-digit
        paths_3 = [str(temp_dir / f"lo.{n:03d}.exr") for n in [1, 2]]
        for p in paths_3:
            Path(p).write_text("f")

        request = DeduceSequencesFromFileListRequest(
            file_paths=paths_4 + paths_3,
            sequence_options=SequenceScanOptions(padding=4),
        )
        result = os_manager.on_deduce_sequences_from_file_list_request(request)

        assert isinstance(result, DeduceSequencesFromFileListResultSuccess)
        assert len(result.sequences) == 1
        assert result.sequences[0].padding == 4  # noqa: PLR2004

    def test_frame_bounds(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """start_number / end_number clip the active range."""
        os_manager = griptape_nodes.OSManager()
        paths = self._write_frames(temp_dir, "render", "exr", [1, 2, 3, 4, 5])

        request = DeduceSequencesFromFileListRequest(
            file_paths=paths,
            sequence_options=SequenceScanOptions(
                policy=MissingItemPolicy.SKIP,
                start_number=2,
                end_number=4,
            ),
        )
        result = os_manager.on_deduce_sequences_from_file_list_request(request)

        assert isinstance(result, DeduceSequencesFromFileListResultSuccess)
        assert len(result.sequences) == 1
        seq = result.sequences[0]
        assert seq.first == 2  # noqa: PLR2004
        assert seq.last == 4  # noqa: PLR2004
        assert [e.number for e in seq.entries] == [2, 3, 4]

    def test_bounds_clip_entire_sequence_files_stay_in_entries(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Files whose sequence is fully clipped by bounds are NOT removed from the result.

        Regression: consumed_filenames must not be updated before the active-range
        check, otherwise files that produce no Sequence (because start_number >
        discovered_last) are silently consumed and callers lose them.
        """
        os_manager = griptape_nodes.OSManager()
        paths = self._write_frames(temp_dir, "render", "exr", [1, 2, 3])

        # Ask for frames 100+, which clips the entire on-disk range out.
        request = DeduceSequencesFromFileListRequest(
            file_paths=paths,
            sequence_options=SequenceScanOptions(start_number=100),
        )
        result = os_manager.on_deduce_sequences_from_file_list_request(request)

        assert isinstance(result, DeduceSequencesFromFileListResultSuccess)
        assert result.sequences == []

    def test_invalid_bounds_returns_failure(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """A negative start_number surfaces as INVALID_BOUNDS failure."""
        os_manager = griptape_nodes.OSManager()
        paths = self._write_frames(temp_dir, "render", "exr", [1, 2, 3])

        request = DeduceSequencesFromFileListRequest(
            file_paths=paths,
            sequence_options=SequenceScanOptions(start_number=-1),
        )
        result = os_manager.on_deduce_sequences_from_file_list_request(request)

        assert isinstance(result, DeduceSequencesFromFileListResultFailure)
        assert result.failure_reason == SequenceScanFailureReason.INVALID_BOUNDS

    def test_abort_policy_with_single_gap_returns_failure(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """ABORT policy with exactly one gap returns ABORTED_AT_GAP with a single-gap message."""
        os_manager = griptape_nodes.OSManager()
        paths = self._write_frames(temp_dir, "render", "exr", [1, 3])  # gap at 2

        request = DeduceSequencesFromFileListRequest(
            file_paths=paths,
            sequence_options=SequenceScanOptions(policy=MissingItemPolicy.ABORT),
        )
        result = os_manager.on_deduce_sequences_from_file_list_request(request)

        assert isinstance(result, DeduceSequencesFromFileListResultFailure)
        assert result.failure_reason == SequenceScanFailureReason.ABORTED_AT_GAP
        assert isinstance(result.result_details, ResultDetails)
        assert "gap at item 2" in result.result_details.result_details[0].message

    def test_abort_policy_with_multiple_gaps_returns_failure(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """ABORT policy with multiple gaps lists all gap positions in the message."""
        os_manager = griptape_nodes.OSManager()
        paths = self._write_frames(temp_dir, "render", "exr", [1, 3, 5])  # gaps at 2, 4

        request = DeduceSequencesFromFileListRequest(
            file_paths=paths,
            sequence_options=SequenceScanOptions(policy=MissingItemPolicy.ABORT),
        )
        result = os_manager.on_deduce_sequences_from_file_list_request(request)

        assert isinstance(result, DeduceSequencesFromFileListResultFailure)
        assert result.failure_reason == SequenceScanFailureReason.ABORTED_AT_GAP
        assert isinstance(result.result_details, ResultDetails)
        assert "2 gaps" in result.result_details.result_details[0].message

    def test_abort_policy_with_many_gaps_truncates_preview(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """ABORT with more gaps than ABORTED_AT_GAP_PREVIEW_COUNT appends a '+ N more' suffix."""
        os_manager = griptape_nodes.OSManager()
        # 6 gaps: missing 2, 4, 6, 8, 10, 12 — exceeds the preview count of 5
        paths = self._write_frames(temp_dir, "render", "exr", [1, 3, 5, 7, 9, 11, 13])

        request = DeduceSequencesFromFileListRequest(
            file_paths=paths,
            sequence_options=SequenceScanOptions(policy=MissingItemPolicy.ABORT),
        )
        result = os_manager.on_deduce_sequences_from_file_list_request(request)

        assert isinstance(result, DeduceSequencesFromFileListResultFailure)
        assert result.failure_reason == SequenceScanFailureReason.ABORTED_AT_GAP
        assert isinstance(result.result_details, ResultDetails)
        assert "more" in result.result_details.result_details[0].message

    def test_unexpected_exception_returns_unknown_failure(self, griptape_nodes: GriptapeNodes) -> None:
        """An unexpected exception from the scan is caught and returned as UNKNOWN failure."""
        os_manager = griptape_nodes.OSManager()

        request = DeduceSequencesFromFileListRequest(file_paths=["render.0001.exr"])
        with patch(
            "griptape_nodes.retained_mode.managers.os_manager.scan_sequences_from_filenames",
            side_effect=RuntimeError("boom"),
        ):
            result = os_manager.on_deduce_sequences_from_file_list_request(request)

        assert isinstance(result, DeduceSequencesFromFileListResultFailure)
        assert result.failure_reason == FileIOFailureReason.UNKNOWN
        assert isinstance(result.result_details, ResultDetails)
        assert "boom" in result.result_details.result_details[0].message

    def test_single_frame_sequence_not_returned(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """A sequence with only one present frame is not included in the result."""
        os_manager = griptape_nodes.OSManager()
        paths = self._write_frames(temp_dir, "render", "exr", [1])

        request = DeduceSequencesFromFileListRequest(file_paths=paths)
        result = os_manager.on_deduce_sequences_from_file_list_request(request)

        assert isinstance(result, DeduceSequencesFromFileListResultSuccess)
        assert result.sequences == []

    def test_reject_no_token_behavior_skips_token_less_sequences(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """NoTokenBehavior.REJECT causes token-less files to be silently skipped."""
        os_manager = griptape_nodes.OSManager()
        paths = self._write_frames(temp_dir, "render", "exr", [1, 2])

        request = DeduceSequencesFromFileListRequest(
            file_paths=paths,
            sequence_options=SequenceScanOptions(
                no_token_behavior=NoTokenBehavior.REJECT,
                padding=0,  # only match zero-padded (token-less) sequences
            ),
        )
        result = os_manager.on_deduce_sequences_from_file_list_request(request)

        assert isinstance(result, DeduceSequencesFromFileListResultSuccess)


class TestNormalizePathPartsForSpecialFolder:
    """Test normalize_path_parts_for_special_folder helper."""

    def test_tilde_single_part(self) -> None:
        """~/Downloads -> ['downloads']."""
        result = OSManager.normalize_path_parts_for_special_folder("~/Downloads")
        assert result == ["downloads"]

    def test_tilde_with_slash_single_part(self) -> None:
        """~/Desktop -> ['desktop']."""
        result = OSManager.normalize_path_parts_for_special_folder("~/Desktop")
        assert result == ["desktop"]

    def test_tilde_multiple_parts(self) -> None:
        """~/Desktop/subfolder -> ['desktop', 'subfolder']."""
        result = OSManager.normalize_path_parts_for_special_folder("~/Desktop/subfolder")
        assert result == ["desktop", "subfolder"]

    def test_tilde_only(self) -> None:
        """~ -> [] (no path parts after stripping)."""
        result = OSManager.normalize_path_parts_for_special_folder("~")
        assert result == []

    def test_backslash_normalized_to_slash(self) -> None:
        r"""~\Downloads -> ['downloads']."""
        result = OSManager.normalize_path_parts_for_special_folder("~\\Downloads")
        assert result == ["downloads"]

    def test_empty_string(self) -> None:
        """Empty string -> []."""
        result = OSManager.normalize_path_parts_for_special_folder("")
        assert result == []

    def test_parts_lowercased(self) -> None:
        """Path parts are lowercased."""
        result = OSManager.normalize_path_parts_for_special_folder("~/DOCUMENTS/SubDir")
        assert result == ["documents", "subdir"]

    def test_userprofile_desktop_normalizes_to_desktop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        r"""%UserProfile%\Desktop -> ['desktop']; expandvars can return backslashes on Windows."""
        monkeypatch.setenv("USERPROFILE", "C:\\Users\\jason")

        def expandvars_windows_style(path: str) -> str:
            if "%UserProfile%" in path or "%USERPROFILE%" in path:
                return path.replace("%UserProfile%", "C:\\Users\\jason").replace("%USERPROFILE%", "C:\\Users\\jason")
            return os.path.expandvars(path)

        with patch("griptape_nodes.retained_mode.managers.os_manager.os.path.expandvars", expandvars_windows_style):
            result = OSManager.normalize_path_parts_for_special_folder("%UserProfile%/Desktop")
        assert result == ["desktop"]

    def test_userprofile_downloads_with_subdir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        r"""%UserProfile%\Downloads\sub -> ['downloads', 'sub']."""
        monkeypatch.setenv("USERPROFILE", "C:\\Users\\jason")

        def expandvars_windows_style(path: str) -> str:
            if "%UserProfile%" in path or "%USERPROFILE%" in path:
                return path.replace("%UserProfile%", "C:\\Users\\jason").replace("%USERPROFILE%", "C:\\Users\\jason")
            return os.path.expandvars(path)

        with patch("griptape_nodes.retained_mode.managers.os_manager.os.path.expandvars", expandvars_windows_style):
            result = OSManager.normalize_path_parts_for_special_folder("%UserProfile%/Downloads/sub")
        assert result == ["downloads", "sub"]


class TestTryResolveWindowsSpecialFolder:
    """Test try_resolve_windows_special_folder helper."""

    def test_unknown_folder_returns_none(self, griptape_nodes: GriptapeNodes) -> None:
        """Unknown first part returns None."""
        os_manager = griptape_nodes.OSManager()
        result = os_manager.try_resolve_windows_special_folder(["unknown", "sub"])
        assert result is None

    def test_empty_parts_returns_none(self, griptape_nodes: GriptapeNodes) -> None:
        """Empty parts returns None."""
        os_manager = griptape_nodes.OSManager()
        result = os_manager.try_resolve_windows_special_folder([])
        assert result is None

    def test_downloads_resolved_returns_path_and_empty_remaining(self, griptape_nodes: GriptapeNodes) -> None:
        """Known folder with no remaining parts."""
        os_manager = griptape_nodes.OSManager()
        mock_path = Path("/mock/Downloads")

        def mock_get(csidl: int) -> Path:
            assert csidl == OSManager.WINDOWS_CSIDL_MAP["downloads"]
            return mock_path

        with patch.object(os_manager, "_get_windows_special_folder_path", side_effect=mock_get):
            result = os_manager.try_resolve_windows_special_folder(["downloads"])
        assert result is not None
        assert result.special_path == mock_path
        assert result.remaining_parts == []

    def test_desktop_with_remaining_parts(self, griptape_nodes: GriptapeNodes) -> None:
        """Known folder with remaining parts."""
        os_manager = griptape_nodes.OSManager()
        mock_path = Path("/mock/Desktop")

        def mock_get(csidl: int) -> Path:
            assert csidl == OSManager.WINDOWS_CSIDL_MAP["desktop"]
            return mock_path

        with patch.object(os_manager, "_get_windows_special_folder_path", side_effect=mock_get):
            result = os_manager.try_resolve_windows_special_folder(["desktop", "sub", "file.txt"])
        assert result is not None
        assert result.special_path == mock_path
        assert result.remaining_parts == ["sub", "file.txt"]

    def test_get_folder_raises_returns_none(self, griptape_nodes: GriptapeNodes) -> None:
        """When _get_windows_special_folder_path raises WindowsSpecialFolderError, result is None."""
        os_manager = griptape_nodes.OSManager()
        with patch.object(
            os_manager, "_get_windows_special_folder_path", side_effect=WindowsSpecialFolderError("mock")
        ):
            result = os_manager.try_resolve_windows_special_folder(["downloads"])
        assert result is None


class TestExpandPath:
    """Test OSManager._expand_path integration."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture(autouse=True)
    def setup_workspace(self, temp_dir: Path, griptape_nodes: GriptapeNodes) -> Generator[None, None, None]:
        """Set workspace to temp_dir for tests."""
        original_workspace = griptape_nodes.ConfigManager().workspace_path
        griptape_nodes.ConfigManager().workspace_path = temp_dir
        yield
        griptape_nodes.ConfigManager().workspace_path = original_workspace

    def test_expand_path_relative_resolved_against_cwd(
        self,
        griptape_nodes: GriptapeNodes,
        temp_dir: Path,  # noqa: ARG002
    ) -> None:
        """Relative path is resolved against current working directory."""
        os_manager = griptape_nodes.OSManager()
        result = os_manager._expand_path("subdir")
        # resolve_path_safely resolves relative paths against Path.cwd()
        assert result.is_absolute()
        assert result.name == "subdir"

    def test_expand_path_expands_vars_and_tilde(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Expandvars and expanduser are applied when not a Windows special folder."""
        os_manager = griptape_nodes.OSManager()
        # Use a path that won't match Windows special folder logic on this platform
        result = os_manager._expand_path(str(temp_dir))
        assert result == temp_dir or result.resolve() == temp_dir.resolve()

    @pytest.mark.skipif(platform.system() != "Windows", reason="Windows-specific special folder test")
    def test_expand_path_windows_special_folder_mocked(
        self,
        griptape_nodes: GriptapeNodes,
        temp_dir: Path,  # noqa: ARG002
    ) -> None:
        """On Windows, special folder is resolved via Shell API when path is ~/Downloads."""
        os_manager = griptape_nodes.OSManager()
        mock_downloads = Path("C:/mock/Downloads")

        with patch.object(os_manager, "_get_windows_special_folder_path", return_value=mock_downloads) as mock_get:
            result = os_manager._expand_path("~/Downloads")
            mock_get.assert_called_once()
            assert result == resolve_path_safely(mock_downloads)

    def test_expand_path_non_windows_uses_expanduser(
        self,
        griptape_nodes: GriptapeNodes,
        temp_dir: Path,  # noqa: ARG002
    ) -> None:
        """On non-Windows, ~/path uses expanduser (no special folder logic)."""
        if platform.system() == "Windows":
            pytest.skip("Non-Windows test")
        os_manager = griptape_nodes.OSManager()
        result = os_manager._expand_path("~/Downloads")
        expected = resolve_path_safely(Path.home() / "Downloads")
        assert result == expected


class TestWindowsLongPathHandling:
    r"""Test Windows long path handling with \\?\ prefix."""

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

    @pytest.fixture
    def long_path(self, temp_dir: Path) -> Path:
        """Create a path longer than 260 characters."""
        # Create a path component that when repeated will exceed 260 chars
        long_component = "a" * 50
        path_parts = [temp_dir] + [long_component] * 6  # Will exceed 260 chars
        return Path(*path_parts)

    def test_normalize_path_short_path(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:  # noqa: ARG002
        """Test that short paths are not modified."""
        short_path = temp_dir / "short.txt"
        result = normalize_path_for_platform(short_path)

        # Should return string without \\?\ prefix
        assert not result.startswith("\\\\?\\")

    @pytest.mark.skipif(platform.system() != "Windows", reason="Windows-specific test")
    def test_normalize_path_long_path_windows(self, griptape_nodes: GriptapeNodes, long_path: Path) -> None:  # noqa: ARG002
        r"""Test that long paths on Windows get \\?\ prefix."""
        result = normalize_path_for_platform(long_path)

        # On Windows, long paths should get the prefix
        if len(str(long_path.resolve())) >= WINDOWS_MAX_PATH:
            assert result.startswith("\\\\?\\")

    @pytest.mark.skipif(platform.system() == "Windows", reason="Non-Windows test")
    def test_normalize_path_long_path_non_windows(self, griptape_nodes: GriptapeNodes, long_path: Path) -> None:  # noqa: ARG002
        """Test that long paths on non-Windows don't get prefix."""
        result = normalize_path_for_platform(long_path)

        # On non-Windows, no prefix should be added
        assert not result.startswith("\\\\?\\")

    def test_write_file_with_long_path(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test writing file with long path works correctly."""
        os_manager = griptape_nodes.OSManager()
        # Create a moderately long path (not exceeding OS limits)
        subdir = temp_dir / ("a" * 30) / ("b" * 30) / ("c" * 30)
        file_path = subdir / "test.txt"

        request = WriteFileRequest(file_path=str(file_path), content="Content", create_parents=True)
        result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultSuccess)
        # The returned path should not contain \\?\ prefix
        assert not result.final_file_path.startswith("\\\\?\\")
        # But the file should exist
        assert file_path.exists()


class TestDeleteFileRequest:
    """Test DeleteFileRequest with various scenarios."""

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

    @pytest.mark.asyncio
    async def test_delete_file_success(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test successfully deleting a file."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "test.txt"
        file_path.write_text("test content")
        request = DeleteFileRequest(path=str(file_path), workspace_only=False)

        result = await os_manager.on_delete_file_request(request)

        assert isinstance(result, DeleteFileResultSuccess)
        # Compare resolved paths to handle symlinks (e.g., /var -> /private/var on macOS)
        assert await anyio.Path(result.deleted_path).resolve() == file_path.resolve()
        assert result.was_directory is False
        assert len(result.deleted_paths) == 1
        assert await anyio.Path(result.deleted_paths[0]).resolve() == file_path.resolve()
        assert not file_path.exists()

    @pytest.mark.asyncio
    async def test_delete_empty_directory(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test deleting an empty directory."""
        os_manager = griptape_nodes.OSManager()
        dir_path = temp_dir / "testdir"
        dir_path.mkdir()
        request = DeleteFileRequest(path=str(dir_path), workspace_only=False)

        result = await os_manager.on_delete_file_request(request)

        assert isinstance(result, DeleteFileResultSuccess)
        assert result.was_directory is True
        assert len(result.deleted_paths) >= 1
        assert str(dir_path) in result.deleted_paths or str(dir_path.resolve()) in result.deleted_paths
        assert not dir_path.exists()

    @pytest.mark.asyncio
    async def test_delete_directory_with_contents(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test deleting a directory with contents."""
        os_manager = griptape_nodes.OSManager()
        dir_path = temp_dir / "testdir"
        dir_path.mkdir()
        (dir_path / "file1.txt").write_text("content1")
        (dir_path / "file2.txt").write_text("content2")
        subdir = dir_path / "subdir"
        subdir.mkdir()
        (subdir / "file3.txt").write_text("content3")

        request = DeleteFileRequest(path=str(dir_path), workspace_only=False)

        result = await os_manager.on_delete_file_request(request)

        assert isinstance(result, DeleteFileResultSuccess)
        assert result.was_directory is True
        expected_items = 4  # dir + 2 files + subdir + 1 file
        assert len(result.deleted_paths) >= expected_items
        # Verify that all expected paths are in the deleted_paths list
        assert any(str(dir_path / "file1.txt") in path for path in result.deleted_paths)
        assert any(str(dir_path / "file2.txt") in path for path in result.deleted_paths)
        assert any(str(subdir / "file3.txt") in path for path in result.deleted_paths)
        assert not dir_path.exists()

    @pytest.mark.asyncio
    async def test_delete_nonexistent_file_fails(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test that deleting a nonexistent file fails."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "nonexistent.txt"
        request = DeleteFileRequest(path=str(file_path), workspace_only=False)

        result = await os_manager.on_delete_file_request(request)

        assert isinstance(result, DeleteFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.FILE_NOT_FOUND

    @pytest.mark.asyncio
    async def test_delete_invalid_path_fails(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that deleting with neither path nor file_entry fails."""
        os_manager = griptape_nodes.OSManager()
        request = DeleteFileRequest(path=None, file_entry=None)

        result = await os_manager.on_delete_file_request(request)

        assert isinstance(result, DeleteFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.INVALID_PATH

    @pytest.mark.asyncio
    async def test_delete_with_permission_error(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test that permission errors are properly handled."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "test.txt"
        file_path.write_text("test content")

        with patch.object(anyio.Path, "unlink", AsyncMock(side_effect=PermissionError("Access denied"))):
            request = DeleteFileRequest(
                path=str(file_path),
                workspace_only=False,
                deletion_behavior=DeletionBehavior.PERMANENTLY_DELETE,
            )
            result = await os_manager.on_delete_file_request(request)

        assert isinstance(result, DeleteFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.PERMISSION_DENIED

    @pytest.mark.asyncio
    async def test_delete_file_behavior_permanently_delete(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test default PERMANENTLY_DELETE behavior for files."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "test.txt"
        file_path.write_text("test content")
        request = DeleteFileRequest(
            path=str(file_path),
            workspace_only=False,
            deletion_behavior=DeletionBehavior.PERMANENTLY_DELETE,
        )

        result = await os_manager.on_delete_file_request(request)

        assert isinstance(result, DeleteFileResultSuccess)
        assert result.outcome == DeletionOutcome.PERMANENTLY_DELETED
        assert not file_path.exists()

    @pytest.mark.asyncio
    async def test_delete_directory_behavior_permanently_delete(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Test default PERMANENTLY_DELETE behavior for directories."""
        os_manager = griptape_nodes.OSManager()
        dir_path = temp_dir / "testdir"
        dir_path.mkdir()
        (dir_path / "file1.txt").write_text("content1")
        request = DeleteFileRequest(
            path=str(dir_path),
            workspace_only=False,
            deletion_behavior=DeletionBehavior.PERMANENTLY_DELETE,
        )

        result = await os_manager.on_delete_file_request(request)

        assert isinstance(result, DeleteFileResultSuccess)
        assert result.outcome == DeletionOutcome.PERMANENTLY_DELETED
        assert result.was_directory is True
        assert not dir_path.exists()

    @pytest.mark.asyncio
    async def test_delete_file_behavior_recycle_bin_only_success(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Test RECYCLE_BIN_ONLY behavior successfully sends file to recycle bin."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "test.txt"
        file_path.write_text("test content")

        with patch("griptape_nodes.retained_mode.managers.os_manager.send2trash") as mock_send2trash:
            mock_send2trash.TrashPermissionError = send2trash.TrashPermissionError
            mock_send2trash.send2trash.return_value = None
            request = DeleteFileRequest(
                path=str(file_path),
                workspace_only=False,
                deletion_behavior=DeletionBehavior.RECYCLE_BIN_ONLY,
            )

            result = await os_manager.on_delete_file_request(request)

        assert isinstance(result, DeleteFileResultSuccess)
        assert result.outcome == DeletionOutcome.SENT_TO_RECYCLE_BIN
        mock_send2trash.send2trash.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_directory_behavior_recycle_bin_only_success(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Test RECYCLE_BIN_ONLY behavior successfully sends directory to recycle bin."""
        os_manager = griptape_nodes.OSManager()
        dir_path = temp_dir / "testdir"
        dir_path.mkdir()
        (dir_path / "file1.txt").write_text("content1")

        with patch("griptape_nodes.retained_mode.managers.os_manager.send2trash") as mock_send2trash:
            mock_send2trash.TrashPermissionError = send2trash.TrashPermissionError
            mock_send2trash.send2trash.return_value = None
            request = DeleteFileRequest(
                path=str(dir_path),
                workspace_only=False,
                deletion_behavior=DeletionBehavior.RECYCLE_BIN_ONLY,
            )

            result = await os_manager.on_delete_file_request(request)

        assert isinstance(result, DeleteFileResultSuccess)
        assert result.outcome == DeletionOutcome.SENT_TO_RECYCLE_BIN
        assert result.was_directory is True
        mock_send2trash.send2trash.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_file_behavior_recycle_bin_only_failure(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Test RECYCLE_BIN_ONLY behavior returns failure when recycle bin unavailable."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "test.txt"
        file_path.write_text("test content")

        with patch("griptape_nodes.retained_mode.managers.os_manager.send2trash") as mock_send2trash:
            mock_send2trash.TrashPermissionError = send2trash.TrashPermissionError
            mock_send2trash.send2trash.side_effect = OSError("I/O error")
            request = DeleteFileRequest(
                path=str(file_path),
                workspace_only=False,
                deletion_behavior=DeletionBehavior.RECYCLE_BIN_ONLY,
            )

            result = await os_manager.on_delete_file_request(request)

        assert isinstance(result, DeleteFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.IO_ERROR

    @pytest.mark.asyncio
    async def test_delete_directory_behavior_recycle_bin_only_failure(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Test RECYCLE_BIN_ONLY behavior returns failure for directories when I/O error occurs."""
        os_manager = griptape_nodes.OSManager()
        dir_path = temp_dir / "testdir"
        dir_path.mkdir()
        (dir_path / "file1.txt").write_text("content1")

        with patch("griptape_nodes.retained_mode.managers.os_manager.send2trash") as mock_send2trash:
            mock_send2trash.TrashPermissionError = send2trash.TrashPermissionError
            mock_send2trash.send2trash.side_effect = OSError("I/O error")
            request = DeleteFileRequest(
                path=str(dir_path),
                workspace_only=False,
                deletion_behavior=DeletionBehavior.RECYCLE_BIN_ONLY,
            )

            result = await os_manager.on_delete_file_request(request)

        assert isinstance(result, DeleteFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.IO_ERROR

    @pytest.mark.asyncio
    async def test_delete_file_behavior_prefer_recycle_bin_uses_trash(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Test PREFER_RECYCLE_BIN behavior uses recycle bin when available."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "test.txt"
        file_path.write_text("test content")

        with patch("griptape_nodes.retained_mode.managers.os_manager.send2trash") as mock_send2trash:
            mock_send2trash.TrashPermissionError = send2trash.TrashPermissionError
            mock_send2trash.send2trash.return_value = None
            request = DeleteFileRequest(
                path=str(file_path),
                workspace_only=False,
                deletion_behavior=DeletionBehavior.PREFER_RECYCLE_BIN,
            )

            result = await os_manager.on_delete_file_request(request)

        assert isinstance(result, DeleteFileResultSuccess)
        assert result.outcome == DeletionOutcome.SENT_TO_RECYCLE_BIN
        mock_send2trash.send2trash.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_directory_behavior_prefer_recycle_bin_uses_trash(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Test PREFER_RECYCLE_BIN behavior uses recycle bin for directories when available."""
        os_manager = griptape_nodes.OSManager()
        dir_path = temp_dir / "testdir"
        dir_path.mkdir()
        (dir_path / "file1.txt").write_text("content1")

        with patch("griptape_nodes.retained_mode.managers.os_manager.send2trash") as mock_send2trash:
            mock_send2trash.TrashPermissionError = send2trash.TrashPermissionError
            mock_send2trash.send2trash.return_value = None
            request = DeleteFileRequest(
                path=str(dir_path),
                workspace_only=False,
                deletion_behavior=DeletionBehavior.PREFER_RECYCLE_BIN,
            )

            result = await os_manager.on_delete_file_request(request)

        assert isinstance(result, DeleteFileResultSuccess)
        assert result.outcome == DeletionOutcome.SENT_TO_RECYCLE_BIN
        assert result.was_directory is True
        mock_send2trash.send2trash.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_file_behavior_prefer_recycle_bin_falls_back(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Test PREFER_RECYCLE_BIN behavior falls back to permanent deletion when trash fails."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "test.txt"
        file_path.write_text("test content")

        with patch("griptape_nodes.retained_mode.managers.os_manager.send2trash") as mock_send2trash:
            mock_send2trash.TrashPermissionError = send2trash.TrashPermissionError
            mock_send2trash.send2trash.side_effect = OSError("Recycle bin unavailable")
            request = DeleteFileRequest(
                path=str(file_path),
                workspace_only=False,
                deletion_behavior=DeletionBehavior.PREFER_RECYCLE_BIN,
            )

            result = await os_manager.on_delete_file_request(request)

        assert isinstance(result, DeleteFileResultSuccess)
        assert result.outcome == DeletionOutcome.PERMANENTLY_DELETED
        assert not file_path.exists()
        # Verify result_details is WARNING level
        assert isinstance(result.result_details, ResultDetails)

    @pytest.mark.asyncio
    async def test_delete_directory_behavior_prefer_recycle_bin_falls_back(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Test PREFER_RECYCLE_BIN behavior falls back to permanent deletion for directories when trash fails."""
        os_manager = griptape_nodes.OSManager()
        dir_path = temp_dir / "testdir"
        dir_path.mkdir()
        (dir_path / "file1.txt").write_text("content1")

        with patch("griptape_nodes.retained_mode.managers.os_manager.send2trash") as mock_send2trash:
            mock_send2trash.TrashPermissionError = send2trash.TrashPermissionError
            mock_send2trash.send2trash.side_effect = OSError("Recycle bin unavailable")
            request = DeleteFileRequest(
                path=str(dir_path),
                workspace_only=False,
                deletion_behavior=DeletionBehavior.PREFER_RECYCLE_BIN,
            )

            result = await os_manager.on_delete_file_request(request)

        assert isinstance(result, DeleteFileResultSuccess)
        assert result.outcome == DeletionOutcome.PERMANENTLY_DELETED
        assert result.was_directory is True
        assert not dir_path.exists()
        # Verify result_details is WARNING level
        assert isinstance(result.result_details, ResultDetails)

    @pytest.mark.asyncio
    async def test_delete_outcome_default_is_sent_to_recycle_bin(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Test that default deletion (no behavior specified) reports SENT_TO_RECYCLE_BIN outcome."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "test.txt"
        file_path.write_text("test content")
        request = DeleteFileRequest(path=str(file_path), workspace_only=False)

        result = await os_manager.on_delete_file_request(request)

        assert isinstance(result, DeleteFileResultSuccess)
        assert result.outcome == DeletionOutcome.SENT_TO_RECYCLE_BIN


class TestGetFileInfoRequest:
    """Test GetFileInfoRequest with various scenarios."""

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

    def test_get_file_info_success(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test successfully getting file info."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "test.txt"
        file_path.write_text("test content")
        request = GetFileInfoRequest(path=str(file_path), workspace_only=False)

        result = os_manager.on_get_file_info_request(request)

        assert isinstance(result, GetFileInfoResultSuccess)
        assert result.file_entry is not None
        assert result.file_entry.is_dir is False
        assert result.file_entry.name == "test.txt"
        assert result.file_entry.size > 0
        assert result.file_entry.mime_type is not None

    def test_get_directory_info_success(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test successfully getting directory info."""
        os_manager = griptape_nodes.OSManager()
        dir_path = temp_dir / "testdir"
        dir_path.mkdir()
        request = GetFileInfoRequest(path=str(dir_path), workspace_only=False)

        result = os_manager.on_get_file_info_request(request)

        assert isinstance(result, GetFileInfoResultSuccess)
        assert result.file_entry is not None
        assert result.file_entry.is_dir is True
        assert result.file_entry.name == "testdir"
        assert result.file_entry.mime_type is None

    def test_get_file_info_nonexistent_returns_none(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test that getting info for nonexistent path returns success with file_entry=None."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "nonexistent.txt"
        request = GetFileInfoRequest(path=str(file_path), workspace_only=False)

        result = os_manager.on_get_file_info_request(request)

        assert isinstance(result, GetFileInfoResultSuccess)
        assert result.file_entry is None

    def test_get_file_info_empty_path_fails(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that empty path fails."""
        os_manager = griptape_nodes.OSManager()
        request = GetFileInfoRequest(path="", workspace_only=False)

        result = os_manager.on_get_file_info_request(request)

        assert isinstance(result, GetFileInfoResultFailure)
        assert result.failure_reason == FileIOFailureReason.INVALID_PATH


class TestFileIOFailureReasons:
    """Test that all failure reasons are properly set."""

    def test_all_failure_reasons_have_valid_values(self) -> None:
        """Test that all FileIOFailureReason enum values are strings."""
        for reason in FileIOFailureReason:
            assert isinstance(reason.value, str)
            assert len(reason.value) > 0

    def test_failure_reason_uniqueness(self) -> None:
        """Test that all failure reason values are unique."""
        values = [reason.value for reason in FileIOFailureReason]
        assert len(values) == len(set(values))


class TestCreateNewFilePolicy:
    """Test CREATE_NEW file policy with auto-incrementing filenames."""

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

    def test_create_new_first_file(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test CREATE_NEW policy creates file with requested name if available."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "test.txt"
        request = WriteFileRequest(
            file_path=str(file_path),
            content="First file",
            existing_file_policy=ExistingFilePolicy.CREATE_NEW,
        )

        result = os_manager.on_write_file_request(request)

        assert isinstance(result, WriteFileResultSuccess)
        # First file should use requested name (test.txt) since it's available
        expected_path = temp_dir / "test.txt"
        assert Path(result.final_file_path).resolve() == expected_path.resolve()
        assert expected_path.read_text() == "First file"

    def test_create_new_increments_suffix(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test CREATE_NEW policy increments suffix for subsequent files."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "output.txt"

        # Create first file (gets output.txt since it's available)
        request1 = WriteFileRequest(
            file_path=str(file_path),
            content="File 1",
            existing_file_policy=ExistingFilePolicy.CREATE_NEW,
        )
        result1 = os_manager.on_write_file_request(request1)
        assert isinstance(result1, WriteFileResultSuccess)
        assert (temp_dir / "output.txt").exists()

        # Create second file (gets output_1.txt since output.txt now exists)
        request2 = WriteFileRequest(
            file_path=str(file_path),
            content="File 2",
            existing_file_policy=ExistingFilePolicy.CREATE_NEW,
        )
        result2 = os_manager.on_write_file_request(request2)
        assert isinstance(result2, WriteFileResultSuccess)
        assert (temp_dir / "output_1.txt").exists()

        # Create third file (gets output_2.txt)
        request3 = WriteFileRequest(
            file_path=str(file_path),
            content="File 3",
            existing_file_policy=ExistingFilePolicy.CREATE_NEW,
        )
        result3 = os_manager.on_write_file_request(request3)
        assert isinstance(result3, WriteFileResultSuccess)
        assert (temp_dir / "output_2.txt").exists()

    def test_create_new_fills_gaps(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Test CREATE_NEW policy fills gaps in sequence."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "render.png"

        # Create render.png and files with gaps manually
        (temp_dir / "render.png").write_text("Original")
        (temp_dir / "render_1.png").write_text("File 1")
        (temp_dir / "render_5.png").write_text("File 5")

        # CREATE_NEW should fill gap at _2
        request = WriteFileRequest(
            file_path=str(file_path),
            content="File 2",
            existing_file_policy=ExistingFilePolicy.CREATE_NEW,
        )
        result = os_manager.on_write_file_request(request)
        assert isinstance(result, WriteFileResultSuccess)
        expected_path = temp_dir / "render_2.png"
        assert Path(result.final_file_path).resolve() == expected_path.resolve()

    def test_create_new_with_fully_resolved_macro_should_use_suffix_injection(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Test CREATE_NEW policy with fully-resolved MacroPath falls back to suffix injection.

        When a MacroPath has all variables resolved and no index variable, the CREATE_NEW
        policy should fall back to parsing the filename and adding _N suffix. Currently
        this fails with "no index variable found".
        """
        from griptape_nodes.common.macro_parser import ParsedMacro
        from griptape_nodes.retained_mode.events.project_events import MacroPath

        os_manager = griptape_nodes.OSManager()

        # Create first file manually
        first_file = temp_dir / "render.png"
        first_file.write_text("Original")

        # Use MacroPath with all variables resolved (no index variable)
        macro_path = MacroPath(
            parsed_macro=ParsedMacro(f"{temp_dir}/render.png"),
            variables={},  # No variables to resolve
        )

        # This should fall back to suffix injection and create render_1.png
        request = WriteFileRequest(
            file_path=macro_path,
            content="Second file",
            existing_file_policy=ExistingFilePolicy.CREATE_NEW,
        )

        result = os_manager.on_write_file_request(request)

        # EXPECTED: Should succeed and create render_1.png
        # ACTUAL: Currently fails with WriteFileResultFailure
        assert isinstance(result, WriteFileResultSuccess)
        expected_path = temp_dir / "render_1.png"
        assert Path(result.final_file_path).resolve() == expected_path.resolve()
        assert expected_path.read_text() == "Second file"


class TestDiskSpaceProbe:
    """Disk-space helpers must probe the nearest existing ancestor.

    Save situations create parent dirs on write, so callers routinely hand in
    a target path whose directory does not yet exist. get_disk_space_info would
    raise FileNotFoundError; the helpers must walk up to an existing ancestor
    so the reported numbers reflect the mount the write will land on.
    """

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_check_available_disk_space_nonexistent_target_probes_ancestor(self, temp_dir: Path) -> None:
        """A yet-to-be-created target resolves to an existing ancestor rather than raising."""
        nonexistent_target = temp_dir / "not_yet" / "deeper" / "file.bin"

        # required_gb=0 means any amount of free space satisfies; the point of
        # this test is that the call returns True rather than False-on-OSError.
        assert OSManager.check_available_disk_space(nonexistent_target, required_gb=0) is True

    def test_check_available_disk_space_returns_false_when_insufficient(self, temp_dir: Path) -> None:
        """Probing succeeds; a wildly oversized requirement still returns False."""
        nonexistent_target = temp_dir / "not_yet" / "file.bin"

        # Require more space than any realistic filesystem has, so the probe
        # succeeds but the free-space check fails.
        assert OSManager.check_available_disk_space(nonexistent_target, required_gb=10**9) is False

    def test_format_disk_space_error_nonexistent_target_probes_ancestor(self, temp_dir: Path) -> None:
        """Error formatter must report numbers rather than the manual-check fallback."""
        nonexistent_target = temp_dir / "not_yet" / "deeper" / "file.bin"

        message = OSManager.format_disk_space_error(nonexistent_target)

        # The fallback branch emits "Could not determine disk space"; the
        # probe branch emits "Available:" numbers. We want the probe branch.
        assert "Available:" in message
        assert "Could not determine disk space" not in message


class TestGetNextUnusedFilenameRequest:
    """Test GetNextUnusedFilenameRequest preview behavior."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture(autouse=True)
    def setup_workspace(self, temp_dir: Path, griptape_nodes: GriptapeNodes) -> Generator[None, None, None]:
        original_workspace = griptape_nodes.ConfigManager().workspace_path
        griptape_nodes.ConfigManager().workspace_path = temp_dir
        yield
        griptape_nodes.ConfigManager().workspace_path = original_workspace

    def test_base_filename_available_returns_unindexed_path(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """When the base filename is free, preview returns that path and no index."""
        os_manager = griptape_nodes.OSManager()
        requested_path = temp_dir / "render.png"

        result = os_manager.on_get_next_unused_filename_request(
            GetNextUnusedFilenameRequest(file_path=str(requested_path))
        )

        assert isinstance(result, GetNextUnusedFilenameResultSuccess)
        assert Path(result.available_filename).resolve() == requested_path.resolve()
        assert result.index_used is None
        assert not requested_path.exists()

    def test_existing_base_filename_returns_indexed_candidate(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """When base exists, preview switches to indexed naming."""
        os_manager = griptape_nodes.OSManager()
        (temp_dir / "render.png").write_text("base")

        result = os_manager.on_get_next_unused_filename_request(
            GetNextUnusedFilenameRequest(file_path=str(temp_dir / "render.png"))
        )

        assert isinstance(result, GetNextUnusedFilenameResultSuccess)
        assert result.index_used == 1
        assert Path(result.available_filename).resolve() == (temp_dir / "render_1.png").resolve()

    def test_macro_without_unresolved_index_fails(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Macro path without an unresolved index variable cannot be auto-incremented."""
        os_manager = griptape_nodes.OSManager()
        macro_path = MacroPath(parsed_macro=ParsedMacro(f"{temp_dir}/render.png"), variables={})

        result = os_manager.on_get_next_unused_filename_request(GetNextUnusedFilenameRequest(file_path=macro_path))

        assert isinstance(result, GetNextUnusedFilenameResultFailure)
        assert result.failure_reason == FileIOFailureReason.INVALID_PATH


class TestGetNextVersionIndexRequest:
    """Test GetNextVersionIndexRequest index-preview behavior."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture(autouse=True)
    def setup_workspace(self, temp_dir: Path, griptape_nodes: GriptapeNodes) -> Generator[None, None, None]:
        original_workspace = griptape_nodes.ConfigManager().workspace_path
        griptape_nodes.ConfigManager().workspace_path = temp_dir
        yield
        griptape_nodes.ConfigManager().workspace_path = original_workspace

    def test_required_index_returns_one_when_no_matches(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Required index templates start at index 1 when nothing exists yet."""
        os_manager = griptape_nodes.OSManager()

        request = GetNextVersionIndexRequest(
            macro_path=MacroPath(
                parsed_macro=ParsedMacro("{outputs}/render_v{_index:03}"),
                variables={"outputs": str(temp_dir)},
            )
        )
        result = os_manager.on_get_next_version_index_request(request)

        assert isinstance(result, GetNextVersionIndexResultSuccess)
        assert result.index == 1

    def test_optional_index_is_rejected_as_invalid_path(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Optional index templates currently fail because no required unresolved index exists."""
        os_manager = griptape_nodes.OSManager()

        request = GetNextVersionIndexRequest(
            macro_path=MacroPath(
                parsed_macro=ParsedMacro("{outputs}/render{_index?:_}.png"),
                variables={"outputs": str(temp_dir)},
            )
        )
        result = os_manager.on_get_next_version_index_request(request)

        assert isinstance(result, GetNextVersionIndexResultFailure)
        assert result.failure_reason == FileIOFailureReason.INVALID_PATH

    def test_missing_unresolved_index_returns_failure(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Requests without an unresolved {_index} variable should fail as invalid path input."""
        os_manager = griptape_nodes.OSManager()
        request = GetNextVersionIndexRequest(
            macro_path=MacroPath(
                parsed_macro=ParsedMacro("{outputs}/render.png"),
                variables={"outputs": str(temp_dir)},
            )
        )

        result = os_manager.on_get_next_version_index_request(request)

        assert isinstance(result, GetNextVersionIndexResultFailure)
        assert result.failure_reason == FileIOFailureReason.INVALID_PATH


class TestMakeDirectoryRequest:
    """Test MakeDirectoryRequest handler."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture(autouse=True)
    def setup_workspace(self, temp_dir: Path, griptape_nodes: GriptapeNodes) -> Generator[None, None, None]:
        original_workspace = griptape_nodes.ConfigManager().workspace_path
        griptape_nodes.ConfigManager().workspace_path = temp_dir
        yield
        griptape_nodes.ConfigManager().workspace_path = original_workspace

    def test_create_new_directory_success(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Creating a directory that does not yet exist returns success with already_existed=False."""
        os_manager = griptape_nodes.OSManager()
        dir_path = temp_dir / "new_dir"

        result = os_manager.on_make_directory_request(MakeDirectoryRequest(path=str(dir_path)))

        assert isinstance(result, MakeDirectoryResultSuccess)
        assert dir_path.is_dir()
        assert not result.already_existed
        assert Path(result.created_path).resolve() == dir_path.resolve()

    def test_directory_already_exists_exist_ok_true(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """When the directory already exists and exist_ok=True, returns success with already_existed=True."""
        os_manager = griptape_nodes.OSManager()
        dir_path = temp_dir / "existing_dir"
        dir_path.mkdir()

        result = os_manager.on_make_directory_request(MakeDirectoryRequest(path=str(dir_path), exist_ok=True))

        assert isinstance(result, MakeDirectoryResultSuccess)
        assert result.already_existed

    def test_directory_already_exists_exist_ok_false(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """When the directory already exists and exist_ok=False, returns POLICY_NO_OVERWRITE failure."""
        os_manager = griptape_nodes.OSManager()
        dir_path = temp_dir / "existing_dir"
        dir_path.mkdir()

        result = os_manager.on_make_directory_request(MakeDirectoryRequest(path=str(dir_path), exist_ok=False))

        assert isinstance(result, MakeDirectoryResultFailure)
        assert result.failure_reason == FileIOFailureReason.POLICY_NO_OVERWRITE

    def test_file_at_path_returns_invalid_path(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """When a file already exists at the requested path, returns INVALID_PATH failure."""
        os_manager = griptape_nodes.OSManager()
        file_path = temp_dir / "not_a_dir.txt"
        file_path.write_text("content")

        result = os_manager.on_make_directory_request(MakeDirectoryRequest(path=str(file_path)))

        assert isinstance(result, MakeDirectoryResultFailure)
        assert result.failure_reason == FileIOFailureReason.INVALID_PATH

    def test_create_parents_true(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """With create_parents=True, missing intermediate directories are created."""
        os_manager = griptape_nodes.OSManager()
        dir_path = temp_dir / "a" / "b" / "c"

        result = os_manager.on_make_directory_request(MakeDirectoryRequest(path=str(dir_path), create_parents=True))

        assert isinstance(result, MakeDirectoryResultSuccess)
        assert dir_path.is_dir()

    def test_create_parents_false_missing_parent(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """With create_parents=False and a missing parent, returns POLICY_NO_CREATE_PARENT_DIRS failure."""
        os_manager = griptape_nodes.OSManager()
        dir_path = temp_dir / "missing_parent" / "child"

        result = os_manager.on_make_directory_request(MakeDirectoryRequest(path=str(dir_path), create_parents=False))

        assert isinstance(result, MakeDirectoryResultFailure)
        assert result.failure_reason == FileIOFailureReason.POLICY_NO_CREATE_PARENT_DIRS

    def test_invalid_path_returns_failure(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """A path that cannot be resolved returns INVALID_PATH before any filesystem operation."""
        os_manager = griptape_nodes.OSManager()

        with patch.object(OSManager, "_resolve_file_path", side_effect=ValueError("bad path")):
            result = os_manager.on_make_directory_request(MakeDirectoryRequest(path=str(temp_dir / "any")))

        assert isinstance(result, MakeDirectoryResultFailure)
        assert result.failure_reason == FileIOFailureReason.INVALID_PATH

    def test_permission_denied_returns_failure(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """A PermissionError from mkdir is surfaced as a PERMISSION_DENIED failure."""
        os_manager = griptape_nodes.OSManager()
        dir_path = temp_dir / "protected_dir"

        with patch.object(Path, "mkdir", side_effect=PermissionError("Permission denied")):
            result = os_manager.on_make_directory_request(MakeDirectoryRequest(path=str(dir_path)))

        assert isinstance(result, MakeDirectoryResultFailure)
        assert result.failure_reason == FileIOFailureReason.PERMISSION_DENIED
