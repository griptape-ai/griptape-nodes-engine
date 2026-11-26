import logging
from collections.abc import Callable
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

from griptape_nodes.common.macro_parser import ParsedMacro
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.artifact_events import (
    GeneratePreviewRequest,
    GeneratePreviewResultSuccess,
    GetArtifactProviderDetailsRequest,
    GetArtifactProviderDetailsResultSuccess,
    GetPreviewGeneratorDetailsRequest,
    GetPreviewGeneratorDetailsResultSuccess,
    RegisterArtifactProviderRequest,
    RegisterArtifactProviderResultSuccess,
)
from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
from griptape_nodes.retained_mode.events.os_events import FileIOFailureReason, WriteFileRequest, WriteFileResultFailure
from griptape_nodes.retained_mode.events.project_events import MacroPath
from griptape_nodes.retained_mode.managers.artifact_providers.image._backends import openimageio
from griptape_nodes.retained_mode.managers.artifact_providers.image.openimageio_artifact_provider import (
    OpenImageIOArtifactProvider,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.openimageio_thumbnail_generator import (
    EXIF_ORIENTATION_FLIP_HORIZONTAL,
    EXIF_ORIENTATION_FLIP_VERTICAL,
    EXIF_ORIENTATION_NORMAL,
    EXIF_ORIENTATION_ROTATE_90_CLOCKWISE,
    EXIF_ORIENTATION_ROTATE_90_COUNTERCLOCKWISE,
    EXIF_ORIENTATION_ROTATE_180,
    EXIF_ORIENTATION_TRANSPOSE,
    EXIF_ORIENTATION_TRANSVERSE,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.openimageio_thumbnail_generator import (
    OpenImageIOThumbnailGenerator as ThumbnailGenerator,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.thumbnail_generator_base import (
    BaseThumbnailGenerator,
)


class TestThumbnailGeneratorSourceFormats:
    """Discover readable formats from the installed OpenImageIO plugins."""

    def test_source_formats_follow_available_input_plugins(self, mocker: MockerFixture) -> None:
        openimageio._discover_source_formats.cache_clear()
        mocker.patch.object(
            oiio,
            "get_string_attribute",
            return_value="png:png;openexr:exr,sxr;jpegxl:jxl;null:null,nul;term:term;iff:iff,z;zfile:zfile;future:future",
        )
        create_input = mocker.patch.object(oiio.ImageInput, "create", side_effect=[object(), object(), None])
        mocker.patch.object(oiio, "geterror", return_value="No input plugin for jpegxl")

        try:
            formats = ThumbnailGenerator.get_supported_source_formats()
            assert formats == {"png", "exr", "sxr"}
            provider_formats = OpenImageIOArtifactProvider.get_supported_formats()
            assert provider_formats == formats
            formats.clear()
            provider_formats.clear()
            assert ThumbnailGenerator.get_supported_source_formats() == {"png", "exr", "sxr"}
            assert OpenImageIOArtifactProvider.get_supported_formats() == {"png", "exr", "sxr"}
            assert create_input.call_args_list == [
                mocker.call("png"),
                mocker.call("openexr"),
                mocker.call("jpegxl"),
            ]
        finally:
            openimageio._discover_source_formats.cache_clear()

    def test_source_formats_exclude_unapproved_plugins(self) -> None:
        excluded_extensions = {"null", "nul", "term", "iff", "z", "zfile"}

        assert ThumbnailGenerator.get_supported_source_formats().isdisjoint(excluded_extensions)

    def test_source_formats_include_previous_supported_formats(self) -> None:
        previous_formats = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "tiff", "tif", "tga"}

        assert previous_formats <= ThumbnailGenerator.get_supported_source_formats()


@pytest.mark.asyncio
async def test_registered_image_provider_previews_exr(engine: Engine, tmp_path: Path) -> None:
    """The standard preview request accepts EXR and reports matching source formats."""
    # Given an EXR and the registered OpenImageIO provider.
    source = tmp_path / "source.exr"
    image = oiio.ImageBuf(oiio.ImageSpec(8, 4, 3, oiio.HALF))
    filled = oiio.ImageBufAlgo.fill(image, (0.25, 0.5, 0.75))
    assert filled
    written = image.write(str(source), oiio.HALF, "openexr")
    assert written
    registration = await engine.ahandle_request(
        RegisterArtifactProviderRequest(provider_class=OpenImageIOArtifactProvider)
    )
    assert isinstance(registration, RegisterArtifactProviderResultSuccess)

    # When clients query capabilities and request a standard preview.
    provider = await engine.ahandle_request(GetArtifactProviderDetailsRequest(friendly_name="Image"))
    generator = await engine.ahandle_request(
        GetPreviewGeneratorDetailsRequest(
            provider_friendly_name="Image", preview_generator_friendly_name="Standard Thumbnail Generation"
        )
    )
    result = await engine.ahandle_request(
        GeneratePreviewRequest(
            macro_path=MacroPath(ParsedMacro(str(source)), {}), artifact_provider_name="Image", format="png"
        )
    )

    # Then both details agree and the preview contains a readable PNG.
    assert isinstance(provider, GetArtifactProviderDetailsResultSuccess)
    assert isinstance(generator, GetPreviewGeneratorDetailsResultSuccess)
    assert "exr" in provider.supported_formats
    assert provider.supported_formats == generator.supported_source_formats
    assert isinstance(result, GeneratePreviewResultSuccess), result
    assert isinstance(result.paths_to_preview, str)
    preview_path = anyio.Path(result.paths_to_preview)
    content = await preview_path.read_bytes()
    assert content.startswith(b"\x89PNG\r\n\x1a\n")
    preview = oiio.ImageBuf(result.paths_to_preview)
    read = preview.read(force=True)
    assert read


class TestThumbnailGeneratorParameterValidation:
    """Test ThumbnailGenerator parameter validation."""

    def test_generator_constructor_uses_validation(self, tmp_path: Path) -> None:
        """Test that the constructor raises ValidationError for invalid parameters."""
        invalid_params = {"max_width": "not_a_number", "max_height": 768}
        source_path = tmp_path / "test.png"
        dest_dir = tmp_path / "preview"

        with pytest.raises(ValidationError):
            ThumbnailGenerator(
                source_file_location=str(source_path),
                preview_format="webp",
                destination_preview_directory=str(dest_dir),
                destination_preview_file_name="preview.webp",
                params=invalid_params,
            )

    @pytest.mark.parametrize(
        ("params", "expected"),
        [
            pytest.param({"max_width": 1024, "max_height": 768}, {"max_width": 1024, "max_height": 768}, id="valid"),
            pytest.param({"max_width": 1024}, {"max_width": 1024, "max_height": 1024}, id="missing-key"),
            # Ignore extra fields for compatibility with old configs: https://github.com/griptape-ai/griptape-nodes-engine/issues/3980
            pytest.param(
                {"max_width": 1024, "max_height": 768, "unknown_param": 42},
                {"max_width": 1024, "max_height": 768},
                id="extra-key",
            ),
        ],
    )
    def test_valid_parameters(self, params: dict[str, int], expected: dict[str, int]) -> None:
        params_model_class = ThumbnailGenerator.get_parameters()

        validated_params = params_model_class.model_validate(params)

        assert validated_params.model_dump() == expected
        assert not hasattr(validated_params, "unknown_param")

    @pytest.mark.parametrize(
        ("params", "error_type"),
        [
            pytest.param({"max_width": "not_a_number", "max_height": 768}, None, id="invalid-type"),
            pytest.param({"max_width": -100, "max_height": 768}, "greater_than", id="negative-value"),
        ],
    )
    def test_invalid_parameters(self, params: dict[str, int | str], error_type: str | None) -> None:
        params_model_class = ThumbnailGenerator.get_parameters()

        with pytest.raises(ValidationError) as exc_info:
            params_model_class.model_validate(params)

        errors = exc_info.value.errors()
        assert len(errors) >= 1
        assert any(error["loc"][0] == "max_width" for error in errors)
        if error_type is not None:
            assert any(error["loc"][0] == "max_width" and error_type in error["type"] for error in errors)


class TestThumbnailGeneratorGeneration:
    """Internal geometry ordering and invalid input checks."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("orientation", "expected_resize_size"),
        [
            pytest.param(EXIF_ORIENTATION_NORMAL, (32, 21), id="normal"),
            pytest.param(EXIF_ORIENTATION_FLIP_HORIZONTAL, (32, 21), id="flip-horizontal"),
            pytest.param(EXIF_ORIENTATION_ROTATE_180, (32, 21), id="rotate-180"),
            pytest.param(EXIF_ORIENTATION_FLIP_VERTICAL, (32, 21), id="flip-vertical"),
            pytest.param(EXIF_ORIENTATION_TRANSPOSE, (48, 32), id="transpose"),
            pytest.param(EXIF_ORIENTATION_ROTATE_90_CLOCKWISE, (48, 32), id="rotate-90-clockwise"),
            pytest.param(EXIF_ORIENTATION_TRANSVERSE, (48, 32), id="transverse"),
            pytest.param(EXIF_ORIENTATION_ROTATE_90_COUNTERCLOCKWISE, (48, 32), id="rotate-90-counterclockwise"),
        ],
    )
    async def test_resize_before_reorient(  # noqa: PLR0913, PLR0917 - Independent fixtures and orientation axes.
        self,
        engine: Engine,
        tmp_path: Path,
        mocker: MockerFixture,
        orientation: int,
        expected_resize_size: tuple[int, int],
        image_source: Callable[[str, str], Path],
    ) -> None:
        # Given an asymmetric image with distinct corners and rectangular bounds.
        source = image_source("orientation", f"orientation_{orientation}")
        destination = tmp_path / "preview.png"
        reorient = mocker.spy(oiio.ImageBufAlgo, "reorient")
        generator = ThumbnailGenerator(
            str(source), "png", str(tmp_path), destination.name, {"max_width": 32, "max_height": 48}, engine=engine
        )

        # When the thumbnail is rendered.
        filename = await generator.attempt_generate_preview()

        # Then only thumbnail-sized pixels are reoriented, with the expected corners and bounds.
        assert filename == destination.name
        resized = reorient.call_args.args[0].spec()
        assert (resized.width, resized.height) == expected_resize_size

    @pytest.mark.asyncio
    async def test_invalid_image_raises(self, engine: Engine, tmp_path: Path) -> None:
        source = tmp_path / "invalid.png"
        source.write_bytes(b"\x89PNG\r\n\x1a\ninvalid image")
        generator = ThumbnailGenerator(str(source), "png", str(tmp_path), "preview.png", {}, engine=engine)

        with pytest.raises(OSError, match="Attempted to load image") as error:
            await generator.attempt_generate_preview()

        message = str(error.value)
        assert message.count("Attempted to") == 1
        assert message.count("Failed due to") == 1
        assert str(source) in message

    @pytest.mark.asyncio
    async def test_missing_source_has_one_read_failure_sentence(self, engine: Engine, tmp_path: Path) -> None:
        source = tmp_path / "missing.png"
        generator = ThumbnailGenerator(str(source), "png", str(tmp_path), "preview.png", {}, engine=engine)

        with pytest.raises(OSError, match="File not found") as error:
            await generator.attempt_generate_preview()

        message = str(error.value)
        assert str(source) in message

    @pytest.mark.asyncio
    async def test_overscan_colour_cannot_bleed_into_display_frame(self, engine: Engine, tmp_path: Path) -> None:
        # Given green frame pixels surrounded by red overscan.
        spec = oiio.ImageSpec(12, 12, 3, oiio.FLOAT)
        spec.x, spec.y = -2, -2
        spec.full_width, spec.full_height = 8, 8
        spec.attribute("oiio:ColorSpace", "sRGB")
        image = oiio.ImageBuf(spec)
        filled = oiio.ImageBufAlgo.fill(image, (1, 0, 0))
        assert filled
        framed = oiio.ImageBufAlgo.fill(image, (0, 1, 0), roi=oiio.ROI(0, 8, 0, 8, 0, 1, 0, 3))
        assert framed
        source = tmp_path / "overscan.exr"
        written = image.write(str(source))
        assert written
        generator = ThumbnailGenerator(
            str(source), "png", str(tmp_path), "preview.png", {"max_width": 4, "max_height": 4}, engine=engine
        )

        # When the display frame is reduced, exterior red must contribute nothing.
        filename = await generator.attempt_generate_preview()
        preview = oiio.ImageBuf(str(tmp_path / filename))
        read = preview.read(force=True)
        assert read
        corners = [preview.getpixel(x, y) for x, y in [(0, 0), (3, 0), (0, 3), (3, 3)]]
        for corner in corners:
            assert corner == pytest.approx((0, 1, 0), abs=1e-6)

    @pytest.mark.asyncio
    async def test_write_failure_names_destination_and_source(
        self, engine: Engine, tmp_path: Path, image_source: Callable[[str, str], Path], mocker: MockerFixture
    ) -> None:
        source = image_source("square", "png8")
        destination = tmp_path / "preview.png"
        failure = WriteFileResultFailure(
            failure_reason=FileIOFailureReason.IO_ERROR, result_details="The destination is unavailable"
        )
        dispatch = engine.ahandle_request

        async def fail_write(request: RequestPayload) -> ResultPayload:
            if isinstance(request, WriteFileRequest):
                return failure
            return await dispatch(request)

        mocker.patch.object(engine, "ahandle_request", side_effect=fail_write)
        generator = ThumbnailGenerator(str(source), "png", str(tmp_path), destination.name, {}, engine=engine)

        with pytest.raises(OSError, match="Attempted to write preview") as error:
            await generator.attempt_generate_preview()

        assert str(error.value) == (
            f"Attempted to write preview '{destination}' for '{source}'. Failed due to The destination is unavailable."
        )


class TestThumbnailGeneratorOpenImageIOFailures:
    """A failing OpenImageIO step stops the thumbnail with its own explanation and writes nothing."""

    @staticmethod
    def _failed_image() -> oiio.ImageBuf:
        """An image buffer that reports an error, as OpenImageIO does when an operation fails."""
        failed = oiio.ImageBuf("/nonexistent/failed.png")
        assert failed.has_error
        return failed

    @staticmethod
    def _generator(
        engine: Engine, source: Path, tmp_path: Path, preview_format: str, params: dict[str, int] | None = None
    ) -> BaseThumbnailGenerator:
        return ThumbnailGenerator(
            str(source), preview_format, str(tmp_path), f"preview.{preview_format}", params or {}, engine=engine
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("step", "message"),
        [("resize", "resize image"), ("reorient", "orient image")],
    )
    async def test_failed_geometry_step_raises(  # noqa: PLR0913, PLR0917 - Independent fixtures and failure axes.
        self,
        engine: Engine,
        tmp_path: Path,
        mocker: MockerFixture,
        step: str,
        message: str,
        image_source: Callable[[str, str], Path],
    ) -> None:
        # Given a source larger than the bounds, so it is resized, and a failing OpenImageIO step.
        source = image_source("small_square", "png8")
        mocker.patch.object(oiio.ImageBufAlgo, step, return_value=self._failed_image())
        generator = self._generator(engine, source, tmp_path, "png", {"max_width": 16, "max_height": 16})

        # When the thumbnail is generated, then the failing step is named and nothing is published.
        with pytest.raises(OSError, match=f"Attempted to {message}"):
            await generator.attempt_generate_preview()

        exists = await anyio.Path(tmp_path / "preview.png").exists()
        assert not exists

    @pytest.mark.asyncio
    async def test_failed_channel_conversion_raises(
        self, engine: Engine, tmp_path: Path, image_source: Callable[[str, str], Path], mocker: MockerFixture
    ) -> None:
        # Given a grayscale source that JPEG output must expand to RGB, and a failing conversion.
        source = image_source("grayscale", "png8")
        mocker.patch.object(oiio.ImageBufAlgo, "channels", return_value=self._failed_image())
        generator = self._generator(engine, source, tmp_path, "jpg")

        with pytest.raises(OSError, match="Attempted to convert image channels"):
            await generator.attempt_generate_preview()

        exists = await anyio.Path(tmp_path / "preview.jpg").exists()
        assert not exists

    @pytest.mark.asyncio
    @pytest.mark.parametrize("step", ["crop", "cut"])
    async def test_failed_display_framing_keeps_previous_preview(
        self, engine: Engine, tmp_path: Path, image_source: Callable[[str, str], Path], mocker: MockerFixture, step: str
    ) -> None:
        # Given windowed input and an existing preview, with a failing framing operation.
        if step == "crop":
            source = image_source("window_overscan", "exr_half")
        else:
            source = image_source("window_nonzero_display", "exr_half")
        existing = tmp_path / "preview.png"
        existing.write_bytes(b"previous preview")
        mocker.patch.object(oiio.ImageBufAlgo, step, return_value=self._failed_image())
        generator = self._generator(engine, source, tmp_path, "png")

        # When framing fails, the previous preview survives and staging is removed.
        with pytest.raises(OSError, match="Attempted to frame"):
            await generator.attempt_generate_preview()

        surviving = existing.read_bytes()
        assert surviving == b"previous preview"
        staging = [path.name async for path in anyio.Path(tmp_path).iterdir() if path.name.startswith(".gtn-write")]
        assert staging == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("step", ["premult", "unpremult"])
    async def test_failed_alpha_resize_keeps_previous_preview(
        self, engine: Engine, tmp_path: Path, image_source: Callable[[str, str], Path], mocker: MockerFixture, step: str
    ) -> None:
        source = image_source("alpha_edge_black", "png8")
        existing = tmp_path / "preview.png"
        existing.write_bytes(b"previous preview")
        mocker.patch.object(oiio.ImageBufAlgo, step, return_value=self._failed_image())
        generator = self._generator(engine, source, tmp_path, "png", {"max_width": 16, "max_height": 16})

        with pytest.raises(OSError, match="for its preview"):
            await generator.attempt_generate_preview()

        surviving = existing.read_bytes()
        assert surviving == b"previous preview"
        staging = [path.name async for path in anyio.Path(tmp_path).iterdir() if path.name.startswith(".gtn-write")]
        assert staging == []

    @pytest.mark.asyncio
    async def test_failed_encode_raises_and_keeps_previous_preview(
        self, engine: Engine, tmp_path: Path, image_source: Callable[[str, str], Path], mocker: MockerFixture
    ) -> None:
        # Given an existing preview and an encoder that fails.
        source = image_source("square", "png8")
        existing = tmp_path / "preview.png"
        existing.write_bytes(b"previous preview")
        mocker.patch.object(oiio.ImageBuf, "write", return_value=False)
        generator = self._generator(engine, source, tmp_path, "png")

        # When the thumbnail is generated, then the failure is explained and the old preview survives.
        with pytest.raises(OSError, match="Attempted to encode image"):
            await generator.attempt_generate_preview()

        surviving = existing.read_bytes()
        assert surviving == b"previous preview"
        staging = [path.name async for path in anyio.Path(tmp_path).iterdir() if path.name.startswith(".gtn-write")]
        assert staging == []


async def _render(engine: Engine, source: Path, tmp_path: Path, preview_format: str = "png") -> Path:
    generator = ThumbnailGenerator(
        str(source), preview_format, str(tmp_path), f"preview.{preview_format}", {}, engine=engine
    )
    return tmp_path / await generator.attempt_generate_preview()


class TestThumbnailGeneratorColourManagement:
    """Scene-referred sources are converted to sRGB before the 8-bit encode.

    Mid-grey 0.18 in scene-linear light is 118/255 in sRGB. Without a transform it is written as 46/255.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("image_id", "variant_id"), [("display_grey", "tiff"), ("display_grey", "png"), ("dpx_grey", "rec709")]
    )
    async def test_display_referred_sources_skip_the_conversion_step(  # noqa: PLR0913, PLR0917 - Fixtures and source axes.
        self,
        engine: Engine,
        tmp_path: Path,
        mocker: MockerFixture,
        image_id: str,
        variant_id: str,
        image_source: Callable[[str, str], Path],
    ) -> None:
        """Converting sRGB to sRGB changes nothing, so the work is not done."""
        source = image_source(image_id, variant_id)
        convert = mocker.spy(oiio.ImageBufAlgo, "colorconvert")

        await _render(engine, source, tmp_path)

        convert.assert_not_called()

    @pytest.mark.asyncio
    async def test_log_dpx_is_left_unchanged_and_the_reason_is_logged(
        self, engine: Engine, tmp_path: Path, caplog: pytest.LogCaptureFixture, image_source: Callable[[str, str], Path]
    ) -> None:
        """The built-in colour config has no Kodak log space, so the pixels are written as they are."""
        source = image_source("dpx_grey", "log")

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            await _render(engine, source, tmp_path)

        messages = [record.message for record in caplog.records if "colour space" in record.message]
        assert len(messages) == 1
        assert "KodakLog" in messages[0]
        assert source.name in messages[0]

    @pytest.mark.asyncio
    async def test_conversion_uses_the_built_in_config_not_the_environment_one(
        self, engine: Engine, tmp_path: Path, mocker: MockerFixture, image_source: Callable[[str, str], Path]
    ) -> None:
        """A studio `$OCIO` config without these space names must not break thumbnails."""
        source = image_source("scene_grey", "exr")
        convert = mocker.spy(oiio.ImageBufAlgo, "colorconvert")

        await _render(engine, source, tmp_path)

        convert.assert_called_once()
        assert convert.call_args.kwargs["colorconfig"] == "ocio://default"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("step", ["unpremult", "colorconvert"])
    async def test_failed_conversion_step_raises_and_writes_nothing(
        self, engine: Engine, tmp_path: Path, mocker: MockerFixture, step: str, image_source: Callable[[str, str], Path]
    ) -> None:
        source = image_source("scene_alpha", "exr")
        failed = oiio.ImageBuf("/nonexistent/failed.png")
        assert failed.has_error
        mocker.patch.object(oiio.ImageBufAlgo, step, return_value=failed)

        with pytest.raises(OSError, match="for its preview"):
            await _render(engine, source, tmp_path)

        exists = await anyio.Path(tmp_path / "preview.png").exists()
        assert not exists

    @pytest.mark.asyncio
    async def test_conversion_happens_after_resizing(
        self, engine: Engine, tmp_path: Path, mocker: MockerFixture, image_source: Callable[[str, str], Path]
    ) -> None:
        """Only thumbnail-sized pixels are converted."""
        source = image_source("large_scene", "exr")
        convert = mocker.spy(oiio.ImageBufAlgo, "colorconvert")
        generator = ThumbnailGenerator(
            str(source), "png", str(tmp_path), "preview.png", {"max_width": 16, "max_height": 16}, engine=engine
        )

        await generator.attempt_generate_preview()

        converted = convert.call_args.args[0].spec()
        assert (converted.width, converted.height) == (16, 16)
