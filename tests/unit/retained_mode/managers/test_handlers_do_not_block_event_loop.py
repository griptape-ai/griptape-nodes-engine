"""Handlers that do CPU-bound image work must leave the engine's event loop free.

A 9000x9000 image takes seconds to re-encode for a write or to shrink for a preview. Done on the
event loop, it stops the engine from answering the editor for that long. Each test parks the slow
step on a worker thread and asks the engine something else while it is parked: if the loop were
held, the question could not be answered until the step gave up waiting.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from PIL import Image

from griptape_nodes.common.macro_parser import ParsedMacro
from griptape_nodes.retained_mode.events.config_events import GetConfigValueRequest, GetConfigValueResultSuccess
from griptape_nodes.retained_mode.events.os_events import (
    ExistingFilePolicy,
    ReadFileRequest,
    ReadFileResultSuccess,
    WriteFileRequest,
    WriteFileResultSuccess,
)
from griptape_nodes.retained_mode.events.project_events import MacroPath
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.pil_rounded_preview_generator import (
    PILRoundedPreviewGenerator,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.pil_thumbnail_generator import (
    PILThumbnailGenerator,
)

if TYPE_CHECKING:
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine

# What the editor would tolerate: far shorter than the slow step, far longer than a healthy request.
_RESPONSIVE_WITHIN_SECONDS = 1.0
# Only reached if the loop is held, so the test cannot release the step in time.
_SLOW_STEP_GIVES_UP_SECONDS = 5.0
_START_TIMEOUT_SECONDS = 10.0


class _SlowStep:
    """A stand-in for a slow, synchronous step that stays busy until the test releases it."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.gave_up = False
        self.thread_id: int | None = None

    def run(self, result: Any) -> Any:
        self.thread_id = threading.get_ident()
        self.started.set()
        if not self.release.wait(timeout=_SLOW_STEP_GIVES_UP_SECONDS):
            self.gave_up = True
        return result

    async def wait_until_started(self) -> None:
        deadline = time.monotonic() + _START_TIMEOUT_SECONDS
        while not self.started.is_set():
            if time.monotonic() > deadline:
                msg = "The slow step never started."
                raise AssertionError(msg)
            await asyncio.sleep(0.01)


async def _assert_engine_answers_while_parked(engine: Engine, step: _SlowStep) -> None:
    await step.wait_until_started()
    asked_at = time.monotonic()
    result = await asyncio.wait_for(
        engine.ahandle_request(GetConfigValueRequest(category_and_key="log_level")), timeout=_START_TIMEOUT_SECONDS
    )
    answered_in = time.monotonic() - asked_at
    assert isinstance(result, GetConfigValueResultSuccess), result
    assert answered_in < _RESPONSIVE_WITHIN_SECONDS


def _png_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (64, 64), "red").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def slow_metadata_injection(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> _SlowStep:
    """Make the metadata injection a write does (a full image re-encode) wait for the test."""
    step = _SlowStep()
    engine.config_manager.set_config_value("auto_inject_workflow_metadata", True)
    provider = MagicMock()
    provider.collect_metadata_for_write.return_value = {"k": "v"}
    provider.prepare_content_for_write.side_effect = lambda data, _name, _metadata: step.run(data)
    monkeypatch.setattr(engine.artifact_manager, "provider_for_write", lambda _name: provider)
    return step


class TestWriteFileRequest:
    @pytest.mark.asyncio
    async def test_engine_answers_while_a_write_prepares_its_content(
        self, engine: Engine, tmp_path: Path, slow_metadata_injection: _SlowStep
    ) -> None:
        target = tmp_path / "out.png"
        write = asyncio.create_task(
            engine.ahandle_request(
                WriteFileRequest(
                    file_path=str(target), content=_png_bytes(), existing_file_policy=ExistingFilePolicy.OVERWRITE
                )
            )
        )
        try:
            await _assert_engine_answers_while_parked(engine, slow_metadata_injection)
        finally:
            slow_metadata_injection.release.set()
            result = await asyncio.wait_for(write, timeout=_START_TIMEOUT_SECONDS)

        assert not slow_metadata_injection.gave_up, "The write held the event loop until its step gave up."
        assert isinstance(result, WriteFileResultSuccess), result
        assert target.read_bytes() == _png_bytes()
        assert slow_metadata_injection.thread_id != threading.get_ident()

    def test_sync_request_without_a_running_loop_writes_the_file(self, engine: Engine, tmp_path: Path) -> None:
        target = tmp_path / "bootstrap.png"

        result = engine.handle_request(
            WriteFileRequest(
                file_path=str(target), content=_png_bytes(), existing_file_policy=ExistingFilePolicy.OVERWRITE
            )
        )

        assert isinstance(result, WriteFileResultSuccess), result
        assert target.exists()

    @pytest.mark.asyncio
    async def test_sync_request_from_inside_a_running_loop_writes_the_file(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        target = tmp_path / "inside_loop.png"

        result = engine.handle_request(
            WriteFileRequest(
                file_path=str(target), content=_png_bytes(), existing_file_policy=ExistingFilePolicy.OVERWRITE
            )
        )

        assert isinstance(result, WriteFileResultSuccess), result
        assert target.exists()

    def test_sync_request_from_a_worker_thread_writes_the_file(self, engine: Engine, tmp_path: Path) -> None:
        """Storage drivers and the config manager send their writes through the sync request path."""
        target = tmp_path / "worker_thread.bin"
        request = WriteFileRequest(
            file_path=str(target), content=b"abc", existing_file_policy=ExistingFilePolicy.OVERWRITE
        )

        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(engine.handle_request, request).result(timeout=_START_TIMEOUT_SECONDS)

        assert isinstance(result, WriteFileResultSuccess), result
        assert target.read_bytes() == b"abc"

    @pytest.mark.asyncio
    async def test_engine_state_is_read_on_the_loop_and_only_the_disk_write_leaves_it(
        self, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Manager state has no locks, so path resolution and the index scan must stay on the loop."""
        loop_thread = threading.get_ident()
        threads: dict[str, set[int]] = {"macro resolution": set(), "index scan": set(), "disk write": set()}
        os_manager = engine.os_manager

        def record(step: str, method_name: str) -> None:
            original = getattr(os_manager, method_name)

            def wrapper(*args: Any, **kwargs: Any) -> Any:
                threads[step].add(threading.get_ident())
                return original(*args, **kwargs)

            monkeypatch.setattr(os_manager, method_name, wrapper)

        record("macro resolution", "_resolve_macro_path_to_string")
        record("index scan", "_scan_for_next_available_index")
        record("disk write", "_attempt_file_write")
        engine.config_manager.workspace_path = tmp_path
        (tmp_path / "taken.txt").write_text("already here")

        macro_result = await engine.ahandle_request(
            WriteFileRequest(file_path=MacroPath(ParsedMacro("{workspace_dir}/from_macro.txt"), {}), content="macro")
        )
        indexed_result = await engine.ahandle_request(
            WriteFileRequest(
                file_path=str(tmp_path / "taken.txt"),
                content="indexed",
                existing_file_policy=ExistingFilePolicy.CREATE_NEW,
            )
        )

        assert isinstance(macro_result, WriteFileResultSuccess), macro_result
        assert isinstance(indexed_result, WriteFileResultSuccess), indexed_result
        assert (tmp_path / "taken_1.txt").read_text() == "indexed"
        assert threads["macro resolution"] == {loop_thread}
        assert threads["index scan"] == {loop_thread}
        assert threads["disk write"], "The write never reached the disk step."
        assert loop_thread not in threads["disk write"]


def _read_max_nodes_in_parallel(user_config: Path) -> int:
    return json.loads(user_config.read_text())["max_nodes_in_parallel"]


class TestReadFileThumbnail:
    """ReadFileRequest turns an image into a thumbnail data URL unless it is already in the workspace."""

    @pytest.mark.asyncio
    async def test_engine_answers_while_a_read_makes_its_thumbnail(
        self, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine.config_manager.workspace_path = tmp_path / "workspace"
        source = tmp_path / "source.png"
        source.write_bytes(_png_bytes())
        step = _SlowStep()
        # Pillow runs before any registry lookup, so this exercises the off-loop encode directly.
        monkeypatch.setattr(engine.os_manager, "_image_bytes_to_preview_data_url", lambda _data: step.run("data:thumb"))

        read = asyncio.create_task(engine.ahandle_request(ReadFileRequest(file_path=str(source), workspace_only=False)))
        try:
            await _assert_engine_answers_while_parked(engine, step)
        finally:
            step.release.set()
            result = await asyncio.wait_for(read, timeout=_START_TIMEOUT_SECONDS)

        assert not step.gave_up, "Making the thumbnail held the event loop until it gave up."
        assert isinstance(result, ReadFileResultSuccess), result
        assert result.content == "data:thumb"
        assert step.thread_id != threading.get_ident()

    @pytest.mark.asyncio
    async def test_thumbnail_is_a_webp_data_url(self, engine: Engine, tmp_path: Path) -> None:
        engine.config_manager.workspace_path = tmp_path / "workspace"
        source = tmp_path / "source.png"
        source.write_bytes(_png_bytes())

        result = await engine.ahandle_request(ReadFileRequest(file_path=str(source), workspace_only=False))

        assert isinstance(result, ReadFileResultSuccess), result
        assert isinstance(result.content, str)
        assert result.content.startswith("data:image/webp;base64,")

    @pytest.mark.asyncio
    async def test_unreadable_image_falls_back_to_its_full_data_url(self, engine: Engine, tmp_path: Path) -> None:
        engine.config_manager.workspace_path = tmp_path / "workspace"
        source = tmp_path / "broken.png"
        broken = b"\x89PNG\r\n\x1a\n\x00\xff not really an image"
        source.write_bytes(broken)

        result = await engine.ahandle_request(ReadFileRequest(file_path=str(source), workspace_only=False))

        assert isinstance(result, ReadFileResultSuccess), result
        assert result.content == f"data:image/png;base64,{base64.b64encode(broken).decode('utf-8')}"

    @pytest.mark.asyncio
    async def test_workspace_check_runs_on_the_loop(
        self, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An image already in the workspace gets a static URL, built from engine state on the loop."""
        loop_thread = threading.get_ident()
        threads: set[int] = set()

        def base_url(_self: object) -> str:
            threads.add(threading.get_ident())
            return "http://static"

        monkeypatch.setattr(type(engine.static_files_manager), "static_server_base_url", property(base_url))
        engine.config_manager.workspace_path = tmp_path
        source = tmp_path / "source.png"
        source.write_bytes(_png_bytes())

        result = await engine.ahandle_request(ReadFileRequest(file_path=str(source), workspace_only=False))

        assert isinstance(result, ReadFileResultSuccess), result
        assert result.content == "http://static/workspace/source.png"
        assert threads == {loop_thread}


class TestConfigWrites:
    """Config writes are sync callers of the async WriteFileRequest handler."""

    def test_config_write_without_a_running_loop_reaches_the_user_config(
        self, engine: Engine, isolate_user_config: Path
    ) -> None:
        assert engine.config_manager.set_config_value("max_nodes_in_parallel", 7)

        assert _read_max_nodes_in_parallel(isolate_user_config) == 7  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_config_write_on_the_loop_thread_reaches_the_user_config(
        self, engine: Engine, isolate_user_config: Path
    ) -> None:
        """The sync caller blocks the loop thread, so the write must run on a loop of its own."""
        assert engine.config_manager.set_config_value("max_nodes_in_parallel", 8)

        assert _read_max_nodes_in_parallel(isolate_user_config) == 8  # noqa: PLR2004

    def test_config_write_reports_failure_when_the_write_raises(
        self,
        engine: Engine,
        isolate_user_config: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A handler that raises comes back from the dispatcher as a generic failure, not a write failure."""
        isolate_user_config.write_text(json.dumps({"max_nodes_in_parallel": 3}))

        def fail_to_write(*_args: Any, **_kwargs: Any) -> Any:
            msg = "disk unavailable"
            raise RuntimeError(msg)

        monkeypatch.setattr(engine.os_manager, "_attempt_atomic_file_write", fail_to_write)
        monkeypatch.setattr(engine.os_manager, "_attempt_file_write", fail_to_write)

        with caplog.at_level(logging.ERROR):
            written = engine.config_manager._write_user_config_delta({"max_nodes_in_parallel": 9})

        assert written is False
        assert _read_max_nodes_in_parallel(isolate_user_config) == 3  # noqa: PLR2004
        assert any("Attempted to write merged config" in record.message for record in caplog.records)

    def test_config_write_reports_failure_when_creating_the_file_fails(
        self,
        engine: Engine,
        isolate_user_config: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        isolate_user_config.unlink()

        def fail_to_write(*_args: Any, **_kwargs: Any) -> Any:
            msg = "disk unavailable"
            raise RuntimeError(msg)

        monkeypatch.setattr(engine.os_manager, "_attempt_file_write", fail_to_write)

        with caplog.at_level(logging.ERROR):
            written = engine.config_manager._write_user_config_delta({"max_nodes_in_parallel": 9})

        assert written is False
        assert not isolate_user_config.exists()
        assert any("Attempted to create user config file" in record.message for record in caplog.records)


def _make_generator(generator_type: type, engine: Engine, source: Path, destination_dir: Path) -> Any:
    return generator_type(
        str(source),
        "webp",
        str(destination_dir),
        "preview.webp",
        {"max_width": 32, "max_height": 32},
        engine=engine,
    )


class TestPreviewGenerators:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("generator_type", "render_method"),
        [
            (PILThumbnailGenerator, "_render_thumbnail"),
            (PILRoundedPreviewGenerator, "_render_rounded_preview"),
        ],
        ids=["thumbnail", "rounded"],
    )
    async def test_engine_answers_while_a_preview_is_rendered(
        self,
        engine: Engine,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        generator_type: type,
        render_method: str,
    ) -> None:
        source = tmp_path / "source.png"
        source.write_bytes(_png_bytes())
        generator = _make_generator(generator_type, engine, source, tmp_path / "previews")
        step = _SlowStep()
        monkeypatch.setattr(generator, render_method, lambda _image_data: step.run(_png_bytes()))

        generation = asyncio.create_task(generator.attempt_generate_preview())
        try:
            await _assert_engine_answers_while_parked(engine, step)
        finally:
            step.release.set()
            file_name = await asyncio.wait_for(generation, timeout=_START_TIMEOUT_SECONDS)

        assert not step.gave_up, "Rendering the preview held the event loop until it gave up."
        assert (tmp_path / "previews" / file_name).exists()
        assert step.thread_id != threading.get_ident()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "generator_type", [PILThumbnailGenerator, PILRoundedPreviewGenerator], ids=["thumbnail", "rounded"]
    )
    @pytest.mark.parametrize("mode", ["RGB", "RGBA"])
    async def test_preview_is_still_rendered_from_the_source_image(
        self, engine: Engine, tmp_path: Path, generator_type: type, mode: str
    ) -> None:
        source = tmp_path / "source.png"
        Image.new(mode, (200, 100), "blue").save(source)
        generator = _make_generator(generator_type, engine, source, tmp_path / "previews")

        file_name = await generator.attempt_generate_preview()

        with Image.open(tmp_path / "previews" / file_name) as preview:
            assert preview.width == 32  # noqa: PLR2004
            assert preview.height <= 32  # noqa: PLR2004

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "generator_type", [PILThumbnailGenerator, PILRoundedPreviewGenerator], ids=["thumbnail", "rounded"]
    )
    async def test_preview_write_failure_raises(
        self, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, generator_type: type
    ) -> None:
        source = tmp_path / "source.png"
        source.write_bytes(_png_bytes())
        generator = _make_generator(generator_type, engine, source, tmp_path / "previews")

        def fail_to_write(*_args: Any, **_kwargs: Any) -> Any:
            msg = "disk unavailable"
            raise RuntimeError(msg)

        monkeypatch.setattr(engine.os_manager, "_attempt_atomic_file_write", fail_to_write)
        monkeypatch.setattr(engine.os_manager, "_attempt_file_write", fail_to_write)

        with pytest.raises(OSError, match="Failed to write preview image"):
            await generator.attempt_generate_preview()
