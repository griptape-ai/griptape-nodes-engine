"""Save-path macros can use `media.*` variables read from the bytes being written."""

import subprocess
from collections.abc import Generator
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image
from static_ffmpeg import run as static_ffmpeg_run

from griptape_nodes.common.macro_parser import ParsedMacro
from griptape_nodes.common.project_templates import (
    DEFAULT_PROJECT_TEMPLATE,
    ProjectTemplate,
    ProjectValidationInfo,
    ProjectValidationStatus,
    load_partial_project_template,
)
from griptape_nodes.files.media_variables import MediaInfo, reserved_variables_in, unknown_media_variables
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.artifact_events import RegisterArtifactProviderRequest
from griptape_nodes.retained_mode.events.os_events import (
    ExistingFilePolicy,
    FileIOFailureReason,
    WriteFileRequest,
    WriteFileResultFailure,
    WriteFileResultSuccess,
)
from griptape_nodes.retained_mode.events.project_events import (
    LoadProjectTemplateRequest,
    LoadProjectTemplateResultSuccess,
    MacroPath,
    SetCurrentProjectRequest,
)
from griptape_nodes.retained_mode.file_metadata.sidecar_metadata import SidecarContent, SituationMetadata
from griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider import (
    ImageArtifactProvider,
)
from griptape_nodes.retained_mode.managers.artifact_providers.video.video_artifact_provider import (
    VideoArtifactMetadata,
    VideoArtifactProvider,
)
from tests.workflow_context import unsaved_workflow_open


def _png_bytes(width: int, height: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height)).save(buffer, format="PNG")
    return buffer.getvalue()


async def _write(engine: Engine, template: str, content: bytes | str, **kwargs: object) -> object:
    request = WriteFileRequest(file_path=MacroPath(ParsedMacro(template), {}), content=content, **kwargs)  # type: ignore[arg-type]
    return await engine.os_manager.writer.on_write_file_request(request, engine.current_workflow())


def _written_name(result: object) -> str:
    assert isinstance(result, WriteFileResultSuccess), result
    return Path(result.final_file_path).name


@pytest.fixture
def project(engine: Engine, tmp_path: Path) -> Generator[None, None, None]:
    """The default project, with the image and video providers registered."""
    original_workspace = engine.config_manager.workspace_path
    project_yml = tmp_path / "project_template.yml"
    project_yml.write_text(DEFAULT_PROJECT_TEMPLATE.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE))
    load_result = engine.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
    assert isinstance(load_result, LoadProjectTemplateResultSuccess)
    engine.handle_request(SetCurrentProjectRequest(project_id=load_result.project_id))
    engine.config_manager.workspace_path = tmp_path
    for provider_class in (ImageArtifactProvider, VideoArtifactProvider):
        engine.artifact_manager.on_handle_register_artifact_provider_request(
            RegisterArtifactProviderRequest(provider_class=provider_class)
        )
    with unsaved_workflow_open(engine):
        yield
    engine.handle_request(SetCurrentProjectRequest(project_id=None))
    engine.config_manager.workspace_path = original_workspace


class TestMediaInfo:
    def test_values_become_media_variables_without_scientific_notation(self) -> None:
        info = MediaInfo(width=1920, height=1080, fps=24.0, duration=1234567.25)

        assert info.to_macro_variables() == {
            "media.width": "1920",
            "media.height": "1080",
            "media.fps": "24",
            "media.duration": "1234567.25",
        }

    def test_fractional_frame_rates_keep_three_decimals(self) -> None:
        assert MediaInfo(fps=24000 / 1001).to_macro_variables() == {"media.fps": "23.976"}

    def test_what_ffprobe_could_not_read_stays_unset(self) -> None:
        metadata = VideoArtifactMetadata(
            width=0, height=0, duration_seconds=0.0, codec="unknown", frame_rate=0.0, file_size=10
        )

        assert metadata.to_media_info() == MediaInfo()

    def test_unknown_and_reserved_names_are_found(self) -> None:
        parsed = ParsedMacro("{outputs}/{media.width}_{media.bitrate?}_{media.fps?}.png")

        assert unknown_media_variables(parsed) == ["media.bitrate"]
        assert reserved_variables_in({"media.width": 1, "file_name_base": "x"}) == ["media.width"]


@pytest.mark.usefixtures("project")
class TestImageSavePaths:
    @pytest.mark.asyncio
    async def test_width_and_height_name_the_file(self, engine: Engine) -> None:
        result = await _write(engine, "{outputs}/render_{media.width}x{media.height}.png", _png_bytes(64, 32))

        assert _written_name(result) == "render_64x32.png"

    @pytest.mark.asyncio
    async def test_an_optional_value_the_image_lacks_drops_out(self, engine: Engine) -> None:
        result = await _write(engine, "{outputs}/render_{media.width}{media.fps?:^_}.png", _png_bytes(8, 8))

        assert _written_name(result) == "render_8.png"

    @pytest.mark.asyncio
    async def test_a_required_value_fails_for_content_that_is_not_media(self, engine: Engine) -> None:
        result = await _write(engine, "{outputs}/notes_{media.width}.txt", "just text")

        assert isinstance(result, WriteFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.MISSING_MACRO_VARIABLES
        assert result.missing_variables == {"media.width"}

    @pytest.mark.asyncio
    async def test_values_come_from_the_bytes_when_the_suffix_is_wrong(self, engine: Engine) -> None:
        result = await _write(engine, "{outputs}/shot_{media.width}.jpg", _png_bytes(10, 20))

        assert _written_name(result) == "shot_10.png"

    @pytest.mark.asyncio
    async def test_create_new_walks_the_index_with_the_values_in_the_name(self, engine: Engine) -> None:
        template = "{outputs}/render_{media.width}x{media.height}_v{_index:02}.png"
        names = []
        for _ in range(2):
            result = await _write(
                engine, template, _png_bytes(16, 9), existing_file_policy=ExistingFilePolicy.CREATE_NEW
            )
            names.append(_written_name(result))

        assert names == ["render_16x9_v01.png", "render_16x9_v02.png"]

    @pytest.mark.asyncio
    async def test_callers_cannot_supply_media_variables(self, engine: Engine) -> None:
        request = WriteFileRequest(
            file_path=MacroPath(ParsedMacro("{outputs}/x_{media.width}.png"), {"media.width": 5}),
            content=_png_bytes(4, 4),
        )

        result = await engine.os_manager.writer.on_write_file_request(request, engine.current_workflow())

        assert isinstance(result, WriteFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.INVALID_PATH
        assert "media.width" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_a_template_without_media_variables_stages_nothing(self, engine: Engine) -> None:
        writer = engine.os_manager.writer
        with patch.object(writer, "_stage_bytes_at_temp", wraps=writer._stage_bytes_at_temp) as stage:
            result = await _write(engine, "{outputs}/plain.png", _png_bytes(4, 4))

        assert _written_name(result) == "plain.png"
        assert stage.call_count == 0

    @pytest.mark.asyncio
    async def test_the_sidecar_records_the_values_the_path_used(self, engine: Engine) -> None:
        template = "{outputs}/render_{media.width}.png"
        metadata = SidecarContent(situation=SituationMetadata(name="custom", macro=template, variables={}))
        with patch("griptape_nodes.retained_mode.managers.filesystem.file_writing.write_sidecar") as write_sidecar:
            await _write(engine, template, _png_bytes(12, 5), file_metadata=metadata)

        recorded = write_sidecar.call_args.args[1]
        assert recorded.situation.variables == {
            "media.width": "12",
            "media.height": "5",
            "media.channels": "3",
            "media.color_space": "RGB",
        }


@pytest.mark.usefixtures("project")
class TestVideoSavePaths:
    @pytest.mark.asyncio
    async def test_the_vet_and_the_media_read_share_one_staged_copy(self, engine: Engine, tmp_path: Path) -> None:
        try:
            ffmpeg_path, _ = static_ffmpeg_run.get_or_fetch_platform_executables_else_raise()
        except Exception:
            pytest.skip("ffmpeg is not available")
        video = tmp_path / "source.mp4"
        subprocess.run(  # noqa: S603, ASYNC221
            [
                ffmpeg_path,
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x240:rate=24",
                "-t",
                "1",
                str(video),
            ],
            check=True,
        )
        writer = engine.os_manager.writer

        with patch.object(writer, "_stage_bytes_at_temp", wraps=writer._stage_bytes_at_temp) as stage:
            result = await _write(
                engine, "{outputs}/clip_{media.width}x{media.height}_{media.fps}fps.mp4", video.read_bytes()
            )

        assert _written_name(result) == "clip_320x240_24fps.mp4"
        assert stage.call_count == 1


class TestProjectValidation:
    @staticmethod
    def _merge(macro: str, validation: ProjectValidationInfo) -> ProjectTemplate:
        yaml_text = f"""
project_template_schema_version: "0.1.0"
name: "Media Project"
situations:
  my_situation:
    macro: "{macro}"
    policy:
      on_collision: "overwrite"
      create_dirs: true
"""
        overlay = load_partial_project_template(yaml_text, validation)
        assert overlay is not None
        return ProjectTemplate.merge(DEFAULT_PROJECT_TEMPLATE, overlay, validation)

    def test_an_unknown_name_warns_and_the_project_stays_usable(self) -> None:
        macro = "{outputs}/{media.bitrate?}.png"
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)

        template = self._merge(macro, validation)

        assert validation.status == ProjectValidationStatus.FLAWED
        assert [problem.field_path for problem in validation.problems] == ["situations.my_situation.macro"]
        assert "media.bitrate" in validation.problems[0].message
        assert template.situations["my_situation"].macro == macro

    def test_known_names_load_cleanly(self) -> None:
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)

        self._merge("{outputs}/{media.width}x{media.height}{media.fps?:^_}.png", validation)

        assert validation.status == ProjectValidationStatus.GOOD
