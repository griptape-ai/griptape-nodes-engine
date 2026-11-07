"""Tests for uv_utils module."""

import platform
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from griptape_nodes.utils.uv_utils import find_uv_bin, is_venv_functional, venv_python_path


@pytest.mark.skipif(
    platform.system() == "Windows", reason="xdg_base_dirs cannot find XDG_DATA_HOME on Windows on GitHub Actions"
)
class TestUvUtils:
    """Test UV utilities functionality."""

    def test_find_uv_bin_uses_dedicated_installation_when_exists(self) -> None:
        """Test that find_uv_bin prefers dedicated Griptape installation when it exists."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create a mock dedicated UV path that exists
            dedicated_path = Path(temp_dir) / "griptape_nodes" / "bin" / "uv"
            dedicated_path.parent.mkdir(parents=True)
            dedicated_path.touch()

            with patch("griptape_nodes.utils.engine_dirs.xdg_data_home") as mock_xdg:
                mock_xdg.return_value = Path(temp_dir)

                result = find_uv_bin()

                assert result == str(dedicated_path)

    def test_find_uv_bin_falls_back_to_system_uv_when_dedicated_not_exists(self) -> None:
        """Test that find_uv_bin falls back to system UV when dedicated installation doesn't exist."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Mock system UV path
            system_uv_path = "/usr/local/bin/uv"

            with (
                patch("griptape_nodes.utils.engine_dirs.xdg_data_home") as mock_xdg,
                patch("griptape_nodes.utils.uv_utils.uv.find_uv_bin") as mock_system_uv,
            ):
                mock_xdg.return_value = Path(temp_dir)
                mock_system_uv.return_value = system_uv_path

                result = find_uv_bin()

                assert result == system_uv_path
                mock_system_uv.assert_called_once()

    def test_find_uv_bin_dedicated_path_construction(self) -> None:
        """Test that the dedicated UV path is constructed correctly."""
        mock_data_home = Path("/mock/data/home")

        with (
            patch("griptape_nodes.utils.engine_dirs.xdg_data_home") as mock_xdg,
            patch("griptape_nodes.utils.uv_utils.uv.find_uv_bin") as mock_system_uv,
        ):
            mock_xdg.return_value = mock_data_home
            mock_system_uv.return_value = "/system/uv"

            # Mock the path.exists() to return False so we test the fallback
            with patch.object(Path, "exists", return_value=False):
                find_uv_bin()

            # Verify that we checked the correct path
            mock_system_uv.assert_called_once()

    def test_find_uv_bin_returns_string(self) -> None:
        """Test that find_uv_bin always returns a string."""
        with tempfile.TemporaryDirectory() as temp_dir:
            dedicated_path = Path(temp_dir) / "griptape_nodes" / "bin" / "uv"
            dedicated_path.parent.mkdir(parents=True)
            dedicated_path.touch()

            with patch("griptape_nodes.utils.engine_dirs.xdg_data_home") as mock_xdg:
                mock_xdg.return_value = Path(temp_dir)

                result = find_uv_bin()

                assert isinstance(result, str)
                assert result == str(dedicated_path)

    def test_find_uv_bin_system_fallback_returns_string(self) -> None:
        """Test that system UV fallback also returns a string."""
        with tempfile.TemporaryDirectory() as temp_dir:
            system_uv_path = "/usr/local/bin/uv"

            with (
                patch("griptape_nodes.utils.engine_dirs.xdg_data_home") as mock_xdg,
                patch("griptape_nodes.utils.uv_utils.uv.find_uv_bin") as mock_system_uv,
            ):
                mock_xdg.return_value = Path(temp_dir)
                mock_system_uv.return_value = system_uv_path

                result = find_uv_bin()

                assert isinstance(result, str)
                assert result == system_uv_path

    @patch("griptape_nodes.utils.uv_utils.uv.find_uv_bin")
    @patch("griptape_nodes.utils.engine_dirs.xdg_data_home")
    def test_find_uv_bin_handles_system_uv_exception(self, mock_xdg: Mock, mock_system_uv: Mock) -> None:
        """Test that find_uv_bin handles exceptions from system UV lookup appropriately."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Set up mocks - use temp dir instead of hardcoded path
            mock_xdg.return_value = Path(temp_dir) / "nonexistent"
            mock_system_uv.side_effect = RuntimeError("UV not found")

            # Should raise the exception from the system UV lookup
            with pytest.raises(RuntimeError, match="UV not found"):
                find_uv_bin()


def _make_functional_venv(venv_path: Path) -> Path:
    """Create a directory layout that mimics a working venv on the current platform."""
    venv_path.mkdir(parents=True, exist_ok=True)
    (venv_path / "pyvenv.cfg").write_text("home = /fake\n")
    if sys.platform == "win32":
        python_dir = venv_path / "Scripts"
        python_path = python_dir / "python.exe"
    else:
        python_dir = venv_path / "bin"
        python_path = python_dir / "python"
    python_dir.mkdir(parents=True, exist_ok=True)
    python_path.write_text("")
    return python_path


class TestVenvPythonPath:
    """Test the platform-specific Python executable path resolution."""

    def test_returns_bin_python_on_posix(self) -> None:
        with patch("griptape_nodes.utils.uv_utils.sys.platform", "linux"):
            assert venv_python_path(Path("/v")) == Path("/v/bin/python")

    def test_returns_scripts_python_exe_on_windows(self) -> None:
        with patch("griptape_nodes.utils.uv_utils.sys.platform", "win32"):
            assert venv_python_path(Path("/v")) == Path("/v/Scripts/python.exe")


class TestIsVenvFunctional:
    """Test the venv-layout health check."""

    def test_returns_false_when_directory_missing(self, tmp_path: Path) -> None:
        assert is_venv_functional(tmp_path / "nope") is False

    def test_returns_false_when_path_is_a_file(self, tmp_path: Path) -> None:
        venv_path = tmp_path / ".venv"
        venv_path.write_text("not a venv")

        assert is_venv_functional(venv_path) is False

    def test_returns_false_when_pyvenv_cfg_missing(self, tmp_path: Path) -> None:
        venv_path = tmp_path / ".venv"
        _make_functional_venv(venv_path)
        (venv_path / "pyvenv.cfg").unlink()

        assert is_venv_functional(venv_path) is False

    def test_returns_false_when_python_executable_missing(self, tmp_path: Path) -> None:
        venv_path = tmp_path / ".venv"
        python_path = _make_functional_venv(venv_path)
        python_path.unlink()

        assert is_venv_functional(venv_path) is False

    def test_returns_true_for_complete_layout(self, tmp_path: Path) -> None:
        venv_path = tmp_path / ".venv"
        _make_functional_venv(venv_path)

        assert is_venv_functional(venv_path) is True
