"""Unit tests for artifact_normalization module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from griptape.artifacts import ImageUrlArtifact

from griptape_nodes.utils.artifact_normalization import normalize_artifact_input


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
