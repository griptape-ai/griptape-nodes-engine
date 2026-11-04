"""Behavior every GriptapeImage backend shares, run against each backend."""

import struct
import zlib
from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from griptape_nodes.drivers.image_metadata.exif_metadata_driver import EXIF_USERCOMMENT_TAG, ExifMetadataDriver
from griptape_nodes.image import (
    IMAGE_BACKEND_ENV_VAR,
    OPENCV_BACKEND,
    PILLOW_BACKEND,
    OpenCVImage,
    PillowImage,
    image_backend_name,
    open_image,
)
from griptape_nodes.image.formats import sniff_image_format
from griptape_nodes.image.png_text_chunks import MAX_COMPRESSED_TEXT_BYTES, read_text_chunks

_EXIF_ORIENTATION_TAG = 0x0112
_ROTATE_90_CLOCKWISE = 6
# Offset of the bit depth byte in a PNG: signature (8), IHDR length and type (8), width, height (8)
_PNG_BIT_DEPTH_OFFSET = 24


@pytest.fixture(params=[PILLOW_BACKEND, OPENCV_BACKEND])
def backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run the test once per image backend."""
    monkeypatch.setenv(IMAGE_BACKEND_ENV_VAR, request.param)
    return request.param


def _encode(image: Image.Image, image_format: str, **save_options: object) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format=image_format, **save_options)
    return buffer.getvalue()


def _sixteen_bit_rgb_png() -> bytes:
    # Pillow cannot write 16-bit color, so build the source with OpenCV
    gradient = (np.arange(48 * 64 * 3, dtype=np.uint32).reshape(48, 64, 3) * 7 % 65536).astype(np.uint16)
    ok, buffer = cv2.imencode(".png", gradient)
    assert ok
    return buffer.tobytes()


def _decode_rgba(data: bytes) -> Image.Image:
    return Image.open(BytesIO(data)).convert("RGBA")


class TestBackendSelection:
    def test_defaults_to_pillow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(IMAGE_BACKEND_ENV_VAR, raising=False)

        assert isinstance(open_image(_encode(Image.new("RGB", (2, 2)), "PNG")), PillowImage)

    def test_env_var_selects_opencv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(IMAGE_BACKEND_ENV_VAR, " OpenCV ")

        assert isinstance(open_image(_encode(Image.new("RGB", (2, 2)), "PNG")), OpenCVImage)

    def test_unknown_value_falls_back_to_pillow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(IMAGE_BACKEND_ENV_VAR, "imagemagick")

        assert image_backend_name() == PILLOW_BACKEND


class TestReading:
    @pytest.mark.usefixtures("backend")
    @pytest.mark.parametrize(
        ("image_format", "expected"),
        [("PNG", "PNG"), ("JPEG", "JPEG"), ("WEBP", "WEBP"), ("TIFF", "TIFF"), ("BMP", "BMP"), ("GIF", "GIF")],
    )
    def test_reports_dimensions_and_format(self, image_format: str, expected: str) -> None:
        with open_image(_encode(Image.new("RGB", (30, 20), "red"), image_format)) as image:
            assert (image.width, image.height) == (30, 20)
            assert image.format == expected

    @pytest.mark.usefixtures("backend")
    @pytest.mark.parametrize(
        ("mode", "channels", "color_space"),
        [("RGB", 3, "RGB"), ("RGBA", 4, "RGBA"), ("L", 1, "Grayscale")],
    )
    def test_reports_color_model(self, mode: str, channels: int, color_space: str) -> None:
        with open_image(_encode(Image.new(mode, (4, 4)), "PNG")) as image:
            assert image.channels == channels
            assert image.color_space == color_space
            assert image.bit_depth == 8  # noqa: PLR2004

    @pytest.mark.usefixtures("backend")
    def test_opens_from_a_path(self, tmp_path: Path) -> None:
        path = tmp_path / "image.png"
        path.write_bytes(_encode(Image.new("RGB", (5, 3)), "PNG"))

        with open_image(path) as image:
            assert (image.width, image.height) == (5, 3)

    def test_sniffs_format_from_header(self) -> None:
        assert sniff_image_format(_encode(Image.new("RGB", (2, 2)), "JPEG")) == "JPEG"
        assert sniff_image_format(b"not an image at all") is None

    def test_opencv_reads_text_metadata_without_decoding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(IMAGE_BACKEND_ENV_VAR, OPENCV_BACKEND)
        png = open_image(_encode(Image.new("RGB", (2, 2)), "PNG")).encode("PNG", text_metadata={"k": "v"})

        image = open_image(png)

        assert image.text_metadata == {"k": "v"}
        assert isinstance(image, OpenCVImage)
        assert image._pixels is None


class TestBitDepth:
    def test_opencv_keeps_sixteen_bit_color_through_a_png_round_trip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(IMAGE_BACKEND_ENV_VAR, OPENCV_BACKEND)

        with open_image(_sixteen_bit_rgb_png()) as image:
            assert image.bit_depth == 16  # noqa: PLR2004
            reencoded = image.encode("PNG", text_metadata={"gtn_key": "value"})

        assert reencoded[_PNG_BIT_DEPTH_OFFSET] == 16  # noqa: PLR2004
        assert open_image(reencoded).text_metadata == {"gtn_key": "value"}

    def test_pillow_drops_sixteen_bit_color_to_eight(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(IMAGE_BACKEND_ENV_VAR, PILLOW_BACKEND)

        with open_image(_sixteen_bit_rgb_png()) as image:
            assert image.bit_depth == 8  # noqa: PLR2004
            reencoded = image.encode("PNG")

        assert reencoded[_PNG_BIT_DEPTH_OFFSET] == 8  # noqa: PLR2004

    @pytest.mark.usefixtures("backend")
    def test_sixteen_bit_source_encodes_to_eight_bit_formats(self) -> None:
        with open_image(_sixteen_bit_rgb_png()) as image:
            jpeg = image.encode("jpg")

        assert sniff_image_format(jpeg) == "JPEG"


@pytest.mark.usefixtures("backend")
class TestTransforms:
    def test_exif_transposed_applies_orientation(self) -> None:
        source = Image.new("RGB", (40, 20), "blue")
        exif = source.getexif()
        exif[_EXIF_ORIENTATION_TAG] = _ROTATE_90_CLOCKWISE

        with open_image(_encode(source, "JPEG", exif=exif)) as image, image.exif_transposed() as upright:
            assert (upright.width, upright.height) == (20, 40)

    def test_exif_transposed_without_orientation_keeps_dimensions(self) -> None:
        with open_image(_encode(Image.new("RGB", (40, 20)), "PNG")) as image, image.exif_transposed() as upright:
            assert (upright.width, upright.height) == (40, 20)

    def test_thumbnail_fits_bounds_and_keeps_aspect_ratio(self) -> None:
        with open_image(_encode(Image.new("RGB", (400, 200)), "PNG")) as image, image.thumbnail(100, 100) as small:
            assert (small.width, small.height) == (100, 50)

    def test_thumbnail_never_enlarges(self) -> None:
        with open_image(_encode(Image.new("RGB", (40, 20)), "PNG")) as image, image.thumbnail(100, 100) as same:
            assert (same.width, same.height) == (40, 20)

    def test_to_rgb_drops_alpha_and_to_rgba_adds_it(self) -> None:
        with open_image(_encode(Image.new("RGBA", (4, 4)), "PNG")) as image:
            assert image.to_rgb().channels == 3  # noqa: PLR2004
            assert image.to_rgb().to_rgba().channels == 4  # noqa: PLR2004

    def test_rounded_corners_make_corners_transparent(self) -> None:
        with (
            open_image(_encode(Image.new("RGB", (100, 100), "red"), "PNG")) as image,
            image.with_rounded_corners(20) as rounded,
        ):
            decoded = _decode_rgba(rounded.encode("PNG"))

        assert decoded.getpixel((0, 0))[3] == 0  # type: ignore[index]
        assert decoded.getpixel((50, 50)) == (255, 0, 0, 255)

    def test_flattened_composites_onto_background(self) -> None:
        with (
            open_image(_encode(Image.new("RGB", (100, 100), "red"), "PNG")) as image,
            image.with_rounded_corners(20) as rounded,
            rounded.flattened((255, 255, 255)) as flat,
        ):
            assert flat.channels == 3  # noqa: PLR2004
            decoded = _decode_rgba(flat.encode("PNG"))

        assert decoded.getpixel((0, 0)) == (255, 255, 255, 255)
        assert decoded.getpixel((50, 50)) == (255, 0, 0, 255)


@pytest.mark.usefixtures("backend")
class TestMetadata:
    def test_png_text_round_trips_latin1_unicode_and_long_text(self) -> None:
        long_text = '{"nodes": [' + ", ".join(['{"name": "node"}'] * 200) + "]}"
        texts = {"plain": "x", "unicode": "café ☕", "long": long_text}

        png = open_image(_encode(Image.new("RGB", (4, 4)), "PNG")).encode("PNG", text_metadata=texts)

        assert open_image(png).text_metadata == texts
        assert Image.open(BytesIO(png)).info == texts

    def test_text_metadata_is_empty_for_derived_images(self) -> None:
        png = open_image(_encode(Image.new("RGB", (4, 4)), "PNG")).encode("PNG", text_metadata={"k": "v"})

        with open_image(png) as image:
            assert image.thumbnail(2, 2).text_metadata == {}

    def test_exif_driver_round_trips_user_comment_and_keeps_existing_tags(self) -> None:
        source = Image.new("RGB", (8, 8))
        exif = source.getexif()
        exif[0x010F] = "Camera Maker"  # Make

        with open_image(_encode(source, "JPEG", exif=exif)) as image:
            injected = ExifMetadataDriver().inject_metadata(image, {"gtn_key": "value"})

        with open_image(injected) as image:
            metadata = ExifMetadataDriver().extract_metadata(image)

        assert metadata["gtn_key"] == "value"
        assert metadata["Make"] == "Camera Maker"
        assert EXIF_USERCOMMENT_TAG in Image.open(BytesIO(injected)).getexif()


class TestOpenCVEdgeCases:
    def test_oversized_compressed_text_is_skipped(self) -> None:
        bomb = zlib.compress(b"a" * (MAX_COMPRESSED_TEXT_BYTES + 1))
        chunk_data = b"bomb\x00\x00" + bomb
        chunk = struct.pack(">I", len(chunk_data)) + b"zTXt" + chunk_data
        chunk += struct.pack(">I", zlib.crc32(b"zTXt" + chunk_data))
        png = _encode(Image.new("RGB", (2, 2)), "PNG")
        idat = png.index(b"IDAT") - 4

        assert read_text_chunks(png[:idat] + chunk + png[idat:]) == {}

    def test_flattened_keeps_float_precision(self) -> None:
        pixels = np.full((2, 2, 4), [0.6, 0.4, 0.2, 1.0], dtype=np.float32)

        flat = OpenCVImage(source=None, pixels=pixels, image_format="TIFF").flattened((255, 255, 255))

        assert np.allclose(flat._decoded()[0, 0], [0.6, 0.4, 0.2])

    def test_open_rejects_data_that_is_not_an_image(self) -> None:
        with pytest.raises(ValueError, match="not a PNG"):
            OpenCVImage.open(b"<html>not an image</html>")
