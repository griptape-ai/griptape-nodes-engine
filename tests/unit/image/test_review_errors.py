"""Exercise metadata limits and codec failures through stable public behavior."""

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import OpenImageIO as oiio  # noqa: N813
import pytest
from pydantic import ValidationError

from griptape_nodes.image import GriptapeImage
from griptape_nodes.image.cache import refresh_image_cache
from griptape_nodes.image.image_io import write_file
from griptape_nodes.image.metadata_reader import FileMetadataSource
from griptape_nodes.image.pipeline import Orient, PipelineStep
from griptape_nodes.image.png_text_chunks import insert_text_chunks, read_text_chunks
from griptape_nodes.retained_mode.managers.settings import FROM_ENV_CONTEXT, OpenImageIOSettings, Settings
from tests.unit.image.sample_images import RED, encode, solid


class TestReviewErrors:
    @pytest.mark.parametrize(
        "key", ["max_memory_mb", "max_open_files", "threads", "max_image_size_mb", "failure_retries", "autotile_size"]
    )
    @pytest.mark.parametrize("value", [True, "bad", "-1"])
    def test_environment_settings_reject_invalid_values(self, key: str, value: object) -> None:
        with pytest.raises(ValidationError):
            OpenImageIOSettings.model_validate({key: value}, context={FROM_ENV_CONTEXT: True})

    def test_settings_accept_existing_model(self) -> None:
        existing = OpenImageIOSettings()
        assert Settings.model_validate({"openimageio": existing}).openimageio is existing

    def test_environment_rejects_non_mapping(self) -> None:
        with pytest.raises(ValidationError):
            Settings.model_validate({"openimageio": "bad"}, context={FROM_ENV_CONTEXT: True})

    @pytest.mark.parametrize("orientation", range(2, 9))
    def test_all_orientation_operations(self, orientation: int) -> None:
        with (
            GriptapeImage.open(encode(solid(64, 32, RED), "png")) as image,
            image.run_pipeline([Orient(orientation)]) as result,
        ):
            assert sorted((result.width, result.height)) == [32, 64]

    @pytest.mark.parametrize(("offset", "size"), [(-1, 1), (0, -1), (100, 1), (1, 100)])
    def test_metadata_range_bounds(self, tmp_path: Path, offset: int, size: int) -> None:
        path = tmp_path / "metadata"
        path.write_bytes(b"abc")
        with path.open("rb") as handle, pytest.raises(ValueError, match="outside"):
            FileMetadataSource(handle).read_at(offset, size)

    def test_metadata_budget(self, tmp_path: Path) -> None:
        path = tmp_path / "metadata"
        path.write_bytes(b"abc")
        with path.open("rb") as handle:
            source = FileMetadataSource(handle)
            with patch("griptape_nodes.image.metadata_reader.MAX_METADATA_READ_BYTES", 1):
                source = FileMetadataSource(handle)
            with pytest.raises(ValueError, match="size limit"):
                source.read_at(0, 2)

    def test_png_total_limit_fails_explicitly(self) -> None:
        source = insert_text_chunks(encode(solid(64, 64, RED), "png"), {"a": "hello", "b": "world"})
        with (
            patch("griptape_nodes.image.png_text_chunks.MAX_TOTAL_TEXT_BYTES", 6),
            pytest.raises(ValueError, match="size limit"),
        ):
            read_text_chunks(source)

    def test_missing_cached_source_is_evicted(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            refresh_image_cache(tmp_path / "missing.png")

    @pytest.mark.parametrize("stage", ["open", "write", "close"])
    def test_writer_errors_are_retained(self, tmp_path: Path, stage: str) -> None:
        writer = MagicMock()
        writer.open.return_value = True
        writer.close.return_value = True
        errors = ["", "", ""]
        errors[["open", "write", "close"].index(stage)] = "codec error"
        writer.geterror.side_effect = errors
        buffer = MagicMock()
        buffer.write.return_value = True
        buffer.geterror.return_value = ""
        buffer.has_error = False
        with (
            patch("griptape_nodes.image.image_io.oiio.ImageOutput.create", return_value=writer),
            pytest.raises(ValueError, match="codec error"),
        ):
            write_file(buffer, oiio.ImageSpec(64, 64, 3, oiio.UINT8), tmp_path / "output.png")

    def test_missing_writer_fails_before_output_read(self, tmp_path: Path) -> None:
        with (
            patch("griptape_nodes.image.image_io.oiio.ImageOutput.create", return_value=None),
            pytest.raises(ValueError, match="no writer"),
        ):
            write_file(MagicMock(), oiio.ImageSpec(64, 64, 3, oiio.UINT8), tmp_path / "unknown.extension")

    def test_unknown_step_is_rejected(self) -> None:
        with (
            GriptapeImage.open(encode(solid(64, 64, RED), "png")) as image,
            pytest.raises(TypeError, match="unsupported"),
        ):
            image.run_pipeline([cast("PipelineStep", object())])
