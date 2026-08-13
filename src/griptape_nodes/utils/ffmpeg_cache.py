r"""Redirect `static_ffmpeg`'s lock and download directory to a writable location.

`static_ffmpeg` resolves ffmpeg/ffprobe by writing next to its own installed package
files, with no environment variable or public setter to move them:

    SELF_DIR  = os.path.abspath(os.path.dirname(__file__))   # static_ffmpeg/run.py:22
    LOCK_FILE = os.path.join(SELF_DIR, "lock.file")          # static_ffmpeg/run.py:23

That breaks any packaging where `site-packages` is read-only. The Linux desktop AppImage
runs as a FUSE-mounted squashfs (`/tmp/.mount_<...>/`), so every ffmpeg-dependent feature
fails with `OSError: [Errno 30] Read-only file system` before ffmpeg is ever resolved.

Passing `download_dir=` to `get_or_fetch_platform_executables_else_raise()` does not help:
the lock is acquired at `run.py:102`, before `download_dir` is consulted at `run.py:121`.
The lock is what raises. So the module globals themselves have to move.

Reassigning them is safe because both are read at *call* time, not import time:
`LOCK_FILE` inside `get_or_fetch_platform_executables_else_raise()` (`run.py:102`), and
`SELF_DIR` inside `get_platform_dir()` (`run.py:72`). Redirecting `SELF_DIR` moves the
download and extract target along with the lock, and lets `get_platform_dir()` keep
deriving the platform subdirectory itself. Re-verify those two line references when
bumping `static_ffmpeg`.

One redirect covers every caller in the process. The engine imports `static_ffmpeg` at
boot (`artifact_manager` -> `VideoArtifactProvider`), so `sys.modules["static_ffmpeg"]`
is pinned to the engine's own copy and every node library that imports `static_ffmpeg`
receives that same module object -- including libraries whose venvs live elsewhere.
"""

import logging
from pathlib import Path

import static_ffmpeg.run
from xdg_base_dirs import xdg_data_home

logger = logging.getLogger("griptape_nodes")


def resolve_ffmpeg_directory(configured_directory: str) -> Path:
    """Resolve the `ffmpeg_directory` config value to a concrete path.

    Args:
        configured_directory: The `ffmpeg_directory` config value. Empty means "use the
            default"; anything else is taken as an absolute path, with `~` expanded.

    Returns:
        The directory that should hold `lock.file` and the `bin/<platform>/` tree.
    """
    if configured_directory:
        return Path(configured_directory).expanduser()

    return xdg_data_home() / "griptape_nodes" / "ffmpeg"


def redirect_ffmpeg_cache(directory: Path) -> None:
    """Point `static_ffmpeg`'s lock file and downloaded binaries at `directory`.

    Safe to call repeatedly; the last call wins. Never raises -- if `directory` cannot be
    created, `static_ffmpeg` is left alone so behavior matches an unpatched engine rather
    than trading one failure for another.

    Args:
        directory: Writable directory to hold `lock.file` and the `bin/<platform>/` tree.
    """
    # FAILURE CASE: the target directory cannot be created, so redirecting into it would
    # only move the write error rather than fix it.
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning(
            "Attempted to redirect the ffmpeg cache to '%s'. Failed to create that directory (%s), "
            "so ffmpeg will keep resolving next to its installed package files. On read-only "
            "installations this leaves video features broken; set the 'ffmpeg_directory' config "
            "value to a writable path.",
            directory,
            e,
        )
        return

    static_ffmpeg.run.SELF_DIR = str(directory)
    static_ffmpeg.run.LOCK_FILE = str(directory / "lock.file")

    logger.debug("Redirected ffmpeg cache to %s", directory)
