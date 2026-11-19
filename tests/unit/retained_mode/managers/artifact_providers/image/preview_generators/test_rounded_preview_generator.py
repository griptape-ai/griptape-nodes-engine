"""Tests for ThumbnailGenerator and RoundedPreviewGenerator."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import OpenImageIO as oiio  # noqa: N813
import pytest
from pydantic import ValidationError

from griptape_nodes.image import GriptapeImage, ImageFormat
from griptape_nodes.image.oiio_config import apply_openimageio_settings
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators import (
    RoundedPreviewGenerator,
    ThumbnailGenerator,
)
from tests.unit.image.sample_images import BLUE, RED, encode, solid

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


@pytest.fixture
def temp_test_image(tmp_path: Path) -> str:
    """Create a temporary 200x100 RGB PNG image."""
    path = tmp_path / "source.png"
    path.write_bytes(encode(solid(200, 100, RED), "png"))
    return str(path)


@pytest.fixture
def temp_transparent_image(tmp_path: Path) -> str:
    """Create a temporary 200x100 semi-transparent RGBA PNG image."""
    path = tmp_path / "transparent.png"
    path.write_bytes(encode(solid(200, 100, (255, 0, 0, 128)), "png"))
    return str(path)


@pytest.fixture
def temp_output_dir(tmp_path: Path) -> str:
    """Return the isolated output directory."""
    return str(tmp_path)


class TestGeneratorFriendlyNames:
    def test_persisted_friendly_names_are_unchanged(self) -> None:
        assert ThumbnailGenerator.get_friendly_name() == "Standard Thumbnail Generation"
        assert RoundedPreviewGenerator.get_friendly_name() == "Rounded Image Preview Generation"


class TestRoundedPreviewGeneratorParameters:
    @pytest.mark.parametrize(
        "params",
        [
            {"max_width": -100, "max_height": 100, "corner_radius_percent": 2.0},
            {"max_width": 0, "max_height": 100, "corner_radius_percent": 2.0},
            {"max_width": 8193, "max_height": 100, "corner_radius_percent": 2.0},
            {"max_width": 100, "max_height": -100, "corner_radius_percent": 2.0},
            {"max_width": 100, "max_height": 0, "corner_radius_percent": 2.0},
            {"max_width": 100, "max_height": 8193, "corner_radius_percent": 2.0},
            {"max_width": 100, "max_height": 100, "corner_radius_percent": -1.0},
            {"max_width": 100, "max_height": 100, "corner_radius_percent": 11.0},
        ],
    )
    def test_invalid_parameters_raise_validation_error(self, params: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            RoundedPreviewGenerator(
                source_file_location="source.png",
                preview_format="png",
                destination_preview_directory="previews",
                destination_preview_file_name="output.png",
                params=params,
            )

    def test_string_coercion_and_valid_parameters(self) -> None:
        generator = RoundedPreviewGenerator(
            source_file_location="source.png",
            preview_format="png",
            destination_preview_directory="previews",
            destination_preview_file_name="output.png",
            params={"max_width": "150", "max_height": 150, "corner_radius_percent": "2.0"},
        )
        assert generator.params.max_width == 150  # noqa: PLR2004
        assert generator.params.max_height == 150  # noqa: PLR2004
        assert generator.params.corner_radius_percent == 2.0  # noqa: PLR2004


class TestRoundedPreviewGeneratorClassMethods:
    def test_get_supported_source_formats_includes_expanded_formats(self) -> None:
        for gen_cls in (ThumbnailGenerator, RoundedPreviewGenerator):
            formats = gen_cls.get_supported_source_formats()
            for expected in ("png", "jpg", "jpeg", "webp", "exr", "hdr", "dpx", "psd", "jp2", "dds"):
                assert expected in formats

    def test_get_supported_preview_formats(self) -> None:
        assert RoundedPreviewGenerator.get_supported_preview_formats() == {"webp", "jpg", "png"}

    def test_get_parameters(self) -> None:
        params_model_class = RoundedPreviewGenerator.get_parameters()
        model_fields = params_model_class.model_fields
        assert set(model_fields) == {"max_width", "max_height", "corner_radius_percent"}


class TestPreviewGeneratorGeneration:
    @pytest.mark.asyncio
    async def test_generate_png_webp_and_jpeg_outputs(
        self, engine: Engine, temp_test_image: str, temp_output_dir: str
    ) -> None:
        for ext, expected_fmt, expected_cs in (
            ("png", ImageFormat.PNG, "RGBA"),
            ("webp", ImageFormat.WEBP, "RGBA"),
            ("jpg", ImageFormat.JPEG, "RGB"),
        ):
            generator = RoundedPreviewGenerator(
                source_file_location=temp_test_image,
                preview_format=ext,
                destination_preview_directory=temp_output_dir,
                destination_preview_file_name=f"output.{ext}",
                params={"max_width": 100, "max_height": 100, "corner_radius_percent": 2.0},
                engine=engine,
            )
            result_filename = await generator.attempt_generate_preview()
            output_path = Path(temp_output_dir) / result_filename
            with GriptapeImage.open(output_path) as img:
                assert img.format == expected_fmt
                assert img.color_space == expected_cs
                assert img.width == 100  # noqa: PLR2004
                assert img.height == 50  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_generate_transparent_and_small_inputs(
        self, engine: Engine, temp_transparent_image: str, temp_output_dir: str, tmp_path: Path
    ) -> None:
        generator = RoundedPreviewGenerator(
            source_file_location=temp_transparent_image,
            preview_format="png",
            destination_preview_directory=temp_output_dir,
            destination_preview_file_name="transparent_out.png",
            params={"max_width": 150, "max_height": 150, "corner_radius_percent": 0.0},
            engine=engine,
        )
        result_filename = await generator.attempt_generate_preview()
        with GriptapeImage.open(Path(temp_output_dir) / result_filename) as img:
            assert img.color_space == "RGBA"

        tiny_path = tmp_path / "tiny.png"
        tiny_path.write_bytes(encode(solid(10, 10, BLUE), "png"))
        tiny_gen = RoundedPreviewGenerator(
            source_file_location=str(tiny_path),
            preview_format="png",
            destination_preview_directory=temp_output_dir,
            destination_preview_file_name="tiny_out.png",
            params={"max_width": 100, "max_height": 100, "corner_radius_percent": 0.5},
            engine=engine,
        )
        await tiny_gen.attempt_generate_preview()
        with GriptapeImage.open(Path(temp_output_dir) / "tiny_out.png") as img:
            assert (img.width, img.height) == (10, 10)

    @pytest.mark.asyncio
    async def test_float_exr_streams_through_image_cache_for_preview(self, engine: Engine, tmp_path: Path) -> None:
        # Given a 32-bit float EXR on disk and an invalidated shared ImageCache
        apply_openimageio_settings(engine.config_manager)
        cache = oiio.ImageCache(shared=True)
        exr_path = tmp_path / "shot.exr"
        pixels = np.full((64, 32, 4), 0.5, dtype=np.float32)
        written = oiio.ImageBuf(np.ascontiguousarray(pixels)).write(str(exr_path))
        assert written
        cache.invalidate_all(True)
        unique_before = cache.getattribute("stat:unique_files", oiio.INT)

        # When ThumbnailGenerator renders a WebP preview from the local EXR path
        out_dir = tmp_path / "previews"
        generator = ThumbnailGenerator(
            source_file_location=str(exr_path),
            preview_format="webp",
            destination_preview_directory=str(out_dir),
            destination_preview_file_name="shot.exr.webp",
            params={"max_width": 16, "max_height": 16},
            engine=engine,
        )
        filename = await generator.attempt_generate_preview()

        # Then the preview is generated via ImageCache and fits within 16x16
        assert cache.getattribute("stat:unique_files", oiio.INT) >= unique_before + 1
        with GriptapeImage.open(out_dir / filename) as preview:
            assert preview.format == ImageFormat.WEBP
            assert preview.width == 8  # noqa: PLR2004
            assert preview.height == 16  # noqa: PLR2004
