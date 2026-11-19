"""Regressions for the second OpenImageIO migration review."""

from io import BytesIO
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import numpy as np
import OpenImageIO as oiio  # noqa: N813
import pytest

from griptape_nodes.drivers.image_metadata.png_metadata_driver import PngMetadataDriver
from griptape_nodes.image import Exif, GriptapeImage, GriptapeImagePipeline, ImageFormat, ImageMetadata
from griptape_nodes.image.operations import alpha_channel
from griptape_nodes.image.path_metadata import read_path_extra_metadata_kinds, read_path_metadata
from griptape_nodes.image.pipeline import Thumbnail
from griptape_nodes.image.png_text_chunks import PNG_SIGNATURE, png_chunk, replace_text_chunks
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.flow_events import ExtractFlowCommandsFromImageMetadataResultFailure
from tests.unit.image.sample_images import BLUE, RED, decode, encode, gray_tiff, noise, solid


class TestSecondReview:
    def test_metadata_disappears_after_header_returns_structured_failure(self, engine: Engine, tmp_path: Path) -> None:
        path = tmp_path / "deleted.png"
        path.write_bytes(encode(solid(64, 64), "png"))
        image = GriptapeImage.open(path)

        def remove_after_open(_path: Path) -> GriptapeImage:
            path.unlink()
            return image

        with patch(
            "griptape_nodes.retained_mode.managers.flow.serialization.GriptapeImage.open", side_effect=remove_after_open
        ):
            result = engine.flow_manager.serialization._read_image_text_metadata(str(path))
        assert isinstance(result, ExtractFlowCommandsFromImageMetadataResultFailure)

    def test_png_driver_accepts_legacy_image_without_format(self) -> None:
        image = MagicMock()
        image.format = None
        image.info = {}
        image.getexif.return_value = None

        def save(buffer: BytesIO, **kwargs: object) -> None:
            assert kwargs["format"] == "PNG"
            buffer.write(encode(solid(64, 64), "png"))

        image.save.side_effect = save
        result = PngMetadataDriver().inject_metadata(image, {"key": "value"})
        assert PngMetadataDriver().extract_metadata(result) == {"key": "value"}

    @pytest.mark.parametrize("operation", ["thumbnail", "cover"])
    def test_gif_resize_keeps_opaque_color(self, operation: str) -> None:
        with GriptapeImage.open(encode(solid(200, 120, (100, 60, 20)), "gif")) as image:
            pipeline = GriptapeImagePipeline(image)
            if operation == "thumbnail":
                pipeline.thumbnail(100, 100)
            else:
                pipeline.cover(100, 100)
            with pipeline.end() as preview:
                pixels = preview._decoded().get_pixels(oiio.UINT8)
                assert isinstance(pixels, np.ndarray)
                np.testing.assert_allclose(pixels, solid(preview.width, preview.height, (100, 60, 20, 255)), atol=1)

    def test_invalid_alpha_index_is_absent(self) -> None:
        buffer = oiio.ImageBuf(solid(64, 64, (100, 60, 20, 128)))
        buffer.specmod().alpha_channel = buffer.nchannels
        assert alpha_channel(buffer) is None

    def test_metadata_override_survives_path_pipeline(self, tmp_path: Path) -> None:
        path = tmp_path / "source.png"
        path.write_bytes(encode(solid(64, 64), "png"))
        with (
            GriptapeImage.open(path) as image,
            image.with_metadata(ImageMetadata(text={"x": "y"})) as attached,
            attached.run_pipeline([Thumbnail(8, 8)]) as preview,
        ):
            assert attached.metadata.text == {"x": "y"}
            assert preview.metadata.text == {"x": "y"}

    @pytest.mark.parametrize("use_path", [False, True])
    def test_metadata_override_is_encoded(self, tmp_path: Path, *, use_path: bool) -> None:
        source: bytes | Path = encode(solid(64, 64), "png")
        if use_path:
            path = tmp_path / "source.png"
            path.write_bytes(source)
            source = path
        with GriptapeImage.open(source) as image, image.with_metadata(ImageMetadata(text={"x": "y"})) as attached:
            result = attached.encode(ImageFormat.PNG)
        assert PngMetadataDriver().extract_metadata(result) == {"x": "y"}

    def test_reused_path_splice_refreshes_source_bytes(self, tmp_path: Path) -> None:
        path = tmp_path / "source.png"
        path.write_bytes(encode(solid(64, 64, RED), "png"))
        with GriptapeImage.open(path) as image:
            image.encode(ImageFormat.PNG)
            path.write_bytes(encode(solid(64, 64, BLUE), "png"))
            np.testing.assert_array_equal(decode(image.encode(ImageFormat.PNG))[0, 0], BLUE)

    def test_reused_path_orientation_refreshes_before_planning(self, tmp_path: Path) -> None:
        path = tmp_path / "oriented.png"
        pixels = encode(solid(80, 40), "png")
        path.write_bytes(pixels)
        with GriptapeImage.open(path) as image:
            with GriptapeImagePipeline(image).exif_transpose().thumbnail(40, 40).end() as first:
                assert (first.width, first.height) == (40, 20)
            exif = Exif()
            exif[0x0112] = 6
            with GriptapeImage.open(pixels) as replacement:
                path.write_bytes(replacement.encode(ImageFormat.PNG, metadata=ImageMetadata(exif=exif)))
            with GriptapeImagePipeline(image).exif_transpose().thumbnail(40, 40).end() as second:
                assert (second.width, second.height) == (20, 40)

    def test_many_pixel_chunks_do_not_exhaust_metadata_count(self, tmp_path: Path) -> None:
        path = tmp_path / "chunks.png"
        path.write_bytes(
            PNG_SIGNATURE + png_chunk(b"IHDR", bytes(13)) + png_chunk(b"IDAT", b"pixel") * 10 + png_chunk(b"IEND", b"")
        )
        with patch("griptape_nodes.image.path_metadata._MAX_METADATA_CHUNKS", 2):
            assert read_path_metadata(path, ImageFormat.PNG).is_empty()

    def test_png_splice_does_not_slice_pixel_payload(self) -> None:
        class SliceRecorder(bytes):
            def __getitem__(self, key: object) -> bytes:
                assert key != slice(16, 21), "pixel payload copied"
                return super().__getitem__(cast("slice", key))

        source = SliceRecorder(PNG_SIGNATURE + png_chunk(b"IDAT", b"pixel") + png_chunk(b"IEND", b""))
        assert replace_text_chunks(source, {"key": "value"})

    def test_bigtiff_metadata_preserves_header_orientation(self, tmp_path: Path) -> None:
        path = tmp_path / "big.tif"
        path.write_bytes(encode(solid(40, 16), "tif", **{"tiff:bigtiff": 1, "Orientation": 6}))
        with GriptapeImage.open(path) as image:
            assert image.orientation == 6  # noqa: PLR2004
            _ = image.metadata
            assert image.orientation == 6  # noqa: PLR2004
            assert image.oriented_size == (16, 40)

    def test_generated_tiff_xmp_stamp_is_not_reported(self, tmp_path: Path) -> None:
        path = tmp_path / "stamp.tif"
        path.write_bytes(encode(solid(64, 64), "tif"))
        assert read_path_extra_metadata_kinds(path, ImageFormat.TIFF) == []

    def test_custom_large_xmp_is_reported(self, tmp_path: Path) -> None:
        path = tmp_path / "custom.tif"
        packet = b'<dc:title xmlns:dc="custom">' + b"x" * 5000 + b"</dc:title>"
        path.write_bytes(gray_tiff({0x02BC: (7, packet)}))
        assert read_path_extra_metadata_kinds(path, ImageFormat.TIFF) == ["XMP"]

    @pytest.mark.parametrize("zero_steps", [False, True])
    def test_each_truncated_cached_read_fails(self, tmp_path: Path, *, zero_steps: bool) -> None:
        data = encode(noise(600, 600), "tif")
        path = tmp_path / "broken.tif"
        path.write_bytes(data[: len(data) // 2])
        for _ in range(2):
            with GriptapeImage.open(path) as image:
                pipeline = GriptapeImagePipeline(image)
                if not zero_steps:
                    pipeline.thumbnail(64, 64)
                with pytest.raises(ValueError, match="Failed"):
                    pipeline.end()
