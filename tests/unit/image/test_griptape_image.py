"""Decoding, transforming, and encoding images through GriptapeImage."""

import logging
import struct
import zlib
from pathlib import Path

import numpy as np
import OpenImageIO as oiio  # noqa: N813
import pytest
from pytest_mock import MockerFixture

from griptape_nodes.drivers.image_metadata.exif_metadata_driver import EXIF_USERCOMMENT_TAG, ExifMetadataDriver
from griptape_nodes.image import (
    EXIF_GPSINFO_TAG,
    EXIF_ORIENTATION_TAG,
    Exif,
    ExifRational,
    GriptapeImage,
    GriptapeImagePipeline,
    ImageFormat,
    ImageMetadata,
    PixelType,
)
from griptape_nodes.image.containers import (
    MAX_ICC_PROFILE_BYTES,
    read_exif,
    read_extra_metadata_kinds,
    read_icc_profile,
    replace_jpeg_exif,
)
from griptape_nodes.image.exif import EXIF_PIXEL_X_DIMENSION_TAG, EXIF_PIXEL_Y_DIMENSION_TAG, tiff_metadata
from griptape_nodes.image.formats import sniff_image_format
from griptape_nodes.image.pipeline import Flatten, Orient, RoundCorners, Thumbnail, ToRGB, _ExifTranspose, _plan_steps
from griptape_nodes.image.png_text_chunks import (
    MAX_COMPRESSED_TEXT_BYTES,
    insert_text_chunks,
    png_chunk,
    read_text_chunks,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.rounded_preview_generator import (
    RoundedPreviewGenerator,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.thumbnail_generator import (
    ThumbnailGenerator,
)
from griptape_nodes.utils.image_preview import create_image_preview_from_bytes
from tests.unit.image.sample_images import (
    BLUE,
    GREEN,
    RED,
    SRGB_PROFILE,
    WHITE,
    decode,
    encode,
    gray_tiff,
    icc_profile_of,
    noise,
    palette_png,
    read_spec,
    size_of,
    solid,
)

_EXIF_MAKE_TAG = 0x010F
_EXIF_DATETIME_ORIGINAL_TAG = 0x9003
_GPS_LATITUDE_REF_TAG = 0x0001
_ROTATE_90_CLOCKWISE = 6
# Offset of the bit depth byte in a PNG: signature (8), IHDR length and type (8), width, height (8)
_PNG_BIT_DEPTH_OFFSET = 24
_JPEG_XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"
_EXIF_MAKER_NOTE_TAG = 0x927C
_TIFF_XMP_TAG = 0x02BC
_TIFF_ASCII = 2
_TIFF_BYTE = 1
# More than the 64 KB one JPEG segment holds
_OVERSIZE_BYTES = 70_000


def _png(width: int, height: int, color: tuple[int, ...] | int = WHITE) -> bytes:
    return encode(solid(width, height, color), "png")


def _camera_exif() -> Exif:
    exif = Exif()
    exif[_EXIF_MAKE_TAG] = "Camera Maker"
    return exif


def _with_exif(jpeg: bytes, exif: Exif) -> bytes:
    return replace_jpeg_exif(jpeg, exif.to_bytes())


def _rotated_jpeg(width: int, height: int, exif: Exif | None = None) -> bytes:
    """A JPEG stored ``width`` x ``height`` with red on the left half, tagged to display rotated 90 degrees clockwise."""
    pixels = solid(width, height, BLUE)
    pixels[:, : width // 2] = RED
    if exif is None:
        exif = Exif()
    exif[EXIF_ORIENTATION_TAG] = _ROTATE_90_CLOCKWISE
    return _with_exif(encode(pixels, "jpg", compression="jpeg:95"), exif)


def _with_gps(exif: Exif) -> Exif:
    """``exif`` plus a GPS sub-IFD holding GPSLatitudeRef "N", built as a camera writes it."""
    gps_entry = struct.pack("<HHI4s", _GPS_LATITUDE_REF_TAG, 2, 2, b"N\x00\x00\x00")
    gps_ifd = struct.pack("<H", 1) + gps_entry + struct.pack("<I", 0)
    pointer = struct.pack("<HHII", EXIF_GPSINFO_TAG, 4, 1, 26)
    block = b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", 1) + pointer + struct.pack("<I", 0) + gps_ifd
    merged = Exif.from_bytes(block)
    for tag, value in exif.items():
        merged[tag] = value
    return merged


def _webp(pixels: np.ndarray, metadata: ImageMetadata) -> bytes:
    """A WEBP holding ``metadata``. OpenImageIO's WEBP writer keeps only the color profile, so EXIF is added by encode."""
    return _from_pixels(pixels, metadata).encode(ImageFormat.WEBP)


def _with_xmp(jpeg: bytes) -> bytes:
    payload = _JPEG_XMP_HEADER + b"<x:xmpmeta/>"
    segment = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    return jpeg[:2] + segment + jpeg[2:]


def _from_pixels(pixels: np.ndarray, metadata: ImageMetadata | None = None) -> GriptapeImage:
    if metadata is None:
        metadata = ImageMetadata()
    return GriptapeImage(source=None, source_path=None, buf=oiio.ImageBuf(pixels), image_format=None, metadata=metadata)


def _float_pixels(image: GriptapeImage) -> np.ndarray:
    pixels = image._decoded().get_pixels(oiio.FLOAT)
    assert isinstance(pixels, np.ndarray)
    return pixels


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
    @pytest.mark.parametrize(
        ("extension", "expected"),
        [
            ("png", ImageFormat.PNG),
            ("jpg", ImageFormat.JPEG),
            ("webp", ImageFormat.WEBP),
            ("tif", ImageFormat.TIFF),
            ("bmp", ImageFormat.BMP),
            ("gif", ImageFormat.GIF),
            ("tga", ImageFormat.TGA),
            ("exr", ImageFormat.EXR),
        ],
    )
    def test_reports_dimensions_and_format(self, extension: str, expected: ImageFormat) -> None:
        with GriptapeImage.open(encode(solid(30, 20, RED), extension)) as image:
            assert (image.width, image.height) == (30, 20)
            assert image.format is expected

    @pytest.mark.parametrize(
        ("color", "channels", "color_space"),
        [(RED, 3, "RGB"), ((*RED, 255), 4, "RGBA"), (128, 1, "Grayscale"), ((128, 255), 2, "Grayscale+Alpha")],
    )
    def test_reports_color_model(self, color: tuple[int, ...] | int, channels: int, color_space: str) -> None:
        with GriptapeImage.open(_png(4, 4, color)) as image:
            assert image.channels == channels
            assert image.color_space == color_space
            assert image.pixel_type is PixelType.UINT8

    def test_palette_png_decodes_to_full_color(self) -> None:
        with GriptapeImage.open(palette_png(4, 4, [BLUE, WHITE])) as image:
            assert image.color_space == "RGB"
            pixels = decode(image.encode(ImageFormat.PNG))

        assert tuple(pixels[0, 0]) == BLUE

    def test_opens_from_a_path(self, tmp_path: Path) -> None:
        path = tmp_path / "image.png"
        path.write_bytes(_png(5, 3))

        with GriptapeImage.open(path) as image:
            assert (image.width, image.height) == (5, 3)

    def test_sniffs_format_from_header(self) -> None:
        assert sniff_image_format(encode(solid(2, 2), "jpg")) is ImageFormat.JPEG
        assert sniff_image_format(b"not an image at all") is None

    @pytest.mark.parametrize("extension", ["jpg", "png", "webp", "tif"])
    def test_reads_metadata_without_decoding(self, extension: str) -> None:
        if extension == "webp":
            source = _webp(solid(8, 8), ImageMetadata(exif=_camera_exif(), icc_profile=SRGB_PROFILE))
        else:
            source = encode(solid(8, 8), extension, icc_profile=SRGB_PROFILE, Make="Camera Maker")

        image = GriptapeImage.open(source)
        metadata = image.metadata

        assert metadata.exif is not None
        assert metadata.exif[_EXIF_MAKE_TAG] == "Camera Maker"
        assert metadata.icc_profile == SRGB_PROFILE
        assert image._buf is None

    @pytest.mark.parametrize(("extension", "expected"), [("tga", ImageFormat.TGA), ("exr", ImageFormat.EXR)])
    def test_learns_unsniffed_formats_from_the_header(self, extension: str, expected: ImageFormat) -> None:
        image = GriptapeImage.open(encode(solid(6, 4, RED), extension))

        assert image.format is expected
        assert image._buf is None

    def test_open_rejects_data_that_is_not_an_image(self) -> None:
        with pytest.raises(ValueError, match="could not read"):
            GriptapeImage.open(b"<html>not an image</html>")

    def test_truncated_source_is_rejected(self) -> None:
        png = encode(noise(64, 64), "png")

        image = GriptapeImage.open(png[: len(png) // 2])

        with pytest.raises(ValueError, match="could not read"):
            image.encode(ImageFormat.PNG, metadata=ImageMetadata(text={"k": "v"}))

    def test_closed_image_has_no_pixels(self) -> None:
        image = GriptapeImage.open(_png(2, 2))
        image.close()

        assert image.metadata == ImageMetadata()
        with pytest.raises(ValueError, match="was closed"):
            _ = image.width

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            (encode(solid(6, 4, RED), "jpg"), (6, 4, 3, PixelType.UINT8)),
            (encode(np.full((4, 6, 4), 40000, dtype=np.uint16), "png"), (6, 4, 4, PixelType.UINT16)),
            (palette_png(6, 4), (6, 4, 3, PixelType.UINT8)),
            (encode(solid(6, 4, (128, 255)), "png"), (6, 4, 2, PixelType.UINT8)),
            (encode(np.zeros((4, 6, 3), dtype=np.float16), "exr"), (6, 4, 3, PixelType.HALF)),
        ],
    )
    def test_reads_the_header_without_decoding(self, source: bytes, expected: tuple) -> None:
        with GriptapeImage.open(source) as image:
            assert (image.width, image.height, image.channels, image.pixel_type) == expected
            assert image._buf is None
            header = (image.width, image.height, image.channels, image.pixel_type)
            image._decoded()
            assert (image.width, image.height, image.channels, image.pixel_type) == header

    @pytest.mark.parametrize("extension", ["bmp", "gif", "tga"])
    def test_reads_metadata_of_other_formats_without_decoding(self, extension: str) -> None:
        with GriptapeImage.open(encode(solid(6, 4, RED), extension)) as image:
            assert image.metadata.text == {}
            assert image._buf is None

    def test_unreadable_header_raises_on_metadata(self) -> None:
        image = GriptapeImage.open(b"BM" + bytes(64))

        with pytest.raises(ValueError, match="could not read"):
            _ = image.metadata


class TestPixelDepth:
    def test_keeps_sixteen_bit_color_through_a_png_round_trip(self) -> None:
        gradient = (np.arange(48 * 64 * 3, dtype=np.uint32).reshape(48, 64, 3) * 7 % 65536).astype(np.uint16)

        with GriptapeImage.open(encode(gradient, "png")) as image:
            assert image.pixel_type is PixelType.UINT16
            reencoded = image.encode(ImageFormat.PNG, metadata=ImageMetadata(text={"gtn_key": "value"}))

        reread = GriptapeImage.open(reencoded).metadata.text
        assert reencoded[_PNG_BIT_DEPTH_OFFSET] == 16  # noqa: PLR2004
        assert reread == {"gtn_key": "value"}

    def test_keeps_float_pixels_through_exr(self) -> None:
        pixels = np.full((4, 4, 3), [0.25, 2.5, -0.5], dtype=np.float32)

        with GriptapeImage.open(encode(pixels, "exr")) as image:
            encoded = image.encode(ImageFormat.EXR)

        with GriptapeImage.open(encoded) as reread:
            assert reread.pixel_type.is_float
            assert np.allclose(_float_pixels(reread)[0, 0], [0.25, 2.5, -0.5])

    def test_sixteen_bit_source_encodes_to_eight_bit_formats(self) -> None:
        with GriptapeImage.open(encode(np.full((4, 4, 3), 40000, dtype=np.uint16), "png")) as image:
            jpeg = image.encode(ImageFormat.JPEG)

        assert sniff_image_format(jpeg) is ImageFormat.JPEG

    def test_flatten_keeps_float_precision(self) -> None:
        image = _from_pixels(np.full((2, 2, 4), [0.6, 0.4, 0.2, 1.0], dtype=np.float32))

        with GriptapeImagePipeline(image).flatten((255, 255, 255)).end() as flat:
            assert flat.pixel_type is PixelType.FLOAT
            assert np.allclose(_float_pixels(flat)[0, 0], [0.6, 0.4, 0.2])

    @pytest.mark.parametrize("image_format", [ImageFormat.PNG, ImageFormat.JPEG, ImageFormat.WEBP])
    def test_float_pixels_get_the_srgb_curve_in_display_formats(self, image_format: ImageFormat) -> None:
        # Linear 0.214 is sRGB 0.5; alpha stays linear
        image = _from_pixels(np.full((4, 4, 4), [0.214, 0.0, 1.0, 0.5], dtype=np.float32))

        pixel = decode(image.encode(image_format))[0, 0]

        assert abs(int(pixel[0]) - 128) <= 2  # noqa: PLR2004
        assert pixel[2] == 255  # noqa: PLR2004
        if image_format != ImageFormat.JPEG:
            assert abs(int(pixel[3]) - 128) <= 1

    def test_exr_preview_is_not_dark(self) -> None:
        source = encode(np.full((8, 8, 3), 0.214, dtype=np.float32), "exr")

        with GriptapeImage.open(source) as image, GriptapeImagePipeline(image).thumbnail(4, 4).end() as small:
            pixel = decode(small.encode(ImageFormat.WEBP))[0, 0]

        assert abs(int(pixel[0]) - 128) <= 2  # noqa: PLR2004

    def test_float_pixels_with_a_profile_keep_their_values(self) -> None:
        # The profile says the values are sRGB already; the curve would encode them twice
        source = encode(np.full((4, 4, 3), 0.5, dtype=np.float32), "tif", icc_profile=SRGB_PROFILE)

        with GriptapeImage.open(source) as image:
            png = image.encode(ImageFormat.PNG)

        assert abs(int(decode(png)[0, 0, 0]) - 128) <= 1
        assert icc_profile_of(png) == SRGB_PROFILE

    def test_flattened_float_pixels_with_a_profile_keep_their_values(self) -> None:
        source = encode(np.full((4, 4, 4), [0.5, 0.5, 0.5, 1.0], dtype=np.float32), "tif", icc_profile=SRGB_PROFILE)

        with GriptapeImage.open(source) as image, GriptapeImagePipeline(image).flatten(WHITE).end() as flat:
            png = flat.encode(ImageFormat.PNG)

        assert abs(int(decode(png)[0, 0, 0]) - 128) <= 1

    @pytest.mark.parametrize("image_format", [ImageFormat.EXR, ImageFormat.TIFF])
    def test_float_pixels_stay_linear_in_float_formats(self, image_format: ImageFormat) -> None:
        image = _from_pixels(np.full((2, 2, 3), [0.214, 2.5, 0.0], dtype=np.float32))

        with GriptapeImage.open(image.encode(image_format)) as reread:
            assert np.allclose(_float_pixels(reread)[0, 0], [0.214, 2.5, 0.0], atol=1e-3)


class TestPlanSteps:
    def test_moves_orientation_after_thumbnail_and_swaps_bounds(self) -> None:
        planned = _plan_steps([_ExifTranspose(), ToRGB(), Thumbnail(100, 50)], _ROTATE_90_CLOCKWISE)

        assert planned == [ToRGB(), Thumbnail(50, 100), Orient(_ROTATE_90_CLOCKWISE)]

    def test_applies_only_the_first_exif_transpose(self) -> None:
        planned = _plan_steps([_ExifTranspose(), ToRGB(), _ExifTranspose()], _ROTATE_90_CLOCKWISE)

        assert planned == [ToRGB(), Orient(_ROTATE_90_CLOCKWISE)]

    def test_keeps_orientation_before_steps_that_depend_on_it(self) -> None:
        planned = _plan_steps([_ExifTranspose(), RoundCorners(0.1), Flatten((0, 0, 0))], 3)

        assert planned == [Orient(3), RoundCorners(0.1), Flatten((0, 0, 0))]

    def test_drops_an_upright_orientation(self) -> None:
        assert _plan_steps([_ExifTranspose(), Thumbnail(10, 10)], 1) == [Thumbnail(10, 10)]


class TestPipeline:
    def test_exif_transpose_applies_orientation(self) -> None:
        with (
            GriptapeImage.open(_rotated_jpeg(40, 20)) as image,
            GriptapeImagePipeline(image).exif_transpose().end() as upright,
        ):
            pixels = decode(upright.encode(ImageFormat.PNG))

        # Rotated clockwise, the red left half ends up on top
        assert pixels.shape[:2] == (40, 20)
        red, _, blue = pixels[5, 10]
        assert red > 200 > blue  # noqa: PLR2004

    def test_exif_transpose_after_thumbnail_matches_before(self) -> None:
        with (
            GriptapeImage.open(_rotated_jpeg(400, 200)) as image,
            GriptapeImagePipeline(image).exif_transpose().thumbnail(100, 50).end() as preview,
        ):
            pixels = decode(preview.encode(ImageFormat.PNG))

        # The bounds apply to the upright image, so it fits 100x50 as 25x50
        assert pixels.shape[:2] == (50, 25)
        red, _, blue = pixels[5, 12]
        assert red > 200 > blue  # noqa: PLR2004

    def test_exif_transpose_after_thumbnail_rounds_like_shrinking_upright(self) -> None:
        with GriptapeImage.open(_rotated_jpeg(364, 11)) as image:
            with (
                GriptapeImagePipeline(image).exif_transpose().end() as upright,
                GriptapeImagePipeline(upright).thumbnail(280, 281).end() as expected,
            ):
                expected_size = (expected.width, expected.height)
            with GriptapeImagePipeline(image).exif_transpose().thumbnail(280, 281).end() as preview:
                size = (preview.width, preview.height)

        assert size == expected_size

    def test_failed_step_still_closes_the_intermediate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        closed = []
        original_apply = GriptapeImage._apply_step

        def apply_step(current: GriptapeImage, step: object) -> GriptapeImage:
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

        with GriptapeImage.open(_png(40, 20)) as image:
            monkeypatch.setattr(GriptapeImage, "_apply_step", apply_step)
            pipeline = GriptapeImagePipeline(image).to_rgba().flatten((0, 0, 0))
            with pytest.raises(RuntimeError, match="boom"):
                pipeline.end()

        assert len(closed) == 1

    def test_exif_transpose_without_orientation_keeps_dimensions(self) -> None:
        with (
            GriptapeImage.open(_png(40, 20)) as image,
            GriptapeImagePipeline(image).exif_transpose().end() as upright,
        ):
            assert (upright.width, upright.height) == (40, 20)

    def test_thumbnail_fits_bounds_and_keeps_aspect_ratio(self) -> None:
        with (
            GriptapeImage.open(_png(400, 200)) as image,
            GriptapeImagePipeline(image).thumbnail(100, 100).end() as small,
        ):
            assert (small.width, small.height) == (100, 50)

    def test_thumbnail_of_a_large_jpeg_matches_the_bounds(self) -> None:
        with (
            GriptapeImage.open(encode(solid(1600, 1000, GREEN), "jpg")) as image,
            GriptapeImagePipeline(image).thumbnail(160, 160).end() as small,
        ):
            assert (small.width, small.height) == (160, 100)

    def test_thumbnail_of_a_gray_jpeg_stays_gray(self) -> None:
        with (
            GriptapeImage.open(encode(solid(1600, 1000, 128), "jpg")) as image,
            GriptapeImagePipeline(image).thumbnail(160, 160).to_rgb().end() as small,
        ):
            pixels = decode(small.encode(ImageFormat.PNG))

        red, green, blue = (int(value) for value in pixels[50, 80])
        assert abs(red - 128) <= 2 and red == green == blue  # noqa: PLR2004, PT018

    def test_thumbnail_never_enlarges(self) -> None:
        with (
            GriptapeImage.open(_png(40, 20)) as image,
            GriptapeImagePipeline(image).thumbnail(100, 100).end() as same,
        ):
            assert (same.width, same.height) == (40, 20)

    def test_thumbnail_defaults_to_the_preview_size(self) -> None:
        with (
            GriptapeImage.open(_png(2048, 512)) as image,
            GriptapeImagePipeline(image).thumbnail().end() as small,
        ):
            assert (small.width, small.height) == (1024, 256)

    def test_to_rgb_drops_alpha_and_to_rgba_adds_it(self) -> None:
        with GriptapeImage.open(_png(4, 4, (*RED, 255))) as image:
            with GriptapeImagePipeline(image).to_rgb().end() as rgb:
                assert rgb.channels == 3  # noqa: PLR2004
            with GriptapeImagePipeline(image).to_rgb().to_rgba().end() as rgba:
                assert rgba.channels == 4  # noqa: PLR2004

    def test_end_without_steps_returns_a_copy(self) -> None:
        with GriptapeImage.open(_png(6, 4)) as image, GriptapeImagePipeline(image).end() as copy:
            assert copy is not image
            assert (copy.width, copy.height) == (6, 4)

    def test_round_corners_make_corners_transparent(self) -> None:
        with (
            GriptapeImage.open(_png(100, 100, RED)) as image,
            GriptapeImagePipeline(image).round_corners(0.2).end() as rounded,
        ):
            pixels = decode(rounded.encode(ImageFormat.PNG))

        assert pixels[0, 0, 3] == 0
        assert tuple(pixels[50, 50]) == (*RED, 255)

    def test_flatten_composites_onto_background(self) -> None:
        with (
            GriptapeImage.open(_png(100, 100, RED)) as image,
            GriptapeImagePipeline(image).round_corners(0.2).flatten(WHITE).end() as flat,
        ):
            assert flat.channels == 3  # noqa: PLR2004
            pixels = decode(flat.encode(ImageFormat.PNG))

        assert tuple(pixels[0, 0]) == WHITE
        assert tuple(pixels[50, 50]) == RED

    def test_steps_keep_the_subclass(self) -> None:
        subclass = type("Subclass", (GriptapeImage,), {})

        with (
            subclass.open(_png(40, 20)) as image,
            GriptapeImagePipeline(image).thumbnail(10, 10).to_rgba().end() as result,
        ):
            result_type = type(result)

        assert result_type is subclass

    def test_leaves_the_source_unchanged(self) -> None:
        with GriptapeImage.open(_png(40, 20)) as image:
            with GriptapeImagePipeline(image).thumbnail(10, 10).end():
                pass
            assert (image.width, image.height) == (40, 20)

    def test_transformed_pixels_are_reencoded(self) -> None:
        with (
            GriptapeImage.open(_png(40, 20)) as image,
            GriptapeImagePipeline(image).thumbnail(10, 10).end() as small,
        ):
            encoded = small.encode(ImageFormat.PNG)

        assert size_of(encoded) == (10, 5)


class TestAlpha:
    @pytest.mark.parametrize("image_format", [ImageFormat.WEBP, ImageFormat.PNG, ImageFormat.TIFF])
    def test_grayscale_with_alpha_encodes(self, image_format: ImageFormat) -> None:
        with (
            GriptapeImage.open(_png(8, 8, (128, 200))) as image,
            GriptapeImagePipeline(image).thumbnail(4, 4).end() as small,
        ):
            encoded = small.encode(image_format)

        assert size_of(encoded) == (4, 4)

    def test_thumbnail_keeps_transparent_color_out_of_edges(self) -> None:
        pixels = solid(64, 64, (0, 0, 0, 0))
        pixels[:, 32:] = (255, 255, 255, 255)

        with (
            GriptapeImage.open(encode(pixels, "png")) as image,
            GriptapeImagePipeline(image).thumbnail(8, 8).end() as small,
        ):
            decoded = decode(small.encode(ImageFormat.PNG))

        for x in range(8):
            red, _, _, alpha = decoded[4, x]
            if alpha > 64:  # noqa: PLR2004
                assert red > 200  # noqa: PLR2004

    def test_partial_alpha_keeps_its_color(self) -> None:
        source = encode(solid(4, 4, (200, 100, 50, 7)), "png")

        with GriptapeImage.open(source) as image:
            encoded = image.encode(ImageFormat.PNG, metadata=ImageMetadata())

        assert tuple(decode(encoded)[0, 0]) == (200, 100, 50, 7)


class TestMetadata:
    def test_png_text_round_trips_latin1_unicode_and_long_text(self) -> None:
        long_text = '{"nodes": [' + ", ".join(['{"name": "node"}'] * 200) + "]}"
        texts = {"plain": "x", "unicode": "café ☕", "long": long_text}

        png = GriptapeImage.open(_png(4, 4)).encode(ImageFormat.PNG, metadata=ImageMetadata(text=texts))

        reread = GriptapeImage.open(png).metadata.text
        assert reread == texts
        assert read_spec(png).getattribute("plain") == "x"

    def test_malformed_exif_reads_as_none(self) -> None:
        source = replace_jpeg_exif(encode(solid(8, 4), "jpg"), b"not a TIFF block")

        with GriptapeImage.open(source) as image:
            exif = image.metadata.exif
            with GriptapeImagePipeline(image).exif_transpose().end() as upright:
                size = (upright.width, upright.height)

        assert exif is None
        assert size == (8, 4)

    def test_reads_parsed_exif(self) -> None:
        with GriptapeImage.open(_rotated_jpeg(8, 4)) as image:
            exif = image.metadata.exif

        assert exif is not None
        assert exif.orientation == _ROTATE_90_CLOCKWISE
        assert exif.swaps_dimensions

    def test_metadata_is_a_copy(self) -> None:
        with GriptapeImage.open(_with_exif(encode(solid(8, 8), "jpg"), _camera_exif())) as image:
            exif = image.metadata.exif
            assert exif is not None
            exif[_EXIF_MAKE_TAG] = "Changed"
            reread = image.metadata.exif

        assert reread is not None
        assert reread[_EXIF_MAKE_TAG] == "Camera Maker"

    def test_exif_driver_round_trips_user_comment_and_keeps_existing_tags(self) -> None:
        source = _with_exif(encode(solid(8, 8), "jpg"), _camera_exif())

        with GriptapeImage.open(source) as image:
            injected = ExifMetadataDriver().inject_metadata(image, {"gtn_key": "value"})

        with GriptapeImage.open(injected) as image:
            metadata = ExifMetadataDriver().extract_metadata(image)

        block = read_exif(injected, ImageFormat.JPEG)
        assert block is not None
        assert metadata["gtn_key"] == "value"
        assert metadata["Make"] == "Camera Maker"
        assert EXIF_USERCOMMENT_TAG in Exif.from_bytes(block)
        assert read_spec(injected).getattribute("Make") == "Camera Maker"

    @pytest.mark.parametrize("color", [RED, (*RED, 128)])
    def test_exif_round_trips_through_webp(self, color: tuple[int, ...]) -> None:
        source = _webp(solid(8, 8, color), ImageMetadata(exif=_camera_exif()))

        with GriptapeImage.open(source) as image:
            webp = image.encode(ImageFormat.WEBP, metadata=image.metadata)

        spec = read_spec(webp)
        assert spec.getattribute("Make") == "Camera Maker"
        assert spec.nchannels == len(color)

    def test_encode_writes_the_images_own_metadata_by_default(self) -> None:
        source = _with_exif(encode(solid(8, 8), "jpg"), _camera_exif())

        with GriptapeImage.open(source) as image:
            same = image.encode(ImageFormat.JPEG)
            png = image.encode(ImageFormat.PNG)

        assert same == source
        assert read_spec(png).getattribute("Make") == "Camera Maker"

    def test_empty_metadata_writes_none(self) -> None:
        source = _with_exif(encode(solid(8, 8), "jpg", icc_profile=SRGB_PROFILE), _camera_exif())

        with GriptapeImage.open(source) as image:
            stripped = image.encode(ImageFormat.JPEG, metadata=ImageMetadata())

        assert read_exif(stripped, ImageFormat.JPEG) is None
        assert icc_profile_of(stripped) is None

    def test_written_exif_is_edited_through_exif(self) -> None:
        with GriptapeImage.open(_png(8, 8)) as image:
            jpeg = image.encode(ImageFormat.JPEG, metadata=ImageMetadata(exif=_camera_exif()))

        assert read_spec(jpeg).getattribute("Make") == "Camera Maker"

    def test_rewrites_png_metadata_without_decoding(self) -> None:
        png = insert_text_chunks(_png(8, 8), {"kept": "old", "replaced": "old"})

        image = GriptapeImage.open(png)
        rewritten = image.encode(ImageFormat.PNG, metadata=ImageMetadata(text={"replaced": "new"}))

        assert image._buf is None
        assert read_text_chunks(rewritten) == {"replaced": "new"}
        assert rewritten.count(b"tEXt") == 1

    def test_rewrites_jpeg_exif_without_decoding(self) -> None:
        jpeg = encode(solid(8, 8, RED), "jpg")
        exif = _camera_exif()

        image = GriptapeImage.open(jpeg)
        rewritten = image.encode(ImageFormat.JPEG, metadata=ImageMetadata(exif=exif))

        assert image._buf is None
        assert read_spec(rewritten).getattribute("Make") == "Camera Maker"
        assert rewritten == replace_jpeg_exif(jpeg, exif.to_bytes())

    def test_quality_reencodes_the_source(self) -> None:
        jpeg = encode(noise(64, 64), "jpg", compression="jpeg:95")

        with GriptapeImage.open(jpeg) as image:
            encoded = image.encode(ImageFormat.JPEG, quality=20)

        assert len(encoded) < len(jpeg)

    def test_oversized_compressed_text_is_skipped(self) -> None:
        bomb = zlib.compress(b"a" * (MAX_COMPRESSED_TEXT_BYTES + 1))
        chunk_data = b"bomb\x00\x00" + bomb
        chunk = struct.pack(">I", len(chunk_data)) + b"zTXt" + chunk_data
        chunk += struct.pack(">I", zlib.crc32(b"zTXt" + chunk_data))
        png = _png(2, 2)
        idat = png.index(b"IDAT") - 4

        assert read_text_chunks(png[:idat] + chunk + png[idat:]) == {}


class TestSplicingOwnMetadata:
    @pytest.mark.parametrize("extension", ["jpg", "png"])
    def test_image_without_metadata_saves_as_its_source(self, extension: str) -> None:
        source = encode(noise(8, 8), extension)

        with GriptapeImage.open(source) as image:
            encoded = image.encode(image.format or ImageFormat.PNG)
            assert image._buf is None

        assert encoded == source

    def test_keeps_the_xmp_of_a_jpeg_with_no_other_metadata(self) -> None:
        source = _with_xmp(encode(noise(8, 8), "jpg"))

        with GriptapeImage.open(source) as image:
            encoded = image.encode(ImageFormat.JPEG)

        assert encoded == source

    def test_explicit_empty_metadata_writes_none(self) -> None:
        source = _with_xmp(encode(noise(8, 8), "jpg"))

        with GriptapeImage.open(source) as image:
            encoded = image.encode(ImageFormat.JPEG, metadata=ImageMetadata())

        assert read_extra_metadata_kinds(encoded, ImageFormat.JPEG) == []

    def test_exif_driver_keeps_the_xmp_of_a_jpeg(self) -> None:
        source = _with_xmp(encode(noise(8, 8), "jpg"))

        with GriptapeImage.open(source) as image:
            injected = ExifMetadataDriver().inject_metadata(image, {"gtn_key": "value"})
            assert image._buf is None

        assert read_extra_metadata_kinds(injected, ImageFormat.JPEG) == ["XMP"]
        block = read_exif(injected, ImageFormat.JPEG)
        assert block is not None
        assert EXIF_USERCOMMENT_TAG in Exif.from_bytes(block)


class TestPipelineMetadata:
    def test_output_carries_the_source_metadata(self) -> None:
        png = encode(solid(40, 20), "png", icc_profile=SRGB_PROFILE)
        png = insert_text_chunks(png, {"workflow": "{}"})

        with GriptapeImage.open(png) as image, GriptapeImagePipeline(image).thumbnail(10, 10).end() as small:
            metadata = small.metadata
            encoded = small.encode(ImageFormat.PNG)

        assert metadata.text == {"workflow": "{}"}
        assert metadata.icc_profile == SRGB_PROFILE
        assert read_text_chunks(encoded) == {"workflow": "{}"}
        assert icc_profile_of(encoded) == SRGB_PROFILE

    def test_orienting_resets_orientation_and_updates_dimensions(self) -> None:
        exif = _camera_exif()
        exif[EXIF_PIXEL_X_DIMENSION_TAG] = 400
        exif[EXIF_PIXEL_Y_DIMENSION_TAG] = 200

        with (
            GriptapeImage.open(_rotated_jpeg(400, 200, exif)) as image,
            GriptapeImagePipeline(image).exif_transpose().thumbnail(100, 50).end() as preview,
        ):
            written = preview.metadata.exif
            jpeg = preview.encode(ImageFormat.JPEG)

        assert written is not None
        assert written.orientation == 1
        assert written[_EXIF_MAKE_TAG] == "Camera Maker"
        assert (written[EXIF_PIXEL_X_DIMENSION_TAG], written[EXIF_PIXEL_Y_DIMENSION_TAG]) == (25, 50)
        # A reader applying the orientation again would turn the upright pixels sideways
        spec = read_spec(jpeg)
        assert spec.get_int_attribute("Orientation", 0) == 1
        assert (spec.width, spec.height) == (25, 50)

    def test_steps_without_orienting_keep_the_orientation(self) -> None:
        with (
            GriptapeImage.open(_rotated_jpeg(40, 20)) as image,
            GriptapeImagePipeline(image).thumbnail(10, 10).end() as small,
        ):
            exif = small.metadata.exif

        assert exif is not None
        assert exif.orientation == _ROTATE_90_CLOCKWISE

    def test_changing_the_output_metadata_leaves_the_source_alone(self) -> None:
        source = _with_exif(encode(solid(8, 8), "jpg"), _camera_exif())

        with GriptapeImage.open(source) as image, GriptapeImagePipeline(image).to_rgba().end() as rgba:
            exif = rgba.metadata.exif
            assert exif is not None
            exif[_EXIF_MAKE_TAG] = "Changed"
            original = image.metadata.exif

        assert original is not None
        assert original[_EXIF_MAKE_TAG] == "Camera Maker"


class TestDroppedMetadataWarning:
    def test_warns_once_naming_the_format_and_what_was_dropped(self, caplog: pytest.LogCaptureFixture) -> None:
        metadata = ImageMetadata(text={"workflow": "{}"}, exif=_camera_exif(), icc_profile=SRGB_PROFILE)

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            _from_pixels(solid(4, 4), metadata).encode(ImageFormat.BMP)

        assert [record.getMessage() for record in caplog.records] == [
            "Saved an image as BMP without its text metadata, EXIF, and color profile."
        ]

    def test_warns_about_text_a_jpeg_cannot_hold(self, caplog: pytest.LogCaptureFixture) -> None:
        png = insert_text_chunks(_png(4, 4), {"workflow": "{}"})

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"), GriptapeImage.open(png) as image:
            image.encode(ImageFormat.JPEG)

        assert [record.getMessage() for record in caplog.records] == [
            "Saved an image as JPEG without its text metadata."
        ]

    def test_warns_about_xmp_a_reencode_loses(self, caplog: pytest.LogCaptureFixture) -> None:
        jpeg = _with_xmp(_with_exif(encode(solid(4, 4), "jpg"), _camera_exif()))

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"), GriptapeImage.open(jpeg) as image:
            spliced = image.encode(ImageFormat.JPEG)
            png = image.encode(ImageFormat.PNG)

        assert spliced == jpeg
        assert read_spec(png).getattribute("Make") == "Camera Maker"
        assert [record.getMessage() for record in caplog.records] == ["Saved an image as PNG without its XMP."]

    def test_warns_about_text_spliced_into_a_jpeg(self, caplog: pytest.LogCaptureFixture) -> None:
        jpeg = encode(solid(4, 4), "jpg")

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"), GriptapeImage.open(jpeg) as image:
            encoded = image.encode(
                ImageFormat.JPEG, metadata=ImageMetadata(text={"workflow": "{}"}, exif=_camera_exif())
            )
            # Spliced: the pixels were never decoded
            assert image._buf is None

        assert read_spec(encoded).getattribute("Make") == "Camera Maker"
        assert [record.getMessage() for record in caplog.records] == [
            "Saved an image as JPEG without its text metadata."
        ]

    @pytest.mark.parametrize("image_format", [ImageFormat.PNG, ImageFormat.JPEG, ImageFormat.WEBP, ImageFormat.TIFF])
    def test_silent_when_everything_fits(self, caplog: pytest.LogCaptureFixture, image_format: ImageFormat) -> None:
        text = {}
        if image_format == ImageFormat.PNG:
            text = {"workflow": "{}"}
        metadata = ImageMetadata(text=text, exif=_camera_exif(), icc_profile=SRGB_PROFILE)

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            _from_pixels(solid(4, 4), metadata).encode(image_format)

        assert caplog.records == []

    def test_drops_exif_too_large_for_a_jpeg(self, caplog: pytest.LogCaptureFixture) -> None:
        exif = _camera_exif()
        exif[_EXIF_MAKER_NOTE_TAG] = bytes(_OVERSIZE_BYTES)

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            jpeg = _from_pixels(solid(4, 4), ImageMetadata(exif=exif)).encode(ImageFormat.JPEG)

        assert read_exif(jpeg, ImageFormat.JPEG) is None
        assert [record.getMessage() for record in caplog.records] == ["Saved an image as JPEG without its EXIF."]

    def test_drops_exif_too_large_to_splice_into_a_jpeg(self, caplog: pytest.LogCaptureFixture) -> None:
        exif = _camera_exif()
        exif[_EXIF_MAKER_NOTE_TAG] = bytes(_OVERSIZE_BYTES)
        source = _with_exif(encode(solid(4, 4), "jpg"), _camera_exif())

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"), GriptapeImage.open(source) as image:
            jpeg = image.encode(ImageFormat.JPEG, metadata=ImageMetadata(exif=exif))
            assert image._buf is None

        assert read_exif(jpeg, ImageFormat.JPEG) is None
        assert [record.getMessage() for record in caplog.records] == ["Saved an image as JPEG without its EXIF."]

    def test_pipeline_output_warns_about_xmp_it_lost(self, caplog: pytest.LogCaptureFixture) -> None:
        jpeg = _with_xmp(_with_exif(encode(solid(8, 8), "jpg"), _camera_exif()))

        with (
            caplog.at_level(logging.WARNING, logger="griptape_nodes"),
            GriptapeImage.open(jpeg) as image,
            GriptapeImagePipeline(image).thumbnail(4, 4).end() as small,
        ):
            small.encode(ImageFormat.JPEG)

        assert [record.getMessage() for record in caplog.records] == ["Saved an image as JPEG without its XMP."]

    def test_previews_stay_quiet_about_metadata_left_out(
        self, caplog: pytest.LogCaptureFixture, tmp_path: Path
    ) -> None:
        source = _with_xmp(_with_exif(encode(solid(64, 32), "jpg", icc_profile=SRGB_PROFILE), _camera_exif()))
        thumbnail_generator = ThumbnailGenerator(
            source_file_location=str(tmp_path / "source.jpg"),
            preview_format="webp",
            destination_preview_directory=str(tmp_path),
            destination_preview_file_name="thumbnail.webp",
            params={"max_width": 16, "max_height": 16},
        )
        rounded_generator = RoundedPreviewGenerator(
            source_file_location=str(tmp_path / "source.jpg"),
            preview_format="png",
            destination_preview_directory=str(tmp_path),
            destination_preview_file_name="rounded.png",
            params={"max_width": 16, "max_height": 16, "corner_radius_percent": 10.0},
        )

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            thumbnail_generator._render_thumbnail(source)
            rounded_generator._render_rounded_preview(source)
            data_url = create_image_preview_from_bytes(source, max_width=16, max_height=16)

        assert data_url is not None
        assert caplog.records == []

    def test_silent_about_the_xmp_stamp_in_a_tiff_it_wrote(self, caplog: pytest.LogCaptureFixture) -> None:
        with GriptapeImage.open(_png(4, 4)) as image:
            tiff = image.encode(ImageFormat.TIFF, metadata=ImageMetadata(exif=_camera_exif()))

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"), GriptapeImage.open(tiff) as image:
            image.encode(ImageFormat.TIFF)
            image.encode(ImageFormat.JPEG)

        assert caplog.records == []

    def test_silent_when_writing_no_metadata(self, caplog: pytest.LogCaptureFixture) -> None:
        jpeg = _with_xmp(_with_exif(encode(solid(4, 4), "jpg"), _camera_exif()))

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"), GriptapeImage.open(jpeg) as image:
            image.encode(ImageFormat.BMP, metadata=ImageMetadata())

        assert caplog.records == []


class TestColorProfile:
    @pytest.mark.parametrize("source_extension", ["jpg", "png", "webp", "tif"])
    @pytest.mark.parametrize("image_format", [ImageFormat.PNG, ImageFormat.TIFF, ImageFormat.JPEG, ImageFormat.WEBP])
    def test_pipeline_output_keeps_the_profile(self, source_extension: str, image_format: ImageFormat) -> None:
        source = encode(solid(16, 8, (10, 200, 30)), source_extension, icc_profile=SRGB_PROFILE)

        with (
            GriptapeImage.open(source) as image,
            GriptapeImagePipeline(image).exif_transpose().thumbnail(8, 8).round_corners(0.25).end() as small,
        ):
            encoded = small.encode(image_format)

        assert icc_profile_of(encoded) == SRGB_PROFILE

    @pytest.mark.parametrize("image_format", [ImageFormat.PNG, ImageFormat.TIFF])
    def test_reencoding_the_source_keeps_the_profile(self, image_format: ImageFormat) -> None:
        source = encode(solid(8, 8), "tif", icc_profile=SRGB_PROFILE)

        with GriptapeImage.open(source) as image:
            encoded = image.encode(
                image_format, optimize=True, metadata=ImageMetadata(exif=_camera_exif(), icc_profile=SRGB_PROFILE)
            )

        assert icc_profile_of(encoded) == SRGB_PROFILE

    def test_flatten_drops_the_profile(self) -> None:
        source = encode(solid(8, 8, (10, 200, 30, 128)), "png", icc_profile=SRGB_PROFILE)

        with GriptapeImage.open(source) as image, GriptapeImagePipeline(image).flatten().end() as flat:
            encoded = flat.encode(ImageFormat.PNG)

        assert icc_profile_of(encoded) is None

    @pytest.mark.parametrize("image_format", [ImageFormat.PNG, ImageFormat.JPEG, ImageFormat.WEBP])
    def test_drops_a_profile_for_another_color_space(
        self, caplog: pytest.LogCaptureFixture, image_format: ImageFormat
    ) -> None:
        # A CMYK JPEG's profile describes its stored ink values, not the RGB they decode to
        cmyk_profile = SRGB_PROFILE[:16] + b"CMYK" + SRGB_PROFILE[20:]
        source = encode(solid(8, 8), "jpg", icc_profile=cmyk_profile)

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"), GriptapeImage.open(source) as image:
            assert image.metadata.icc_profile == cmyk_profile
            encoded = image.encode(image_format, quality=90)
            with GriptapeImagePipeline(image).thumbnail(4, 4).end() as small:
                assert small.metadata.icc_profile is None

        assert icc_profile_of(encoded) is None
        assert [record.getMessage() for record in caplog.records] == [
            f"Saved an image as {image_format} without its color profile."
        ]

    @pytest.mark.parametrize(("color", "keeps"), [(128, True), (RED, False)])
    def test_gray_profile_describes_only_gray(self, color: tuple[int, ...] | int, *, keeps: bool) -> None:
        gray_profile = SRGB_PROFILE[:16] + b"GRAY" + SRGB_PROFILE[20:]

        with _from_pixels(solid(4, 4, color), ImageMetadata(icc_profile=gray_profile)) as image:
            encoded = image.encode(ImageFormat.JPEG)

        assert (icc_profile_of(encoded) == gray_profile) is keeps

    def test_oversized_compressed_profile_reads_as_none(self) -> None:
        bomb = zlib.compress(bytes(MAX_ICC_PROFILE_BYTES + 1))
        png = _png(4, 4)
        idat = png.index(b"IDAT") - 4
        png = png[:idat] + png_chunk(b"iCCP", b"bomb\x00\x00" + bomb) + png[idat:]

        assert read_icc_profile(png, ImageFormat.PNG) is None
        with GriptapeImage.open(png) as image:
            assert image.metadata.icc_profile is None

    def test_compressed_profile_reads_back(self) -> None:
        png = encode(solid(4, 4), "png", icc_profile=SRGB_PROFILE)

        assert read_icc_profile(png, ImageFormat.PNG) == SRGB_PROFILE


class TestTiffExif:
    def test_writes_exif_gps_and_exif_ifd_tags(self) -> None:
        exif = _camera_exif()
        exif[_EXIF_DATETIME_ORIGINAL_TAG] = "2024:01:02 03:04:05"
        with GriptapeImage.open(_png(8, 4)) as image:
            tiff = image.encode(ImageFormat.TIFF, metadata=ImageMetadata(exif=_with_gps(exif)))

        spec = read_spec(tiff)
        reread = tiff_metadata(tiff).exif
        assert (spec.width, spec.height) == (8, 4)
        assert spec.getattribute("Make") == "Camera Maker"
        assert spec.getattribute("Exif:DateTimeOriginal") == "2024:01:02 03:04:05"
        assert reread[_EXIF_DATETIME_ORIGINAL_TAG] == "2024:01:02 03:04:05"
        assert reread.gps == {_GPS_LATITUDE_REF_TAG: "N"}

    def test_round_trips_through_a_tiff_source(self) -> None:
        exif = _camera_exif()
        exif[0x011A] = ExifRational(300, 1)
        with GriptapeImage.open(_png(8, 4)) as image:
            tiff = image.encode(ImageFormat.TIFF, metadata=ImageMetadata(exif=exif))

        with GriptapeImage.open(tiff) as image:
            reread = image.metadata.exif
            jpeg = image.encode(ImageFormat.JPEG)

        assert reread is not None
        assert reread[_EXIF_MAKE_TAG] == "Camera Maker"
        assert reread[0x011A] == ExifRational(300, 1)
        assert read_spec(jpeg).getattribute("Make") == "Camera Maker"

    def test_reads_tiff_metadata_in_one_parse(self, mocker: MockerFixture) -> None:
        source = encode(solid(8, 4), "tif", icc_profile=SRGB_PROFILE, Make="Camera Maker")
        parse = mocker.spy(Exif, "from_bytes")

        with GriptapeImage.open(source) as image:
            metadata = image.metadata

        assert metadata.icc_profile == SRGB_PROFILE
        assert metadata.exif is not None
        assert metadata.exif[_EXIF_MAKE_TAG] == "Camera Maker"
        assert parse.call_count == 1

    def test_tiff_xmp_stays_out_of_jpeg_exif(self, caplog: pytest.LogCaptureFixture) -> None:
        source = gray_tiff(
            {
                _EXIF_MAKE_TAG: (_TIFF_ASCII, b"Camera Maker\x00"),
                _TIFF_XMP_TAG: (_TIFF_BYTE, b"<x:xmpmeta/>".ljust(_OVERSIZE_BYTES)),
            }
        )

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"), GriptapeImage.open(source) as image:
            jpeg = image.encode(ImageFormat.JPEG)

        assert read_spec(jpeg).getattribute("Make") == "Camera Maker"
        assert [record.getMessage() for record in caplog.records] == ["Saved an image as JPEG without its XMP."]

    def test_reads_tiff_orientation(self) -> None:
        source = encode(solid(8, 4), "tif", Orientation=_ROTATE_90_CLOCKWISE)

        with (
            GriptapeImage.open(source) as image,
            GriptapeImagePipeline(image).exif_transpose().end() as upright,
        ):
            assert (upright.width, upright.height) == (4, 8)
