"""OpenImageIO runtime settings and shared ImageCache management."""

from __future__ import annotations

from typing import TYPE_CHECKING

import OpenImageIO as oiio  # noqa: N813

from griptape_nodes.files.path_utils import canonicalize_for_identity
from griptape_nodes.retained_mode.managers.settings import OPENIMAGEIO_KEY, OpenImageIOSettings, Settings

if TYPE_CHECKING:
    from pathlib import Path

    from griptape_nodes.retained_mode.managers.config_manager import ConfigManager

_WINDOWS_LONG_PATH_PREFIX = "\\\\?\\"


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


def invalidate_image_cache(path: str | Path) -> None:
    """Evict cached tiles and close any open ImageInput handle for ``path`` in the shared ImageCache.

    Args:
        path: Local file path to invalidate.
    """
    cache = oiio.ImageCache(shared=True)
    raw = str(path)
    candidates = {raw}
    if raw.startswith(_WINDOWS_LONG_PATH_PREFIX):
        candidates.add(raw.removeprefix(_WINDOWS_LONG_PATH_PREFIX))
    candidates.add(str(canonicalize_for_identity(path)))

    for candidate in candidates:
        cache.invalidate(candidate, True)
