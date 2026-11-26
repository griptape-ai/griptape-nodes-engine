"""Pillow thumbnails retain the standard size, orientation and text policies."""

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin, UnidentifiedImageError
from pytest_mock import MockerFixture

from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.pil_thumbnail_generator import (
    PILThumbnailGenerator,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.thumbnail_parameters import (
    ThumbnailParameters,
)

LONG_TEXT = "source workflow " * 14000
PIXEL_BUDGET = 10000000


def _source_bytes(image: Image.Image, **kwargs: object) -> bytes:
    with BytesIO() as buffer:
        image.save(buffer, format="PNG", **kwargs)
        return buffer.getvalue()


def _generator(engine: Engine, *, preview_format: str = "png", max_width: int = 1024) -> PILThumbnailGenerator:
    return PILThumbnailGenerator(
        "source.png", preview_format, "unused", f"preview.{preview_format}", {"max_width": max_width}, engine=engine
    )


def test_standard_generator_contract(engine: Engine) -> None:
    """The fallback uses the same public name, parameters and nine original formats."""
    generator = _generator(engine)

    assert generator.get_friendly_name() == "Standard Thumbnail Generation"
    assert generator.get_parameters() is ThumbnailParameters
    assert generator.params.max_width == 1024  # noqa: PLR2004 - Public default.
    assert generator.params.max_height == 1024  # noqa: PLR2004 - Public default.
    assert generator.get_supported_source_formats() == {
        "png",
        "jpg",
        "jpeg",
        "gif",
        "bmp",
        "webp",
        "tiff",
        "tif",
        "tga",
    }
    assert generator.get_supported_preview_formats() == {"png", "jpg", "webp"}


@pytest.mark.parametrize("preview_format", ["png", "jpg", "webp"])
@pytest.mark.parametrize(("source_size", "expected_size"), [((1600, 800), (1024, 512)), ((8, 4), (8, 4))])
def test_thumbnail_fits_defaults_without_upscaling(
    engine: Engine, tmp_path: Path, preview_format: str, source_size: tuple[int, int], expected_size: tuple[int, int]
) -> None:
    """All outputs preserve aspect ratio and leave small inputs at their original size."""
    generator = _generator(engine, preview_format=preview_format)
    with Image.new("RGB", source_size, "red") as image:
        source = _source_bytes(image)

    encoded = generator._encode_thumbnail(source, tmp_path, preserve_text=False, max_source_pixels=PIXEL_BUDGET)

    with BytesIO(encoded) as buffer, Image.open(buffer) as thumbnail:
        assert thumbnail.size == expected_size
        assert thumbnail.format == {"png": "PNG", "jpg": "JPEG", "webp": "WEBP"}[preview_format]
    assert list(tmp_path.iterdir()) == []


def test_orientation_is_applied_before_resizing(engine: Engine, tmp_path: Path) -> None:
    """A rotated phone image fits the limits using its displayed dimensions."""
    generator = PILThumbnailGenerator(
        "phone.png", "png", "unused", "preview.png", {"max_width": 6, "max_height": 4}, engine=engine
    )
    exif = Image.Exif()
    exif[274] = 6
    with Image.new("RGB", (8, 4), "red") as image:
        image.putpixel((0, 0), (0, 255, 0))
        source = _source_bytes(image, exif=exif)

    encoded = generator._encode_thumbnail(source, tmp_path, preserve_text=False, max_source_pixels=PIXEL_BUDGET)

    with BytesIO(encoded) as buffer, Image.open(buffer) as thumbnail:
        assert thumbnail.size == (2, 4)
        assert thumbnail.getexif().get(274) is None


@pytest.mark.parametrize("preserve", [False, True])
def test_png_text_is_opt_in_and_retains_compressed_unicode(engine: Engine, tmp_path: Path, *, preserve: bool) -> None:
    """Source provenance and Unicode text survive only when requested, without bloating output."""
    text = PngImagePlugin.PngInfo()
    text.add_text("gtn_workflow", LONG_TEXT, zip=True)
    text.add_itxt("artist_note", "\u00e9 \u732b", zip=True)
    text.add(b"tEXt", b"after_pixels\x00trailing text", after_idat=True)
    with Image.new("RGB", (40, 30), "red") as image:
        source = _source_bytes(image, pnginfo=text)
    generator = _generator(engine, max_width=20)

    encoded = generator._encode_thumbnail(source, tmp_path, preserve_text=preserve, max_source_pixels=PIXEL_BUDGET)

    with BytesIO(encoded) as buffer, Image.open(buffer) as thumbnail:
        assert isinstance(thumbnail, PngImagePlugin.PngImageFile)
        thumbnail.load()
        expected: dict[str, str] = {}
        if preserve:
            expected = {"gtn_workflow": LONG_TEXT, "artist_note": "\u00e9 \u732b", "after_pixels": "trailing text"}
        assert thumbnail.text == expected
        assert thumbnail.size == (20, 15)
    assert len(encoded) < 10000  # noqa: PLR2004 - Long text must stay compressed.


def test_pixel_budget_rejects_before_load(engine: Engine, tmp_path: Path, mocker: MockerFixture) -> None:
    """The header budget prevents even the first pixel decode."""
    with Image.new("RGB", (8, 4), "red") as image:
        source = _source_bytes(image)
    load = mocker.spy(PngImagePlugin.PngImageFile, "load")
    generator = _generator(engine)

    with pytest.raises(OSError, match="max_thumbnail_source_pixels=31") as failure:
        generator._encode_thumbnail(source, tmp_path, preserve_text=True, max_source_pixels=31)

    load.assert_not_called()
    assert "source.png" in str(failure.value)
    assert "8 x 4" in str(failure.value)
    assert "32 pixels" in str(failure.value)
    assert list(tmp_path.iterdir()) == []


def test_pillow_ceiling_explains_backend_limit(engine: Engine, tmp_path: Path, mocker: MockerFixture) -> None:
    """Raising the configured budget cannot bypass Pillow's independent refusal."""
    with Image.new("RGB", (8, 4), "red") as image:
        source = _source_bytes(image)
    mocker.patch.object(Image, "MAX_IMAGE_PIXELS", 15)
    load = mocker.spy(PngImagePlugin.PngImageFile, "load")
    generator = _generator(engine)

    with pytest.raises(OSError, match="Pillow") as failure:
        generator._encode_thumbnail(source, tmp_path, preserve_text=False, max_source_pixels=100)

    message = str(failure.value)
    assert "source.png" in message
    assert "max_thumbnail_source_pixels=100" in message
    assert "30" in message
    assert "OpenImageIO" in message
    assert "restart" in message
    assert isinstance(failure.value.__cause__, Image.DecompressionBombError)
    load.assert_not_called()
    assert Image.MAX_IMAGE_PIXELS == 15  # noqa: PLR2004 - Test-local threshold remains unchanged.
    assert list(tmp_path.iterdir()) == []


def test_pillow_load_ceiling_closes_image(engine: Engine, tmp_path: Path, mocker: MockerFixture) -> None:
    """TIFF can reject tile dimensions during load after its header was accepted."""
    with BytesIO() as buffer:
        with Image.new("RGB", (8, 4), "red") as image:
            image.save(buffer, format="TIFF")
        source = buffer.getvalue()
    raw_image = Image.open(BytesIO(source))
    mocker.patch.object(Image, "open", return_value=raw_image)
    mocker.patch.object(raw_image, "load", side_effect=Image.DecompressionBombError("oversized TIFF tile"))
    closed = mocker.spy(raw_image, "close")
    original_ceiling = Image.MAX_IMAGE_PIXELS
    generator = _generator(engine)

    with pytest.raises(OSError, match="Pillow") as failure:
        generator._encode_thumbnail(source, tmp_path, preserve_text=False, max_source_pixels=100)

    assert isinstance(failure.value.__cause__, Image.DecompressionBombError)
    closed.assert_called_once()
    assert original_ceiling == Image.MAX_IMAGE_PIXELS


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_pillow_ceiling_does_not_publish(
    engine: Engine, tmp_path: Path, mocker: MockerFixture, *, existing: bool
) -> None:
    """Pillow's early refusal leaves the previous destination untouched."""
    source_path = tmp_path / "source.png"
    with Image.new("RGB", (8, 4), "red") as image:
        source_path.write_bytes(_source_bytes(image))
    destination = tmp_path / "preview.png"
    if existing:
        destination.write_bytes(b"previous preview")
    mocker.patch.object(Image, "MAX_IMAGE_PIXELS", 15)
    engine.config_manager.set_config_value("max_thumbnail_source_pixels", 100)
    generator = PILThumbnailGenerator(str(source_path), "png", str(tmp_path), destination.name, {}, engine=engine)

    with pytest.raises(OSError, match="Pillow"):
        await generator.attempt_generate_preview()

    if existing:
        surviving = destination.read_bytes()
        assert surviving == b"previous preview"
    else:
        assert not destination.exists()
    assert Image.MAX_IMAGE_PIXELS == 15  # noqa: PLR2004 - Encoding must leave the global unchanged.


@pytest.mark.parametrize("limit", [32, 33])
def test_pixel_budget_allows_boundary(engine: Engine, tmp_path: Path, limit: int) -> None:
    """A source at the configured budget can be decoded."""
    with Image.new("RGB", (8, 4), "red") as image:
        source = _source_bytes(image)
    generator = _generator(engine)

    encoded = generator._encode_thumbnail(source, tmp_path, preserve_text=False, max_source_pixels=limit)

    with BytesIO(encoded) as buffer, Image.open(buffer) as thumbnail:
        assert thumbnail.size == (8, 4)


@pytest.mark.parametrize("preview_format", ["png", "webp", "jpg"])
@pytest.mark.parametrize(("mode", "colour"), [("RGBA", (240, 40, 20, 128)), ("LA", (180, 128))])
def test_half_opaque_colour_is_not_premultiplied(
    engine: Engine, tmp_path: Path, preview_format: str, mode: str, colour: tuple[int, ...]
) -> None:
    """PNG and WebP retain straight alpha; JPEG drops alpha without darkening the colour."""
    with Image.new(mode, (8, 4), colour) as image:
        source = _source_bytes(image)
    generator = _generator(engine, preview_format=preview_format)

    encoded = generator._encode_thumbnail(source, tmp_path, preserve_text=False, max_source_pixels=PIXEL_BUDGET)

    with BytesIO(encoded) as buffer, Image.open(buffer) as thumbnail, thumbnail.convert("RGBA") as rgba:
        pixel = rgba.getpixel((0, 0))
        expected_rgb = colour[:3]
        if mode == "LA":
            expected_rgb = (colour[0],) * 3
        assert isinstance(pixel, tuple)
        assert pixel[:3] == pytest.approx(expected_rgb, abs=4)
        if preview_format == "jpg":
            assert thumbnail.mode == "RGB"
            assert pixel[3] == 255  # noqa: PLR2004 - Opaque JPEG.
        else:
            assert pixel[3] == 128  # noqa: PLR2004 - Source alpha.


def test_grayscale_webp_is_supported(engine: Engine, tmp_path: Path) -> None:
    """WebP receives RGB pixels when the source has only a luminance channel."""
    with Image.new("L", (8, 4), 180) as image:
        source = _source_bytes(image)
    generator = _generator(engine, preview_format="webp")

    encoded = generator._encode_thumbnail(source, tmp_path, preserve_text=False, max_source_pixels=PIXEL_BUDGET)

    with BytesIO(encoded) as buffer, Image.open(buffer) as thumbnail:
        assert thumbnail.mode == "RGB"
        assert thumbnail.getpixel((0, 0)) == pytest.approx((180, 180, 180), abs=4)


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_failed_encode_does_not_publish(
    engine: Engine, tmp_path: Path, mocker: MockerFixture, *, existing: bool
) -> None:
    """Shared orchestration never creates or overwrites the destination after an encoder failure."""
    source_path = tmp_path / "source.png"
    with Image.new("RGB", (8, 4), "red") as image:
        source_path.write_bytes(_source_bytes(image))
    destination = tmp_path / "preview.png"
    if existing:
        destination.write_bytes(b"previous preview")
    generator = PILThumbnailGenerator(str(source_path), "png", str(tmp_path), destination.name, {}, engine=engine)
    mocker.patch.object(Image.Image, "save", side_effect=OSError("encoder failed"))

    with pytest.raises(OSError, match="encoder failed"):
        await generator.attempt_generate_preview()

    if existing:
        surviving = destination.read_bytes()
        assert surviving == b"previous preview"
    else:
        assert not destination.exists()


def test_invalid_image_leaves_no_files(engine: Engine, tmp_path: Path) -> None:
    """An invalid source cannot create a destination or a scratch file."""
    generator = _generator(engine)

    with pytest.raises(UnidentifiedImageError):
        generator._encode_thumbnail(b"not an image", tmp_path, preserve_text=False, max_source_pixels=PIXEL_BUDGET)

    assert list(tmp_path.iterdir()) == []
