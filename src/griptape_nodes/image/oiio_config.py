"""OpenImageIO runtime settings and shared ImageCache management."""

from __future__ import annotations

from typing import TYPE_CHECKING

import OpenImageIO as oiio  # noqa: N813

from griptape_nodes.image.cache import invalidate_image_cache
from griptape_nodes.retained_mode.managers.settings import OPENIMAGEIO_KEY, OpenImageIOSettings, Settings

__all__ = ["apply_openimageio_settings", "invalidate_image_cache"]

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.managers.config_manager import ConfigManager


def apply_openimageio_settings(config_manager: ConfigManager) -> OpenImageIOSettings:
    """Apply the engine's configured OpenImageIO limits and cache attributes to the current process.

    Args:
        config_manager: The engine's configuration manager.

    Returns:
        The validated OpenImageIOSettings applied to OpenImageIO.
    """
    raw_value = config_manager.get_config_value(OPENIMAGEIO_KEY)
    if raw_value is None:
        settings = OpenImageIOSettings()
    else:
        settings = Settings.model_validate({OPENIMAGEIO_KEY: raw_value}).openimageio

    oiio.attribute("imagebuf:use_imagecache", 1)
    oiio.attribute("threads", settings.threads)
    oiio.attribute("exr_threads", settings.threads)
    oiio.attribute("limits:imagesize_MB", settings.max_image_size_mb)

    cache = oiio.ImageCache(shared=True)
    cache.attribute("max_memory_MB", float(settings.max_memory_mb))
    cache.attribute("max_open_files", settings.max_open_files)
    cache.attribute("autotile", settings.autotile_size)
    cache.attribute("autoscanline", int(settings.autotile_size > 0))
    cache.attribute("deduplicate", int(settings.deduplicate))
    cache.attribute("unassociatedalpha", 1)
    cache.attribute("failure_retries", settings.failure_retries)
    return settings
