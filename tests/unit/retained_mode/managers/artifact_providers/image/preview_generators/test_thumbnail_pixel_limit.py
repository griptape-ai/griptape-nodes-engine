"""Thumbnail source budgets are checked from headers before eager decoding."""

from pathlib import Path

import anyio
import pytest

try:
    import OpenImageIO as oiio
except ModuleNotFoundError as error:
    if error.name != "OpenImageIO":
        raise
    pytest.skip("OpenImageIO renderer tests require the optional dependency", allow_module_level=True)
from pydantic import ValidationError
from pytest_mock import MockerFixture

from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.openimageio_thumbnail_generator import (
    OpenImageIOThumbnailGenerator as ThumbnailGenerator,
)
from griptape_nodes.retained_mode.managers.settings import FROM_ENV_CONTEXT, Settings

SETTING = "max_thumbnail_source_pixels"
PILLOW_REFUSAL_PIXELS = 178956970


def _source(path: Path) -> None:
    image = oiio.ImageBuf(oiio.ImageSpec(8, 4, 3, oiio.UINT8))
    filled = oiio.ImageBufAlgo.fill(image, (1, 0, 0))
    assert filled
    written = image.write(str(path))
    assert written


def test_pixel_budget_default_matches_pillow_refusal() -> None:
    """The default is twice Pillow's historic MAX_IMAGE_PIXELS value."""
    assert getattr(Settings(), SETTING) == PILLOW_REFUSAL_PIXELS


@pytest.mark.parametrize("value", [0, -1])
def test_pixel_budget_rejects_nonpositive_values(value: int) -> None:
    """The limit cannot be accidentally disabled with a nonpositive value."""
    with pytest.raises(ValidationError, match="greater than 0"):
        Settings.model_validate({SETTING: value})


def test_pixel_budget_loads_environment() -> None:
    """An environment override uses the positive integer setting."""
    settings = Settings.model_validate({SETTING: "32"}, context={FROM_ENV_CONTEXT: True})
    assert getattr(settings, SETTING) == 32  # noqa: PLR2004 - Literal fixture boundary.


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [32, 33], ids=["at-limit", "higher-limit"])
async def test_source_at_or_below_limit_decodes(engine: Engine, tmp_path: Path, limit: int) -> None:
    """A small fixture exercises the boundary without large allocations."""
    source = tmp_path / "source.png"
    _source(source)
    engine.config_manager.set_config_value(SETTING, limit)
    generator = ThumbnailGenerator(str(source), "png", str(tmp_path), "preview.png", {}, engine=engine)

    filename = await generator.attempt_generate_preview()

    assert filename == "preview.png"
    assert (tmp_path / filename).exists()


@pytest.mark.asyncio
async def test_oversized_source_is_refused_before_decode_and_cleans_temp(
    engine: Engine, tmp_path: Path, mocker: MockerFixture
) -> None:
    """Header rejection prevents eager pixel reads, keeps the preview and removes scratch."""
    # Given a 32-pixel source and a smaller source budget.
    source = tmp_path / "source.png"
    _source(source)
    destination = tmp_path / "preview.png"
    destination.write_bytes(b"previous")
    engine.config_manager.set_config_value(SETTING, 31)
    eager_read = mocker.spy(oiio.ImageBuf, "read")
    generator = ThumbnailGenerator(str(source), "png", str(tmp_path), destination.name, {}, engine=engine)
    decode = mocker.spy(generator, "_read_image")
    render = mocker.spy(generator, "_render_thumbnail")

    # When the header exceeds the budget, no pixel decoding or rendering occurs.
    with pytest.raises(OSError, match=SETTING) as failure:
        await generator.attempt_generate_preview()

    eager_read.assert_not_called()
    render.assert_not_called()
    message = str(failure.value)
    assert str(source) in message
    assert "8 x 4" in message
    assert "32 pixels" in message
    assert "31" in message
    scratch_source = decode.call_args.args[0]
    exists = await anyio.Path(scratch_source.parent).exists()
    assert not exists
    surviving = destination.read_bytes()
    assert surviving == b"previous"


@pytest.mark.asyncio
async def test_budget_uses_data_window_not_display_window(engine: Engine, tmp_path: Path) -> None:
    """A large display frame does not inflate the stored-pixel decode count."""
    source = tmp_path / "offset.exr"
    spec = oiio.ImageSpec(8, 4, 3, oiio.HALF)
    spec.x, spec.y = 10, 20
    spec.full_width, spec.full_height = 100, 100
    image = oiio.ImageBuf(spec)
    filled = oiio.ImageBufAlgo.fill(image, (1, 0, 0))
    assert filled
    written = image.write(str(source))
    assert written
    engine.config_manager.set_config_value(SETTING, 32)
    generator = ThumbnailGenerator(str(source), "png", str(tmp_path), "preview.png", {}, engine=engine)

    filename = await generator.attempt_generate_preview()

    assert filename == "preview.png"
