"""Unit tests for ffmpeg_cache module."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
import static_ffmpeg.run

# The lock class `get_or_fetch_platform_executables_else_raise` constructs at
# `static_ffmpeg/run.py:102`. Imported from its own package rather than through
# `static_ffmpeg.run` because that module does not re-export it.
from filelock import FileLock
from xdg_base_dirs import xdg_data_home

from griptape_nodes.utils import ffmpeg_cache
from griptape_nodes.utils.ffmpeg_cache import (
    install_ffmpeg_cache_redirect,
    redirect_ffmpeg_cache,
    resolve_ffmpeg_directory,
)

# The lock is uncontended in these tests, so any wait at all means something is wrong. Fail fast
# instead of hanging the suite.
LOCK_TIMEOUT_SECONDS = 5


@pytest.fixture(autouse=True)
def _restore_static_ffmpeg_globals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore `static_ffmpeg`'s module globals and the install-once flag after each test.

    `redirect_ffmpeg_cache` mutates process-wide state. Leaking it would change where
    unrelated tests in the same session look for ffmpeg. `_redirect_installed` is forced
    to `False` so every test starts from an uninstalled state, even when an engine built
    elsewhere in the session already performed the process-wide install.
    """
    monkeypatch.setattr(static_ffmpeg.run, "SELF_DIR", static_ffmpeg.run.SELF_DIR)
    monkeypatch.setattr(static_ffmpeg.run, "LOCK_FILE", static_ffmpeg.run.LOCK_FILE)
    monkeypatch.setattr(ffmpeg_cache, "_redirect_installed", False)


class TestResolveFfmpegDirectory:
    def test_empty_uses_xdg_data_home(self) -> None:
        assert resolve_ffmpeg_directory("") == xdg_data_home() / "griptape_nodes" / "ffmpeg"

    def test_configured_path_used_as_is(self, tmp_path: Path) -> None:
        assert resolve_ffmpeg_directory(str(tmp_path)) == tmp_path

    def test_tilde_expanded(self) -> None:
        assert resolve_ffmpeg_directory("~/ffmpeg") == Path.home() / "ffmpeg"

    def test_relative_value_falls_back_to_the_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A relative value is refused, not resolved against the working directory.

        Unlike the sibling File System settings this one is machine-scoped, so there is no
        base directory to make a relative value meaningful. Honoring it would tie the cache
        to whichever directory the engine booted in: the download would land there and any
        later lookup from elsewhere would miss it and download again.
        """
        monkeypatch.chdir(tmp_path)

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            resolved = resolve_ffmpeg_directory("ffmpeg-cache")

        assert resolved == xdg_data_home() / "griptape_nodes" / "ffmpeg"
        assert "absolute" in caplog.text


class TestRedirectFfmpegCache:
    def test_redirects_lock_file(self, tmp_path: Path) -> None:
        redirect_ffmpeg_cache(tmp_path)

        assert str(tmp_path / "lock.file") == static_ffmpeg.run.LOCK_FILE

    def test_redirects_download_target(self, tmp_path: Path) -> None:
        """The regression assertion: the binaries move, not just the lock.

        `get_platform_dir()` derives its path from `SELF_DIR` at call time, so redirecting
        `SELF_DIR` is what keeps the download and extract out of the read-only package dir.
        """
        redirect_ffmpeg_cache(tmp_path)

        assert Path(static_ffmpeg.run.get_platform_dir()).is_relative_to(tmp_path)

    def test_creates_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "ffmpeg"

        redirect_ffmpeg_cache(target)

        assert target.is_dir()

    def test_idempotent(self, tmp_path: Path) -> None:
        redirect_ffmpeg_cache(tmp_path)
        first = (static_ffmpeg.run.SELF_DIR, static_ffmpeg.run.LOCK_FILE)

        redirect_ffmpeg_cache(tmp_path)

        assert first == (static_ffmpeg.run.SELF_DIR, static_ffmpeg.run.LOCK_FILE)

    def test_last_call_wins(self, tmp_path: Path) -> None:
        redirect_ffmpeg_cache(tmp_path / "first")
        redirect_ffmpeg_cache(tmp_path / "second")

        assert str(tmp_path / "second") == static_ffmpeg.run.SELF_DIR

    def test_unwritable_directory_leaves_globals_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A read-only target must not be redirected into, or we trade one write error for another."""
        original = (static_ffmpeg.run.SELF_DIR, static_ffmpeg.run.LOCK_FILE)

        def _read_only_mkdir(*_args: object, **_kwargs: object) -> None:
            raise OSError(30, "Read-only file system")

        monkeypatch.setattr(Path, "mkdir", _read_only_mkdir)

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            redirect_ffmpeg_cache(tmp_path / "read-only")

        assert original == (static_ffmpeg.run.SELF_DIR, static_ffmpeg.run.LOCK_FILE)
        assert "ffmpeg_directory" in caplog.text

    def test_unwritable_directory_does_not_raise(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _read_only_mkdir(*_args: object, **_kwargs: object) -> None:
            raise OSError(30, "Read-only file system")

        monkeypatch.setattr(Path, "mkdir", _read_only_mkdir)

        redirect_ffmpeg_cache(tmp_path / "read-only")


class TestInstallFfmpegCacheRedirect:
    def test_first_call_redirects(self, tmp_path: Path) -> None:
        install_ffmpeg_cache_redirect(str(tmp_path))

        assert str(tmp_path) == static_ffmpeg.run.SELF_DIR

    def test_second_call_is_a_noop(self, tmp_path: Path) -> None:
        """The redirect is installed once per process; a later engine must not move the cache.

        `redirect_ffmpeg_cache` itself is last-call-wins, so this pins the install-once
        guard specifically: the second call's differing directory is ignored, and nothing
        is created for it.
        """
        install_ffmpeg_cache_redirect(str(tmp_path / "first"))

        install_ffmpeg_cache_redirect(str(tmp_path / "second"))

        assert str(tmp_path / "first") == static_ffmpeg.run.SELF_DIR
        assert not (tmp_path / "second").exists()


class TestPackageDirectoryIsNeverWritten:
    """The defect in desktop#395 was a *write into `static_ffmpeg`'s own package directory*.

    A read-only filesystem was only how that write became visible. Where the write lands is
    observable on every platform, so the first test below pins the actual defect rather than
    the symptom -- no AppImage, no FUSE mount and no Linux host required. The second
    reproduces the failure itself, and only runs where a directory can genuinely be made
    unwritable.

    Both drive `filelock.FileLock`, the class
    `get_or_fetch_platform_executables_else_raise` constructs at `static_ffmpeg/run.py:102`,
    rather than a stand-in for it.
    """

    def test_locking_lands_outside_the_package_directory(self, tmp_path: Path) -> None:
        """Taking the real lock must create it under the redirect target, not in `site-packages`.

        Asserts on the file the lock actually creates rather than on the state of
        `static_ffmpeg`'s package directory, which cannot be asserted on at all: the suite
        runs under `pytest -n auto`, and `test_ffmpeg_preview_generator` resolves ffmpeg for
        real at module import, dropping a `lock.file` in that shared directory at a moment
        this test cannot predict. `filelock` then leaves the file behind on POSIX -- only the
        Windows implementation unlinks on release -- so its presence there is evidence of
        nothing.
        """
        package_dir = Path(static_ffmpeg.run.__file__).parent

        redirect_ffmpeg_cache(tmp_path)

        # Asserted while the lock is held, for the same release-behavior difference.
        with FileLock(static_ffmpeg.run.LOCK_FILE, timeout=LOCK_TIMEOUT_SECONDS):
            held_lock = Path(static_ffmpeg.run.LOCK_FILE)
            assert held_lock.exists()
            assert held_lock.is_relative_to(tmp_path)
            assert not held_lock.is_relative_to(package_dir)

    @pytest.mark.skipif(os.name != "posix", reason="needs POSIX permission bits to make a directory unwritable")
    @pytest.mark.skipif(os.name == "posix" and os.geteuid() == 0, reason="root bypasses permission bits")
    def test_redirect_rescues_an_unwritable_package_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end against a real unwritable directory: broken before the redirect, working after.

        The closest reproduction of desktop#395 that does not need an AppImage. The errno
        differs -- `EACCES` from permission bits here, so `PermissionError`, against `EROFS`
        and a plain `OSError` from the FUSE-mounted squashfs in the bug report -- but the
        refused operation is the same one: creating `lock.file` inside a directory the
        process may not write to. Both errnos were confirmed by hand on Linux as uid 1000.

        Skipped as root, which ignores permission bits and would make this pass without
        proving anything. That is also why checking this by hand in a default Docker
        container is misleading, and why a read-only bind mount is needed there instead.
        """
        unwritable_package_dir = tmp_path / "site-packages" / "static_ffmpeg"
        unwritable_package_dir.mkdir(parents=True)
        unwritable_package_dir.chmod(0o500)
        monkeypatch.setattr(static_ffmpeg.run, "SELF_DIR", str(unwritable_package_dir))
        monkeypatch.setattr(static_ffmpeg.run, "LOCK_FILE", str(unwritable_package_dir / "lock.file"))
        writable_directory = tmp_path / "writable"

        try:
            with (
                pytest.raises(PermissionError),
                FileLock(static_ffmpeg.run.LOCK_FILE, timeout=LOCK_TIMEOUT_SECONDS),
            ):
                pass

            redirect_ffmpeg_cache(writable_directory)

            with FileLock(static_ffmpeg.run.LOCK_FILE, timeout=LOCK_TIMEOUT_SECONDS):
                assert Path(static_ffmpeg.run.LOCK_FILE).is_relative_to(writable_directory)
        finally:
            # Restore write permission so pytest can tear the tmp_path down.
            unwritable_package_dir.chmod(0o700)
