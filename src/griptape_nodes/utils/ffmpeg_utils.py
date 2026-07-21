"""Locate the ffmpeg and ffprobe binaries used for video processing.

Each binary is resolved independently, in this order:

1. An explicit path from settings (``ffmpeg_path`` / ``ffprobe_path``).
2. The system ``PATH`` (a Homebrew, apt, or manual install).
3. ``static-ffmpeg``, which downloads a bundled build from GitHub on first use.

A system binary is preferred over the ``static-ffmpeg`` download so an install
that already has ffmpeg never depends on fetching binaries over the network, and
so a transient download failure does not break video nodes when a usable ffmpeg
is already present.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from functools import cache

from static_ffmpeg import run as static_ffmpeg_run

logger = logging.getLogger("griptape_nodes")


@dataclass(frozen=True)
class FFmpegBinaries:
    """Absolute paths to the resolved ffmpeg and ffprobe executables."""

    ffmpeg: str
    ffprobe: str


def resolve_ffmpeg_binaries() -> FFmpegBinaries:
    """Resolve ffmpeg/ffprobe, preferring configured paths, then PATH, then static-ffmpeg.

    Reads the ``ffmpeg_path`` and ``ffprobe_path`` settings and delegates to the
    cached resolver so repeated calls do not re-scan PATH or re-trigger a
    static-ffmpeg download.

    Raises:
        FileNotFoundError: If a configured path is not an executable, or if no
            binary is found and the static-ffmpeg download fails.
    """
    # Lazy import: ffmpeg_utils is a leaf util imported by the artifact providers,
    # so reaching GriptapeNodes/ConfigManager or the settings keys at module
    # top-level would create a cycle (ffmpeg_utils <- artifact providers <-
    # settings <- ffmpeg_utils). file_utils.py breaks the same cycle the same way.
    from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
    from griptape_nodes.retained_mode.managers.settings import FFMPEG_PATH_KEY, FFPROBE_PATH_KEY

    config_manager = GriptapeNodes.ConfigManager()
    configured_ffmpeg = config_manager.get_config_value(FFMPEG_PATH_KEY)
    configured_ffprobe = config_manager.get_config_value(FFPROBE_PATH_KEY)
    return _resolve_ffmpeg_binaries(configured_ffmpeg, configured_ffprobe, FFMPEG_PATH_KEY, FFPROBE_PATH_KEY)


@cache
def _resolve_ffmpeg_binaries(
    configured_ffmpeg: str | None,
    configured_ffprobe: str | None,
    ffmpeg_key: str,
    ffprobe_key: str,
) -> FFmpegBinaries:
    """Resolve both binaries from the given configured paths, PATH, then static-ffmpeg.

    The setting-key names are passed in rather than imported so this function
    stays free of the settings import that would re-introduce a circular import,
    and so it is trivially testable without engine state.

    Cached on its arguments so a successful resolution is reused for the life of
    the process. Exceptions are not cached, so a transient static-ffmpeg download
    failure can be retried on the next call.

    Raises:
        FileNotFoundError: If a configured path is not an executable, or if no
            binary is found and the static-ffmpeg download fails.
    """
    # FAILURE CASE: a configured path is set but does not point at an executable.
    ffmpeg = _resolve_configured_binary(configured_ffmpeg, "ffmpeg", ffmpeg_key)
    ffprobe = _resolve_configured_binary(configured_ffprobe, "ffprobe", ffprobe_key)

    # Prefer a system binary on PATH for anything not pinned in settings.
    if ffmpeg is None:
        ffmpeg = shutil.which("ffmpeg")
    if ffprobe is None:
        ffprobe = shutil.which("ffprobe")

    # FAILURE CASE: still missing a binary, so fall back to the static-ffmpeg download.
    if ffmpeg is None or ffprobe is None:
        try:
            static_ffmpeg_path, static_ffprobe_path = static_ffmpeg_run.get_or_fetch_platform_executables_else_raise()
        except Exception as e:
            msg = (
                "Attempted to download ffmpeg via static-ffmpeg because no ffmpeg/ffprobe was found on PATH "
                f"or in settings. Failed because: {e}. Install ffmpeg (for example 'brew install ffmpeg') and "
                f"make sure it is on your PATH, or set the '{ffmpeg_key}' and '{ffprobe_key}' settings."
            )
            raise FileNotFoundError(msg) from e
        if ffmpeg is None:
            ffmpeg = static_ffmpeg_path
        if ffprobe is None:
            ffprobe = static_ffprobe_path

    logger.debug("Resolved ffmpeg=%s ffprobe=%s", ffmpeg, ffprobe)
    return FFmpegBinaries(ffmpeg=ffmpeg, ffprobe=ffprobe)


def _resolve_configured_binary(configured_path: str | None, binary_name: str, config_key: str) -> str | None:
    """Return the executable for an explicitly configured path, or None if unset.

    Raises:
        FileNotFoundError: If a path is configured but does not resolve to an
            executable file.
    """
    if not configured_path:
        return None

    resolved = shutil.which(configured_path)
    # FAILURE CASE: configured but not an executable file.
    if resolved is None:
        msg = (
            f"Attempted to locate the {binary_name} binary at the configured path '{configured_path}' "
            f"(setting '{config_key}'). Failed because it is not an executable file. Point the setting at a "
            "valid executable, or clear it to auto-detect one on PATH."
        )
        raise FileNotFoundError(msg)
    return resolved
