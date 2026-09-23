"""Unit tests for artifact_normalization module."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from griptape.artifacts import ImageUrlArtifact

from griptape_nodes.utils.artifact_normalization import normalize_artifact_input

if TYPE_CHECKING:
    from pathlib import Path


class TestNormalizeArtifactInputUrls:
    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:8124/workspace/staticfiles/image.jpg?t=123",
            "http://localhost:8124/workspace/renders/image.jpg",
            "https://example.com/image.jpg",
        ],
    )
    def test_url_is_wrapped_without_copying_to_static_storage(self, url: str) -> None:
        engine = MagicMock()
        with patch("griptape_nodes.utils.artifact_normalization.current_engine", return_value=engine):
            result = normalize_artifact_input(url, ImageUrlArtifact)

        assert isinstance(result, ImageUrlArtifact)
        assert result.value == url
        engine.static_files_manager.save_static_file.assert_not_called()


class TestNormalizeArtifactInputPaths:
    @pytest.mark.parametrize("relative", [True, False])
    def test_path_is_wrapped_in_place_without_copying(self, tmp_path: Path, relative: bool) -> None:  # noqa: FBT001
        file_path = tmp_path / "renders" / "image.jpg"
        file_path.parent.mkdir()
        file_path.write_bytes(b"data")
        artifact_input = "renders/image.jpg" if relative else str(file_path)

        engine = MagicMock()
        engine.config_manager.workspace_path = tmp_path
        storage_driver = engine.static_files_manager.storage_driver
        storage_driver.create_signed_download_url.return_value = "http://localhost:8124/workspace/renders/image.jpg?v=1"
        with patch("griptape_nodes.utils.artifact_normalization.current_engine", return_value=engine):
            result = normalize_artifact_input(artifact_input, ImageUrlArtifact)

        assert isinstance(result, ImageUrlArtifact)
        assert result.value == "http://localhost:8124/workspace/renders/image.jpg?v=1"
        storage_driver.create_signed_download_url.assert_called_once_with(file_path)
        engine.static_files_manager.save_static_file.assert_not_called()

    def test_missing_path_is_returned_unchanged(self, tmp_path: Path) -> None:
        engine = MagicMock()
        engine.config_manager.workspace_path = tmp_path
        with patch("griptape_nodes.utils.artifact_normalization.current_engine", return_value=engine):
            result = normalize_artifact_input("missing.jpg", ImageUrlArtifact)

        assert result == "missing.jpg"
        engine.static_files_manager.storage_driver.create_signed_download_url.assert_not_called()
