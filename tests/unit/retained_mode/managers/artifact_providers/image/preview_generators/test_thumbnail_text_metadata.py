"""PNG source text retention is opt-in and separate from publication provenance."""

import struct
from io import BytesIO
from pathlib import Path

import anyio
import pytest
from PIL import Image, ImageCms, PngImagePlugin
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
from griptape_nodes.retained_mode.managers.settings import FROM_ENV_CONTEXT, Settings
from tests.workflow_context import unsaved_workflow_open

SETTING = "preserve_source_metadata_in_thumbnails"
LONG_TEXT = "source workflow " * 14000


def _source(path: Path) -> None:
    text = PngImagePlugin.PngInfo()
    text.add_text("gtn_workflow", LONG_TEXT, zip=True)
    text.add_text("gtn_node_name", "source_node")
    text.add_text("gtn_flow_commands", "SOURCE")
    text.add_itxt("artist_note", "\u00e9 \u732b", zip=True)
    with Image.new("RGB", (40, 30), "red") as image:
        image.save(path, pnginfo=text)


def _text(data: bytes) -> dict[str, str]:
    with BytesIO(data) as buffer, Image.open(buffer) as image:
        assert isinstance(image, PngImagePlugin.PngImageFile)
        image.load()
        return dict(image.text)


def test_preservation_setting_defaults_off() -> None:
    """Existing configs discard source PNG text without opting in."""
    assert getattr(Settings(), SETTING) is False


@pytest.mark.parametrize("enabled", [False, True])
def test_preservation_setting_accepts_boolean(*, enabled: bool) -> None:
    """The preservation option accepts explicit booleans."""
    settings = Settings.model_validate({SETTING: enabled})
    assert getattr(settings, SETTING) is enabled


@pytest.mark.parametrize(("value", "expected"), [("true", True), ("false", False)])
def test_preservation_setting_loads_environment(value: str, *, expected: bool) -> None:
    """Environment configuration uses the same boolean setting."""
    settings = Settings.model_validate({SETTING: value}, context={FROM_ENV_CONTEXT: True})
    assert getattr(settings, SETTING) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("preserve", [False, True])
async def test_source_text_policy_preserves_icc_profile(engine: Engine, tmp_path: Path, *, preserve: bool) -> None:
    """Removing artist text leaves the rendering profile intact."""
    source = tmp_path / "profile.png"
    text = PngImagePlugin.PngInfo()
    text.add_text("artist_note", "keep if enabled")
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    with Image.new("RGB", (8, 4), "red") as image:
        image.save(source, pnginfo=text, icc_profile=profile)
    engine.config_manager.set_config_value(SETTING, preserve)
    engine.config_manager.set_config_value("auto_inject_workflow_metadata", False)
    registration = engine.handle_request(RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider))
    assert isinstance(registration, RegisterArtifactProviderResultSuccess)
    result = await engine.ahandle_request(
        GeneratePreviewRequest(
            macro_path=MacroPath(ParsedMacro(str(source)), {}), artifact_provider_name="Image", format="png"
        )
    )
    assert isinstance(result, GeneratePreviewResultSuccess)
    assert isinstance(result.paths_to_preview, str)
    with Image.open(result.paths_to_preview) as image:
        output_profile = image.info.get("icc_profile")
    assert output_profile == profile


@pytest.mark.asyncio
@pytest.mark.parametrize("preserve", [False, True])
@pytest.mark.parametrize("inject", [False, True])
@pytest.mark.parametrize("bound", [False, True])
async def test_preview_request_text_policy(  # noqa: PLR0913 - Independent policy axes and fixtures.
    engine: Engine, tmp_path: Path, mocker: MockerFixture, *, preserve: bool, inject: bool, bound: bool
) -> None:
    """Actual preview requests apply preservation independently of workflow injection."""
    # Given a PNG with compressed source provenance and unrelated Unicode text.
    source = tmp_path / "source.png"
    _source(source)
    original = source.read_bytes()
    registration = engine.handle_request(RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider))
    assert isinstance(registration, RegisterArtifactProviderResultSuccess)
    engine.config_manager.set_config_value(SETTING, preserve)
    engine.config_manager.set_config_value("auto_inject_workflow_metadata", inject)
    fields = {"gtn_flow_commands": "CURRENT", "gtn_workflow_name": "current_workflow"}
    if not bound:
        fields = {"gtn_saved_at": "current_time"}
    mocker.patch(
        "griptape_nodes.retained_mode.managers.artifact_providers.image.base_image_artifact_provider.collect_workflow_metadata",
        return_value=fields,
    )
    request = GeneratePreviewRequest(
        macro_path=MacroPath(ParsedMacro(str(source)), {}),
        artifact_provider_name="Image",
        format="png",
        preview_generator_name="Standard Thumbnail Generation",
        preview_generator_parameters={"max_width": 20, "max_height": 20},
    )

    # When the actual engine preview request renders and publishes.
    if bound:
        with unsaved_workflow_open(engine):
            result = await engine.ahandle_request(request)
    else:
        result = await engine.ahandle_request(request)

    # Then preservation and publication metadata apply independently.
    assert isinstance(result, GeneratePreviewResultSuccess)
    assert isinstance(result.paths_to_preview, str)
    encoded = await anyio.Path(result.paths_to_preview).read_bytes()
    text = _text(encoded)
    expected: dict[str, str] = {}
    if preserve:
        expected = {
            "gtn_workflow": LONG_TEXT,
            "gtn_node_name": "source_node",
            "gtn_flow_commands": "SOURCE",
            "artist_note": "\u00e9 \u732b",
        }
    if inject:
        expected.update(fields)
    for key in ["gtn_workflow", "gtn_node_name", "gtn_flow_commands", "artist_note", *fields]:
        assert text.get(key) == expected.get(key)
    assert len(encoded) < 10000  # noqa: PLR2004 - Large source text must remain compressed.
    after = source.read_bytes()
    assert after == original
    if preserve:
        offset = 8
        found = False
        while offset < len(encoded):
            size = struct.unpack(">I", encoded[offset : offset + 4])[0]
            kind = encoded[offset + 4 : offset + 8]
            payload = encoded[offset + 8 : offset + 8 + size]
            if payload.startswith(b"gtn_workflow\x00"):
                assert kind == b"zTXt"
                found = True
            offset += size + 12
        assert found
