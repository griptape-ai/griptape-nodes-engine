"""Engine previews straighten associated alpha independently of resizing."""

from pathlib import Path

import anyio
import pytest

try:
    import OpenImageIO as oiio
except ModuleNotFoundError as error:
    if error.name != "OpenImageIO":
        raise
    pytest.skip("OpenImageIO renderer tests require the optional dependency", allow_module_level=True)
from pytest_mock import MockerFixture

from griptape_nodes.common.macro_parser import ParsedMacro
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.artifact_events import (
    GeneratePreviewRequest,
    GeneratePreviewResultSuccess,
    RegisterArtifactProviderRequest,
    RegisterArtifactProviderResultSuccess,
)
from griptape_nodes.retained_mode.events.project_events import MacroPath
from griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider import ImageArtifactProvider
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.openimageio_thumbnail_generator import (
    OpenImageIOThumbnailGenerator as ThumbnailGenerator,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", ["uint8", "uint16"])
@pytest.mark.parametrize("channels", ["RGBA", "YA"])
@pytest.mark.parametrize("output_format", ["png", "jpg", "webp"])
@pytest.mark.parametrize("size", [20, 64], ids=["resized", "unresized"])
async def test_associated_tiff_preview_has_straight_colour(  # noqa: PLR0913, PLR0917 - Independent input and output axes.
    engine: Engine, tmp_path: Path, depth: str, channels: str, output_format: str, size: int
) -> None:
    """Associated TIFF has the same straight brightness on both processing routes."""
    # Given a non-primary colour encoded with associated alpha.
    datatype = {"uint8": oiio.UINT8, "uint16": oiio.UINT16}[depth]
    spec = oiio.ImageSpec(40, 40, len(channels), datatype)
    spec.channelnames = list(channels)
    spec.alpha_channel = len(channels) - 1
    spec.attribute("oiio:ColorSpace", "sRGB")
    image = oiio.ImageBuf(spec)
    colour = (0.25, 0.125, 0.0625, 0.5)
    if channels == "YA":
        colour = (0.25, 0.5)
    filled = oiio.ImageBufAlgo.fill(image, colour)
    assert filled
    source = tmp_path / "associated.tiff"
    written = image.write(str(source), datatype, "tiff")
    assert written
    registration = engine.handle_request(RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider))
    assert isinstance(registration, RegisterArtifactProviderResultSuccess)
    engine.config_manager.set_config_value("auto_inject_workflow_metadata", False)

    # When a real preview request runs with or without a reduction.
    result = await engine.ahandle_request(
        GeneratePreviewRequest(
            macro_path=MacroPath(ParsedMacro(str(source)), {}),
            artifact_provider_name="Image",
            format=output_format,
            preview_generator_parameters={"max_width": size, "max_height": size},
        )
    )

    # Then alpha is straightened before being retained or dropped.
    assert isinstance(result, GeneratePreviewResultSuccess)
    assert isinstance(result.paths_to_preview, str)
    config = oiio.ImageSpec()
    config.attribute("oiio:UnassociatedAlpha", 1)
    preview = oiio.ImageBuf(result.paths_to_preview, 0, 0, config)
    read = preview.read(force=True)
    assert read
    pixel = preview.getpixel(0, 0)
    expected = [0.5, 0.25, 0.125]
    if channels == "YA":
        expected = [0.5, 0.5, 0.5]
    if output_format == "png" and channels == "YA":
        expected = [0.5]
    if output_format != "jpg":
        expected.append(0.5)
    # One level for integer association/encoding; three levels for lossy output.
    tolerance = 1 / 255 + 1e-7
    if output_format != "png":
        tolerance = 3 / 255 + 1e-7
    assert pixel == pytest.approx(expected, abs=tolerance)
    assert preview.spec().width == min(size, 40)
    if output_format == "png" and depth == "uint16":
        assert preview.nativespec().format == oiio.UINT16


@pytest.mark.asyncio
@pytest.mark.parametrize("channels", ["RGBA", "YA"])
@pytest.mark.parametrize("depth", ["uint8", "uint16"])
@pytest.mark.parametrize("alpha", [3, 128], ids=["low-alpha", "half-alpha"])
async def test_resized_png_retains_straight_colour_at_low_alpha(
    engine: Engine, tmp_path: Path, channels: str, depth: str, alpha: int
) -> None:
    """Alpha arithmetic retains source colour until the final output quantisation."""
    # Given uniform straight colour whose associated values fall between integer samples.
    datatype = {"uint8": oiio.UINT8, "uint16": oiio.UINT16}[depth]
    spec = oiio.ImageSpec(40, 40, len(channels), datatype)
    spec.channelnames = list(channels)
    spec.alpha_channel = len(channels) - 1
    spec.attribute("oiio:UnassociatedAlpha", 1)
    image = oiio.ImageBuf(spec)
    colour = (200 / 255, 100 / 255, 50 / 255, alpha / 255)
    if channels == "YA":
        colour = (200 / 255, alpha / 255)
    filled = oiio.ImageBufAlgo.fill(image, colour)
    assert filled
    source = tmp_path / "straight.png"
    written = image.write(str(source), datatype, "png")
    assert written
    generator = ThumbnailGenerator(
        str(source), "png", str(tmp_path), "preview.png", {"max_width": 20, "max_height": 20}, engine=engine
    )

    # When the thumbnail resizes with associated colour then restores straight alpha.
    filename = await generator.attempt_generate_preview()
    config = oiio.ImageSpec()
    config.attribute("oiio:UnassociatedAlpha", 1)
    preview = oiio.ImageBuf(str(tmp_path / filename), 0, 0, config)
    read = preview.read(force=True)
    assert read
    pixel = preview.getpixel(0, 0)
    levels = 255
    if depth == "uint16":
        levels = 65535

    # Then colour and alpha retain their original samples within final encoding precision.
    assert pixel == pytest.approx(colour, abs=1 / levels + 1e-7)
    assert preview.nativespec().format == datatype


@pytest.fixture(
    params=[("tiff", "uint8", 4), ("tiff", "uint16", 4), ("tga", "uint8", 4), ("tiff", "uint8", 3), ("tga", "uint8", 3)]
)
def untagged_alpha_source(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    """Integer TIFF and TGA sources carry no colour-space tag when read by OIIO."""
    extension, depth, channel_count = request.param
    datatype = {"uint8": oiio.UINT8, "uint16": oiio.UINT16}[depth]
    spec = oiio.ImageSpec(40, 40, channel_count, datatype)
    if channel_count == 4:  # noqa: PLR2004 - RGBA layout.
        spec.alpha_channel = 3
        spec.attribute("oiio:UnassociatedAlpha", 1)
    image = oiio.ImageBuf(spec)
    colour = (100 / 255, 50 / 255, 25 / 255, 128 / 255)[:channel_count]
    filled = oiio.ImageBufAlgo.fill(image, colour)
    assert filled
    source = tmp_path / f"untagged.{extension}"
    written = image.write(str(source))
    assert written
    return source


@pytest.mark.asyncio
@pytest.mark.parametrize("output_format", ["png", "jpg", "webp"])
@pytest.mark.parametrize("size", [20, 64], ids=["resized", "unresized"])
async def test_untagged_integer_alpha_retains_display_colour(
    engine: Engine, tmp_path: Path, untagged_alpha_source: Path, output_format: str, size: int
) -> None:
    """Float alpha arithmetic preserves the integer source's display-referred intent."""
    # Given a real integer source whose decoder leaves colour intent untagged.
    generator = ThumbnailGenerator(
        str(untagged_alpha_source),
        output_format,
        str(tmp_path),
        f"preview.{output_format}",
        {"max_width": size, "max_height": size},
        engine=engine,
    )
    decoded = generator._read_image(untagged_alpha_source, max_source_pixels=1600)
    assert decoded.spec().get_string_attribute("oiio:ColorSpace", "") == ""
    source_depth = decoded.nativespec().format
    assert source_depth in (oiio.UINT8, oiio.UINT16)

    # When the image takes either the resized float-working-buffer path or the unchanged path.
    filename = await generator.attempt_generate_preview()
    config = oiio.ImageSpec()
    config.attribute("oiio:UnassociatedAlpha", 1)
    preview = oiio.ImageBuf(str(tmp_path / filename), 0, 0, config)
    read = preview.read(force=True)
    assert read
    pixel = preview.getpixel(0, 0)

    # Then the source brightness survives, within final encoding quantisation.
    expected = [100 / 255, 50 / 255, 25 / 255]
    if output_format != "jpg" and decoded.spec().alpha_channel >= 0:
        expected.append(128 / 255)
    tolerance = 3 / 255 + 1e-7
    if output_format == "png":
        levels = 255
        if source_depth == oiio.UINT16:
            levels = 65535
        tolerance = 1 / levels + 1e-7
        assert preview.nativespec().format == source_depth
    assert pixel == pytest.approx(expected, abs=tolerance)
    assert preview.spec().width == min(size, 40)


def test_failed_float_conversion_stops_alpha_resize(engine: Engine, tmp_path: Path, mocker: MockerFixture) -> None:
    """A failed working-format conversion surfaces before alpha arithmetic."""
    image = oiio.ImageBuf(oiio.ImageSpec(40, 40, 4, oiio.UINT8))
    image.specmod().alpha_channel = 3
    failed = oiio.ImageBuf("/nonexistent/failed.png")
    mocker.patch.object(oiio.ImageBuf, "copy", return_value=failed)
    premult = mocker.spy(oiio.ImageBufAlgo, "premult")
    generator = ThumbnailGenerator("source.png", "png", str(tmp_path), "preview.png", {}, engine=engine)

    with pytest.raises(OSError, match="prepare alpha precision"):
        generator._resize_image(image, 20, 20)

    premult.assert_not_called()


@pytest.mark.parametrize("alpha", [False, True])
def test_straight_and_rgb_inputs_skip_unpremultiplication(
    engine: Engine, tmp_path: Path, mocker: MockerFixture, *, alpha: bool
) -> None:
    """The shared boundary leaves RGB and already-straight colour untouched."""
    count = 3
    if alpha:
        count = 4
    spec = oiio.ImageSpec(4, 4, count, oiio.FLOAT)
    spec.attribute("oiio:UnassociatedAlpha", 1)
    image = oiio.ImageBuf(spec)
    unpremult = mocker.spy(oiio.ImageBufAlgo, "unpremult")
    generator = ThumbnailGenerator("source.tiff", "png", str(tmp_path), "preview.png", {}, engine=engine)

    result = generator._ensure_straight_alpha(image)

    assert result is image
    unpremult.assert_not_called()


@pytest.mark.asyncio
async def test_unresized_alpha_failure_cleans_temp_and_preserves_preview(
    engine: Engine, tmp_path: Path, mocker: MockerFixture
) -> None:
    """Straightening failure cleans the private encoding directory before publication."""
    source = tmp_path / "associated.tiff"
    spec = oiio.ImageSpec(8, 8, 4, oiio.UINT8)
    spec.alpha_channel = 3
    image = oiio.ImageBuf(spec)
    filled = oiio.ImageBufAlgo.fill(image, (0.25, 0.125, 0.0625, 0.5))
    assert filled
    written = image.write(str(source))
    assert written
    destination = tmp_path / "preview.webp"
    destination.write_bytes(b"previous")
    failed = oiio.ImageBuf("/nonexistent/failed.png")
    mocker.patch.object(oiio.ImageBufAlgo, "unpremult", return_value=failed)
    generator = ThumbnailGenerator(str(source), "webp", str(tmp_path), destination.name, {}, engine=engine)
    render = mocker.spy(generator, "_render_thumbnail")

    with pytest.raises(OSError, match="Attempted to unpremultiply"):
        await generator.attempt_generate_preview()

    surviving = destination.read_bytes()
    assert surviving == b"previous"
    scratch = render.call_args.args[1]
    exists = await anyio.Path(scratch.parent).exists()
    assert not exists
