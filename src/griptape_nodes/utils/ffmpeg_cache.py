r"""Control where `static_ffmpeg` finds ffmpeg/ffprobe: PATH first, then a writable cache or download.

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

`get_or_fetch_platform_executables_else_raise()` never looks at PATH: it downloads unless
the cache holds a completed install. That breaks networks that block the download even
when ffmpeg is installed system-wide. The same module-object pinning lets one replacement
of that function cover every caller: node libraries look it up on `static_ffmpeg.run` at
call time, and `static_ffmpeg._add_paths` bound its own copy at import, so both names are
replaced. The replacement prefers, in order: a completed install in an explicitly set
`ffmpeg_directory`, ffmpeg and ffprobe on PATH, then `static_ffmpeg`'s cache or download.
"""

import logging
import shutil
from pathlib import Path

import static_ffmpeg._add_paths
import static_ffmpeg.run

from griptape_nodes.utils.engine_dirs import engine_data_dir

logger = logging.getLogger("griptape_nodes")

_resolution_installed = False
_configured_install_wins = False
_logged_source: str | None = None

# Captured at import, before `prefer_system_ffmpeg` replaces the module attribute.
_fetch_platform_executables = static_ffmpeg.run.get_or_fetch_platform_executables_else_raise


def install_ffmpeg_resolution(configured_directory: str) -> None:
    """Redirect `static_ffmpeg`'s cache and let it fall back to ffmpeg on PATH, once per process.

    Both changes mutate process-wide state, so they are
    installed once and subsequent calls are no-ops. The first caller's configuration wins;
    building further engines in the same process must not move the cache out from under
    callers that already resolved ffmpeg.

    Args:
        configured_directory: The `ffmpeg_directory` config value; see
            `resolve_ffmpeg_directory`.
    """
    global _resolution_installed  # noqa: PLW0603

    if _resolution_installed:
        logger.debug("ffmpeg resolution already installed, skipping")
        return

    directory = resolve_ffmpeg_directory(configured_directory)
    redirect_ffmpeg_cache(directory)
    prefer_system_ffmpeg(configured_install_wins=directory != engine_data_dir() / "ffmpeg")
    _resolution_installed = True


def resolve_ffmpeg_directory(configured_directory: str) -> Path:
    """Resolve the `ffmpeg_directory` config value to a concrete path.

    Args:
        configured_directory: The `ffmpeg_directory` config value. Empty means "use the
            default". A non-empty value must be absolute; `~` is expanded first.

    Returns:
        The directory that should hold `lock.file` and the `bin/<platform>/` tree.
    """
    default_directory = engine_data_dir() / "ffmpeg"

    if not configured_directory:
        return default_directory

    configured_path = Path(configured_directory).expanduser()

    # FAILURE CASE: a relative value gets resolved against the process working directory, which
    # is not stable for the engine's lifetime. The download would land relative to the directory
    # the engine happened to boot in, and a later lookup from anywhere else would miss it and
    # download again. The setting is documented as absolute, so refuse rather than silently
    # pick a location that moves.
    if not configured_path.is_absolute():
        logger.warning(
            "The 'ffmpeg_directory' setting is '%s', which is not an absolute path. Using the "
            "default location '%s' instead. Set 'ffmpeg_directory' to an absolute path to choose "
            "where ffmpeg is stored.",
            configured_directory,
            default_directory,
        )
        return default_directory

    return configured_path


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


def prefer_system_ffmpeg(*, configured_install_wins: bool) -> None:
    """Make `static_ffmpeg` use ffmpeg and ffprobe from PATH instead of downloading them.

    Safe to call repeatedly; the replacement always wraps the original function.

    Args:
        configured_install_wins: Whether a completed install in the cache directory beats
            PATH. True when the user set `ffmpeg_directory`, since that is an explicit choice
            of binaries and PATH is ambient.
    """
    global _configured_install_wins  # noqa: PLW0603

    _configured_install_wins = configured_install_wins
    static_ffmpeg.run.get_or_fetch_platform_executables_else_raise = get_or_find_platform_executables
    static_ffmpeg._add_paths.get_or_fetch_platform_executables_else_raise = get_or_find_platform_executables  # pyright: ignore[reportPrivateImportUsage]


def get_or_find_platform_executables(
    fix_permissions: bool = True,  # noqa: FBT001, FBT002
    download_dir: str | None = None,
) -> tuple[str, str]:
    """Return ffmpeg and ffprobe paths, downloading them only when no local copy exists.

    Same signature as `static_ffmpeg.run.get_or_fetch_platform_executables_else_raise`,
    which it replaces. A caller that passes `download_dir` asked for that directory
    specifically, so it skips the PATH lookup.
    """
    if download_dir is not None:
        return _fetch_platform_executables(fix_permissions=fix_permissions, download_dir=download_dir)

    if _configured_install_wins and _has_cached_install():
        _log_source(f"Using ffmpeg from the 'ffmpeg_directory' setting: {static_ffmpeg.run.get_platform_dir()}")
        return _fetch_platform_executables(fix_permissions=fix_permissions)

    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if ffmpeg_path is None or ffprobe_path is None:
        _log_source(
            f"ffmpeg and ffprobe are not both on PATH. Using the copy in '{static_ffmpeg.run.SELF_DIR}', "
            "downloading it first if it is missing."
        )
        return _fetch_platform_executables(fix_permissions=fix_permissions)

    _log_source(f"Using ffmpeg '{ffmpeg_path}' and ffprobe '{ffprobe_path}' from PATH")
    return ffmpeg_path, ffprobe_path


def _has_cached_install() -> bool:
    """Whether the `static_ffmpeg` cache holds a completed install for this platform."""
    try:
        platform_dir = static_ffmpeg.run.get_platform_dir()
    except OSError:
        # `static_ffmpeg` has no build for this platform, so there is nothing cached.
        return False
    return (Path(platform_dir) / "installed.crumb").exists()


def _log_source(message: str) -> None:
    """Log which ffmpeg is used at INFO, once per change, since callers resolve it on every run."""
    global _logged_source  # noqa: PLW0603

    if message == _logged_source:
        return
    _logged_source = message
    logger.info(message)
