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
    EXIF_ORIENTATION_TAG,
    IMAGE_BACKEND_ENV_VAR,
    OPENCV_BACKEND,
    OPENIMAGEIO_BACKEND,
    PILLOW_BACKEND,
    Exif,
    GriptapeImagePipeline,
    ImageFormat,
    ImageMetadata,
    OpenCVImage,
    OpenImageIOImage,
    PillowImage,
    PixelType,
    image_backend_name,
    open_image,
)
from griptape_nodes.image.containers import replace_jpeg_exif
from griptape_nodes.image.formats import sniff_image_format
from griptape_nodes.image.pipeline import Flatten, Orient, RoundCorners, Thumbnail, ToRGB, _ExifTranspose, plan_steps
from griptape_nodes.image.png_text_chunks import MAX_COMPRESSED_TEXT_BYTES, read_text_chunks

_EXIF_MAKE_TAG = 0x010F
_ROTATE_90_CLOCKWISE = 6
# Offset of the bit depth byte in a PNG: signature (8), IHDR length and type (8), width, height (8)
_PNG_BIT_DEPTH_OFFSET = 24


@pytest.fixture(params=[PILLOW_BACKEND, OPENCV_BACKEND, OPENIMAGEIO_BACKEND])
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


def _rotated_jpeg(width: int, height: int) -> bytes:
    """A JPEG stored ``width`` x ``height`` with red on the left half, tagged to display rotated 90 degrees clockwise."""
    source = Image.new("RGB", (width, height), "blue")
    source.paste((255, 0, 0), (0, 0, width // 2, height))
    exif = source.getexif()
    exif[EXIF_ORIENTATION_TAG] = _ROTATE_90_CLOCKWISE
    return _encode(source, "JPEG", exif=exif, quality=95)


def _decode_rgba(data: bytes) -> Image.Image:
    return Image.open(BytesIO(data)).convert("RGBA")


def _exif_of(data: bytes) -> Image.Exif:
    return Image.open(BytesIO(data)).getexif()


class TestBackendSelection:
    def test_defaults_to_pillow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(IMAGE_BACKEND_ENV_VAR, raising=False)

        assert isinstance(open_image(_encode(Image.new("RGB", (2, 2)), "PNG")), PillowImage)

    def test_env_var_selects_opencv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(IMAGE_BACKEND_ENV_VAR, " OpenCV ")

        assert isinstance(open_image(_encode(Image.new("RGB", (2, 2)), "PNG")), OpenCVImage)

    def test_env_var_selects_openimageio(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(IMAGE_BACKEND_ENV_VAR, "openimageio")

        assert isinstance(open_image(_encode(Image.new("RGB", (2, 2)), "PNG")), OpenImageIOImage)

    def test_unknown_value_falls_back_to_pillow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(IMAGE_BACKEND_ENV_VAR, "imagemagick")

        assert image_backend_name() == PILLOW_BACKEND


class TestImageFormat:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("jpg", ImageFormat.JPEG),
            (".JPEG", ImageFormat.JPEG),
            ("Jpe", ImageFormat.JPEG),
            ("tif", ImageFormat.TIFF),
            ("targa", ImageFormat.TGA),
            ("openexr", ImageFormat.EXR),
            ("webp", ImageFormat.WEBP),
        ],
    )
    def test_from_name_accepts_any_spelling(self, name: str, expected: ImageFormat) -> None:
        assert ImageFormat.from_name(name) is expected

    def test_from_name_returns_none_for_unknown(self) -> None:
        assert ImageFormat.from_name("heic") is None

    def test_members_compare_equal_to_their_name(self) -> None:
        assert ImageFormat.JPEG == "JPEG"
        assert ImageFormat.JPEG.extension == "jpg"


class TestPixelType:
    @pytest.mark.parametrize(
        ("dtype", "expected"),
        [(np.uint8, PixelType.UINT8), (np.uint16, PixelType.UINT16), (np.float16, PixelType.HALF)],
    )
    def test_from_numpy(self, dtype: type, expected: PixelType) -> None:
        assert PixelType.from_numpy(np.dtype(dtype)) is expected

    def test_bits_and_float(self) -> None:
        assert PixelType.UINT16.bits == 16  # noqa: PLR2004
        assert PixelType.HALF.is_float
        assert not PixelType.INT32.is_float


class TestReading:
    @pytest.mark.usefixtures("backend")
    @pytest.mark.parametrize(
        ("image_format", "expected"),
        [
            ("PNG", ImageFormat.PNG),
            ("JPEG", ImageFormat.JPEG),
            ("WEBP", ImageFormat.WEBP),
            ("TIFF", ImageFormat.TIFF),
            ("BMP", ImageFormat.BMP),
            ("GIF", ImageFormat.GIF),
        ],
    )
    def test_reports_dimensions_and_format(self, image_format: str, expected: ImageFormat) -> None:
        with open_image(_encode(Image.new("RGB", (30, 20), "red"), image_format)) as image:
            assert (image.width, image.height) == (30, 20)
            assert image.format is expected

    @pytest.mark.usefixtures("backend")
    @pytest.mark.parametrize(
        ("mode", "channels", "color_space"),
        [("RGB", 3, "RGB"), ("RGBA", 4, "RGBA"), ("L", 1, "Grayscale")],
    )
    def test_reports_color_model(self, mode: str, channels: int, color_space: str) -> None:
        with open_image(_encode(Image.new(mode, (4, 4)), "PNG")) as image:
            assert image.channels == channels
            assert image.color_space == color_space
            assert image.pixel_type is PixelType.UINT8

    @pytest.mark.usefixtures("backend")
    def test_opens_from_a_path(self, tmp_path: Path) -> None:
        path = tmp_path / "image.png"
        path.write_bytes(_encode(Image.new("RGB", (5, 3)), "PNG"))

        with open_image(path) as image:
            assert (image.width, image.height) == (5, 3)

    def test_sniffs_format_from_header(self) -> None:
        assert sniff_image_format(_encode(Image.new("RGB", (2, 2)), "JPEG")) is ImageFormat.JPEG
        assert sniff_image_format(b"not an image at all") is None

    def test_opencv_reads_metadata_without_decoding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(IMAGE_BACKEND_ENV_VAR, OPENCV_BACKEND)
        png = open_image(_encode(Image.new("RGB", (2, 2)), "PNG")).encode(
            ImageFormat.PNG, metadata=ImageMetadata(text={"k": "v"})
        )

        image = open_image(png)
        text = image.metadata.text

        assert text == {"k": "v"}
        assert isinstance(image, OpenCVImage)
        assert image._pixels is None


    def test_pillow_reads_png_metadata_without_decoding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(IMAGE_BACKEND_ENV_VAR, PILLOW_BACKEND)
        png = _encode(Image.new("RGB", (2, 2)), "PNG")

        image = open_image(png)
        metadata = image.metadata

        assert metadata == ImageMetadata()
        assert isinstance(image, PillowImage)
        assert image._image._im is None


class TestPixelDepth:
    @pytest.mark.parametrize("deep_backend", [OPENCV_BACKEND, OPENIMAGEIO_BACKEND])
    def test_keeps_sixteen_bit_color_through_a_png_round_trip(
        self, monkeypatch: pytest.MonkeyPatch, deep_backend: str
    ) -> None:
        monkeypatch.setenv(IMAGE_BACKEND_ENV_VAR, deep_backend)

        with open_image(_sixteen_bit_rgb_png()) as image:
            assert image.pixel_type is PixelType.UINT16
            reencoded = image.encode(ImageFormat.PNG, metadata=ImageMetadata(text={"gtn_key": "value"}))

        reread = open_image(reencoded).metadata.text
        assert reencoded[_PNG_BIT_DEPTH_OFFSET] == 16  # noqa: PLR2004
        assert reread == {"gtn_key": "value"}

    def test_pillow_drops_sixteen_bit_color_to_eight(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(IMAGE_BACKEND_ENV_VAR, PILLOW_BACKEND)

        with open_image(_sixteen_bit_rgb_png()) as image:
            assert image.pixel_type is PixelType.UINT8
            reencoded = image.encode(ImageFormat.PNG)

        assert reencoded[_PNG_BIT_DEPTH_OFFSET] == 8  # noqa: PLR2004

    @pytest.mark.usefixtures("backend")
    def test_sixteen_bit_source_encodes_to_eight_bit_formats(self) -> None:
        with open_image(_sixteen_bit_rgb_png()) as image:
            jpeg = image.encode(ImageFormat.JPEG)

        assert sniff_image_format(jpeg) is ImageFormat.JPEG


class TestPlanSteps:
    def test_moves_orientation_after_thumbnail_and_swaps_bounds(self) -> None:
        planned = plan_steps([_ExifTranspose(), ToRGB(), Thumbnail(100, 50)], _ROTATE_90_CLOCKWISE)

        assert planned == [ToRGB(), Thumbnail(50, 100, transposed=True), Orient(_ROTATE_90_CLOCKWISE)]

    def test_applies_only_the_first_exif_transpose(self) -> None:
        planned = plan_steps([_ExifTranspose(), ToRGB(), _ExifTranspose()], _ROTATE_90_CLOCKWISE)

        assert planned == [ToRGB(), Orient(_ROTATE_90_CLOCKWISE)]

    def test_keeps_orientation_before_steps_that_depend_on_it(self) -> None:
        planned = plan_steps([_ExifTranspose(), RoundCorners(0.1), Flatten((0, 0, 0))], 3)

        assert planned == [Orient(3), RoundCorners(0.1), Flatten((0, 0, 0))]

    def test_drops_an_upright_orientation(self) -> None:
        assert plan_steps([_ExifTranspose(), Thumbnail(10, 10)], 1) == [Thumbnail(10, 10)]


@pytest.mark.usefixtures("backend")
class TestPipeline:
    def test_exif_transpose_applies_orientation(self) -> None:
        with (
            open_image(_rotated_jpeg(40, 20)) as image,
            GriptapeImagePipeline(image).exif_transpose().end() as upright,
        ):
            decoded = _decode_rgba(upright.encode(ImageFormat.PNG))

        # Rotated clockwise, the red left half ends up on top
        assert decoded.size == (20, 40)
        red, _, blue, _ = decoded.getpixel((10, 5))  # type: ignore[misc]
        assert red > 200 > blue  # noqa: PLR2004

    def test_exif_transpose_after_thumbnail_matches_before(self) -> None:
        with (
            open_image(_rotated_jpeg(400, 200)) as image,
            GriptapeImagePipeline(image).exif_transpose().thumbnail(100, 50).end() as preview,
        ):
            decoded = _decode_rgba(preview.encode(ImageFormat.PNG))

        # The bounds apply to the upright image, so it fits 100x50 as 25x50
        assert decoded.size == (25, 50)
        red, _, blue, _ = decoded.getpixel((12, 5))  # type: ignore[misc]
        assert red > 200 > blue  # noqa: PLR2004

    def test_exif_transpose_after_thumbnail_rounds_like_shrinking_upright(self) -> None:
        # Pillow rounds a thumbnail's two sides differently, so shrinking the stored 364x11 into
        # swapped bounds would come out 9x281 rather than the 8x281 shrinking upright gives
        with open_image(_rotated_jpeg(364, 11)) as image:
            upright = Image.open(BytesIO(image.encode(ImageFormat.PNG))).transpose(Image.Transpose.ROTATE_270)
            upright.thumbnail((280, 281))
            with GriptapeImagePipeline(image).exif_transpose().thumbnail(280, 281).end() as preview:
                size = (preview.width, preview.height)

        assert size == upright.size

    def test_failed_step_still_closes_the_intermediate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        closed = []
        with open_image(_encode(Image.new("RGB", (40, 20)), "PNG")) as image:
            original_apply = type(image)._apply_step

            def apply_step(current: object, step: object) -> object:
                if isinstance(step, Flatten):
                    msg = "boom"
                    raise RuntimeError(msg)  # noqa: TRY004
                following = original_apply(current, step)  # type: ignore[arg-type]
                original_close = following.close

                def close() -> None:
                    closed.append(following)
                    original_close()

                following.close = close  # type: ignore[method-assign]
                return following

            monkeypatch.setattr(type(image), "_apply_step", apply_step)
            pipeline = GriptapeImagePipeline(image).to_rgba().flatten((0, 0, 0))
            with pytest.raises(RuntimeError, match="boom"):
                pipeline.end()

        assert len(closed) == 1

    def test_exif_transpose_without_orientation_keeps_dimensions(self) -> None:
        with (
            open_image(_encode(Image.new("RGB", (40, 20)), "PNG")) as image,
            GriptapeImagePipeline(image).exif_transpose().end() as upright,
        ):
            assert (upright.width, upright.height) == (40, 20)

    def test_thumbnail_fits_bounds_and_keeps_aspect_ratio(self) -> None:
        with (
            open_image(_encode(Image.new("RGB", (400, 200)), "PNG")) as image,
            GriptapeImagePipeline(image) as pipeline,
            pipeline.thumbnail(100, 100).end() as small,
        ):
            assert (small.width, small.height) == (100, 50)

    def test_thumbnail_of_a_large_jpeg_matches_the_bounds(self) -> None:
        # Large enough that Pillow and OpenCV decode at reduced size first
        source = _encode(Image.new("RGB", (1600, 1000), "green"), "JPEG")

        with (
            open_image(source) as image,
            GriptapeImagePipeline(image).thumbnail(160, 160).end() as small,
        ):
            assert (small.width, small.height) == (160, 100)

    def test_thumbnail_never_enlarges(self) -> None:
        with (
            open_image(_encode(Image.new("RGB", (40, 20)), "PNG")) as image,
            GriptapeImagePipeline(image).thumbnail(100, 100).end() as same,
        ):
            assert (same.width, same.height) == (40, 20)

    def test_thumbnail_defaults_to_the_preview_size(self) -> None:
        with (
            open_image(_encode(Image.new("RGB", (2048, 512)), "PNG")) as image,
            GriptapeImagePipeline(image) as pipeline,
            pipeline.thumbnail().end() as small,
        ):
            assert (small.width, small.height) == (1024, 256)

    def test_to_rgb_drops_alpha_and_to_rgba_adds_it(self) -> None:
        with open_image(_encode(Image.new("RGBA", (4, 4)), "PNG")) as image:
            with GriptapeImagePipeline(image).to_rgb().end() as rgb:
                assert rgb.channels == 3  # noqa: PLR2004
            with GriptapeImagePipeline(image).to_rgb().to_rgba().end() as rgba:
                assert rgba.channels == 4  # noqa: PLR2004

    def test_end_without_steps_returns_a_copy(self) -> None:
        with open_image(_encode(Image.new("RGB", (6, 4)), "PNG")) as image, GriptapeImagePipeline(image).end() as copy:
            assert copy is not image
            assert (copy.width, copy.height) == (6, 4)

    def test_round_corners_make_corners_transparent(self) -> None:
        with (
            open_image(_encode(Image.new("RGB", (100, 100), "red"), "PNG")) as image,
            GriptapeImagePipeline(image).round_corners(0.2).end() as rounded,
        ):
            decoded = _decode_rgba(rounded.encode(ImageFormat.PNG))

        assert decoded.getpixel((0, 0))[3] == 0  # type: ignore[index]
        assert decoded.getpixel((50, 50)) == (255, 0, 0, 255)

    def test_flatten_composites_onto_background(self) -> None:
        with (
            open_image(_encode(Image.new("RGB", (100, 100), "red"), "PNG")) as image,
            GriptapeImagePipeline(image).round_corners(0.2).flatten((255, 255, 255)).end() as flat,
        ):
            assert flat.channels == 3  # noqa: PLR2004
            decoded = _decode_rgba(flat.encode(ImageFormat.PNG))

        assert decoded.getpixel((0, 0)) == (255, 255, 255, 255)
        assert decoded.getpixel((50, 50)) == (255, 0, 0, 255)

    def test_leaves_the_source_unchanged(self) -> None:
        with open_image(_encode(Image.new("RGB", (40, 20)), "PNG")) as image:
            with GriptapeImagePipeline(image).thumbnail(10, 10).end():
                pass
            assert (image.width, image.height) == (40, 20)


@pytest.mark.usefixtures("backend")
class TestAlpha:
    @pytest.mark.parametrize("image_format", [ImageFormat.WEBP, ImageFormat.PNG, ImageFormat.TIFF])
    def test_grayscale_with_alpha_encodes(self, image_format: ImageFormat) -> None:
        with (
            open_image(_encode(Image.new("LA", (8, 8), (128, 200)), "PNG")) as image,
            GriptapeImagePipeline(image).thumbnail(4, 4).end() as small,
        ):
            encoded = small.encode(image_format)

        size = Image.open(BytesIO(encoded)).size
        assert size == (4, 4)

    def test_thumbnail_keeps_transparent_color_out_of_edges(self) -> None:
        source = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        source.paste((255, 255, 255, 255), (32, 0, 64, 64))

        with (
            open_image(_encode(source, "PNG")) as image,
            GriptapeImagePipeline(image).thumbnail(8, 8).end() as small,
        ):
            decoded = _decode_rgba(small.encode(ImageFormat.PNG))

        for x in range(8):
            red, _, _, alpha = decoded.getpixel((x, 4))  # type: ignore[misc]
            if alpha > 64:  # noqa: PLR2004
                assert red > 200  # noqa: PLR2004


@pytest.mark.usefixtures("backend")
class TestMetadata:
    def test_png_text_round_trips_latin1_unicode_and_long_text(self) -> None:
        long_text = '{"nodes": [' + ", ".join(['{"name": "node"}'] * 200) + "]}"
        texts = {"plain": "x", "unicode": "café ☕", "long": long_text}

        png = open_image(_encode(Image.new("RGB", (4, 4)), "PNG")).encode(
            ImageFormat.PNG, metadata=ImageMetadata(text=texts)
        )

        reread = open_image(png).metadata.text
        pillow_info = Image.open(BytesIO(png)).info
        assert reread == texts
        assert pillow_info == texts

    def test_metadata_is_empty_for_pipeline_output(self) -> None:
        png = open_image(_encode(Image.new("RGB", (4, 4)), "PNG")).encode(
            ImageFormat.PNG, metadata=ImageMetadata(text={"k": "v"})
        )

        with open_image(png) as image, GriptapeImagePipeline(image).thumbnail(2, 2).end() as small:
            assert small.metadata == ImageMetadata()

    def test_malformed_exif_reads_as_none(self) -> None:
        source = replace_jpeg_exif(_encode(Image.new("RGB", (8, 4)), "JPEG"), b"not a TIFF block")

        with open_image(source) as image:
            exif = image.metadata.exif
            with GriptapeImagePipeline(image).exif_transpose().end() as upright:
                size = (upright.width, upright.height)

        assert exif is None
        assert size == (8, 4)

    def test_reads_parsed_exif(self) -> None:
        with open_image(_rotated_jpeg(8, 4)) as image:
            exif = image.metadata.exif

        assert exif is not None
        assert exif.orientation == _ROTATE_90_CLOCKWISE
        assert exif.swaps_dimensions

    def test_exif_driver_round_trips_user_comment_and_keeps_existing_tags(self) -> None:
        source = Image.new("RGB", (8, 8))
        exif = source.getexif()
        exif[_EXIF_MAKE_TAG] = "Camera Maker"

        with open_image(_encode(source, "JPEG", exif=exif)) as image:
            injected = ExifMetadataDriver().inject_metadata(image, {"gtn_key": "value"})

        with open_image(injected) as image:
            metadata = ExifMetadataDriver().extract_metadata(image)

        injected_exif = _exif_of(injected)
        assert metadata["gtn_key"] == "value"
        assert metadata["Make"] == "Camera Maker"
        assert EXIF_USERCOMMENT_TAG in injected_exif

    @pytest.mark.parametrize(("mode", "color"), [("RGB", (255, 0, 0)), ("RGBA", (255, 0, 0, 128))])
    def test_exif_round_trips_through_webp(self, mode: str, color: tuple[int, ...]) -> None:
        source = Image.new(mode, (8, 8), color)
        exif = source.getexif()
        exif[_EXIF_MAKE_TAG] = "Camera Maker"

        with open_image(_encode(source, "WEBP", exif=exif)) as image:
            webp = image.encode(ImageFormat.WEBP, metadata=image.metadata)

        decoded = Image.open(BytesIO(webp))
        assert decoded.getexif()[_EXIF_MAKE_TAG] == "Camera Maker"
        assert decoded.mode == mode

    def test_encode_writes_only_the_given_exif(self) -> None:
        source = Image.new("RGB", (8, 8))
        exif = source.getexif()
        exif[_EXIF_MAKE_TAG] = "Camera Maker"

        with open_image(_encode(source, "JPEG", exif=exif)) as image:
            stripped = image.encode(ImageFormat.JPEG)
            kept = image.encode(ImageFormat.PNG, metadata=image.metadata)

        stripped_exif = _exif_of(stripped)
        kept_exif = _exif_of(kept)
        assert not stripped_exif
        assert kept_exif[_EXIF_MAKE_TAG] == "Camera Maker"

    def test_written_exif_is_edited_through_exif(self) -> None:
        exif = Exif()
        exif[_EXIF_MAKE_TAG] = "Camera Maker"

        with open_image(_encode(Image.new("RGB", (8, 8)), "PNG")) as image:
            jpeg = image.encode(ImageFormat.JPEG, metadata=ImageMetadata(exif=exif))

        written = _exif_of(jpeg)
        assert written[_EXIF_MAKE_TAG] == "Camera Maker"


class TestOpenImageIOEdgeCases:
    def test_reads_metadata_without_decoding(self) -> None:
        source = Image.new("RGB", (8, 8))
        exif = source.getexif()
        exif[_EXIF_MAKE_TAG] = "Camera Maker"

        image = OpenImageIOImage.open(_encode(source, "JPEG", exif=exif))
        parsed = image.metadata.exif

        assert parsed is not None
        assert parsed[_EXIF_MAKE_TAG] == "Camera Maker"
        assert image._buf is None

    def test_reads_and_writes_targa(self) -> None:
        with OpenImageIOImage.open(_encode(Image.new("RGB", (6, 4), "red"), "TGA")) as image:
            assert image.format is ImageFormat.TGA
            assert (image.width, image.height) == (6, 4)
            encoded = image.encode(ImageFormat.TGA)

        size = Image.open(BytesIO(encoded)).size
        assert size == (6, 4)

    def test_reads_tiff_orientation(self) -> None:
        source = Image.new("RGB", (8, 4))
        exif = source.getexif()
        exif[EXIF_ORIENTATION_TAG] = _ROTATE_90_CLOCKWISE

        with (
            OpenImageIOImage.open(_encode(source, "TIFF", exif=exif)) as image,
            GriptapeImagePipeline(image).exif_transpose().end() as upright,
        ):
            assert (upright.width, upright.height) == (4, 8)

    def test_open_rejects_data_that_is_not_an_image(self) -> None:
        with pytest.raises(ValueError, match="could not read"):
            OpenImageIOImage.open(b"<html>not an image</html>")


class TestOpenCVEdgeCases:
    def test_oversized_compressed_text_is_skipped(self) -> None:
        bomb = zlib.compress(b"a" * (MAX_COMPRESSED_TEXT_BYTES + 1))
        chunk_data = b"bomb\x00\x00" + bomb
        chunk = struct.pack(">I", len(chunk_data)) + b"zTXt" + chunk_data
        chunk += struct.pack(">I", zlib.crc32(b"zTXt" + chunk_data))
        png = _encode(Image.new("RGB", (2, 2)), "PNG")
        idat = png.index(b"IDAT") - 4

        assert read_text_chunks(png[:idat] + chunk + png[idat:]) == {}

    def test_flatten_keeps_float_precision(self) -> None:
        pixels = np.full((2, 2, 4), [0.6, 0.4, 0.2, 1.0], dtype=np.float32)
        image = OpenCVImage(source=None, pixels=pixels, image_format=ImageFormat.TIFF)

        with GriptapeImagePipeline(image).flatten((255, 255, 255)).end() as flat:
            assert isinstance(flat, OpenCVImage)
            assert np.allclose(flat._decoded()[0, 0], [0.6, 0.4, 0.2])

    def test_reduced_jpeg_decode_keeps_grayscale_gray(self) -> None:
        source = _encode(Image.new("L", (1600, 1000), 128), "JPEG")

        with OpenCVImage.open(source) as image, GriptapeImagePipeline(image).thumbnail(160, 160).end() as small:
            assert (small.width, small.height) == (160, 100)
            decoded = _decode_rgba(small.encode(ImageFormat.PNG))

        red, green, blue, _ = decoded.getpixel((80, 50))  # type: ignore[misc]
        assert abs(red - 128) <= 2 and red == green == blue  # noqa: PLR2004, PT018

    def test_open_rejects_data_that_is_not_an_image(self) -> None:
        with pytest.raises(ValueError, match="not a PNG"):
            OpenCVImage.open(b"<html>not an image</html>")
