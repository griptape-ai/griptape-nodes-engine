"""Ordinary thumbnail byte publication preserves PNG precision and workflow metadata."""

from pathlib import Path

import pytest

try:
    import OpenImageIO as oiio
except ModuleNotFoundError as error:
    if error.name != "OpenImageIO":
        raise
    pytest.skip("OpenImageIO renderer tests require the optional dependency", allow_module_level=True)
from PIL import Image
from pytest_mock import MockerFixture

from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider import ImageArtifactProvider
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.openimageio_thumbnail_generator import (
    OpenImageIOThumbnailGenerator as ThumbnailGenerator,
)
from griptape_nodes.retained_mode.managers.artifact_providers.provider_registry import ProviderRegistry


@pytest.mark.asyncio
async def test_thumbnail_byte_write_preserves_unicode_workflow_metadata(
    engine: Engine, tmp_path: Path, mocker: MockerFixture
) -> None:
    """PNG metadata arrives before publication without reducing source precision."""
    source = tmp_path / "source.png"
    image = oiio.ImageBuf(oiio.ImageSpec(8, 4, 3, oiio.UINT16))
    image_fill_ok = oiio.ImageBufAlgo.fill(image, (0.2501, 0.5001, 0.7501))
    assert image_fill_ok
    image_write_ok = image.write(str(source))
    assert image_write_ok
    metadata = {"gtn_flow_commands": "[]", "gtn_workflow_name": "\u00e9 \u732b"}
    _fake_png_provider(mocker, engine, metadata)
    generator = ThumbnailGenerator(str(source), "png", str(tmp_path), "preview.png", {}, engine=engine)

    filename = await generator.attempt_generate_preview()

    with Image.open(tmp_path / filename) as preview:
        assert all(preview.info[name] == value for name, value in metadata.items())
    result_image = oiio.ImageBuf(str(tmp_path / filename))
    result_image_read_ok = result_image.read()
    assert result_image_read_ok
    assert result_image.nativespec().format == oiio.UINT16


def _fake_png_provider(mocker: MockerFixture, engine: Engine, metadata: dict[str, str]) -> None:
    provider = ImageArtifactProvider(ProviderRegistry(engine=engine), engine=engine)
    mocker.patch.object(provider, "collect_metadata_for_write", return_value=metadata)
    mocker.patch.object(engine.artifact_manager, "provider_for_write", return_value=provider)
    engine.config_manager.set_config_value("auto_inject_workflow_metadata", True)


@pytest.mark.asyncio
async def test_thumbnail_metadata_replaces_workflow_metadata_copied_from_source(
    engine: Engine, tmp_path: Path, mocker: MockerFixture
) -> None:
    """A thumbnail of an engine-made PNG describes the new workflow, not the source's."""
    # Given a 16-bit source PNG that already carries another workflow's metadata and an unrelated key.
    source = tmp_path / "source.png"
    image = oiio.ImageBuf(oiio.ImageSpec(8, 4, 3, oiio.UINT16))
    image_fill_ok = oiio.ImageBufAlgo.fill(image, (0.2501, 0.5001, 0.7501))
    assert image_fill_ok
    image.specmod().attribute("gtn_workflow_name", "OLD")
    image.specmod().attribute("gtn_flow_commands", "OLD")
    image.specmod().attribute("prompt", "someone")
    image_write_ok = image.write(str(source), oiio.UINT16, "png")
    assert image_write_ok
    metadata = {"gtn_flow_commands": "[]", "gtn_workflow_name": "NEW"}
    _fake_png_provider(mocker, engine, metadata)
    engine.config_manager.set_config_value("preserve_source_metadata_in_thumbnails", True)
    generator = ThumbnailGenerator(str(source), "png", str(tmp_path), "preview.png", {}, engine=engine)

    # When the thumbnail is generated.
    filename = await generator.attempt_generate_preview()

    # Then the new values win, unrelated text survives, and 16-bit precision is kept.
    with Image.open(tmp_path / filename) as preview:
        assert preview.info["gtn_workflow_name"] == "NEW"
        assert preview.info["gtn_flow_commands"] == "[]"
        assert preview.info["prompt"] == "someone"
    result_image = oiio.ImageBuf(str(tmp_path / filename))
    result_image_read_ok = result_image.read(force=True)
    assert result_image_read_ok
    assert result_image.nativespec().format == oiio.UINT16
