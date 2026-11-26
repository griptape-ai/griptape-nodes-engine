"""Temporary encoding feeds ordinary byte writes and owns its scratch lifetime."""

import asyncio
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import anyio
import pytest
from pytest_mock import MockerFixture

from griptape_nodes.common.macro_parser import ParsedMacro
from griptape_nodes.files.drivers.local_file_driver import LocalFileDriver
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
from griptape_nodes.retained_mode.events.os_events import (
    FileIOFailureReason,
    ReadFileRequest,
    ReadFileResultFailure,
    ReadFileResultSuccess,
    WriteFileRequest,
)
from griptape_nodes.retained_mode.events.project_events import (
    GetCurrentProjectRequest,
    GetCurrentProjectResultSuccess,
    GetPathForMacroRequest,
    GetPathForMacroResultFailure,
    GetPathForMacroResultSuccess,
    PathResolutionFailureReason,
)

try:
    from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.openimageio_thumbnail_generator import (
        OpenImageIOThumbnailGenerator,
    )
except ModuleNotFoundError as error:
    if error.name != "OpenImageIO":
        raise
    pytest.skip("OpenImageIO renderer tests require the optional dependency", allow_module_level=True)
from griptape_nodes.retained_mode.managers.event_manager import ResultContext


@pytest.fixture
def project_temp(engine: Engine, tmp_path: Path) -> Iterator[Path]:
    """Override the real project's temp directory while retaining macro resolution."""
    result = engine.handle_request(GetCurrentProjectRequest())
    assert isinstance(result, GetCurrentProjectResultSuccess)
    directory = result.project_info.template.directories["temp"]
    original = directory.path_macro
    location = tmp_path / "custom-temp"
    directory.path_macro = str(location)
    try:
        yield location
    finally:
        directory.path_macro = original


def _generator(engine: Engine, source: Path, destination: Path) -> OpenImageIOThumbnailGenerator:
    return OpenImageIOThumbnailGenerator(
        str(source), "png", str(destination.parent), destination.name, {}, engine=engine
    )


@pytest.mark.asyncio
async def test_encoding_uses_project_temp_and_dispatches_bytes(
    engine: Engine,
    tmp_path: Path,
    project_temp: Path,
    image_source: Callable[[str, str], Path],
    mocker: MockerFixture,
) -> None:
    """Verify scratch ownership and lifetime across the public generator."""
    # Given a customised project temp directory and a real source.
    source = image_source("small_square", "png8")
    destination = tmp_path / "previews" / "preview.png"
    generator = _generator(engine, source, destination)
    render = mocker.spy(generator, "_render_thumbnail")
    decode = mocker.spy(generator, "_read_image")
    reads: list[ReadFileRequest] = []
    writes: list[WriteFileRequest] = []

    def observe(request: RequestPayload, _context: ResultContext) -> ResultPayload | None:
        if isinstance(request, ReadFileRequest):
            reads.append(request)
        if isinstance(request, WriteFileRequest):
            writes.append(request)
        return None

    engine.event_manager.add_pre_dispatch_hook(observe)
    try:
        filename = await generator.attempt_generate_preview()
    finally:
        engine.event_manager.remove_pre_dispatch_hook(observe)

    # Then a uniquely owned, correctly suffixed encoding was removed before normal publication.
    scratch = render.call_args.args[1]
    scratch_is_under_project_temp = scratch.parent.parent.samefile(project_temp)
    assert scratch_is_under_project_temp
    assert scratch.suffix == ".png"
    decode_source = decode.call_args.args[0]
    assert decode_source != source
    assert decode_source.parent == scratch.parent
    assert decode_source.suffix == source.suffix
    assert not scratch.parent.exists()
    assert len(reads) == 1
    assert reads[0].should_transform_image_content_to_thumbnail is False
    assert filename == destination.name
    assert len(writes) == 1
    assert isinstance(writes[0].content, bytes)
    assert writes[0].content.startswith(b"\x89PNG\r\n\x1a\n")
    remaining = [path async for path in anyio.Path(project_temp).iterdir()]
    assert remaining == []


@pytest.mark.asyncio
async def test_temp_resolution_failure_does_not_encode_or_replace_preview(
    engine: Engine, tmp_path: Path, image_source: Callable[[str, str], Path], mocker: MockerFixture
) -> None:
    """A failed temp lookup stops before encoding or publication."""
    source = image_source("small_square", "png8")
    destination = tmp_path / "preview.png"
    destination.write_bytes(b"previous")
    generator = _generator(engine, source, destination)
    render = mocker.spy(generator, "_render_thumbnail")
    failure = GetPathForMacroResultFailure(
        failure_reason=PathResolutionFailureReason.MACRO_RESOLUTION_ERROR,
        result_details="The project temp directory is unavailable",
    )

    def refuse(request: RequestPayload, _context: ResultContext) -> ResultPayload | None:
        if isinstance(request, GetPathForMacroRequest) and request.parsed_macro.template == "{temp}":
            return failure
        return None

    engine.event_manager.add_pre_dispatch_hook(refuse)
    try:
        with pytest.raises(OSError, match="temp directory is unavailable"):
            await generator.attempt_generate_preview()
    finally:
        engine.event_manager.remove_pre_dispatch_hook(refuse)

    render.assert_not_called()
    surviving = destination.read_bytes()
    assert surviving == b"previous"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["encode", "read-back"])
async def test_failed_temp_operation_cleans_private_directory(  # noqa: PLR0913, PLR0917 - Independent fixtures and failure stages.
    engine: Engine,
    tmp_path: Path,
    project_temp: Path,
    image_source: Callable[[str, str], Path],
    mocker: MockerFixture,
    failure_stage: str,
) -> None:
    """Encoding and read-back failures remove only their private scratch directory."""
    source = image_source("small_square", "png8")
    destination = tmp_path / "preview.png"
    destination.write_bytes(b"previous")
    generator = _generator(engine, source, destination)
    staged: list[Path] = []

    def render(_image: object, path: Path) -> None:
        staged.append(path)
        path.write_bytes(b"partial")
        if failure_stage == "encode":
            message = "Encoding failed"
            raise OSError(message)
        path.unlink()

    mocker.patch.object(generator, "_render_thumbnail", side_effect=render)

    with pytest.raises(OSError, match=r".+") as failure:
        await generator.attempt_generate_preview()

    if failure_stage == "encode":
        assert str(failure.value) == "Encoding failed"
    else:
        assert isinstance(failure.value, FileNotFoundError)
    assert len(staged) == 1
    assert not staged[0].parent.exists()
    remaining = [path async for path in anyio.Path(project_temp).iterdir()]
    assert remaining == []
    surviving = destination.read_bytes()
    assert surviving == b"previous"


@pytest.mark.asyncio
async def test_cancellation_waits_for_encoding_before_cleaning_temp(
    engine: Engine,
    tmp_path: Path,
    project_temp: Path,
    image_source: Callable[[str, str], Path],
    mocker: MockerFixture,
) -> None:
    """Verify scratch ownership and lifetime across the public generator."""
    # Given an encoder still using its scratch file on a worker thread.
    source = image_source("small_square", "png8")
    destination = tmp_path / "preview.png"
    destination.write_bytes(b"previous")
    generator = _generator(engine, source, destination)
    started = threading.Event()
    release = threading.Event()
    staged: list[Path] = []

    def render(_image: object, path: Path) -> None:
        staged.append(path)
        path.write_bytes(b"partial")
        started.set()
        if not release.wait(timeout=5):
            message = "Encoder was not released"
            raise TimeoutError(message)
        path.write_bytes(b"complete")

    mocker.patch.object(generator, "_render_thumbnail", side_effect=render)
    operation = asyncio.create_task(generator.attempt_generate_preview())
    try:
        ready = await asyncio.to_thread(started.wait, 5)
        assert ready
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
        assert staged[0].exists()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation

    # Then encoding has finished before cleanup and the destination has never been published.
    assert not staged[0].parent.exists()
    remaining = [path async for path in anyio.Path(project_temp).iterdir()]
    assert remaining == []
    surviving = destination.read_bytes()
    assert surviving == b"previous"


@pytest.mark.asyncio
async def test_resolver_oserror_uses_normal_driver_not_found_result(
    engine: Engine, tmp_path: Path, mocker: MockerFixture
) -> None:
    """A resolver OSError follows LocalFileDriver's ordinary FILE_NOT_FOUND contract."""
    source = tmp_path / "unreachable.png"
    mocker.patch.object(LocalFileDriver, "_resolve_path", side_effect=OSError("network host unavailable"))
    requests: list[ReadFileRequest] = []
    results: list[ReadFileResultFailure] = []
    dispatch = engine.ahandle_request

    async def record(request: RequestPayload) -> ResultPayload:
        result = await dispatch(request)
        if isinstance(request, ReadFileRequest):
            requests.append(request)
            assert isinstance(result, ReadFileResultFailure)
            results.append(result)
        return result

    mocker.patch.object(engine, "ahandle_request", side_effect=record)
    generator = _generator(engine, source, tmp_path / "preview.png")
    with pytest.raises(OSError, match="File not found"):
        await generator.attempt_generate_preview()

    assert len(requests) == 1
    assert results[0].failure_reason is FileIOFailureReason.FILE_NOT_FOUND


@pytest.mark.asyncio
async def test_text_read_result_is_rejected_before_decoding(
    engine: Engine, tmp_path: Path, mocker: MockerFixture
) -> None:
    """Text results cannot become temporary image inputs."""
    result = ReadFileResultSuccess(
        content="plain text", file_size=10, mime_type="text/plain", encoding="utf-8", result_details="Read text"
    )
    mocker.patch.object(engine, "ahandle_request", return_value=result)
    generator = _generator(engine, tmp_path / "source.txt", tmp_path / "preview.png")
    decode = mocker.spy(generator, "_read_image")
    with pytest.raises(OSError, match="source containing text"):
        await generator.attempt_generate_preview()
    decode.assert_not_called()


@pytest.mark.asyncio
async def test_decode_failure_cleans_temp_and_preserves_original_context(
    engine: Engine, tmp_path: Path, project_temp: Path, mocker: MockerFixture
) -> None:
    """Decoder ValueError stays a decoder error and private source storage is removed."""
    source = tmp_path / "source.png"
    source.write_bytes(b"invalid image")
    destination = tmp_path / "preview.png"
    destination.write_bytes(b"previous")
    generator = _generator(engine, source, destination)
    decoded_paths: list[Path] = []

    def fail_decode(path: Path, *, max_source_pixels: int) -> None:  # noqa: ARG001 - Decoder signature.
        decoded_paths.append(path)
        message = "Unsupported pixel layout"
        raise ValueError(message)

    mocker.patch.object(generator, "_read_image", side_effect=fail_decode)
    with pytest.raises(ValueError, match="Unsupported pixel layout"):
        await generator.attempt_generate_preview()

    assert len(decoded_paths) == 1
    assert decoded_paths[0] != source
    assert not decoded_paths[0].parent.exists()
    remaining = [path async for path in anyio.Path(project_temp).iterdir()]
    assert remaining == []
    surviving = destination.read_bytes()
    assert surviving == b"previous"


@pytest.mark.asyncio
async def test_buffered_source_survives_original_deletion(
    engine: Engine, tmp_path: Path, project_temp: Path, image_source: Callable[[str, str], Path], mocker: MockerFixture
) -> None:
    """Decoding uses bytes already returned by the driver rather than reopening the source."""
    original = image_source("small_square", "png8")
    source = tmp_path / "source.png"
    source.write_bytes(original.read_bytes())
    destination = tmp_path / "preview.png"
    generator = _generator(engine, source, destination)
    decode = generator._read_image

    def delete_original(path: Path, *, max_source_pixels: int) -> object:
        source.unlink()
        return decode(path, max_source_pixels=max_source_pixels)

    mocker.patch.object(generator, "_read_image", side_effect=delete_original)
    filename = await generator.attempt_generate_preview()

    assert filename == destination.name
    assert not source.exists()
    assert destination.exists()
    remaining = [path async for path in anyio.Path(project_temp).iterdir()]
    assert remaining == []


def test_default_temp_location_resolves_through_project(engine: Engine) -> None:
    """The default project provides an absolute temp location."""
    result = engine.handle_request(GetPathForMacroRequest(parsed_macro=ParsedMacro("{temp}"), variables={}))
    assert isinstance(result, GetPathForMacroResultSuccess)
    assert result.absolute_path.is_absolute()
