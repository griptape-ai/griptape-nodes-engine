"""Cache freshness regression tests."""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch

import numpy as np
import OpenImageIO as oiio  # noqa: N813
import pytest

from griptape_nodes.image import GriptapeImage, GriptapeImagePipeline, ImageFormat
from griptape_nodes.image.cache import image_directory_mutation, image_source_access
from griptape_nodes.image.oiio_config import invalidate_image_cache
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.os_events import RenameFileRequest, RenameFileResultSuccess
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators import (
    RoundedPreviewGenerator,
    ThumbnailGenerator,
)
from tests.unit.image.sample_images import BLUE, RED, encode, solid


class TestCacheReview:
    def test_external_same_second_rewrite_refreshes_pixels(self, tmp_path: Path) -> None:
        path = tmp_path / "source.png"
        path.write_bytes(encode(solid(64, 64, RED), "png"))
        timestamp = path.stat().st_mtime_ns
        with GriptapeImage.open(path) as image:
            image._decoded().get_pixels(oiio.UINT8)
        path.write_bytes(encode(solid(64, 64, BLUE), "png"))
        os.utime(path, ns=(timestamp, timestamp + 1))
        with GriptapeImage.open(path) as image:
            pixels = image._decoded().get_pixels(oiio.UINT8)
            assert isinstance(pixels, np.ndarray)
            np.testing.assert_array_equal(pixels[0, 0], BLUE)

    def test_atomic_write_invalidates_after_failure(self, engine: Engine, tmp_path: Path) -> None:
        path = tmp_path / "source.png"
        writer = engine.os_manager.writer
        with (
            patch(
                "griptape_nodes.retained_mode.managers.filesystem.file_writing.atomic_write_bytes",
                side_effect=OSError("write failed"),
            ),
            patch("griptape_nodes.retained_mode.managers.filesystem.file_writing.invalidate_image_cache") as invalidate,
        ):
            result = writer._attempt_atomic_file_write(path, b"new", "utf-8", path)
        assert result.bytes_written is None
        expected_invalidations = 2
        assert invalidate.call_count == expected_invalidations

    def test_directory_rename_clears_descendant_cache(self, engine: Engine, tmp_path: Path) -> None:
        source = tmp_path / "folder"
        source.mkdir()
        (source / "image.png").write_bytes(encode(solid(64, 64, RED), "png"))
        with patch("griptape_nodes.image.cache.invalidate_all_image_cache") as clear:
            result = engine.os_manager.on_rename_file_request(
                RenameFileRequest(old_path=str(source), new_path=str(tmp_path / "renamed"), workspace_only=False)
            )
        assert isinstance(result, RenameFileResultSuccess)
        expected_invalidations = 2
        assert clear.call_count == expected_invalidations

    @pytest.mark.asyncio
    @pytest.mark.parametrize("generator_type", [ThumbnailGenerator, RoundedPreviewGenerator])
    async def test_generators_resolve_workspace_relative_paths(
        self,
        engine: Engine,
        tmp_path: Path,
        generator_type: type[ThumbnailGenerator | RoundedPreviewGenerator],
    ) -> None:
        engine.config_manager.workspace_path = tmp_path
        path = tmp_path / "source.png"
        path.write_bytes(encode(solid(64, 64, RED), "png"))
        generator = generator_type(
            source_file_location="source.png",
            preview_format="png",
            destination_preview_directory=str(tmp_path),
            destination_preview_file_name="preview.png",
            params={},
            engine=engine,
        )
        assert await generator._resolve_source() == path

    def test_source_consumer_blocks_mutation_until_pixels_are_consumed(self, tmp_path: Path) -> None:
        # Given a reader consuming one source and another source available in parallel.
        path = tmp_path / "source.png"
        entered = Event()
        release = Event()
        mutated = Event()

        def consume() -> None:
            with image_source_access(path):
                entered.set()
                assert release.wait(5)

        def mutate() -> None:
            with image_source_access(path):
                mutated.set()

        # When a mutation races with the reader, the other source remains usable.
        with ThreadPoolExecutor(max_workers=2) as executor:
            reader = executor.submit(consume)
            assert entered.wait(5)
            mutation = executor.submit(mutate)
            with image_source_access(tmp_path / "other.png"):
                assert not mutated.is_set()
            release.set()
            reader.result(timeout=5)
            mutation.result(timeout=5)

        # Then the mutation enters after the reader releases its source.
        assert mutated.is_set()

    def test_directory_gate_released_after_invalidation_failure(self, tmp_path: Path) -> None:
        with (
            patch(
                "griptape_nodes.image.cache.invalidate_all_image_cache", side_effect=[None, OSError("eviction failed")]
            ),
            pytest.raises(OSError, match="eviction failed"),
            image_directory_mutation(),
        ):
            pass
        with image_source_access(tmp_path / "source.png"):
            pass

    def test_copy_overwrite_invalidates_destination(self, engine: Engine, tmp_path: Path) -> None:
        source = tmp_path / "source.png"
        target = tmp_path / "target.png"
        source.write_bytes(encode(solid(64, 64, BLUE), "png"))
        target.write_bytes(encode(solid(64, 64, RED), "png"))
        with patch("griptape_nodes.image.cache.invalidate_image_cache") as invalidate:
            engine.os_manager._copy_file(source, target)
        assert invalidate.call_args_list == [((str(target),),), ((str(target),),)]

    def test_open_wrapper_refreshes_between_public_operations(self, tmp_path: Path) -> None:
        path = tmp_path / "source.png"
        path.write_bytes(encode(solid(64, 64, RED), "png"))
        with GriptapeImage.open(path) as source:
            with GriptapeImagePipeline(source).thumbnail(32, 32).end() as first:
                first.encode(ImageFormat.PNG)
            path.write_bytes(encode(solid(64, 64, BLUE), "png"))
            with GriptapeImagePipeline(source).thumbnail(32, 32).end() as second:
                pixels = second._decoded().get_pixels(oiio.UINT8)
                assert isinstance(pixels, np.ndarray)
                np.testing.assert_array_equal(pixels[0, 0], BLUE)

    def test_cached_missing_source_fails(self, tmp_path: Path) -> None:
        path = tmp_path / "source.png"
        path.write_bytes(encode(solid(64, 64, RED), "png"))
        with GriptapeImage.open(path) as image:
            image._decoded().get_pixels(oiio.UINT8)
        path.unlink()
        with pytest.raises(FileNotFoundError):
            GriptapeImage.open(path)
        invalidate_image_cache(path)
