from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.servers.static import WorkspaceStaticFiles, _serve_external_file


class TestServeExternalFile:
    """Test _serve_external_file() path reconstruction."""

    @pytest.mark.asyncio
    async def test_unix_path_reconstruction(self, tmp_path: Path) -> None:
        """Unix-style paths (no drive letter) should get a leading slash prepended."""
        test_file = tmp_path / "image.png"
        test_file.write_bytes(b"fake png")

        # Strip the leading slash, as the URL router would
        file_path_in_url = str(test_file).removeprefix("/")

        with patch("griptape_nodes.servers.static.STATIC_SERVER_ENABLED", True):
            response = await _serve_external_file(file_path_in_url)

        assert Path(response.path) == test_file

    @pytest.mark.asyncio
    async def test_absolute_path_not_prepended_with_slash(self) -> None:
        """Paths that are already absolute should not get a leading slash prepended."""
        # On Windows, Path("C:/Users/foo/image.png") is already absolute.
        # Prepending "/" would produce "\C:\Users\..." which is invalid.
        # We simulate this by patching Path.is_absolute to return True.
        already_absolute_path = "C:/Users/foo/image.png"

        with (
            patch("griptape_nodes.servers.static.STATIC_SERVER_ENABLED", True),
            patch("griptape_nodes.servers.static.Path") as mock_path_cls,
            patch("griptape_nodes.servers.static.anyio.Path") as mock_anyio_path,
            patch("griptape_nodes.servers.static.FileResponse") as mock_response,
        ):
            mock_candidate = mock_path_cls.return_value
            mock_candidate.is_absolute.return_value = True
            mock_anyio_instance = AsyncMock()
            mock_anyio_instance.exists.return_value = True
            mock_anyio_instance.is_file.return_value = True
            mock_anyio_path.return_value = mock_anyio_instance

            await _serve_external_file(already_absolute_path)

            # Path() should have been called with the raw path, not with "/" prepended
            mock_path_cls.assert_called_once_with(already_absolute_path)
            mock_response.assert_called_once_with(mock_candidate)


class TestWorkspaceStaticFiles:
    """Test that WorkspaceStaticFiles resolves the served directory live, per request."""

    def _patch_workspace(self, workspace: Path) -> AbstractContextManager[MagicMock]:
        """Patch ConfigManager().workspace_path to return the given directory."""
        config_manager = MagicMock()
        config_manager.workspace_path = workspace
        griptape_nodes = MagicMock()
        griptape_nodes.ConfigManager.return_value = config_manager
        return patch("griptape_nodes.servers.static.GriptapeNodes", griptape_nodes)

    def test_lookup_follows_workspace_change(self, tmp_path: Path) -> None:
        """A file written under the new workspace resolves after the workspace changes at runtime."""
        boot_workspace = tmp_path / "boot"
        new_workspace = tmp_path / "project"
        new_workspace.mkdir()
        (new_workspace / "outputs").mkdir()
        served = new_workspace / "outputs" / "image.png"
        served.write_bytes(b"fake png")

        # Construct against the boot workspace, then switch the active workspace.
        with self._patch_workspace(boot_workspace):
            static = WorkspaceStaticFiles()

        with self._patch_workspace(new_workspace):
            full_path, stat_result = static.lookup_path("outputs/image.png")

        assert Path(full_path) == served
        assert stat_result is not None

    def test_subdirectory_is_appended_to_live_workspace(self, tmp_path: Path) -> None:
        """The legacy /static mount serves from <workspace>/<subdirectory>, resolved live."""
        workspace = tmp_path / "ws"
        static_dir = workspace / "staticfiles"
        static_dir.mkdir(parents=True)
        served = static_dir / "asset.bin"
        served.write_bytes(b"data")

        with self._patch_workspace(workspace):
            static = WorkspaceStaticFiles(subdirectory="staticfiles")
            full_path, stat_result = static.lookup_path("asset.bin")

        assert Path(full_path) == served
        assert stat_result is not None

    def test_path_traversal_is_rejected(self, tmp_path: Path) -> None:
        """A path escaping the workspace must not resolve, even with the live directory."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_bytes(b"top secret")

        with self._patch_workspace(workspace):
            static = WorkspaceStaticFiles()
            full_path, stat_result = static.lookup_path("../secret.txt")

        assert full_path == ""
        assert stat_result is None
