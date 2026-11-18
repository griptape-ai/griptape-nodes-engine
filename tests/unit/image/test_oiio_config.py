"""Tests for OpenImageIO runtime configuration and shared ImageCache invalidation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import OpenImageIO as oiio  # noqa: N813
import pytest

from griptape_nodes.image.oiio_config import apply_openimageio_settings, invalidate_image_cache
from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete, ConfigChanged
from griptape_nodes.retained_mode.events.config_events import SetConfigValueRequest, SetConfigValueResultSuccess
from griptape_nodes.retained_mode.events.os_events import (
    ExistingFilePolicy,
    WriteFileRequest,
    WriteFileResultSuccess,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine


@pytest.fixture(autouse=True)
def reset_oiio_defaults(engine: Engine) -> Generator[None, None, None]:
    """Restore default OpenImageIO attributes after each test."""
    yield
    apply_openimageio_settings(engine.config_manager)
    oiio.ImageCache(shared=True).invalidate_all(True)


def _write_solid_png(path: Path, rgb: tuple[int, int, int]) -> bytes:
    pixels = np.empty((64, 64, 3), dtype=np.uint8)
    pixels[:, :] = rgb
    spec = oiio.ImageSpec(64, 64, 3, oiio.UINT8)
    buf = oiio.ImageBuf(spec)
    buf.set_pixels(oiio.ROI(0, 64, 0, 64, 0, 1, 0, 3), pixels)
    buf.write(str(path))
    return path.read_bytes()


class TestApplyOpenImageIOSettings:
    def test_applies_configured_values_to_shared_cache_and_global_attributes(self, engine: Engine) -> None:
        # Given configured OpenImageIO settings
        for key, value in (
            ("openimageio.max_memory_mb", 2048),
            ("openimageio.max_open_files", 250),
            ("openimageio.autotile_size", 128),
            ("openimageio.deduplicate", False),
            ("openimageio.threads", 6),
            ("openimageio.max_image_size_mb", 8192),
            ("openimageio.failure_retries", 4),
        ):
            set_result = engine.handle_request(SetConfigValueRequest(category_and_key=key, value=value))
            assert isinstance(set_result, SetConfigValueResultSuccess)

        # When settings are applied
        apply_openimageio_settings(engine.config_manager)

        # Then shared ImageCache and global OpenImageIO attributes reflect the configuration
        cache = oiio.ImageCache(shared=True)
        assert cache.getattribute("max_memory_MB", oiio.FLOAT) == pytest.approx(2048.0)
        assert cache.getattribute("max_open_files", oiio.INT) == 250  # noqa: PLR2004
        assert cache.getattribute("autotile", oiio.INT) == 128  # noqa: PLR2004
        assert cache.getattribute("autoscanline", oiio.INT) == 1
        assert cache.getattribute("deduplicate", oiio.INT) == 0
        assert cache.getattribute("failure_retries", oiio.INT) == 4  # noqa: PLR2004
        assert oiio.get_int_attribute("threads") == 6  # noqa: PLR2004
        assert oiio.get_int_attribute("exr_threads") == 6  # noqa: PLR2004
        assert oiio.get_int_attribute("limits:imagesize_MB") == 8192  # noqa: PLR2004
        assert oiio.get_int_attribute("imagebuf:use_imagecache") == 1

    def test_autotile_zero_disables_autoscanline(self, engine: Engine) -> None:
        set_result = engine.handle_request(SetConfigValueRequest(category_and_key="openimageio.autotile_size", value=0))
        assert isinstance(set_result, SetConfigValueResultSuccess)

        apply_openimageio_settings(engine.config_manager)

        cache = oiio.ImageCache(shared=True)
        assert cache.getattribute("autotile", oiio.INT) == 0
        assert cache.getattribute("autoscanline", oiio.INT) == 0

    @pytest.mark.asyncio
    async def test_artifact_manager_applies_settings_on_init_and_config_changed(self, engine: Engine) -> None:
        # Given a non-default memory limit configured prior to app initialization
        init_set = engine.handle_request(SetConfigValueRequest(category_and_key="openimageio.max_memory_mb", value=768))
        assert isinstance(init_set, SetConfigValueResultSuccess)

        # When AppInitializationComplete runs on ArtifactManager
        await engine.artifact_manager.on_app_initialization_complete(AppInitializationComplete())
        cache = oiio.ImageCache(shared=True)
        assert cache.getattribute("max_memory_MB", oiio.FLOAT) == pytest.approx(768.0)

        # And when a user updates openimageio.max_memory_mb at runtime
        set_result = engine.handle_request(
            SetConfigValueRequest(category_and_key="openimageio.max_memory_mb", value=512)
        )
        assert isinstance(set_result, SetConfigValueResultSuccess)
        engine.artifact_manager._on_config_changed(
            ConfigChanged(key="openimageio.max_memory_mb", old_value=768, new_value=512)
        )

        # Then shared ImageCache updates without restarting the engine
        assert cache.getattribute("max_memory_MB", oiio.FLOAT) == pytest.approx(512.0)


class TestImageCacheInvalidation:
    def test_invalidate_releases_open_file_handles(self, engine: Engine, tmp_path: Path) -> None:
        # Given an image opened through the shared ImageCache
        apply_openimageio_settings(engine.config_manager)
        cache = oiio.ImageCache(shared=True)
        cache.invalidate_all(True)

        image_path = tmp_path / "cached.png"
        _write_solid_png(image_path, (255, 0, 0))
        buf = oiio.ImageBuf(str(image_path))
        assert buf.getpixel(0, 0) == pytest.approx((1.0, 0.0, 0.0))
        assert "1 current" in cache.getstats(1)

        # When the path is invalidated
        invalidate_image_cache(image_path)

        # Then the open ImageInput handle is closed
        assert "0 current" in cache.getstats(1)

    @pytest.mark.asyncio
    async def test_write_file_request_invalidates_cached_tiles_on_overwrite(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        # Given a red image cached in ImageCache via ImageBuf
        engine.config_manager.workspace_path = tmp_path
        apply_openimageio_settings(engine.config_manager)
        image_path = tmp_path / "plate.png"
        _write_solid_png(image_path, (255, 0, 0))

        first_read = oiio.ImageBuf(str(image_path))
        assert first_read.getpixel(0, 0) == pytest.approx((1.0, 0.0, 0.0))

        # Build green replacement PNG bytes
        green_source = tmp_path / "green_source.png"
        green_bytes = _write_solid_png(green_source, (0, 255, 0))

        # When WriteFileRequest overwrites plate.png
        write_result = await engine.ahandle_request(
            WriteFileRequest(
                file_path=str(image_path),
                content=green_bytes,
                existing_file_policy=ExistingFilePolicy.OVERWRITE,
            )
        )
        assert isinstance(write_result, WriteFileResultSuccess)

        # Then a subsequent ImageBuf read observes the green pixels rather than stale red tiles
        second_read = oiio.ImageBuf(str(image_path))
        assert second_read.getpixel(0, 0) == pytest.approx((0.0, 1.0, 0.0))
